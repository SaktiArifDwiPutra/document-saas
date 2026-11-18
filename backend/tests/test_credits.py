"""Credit wallet, ledger, reservation, reconciliation, and PG concurrency tests (Milestone M4)."""

import asyncio
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.api.v1.deps import get_email_service, get_rate_limiter
from app.core.config import Settings
from app.core.errors import AppError
from app.core.rate_limit import SlidingWindowRateLimiter
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.models.credit import CreditPackage, CreditReservation, CreditTransaction, CreditWallet
from app.services.credit import CreditService
from app.services.email import StubEmailService


def _build_credit_test_app(
    tmp_path: Path,
    *,
    db_name: str,
    beta_complimentary_credits: int | None = None,
) -> tuple[AsyncEngine, FastAPI, Settings]:
    db_file = tmp_path / db_name
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        jwt_secret_key="test-jwt-secret-for-unit-tests-only-minimum-32-chars",
        beta_complimentary_credits=beta_complimentary_credits,
        rate_limit_register_ip_max=100,
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    return engine, app, settings


async def _register_and_get_personal_ws(
    app: FastAPI,
    *,
    email: str,
    display_name: str,
) -> tuple[str, UUID, UUID]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        reg = await c.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "StrongPassword123!",
                "display_name": display_name,
            },
        )
        assert reg.status_code == 201
        data = reg.json()["data"]
        token = str(data["access_token"])
        user_id = UUID(str(data["user"]["id"]))

        ws_res = await c.get(
            "/api/v1/workspaces",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ws_res.status_code == 200
        personal_ws_id = UUID(str(ws_res.json()["data"]["items"][0]["id"]))
        return token, user_id, personal_ws_id


async def test_configurable_beta_complimentary_credits_and_ledger_reconciliation(
    tmp_path: Path,
) -> None:
    """Beta complimentary credits stay unset by default and grant via ledger when configured."""
    # Default beta_complimentary_credits=None -> starts at 0 with no grant transaction
    eng_none, app_none, _ = _build_credit_test_app(
        tmp_path, db_name="beta_none.sqlite", beta_complimentary_credits=None
    )
    async with eng_none.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf_none = create_session_factory(eng_none)
    try:
        tok_none, _, ws_none = await _register_and_get_personal_ws(
            app_none, email="beta-none@example.com", display_name="Beta None"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app_none), base_url="http://testserver"
        ) as c:
            wallet_res = await c.get(
                f"/api/v1/workspaces/{ws_none}/credits",
                headers={"Authorization": f"Bearer {tok_none}"},
            )
            assert wallet_res.status_code == 200
            assert wallet_res.json()["data"]["available_balance"] == 0
            assert wallet_res.json()["data"]["reserved_balance"] == 0
            assert wallet_res.json()["data"]["total_balance"] == 0

            tx_res = await c.get(
                f"/api/v1/workspaces/{ws_none}/credits/transactions",
                headers={"Authorization": f"Bearer {tok_none}"},
            )
            assert tx_res.status_code == 200
            assert tx_res.json()["data"]["total"] == 0

        async with sf_none() as session:
            assert (
                await CreditService(
                    session=session,
                    settings=Settings(environment="test"),
                ).verify_wallet_reconciliation(workspace_id=ws_none)
                is True
            )
    finally:
        await eng_none.dispose()

    # Configured beta_complimentary_credits=25 -> granted atomically to both Personal and Org
    eng_25, app_25, settings_25 = _build_credit_test_app(
        tmp_path, db_name="beta_25.sqlite", beta_complimentary_credits=25
    )
    async with eng_25.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf_25 = create_session_factory(eng_25)
    try:
        tok_25, _, personal_ws_25 = await _register_and_get_personal_ws(
            app_25, email="beta-25@example.com", display_name="Beta 25"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app_25), base_url="http://testserver"
        ) as c:
            org_res = await c.post(
                "/api/v1/workspaces",
                headers={"Authorization": f"Bearer {tok_25}"},
                json={"name": "Org Beta 25"},
            )
            assert org_res.status_code == 201
            org_ws_25 = UUID(str(org_res.json()["data"]["id"]))

            for ws_id in (personal_ws_25, org_ws_25):
                w_res = await c.get(
                    f"/api/v1/workspaces/{ws_id}/credits",
                    headers={"Authorization": f"Bearer {tok_25}"},
                )
                assert w_res.status_code == 200
                w_data = w_res.json()["data"]
                assert w_data["available_balance"] == 25
                assert w_data["reserved_balance"] == 0
                assert w_data["total_balance"] == 25

                tx_res = await c.get(
                    f"/api/v1/workspaces/{ws_id}/credits/transactions",
                    headers={"Authorization": f"Bearer {tok_25}"},
                )
                assert tx_res.status_code == 200
                items = tx_res.json()["data"]["items"]
                assert len(items) == 1
                assert items[0]["transaction_type"] == "grant"
                assert items[0]["amount"] == 25

                async with sf_25() as session:
                    svc = CreditService(session=session, settings=settings_25)
                    assert await svc.verify_wallet_reconciliation(workspace_id=ws_id) is True
    finally:
        await eng_25.dispose()


async def test_credit_reservation_commit_boundary_no_double_render_charge_release_and_refund(
    tmp_path: Path,
) -> None:
    """Enforce commit boundary, idempotency, DOCX non-charging, release, and refund rules."""
    engine, app, settings = _build_credit_test_app(
        tmp_path, db_name="lifecycle.sqlite", beta_complimentary_credits=10
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        _, user_id, ws_id = await _register_and_get_personal_ws(
            app, email="lifecycle@example.com", display_name="Lifecycle"
        )
        async with session_factory() as session:
            svc = CreditService(session=session, settings=settings)

            # Insufficient balance rejected with 402 INSUFFICIENT_CREDITS
            with pytest.raises(AppError) as low_exc:
                await svc.reserve_credits(
                    workspace_id=ws_id,
                    amount=15,
                    idempotency_key="res-too-big",
                    created_by_user_id=user_id,
                )
            assert low_exc.value.status_code == 402
            assert low_exc.value.code == "INSUFFICIENT_CREDITS"
            assert low_exc.value.details == {"required": 15, "available": 10}

            # DOCX render and approved re-render must never charge another AI credit
            with pytest.raises(AppError) as render_exc:
                await svc.reserve_credits(
                    workspace_id=ws_id,
                    amount=4,
                    idempotency_key="res-render-1",
                    job_type="docx_render",
                )
            assert render_exc.value.status_code == 400

            with pytest.raises(AppError) as rerender_exc:
                await svc.reserve_credits(
                    workspace_id=ws_id,
                    amount=4,
                    idempotency_key="res-rerender-1",
                    is_rerender_of_approved=True,
                )
            assert rerender_exc.value.status_code == 400

            # Reserve 4 credits idempotently
            job_1 = uuid4()
            r1 = await svc.reserve_credits(
                workspace_id=ws_id,
                amount=4,
                idempotency_key="res-ai-1",
                job_id=job_1,
                created_by_user_id=user_id,
            )
            r1_id = r1.id
            r1_dup = await svc.reserve_credits(
                workspace_id=ws_id,
                amount=4,
                idempotency_key="res-ai-1",
                job_id=job_1,
                created_by_user_id=user_id,
            )
            assert r1_dup.id == r1_id

            with pytest.raises(AppError) as conflict_exc:
                await svc.reserve_credits(
                    workspace_id=ws_id,
                    amount=5,
                    idempotency_key="res-ai-1",
                    job_id=job_1,
                )
            assert conflict_exc.value.status_code == 409
            assert conflict_exc.value.code == "IDEMPOTENCY_CONFLICT"

            # Commit boundary: cannot commit before document reaches review_required
            for bad_status in ("draft", "processing", "failed"):
                with pytest.raises(AppError) as boundary_exc:
                    await svc.commit_reservation(
                        workspace_id=ws_id,
                        reservation_id=r1_id,
                        document_status=bad_status,
                        actor_user_id=user_id,
                    )
                assert boundary_exc.value.status_code == 409
                assert boundary_exc.value.code == "INVALID_JOB_STATE"

            # Commit succeeds once AI draft is persisted as review_required
            committed = await svc.commit_reservation(
                workspace_id=ws_id,
                reservation_id=r1_id,
                document_status="review_required",
                actor_user_id=user_id,
            )
            assert committed.status == "committed"

            # Duplicate commit callback is idempotent
            committed_dup = await svc.commit_reservation(
                workspace_id=ws_id,
                reservation_id=r1_id,
                document_status="review_required",
                actor_user_id=user_id,
            )
            assert committed_dup.id == committed.id

            # Committed reservation cannot be released; must use compensating refund
            with pytest.raises(AppError) as rel_committed_exc:
                await svc.release_reservation(
                    workspace_id=ws_id,
                    reservation_id=r1_id,
                )
            assert rel_committed_exc.value.status_code == 409

            # Reserve 3 credits for failed AI job -> release restores available_balance
            r2 = await svc.reserve_credits(
                workspace_id=ws_id,
                amount=3,
                idempotency_key="res-ai-2",
                job_id=uuid4(),
            )
            r2_id = r2.id
            released = await svc.release_reservation(
                workspace_id=ws_id,
                reservation_id=r2_id,
                reason="AI provider timeout",
            )
            assert released.status == "released"
            released_dup = await svc.release_reservation(
                workspace_id=ws_id,
                reservation_id=r2_id,
            )
            assert released_dup.status == "released"

            # Refund committed reservation r1 via compensating ledger entry
            refund_tx = await svc.refund_reservation(
                workspace_id=ws_id,
                reservation_id=r1_id,
                idempotency_key="refund-ai-1",
                reason="Kompensasi kegagalan kualitas draf",
                actor_user_id=user_id,
            )
            assert refund_tx.transaction_type == "refund"
            assert refund_tx.amount == 4
            refund_dup = await svc.refund_reservation(
                workspace_id=ws_id,
                reservation_id=r1_id,
                idempotency_key="refund-ai-1",
            )
            assert refund_dup.id == refund_tx.id
            with pytest.raises(AppError) as double_refund_exc:
                await svc.refund_reservation(
                    workspace_id=ws_id,
                    reservation_id=r1_id,
                    idempotency_key="refund-ai-1-second-key",
                )
            assert double_refund_exc.value.status_code == 409

            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert wallet is not None
            assert wallet.available_balance == 10
            assert wallet.reserved_balance == 0
            assert await svc.verify_wallet_reconciliation(workspace_id=ws_id) is True
    finally:
        await engine.dispose()


class _FakeJobLivenessChecker:
    def __init__(self, active_job_ids: set[UUID]) -> None:
        self.active_job_ids = active_job_ids

    async def is_job_active(self, _session: AsyncSession, job_id: UUID) -> bool:
        return job_id in self.active_job_ids


async def test_stale_reservation_reconciliation_checks_job_liveness_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Reconciliation never releases an active job's reservation, even past expires_at."""
    engine, app, settings = _build_credit_test_app(
        tmp_path, db_name="reconcile.sqlite", beta_complimentary_credits=10
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        _, _, ws_id = await _register_and_get_personal_ws(
            app, email="reconcile@example.com", display_name="Reconcile"
        )
        active_job_id = uuid4()
        dead_job_id = uuid4()

        async with session_factory() as session:
            svc = CreditService(session=session, settings=settings)
            r_active = await svc.reserve_credits(
                workspace_id=ws_id,
                amount=4,
                idempotency_key="stale-active",
                job_id=active_job_id,
                ttl_seconds=60,
            )
            r_dead = await svc.reserve_credits(
                workspace_id=ws_id,
                amount=3,
                idempotency_key="stale-dead",
                job_id=dead_job_id,
                ttl_seconds=60,
            )

            future_now = datetime.now(UTC) + timedelta(minutes=10)
            checker = _FakeJobLivenessChecker(active_job_ids={active_job_id})

            # Without a liveness checker, job-bound reservations must NOT be released blindly.
            assert (
                await svc.reconcile_stale_reservations(
                    workspace_id=ws_id, now=future_now, job_liveness_checker=None
                )
                == []
            )

            # With checker: dead_job_id is expired, active_job_id stays reserved.
            reconciled = await svc.reconcile_stale_reservations(
                workspace_id=ws_id, now=future_now, job_liveness_checker=checker
            )
            assert reconciled == [r_dead.id]

            # Repeat run is idempotent
            assert (
                await svc.reconcile_stale_reservations(
                    workspace_id=ws_id, now=future_now, job_liveness_checker=checker
                )
                == []
            )

            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert wallet is not None
            assert wallet.available_balance == 6
            assert wallet.reserved_balance == 4

            # Once active_job_id finishes/becomes inactive, reconciliation expires it too.
            checker.active_job_ids.clear()
            reconciled_2 = await svc.reconcile_stale_reservations(
                workspace_id=ws_id, now=future_now, job_liveness_checker=checker
            )
            assert reconciled_2 == [r_active.id]

            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert wallet is not None
            assert wallet.available_balance == 10
            assert wallet.reserved_balance == 0
            assert await svc.verify_wallet_reconciliation(workspace_id=ws_id) is True
    finally:
        await engine.dispose()


async def test_credit_endpoints_tenant_isolation_and_packages_catalog(
    tmp_path: Path,
) -> None:
    """Credit endpoints enforce tenant isolation; package catalog hides inactive items."""
    engine, app, _ = _build_credit_test_app(
        tmp_path, db_name="isolation.sqlite", beta_complimentary_credits=15
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        tok_a, _, ws_a = await _register_and_get_personal_ws(
            app, email="credit-a@example.com", display_name="User A"
        )
        tok_b, _, _ = await _register_and_get_personal_ws(
            app, email="credit-b@example.com", display_name="User B"
        )

        async with session_factory() as session:
            session.add_all(
                [
                    CreditPackage(
                        name="Starter Personal",
                        credits=50,
                        price_minor_units=0,
                        currency="IDR",
                        workspace_type="personal",
                        is_active=True,
                    ),
                    CreditPackage(
                        name="Inactive Legacy",
                        credits=100,
                        price_minor_units=0,
                        currency="IDR",
                        workspace_type="all",
                        is_active=False,
                    ),
                ]
            )
            await session.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            assert (
                await c.get(
                    f"/api/v1/workspaces/{ws_a}/credits",
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
            ).status_code == 200
            assert (
                await c.get(
                    f"/api/v1/workspaces/{ws_a}/credits",
                    headers={"Authorization": f"Bearer {tok_b}"},
                )
            ).status_code == 403
            assert (
                await c.get(
                    f"/api/v1/workspaces/{ws_a}/credits/transactions",
                    headers={"Authorization": f"Bearer {tok_b}"},
                )
            ).status_code == 403

            pkg_res = await c.get("/api/v1/credit-packages?workspace_type=personal")
            assert pkg_res.status_code == 200
            pkg_names = [item["name"] for item in pkg_res.json()["data"]["items"]]
            assert pkg_names == ["Starter Personal"]
    finally:
        await engine.dispose()


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


@pytest.fixture(scope="module")
def pg_credit_db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start an ephemeral PostgreSQL 16 cluster, run migrations, and yield its async URL."""
    pg_bin = Path("/usr/lib/postgresql/16/bin")
    initdb_bin = pg_bin / "initdb"
    pg_ctl_bin = pg_bin / "pg_ctl"
    if not initdb_bin.exists() or not pg_ctl_bin.exists():
        pytest.skip("PostgreSQL 16 binaries not installed locally")

    pg_root = tmp_path_factory.mktemp("pg16_credit_concurrency")
    pg_data = pg_root / "pgdata"
    pg_log = pg_root / "pg.log"
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
            f"-k {pg_root} -p {port}",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        admin_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/postgres"
        test_db_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/doc_saas_credit_test"
        asyncio.run(_create_pg_database(admin_url, "doc_saas_credit_test"))

        backend_dir = Path(__file__).resolve().parents[1]
        alembic_cfg = Config(str(backend_dir / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
        command.upgrade(alembic_cfg, "head")
        yield test_db_url
    finally:
        subprocess.run(
            [str(pg_ctl_bin), "-D", str(pg_data), "stop", "-m", "immediate"],
            check=False,
            capture_output=True,
            text=True,
        )


async def test_concurrent_credit_reservations_and_commits_on_pg(
    pg_credit_db_url: str,
) -> None:
    """Concurrent credit reservations and commits never overspend or double-commit on PG16."""
    settings = Settings(
        environment="test",
        database_url=pg_credit_db_url,
        internal_hmac_secret="pg-credit-concurrency-hmac-secret-minimum-32-chars",
        jwt_secret_key="pg-credit-concurrency-jwt-secret-minimum-32-chars",
        beta_complimentary_credits=10,
        rate_limit_register_ip_max=100,
    )
    engine = create_engine(pg_credit_db_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter

    try:
        _, user_id, ws_id = await _register_and_get_personal_ws(
            app, email="pg-credit@example.com", display_name="PG Credit"
        )

        # 8 concurrent attempts to reserve 3 credits from a 10-credit wallet: at most 3 succeed.
        async def try_reserve(idx: int) -> CreditReservation | str:
            async with session_factory() as session:
                svc = CreditService(session=session, settings=settings)
                try:
                    return await svc.reserve_credits(
                        workspace_id=ws_id,
                        amount=3,
                        idempotency_key=f"pg-conc-{idx}",
                        job_id=uuid4(),
                        created_by_user_id=user_id,
                    )
                except AppError as exc:
                    return exc.code

        results = await asyncio.gather(*[try_reserve(i) for i in range(8)])
        succeeded = [r for r in results if isinstance(r, CreditReservation)]
        rejected = [r for r in results if r == "INSUFFICIENT_CREDITS"]
        assert len(succeeded) == 3
        assert len(rejected) == 5

        async with session_factory() as session:
            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert wallet is not None
            assert wallet.available_balance == 1
            assert wallet.reserved_balance == 9

        # 6 concurrent retries with the same idempotency_key deduct the remaining 1 credit once.
        async def try_idempotent_reserve() -> UUID:
            async with session_factory() as session:
                svc = CreditService(session=session, settings=settings)
                res = await svc.reserve_credits(
                    workspace_id=ws_id,
                    amount=1,
                    idempotency_key="pg-same-key",
                    created_by_user_id=user_id,
                )
                return res.id

        same_ids = await asyncio.gather(*[try_idempotent_reserve() for _ in range(6)])
        assert len(set(same_ids)) == 1

        # 6 concurrent commit_reservation calls on succeeded[0] commit exactly once.
        target_res_id = succeeded[0].id

        async def try_commit() -> str:
            async with session_factory() as session:
                svc = CreditService(session=session, settings=settings)
                committed = await svc.commit_reservation(
                    workspace_id=ws_id,
                    reservation_id=target_res_id,
                    document_status="review_required",
                    actor_user_id=user_id,
                )
                return committed.status

        commit_statuses = await asyncio.gather(*[try_commit() for _ in range(6)])
        assert commit_statuses == ["committed"] * 6

        async with session_factory() as session:
            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert wallet is not None
            assert wallet.available_balance == 0
            assert wallet.reserved_balance == 7

            commit_txs = (
                await session.scalars(
                    select(CreditTransaction).where(
                        CreditTransaction.workspace_id == ws_id,
                        CreditTransaction.transaction_type == "commit",
                    )
                )
            ).all()
            assert len(commit_txs) == 1
            assert commit_txs[0].amount == -3
            credit_svc = CreditService(session=session, settings=settings)
            assert await credit_svc.verify_wallet_reconciliation(workspace_id=ws_id) is True
    finally:
        await engine.dispose()

