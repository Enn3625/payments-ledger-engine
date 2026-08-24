"""Create or update an API user.

There is deliberately no self-service signup endpoint: an API that mints its
own admins is a liability. Accounts are provisioned out of band, which is what
this script is for.

    python -m scripts.create_user --email admin@demo.local --role admin
    python -m scripts.create_user --email viewer@demo.local --role viewer --password demo1234
    python -m scripts.create_user --email admin@demo.local --role admin --reset-password

Omit `--password` and you will be prompted, so the password never lands in your
shell history.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import User, UserRole  # noqa: E402
from app.services.auth import AuthError, create_user, hash_password  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_user", description="Create or update an API user."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in UserRole],
        help="admin writes and retries webhooks; viewer reads only",
    )
    parser.add_argument("--password", help="prompted for if omitted")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="if the user exists, set a new password and role instead of failing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    email = args.email.strip().lower()
    role = UserRole(args.role)

    password = args.password or getpass.getpass(f"password for {email}: ")
    if not password:
        print("password must not be empty", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        existing = session.scalar(select(User).where(User.email == email))

        if existing is not None and not args.reset_password:
            print(
                f"{email} already exists with role {existing.role.value}. "
                "Pass --reset-password to change it.",
                file=sys.stderr,
            )
            return 1

        try:
            if existing is not None:
                existing.password_hash = hash_password(password)
                existing.role = role
                existing.is_active = True
                action = "updated"
            else:
                create_user(session, email=email, password=password, role=role)
                action = "created"
            session.commit()
        except AuthError as error:
            print(str(error), file=sys.stderr)
            return 2

    print(f"{action} {email} ({role.value})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
