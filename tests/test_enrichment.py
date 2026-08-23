from relationship_graph.adapters.enrichment import EnrichedGraphRepository, funding_investors, select_surf_project
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


class Follows:
    def followers(self, handles):
        assert handles == ["ada"]
        return {"ada": [{
            "iosg_member": "Jocy", "iosg_x_username": "jocyiosg",
            "last_confirmed": "2026-08-20T00:00:00Z",
        }]}


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
