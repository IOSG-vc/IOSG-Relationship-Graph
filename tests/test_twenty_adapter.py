import pytest

from relationship_graph.adapters.twenty import TwentyGraphRepository, _creator_name
from relationship_graph.engine import IntroductionPathService
from relationship_graph.models import QueryKind


class FakeTwenty(TwentyGraphRepository):
    def __init__(self):
        super().__init__("https://twenty.test", "test-key")

    def graphql(self, query, variables=None):
        if "FindCompanies" in query:
            return {"companies": {"edges": [{"node": {
                "id": "company-1", "name": "Acme", "domainName": {"primaryLinkUrl": "acme.xyz"},
                "xLink": {"primaryLinkUrl": "https://x.com/acme"},
            }}]}}
        if "PeopleAtCompany" in query:
            return {"people": {"edges": [{"node": {
                "id": "founder-1", "name": {"firstName": "Ada", "lastName": "Founder"},
                "jobTitle": "Co-founder & CEO", "companyId": "company-1", "isIosgTeam": False,
                "relationshipStrength": "WARM", "introducedById": None, "introDistance": 1,
                "createdAt": "2026-08-01T00:00:00Z",
                "createdBy": {"source": "CALENDAR", "workspaceMemberId": "member-1", "name": "Jocy"},
                "xLink": {"primaryLinkUrl": "https://x.com/ada"},
            }}]}}
        if "InteractionOwners" in query:
            member = {"workspaceMemberId": "member-1", "workspaceMember": {
                "id": "member-1", "name": {"firstName": "Jocy", "lastName": ""},
            }}
            return {
                "messageParticipants": {"edges": [{"node": {"messageId": "m1", "message": {
                    "receivedAt": "2026-08-10T00:00:00Z",
                    "messageParticipants": {"edges": [{"node": member}]},
                }}}]},
                "calendarEventParticipants": {"edges": [{"node": {"calendarEventId": "c1", "calendarEvent": {
                    "startsAt": "2026-08-11T00:00:00Z", "isCanceled": False,
                    "calendarEventParticipants": {"edges": [{"node": member}]},
                }}}]},
            }
        if "ReferralsTo" in query:
            return {"refers": {"edges": []}}
        if "SocialConnections" in query:
            return {"socialConnections": {"edges": []}}
        if "CompanyConnections" in query:
            return {"companyConnections": {"edges": []}}
        if "PastEmployers" in query:
            return {"companies": {"edges": []}}
        if "PeopleByIds" in query:
            return {"people": {"edges": []}}
        raise AssertionError(query)


class ReferralTwenty(FakeTwenty):
    def _referrals(self, company_id, person_ids):
        return [{
            "id": "ref-1", "progress": "IN_PROGRESS",
            "from": {"id": "connector-1", "name": {"firstName": "Mario", "lastName": ""}, "isIosgTeam": True},
            "to": {"id": "founder-1", "name": {"firstName": "Ada", "lastName": "Founder"}},
            "accountOwner": None,
        }]


class InvestorTwenty(FakeTwenty):
    def _find_target(self, query, kind):
        if query == "Fund One":
            return [{"id": "fund-1", "name": "Fund One"}]
        return super()._find_target(query, kind)

    def _people(self, company_id):
        if company_id == "fund-1":
            return [{
                "id": "investor-1", "name": {"firstName": "Ivy", "lastName": "Investor"},
                "jobTitle": "Partner", "companyId": "fund-1", "isIosgTeam": False,
                "relationshipStrength": "WARM", "introducedById": None, "introDistance": 0,
                "createdAt": "2026-08-01T00:00:00Z",
                "createdBy": {"source": "API", "workspaceMemberId": None, "name": "Mac studio claude"},
                "xLink": {"primaryLinkUrl": "https://x.com/ivy"},
            }]
        return super()._people(company_id)


class CreatorFallbackTwenty(FakeTwenty):
    def _people(self, company_id):
        people = super()._people(company_id)
        people[0].update({
            "introDistance": 0,
            "createdBy": {"source": "WORKFLOW", "workspaceMemberId": None, "name": "Workflow"},
        })
        return people

    def _interaction_owners(self, person_id):
        return []


def test_twenty_builds_privacy_safe_ranked_path():
    result = IntroductionPathService(FakeTwenty()).search("Acme", QueryKind.COMPANY_NAME)
    assert result.status == "ok"
    assert result.recommended.path == ["Jocy", "Ada Founder", "Acme"]
    assert result.recommended.confidence == "medium"
    evidence = result.recommended.edges[0].evidence
    assert "1 email interaction" in evidence
    assert "1 meeting" in evidence
    assert "contents were requested" in evidence


def test_existing_referral_ranks_above_interaction_metadata():
    result = IntroductionPathService(ReferralTwenty()).search("Acme", QueryKind.COMPANY_NAME)
    assert result.recommended.path == ["Mario", "Ada Founder", "Acme"]
    assert result.recommended.edges[0].relationship == "referral"
    assert result.recommended.confidence == "high"


def test_investor_enrichment_adds_evidence_backed_fund_path():
    repository = InvestorTwenty()
    target = repository.resolve("Acme", QueryKind.COMPANY_NAME)[0]
    repository.add_investor_paths(target, [{
        "id": "surf-fund-1", "name": "Fund One", "round_name": "Seed",
        "round_date": "2025-01-01", "is_lead": True, "portfolio_verified": True,
        "fund_profile": {"name": "Fund One", "x_accounts": [{"handle": "fundone"}],
                         "members": [{"name": "Ivy Investor"}]},
    }])
    relationships = {edge.relationship for edge in repository.edges()}
    assert {"invested_in", "works_at", "created_by_fallback"} <= relationships
    fallback = next(edge for edge in repository.edges() if edge.relationship == "created_by_fallback")
    assert repository._nodes[fallback.source].label == "Yiping Lu"
    assert fallback.confidence == 0.55
    investment = next(edge for edge in repository.edges() if edge.relationship == "invested_in")
    assert investment.evidence_source == "surf"
    assert "fund portfolio both list" in investment.evidence
    fund = next(node for node in repository.nodes() if node.kind == "fund")
    assert fund.x_handle == "fundone"


def test_created_by_is_used_only_when_direct_owner_is_unresolved():
    result = IntroductionPathService(CreatorFallbackTwenty()).search("Acme", QueryKind.COMPANY_NAME)
    assert result.recommended.path == ["Yiping Lu", "Ada Founder", "Acme"]
    assert result.recommended.edges[0].relationship == "created_by_fallback"
    assert result.recommended.edges[0].confidence == 0.55


@pytest.mark.parametrize(
    "raw_name",
    ["Yiping Lu", "Mac studio claude", "Workflow", "Yiping Lu MCP", "MCP Member"],
)
def test_creator_aliases_are_bundled_as_yiping_lu(raw_name):
    assert _creator_name({"createdBy": {"name": raw_name}}) == "Yiping Lu"


def test_other_creator_names_are_unchanged():
    assert _creator_name({"createdBy": {"name": "Momir Amidzic"}}) == "Momir Amidzic"
