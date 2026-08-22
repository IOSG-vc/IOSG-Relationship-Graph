from __future__ import annotations

import heapq
import math
from collections import defaultdict

from .models import Edge, PathResult, QueryKind, SearchResponse
from .repository import GraphRepository

MAX_HOPS = 4
FOLLOW_RELATIONSHIPS = {"x_follow"}


def _confidence(score: int) -> str:
    return "high" if score >= 80 else "medium" if score >= 55 else "low"


def _next_action(contact: str, edges: list[Edge]) -> str:
    if any(edge.relationship == "referral" for edge in edges):
        return f"Ask {contact} to reactivate the recorded referral."
    if all(edge.relationship not in FOLLOW_RELATIONSHIPS for edge in edges):
        return f"Ask {contact} to validate the relationship context, then request the introduction."
    return f"Ask {contact} whether the X connection is genuinely warm before requesting an introduction."


class IntroductionPathService:
    def __init__(self, repository: GraphRepository):
        self.repository = repository

    def search(self, query: str, kind: QueryKind = QueryKind.AUTO, limit: int = 5) -> SearchResponse:
        matches = self.repository.resolve(query, kind)
        nodes = {node.id: node for node in self.repository.nodes()}
        if not matches:
            return SearchResponse(status="not_found", query=query, diagnostics={"matched_targets": 0})
        target = matches[0]
        edges = [edge for edge in self.repository.edges() if not edge.private]
        adjacency: dict[str, list[tuple[str, Edge]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.source].append((edge.target, edge))
            adjacency[edge.target].append((edge.source, edge))

        starts = [node for node in nodes.values() if node.kind == "iosg_member"]
        candidates: list[tuple[float, list[str], list[Edge]]] = []
        for start in starts:
            queue: list[tuple[float, str, list[str], list[Edge]]] = [(0.0, start.id, [start.id], [])]
            while queue:
                cost, current, visited, path_edges = heapq.heappop(queue)
                if current == target.id and path_edges:
                    candidates.append((cost, visited, path_edges))
                    continue
                if len(path_edges) >= MAX_HOPS:
                    continue
                for neighbor, edge in adjacency.get(current, []):
                    if neighbor in visited:
                        continue
                    # Product of edge confidence expressed as additive negative log cost.
                    edge_cost = -math.log(max(edge.confidence, 0.01)) + 0.08
                    heapq.heappush(queue, (cost + edge_cost, neighbor, visited + [neighbor], path_edges + [edge]))

        candidates.sort(key=lambda item: (item[0], len(item[2])))
        results: list[PathResult] = []
        seen: set[tuple[str, ...]] = set()
        for cost, node_ids, path_edges in candidates:
            key = tuple(node_ids)
            if key in seen:
                continue
            seen.add(key)
            reliability = math.prod(edge.confidence for edge in path_edges)
            score = round(100 * reliability * (0.96 ** max(0, len(path_edges) - 1)))
            contact = nodes[node_ids[0]].label
            results.append(PathResult(
                rank=len(results) + 1,
                score=score,
                confidence=_confidence(score),
                path=[nodes[node_id].label for node_id in node_ids],
                iosg_contact=contact,
                edges=path_edges,
                suggested_next_action=_next_action(contact, path_edges),
            ))
            if len(results) >= limit:
                break

        sources = sorted({edge.evidence_source for edge in edges})
        diagnostics = {
            "matched_targets": len(matches),
            "nodes_considered": len(nodes),
            "edges_considered": len(edges),
            "paths_found": len(results),
            "sources_present": sources,
            "source_coverage": {source: sum(edge.evidence_source == source for edge in edges) for source in sources},
            "warnings": ["X follows are weak signals and do not establish willingness to introduce."],
        }
        repository_diagnostics = getattr(self.repository, "diagnostics", None)
        if callable(repository_diagnostics):
            diagnostics.update(repository_diagnostics())
        return SearchResponse(
            status="ok" if results else "no_path",
            query=query,
            resolved_target=target,
            recommended=results[0] if results else None,
            alternatives=results[1:],
            diagnostics=diagnostics,
        )
