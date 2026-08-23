import json
from pathlib import Path

from fastapi.testclient import TestClient

from relationship_graph.api import app
from relationship_graph.engine import IntroductionPathService, _diverse_results
from relationship_graph.models import Edge, PathResult, QueryKind
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
    response = TestClient(app).post("/v1/introduction-paths", json={"query": "Project"})
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
    assert "API access settings" not in response.text
    assert 'id="apiKey"' not in response.text


def test_api_does_not_require_app_level_key(monkeypatch):
    monkeypatch.setenv("RELATIONSHIP_GRAPH_API_KEY", "legacy-deployment-key")
    monkeypatch.setenv("RELATIONSHIP_GRAPH_BACKEND", "fixture")
    monkeypatch.setenv("RELATIONSHIP_GRAPH_DATA", str(FIXTURE))
    response = TestClient(app).post("/v1/introduction-paths", json={"query": "Project"})
    assert response.status_code == 200


def test_vercel_function_allows_live_search_duration():
    config = json.loads((FIXTURE.parent.parent / "vercel.json").read_text())
    assert config["functions"]["api/index.py"]["maxDuration"] >= 60


def test_neon_driver_is_a_runtime_dependency():
    project = (FIXTURE.parent.parent / "pyproject.toml").read_text()
    dependencies = project.split("[project.optional-dependencies]", 1)[0]
    assert "psycopg2-binary" in dependencies


def test_visible_results_preserve_connection_type_diversity():
    def path(rank, relationship, score):
        return PathResult(
            rank=rank, score=score, confidence="medium", path=["IOSG", "Contact", "Target"],
            iosg_contact="IOSG", suggested_next_action="Validate", edges=[Edge(
                id=f"edge-{rank}", source="iosg", target="target", relationship=relationship,
                confidence=0.8, evidence="Test evidence", evidence_source="test",
            )],
        )

    candidates = [path(i, "invested_in", 100 - i) for i in range(1, 6)]
    candidates += [path(6, "x_follow", 40), path(7, "x_follow", 35)]
    selected = _diverse_results(candidates, 5)
    assert [edge.relationship for result in selected for edge in result.edges].count("invested_in") == 3
    assert [edge.relationship for result in selected for edge in result.edges].count("x_follow") == 2
    assert [result.rank for result in selected] == [1, 2, 3, 4, 5]
