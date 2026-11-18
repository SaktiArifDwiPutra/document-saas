"""Credit wallet, append-only ledger, reservation, and reconciliation service (Milestone M4)."""

from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.permissions import (
    WorkspaceRole,
    WorkspaceType,
    require_workspace_action,
)
from app.core.security import ensure_utc
from app.models.auth import User
from app.models.credit import (
    CreditPackage,
    CreditReservation,
    CreditTransaction,
    CreditWallet,
)
from app.schemas.credit import (
    CreditPackageListResponse,
    CreditPackageResponse,
    CreditReservationResponse,
    CreditTransactionListResponse,
    CreditTransactionResponse,
    CreditWalletResponse,
    ReservationStatus,
    TransactionType,
)
from app.services.workspace_access import (
    record_workspace_audit,
    require_active_workspace_membership,
)


class JobLivenessChecker(Protocol):
    """Protocol verifying whether a job associated with a reservation is still active."""

    async def is_job_active(self, session: AsyncSession, job_id: UUID) -> bool:
        """Return True when the job is still queued, running, or validating."""
        ...


def to_wallet_response(wallet: CreditWallet) -> CreditWalletResponse:
    """Convert CreditWallet ORM model to public API contract."""
    return CreditWalletResponse(
        id=wallet.id,
        workspace_id=wallet.workspace_id,
        available_balance=wallet.available_balance,
        reserved_balance=wallet.reserved_balance,
        total_balance=wallet.available_balance + wallet.reserved_balance,
        updated_at=ensure_utc(wallet.updated_at),
    )


def to_transaction_response(tx: CreditTransaction) -> CreditTransactionResponse:
    """Convert CreditTransaction ORM model to public API contract."""
    return CreditTransactionResponse(
        id=tx.id,
        wallet_id=tx.wallet_id,
        workspace_id=tx.workspace_id,
        transaction_type=cast(TransactionType, tx.transaction_type),
        amount=tx.amount,
        reference_type=tx.reference_type,
        reference_id=tx.reference_id,
        idempotency_key=tx.idempotency_key,
        description=tx.description,
        created_by_user_id=tx.created_by_user_id,
        created_at=ensure_utc(tx.created_at),
    )


def to_reservation_response(reservation: CreditReservation) -> CreditReservationResponse:
    """Convert CreditReservation ORM model to response contract."""
    return CreditReservationResponse(
        id=reservation.id,
        wallet_id=reservation.wallet_id,
        workspace_id=reservation.workspace_id,
        job_id=reservation.job_id,
        amount=reservation.amount,
        status=cast(ReservationStatus, reservation.status),
        expires_at=ensure_utc(reservation.expires_at),
        idempotency_key=reservation.idempotency_key,
        created_at=ensure_utc(reservation.created_at),
        resolved_at=ensure_utc(reservation.resolved_at) if reservation.resolved_at else None,
    )


def _to_package_response(package: CreditPackage) -> CreditPackageResponse:
    return CreditPackageResponse(
        id=package.id,
        name=package.name,
        credits=package.credits,
        price_minor_units=package.price_minor_units,
        currency=package.currency,
        workspace_type=package.workspace_type,
        is_active=package.is_active,
    )


class CreditService:
    """Owns atomic workspace credit wallets, reservations, commits, releases, and refunds."""

    def __init__(self, *, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def initialize_workspace_wallet(
        self,
        workspace_id: UUID,
        *,
        created_by_user_id: UUID | None = None,
        commit: bool = True,
    ) -> CreditWallet:
        """Create workspace wallet (if missing) and apply configurable beta grant idempotently."""
        now = datetime.now(UTC)
        wallet = await self._session.scalar(
            select(CreditWallet)
            .where(CreditWallet.workspace_id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is None:
            try:
                async with self._session.begin_nested():
                    wallet = CreditWallet(
                        workspace_id=workspace_id,
                        available_balance=0,
                        reserved_balance=0,
                        updated_at=now,
                    )
                    self._session.add(wallet)
                    await self._session.flush()
            except IntegrityError:
                wallet = await self._session.scalar(
                    select(CreditWallet)
                    .where(CreditWallet.workspace_id == workspace_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
        if wallet is None:
            raise AppError(
                code="INTERNAL_ERROR",
                message="Gagal menginisialisasi dompet kredit workspace.",
                status_code=500,
            )

        beta_amount = self._settings.beta_complimentary_credits
        if beta_amount is not None and beta_amount > 0:
            beta_key = f"initial_beta_grant:{workspace_id}"
            existing_grant = await self._session.scalar(
                select(CreditTransaction).where(
                    CreditTransaction.workspace_id == workspace_id,
                    CreditTransaction.idempotency_key == beta_key,
                )
            )
            if existing_grant is None:
                wallet.available_balance += beta_amount
                wallet.updated_at = now
                self._session.add(
                    CreditTransaction(
                        wallet_id=wallet.id,
                        workspace_id=workspace_id,
                        transaction_type="grant",
                        amount=beta_amount,
                        reference_type="beta_complimentary_grant",
                        reference_id=str(workspace_id),
                        idempotency_key=beta_key,
                        description="Alokasi kredit beta awal yang dapat dikonfigurasi.",
                        created_by_user_id=created_by_user_id,
                        created_at=now,
                    )
                )
                record_workspace_audit(
                    self._session,
                    workspace_id=workspace_id,
                    actor_user_id=created_by_user_id,
                    action="credits.beta_granted",
                    resource_type="credit_wallet",
                    resource_id=str(wallet.id),
                    metadata={"amount": beta_amount},
                )

        if commit:
            await self._session.commit()
        return wallet

    async def _lock_or_create_wallet(
        self,
        workspace_id: UUID,
        *,
        actor_user_id: UUID | None = None,
    ) -> CreditWallet:
        wallet = await self._session.scalar(
            select(CreditWallet)
            .where(CreditWallet.workspace_id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is not None:
            return wallet

        created = await self.initialize_workspace_wallet(
            workspace_id,
            created_by_user_id=actor_user_id,
            commit=False,
        )
        locked = await self._session.scalar(
            select(CreditWallet)
            .where(CreditWallet.id == created.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise AppError(
                code="INTERNAL_ERROR",
                message="Gagal mengunci dompet kredit workspace.",
                status_code=500,
            )
        return locked

    async def get_wallet_summary(
        self,
        user: User,
        *,
        workspace_id: UUID,
    ) -> CreditWalletResponse:
        """Return workspace wallet summary after verifying active workspace membership."""
        workspace, member = await require_active_workspace_membership(
            self._session,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="view_credits",
        )
        wallet = await self._session.scalar(
            select(CreditWallet).where(CreditWallet.workspace_id == workspace.id)
        )
        if wallet is None:
            wallet = await self.initialize_workspace_wallet(
                workspace.id,
                created_by_user_id=user.id,
                commit=True,
            )
        return to_wallet_response(wallet)

    async def list_transactions(
        self,
        user: User,
        *,
        workspace_id: UUID,
        page: int,
        page_size: int,
    ) -> CreditTransactionListResponse:
        """Return paginated append-only credit ledger entries for an authorized member."""
        workspace, member = await require_active_workspace_membership(
            self._session,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="view_credits",
        )
        wallet = await self._session.scalar(
            select(CreditWallet).where(CreditWallet.workspace_id == workspace.id)
        )
        if wallet is None:
            await self.initialize_workspace_wallet(
                workspace.id,
                created_by_user_id=user.id,
                commit=True,
            )

        total = (
            await self._session.scalar(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.workspace_id == workspace.id
                )
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(CreditTransaction)
                .where(CreditTransaction.workspace_id == workspace.id)
                .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return CreditTransactionListResponse(
            items=[to_transaction_response(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def list_active_packages(
        self,
        *,
        workspace_type: str | None = None,
    ) -> CreditPackageListResponse:
        """Return active backend credit packages without exposing payment flows."""
        query = select(CreditPackage).where(CreditPackage.is_active.is_(True))
        if workspace_type in ("personal", "organization"):
            query = query.where(CreditPackage.workspace_type.in_([workspace_type, "all"]))
        rows = (
            await self._session.scalars(
                query.order_by(CreditPackage.credits.asc(), CreditPackage.id.asc())
            )
        ).all()
        return CreditPackageListResponse(items=[_to_package_response(pkg) for pkg in rows])

    async def grant_credits(
        self,
        *,
        workspace_id: UUID,
        amount: int,
        idempotency_key: str,
        description: str = "Penambahan kredit workspace.",
        reference_type: str | None = None,
        reference_id: str | None = None,
        created_by_user_id: UUID | None = None,
    ) -> CreditTransaction:
        """Atomically credit a workspace wallet via an idempotent grant ledger entry."""
        normalized_key = idempotency_key.strip()
        if amount <= 0 or not normalized_key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Jumlah kredit dan idempotency key wajib bernilai valid.",
                status_code=400,
            )

        now = datetime.now(UTC)
        wallet = await self._lock_or_create_wallet(workspace_id, actor_user_id=created_by_user_id)
        existing = await self._session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.workspace_id == workspace_id,
                CreditTransaction.idempotency_key == normalized_key,
            )
        )
        if existing is not None:
            if existing.transaction_type != "grant" or existing.amount != amount:
                await self._session.rollback()
                raise AppError(
                    code="IDEMPOTENCY_CONFLICT",
                    message="Idempotency key sudah digunakan dengan parameter grant berbeda.",
                    status_code=409,
                )
            return existing

        wallet.available_balance += amount
        wallet.updated_at = now
        tx = CreditTransaction(
            wallet_id=wallet.id,
            workspace_id=workspace_id,
            transaction_type="grant",
            amount=amount,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=normalized_key,
            description=description[:255],
            created_by_user_id=created_by_user_id,
            created_at=now,
        )
        self._session.add(tx)
        await self._session.flush()
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=created_by_user_id,
            action="credits.granted",
            resource_type="credit_transaction",
            resource_id=str(tx.id),
            metadata={"amount": amount},
        )
        await self._session.commit()
        return tx

    async def reserve_credits(
        self,
        *,
        workspace_id: UUID,
        amount: int,
        idempotency_key: str,
        job_id: UUID | None = None,
        job_type: str = "ai_generation",
        is_rerender_of_approved: bool = False,
        created_by_user_id: UUID | None = None,
        ttl_seconds: int | None = None,
    ) -> CreditReservation:
        """Atomically reserve AI credits under a row lock with idempotency protection."""
        if job_type == "docx_render" or is_rerender_of_approved:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "Render DOCX atau render ulang versi yang telah disetujui "
                    "tidak memerlukan reservasi kredit AI."
                ),
                status_code=400,
            )

        normalized_key = idempotency_key.strip()
        if amount <= 0 or not normalized_key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Jumlah reservasi kredit dan idempotency key wajib valid.",
                status_code=400,
            )

        now = datetime.now(UTC)
        wallet = await self._lock_or_create_wallet(workspace_id, actor_user_id=created_by_user_id)
        existing = await self._session.scalar(
            select(CreditReservation)
            .where(
                CreditReservation.workspace_id == workspace_id,
                CreditReservation.idempotency_key == normalized_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            if existing.amount != amount or existing.job_id != job_id:
                await self._session.rollback()
                raise AppError(
                    code="IDEMPOTENCY_CONFLICT",
                    message="Idempotency key sudah digunakan dengan parameter reservasi berbeda.",
                    status_code=409,
                )
            return existing

        if wallet.available_balance < amount:
            available = wallet.available_balance
            await self._session.rollback()
            raise AppError(
                code="INSUFFICIENT_CREDITS",
                message="Kredit workspace tidak mencukupi.",
                status_code=402,
                details={"required": amount, "available": available},
            )

        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self._settings.credit_reservation_ttl_seconds
        )
        wallet.available_balance -= amount
        wallet.reserved_balance += amount
        wallet.updated_at = now
        reservation = CreditReservation(
            wallet_id=wallet.id,
            workspace_id=workspace_id,
            job_id=job_id,
            amount=amount,
            status="reserved",
            expires_at=now + timedelta(seconds=ttl),
            idempotency_key=normalized_key,
            created_at=now,
        )
        self._session.add(reservation)
        await self._session.flush()
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=created_by_user_id,
            action="credits.reserved",
            resource_type="credit_reservation",
            resource_id=str(reservation.id),
            metadata={"amount": amount},
        )
        await self._session.commit()
        return reservation

    async def commit_reservation(
        self,
        *,
        workspace_id: UUID,
        reservation_id: UUID,
        document_status: str,
        actor_user_id: UUID | None = None,
    ) -> CreditReservation:
        """Commit reserved credits only after a valid AI draft reaches review_required."""
        if document_status != "review_required":
            raise AppError(
                code="INVALID_JOB_STATE",
                message=(
                    "Kredit hanya dapat di-commit setelah draf AI tersimpan "
                    "sebagai review_required."
                ),
                status_code=409,
            )

        now = datetime.now(UTC)
        wallet = await self._lock_or_create_wallet(workspace_id, actor_user_id=actor_user_id)
        reservation = await self._session.scalar(
            select(CreditReservation)
            .where(
                CreditReservation.id == reservation_id,
                CreditReservation.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reservation is None:
            await self._session.rollback()
            raise AppError(
                code="NOT_FOUND",
                message="Reservasi kredit tidak ditemukan.",
                status_code=404,
            )
        if reservation.status == "committed":
            return reservation
        if reservation.status != "reserved":
            await self._session.rollback()
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Reservasi kredit tidak dalam status aktif.",
                status_code=409,
            )

        wallet.reserved_balance -= reservation.amount
        wallet.updated_at = now
        reservation.status = "committed"
        reservation.resolved_at = now
        self._session.add(
            CreditTransaction(
                wallet_id=wallet.id,
                workspace_id=workspace_id,
                transaction_type="commit",
                amount=-reservation.amount,
                reference_type="credit_reservation",
                reference_id=str(reservation.id),
                idempotency_key=f"commit:{reservation.id}",
                description="Komitmen kredit setelah draf AI tersimpan untuk peninjauan.",
                created_by_user_id=actor_user_id,
                created_at=now,
            )
        )
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action="credits.committed",
            resource_type="credit_reservation",
            resource_id=str(reservation.id),
            metadata={"amount": reservation.amount},
        )
        await self._session.commit()
        return reservation

    async def release_reservation(
        self,
        *,
        workspace_id: UUID,
        reservation_id: UUID,
        reason: str = "Pelepasan reservasi kredit.",
        actor_user_id: UUID | None = None,
    ) -> CreditReservation:
        """Idempotently release an active reservation back to available_balance."""
        now = datetime.now(UTC)
        wallet = await self._lock_or_create_wallet(workspace_id, actor_user_id=actor_user_id)
        reservation = await self._session.scalar(
            select(CreditReservation)
            .where(
                CreditReservation.id == reservation_id,
                CreditReservation.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reservation is None:
            await self._session.rollback()
            raise AppError(
                code="NOT_FOUND",
                message="Reservasi kredit tidak ditemukan.",
                status_code=404,
            )
        if reservation.status in ("released", "expired"):
            return reservation
        if reservation.status != "reserved":
            await self._session.rollback()
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Reservasi yang telah di-commit memerlukan refund kompensasi.",
                status_code=409,
            )

        wallet.reserved_balance -= reservation.amount
        wallet.available_balance += reservation.amount
        wallet.updated_at = now
        reservation.status = "released"
        reservation.resolved_at = now
        self._session.add(
            CreditTransaction(
                wallet_id=wallet.id,
                workspace_id=workspace_id,
                transaction_type="release",
                amount=0,
                reference_type="credit_reservation",
                reference_id=str(reservation.id),
                idempotency_key=f"release:{reservation.id}",
                description=reason[:255],
                created_by_user_id=actor_user_id,
                created_at=now,
            )
        )
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action="credits.released",
            resource_type="credit_reservation",
            resource_id=str(reservation.id),
            metadata={"amount": reservation.amount},
        )
        await self._session.commit()
        return reservation

    async def refund_reservation(
        self,
        *,
        workspace_id: UUID,
        reservation_id: UUID,
        idempotency_key: str,
        reason: str = "Pengembalian kredit (refund).",
        actor_user_id: UUID | None = None,
    ) -> CreditTransaction:
        """Issue an idempotent compensating ledger refund for a committed reservation."""
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Idempotency key wajib diisi untuk refund.",
                status_code=400,
            )

        now = datetime.now(UTC)
        wallet = await self._lock_or_create_wallet(workspace_id, actor_user_id=actor_user_id)
        reservation = await self._session.scalar(
            select(CreditReservation)
            .where(
                CreditReservation.id == reservation_id,
                CreditReservation.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reservation is None:
            await self._session.rollback()
            raise AppError(
                code="NOT_FOUND",
                message="Reservasi kredit tidak ditemukan.",
                status_code=404,
            )
        if reservation.status != "committed":
            await self._session.rollback()
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Hanya reservasi yang sudah di-commit yang dapat di-refund.",
                status_code=409,
            )

        existing = await self._session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.workspace_id == workspace_id,
                or_(
                    CreditTransaction.idempotency_key == normalized_key,
                    (
                        (CreditTransaction.transaction_type == "refund")
                        & (CreditTransaction.reference_type == "credit_reservation_refund")
                        & (CreditTransaction.reference_id == str(reservation.id))
                    ),
                ),
            )
        )
        if existing is not None:
            if (
                existing.idempotency_key == normalized_key
                and existing.transaction_type == "refund"
                and existing.reference_type == "credit_reservation_refund"
                and existing.reference_id == str(reservation.id)
                and existing.amount == reservation.amount
            ):
                return existing
            await self._session.rollback()
            raise AppError(
                code="CONFLICT",
                message="Reservasi ini sudah pernah di-refund atau key konflik.",
                status_code=409,
            )

        wallet.available_balance += reservation.amount
        wallet.updated_at = now
        tx = CreditTransaction(
            wallet_id=wallet.id,
            workspace_id=workspace_id,
            transaction_type="refund",
            amount=reservation.amount,
            reference_type="credit_reservation_refund",
            reference_id=str(reservation.id),
            idempotency_key=normalized_key,
            description=reason[:255],
            created_by_user_id=actor_user_id,
            created_at=now,
        )
        self._session.add(tx)
        await self._session.flush()
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action="credits.refunded",
            resource_type="credit_transaction",
            resource_id=str(tx.id),
            metadata={"amount": reservation.amount, "reservation_id": str(reservation.id)},
        )
        await self._session.commit()
        return tx

    async def reconcile_stale_reservations(
        self,
        *,
        workspace_id: UUID | None = None,
        now: datetime | None = None,
        job_liveness_checker: JobLivenessChecker | None = None,
    ) -> list[UUID]:
        """Expire stale reservations only after row locks and job-liveness verification."""
        current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
        query = select(CreditReservation.id, CreditReservation.workspace_id).where(
            CreditReservation.status == "reserved",
            CreditReservation.expires_at <= current_time,
        )
        if workspace_id is not None:
            query = query.where(CreditReservation.workspace_id == workspace_id)
        candidates = (
            await self._session.execute(
                query.order_by(
                    CreditReservation.workspace_id.asc(),
                    CreditReservation.created_at.asc(),
                    CreditReservation.id.asc(),
                )
            )
        ).all()

        reconciled_ids: list[UUID] = []
        for res_id, res_ws_id in candidates:
            wallet = await self._session.scalar(
                select(CreditWallet)
                .where(CreditWallet.workspace_id == res_ws_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            reservation = await self._session.scalar(
                select(CreditReservation)
                .where(CreditReservation.id == res_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                wallet is None
                or reservation is None
                or reservation.status != "reserved"
                or ensure_utc(reservation.expires_at) > current_time
            ):
                continue

            if reservation.job_id is not None and (
                job_liveness_checker is None
                or await job_liveness_checker.is_job_active(self._session, reservation.job_id)
            ):
                # Never release credits merely because a callback is late while job is active.
                continue

            wallet.reserved_balance -= reservation.amount
            wallet.available_balance += reservation.amount
            wallet.updated_at = current_time
            reservation.status = "expired"
            reservation.resolved_at = current_time
            self._session.add(
                CreditTransaction(
                    wallet_id=wallet.id,
                    workspace_id=res_ws_id,
                    transaction_type="release",
                    amount=0,
                    reference_type="credit_reservation",
                    reference_id=str(reservation.id),
                    idempotency_key=f"expire:{reservation.id}",
                    description="Pelepasan otomatis reservasi kredit yang kedaluwarsa.",
                    created_by_user_id=None,
                    created_at=current_time,
                )
            )
            record_workspace_audit(
                self._session,
                workspace_id=res_ws_id,
                actor_user_id=None,
                action="credits.reservation_expired",
                resource_type="credit_reservation",
                resource_id=str(reservation.id),
                metadata={"amount": reservation.amount},
            )
            reconciled_ids.append(reservation.id)

        if reconciled_ids:
            await self._session.commit()
        return reconciled_ids

    async def verify_wallet_reconciliation(self, *, workspace_id: UUID) -> bool:
        """Verify wallet balances reconcile exactly with active reservations and ledger."""
        wallet = await self._session.scalar(
            select(CreditWallet)
            .where(CreditWallet.workspace_id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is None:
            return False

        active_reserved_sum = (
            await self._session.scalar(
                select(func.coalesce(func.sum(CreditReservation.amount), 0)).where(
                    CreditReservation.workspace_id == workspace_id,
                    CreditReservation.status == "reserved",
                )
            )
            or 0
        )
        ledger_sum = (
            await self._session.scalar(
                select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                    CreditTransaction.workspace_id == workspace_id,
                )
            )
            or 0
        )
        return bool(
            wallet.reserved_balance == int(active_reserved_sum)
            and (wallet.available_balance + wallet.reserved_balance) == int(ledger_sum)
        )

