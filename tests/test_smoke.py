"""Smoke tests: all main pages return 200 with a mocked authenticated session."""
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.main import app
from app.models import User


def _mock_user():
    return User(id=1, google_id="test_google_id", email="test@example.com", display_name="Test User")


@pytest.fixture(autouse=True)
def override_auth():
    """Replace get_current_user with a stub that always returns a valid user."""
    app.dependency_overrides[get_current_user] = _mock_user
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_dashboard_returns_200(client):
    r = client.get("/dashboard")
    assert r.status_code == 200


def test_upload_page_returns_200(client):
    r = client.get("/upload")
    assert r.status_code == 200


def test_review_page_returns_200(client):
    r = client.get("/review")
    assert r.status_code == 200


def test_transactions_page_returns_200(client):
    r = client.get("/transactions")
    assert r.status_code == 200


def test_balances_page_returns_200(client):
    r = client.get("/balances")
    assert r.status_code == 200


def test_login_page_returns_200(client):
    # Login page should be accessible without auth
    r = client.get("/login", follow_redirects=False)
    # If already "logged in" (dependency overridden), it redirects to /dashboard
    assert r.status_code in (200, 302)


def test_health_check(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
