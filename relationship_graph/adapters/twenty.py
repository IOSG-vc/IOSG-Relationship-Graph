from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

from ..models import Edge, Node, QueryKind
from ..repository import infer_kind, normalize_handle


class TwentyError(RuntimeError):
    """Twenty rejected a request or returned an invalid response."""


def _name(value: dict[str, Any] | str | None) -> str:
    if isinstance(value, str):
        return value.strip()
    value = value or {}
    return " ".join(filter(None, (value.get("firstName"), value.get("lastName")))).strip()


def _handle(link: Any) -> str | None:
    if isinstance(link, dict):
        link = link.get("primaryLinkUrl")
    clean = normalize_handle(str(link or ""))
    return clean or None


def _role_relationship(job_title: str | None) -> tuple[str, float]:
    title = job_title or ""
    if re.search(r"\b(co[- ]?founder|founder|founding partner)\b", title, re.I):
        return "founder_of", 0.95
    if re.search(r"\b(ceo|cto|coo|cfo|chief|president|partner)\b", title, re.I):
        return "executive_of", 0.90
    return "employee_of", 0.72 if title else 0.55


class TwentyGraphRepository:
    """Build a query-scoped graph from Twenty without reading private contents."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 20, owner_provider: Any = None):
        if not base_url or not api_key:
            raise ValueError("TWENTY_BASE_URL and TWENTY_API_KEY are required")
        self.graphql_url = f"{base_url.rstrip('/')}/graphql"
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.owner_provider = owner_provider
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = requests.post(
                self.graphql_url, headers=self.headers,
                json={"query": query, "variables": variables or {}}, timeout=self.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TwentyError(f"Could not read Twenty: {exc}") from exc
        if not response.ok or payload.get("errors"):
            messages = "; ".join(item.get("message", "unknown error") for item in payload.get("errors", []))
            raise TwentyError(messages or f"Twenty returned HTTP {response.status_code}")
        return payload["data"]

    @staticmethod
    def _items(connection: dict[str, Any]) -> list[dict[str, Any]]:
        return [edge["node"] for edge in connection.get("edges", [])]

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        return list(self._edges.values())

    def _node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def _edge(self, edge: Edge) -> None:
        self._edges[edge.id] = edge

    def _find_target(self, query: str, kind: QueryKind) -> list[dict[str, Any]]:
        actual = infer_kind(query) if kind == QueryKind.AUTO else kind
        if actual == QueryKind.FOUNDER_X:
            people_query = """
            query FounderByX($value: String!) {
              people(first: 20, filter: {xLink: {primaryLinkUrl: {ilike: $value}}}) {
                edges { node { id companyId } }
              }
            }
            """
            people = self._items(self.graphql(people_query, {"value": f"%{normalize_handle(query)}%"})["people"])
            company_ids = sorted({person.get("companyId") for person in people if person.get("companyId")})
            if not company_ids:
                return []
            query = company_ids[0]
            actual = None

        clauses: list[dict[str, Any]] = []
        if actual == QueryKind.COMPANY_NAME:
            clauses.append({"name": {"ilike": f"%{query.strip()}%"}})
        elif actual == QueryKind.DOMAIN:
            domain = query.lower().removeprefix("https://").removeprefix("http://").strip("/")
            clauses.append({"domainName": {"primaryLinkUrl": {"ilike": f"%{domain}%"}}})
        elif actual == QueryKind.PROJECT_X:
            clauses.append({"xLink": {"primaryLinkUrl": {"ilike": f"%{normalize_handle(query)}%"}}})
        else:
            clauses.append({"id": {"eq": query}})
        company_query = """
        query FindCompanies($filter: CompanyFilterInput!) {
          companies(first: 20, filter: $filter) { edges { node {
            id name domainName { primaryLinkUrl } xLink { primaryLinkUrl }
          } } }
        }
        """
        companies = self._items(self.graphql(company_query, {"filter": clauses[0]})["companies"])
        if actual == QueryKind.COMPANY_NAME:
            needle = query.strip().casefold()
            companies.sort(key=lambda company: (str(company.get("name") or "").casefold() != needle))
        elif actual == QueryKind.DOMAIN:
            needle = query.lower().removeprefix("https://").removeprefix("http://").strip("/")
            companies.sort(key=lambda company: (
                str((company.get("domainName") or {}).get("primaryLinkUrl") or "")
                .lower().removeprefix("https://").removeprefix("http://").strip("/") != needle
            ))
        elif actual == QueryKind.PROJECT_X:
            needle = normalize_handle(query)
            companies.sort(key=lambda company: (_handle(company.get("xLink")) != needle))
        return companies

    def _people(self, company_id: str) -> list[dict[str, Any]]:
        query = """
        query PeopleAtCompany($companyId: UUID!) {
          people(first: 100, filter: {companyId: {eq: $companyId}}) { edges { node {
            id name { firstName lastName } jobTitle companyId isIosgTeam
            relationshipStrength introducedById introDistance xLink { primaryLinkUrl }
          } } }
        }
        """
        return self._items(self.graphql(query, {"companyId": company_id})["people"])

    def _people_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ids = sorted(set(filter(None, ids)))
        if not ids:
            return []
        query = """
        query PeopleByIds($ids: [UUID!]!) {
          people(first: 100, filter: {id: {in: $ids}}) { edges { node {
            id name { firstName lastName } jobTitle isIosgTeam xLink { primaryLinkUrl }
          } } }
        }
        """
        return self._items(self.graphql(query, {"ids": ids})["people"])

    def _interaction_owners(self, person_id: str) -> list[dict[str, Any]]:
        # Deliberately excludes subjects, bodies, titles, descriptions, and attendees' emails.
        query = """
        query InteractionOwners($personId: UUID!) {
          messageParticipants(first: 500, filter: {personId: {eq: $personId}}) {
            edges { node { messageId message { receivedAt messageParticipants(first: 50) {
              edges { node { workspaceMemberId workspaceMember { id name { firstName lastName } } } }
            } } } }
          }
          calendarEventParticipants(first: 500, filter: {personId: {eq: $personId}}) {
            edges { node { calendarEventId calendarEvent { startsAt isCanceled calendarEventParticipants(first: 50) {
              edges { node { workspaceMemberId workspaceMember { id name { firstName lastName } } } }
            } } } }
          }
        }
        """
        data = self.graphql(query, {"personId": person_id})
        owners: dict[str, dict[str, Any]] = defaultdict(lambda: {"email": set(), "meeting": set(), "last": None, "name": ""})
        for connection, event_key, id_key, date_key, count_key in (
            ("messageParticipants", "message", "messageId", "receivedAt", "email"),
            ("calendarEventParticipants", "calendarEvent", "calendarEventId", "startsAt", "meeting"),
        ):
            for row in self._items(data[connection]):
                event = row.get(event_key) or {}
                if event_key == "calendarEvent" and event.get("isCanceled"):
                    continue
                for participant in self._items(event.get(connection) or {}):
                    member = participant.get("workspaceMember")
                    if not member:
                        continue
                    owner = owners[member["id"]]
                    owner["name"] = _name(member.get("name"))
                    owner[count_key].add(row.get(id_key))
                    observed = event.get(date_key)
                    if observed and (not owner["last"] or observed > owner["last"]):
                        owner["last"] = observed
        return [
            {"id": owner_id, "name": item["name"], "email_count": len(item["email"]),
             "meeting_count": len(item["meeting"]), "last": item["last"]}
            for owner_id, item in sorted(
                owners.items(), key=lambda pair: (len(pair[1]["meeting"]) * 5 + len(pair[1]["email"]), pair[1]["last"] or ""), reverse=True
            )
        ]

    def _referrals(self, company_id: str, person_ids: list[str]) -> list[dict[str, Any]]:
        clauses: list[dict[str, Any]] = [{"toCompanyId": {"eq": company_id}}]
        if person_ids:
            clauses.append({"toId": {"in": person_ids}})
        query = """
        query ReferralsTo($filter: ReferFilterInput!) {
          refers(first: 100, filter: $filter) { edges { node {
            id progress fromId toId accountOwnerId
            from { id name { firstName lastName } isIosgTeam xLink { primaryLinkUrl } }
            to { id name { firstName lastName } xLink { primaryLinkUrl } }
            accountOwner { id name { firstName lastName } }
          } } }
        }
        """
        return self._items(self.graphql(query, {"filter": {"or": clauses}})["refers"])

    def _social_connections(self, person_ids: list[str]) -> list[dict[str, Any]]:
        if not person_ids:
            return []
        query = """
        query SocialConnections($ids: [UUID!]!) {
          socialConnections(first: 200, filter: {or: [
            {personId: {in: $ids}}, {connectedPersonId: {in: $ids}}
          ]}) { edges { node {
            id personId connectedPersonId platforms interactionSummary evidenceStatus lastSeen
            person { id name { firstName lastName } isIosgTeam xLink { primaryLinkUrl } }
            connectedPerson { id name { firstName lastName } isIosgTeam xLink { primaryLinkUrl } }
          } } }
        }
        """
        return self._items(self.graphql(query, {"ids": person_ids})["socialConnections"])

    def _company_connections(self, company_id: str) -> list[dict[str, Any]]:
        query = """
        query CompanyConnections($companyId: UUID!) {
          companyConnections(first: 20, filter: {or: [
            {companyId: {eq: $companyId}}, {connectedCompanyId: {eq: $companyId}}
          ]}) { edges { node {
            id companyId connectedCompanyId context evidenceStatus verifiedAt
            company { id name } connectedCompany { id name }
          } } }
        }
        """
        return self._items(self.graphql(query, {"companyId": company_id})["companyConnections"])

    def _past_employers(self, person_ids: list[str]) -> list[dict[str, Any]]:
        if not person_ids:
            return []
        query = """
        query PastEmployers($ids: [UUID!]!) {
          companies(first: 50, filter: {or: [
            {associatedPeopleId: {in: $ids}}, {associated2Id: {in: $ids}}
          ]}) { edges { node { id name associatedPeopleId associated2Id } } }
        }
        """
        return self._items(self.graphql(query, {"ids": person_ids})["companies"])

    def resolve(self, query: str, kind: QueryKind) -> list[Node]:
        self._nodes.clear()
        self._edges.clear()
        companies = self._find_target(query, kind)
        if not companies and kind == QueryKind.AUTO and infer_kind(query) == QueryKind.PROJECT_X:
            companies = self._find_target(query, QueryKind.FOUNDER_X)
        if not companies:
            return []
        company = companies[0]
        company_node = Node(
            id=f"twenty:company:{company['id']}", label=company.get("name") or query,
            kind="company", x_handle=_handle(company.get("xLink")),
            metadata={"domain": (company.get("domainName") or {}).get("primaryLinkUrl"), "source": "twenty"},
        )
        self._node(company_node)
        people = self._people(company["id"])
        people_by_id = {person["id"]: person for person in people}
        introducer_ids = [person.get("introducedById") for person in people if person.get("introducedById")]
        introducers = {person["id"]: person for person in self._people_by_ids(introducer_ids)}
        now = datetime.now(timezone.utc).isoformat()
        for person in people:
            person_node = Node(
                id=f"twenty:person:{person['id']}", label=_name(person.get("name")) or "Unknown person",
                kind="person", x_handle=_handle(person.get("xLink")),
                metadata={"job_title": person.get("jobTitle"), "source": "twenty"},
            )
            self._node(person_node)
            relationship, confidence = _role_relationship(person.get("jobTitle"))
            self._edge(Edge(
                id=f"twenty:employment:{person['id']}:{company['id']}", source=person_node.id,
                target=company_node.id, relationship=relationship, confidence=confidence,
                evidence=f"Twenty lists {person_node.label} as {person.get('jobTitle') or 'a team member'} at {company_node.label}.",
                evidence_source="twenty", observed_at=now,
            ))
            if person.get("introDistance") == 0 and not person.get("introducedById"):
                iosg = Node(id="iosg:unresolved", label="IOSG", kind="iosg_member")
                self._node(iosg)
                self._edge(Edge(
                    id=f"twenty:direct:{person['id']}", source=iosg.id, target=person_node.id,
                    relationship="direct_relationship", confidence=0.80,
                    evidence=("Twenty marks this person with introduction distance 0. "
                              "The specific IOSG relationship owner is not yet resolved."),
                    evidence_source="twenty", observed_at=now,
                ))
            introducer = introducers.get(person.get("introducedById"))
            if introducer:
                connector = Node(id=f"twenty:person:{introducer['id']}", label=_name(introducer.get("name")), kind="iosg_member" if introducer.get("isIosgTeam") else "person")
                self._node(connector)
                self._edge(Edge(
                    id=f"twenty:introducer:{introducer['id']}:{person['id']}", source=connector.id,
                    target=person_node.id, relationship="existing_introducer", confidence=0.94,
                    evidence=f"Twenty records {connector.label} as the existing introducer for {person_node.label}.",
                    evidence_source="twenty", observed_at=now,
                ))
            external_owner = self.owner_provider.resolve(person["id"]) if self.owner_provider else None
            owners = [external_owner] if external_owner else self._interaction_owners(person["id"])[:3]
            for owner in owners:
                count = owner["email_count"] + owner["meeting_count"]
                if not count:
                    continue
                owner_node = Node(id=f"twenty:workspace:{owner['id']}", label=owner["name"] or "IOSG", kind="iosg_member")
                self._node(owner_node)
                confidence = min(0.95, 0.72 + min(owner["email_count"], 5) * 0.025 + min(owner["meeting_count"], 3) * 0.05)
                self._edge(Edge(
                    id=f"twenty:interaction:{owner['id']}:{person['id']}", source=owner_node.id,
                    target=person_node.id, relationship="email_calendar_interaction", confidence=confidence,
                    evidence=(f"Twenty records {owner['email_count']} email interaction(s) and "
                              f"{owner['meeting_count']} meeting(s); latest {owner['last'] or 'unknown'}. "
                              "No message or event contents were requested."),
                    evidence_source="twenty", observed_at=owner["last"],
                ))
        target_ids = set(people_by_id)
        for connection in self._social_connections(list(target_ids)):
            left = connection.get("person") or {}
            right = connection.get("connectedPerson") or {}
            if left.get("id") in target_ids:
                target_person, connector = left, right
            elif right.get("id") in target_ids:
                target_person, connector = right, left
            else:
                continue
            if not connector.get("isIosgTeam"):
                continue
            connector_node = Node(
                id=f"twenty:person:{connector['id']}", label=_name(connector.get("name")) or "IOSG",
                kind="iosg_member", x_handle=_handle(connector.get("xLink")),
            )
            target_node_id = f"twenty:person:{target_person['id']}"
            if target_node_id not in self._nodes:
                continue
            self._node(connector_node)
            verified = connection.get("evidenceStatus") == "VERIFIED"
            platforms = ", ".join(connection.get("platforms") or []) or "social"
            summary = connection.get("interactionSummary") or "Public social interaction recorded."
            self._edge(Edge(
                id=f"twenty:social:{connection['id']}", source=connector_node.id,
                target=target_node_id, relationship="social_connection",
                confidence=0.82 if verified else 0.65,
                evidence=f"Twenty records {platforms} evidence: {summary}",
                evidence_source="twenty", observed_at=connection.get("lastSeen"),
            ))
        for referral in self._referrals(company["id"], list(people_by_id)):
            source = referral.get("from") or {}
            destination = referral.get("to") or people_by_id.get(referral.get("toId")) or {}
            owner = referral.get("accountOwner") or {}
            source_id = source.get("id")
            if not source_id:
                continue
            source_node = Node(
                id=f"twenty:person:{source_id}", label=_name(source.get("name")) or "Known connector",
                kind="iosg_member" if source.get("isIosgTeam") else "person", x_handle=_handle(source.get("xLink")),
            )
            self._node(source_node)
            destination_id = destination.get("id")
            destination_node_id = f"twenty:person:{destination_id}" if destination_id else company_node.id
            if destination_id and destination_node_id not in self._nodes:
                self._node(Node(
                    id=destination_node_id, label=_name(destination.get("name")) or company_node.label,
                    kind="person", x_handle=_handle(destination.get("xLink")),
                ))
            progress = str(referral.get("progress") or "UNKNOWN")
            confidence = 0.99 if progress == "DONE" else 0.96 if progress == "IN_PROGRESS" else 0.90
            self._edge(Edge(
                id=f"twenty:referral:{referral['id']}", source=source_node.id,
                target=destination_node_id, relationship="referral", confidence=confidence,
                evidence=f"Twenty records an existing referral ({progress.lower().replace('_', ' ')}).",
                evidence_source="twenty", observed_at=now,
            ))
            owner_name = _name(owner.get("name"))
            owner_id = owner.get("id")
            if owner_id and owner_name and owner_id != source_id:
                owner_node = Node(id=f"twenty:person:{owner_id}", label=owner_name, kind="iosg_member")
                self._node(owner_node)
                self._edge(Edge(
                    id=f"twenty:referral-owner:{referral['id']}", source=owner_node.id,
                    target=source_node.id, relationship="referral_owner", confidence=0.99,
                    evidence=f"Twenty assigns {owner_name} as owner of this referral.",
                    evidence_source="twenty", observed_at=now,
                ))
        # Bound intermediary traversal: at most five related companies and three warm contacts each.
        for connection in self._company_connections(company["id"])[:5]:
            other_id = connection["connectedCompanyId"] if connection["companyId"] == company["id"] else connection["companyId"]
            other_company = connection.get("connectedCompany") if connection["companyId"] == company["id"] else connection.get("company")
            other_label = (other_company or {}).get("name") or "Connected company"
            other_node = Node(id=f"twenty:company:{other_id}", label=other_label, kind="company")
            self._node(other_node)
            verified = connection.get("evidenceStatus") == "VERIFIED"
            self._edge(Edge(
                id=f"twenty:company-connection:{connection['id']}", source=other_node.id,
                target=company_node.id, relationship="company_relationship",
                confidence=0.82 if verified else 0.62,
                evidence=f"Twenty records a {'verified' if verified else 'potential'} company relationship: {connection.get('context') or 'no additional context'}.",
                evidence_source="twenty", observed_at=connection.get("verifiedAt"),
            ))
            warm_people = [
                item for item in self._people(other_id)
                if item.get("relationshipStrength") in {"HOT", "WARM"} or item.get("introDistance") == 0
            ][:3]
            for connector in warm_people:
                connector_node = Node(
                    id=f"twenty:person:{connector['id']}", label=_name(connector.get("name")) or "Known contact",
                    kind="person", x_handle=_handle(connector.get("xLink")),
                )
                self._node(connector_node)
                self._edge(Edge(
                    id=f"twenty:warm-company-contact:{connector['id']}:{other_id}", source=connector_node.id,
                    target=other_node.id, relationship="works_at", confidence=0.85,
                    evidence=f"Twenty lists {connector_node.label} as a warm contact at {other_label}.",
                    evidence_source="twenty", observed_at=now,
                ))
                for owner in self._interaction_owners(connector["id"])[:1]:
                    if not owner.get("name"):
                        continue
                    owner_node = Node(id=f"twenty:workspace:{owner['id']}", label=owner["name"], kind="iosg_member")
                    self._node(owner_node)
                    self._edge(Edge(
                        id=f"twenty:company-owner:{owner['id']}:{connector['id']}", source=owner_node.id,
                        target=connector_node.id, relationship="email_calendar_interaction", confidence=0.78,
                        evidence=(f"Twenty records {owner['email_count']} email interaction(s) and "
                                  f"{owner['meeting_count']} meeting(s); no contents were requested."),
                        evidence_source="twenty", observed_at=owner.get("last"),
                    ))
        for employer in self._past_employers(list(people_by_id))[:20]:
            ids = [employer.get("associatedPeopleId"), employer.get("associated2Id")]
            target_ids = set(ids) & set(people_by_id)
            connector_ids = [value for value in ids if value and value not in target_ids]
            for connector in self._people_by_ids(connector_ids):
                if not connector.get("isIosgTeam"):
                    continue
                connector_node = Node(
                    id=f"twenty:person:{connector['id']}", label=_name(connector.get("name")) or "IOSG",
                    kind="iosg_member", x_handle=_handle(connector.get("xLink")),
                )
                self._node(connector_node)
                for target_id in target_ids:
                    self._edge(Edge(
                        id=f"twenty:past-employer:{employer['id']}:{connector['id']}:{target_id}",
                        source=connector_node.id, target=f"twenty:person:{target_id}",
                        relationship="past_employer_overlap", confidence=0.65,
                        evidence=f"Twenty associates both people with former employer {employer.get('name') or 'Unknown company'}.",
                        evidence_source="twenty", observed_at=now,
                    ))
        return [company_node]
