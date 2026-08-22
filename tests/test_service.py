import os
from pathlib import Path

from fastapi.testclient import TestClient

from relationship_graph.api import app
from relationship_graph.engine import IntroductionPathService
from relationship_graph.models import QueryKind
from relationship_graph.repository import JsonGraphRepository

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo_graph.json"


def test_ranks_strong_interactions_above_x_follow():
    result = IntroductionPathService(JsonGraphRepository(FIXTURE)).search("Project")
    assert result.status == "ok"
    assert result.recommended.iosg_contact == "Darko"
    assert result.recommended.path == ["Darko", "Alex Advisor", "Project"]
    assert result.alternatives[0].path == ["Jocy", "@founder", "Project"]
    assert result.alternatives[0].confidence == "low"
    assert all(edge.evidence and edge.evidence_source for edge in result.recommended.edges)


def test_all_supported_query_forms_resolve():
    service = IntroductionPathService(JsonGraphRepository(FIXTURE))
    assert service.search("project.xyz").resolved_target.label == "Project"
    assert service.search("@projectx").resolved_target.label == "Project"
    assert service.search("@founder").resolved_target.label == "Project"
    assert service.search("@founder", QueryKind.FOUNDER_X).resolved_target.label == "Project"


def test_api_end_to_end(monkeypatch):
    monkeypatch.setenv("RELATIONSHIP_GRAPH_BACKEND", "fixture")
    monkeypatch.setenv("RELATIONSHIP_GRAPH_DATA", str(FIXTURE))
    key = os.getenv("RELATIONSHIP_GRAPH_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = TestClient(app).post(
        "/v1/introduction-paths", json={"query": "Project"}, headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended"]["iosg_contact"] == "Darko"
    assert payload["diagnostics"]["source_coverage"] == {"sorsa": 1, "surf": 1, "twenty": 2}


def test_readiness_for_fixture(monkeypatch):
    monkeypatch.setenv("RELATIONSHIP_GRAPH_BACKEND", "fixture")
    monkeypatch.setenv("RELATIONSHIP_GRAPH_DATA", str(FIXTURE))
    response = TestClient(app).get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "backend": "fixture"}


def test_web_app_is_served():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "IOSG Relationship Graph" in response.text
    assert "Find paths" in response.text
