from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests


class RelationshipOwnerClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def resolve(self, twenty_person_id: str) -> dict[str, Any] | None:
        if not self.base_url or not self.api_key:
            return None
        try:
            response = requests.get(
                f"{self.base_url}/people/{quote(twenty_person_id, safe='')}/relationship",
                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None
        owner = payload.get("owner") or {}
        evidence = payload.get("evidence") or {}
        if not owner.get("name"):
            return None
        return {
            "id": owner.get("id") or owner["name"], "name": owner["name"],
            "email_count": int(evidence.get("email_count") or 0),
            "meeting_count": int(evidence.get("meeting_count") or 0),
            "last": evidence.get("last_interaction"),
        }

