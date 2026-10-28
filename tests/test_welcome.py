from __future__ import annotations

from html.parser import HTMLParser

from fastapi.testclient import TestClient

import main


class WelcomeMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = []
        self.links = []
        self._active_link = None
        self._textarea = False
        self.textarea_content = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"input", "textarea", "select", "form", "button"} or attributes.get("role") in {"textbox", "searchbox", "combobox"}:
            self.controls.append(tag)
        if tag == "a":
            self._active_link = {"href": attributes.get("href"), "text": ""}
            self.links.append(self._active_link)
        if tag == "textarea":
            self._textarea = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._active_link = None
        if tag == "textarea":
            self._textarea = False

    def handle_data(self, data):
        if self._active_link is not None:
            self._active_link["text"] += data
        if self._textarea:
            self.textarea_content.append(data)


def test_root_serves_input_free_welcome_with_one_analyzer_action():
    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "See what your SQL" in response.text
    assert "UNDERSTAND YOUR DATABASE" in response.text
    assert "From query to execution plan to insight." in response.text
    assert "welcome-lens.svg" in response.text
    assert "welcome-lens-mobile.svg" in response.text
    assert not any(label in response.text.casefold() for label in ("table scan", "aggregate", "join", "sort", "filter"))

    parser = WelcomeMarkupParser()
    parser.feed(response.text)
    assert parser.controls == []
    assert parser.links == [{"href": "/index.html", "text": "Enter QueryLens →"}]


def test_analyzer_is_directly_loadable_with_an_empty_sql_editor():
    response = TestClient(main.app).get("/index.html")

    assert response.status_code == 200
    parser = WelcomeMarkupParser()
    parser.feed(response.text)
    assert parser.textarea_content == []
    assert 'id="btn-run-query"' in response.text
    assert 'id="warnings-list"' in response.text
    assert 'id="ai-panel"' in response.text
    assert 'src="app.js"' in response.text


def test_welcome_lens_assets_are_served_as_local_svg_files():
    client = TestClient(main.app)

    for asset in ("/assets/welcome-lens.svg", "/assets/welcome-lens-mobile.svg"):
        response = client.get(asset)
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers["content-type"]


def test_refresh_and_existing_health_api_route_still_work(monkeypatch):
    client = TestClient(main.app)
    first = client.get("/")
    refreshed = client.get("/")

    class DatabaseAuthError(RuntimeError):
        errno = 1045

    def fail_authentication():
        raise DatabaseAuthError("test database is unavailable")

    monkeypatch.setattr(main, "get_connection", fail_authentication)
    health = client.get("/api/health")

    assert first.status_code == refreshed.status_code == 200
    assert health.status_code == 200
    assert health.json() == {"database": "auth failed"}
