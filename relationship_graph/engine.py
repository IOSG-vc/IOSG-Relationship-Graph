from __future__ import annotations

import heapq
import math
from collections import defaultdict

from .models import Edge, Node, PathResult, QueryKind, SearchResponse
from .repository import GraphRepository

MAX_HOPS = 4
FOLLOW_RELATIONSHIPS = {"x_follow"}


def _path_family(result: PathResult) -> str:
    relationships = {edge.relationship for edge in result.edges}
    if "invested_in" in relationships:
        return "investor"
    if relationships & FOLLOW_RELATIONSHIPS:
        return "x_follow"
    if "referral" in relationships:
        return "referral"
    return "direct"


def _diverse_results(results: list[PathResult], limit: int) -> list[PathResult]:
    """Keep one evidence family from monopolizing the visible alternatives."""
    if limit < 5:
        selected = results[:limit]
    else:
        family_limit = 3
        selected = []
        family_counts: dict[str, int] = defaultdict(int)
        deferred: list[PathResult] = []
        for result in results:
            family = _path_family(result)
            if family_counts[family] >= family_limit:
                deferred.append(result)
                continue
            selected.append(result)
            family_counts[family] += 1
            if len(selected) >= limit:
                break
        if len(selected) < limit:
            selected.extend(deferred[:limit - len(selected)])
    for rank, result in enumerate(selected, 1):
        result.rank = rank
    return selected


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
        if not matches:
            return SearchResponse(status="not_found", query=query, diagnostics={"matched_targets": 0})
        target = matches[0]
        nodes, edges, results = self._rank(target)
        if not results:
            fallback = getattr(self.repository, "enrich_no_path", None)
            if callable(fallback):
                fallback(target)
                nodes, edges, results = self._rank(target)
        else:
            skipped = getattr(self.repository, "skip_no_path_enrichment", None)
            if callable(skipped):
                skipped()
        total_paths_found = len(results)
        results = _diverse_results(results, limit)

        sources = sorted({edge.evidence_source for edge in edges})
        diagnostics = {
            "matched_targets": len(matches),
            "nodes_considered": len(nodes),
            "edges_considered": len(edges),
            "paths_found": len(results),
            "total_paths_found": total_paths_found,
            "sources_present": sources,
            "source_coverage": {source: sum(edge.evidence_source == source for edge in edges) for source in sources},
            "warnings": ["X follows are weak signals and do not establish willingness to introduce."],
        }
        repository_diagnostics = getattr(self.repository, "diagnostics", None)
        if callable(repository_diagnostics):
            diagnostics.update(repository_diagnostics())
        recent_context = (diagnostics.get("sources") or {}).get("recent_context") or []
        return SearchResponse(
            status="ok" if results else "no_path",
            query=query,
            resolved_target=target,
            recommended=results[0] if results else None,
            alternatives=results[1:],
            outreach_context=recent_context,
            diagnostics=diagnostics,
        )

    def _rank(self, target: Node) -> tuple[dict[str, Node], list[Edge], list[PathResult]]:
        nodes = {node.id: node for node in self.repository.nodes()}
        edges = [edge for edge in self.repository.edges() if not edge.private]
        adjacency: dict[str, list[tuple[str, Edge]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.source].append((edge.target, edge))
            adjacency[edge.target].append((edge.source, edge))
        candidates: list[tuple[float, list[str], list[Edge]]] = []
        for start in (node for node in nodes.values() if node.kind == "iosg_member"):
            queue: list[tuple[float, str, list[str], list[Edge]]] = [(0.0, start.id, [start.id], [])]
            while queue:
                cost, current, visited, path_edges = heapq.heappop(queue)
                if current == target.id and path_edges:
                    candidates.append((cost, visited, path_edges))
                    continue
                if len(path_edges) >= MAX_HOPS:
                    continue
                for neighbor, edge in adjacency.get(current, []):
                    if neighbor not in visited:
                        edge_cost = -math.log(max(edge.confidence, 0.01)) + 0.08
                        heapq.heappush(queue, (cost + edge_cost, neighbor, visited + [neighbor], path_edges + [edge]))
        candidates.sort(key=lambda item: (item[0], len(item[2])))
        results: list[PathResult] = []
        seen: set[tuple[str, ...]] = set()
        for _, node_ids, path_edges in candidates:
            if tuple(node_ids) in seen:
                continue
            seen.add(tuple(node_ids))
            score = round(100 * math.prod(edge.confidence for edge in path_edges) * (0.96 ** max(0, len(path_edges) - 1)))
            contact = nodes[node_ids[0]].label
            results.append(PathResult(
                rank=len(results) + 1, score=score, confidence=_confidence(score),
                path=[nodes[node_id].label for node_id in node_ids], iosg_contact=contact,
                path_nodes=[nodes[node_id] for node_id in node_ids],
                edges=path_edges, suggested_next_action=_next_action(contact, path_edges),
            ))
        return nodes, edges, results
