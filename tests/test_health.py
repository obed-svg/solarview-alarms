import pytest


@pytest.mark.django_db
def test_healthcheck_returns_ok(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthcheck_returns_unavailable_when_dependency_fails(client, monkeypatch):
    def fail():
        raise ConnectionError

    monkeypatch.setattr("config.urls.connection.ensure_connection", fail)

    response = client.get("/health/")

    assert response.status_code == 503
    assert response.json() == {"status": "error"}
