"""All backend tests must enter through the isolated test launcher."""

import os

import pytest
from fastapi.testclient import TestClient as RawTestClient

if os.environ.get("SOULSEED_ENVIRONMENT") != "testing":
    pytest.exit("Backend tests require: python backend/scripts/run_isolated.py tests", returncode=4)

try:
    from app.config import get_settings
    TEST_SETTINGS = get_settings()
except Exception as exc:
    pytest.exit(f"Backend test isolation configuration is invalid ({type(exc).__name__})", returncode=4)

TEST_PAIRING_SECRET = TEST_SETTINGS.local_pairing_secret


@pytest.fixture(autouse=True)
def local_pairing_settings(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "local_pairing_secret", TEST_PAIRING_SECRET)


class PairedTestClient(RawTestClient):
    def __init__(self, app, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1:8001")
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        super().__init__(app, **kwargs)
        self.needs_pairing = any(getattr(route, "path", None) == "/api/auth/session" for route in app.routes)

    def request(self, method, url, **kwargs):
        if self.needs_pairing and "/api/auth/" not in str(url):
            self.needs_pairing = False
            paired = super().request("POST", "/api/auth/session", json={"secret": TEST_PAIRING_SECRET})
            assert paired.status_code == 200, paired.text
        return super().request(method, url, **kwargs)
