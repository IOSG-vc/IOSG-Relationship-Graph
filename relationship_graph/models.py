from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class QueryKind(StrEnum):
    AUTO = "auto"
    COMPANY_NAME = "company_name"
    DOMAIN = "domain"
    PROJECT_X = "project_x"
    FOUNDER_X = "founder_x"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=255)
    kind: QueryKind = QueryKind.AUTO
    limit: int = Field(default=5, ge=1, le=20)


class Node(BaseModel):
    id: str
    label: str
    kind: str
    x_handle: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    id: str
    source: str
    target: str
    relationship: str
    confidence: float = Field(ge=0, le=1)
    evidence: str
    evidence_source: str
    observed_at: str | None = None
    private: bool = False


class PathResult(BaseModel):
    rank: int
    score: int
    confidence: str
    path: list[str]
    iosg_contact: str
    edges: list[Edge]
    suggested_next_action: str


class OutreachContext(BaseModel):
    id: str
    title: str
    why_now: str
    signal_type: str
    timestamp: int
    sources: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    status: str
    query: str
    resolved_target: Node | None = None
    recommended: PathResult | None = None
    alternatives: list[PathResult] = Field(default_factory=list)
    outreach_context: list[OutreachContext] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
