from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .engine import IntroductionPathService
from .models import SearchRequest, SearchResponse
from .adapters.twenty import TwentyError, TwentyGraphRepository
from .adapters.enrichment import EnrichedGraphRepository, NeonFollowProvider, SurfClient
from .adapters.relationship_owner import RelationshipOwnerClient
from .repository import JsonGraphRepository

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent


def service() -> IntroductionPathService:
    backend = os.getenv("RELATIONSHIP_GRAPH_BACKEND", "fixture").lower()
    if backend == "twenty":
        relationship_url = os.getenv("RELATIONSHIP_API_BASE_URL", "")
        relationship_key = os.getenv("RELATIONSHIP_API_KEY", "")
        owner_provider = (
            RelationshipOwnerClient(relationship_url, relationship_key)
            if relationship_url and relationship_key else None
        )
        repository = TwentyGraphRepository(
            os.getenv("TWENTY_BASE_URL", ""), os.getenv("TWENTY_API_KEY", ""),
            owner_provider=owner_provider,
        )
        surf_key = os.getenv("SURF_API_KEY", "")
        database_url = os.getenv("NEON_DATABASE_URL") or os.getenv("DATABASE_URL", "")
        if surf_key or database_url:
            repository = EnrichedGraphRepository(
                repository,
                surf=SurfClient(surf_key) if surf_key else None,
                follows=NeonFollowProvider(database_url) if database_url else None,
            )
        return IntroductionPathService(repository)
    data_path = os.getenv("RELATIONSHIP_GRAPH_DATA", str(ROOT / "fixtures" / "demo_graph.json"))
    return IntroductionPathService(JsonGraphRepository(data_path))


app = FastAPI(title="IOSG Relationship Graph", version="0.1.0")


@app.get("/", include_in_schema=False)
def web_app() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    backend = os.getenv("RELATIONSHIP_GRAPH_BACKEND", "fixture").lower()
    if backend == "twenty" and not (os.getenv("TWENTY_BASE_URL") and os.getenv("TWENTY_API_KEY")):
        raise HTTPException(status_code=503, detail="Twenty backend is not configured")
    if backend == "fixture":
        data_path = Path(os.getenv("RELATIONSHIP_GRAPH_DATA", str(ROOT / "fixtures" / "demo_graph.json")))
        if not data_path.exists():
            raise HTTPException(status_code=503, detail="Fixture graph is unavailable")
    return {"status": "ready", "backend": backend}


@app.get("/v1/diagnostics/sources")
def source_diagnostics() -> dict[str, object]:
    backend = os.getenv("RELATIONSHIP_GRAPH_BACKEND", "fixture").lower()
    database_url = os.getenv("NEON_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    sources: dict[str, object] = {
        "twenty": {"status": "configured" if os.getenv("TWENTY_BASE_URL") and os.getenv("TWENTY_API_KEY") else "not_configured"},
        "surf": {"status": "configured" if os.getenv("SURF_API_KEY") else "not_configured"},
        "sorsa": {"status": "cached_in_neon" if database_url else "not_configured"},
        "relationship_api": {"status": "configured" if os.getenv("RELATIONSHIP_API_BASE_URL") and os.getenv("RELATIONSHIP_API_KEY") else "not_configured"},
    }
    if database_url:
        try:
            sources["neon"] = NeonFollowProvider(database_url).sync_status()
            if isinstance(sources["neon"], dict):
                sources["sorsa"] = {"status": "cached_in_neon", **sources["neon"]}
        except Exception as exc:  # noqa: BLE001
            sources["neon"] = {"status": "error", "error": type(exc).__name__}
    else:
        sources["neon"] = {"status": "not_configured"}
    return {"status": "ok", "backend": backend, "sources": sources}


@app.post("/v1/introduction-paths", response_model=SearchResponse)
def introduction_paths(request: SearchRequest) -> SearchResponse:
    try:
        return service().search(request.query, request.kind, request.limit)
    except TwentyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
