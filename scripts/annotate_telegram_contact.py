"""Create or update a manual Telegram contact annotation in Neon."""

from __future__ import annotations

import argparse
import os

import psycopg2
from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("telegram_user_id", type=int)
    parser.add_argument("--company")
    parser.add_argument("--domain")
    parser.add_argument("--role")
    parser.add_argument("--x-username")
    parser.add_argument("--linkedin-url")
    parser.add_argument(
        "--status",
        choices=("unverified", "probable", "verified", "rejected"),
        default="unverified",
    )
    parser.add_argument("--notes")
    parser.add_argument("--updated-by", required=True)
    args = parser.parse_args()

    load_dotenv()
    database_url = os.getenv("NEON_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("NEON_DATABASE_URL is not configured")

    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            insert into deals.telegram_contact_annotations (
                telegram_user_id, company_name, company_domain, role, x_username,
                linkedin_url, identity_status, notes, updated_by
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (telegram_user_id) do update set
                company_name = excluded.company_name,
                company_domain = excluded.company_domain,
                role = excluded.role,
                x_username = excluded.x_username,
                linkedin_url = excluded.linkedin_url,
                identity_status = excluded.identity_status,
                notes = excluded.notes,
                updated_by = excluded.updated_by,
                updated_at = now()
            """,
            (
                args.telegram_user_id,
                args.company,
                args.domain,
                args.role,
                args.x_username,
                args.linkedin_url,
                args.status,
                args.notes,
                args.updated_by,
            ),
        )
    print(f"Updated Telegram contact {args.telegram_user_id}.")


if __name__ == "__main__":
    main()
