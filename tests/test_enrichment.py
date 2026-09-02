from relationship_graph.adapters.enrichment import (
    EnrichedGraphRepository, former_companies_from_bio, funding_investors,
    outreach_context, select_surf_project,
)
from relationship_graph.engine import IntroductionPathService
from relationship_graph.models import Edge, Node, QueryKind


class BaseRepository:
    target = Node(id="company:acme", label="Acme", kind="company", x_handle="acme")

    def resolve(self, query, kind):
        return [self.target]

    def nodes(self):
        return [self.target]

    def edges(self):
        return []


class EmptyBase(BaseRepository):
    def resolve(self, query, kind):
        return []

    def nodes(self):
        return []


class FuzzyWrongBase(BaseRepository):
    target = Node(id="company:not-acme", label="Acme Holdings Europe", kind="company")


class Surf:
    def team(self, project_handle):
        assert project_handle == "acme"
        return [{
            "name": "Ada", "role": "Co-founder & CEO",
            "social_links": {"twitter": "https://x.com/ada"},
        }]


class NewsSurf:
    def __init__(self):
        self.searches = []

    def search_project(self, query):
        self.searches.append(query)
        return {"id": "ethena-1", "name": "Ethena", "slug": "ethena"}

    def ai_news(self, project_id):
        assert project_id == "ethena-1"
        return [{
            "id": "news-1", "title": "Ethena update", "subtitle": "Mention the latest update.",
            "signal_type": "funding", "timestamp": 123,
            "sources": ["https://example.com/ethena"],
        }]


class Follows:
    def followers(self, handles):
        assert handles == ["ada"]
        return {"ada": [{
            "iosg_member": "Jocy", "iosg_x_username": "jocyiosg",
            "last_confirmed": "2026-08-20T00:00:00Z",
        }]}


class Profiles:
    def __init__(self):
        self.calls = []

    def profiles(self, handles):
        self.calls.append(handles)
        assert handles == ["ada"]
        return {"ada": {"description": "Building Acme. Previously at Coinbase."}}


class FormerCompanyBase(BaseRepository):
    def __init__(self):
        self.added = []

    def add_former_company_paths(self, target, founder, companies, bio):
        self.added.append((target.label, founder.label, companies, bio))
        return {"paths_added": 1, "matched_companies": 1}


class CachedProfiles:
    def __init__(self):
        self.claims = []

    def cached_profiles(self, handles, ttl_days):
        assert handles == ["ada"]
        assert ttl_days == 90
        return {"ada": {
            "description": "Previously at Coinbase", "_fetch_status": "ok",
            "_cache_fresh": True, "_fetched_at": "2026-08-01T00:00:00Z",
        }}

    def store_employment_claims(self, handle, description, companies):
        self.claims.append((handle, description, companies))
        return len(companies)


class ExistingPathBase(BaseRepository):
    member = Node(id="iosg:jocy", label="Jocy", kind="iosg_member")

    def nodes(self):
        return [self.target, self.member]

    def edges(self):
        return [Edge(
            id="direct", source=self.member.id, target=self.target.id,
            relationship="direct_company_relationship", confidence=0.8,
            evidence="Existing path", evidence_source="twenty",
        )]


class Neon(Follows):
    stored = []

    def find_companies(self, query, kind):
        if query.lower().lstrip("@") in {"ada", "acme"}:
            return [{"id": "notion-acme", "name": "Acme", "x_handle": "acme"}]
        return []

    def company_people(self, company_id):
        assert company_id == "notion-acme"
        return [{
            "x_handle": "ada", "name": "Ada", "role": "Founder",
            "relationship_type": "founder", "source": "surf", "confidence": "high",
            "last_confirmed": "2026-08-20T00:00:00Z",
        }]

    def store_company_people(self, company_id, company_name, company_x_handle, people):
        self.stored.append((company_id, company_name, company_x_handle, people))
        return len(people)


def test_surf_and_neon_add_sourced_weak_follow_path():
    repository = EnrichedGraphRepository(BaseRepository(), Surf(), Follows())
    result = IntroductionPathService(repository).search("@acme", QueryKind.PROJECT_X)
    assert result.status == "ok"
    assert result.recommended.path == ["Jocy", "Ada", "Acme"]
    assert result.recommended.confidence == "low"
    assert [edge.evidence_source for edge in result.recommended.edges] == ["sorsa_neon", "surf"]
    assert "not proof" in result.recommended.edges[0].evidence
    assert result.recommended.edges[1].confidence == 0.95


def test_sorsa_bio_discovers_former_company_for_surf_founder():
    base = FormerCompanyBase()
    repository = EnrichedGraphRepository(base, surf=Surf(), profiles=Profiles())
    result = IntroductionPathService(repository).search("@acme", QueryKind.PROJECT_X)

    assert base.added == [("Acme", "Ada", ["Coinbase"], "Building Acme. Previously at Coinbase.")]
    assert result.diagnostics["sources"]["sorsa_profiles"]["status"] == "ok"
    assert result.diagnostics["sources"]["sorsa_profiles"]["fallback_triggered"] is True
    assert result.diagnostics["sources"]["sorsa_profiles"]["former_company_candidates"] == 1
    assert result.diagnostics["sources"]["sorsa_profiles"]["paths_added"] == 1


def test_fresh_neon_profile_cache_avoids_sorsa_call():
    base = FormerCompanyBase()
    cache = CachedProfiles()
    profiles = Profiles()
    repository = EnrichedGraphRepository(base, surf=Surf(), follows=cache, profiles=profiles)
    result = IntroductionPathService(repository).search("@acme", QueryKind.PROJECT_X)

    diagnostics = result.diagnostics["sources"]["sorsa_profiles"]
    assert profiles.calls == []
    assert diagnostics["cache_hits"] == 1
    assert diagnostics["profiles_fetched"] == 0
    assert cache.claims == [("ada", "Previously at Coinbase", ["Coinbase"])]


def test_existing_path_skips_sorsa_profiles_entirely():
    profiles = Profiles()
    repository = EnrichedGraphRepository(ExistingPathBase(), profiles=profiles)
    result = IntroductionPathService(repository).search("Acme", QueryKind.COMPANY_NAME)

    assert result.recommended.path == ["Jocy", "Acme"]
    assert profiles.calls == []
    assert result.diagnostics["sources"]["sorsa_profiles"] == {
        "status": "skipped_existing_paths", "fallback_triggered": False,
    }


def test_former_company_parser_requires_explicit_history_language():
    assert former_companies_from_bio("ex-Coinbase | formerly at Meta; building Acme") == ["Coinbase", "Meta"]
    assert former_companies_from_bio("Founder at Acme. Interested in Coinbase.") == []


def test_surf_can_resolve_project_handle_missing_from_twenty():
    repository = EnrichedGraphRepository(EmptyBase(), Surf(), Follows())
    result = IntroductionPathService(repository).search("@acme", QueryKind.PROJECT_X)
    assert result.status == "ok"
    assert result.resolved_target.id == "surf:company:acme"
    assert result.resolved_target.metadata["source"] == "surf"
    assert result.recommended.path == ["Jocy", "Ada", "@acme"]


def test_neon_can_resolve_founder_handle_and_reuse_cached_association():
    repository = EnrichedGraphRepository(EmptyBase(), surf=None, follows=Neon())
    result = IntroductionPathService(repository).search("@ada", QueryKind.FOUNDER_X)
    assert result.status == "ok"
    assert result.resolved_target.label == "Acme"
    assert result.recommended.path == ["Jocy", "Ada", "Acme"]
    assert result.recommended.edges[1].evidence_source == "surf_neon"


def test_exact_neon_company_name_wins_over_fuzzy_base_match():
    repository = EnrichedGraphRepository(FuzzyWrongBase(), surf=None, follows=Neon())
    result = IntroductionPathService(repository).search("Acme", QueryKind.COMPANY_NAME)
    assert result.status == "ok"
    assert result.resolved_target.label == "Acme"
    assert result.resolved_target.metadata["source"] == "neon"
    assert result.recommended.path == ["Jocy", "Ada", "Acme"]


def test_exact_twenty_match_keeps_primary_relationship_edges_when_neon_is_cached():
    repository = EnrichedGraphRepository(ExistingPathBase(), surf=None, follows=Neon())
    result = IntroductionPathService(repository).search("Acme", QueryKind.COMPANY_NAME)

    assert result.resolved_target.id == "company:acme"
    assert result.recommended.path == ["Jocy", "Acme"]
    assert result.recommended.edges[0].evidence_source == "twenty"


def test_company_name_discovers_stores_and_uses_surf_team():
    neon = Neon()
    neon.find_companies = lambda query, kind: []
    repository = EnrichedGraphRepository(BaseRepository(), Surf(), neon)
    result = IntroductionPathService(repository).search("Acme", QueryKind.COMPANY_NAME)
    assert result.status == "ok"
    assert result.recommended.path == ["Jocy", "Ada", "Acme"]
    assert neon.stored[-1][1:3] == ("Acme", "acme")
    assert result.diagnostics["sources"]["surf"]["stored_people"] == 1


def test_funding_investors_prefers_leads_and_deduplicates_funds():
    funding = {"rounds": [
        {"round_name": "Seed", "date": "2024-01-01", "investors": [
            {"id": "fund-1", "name": "Fund One", "is_lead": False},
        ]},
        {"round_name": "Series A", "date": "2025-01-01", "investors": [
            {"id": "fund-2", "name": "Fund Two", "is_lead": False},
            {"id": "fund-1", "name": "Fund One", "is_lead": True},
        ]},
    ]}
    investors = funding_investors(funding)
    assert [item["name"] for item in investors] == ["Fund One", "Fund Two"]
    assert investors[0]["round_name"] == "Series A"


def test_surf_project_selection_prefers_exact_slug_after_rebrand():
    projects = [
        {"id": "wrong", "name": "Eigen Something", "slug": "eigen-something"},
        {"id": "right", "name": "EigenCloud", "slug": "eigenlayer"},
    ]
    assert select_surf_project(projects, "EigenLayer")["id"] == "right"


def test_outreach_context_requires_a_source_and_accepts_live_signal_types():
    items = [
        {"id": "good", "title": "Fresh raise", "subtitle": "Mention the new round.",
         "signal_type": "funding", "timestamp": 123, "sources": ["https://example.com/news"]},
        {"id": "mindshare", "title": "Momentum", "subtitle": "Mention the recent attention.",
         "signal_type": "bn_mindshare", "timestamp": 124,
         "sources": ["https://x.com/example"]},
        {"id": "unsourced", "title": "No source", "signal_type": "funding", "timestamp": 125},
    ]
    assert outreach_context(items) == [{
        "id": "good", "title": "Fresh raise", "why_now": "Mention the new round.",
        "signal_type": "funding", "timestamp": 123, "sources": ["https://example.com/news"],
    }, {
        "id": "mindshare", "title": "Momentum", "why_now": "Mention the recent attention.",
        "signal_type": "bn_mindshare", "timestamp": 124, "sources": ["https://x.com/example"],
    }]


def test_surf_news_uses_original_company_query_when_twenty_label_is_fuzzy():
    surf = NewsSurf()
    repository = EnrichedGraphRepository(FuzzyWrongBase(), surf=surf)
    result = IntroductionPathService(repository).search("Ethena", QueryKind.COMPANY_NAME)

    assert surf.searches == ["Ethena"]
    assert [item.title for item in result.outreach_context] == ["Ethena update"]
    assert result.diagnostics["sources"]["ai_news"]["items"] == 1
