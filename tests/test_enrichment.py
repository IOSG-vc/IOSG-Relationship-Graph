from relationship_graph.adapters.enrichment import EnrichedGraphRepository
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
