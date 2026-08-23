#!/usr/bin/env python3
"""Print raw responses from the Surf API endpoints used by this project."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests
from dotenv import load_dotenv

SURF_PROJECT_DETAIL_URL = "https://api.asksurf.ai/gateway/v1/project/detail"
USED_FIELDS = ("search", "team", "funding")


def get_json(api_key: str, url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        params=params, timeout=timeout,
    )
    print(f"\nGET {response.url}")
    print(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        print(response.text)
        response.raise_for_status()
        raise RuntimeError("Surf returned a non-JSON response")
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    response.raise_for_status()
    return payload


def search_project(api_key: str, query: str, timeout: int) -> dict[str, Any]:
    return get_json(
        api_key, "https://api.asksurf.ai/gateway/v1/search/project",
        {"q": query.lstrip("@"), "limit": 5}, timeout,
    )


def project_detail(api_key: str, project_ref: str, field: str, timeout: int) -> dict[str, Any]:
    lookup = "id" if len(project_ref) == 36 and project_ref.count("-") == 4 else "q"
    return get_json(api_key, SURF_PROJECT_DETAIL_URL, {lookup: project_ref, "fields": field}, timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show raw Surf project-detail responses used by the relationship graph.",
    )
    parser.add_argument("query", help="Project name or X handle (for example: EigenLayer)")
    parser.add_argument(
        "--field", choices=(*USED_FIELDS, "all"), default="all",
        help="Surf field to request (default: all fields used by this app)",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout in seconds")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    api_key = os.getenv("SURF_API_KEY")
    if not api_key:
        print("SURF_API_KEY is missing. Add it to .env or export it in your shell.", file=sys.stderr)
        return 2
    fields = USED_FIELDS if args.field == "all" else (args.field,)
    try:
        project_ref = args.query.lstrip("@")
        search_payload: dict[str, Any] | None = None
        if args.field != "search":
            search_payload = search_project(api_key, args.query, args.timeout)
            results = search_payload.get("data") or []
            if not results:
                print(f"Surf project search found no match for {args.query!r}.", file=sys.stderr)
                return 1
            project_ref = str(results[0]["id"])
        for field in fields:
            if field == "search":
                payload = search_payload or search_project(api_key, args.query, args.timeout)
                results = payload.get("data") or []
                if results:
                    project_ref = str(results[0]["id"])
            else:
                project_detail(api_key, project_ref, field, args.timeout)
    except requests.RequestException as exc:
        print(f"Surf request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
