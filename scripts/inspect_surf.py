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
USED_FIELDS = ("team", "funding")


def project_detail(api_key: str, handle: str, field: str, timeout: int) -> dict[str, Any]:
    response = requests.get(
        SURF_PROJECT_DETAIL_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        params={"handle": handle.lstrip("@"), "fields": field},
        timeout=timeout,
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show raw Surf project-detail responses used by the relationship graph.",
    )
    parser.add_argument("handle", help="Project X handle, with or without @ (for example: eigenlayer)")
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
        for field in fields:
            project_detail(api_key, args.handle, field, args.timeout)
    except requests.RequestException as exc:
        print(f"Surf request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
