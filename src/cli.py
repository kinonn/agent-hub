"""Administrative and database operations CLI (python -m src.cli)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from sqlalchemy import text

from src.config import get_settings
from src.database.checks import check_startup
from src.database.connection import db
from src.errors import AgentHubError
from src.services.artifact_service import artifact_service
from src.services.auth_service import auth_service
from src.services.maintenance_service import backup_database, restore_database


def _password(args) -> str:
    if getattr(args, "password_file", None):
        password = Path(args.password_file).read_text().rstrip("\r\n")
    else:
        password = getpass.getpass("Password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match")
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    return password


async def _run(args) -> None:
    await db.initialize()
    command = args.command
    if command == "create-admin":
        result = await auth_service.create_first_admin(
            args.email, _password(args), args.display_name
        )
        print(f"Created administrator {result['email']} ({result['id']})")
    elif command == "create-user":
        result = await auth_service.create_user(
            args.email,
            _password(args),
            args.display_name or args.email.split("@", 1)[0],
            args.role,
        )
        print(f"Created {result['role']} user {result['email']} ({result['id']})")
    elif command == "create-agent":
        result = await auth_service.create_agent(
            args.name,
            args.agent_type,
            args.description,
            args.capability,
            args.execution_mode,
        )
        print(f"Agent ID: {result['agent']['id']}")
        print(f"Agent token (shown once): {result['token']}")
    elif command == "rotate-token":
        result = await auth_service.rotate_agent_token(args.agent_id)
        print(f"Token ID: {result['token_id']}")
        print(f"Agent token (shown once): {result['token']}")
    elif command == "revoke-token":
        await auth_service.revoke_agent_token(args.agent_id, args.token_id)
        print("Token revoked.")
    elif command == "disable-agent":
        await auth_service.set_agent_status(args.agent_id, "disabled")
        print("Agent disabled and active tokens revoked.")
    elif command == "create-project":
        result = await auth_service.create_project(
            args.slug, args.name, args.description, args.instructions
        )
        print(f"Project created: {result['slug']} ({result['id']})")
    elif command == "add-member":
        async with db.engine.connect() as conn:
            project = (
                (
                    await conn.execute(
                        text("SELECT id FROM projects WHERE id=:ref OR slug=:ref"),
                        {"ref": args.project},
                    )
                )
                .mappings()
                .first()
            )
        if not project:
            raise ValueError("Project not found")
        await auth_service.set_agent_membership(project["id"], args.agent_id, args.role)
        print(f"Added {args.agent_id} to {args.project} as {args.role}.")
    elif command == "backup":
        path = await backup_database(db, get_settings())
        print(path)
    elif command == "restore":
        await db.close()
        await restore_database(Path(args.backup), get_settings())
        print("Database restored; run check-db before starting the stack.")
    elif command == "check-db":
        await check_startup(db, get_settings())
        async with db.engine.connect() as conn:
            integrity = (await conn.exec_driver_sql("PRAGMA integrity_check")).scalar_one()
            version = (
                await conn.exec_driver_sql("SELECT version_num FROM alembic_version")
            ).scalar_one()
            journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
        print(f"integrity_check={integrity} journal_mode={journal} schema={version}")
    elif command == "reindex":
        if args.refresh:
            changed = await artifact_service.refresh_published()
            print(f"Refreshed {changed} published artifacts from GitHub.")
        count = await artifact_service.reindex()
        print(f"Indexed {count} published artifacts.")
    else:
        raise ValueError(f"Unknown command {command}")
    await db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create-admin", "create-user"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--email", required=True)
        cmd.add_argument("--display-name")
        cmd.add_argument("--password-file")
        if name == "create-user":
            cmd.add_argument("--role", choices=("admin", "member"), default="member")
    cmd = commands.add_parser("create-agent")
    cmd.add_argument("--name", required=True)
    cmd.add_argument(
        "--agent-type", choices=("hermes", "opencode", "copilot", "other"), default="other"
    )
    cmd.add_argument("--description")
    cmd.add_argument("--capability", action="append", default=[])
    cmd.add_argument("--execution-mode", choices=("poll", "adapter"), default="poll")
    cmd = commands.add_parser("rotate-token")
    cmd.add_argument("agent_id")
    cmd = commands.add_parser("revoke-token")
    cmd.add_argument("agent_id")
    cmd.add_argument("token_id")
    cmd = commands.add_parser("disable-agent")
    cmd.add_argument("agent_id")
    cmd = commands.add_parser("create-project")
    cmd.add_argument("--slug", required=True)
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--description")
    cmd.add_argument("--instructions")
    cmd = commands.add_parser("add-member")
    cmd.add_argument("project")
    cmd.add_argument("agent_id")
    cmd.add_argument(
        "--role", choices=("reader", "contributor", "maintainer"), default="contributor"
    )
    commands.add_parser("backup")
    cmd = commands.add_parser("restore")
    cmd.add_argument("backup")
    commands.add_parser("check-db")
    cmd = commands.add_parser("reindex")
    cmd.add_argument("--refresh", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_run(args))
    except (AgentHubError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
