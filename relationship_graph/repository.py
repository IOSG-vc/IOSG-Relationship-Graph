from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .models import Edge, Node, QueryKind


class GraphRepository(Protocol):
    def resolve(self, query: str, kind: QueryKind) -> list[Node]: ...
    def nodes(self) -> list[Node]: ...
    def edges(self) -> list[Edge]: ...


def normalize_handle(value: str) -> str:
    value = value.strip().lower()
    for prefix in ("https://x.com/", "http://x.com/", "https://twitter.com/", "http://twitter.com/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.strip("/@ ").split("?")[0]


def infer_kind(query: str) -> QueryKind:
    clean = query.strip()
    if clean.startswith("@") or "x.com/" in clean.lower() or "twitter.com/" in clean.lower():
        return QueryKind.PROJECT_X
    if "." in clean and " " not in clean:
        return QueryKind.DOMAIN
    return QueryKind.COMPANY_NAME


class JsonGraphRepository:
    """Local adapter and executable data contract for future source sync jobs."""

    def __init__(self, path: str | Path):
        payload = json.loads(Path(path).read_text())
        self._nodes = [Node.model_validate(item) for item in payload["nodes"]]
        self._edges = [Edge.model_validate(item) for item in payload["edges"]]

    def nodes(self) -> list[Node]:
        return self._nodes

    def edges(self) -> list[Edge]:
        return self._edges

    def resolve(self, query: str, kind: QueryKind) -> list[Node]:
        requested_kind = kind
        actual_kind = infer_kind(query) if kind == QueryKind.AUTO else kind
        needle = query.strip().lower()
        handle = normalize_handle(query)
        matches: list[Node] = []
        for node in self._nodes:
            aliases = [str(value).lower() for value in node.metadata.get("aliases", [])]
            domain = str(node.metadata.get("domain", "")).lower().removeprefix("https://").removeprefix("http://").strip("/")
            if actual_kind == QueryKind.DOMAIN and domain == needle.removeprefix("https://").removeprefix("http://").strip("/"):
                matches.append(node)
            elif actual_kind in {QueryKind.PROJECT_X, QueryKind.FOUNDER_X} and normalize_handle(node.x_handle or "") == handle:
                matches.append(node)
            elif actual_kind == QueryKind.COMPANY_NAME and (node.label.lower() == needle or needle in aliases):
                matches.append(node)

        # A founder handle resolves to its company. In auto mode, node kind disambiguates
        # founder handles from project handles without an extra caller-side switch.
        founder_matches = [node for node in matches if node.kind == "person"]
        if matches and (actual_kind == QueryKind.FOUNDER_X or (requested_kind == QueryKind.AUTO and founder_matches)):
            founder_ids = {node.id for node in founder_matches or matches}
            company_ids = {
                edge.target if edge.source in founder_ids else edge.source
                for edge in self._edges
                if edge.relationship in {"founder_of", "employee_of"}
                and (edge.source in founder_ids or edge.target in founder_ids)
            }
            return [node for node in self._nodes if node.id in company_ids]
        return matches
