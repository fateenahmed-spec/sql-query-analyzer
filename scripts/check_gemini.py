"""Check Gemini API reachability without exposing the configured API key."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _safe_proxy_value(value: str) -> str:
    parts = urlsplit(value)
    if parts.username is None and parts.password is None:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"[REDACTED]@{host}", parts.path, parts.query, parts.fragment))


def main() -> int:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    for name in ("HTTPS_PROXY", "HTTP_PROXY"):
        value = os.getenv(name)
        if value:
            print(f"{name}={_safe_proxy_value(value)}")

    if not api_key:
        print("GEMINI_API_KEY is not set")
        return 1

    try:
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=(5, 30),
        )
        print(f"Gemini models status: {response.status_code}")
        return 0 if response.ok else 1
    except requests.RequestException as exc:
        print(f"Gemini models request failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
