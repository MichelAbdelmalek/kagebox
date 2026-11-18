"""Admin CLI. There are no default accounts: the first admin is created here.

    docker compose exec api python -m kagebox_api.cli create-user --email you@example.com --admin

The password is read from the terminal (not echoed) and never logged.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from kagebox_api.db import Database, User, audit, make_engine
from kagebox_api.security import hash_password
from pipeline.common.ids import new_id
from pipeline.common.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kagebox_api.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--admin", action="store_true")
    sub.add_parser("migrate", help="apply database migrations")
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        from pathlib import Path

        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(Path(__file__).with_name("alembic.ini"))), "head")
        return 0

    if args.cmd == "create-user":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat password: "):
            print("passwords do not match", file=sys.stderr)
            return 1
        db = Database(make_engine(get_settings()))
        with next(db.session()) as s:
            email = args.email.strip().lower()
            if s.scalar(select(User).where(User.email == email)):
                print("user already exists", file=sys.stderr)
                return 1
            try:
                user = User(
                    id=new_id("user"),
                    email=email,
                    password_hash=hash_password(pw),
                    role="admin" if args.admin else "member",
                )
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            s.add(user)
            audit(s, None, "user.create", user.id, role=user.role)
            s.commit()
            print(f"created {user.role} {user.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
