from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Protocol

import requests

from ..models import Edge, Node, QueryKind
from ..repository import GraphRepository, infer_kind, normalize_handle
from .twenty import _role_relationship

X_URL_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,50})", re.I)


class SurfProvider(Protocol):
    def team(self, project_handle: str) -> list[dict[str, Any]]: ...


class FollowProvider(Protocol):
    def followers(self, handles: list[str]) -> dict[str, list[dict[str, Any]]]: ...


class SurfClient:
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self.timeout = timeout

    def team(self, project_handle: str) -> list[dict[str, Any]]:
        if not self.api_key or not project_handle:
            return []
        response = requests.get(
            "https://api.asksurf.ai/gateway/v1/project/detail",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params={"handle": normalize_handle(project_handle), "fields": "team"},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return ((response.json().get("data") or {}).get("team") or {}).get("members") or []

    def funding(self, project_handle: str) -> dict[str, Any]:
        if not self.api_key or not project_handle:
            return {}
        response = requests.get(
            "https://api.asksurf.ai/gateway/v1/project/detail",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params={"handle": normalize_handle(project_handle), "fields": "funding"},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return (response.json().get("data") or {}).get("funding") or {}


def funding_investors(funding: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """Flatten explicit funding evidence, preferring leads and recent rounds."""
    found: dict[str, dict[str, Any]] = {}
    rounds = sorted(funding.get("rounds") or [], key=lambda item: item.get("date") or "", reverse=True)
    for round_item in rounds:
        for investor in sorted(round_item.get("investors") or [], key=lambda item: not item.get("is_lead")):
            key = str(investor.get("id") or investor.get("name") or "").casefold()
            if not key or key in found:
                continue
            found[key] = {
                **investor,
                "round_name": round_item.get("round_name"),
                "round_date": round_item.get("date"),
                "round_amount": round_item.get("amount"),
            }
            if len(found) >= limit:
                return list(found.values())
    return list(found.values())


def surf_x_handle(member: dict[str, Any]) -> str | None:
    links = member.get("social_links") or {}
    for key in ("twitter", "x"):
        match = X_URL_RE.search(str(links.get(key) or ""))
        if match:
            return match.group(1).lower()
    return None


class NeonFollowProvider:
    """Read normalized company-person and pre-synchronized follow evidence from Neon."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def followers(self, handles: list[str]) -> dict[str, list[dict[str, Any]]]:
        clean = sorted({normalize_handle(handle) for handle in handles if handle})
        if not self.database_url or not clean:
            return {}
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover - deployment configuration
            raise RuntimeError("psycopg2 is required for Neon enrichment") from exc
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select lower(followed_username), iosg_member, iosg_x_username, last_confirmed
                from deals.iosg_x_following
                where lower(followed_username) = any(%s) and is_active
                order by lower(followed_username), iosg_member
                """,
                (clean,),
            )
            result: dict[str, list[dict[str, Any]]] = {}
            for handle, member, member_handle, confirmed in cursor.fetchall():
                result.setdefault(handle, []).append({
                    "iosg_member": member,
                    "iosg_x_username": member_handle,
                    "last_confirmed": confirmed.isoformat() if confirmed else None,
                })
            return result

    def find_companies(self, query: str, kind: QueryKind) -> list[dict[str, Any]]:
        if not self.database_url:
            return []
        actual = infer_kind(query) if kind == QueryKind.AUTO else kind
        handle = normalize_handle(query)
        if actual in {QueryKind.PROJECT_X, QueryKind.FOUNDER_X}:
            predicate = "lower(company_x_username) = %s or lower(person_x_username) = %s"
            params = (handle, handle)
        elif actual == QueryKind.COMPANY_NAME:
            predicate = "lower(company_name) = lower(%s)"
            params = (query.strip(),)
        else:
            return []
        import psycopg2
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                select distinct company_notion_id, company_name, company_x_username
                from deals.x_company_people
                where is_active and ({predicate})
                order by company_name
                limit 20
                """,
                params,
            )
            return [
                {"id": row[0], "name": row[1], "x_handle": row[2]}
                for row in cursor.fetchall()
            ]

    def company_people(self, company_id: str) -> list[dict[str, Any]]:
        if not self.database_url:
            return []
        import psycopg2
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select person_x_username, person_name, role, relationship_type,
                       source, confidence, last_confirmed
                from deals.x_company_people
                where company_notion_id = %s and is_active
                order by relationship_type, person_name
                """,
                (company_id,),
            )
            return [{
                "x_handle": row[0], "name": row[1], "role": row[2],
                "relationship_type": row[3], "source": row[4], "confidence": row[5],
                "last_confirmed": row[6].isoformat() if row[6] else None,
            } for row in cursor.fetchall()]

    def store_company_people(self, company_id: str, company_name: str,
                             company_x_handle: str | None,
                             people: list[dict[str, Any]]) -> int:
        """Persist provider-confirmed people without inferring departures from omissions."""
        rows = []
        for person in people:
            handle = surf_x_handle(person)
            if not handle:
                continue
            role = str(person.get("role") or "Team member").strip()
            relationship, confidence = _role_relationship(role)
            if re.search(r"\b(former|previous|ex[- ])\b", role, re.I):
                relationship, confidence = "former_employee", 0.65
            rows.append((
                company_id, company_name, normalize_handle(company_x_handle or "") or None,
                handle, person.get("name"), role, relationship.removesuffix("_of"),
                "surf", "high" if confidence >= 0.85 else "medium",
            ))
        if not rows:
            return 0
        import psycopg2
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                insert into deals.x_company_people (
                    company_notion_id, company_name, company_x_username,
                    person_x_username, person_name, role, relationship_type,
                    source, confidence, first_seen, last_confirmed, is_active
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), true)
                on conflict (company_notion_id, person_x_username) do update set
                    company_name = excluded.company_name,
                    company_x_username = coalesce(excluded.company_x_username, deals.x_company_people.company_x_username),
                    person_name = excluded.person_name,
                    role = excluded.role,
                    relationship_type = excluded.relationship_type,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    last_confirmed = now(),
                    is_active = true
                """,
                rows,
            )
        return len(rows)

    def store_company_investors(self, company_id: str, company_name: str,
                                company_x_handle: str | None,
                                investors: list[dict[str, Any]]) -> int:
        if not investors:
            return 0
        import psycopg2
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists deals.x_company_investors (
                    company_id text not null,
                    company_name text not null,
                    company_x_username text,
                    investor_source_id text not null,
                    investor_name text not null,
                    investor_type text,
                    round_name text not null default '',
                    round_date date,
                    round_amount numeric,
                    is_lead boolean not null default false,
                    source text not null,
                    first_seen timestamptz not null default now(),
                    last_confirmed timestamptz not null default now(),
                    primary key (company_id, investor_source_id, round_name)
                )
                """
            )
            rows = [(
                company_id, company_name, normalize_handle(company_x_handle or "") or None,
                str(item.get("id") or item.get("name")), item.get("name"), item.get("type"),
                item.get("round_name") or "", item.get("round_date"), item.get("round_amount"),
                bool(item.get("is_lead")), "surf",
            ) for item in investors if item.get("name")]
            cursor.executemany(
                """
                insert into deals.x_company_investors (
                    company_id, company_name, company_x_username, investor_source_id,
                    investor_name, investor_type, round_name, round_date, round_amount,
                    is_lead, source
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (company_id, investor_source_id, round_name) do update set
                    company_name = excluded.company_name,
                    company_x_username = coalesce(excluded.company_x_username, deals.x_company_investors.company_x_username),
                    investor_name = excluded.investor_name,
                    investor_type = excluded.investor_type,
                    round_date = excluded.round_date,
                    round_amount = excluded.round_amount,
                    is_lead = excluded.is_lead,
                    source = excluded.source,
                    last_confirmed = now()
                """,
                rows,
            )
        return len(rows)

    def sync_status(self) -> dict[str, Any]:
        if not self.database_url:
            return {"status": "not_configured"}
        import psycopg2
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select count(*), min(synced_at), max(synced_at),
                       count(*) filter (where synced_at < now() - interval '8 days')
                from deals.iosg_x_sync_status
                """
            )
            members, oldest, newest, stale = cursor.fetchone()
        return {
            "status": "ok" if members else "empty",
            "members_synced": members,
            "oldest_sync": oldest.isoformat() if oldest else None,
            "newest_sync": newest.isoformat() if newest else None,
            "stale_members": stale,
        }


class EnrichedGraphRepository:
    """Decorate a primary graph with sourced Surf roles and Neon follow signals."""

    def __init__(self, base: GraphRepository, surf: SurfProvider | None = None,
                 follows: FollowProvider | None = None):
        self.base = base
        self.surf = surf
        self.follows = follows
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._source_diagnostics: dict[str, Any] = {}

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        return list(self._edges.values())

    def diagnostics(self) -> dict[str, Any]:
        return {"sources": self._source_diagnostics}

    def resolve(self, query: str, kind: QueryKind) -> list[Node]:
        self._source_diagnostics = {
            "twenty": {"status": "ok", "mode": "live"},
            "surf": {"status": "not_configured" if not self.surf else "not_needed"},
            "neon": {"status": "not_configured" if not self.follows else "ok"},
            "sorsa": {"status": "not_configured" if not self.follows else "cached_in_neon"},
        }
        actual_kind = infer_kind(query) if kind == QueryKind.AUTO else kind
        preloaded_neon: list[dict[str, Any]] = []
        if self.follows and hasattr(self.follows, "find_companies"):
            try:
                preloaded_neon = self.follows.find_companies(query, kind)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["neon"] = {"status": "error", "error": type(exc).__name__}
        # Exact cached associations avoid fuzzy remote matches and several slow fallback queries.
        use_neon_resolution = bool(
            preloaded_neon
            and actual_kind in {QueryKind.COMPANY_NAME, QueryKind.PROJECT_X, QueryKind.FOUNDER_X}
        )
        matches = [] if use_neon_resolution else self.base.resolve(query, kind)
        self._nodes = {} if use_neon_resolution else {node.id: node for node in self.base.nodes()}
        self._edges = {} if use_neon_resolution else {edge.id: edge for edge in self.base.edges()}
        surf_members: list[dict[str, Any]] | None = None
        neon_people: list[dict[str, Any]] = []
        neon_company: dict[str, Any] | None = None
        if self.follows and hasattr(self.follows, "find_companies"):
            candidates = preloaded_neon
            if not candidates and matches:
                candidates = self.follows.find_companies(
                    matches[0].x_handle or matches[0].label, QueryKind.PROJECT_X if matches[0].x_handle else QueryKind.COMPANY_NAME
                )  # type: ignore[attr-defined]
            neon_company = candidates[0] if candidates else None
        if not matches:
            if neon_company:
                target = Node(
                    id=f"neon:company:{neon_company['id']}", label=neon_company["name"],
                    kind="company", x_handle=normalize_handle(neon_company.get("x_handle") or "") or None,
                    metadata={"source": "neon", "source_record_id": neon_company["id"]},
                )
                self._nodes[target.id] = target
                matches = [target]
            else:
                target = None
            if matches:
                pass
            elif not self.surf or actual_kind != QueryKind.PROJECT_X:
                return []
            else:
                handle = normalize_handle(query)
                surf_members = self.surf.team(handle)
                if not surf_members:
                    return []
                target = Node(
                    id=f"surf:company:{handle}", label=f"@{handle}", kind="company",
                    x_handle=handle, metadata={"source": "surf", "resolution": "project_x_handle"},
                )
                self._nodes[target.id] = target
                matches = [target]
        target = matches[0]
        if neon_company and hasattr(self.follows, "company_people"):
            try:
                neon_people = self.follows.company_people(neon_company["id"])  # type: ignore[attr-defined]
                self._add_neon_people(target, neon_people)
                self._source_diagnostics["neon"]["company_people"] = len(neon_people)
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["neon"] = {"status": "error", "error": type(exc).__name__}
        surf_handle = target.x_handle or re.sub(r"[^a-z0-9_]", "", target.label.casefold())
        if self.surf and surf_handle and not neon_people:
            try:
                members = surf_members if surf_members is not None else self.surf.team(surf_handle)
                self._add_surf_team(target, members)
                stored = 0
                if members and self.follows and hasattr(self.follows, "store_company_people"):
                    company_id = str(target.metadata.get("source_record_id") or target.id)
                    stored = self.follows.store_company_people(  # type: ignore[attr-defined]
                        company_id, target.label, surf_handle, members,
                    )
                self._source_diagnostics["surf"] = {
                    "status": "ok", "team_members": len(members), "stored_people": stored,
                }
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["surf"] = {"status": "error", "error": type(exc).__name__}
        elif neon_people:
            self._source_diagnostics["surf"] = {"status": "cached_in_neon", "team_members": len(neon_people)}
        if self.surf and surf_handle and hasattr(self.surf, "funding"):
            try:
                investors = funding_investors(self.surf.funding(surf_handle))  # type: ignore[attr-defined]
                stored_investors = 0
                company_id = str(target.metadata.get("source_record_id") or target.id)
                if investors and self.follows and hasattr(self.follows, "store_company_investors"):
                    stored_investors = self.follows.store_company_investors(  # type: ignore[attr-defined]
                        company_id, target.label, surf_handle, investors,
                    )
                if investors and hasattr(self.base, "add_investor_paths"):
                    self.base.add_investor_paths(target, investors)  # type: ignore[attr-defined]
                    self._nodes.update({node.id: node for node in self.base.nodes()})
                    self._edges.update({edge.id: edge for edge in self.base.edges()})
                self._source_diagnostics["investors"] = {
                    "status": "ok", "investors_found": len(investors),
                    "stored_investors": stored_investors,
                }
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["investors"] = {"status": "error", "error": type(exc).__name__}
        if self.follows:
            people = [node for node in self._nodes.values() if node.kind == "person" and node.x_handle]
            try:
                follower_map = self.follows.followers([node.x_handle for node in people if node.x_handle])
                self._add_follows(people, follower_map)
                self._source_diagnostics["sorsa"]["matched_follow_edges"] = sum(map(len, follower_map.values()))
                if hasattr(self.follows, "sync_status"):
                    self._source_diagnostics["sorsa"].update(self.follows.sync_status())  # type: ignore[attr-defined]
                    if self._source_diagnostics["sorsa"].get("stale_members"):
                        self._source_diagnostics["sorsa"]["status"] = "stale"
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["sorsa"] = {"status": "error", "error": type(exc).__name__}
        return [self._nodes.get(target.id, target)]

    def _add_neon_people(self, target: Node, people: list[dict[str, Any]]) -> None:
        confidence_values = {"high": 0.95, "medium": 0.75, "low": 0.50}
        for person in people:
            handle = normalize_handle(person.get("x_handle") or "")
            if not handle:
                continue
            node_id = f"x:person:{handle}"
            node = Node(
                id=node_id, label=person.get("name") or f"@{handle}", kind="person",
                x_handle=handle, metadata={"role": person.get("role"), "source": person.get("source")},
            )
            self._nodes[node_id] = node
            relationship = str(person.get("relationship_type") or "employee")
            relationship = relationship if relationship.endswith("_of") else f"{relationship}_of"
            edge_id = f"neon:company-person:{target.id}:{handle}"
            self._edges[edge_id] = Edge(
                id=edge_id, source=node_id, target=target.id, relationship=relationship,
                confidence=confidence_values.get(str(person.get("confidence") or "").lower(), 0.5),
                evidence=(f"Neon stores a {person.get('source') or 'provider'} association: "
                          f"{node.label} is {person.get('role') or relationship.replace('_', ' ')} at {target.label}."),
                evidence_source=f"{person.get('source') or 'unknown'}_neon",
                observed_at=person.get("last_confirmed"),
            )

    def _add_surf_team(self, target: Node, members: list[dict[str, Any]]) -> None:
        observed = datetime.now(timezone.utc).isoformat()
        for member in members:
            handle = surf_x_handle(member)
            if not handle:
                continue
            role = str(member.get("role") or "")
            relationship, confidence = _role_relationship(role)
            node_id = f"x:person:{handle}"
            node = Node(
                id=node_id, label=str(member.get("name") or f"@{handle}"), kind="person",
                x_handle=handle, metadata={"role": role, "source": "surf"},
            )
            self._nodes[node_id] = node
            self._edges[f"surf:team:{target.id}:{handle}"] = Edge(
                id=f"surf:team:{target.id}:{handle}", source=node_id, target=target.id,
                relationship=relationship, confidence=confidence,
                evidence=f"Surf lists {node.label} as {role or 'a team member'} at {target.label}.",
                evidence_source="surf", observed_at=observed,
            )

    def _add_follows(self, people: list[Node], follower_map: dict[str, list[dict[str, Any]]]) -> None:
        by_handle = {normalize_handle(node.x_handle or ""): node for node in people}
        for handle, followers in follower_map.items():
            person = by_handle.get(normalize_handle(handle))
            if not person:
                continue
            for follower in followers:
                member = str(follower.get("iosg_member") or "IOSG")
                member_handle = normalize_handle(str(follower.get("iosg_x_username") or "")) or None
                member_id = f"iosg:{member.casefold()}"
                self._nodes[member_id] = Node(
                    id=member_id, label=member, kind="iosg_member", x_handle=member_handle,
                )
                edge_id = f"neon:x-follow:{member_id}:{person.id}"
                self._edges[edge_id] = Edge(
                    id=edge_id, source=member_id, target=person.id, relationship="x_follow",
                    confidence=0.35,
                    evidence=(f"Neon's latest Sorsa-synchronized snapshot records that {member} follows "
                              f"@{handle} on X. This is weak evidence, not proof of a warm introduction."),
                    evidence_source="sorsa_neon", observed_at=follower.get("last_confirmed"),
                )
