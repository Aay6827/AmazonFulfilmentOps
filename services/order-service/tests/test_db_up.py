from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_db_up_gauge_is_exposed_and_reports_healthy():
    text = client.get("/metrics").text
    assert "db_up 1.0" in text
