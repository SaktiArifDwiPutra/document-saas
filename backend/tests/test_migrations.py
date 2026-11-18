"""Tests for Alembic migration configuration and baseline upgrade/downgrade."""

import asyncio
import socket
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _create_pg_database(admin_url: str, db_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await engine.dispose()


async def _fetch_alembic_versions(db_url: str) -> list[str]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return [str(row[0]) for row in result.fetchall()]
    finally:
        await engine.dispose()


async def _fetch_pg_tables(db_url: str) -> set[str]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {str(row[0]) for row in result.fetchall()}
    finally:
        await engine.dispose()


def test_alembic_baseline_upgrade_and_downgrade(tmp_path: Path) -> None:
    """Alembic migrations should upgrade to head and downgrade cleanly on SQLite."""
    backend_dir = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "alembic_test.sqlite"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", async_url)

    script_dir = ScriptDirectory.from_config(alembic_cfg)
    assert script_dir.get_heads() == ["0004_m4_credit_engine"]

    command.upgrade(alembic_cfg, "head")

    sync_engine = create_engine(sync_url)
    with sync_engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert {
            "alembic_version",
            "users",
            "auth_sessions",
            "auth_action_tokens",
            "workspaces",
            "workspace_members",
            "invitations",
            "audit_logs",
            "credit_wallets",
            "credit_transactions",
            "credit_reservations",
            "credit_packages",
        }.issubset(tables)
    sync_engine.dispose()

    command.downgrade(alembic_cfg, "base")


def test_alembic_baseline_upgrade_and_downgrade_postgresql(tmp_path: Path) -> None:
    """Alembic baseline migration should upgrade and downgrade against PostgreSQL 16."""
    pg_bin = Path("/usr/lib/postgresql/16/bin")
    initdb_bin = pg_bin / "initdb"
    pg_ctl_bin = pg_bin / "pg_ctl"
    if not initdb_bin.exists() or not pg_ctl_bin.exists():
        pytest.skip("PostgreSQL 16 binaries not installed locally")

    pg_data = tmp_path / "pgdata"
    pg_log = tmp_path / "pg.log"
    port = _find_free_port()

    subprocess.run(
        [str(initdb_bin), "-D", str(pg_data), "-U", "postgres", "--auth=trust"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(pg_ctl_bin),
            "-D",
            str(pg_data),
            "-l",
            str(pg_log),
            "-o",
            f"-k {tmp_path} -p {port}",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        admin_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/postgres"
        test_db_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/doc_saas_test"
        asyncio.run(_create_pg_database(admin_url, "doc_saas_test"))

        backend_dir = Path(__file__).resolve().parents[1]
        alembic_cfg = Config(str(backend_dir / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)

        command.upgrade(alembic_cfg, "head")
        assert asyncio.run(_fetch_alembic_versions(test_db_url)) == [
            "0004_m4_credit_engine"
        ]
        pg_tables = asyncio.run(_fetch_pg_tables(test_db_url))
        assert {
            "alembic_version",
            "users",
            "auth_sessions",
            "auth_action_tokens",
            "workspaces",
            "workspace_members",
            "invitations",
            "audit_logs",
            "credit_wallets",
            "credit_transactions",
            "credit_reservations",
            "credit_packages",
        }.issubset(pg_tables)

        command.downgrade(alembic_cfg, "base")
        assert asyncio.run(_fetch_alembic_versions(test_db_url)) == []
        pg_tables_after = asyncio.run(_fetch_pg_tables(test_db_url))
        assert {
            "users",
            "auth_sessions",
            "auth_action_tokens",
            "workspaces",
            "workspace_members",
            "invitations",
            "audit_logs",
            "credit_wallets",
            "credit_transactions",
            "credit_reservations",
            "credit_packages",
        }.isdisjoint(pg_tables_after)
    finally:
        subprocess.run(
            [str(pg_ctl_bin), "-D", str(pg_data), "stop", "-m", "immediate"],
            check=False,
            capture_output=True,
            text=True,
        )
