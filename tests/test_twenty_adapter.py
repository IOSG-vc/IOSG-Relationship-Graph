from relationship_graph.adapters.twenty import TwentyGraphRepository
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
