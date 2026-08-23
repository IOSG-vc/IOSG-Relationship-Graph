from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
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

    def search_project(self, query: str) -> dict[str, Any] | None:
        if not self.api_key or not query:
            return None
        response = requests.get(
            "https://api.asksurf.ai/gateway/v1/search/project",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params={"q": query, "limit": 5}, timeout=self.timeout,
        )
        response.raise_for_status()
        return select_surf_project(response.json().get("data") or [], query)

    def _project_detail(self, project_ref: str, field: str) -> dict[str, Any]:
        if not self.api_key or not project_ref:
            return {}
        lookup = "id" if re.fullmatch(r"[0-9a-fA-F-]{36}", project_ref) else "q"
        response = requests.get(
            "https://api.asksurf.ai/gateway/v1/project/detail",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params={lookup: project_ref, "fields": field}, timeout=self.timeout,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return response.json().get("data") or {}

    def team(self, project_ref: str) -> list[dict[str, Any]]:
        if not self.api_key or not project_ref:
            return []
        return (self._project_detail(project_ref, "team").get("team") or {}).get("members") or []

    def funding(self, project_ref: str) -> dict[str, Any]:
        if not self.api_key or not project_ref:
            return {}
        return self._project_detail(project_ref, "funding").get("funding") or {}

    def ai_news(self, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.api_key or not project_id:
            return []
        response = requests.get(
            "https://api.asksurf.ai/gateway/v1/project/ai-news",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params={"id": project_id, "limit": limit, "lang": "en"},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return response.json().get("data") or []

    def enrich_fund_networks(self, investors: list[dict[str, Any]],
                             target_project_id: str | None,
                             limit: int = 4) -> list[dict[str, Any]]:
        """Fetch fund profiles and date-bounded portfolio confirmation concurrently."""
        selected = investors[:limit]

        def enrich(investor: dict[str, Any]) -> dict[str, Any]:
            fund_id = str(investor.get("id") or "")
            if not fund_id:
                return investor
            try:
                headers = {"Authorization": f"Bearer {self.api_key}"}
                detail_response = requests.get(
                    "https://api.asksurf.ai/gateway/v1/fund/detail",
                    headers=headers, params={"id": fund_id}, timeout=self.timeout,
                )
                detail_response.raise_for_status()
                profile = detail_response.json().get("data") or {}
                portfolio_params: dict[str, Any] = {"id": fund_id, "limit": 100}
                round_date = investor.get("round_date")
                if round_date:
                    start = int(datetime.fromisoformat(str(round_date)).replace(tzinfo=timezone.utc).timestamp())
                    portfolio_params.update({"invested_after": start, "invested_before": start + 86_399})
                portfolio_response = requests.get(
                    "https://api.asksurf.ai/gateway/v1/fund/portfolio",
                    headers=headers, params=portfolio_params, timeout=self.timeout,
                )
                portfolio_response.raise_for_status()
                portfolio = portfolio_response.json().get("data") or []
                match = next((item for item in portfolio if item.get("project_id") == target_project_id), None)
                return {**investor, "fund_profile": profile, "portfolio_match": match,
                        "portfolio_verified": bool(match)}
            except (requests.RequestException, ValueError) as exc:
                return {**investor, "fund_network_error": type(exc).__name__}

        with ThreadPoolExecutor(max_workers=min(4, len(selected) or 1)) as executor:
            enriched = list(executor.map(enrich, selected))
        return enriched + investors[len(selected):]


def select_surf_project(projects: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    if not projects:
        return None
    needle = re.sub(r"[^a-z0-9]", "", query.casefold().lstrip("@"))

    def rank(project: dict[str, Any]) -> tuple[int, str]:
        values = (project.get("name"), project.get("slug"), project.get("symbol"))
        normalized = {re.sub(r"[^a-z0-9]", "", str(value or "").casefold()) for value in values}
        return (0 if needle in normalized else 1, str(project.get("name") or ""))

    return min(projects, key=rank)


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


def outreach_context(items: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Keep sourced Surf context as talking points, never as relationship evidence."""
    contexts = []
    for item in items:
        sources = [str(url) for url in item.get("sources") or [] if str(url).startswith(("https://", "http://"))]
        if not sources:
            continue
        points = [str(point).strip() for point in item.get("tldr") or [] if str(point).strip()]
        why_now = str(item.get("subtitle") or "").strip() or " ".join(points)
        contexts.append({
            "id": str(item.get("id") or ""), "title": str(item.get("title") or "Recent project update"),
            "why_now": why_now, "signal_type": str(item.get("signal_type")),
            "timestamp": int(item.get("timestamp") or 0), "sources": sources,
        })
        if len(contexts) >= limit:
            break
    return contexts


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
        from psycopg2.extras import Json
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
            cursor.execute("alter table deals.x_company_investors add column if not exists portfolio_verified boolean not null default false")
            cursor.execute("alter table deals.x_company_investors add column if not exists fund_profile jsonb")
            cursor.execute("alter table deals.x_company_investors add column if not exists portfolio_evidence jsonb")
            rows = [(
                company_id, company_name, normalize_handle(company_x_handle or "") or None,
                str(item.get("id") or item.get("name")), item.get("name"), item.get("type"),
                item.get("round_name") or "", item.get("round_date"), item.get("round_amount"),
                bool(item.get("is_lead")), "surf", bool(item.get("portfolio_verified")),
                Json(item.get("fund_profile")) if item.get("fund_profile") else None,
                Json(item.get("portfolio_match")) if item.get("portfolio_match") else None,
            ) for item in investors if item.get("name")]
            cursor.executemany(
                """
                insert into deals.x_company_investors (
                    company_id, company_name, company_x_username, investor_source_id,
                    investor_name, investor_type, round_name, round_date, round_amount,
                    is_lead, source, portfolio_verified, fund_profile, portfolio_evidence
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (company_id, investor_source_id, round_name) do update set
                    company_name = excluded.company_name,
                    company_x_username = coalesce(excluded.company_x_username, deals.x_company_investors.company_x_username),
                    investor_name = excluded.investor_name,
                    investor_type = excluded.investor_type,
                    round_date = excluded.round_date,
                    round_amount = excluded.round_amount,
                    is_lead = excluded.is_lead,
                    source = excluded.source,
                    portfolio_verified = excluded.portfolio_verified,
                    fund_profile = coalesce(excluded.fund_profile, deals.x_company_investors.fund_profile),
                    portfolio_evidence = coalesce(excluded.portfolio_evidence, deals.x_company_investors.portfolio_evidence),
                    last_confirmed = now()
                """,
                rows,
            )
        return len(rows)

    def store_project_identity(self, company_id: str, company_name: str,
                               project: dict[str, Any]) -> None:
        import psycopg2
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists deals.x_company_source_identity (
                    company_id text not null,
                    company_name text not null,
                    source text not null,
                    source_project_id text not null,
                    canonical_name text,
                    source_slug text,
                    first_seen timestamptz not null default now(),
                    last_confirmed timestamptz not null default now(),
                    primary key (company_id, source)
                )
                """
            )
            cursor.execute(
                """
                insert into deals.x_company_source_identity (
                    company_id, company_name, source, source_project_id, canonical_name, source_slug
                ) values (%s, %s, 'surf', %s, %s, %s)
                on conflict (company_id, source) do update set
                    company_name = excluded.company_name,
                    source_project_id = excluded.source_project_id,
                    canonical_name = excluded.canonical_name,
                    source_slug = excluded.source_slug,
                    last_confirmed = now()
                """,
                (company_id, company_name, project["id"], project.get("name"), project.get("slug")),
            )

    def store_outreach_context(self, company_id: str, company_name: str,
                               contexts: list[dict[str, Any]]) -> int:
        if not contexts:
            return 0
        import psycopg2
        from psycopg2.extras import Json
        with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists deals.x_company_outreach_context (
                    company_id text not null,
                    company_name text not null,
                    source text not null,
                    signal_id text not null,
                    title text not null,
                    why_now text,
                    signal_type text not null,
                    signal_timestamp bigint not null,
                    source_urls jsonb not null,
                    first_seen timestamptz not null default now(),
                    last_confirmed timestamptz not null default now(),
                    primary key (company_id, source, signal_id)
                )
                """
            )
            rows = [(
                company_id, company_name, "surf_ai_news", item["id"], item["title"],
                item["why_now"], item["signal_type"], item["timestamp"], Json(item["sources"]),
            ) for item in contexts]
            cursor.executemany(
                """
                insert into deals.x_company_outreach_context (
                    company_id, company_name, source, signal_id, title, why_now,
                    signal_type, signal_timestamp, source_urls
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (company_id, source, signal_id) do update set
                    company_name = excluded.company_name,
                    title = excluded.title,
                    why_now = excluded.why_now,
                    signal_type = excluded.signal_type,
                    signal_timestamp = excluded.signal_timestamp,
                    source_urls = excluded.source_urls,
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
        surf_project = None
        surf_ref = surf_handle
        if self.surf and hasattr(self.surf, "search_project"):
            try:
                surf_project = self.surf.search_project(target.label)  # type: ignore[attr-defined]
                if surf_project:
                    surf_ref = str(surf_project["id"])
                    surf_handle = str(surf_project.get("slug") or surf_handle)
                    target.metadata.update({
                        "surf_project_id": surf_ref,
                        "surf_canonical_name": surf_project.get("name"),
                        "surf_slug": surf_project.get("slug"),
                    })
                    if self.follows and hasattr(self.follows, "store_project_identity"):
                        company_id = str(target.metadata.get("source_record_id") or target.id)
                        self.follows.store_project_identity(  # type: ignore[attr-defined]
                            company_id, target.label, surf_project,
                        )
                self._source_diagnostics["surf_identity"] = {
                    "status": "ok" if surf_project else "not_found",
                    "project_id": surf_project.get("id") if surf_project else None,
                    "canonical_name": surf_project.get("name") if surf_project else None,
                }
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["surf_identity"] = {"status": "error", "error": type(exc).__name__}
        if surf_project and self.surf and hasattr(self.surf, "ai_news"):
            try:
                contexts = outreach_context(self.surf.ai_news(surf_ref))  # type: ignore[attr-defined]
                stored_contexts = 0
                if contexts and self.follows and hasattr(self.follows, "store_outreach_context"):
                    company_id = str(target.metadata.get("source_record_id") or target.id)
                    stored_contexts = self.follows.store_outreach_context(  # type: ignore[attr-defined]
                        company_id, target.label, contexts,
                    )
                self._source_diagnostics["ai_news"] = {
                    "status": "ok" if contexts else "empty", "items": len(contexts),
                    "stored_items": stored_contexts,
                }
                self._source_diagnostics["recent_context"] = contexts
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["ai_news"] = {"status": "error", "error": type(exc).__name__}
        if self.surf and surf_ref and not neon_people:
            try:
                members = surf_members if surf_members is not None else self.surf.team(surf_ref)
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
        if self.surf and surf_ref and hasattr(self.surf, "funding"):
            try:
                investors = funding_investors(self.surf.funding(surf_ref))  # type: ignore[attr-defined]
                if investors and surf_project and hasattr(self.surf, "enrich_fund_networks"):
                    investors = self.surf.enrich_fund_networks(  # type: ignore[attr-defined]
                        investors, str(surf_project.get("id") or "") or None,
                    )
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
                    "fund_profiles": sum(bool(item.get("fund_profile")) for item in investors),
                    "portfolio_verified": sum(bool(item.get("portfolio_verified")) for item in investors),
                }
            except Exception as exc:  # noqa: BLE001
                self._source_diagnostics["investors"] = {"status": "error", "error": type(exc).__name__}
        if self.follows:
            people = [
                node for node in self._nodes.values()
                if node.kind in {"person", "fund"} and node.x_handle
            ]
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
