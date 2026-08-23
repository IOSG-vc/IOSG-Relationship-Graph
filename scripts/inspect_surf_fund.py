#!/usr/bin/env python3
"""Print raw Surf fund search, profile, and portfolio responses."""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.asksurf.ai/gateway/v1"


def request(api_key: str, path: str, params: dict[str, object], timeout: int) -> dict:
    response = requests.get(
        f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {api_key}"},
        params=params, timeout=timeout,
    )
    print(f"\nGET {response.url}\nHTTP {response.status_code}")
    payload = response.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    response.raise_for_status()
    return payload


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Show raw Surf fund-network responses.")
    parser.add_argument("query", help="Fund name, for example: Polychain Capital")
    parser.add_argument("--limit", type=int, default=20, help="Portfolio page size")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    api_key = os.getenv("SURF_API_KEY")
    if not api_key:
        print("SURF_API_KEY is missing.", file=sys.stderr)
        return 2
    try:
        search = request(api_key, "/search/fund", {"q": args.query, "limit": 5}, args.timeout)
        matches = search.get("data") or []
        if not matches:
            print(f"No Surf fund found for {args.query!r}.", file=sys.stderr)
            return 1
        fund_id = matches[0]["id"]
        request(api_key, "/fund/detail", {"id": fund_id}, args.timeout)
        request(api_key, "/fund/portfolio", {"id": fund_id, "limit": args.limit}, args.timeout)
    except (requests.RequestException, ValueError) as exc:
        print(f"Surf request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
