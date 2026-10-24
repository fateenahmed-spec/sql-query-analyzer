import json

import pytest
import requests

from core import ai


API_KEY = "test-secret-gemini-key"
QUERY = "SELECT * FROM orders WHERE amount > 500"
WARNINGS = [{"code": "FULL_SCAN", "message": "A table scan was selected."}]
TREE = {
    "id": "n1",
    "operator": "Table scan",
    "details": "Table scan on orders",
    "actual_rows": 10,
    "time_total_ms": 2.5,
    "children": [],
}
GOOD_RESPONSE_TEXT = json.dumps({
    "summary": "The orders scan can be improved.",
    "suggestions": ["Add a selective index."],
    "fix": "CREATE INDEX idx_orders_amount ON orders(amount);",
})


@pytest.fixture
def ai_setup(monkeypatch, mocker):
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    mocker.patch("core.ai._get_cached", return_value=None)
    mocker.patch("core.ai._store_cached")
    mocker.patch("core.ai.time.sleep")
    post = mocker.patch("core.ai.requests.post")
    return post


def _response(mocker, text=GOOD_RESPONSE_TEXT, status=200, *, candidates=True, finish="STOP"):
    body = {}
    if candidates:
        body["candidates"] = [{
            "finishReason": finish,
            "content": {"parts": [{"text": text}]},
        }]
    response = mocker.Mock(status_code=status)
    response.json.return_value = body
    return response


def _run_and_check_secret(caplog, capsys):
    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)
    response_body = json.dumps(result, ensure_ascii=False)
    output = capsys.readouterr()
    assert API_KEY not in response_body
    assert API_KEY not in caplog.text
    assert API_KEY not in output.out
    assert API_KEY not in output.err
    return result


def test_rest_success_sends_key_only_in_header(ai_setup, mocker, caplog, capsys):
    ai_setup.return_value = _response(mocker)

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "success"
    assert result["summary"] == "The orders scan can be improved."
    assert result["fix"].startswith("CREATE INDEX")
    args, kwargs = ai_setup.call_args
    assert args[0] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-test-model:generateContent"
    )
    assert API_KEY not in args[0]
    assert kwargs["headers"]["x-goog-api-key"] == API_KEY
    assert kwargs["timeout"] == (5, 30)
    assert API_KEY not in json.dumps(kwargs["json"])
    assert set(kwargs["json"]) == {"contents", "generationConfig"}
    prompt = kwargs["json"]["contents"][0]["parts"][0]["text"]
    data = json.loads(prompt.split("DATA:\n", 1)[1])
    assert set(data) == {"sql", "parsed_plan", "schema_info", "rule_flags"}
    assert data["schema_info"]["tables"] == ["orders"]


def test_retries_429_then_succeeds(ai_setup, mocker, caplog, capsys):
    ai_setup.side_effect = [
        _response(mocker, status=429),
        _response(mocker),
    ]

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "success"
    assert ai_setup.call_count == 2


def test_gemini_rate_limit_falls_back_after_retries(ai_setup, mocker, caplog, capsys):
    ai_setup.side_effect = [_response(mocker, status=429) for _ in range(3)]

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini rate limited"
    assert ai_setup.call_count == 3


def test_missing_gemini_key_uses_rule_fallback(monkeypatch, mocker):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    post = mocker.patch("core.ai.requests.post")

    result = ai.get_ai_explanation(QUERY, WARNINGS, tree_summary=TREE)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "API key missing"
    assert result["summary"]
    post.assert_not_called()


def test_retries_500_twice_then_falls_back(ai_setup, mocker, caplog, capsys):
    ai_setup.side_effect = [_response(mocker, status=500) for _ in range(3)]

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini service unavailable"
    assert ai_setup.call_count == 3


def test_malformed_model_json_falls_back(ai_setup, mocker, caplog, capsys):
    ai_setup.return_value = _response(mocker, text="{malformed")

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "invalid Gemini response"


@pytest.mark.parametrize(
    ("body", "expected_reason"),
    [
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "Gemini blocked the prompt"),
        ({}, "Gemini returned no candidates"),
    ],
)
def test_blocked_prompt_or_missing_candidates_falls_back(
    ai_setup, mocker, caplog, capsys, body, expected_reason
):
    response = mocker.Mock(status_code=200)
    response.json.return_value = body
    ai_setup.return_value = response

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == expected_reason


def test_non_stop_finish_reason_falls_back(ai_setup, mocker, caplog, capsys):
    ai_setup.return_value = _response(mocker, finish="MAX_TOKENS")

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini response incomplete"


def test_hallucinated_table_name_falls_back(ai_setup, mocker, caplog, capsys):
    invalid_fix = json.dumps({
        "summary": "Use the unknown table.",
        "suggestions": [],
        "fix": "SELECT * FROM invented_orders",
    })
    ai_setup.return_value = _response(mocker, text=invalid_fix)

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "AI referenced a table missing from the plan"


def test_winerror_10013_connection_error_falls_back(ai_setup, caplog, capsys):
    ai_setup.side_effect = requests.exceptions.ConnectionError(
        "[WinError 10013] Permission denied"
    )

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "network blocked"


def test_timeout_falls_back(ai_setup, caplog, capsys):
    ai_setup.side_effect = requests.exceptions.Timeout("timed out")

    result = _run_and_check_secret(caplog, capsys)

    assert result["ai_status"] == "fallback"
    assert result["reason"] == "Gemini request timed out"
