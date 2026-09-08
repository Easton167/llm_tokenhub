"""钱包服务：余额行锁、预扣/实扣/释放/充值，所有操作基于 request_id 幂等。

并发安全：
  - 所有余额变更使用 SELECT ... FOR UPDATE 行级锁，在同一数据库事务中完成
  - 两资金池（赠送 / 现金）的变更必须在同一把行锁内完成，本服务不自行 commit
  - 幂等键：(wallet_id, ref_request_id, type) 的部分唯一索引（见 wallet.py）
  - 负余额防护：CheckConstraint 兜底，应用层也主动检查；两池均不得变负
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InsufficientBalanceError, ResourceNotFoundError
from app.core.metrics import BILLING_OPS, CAPTURE_AMOUNT, HOLD_AMOUNT
from app.db.models.wallet import Wallet, WalletTransaction

# 流水类型常量
TX_HOLD = "hold"
TX_CAPTURE = "capture"
TX_RELEASE = "release"
TX_RECHARGE = "recharge"
TX_ADJUST = "adjust"
TX_GIFT_GRANT = "gift_grant"
TX_GIFT_REVOKE = "gift_revoke"
TX_GIFT_EXPIRE = "gift_expire"


class WalletService:
    """钱包核心操作服务。

    调用方负责事务边界（在 db 事务内调用各方法），本服务不自行 commit。
    FastAPI 路由层的 db 依赖默认在 request 结束时 commit。
    """

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _get_wallet_for_update(
        self, session: AsyncSession, wallet_id: UUID
    ) -> Wallet:
        """使用 SELECT ... FOR UPDATE 锁定钱包行，防止并发超扣。"""
        stmt = (
            select(Wallet)
            .where(Wallet.id == wallet_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        wallet = result.scalar_one_or_none()
        if wallet is None:
            raise ResourceNotFoundError("Wallet not found")
        return wallet

    async def _get_wallet_by_owner(
        self,
        session: AsyncSession,
        owner_type: str,
        owner_id: UUID,
    ) -> Wallet | None:
        """按主体类型与 ID 查钱包（不加锁，仅读）。"""
        stmt = select(Wallet).where(
            Wallet.owner_type == owner_type,
            Wallet.owner_id == owner_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _require_wallet_by_owner(
        self, session: AsyncSession, user_id: UUID
    ) -> Wallet:
        wallet = await self._get_wallet_by_owner(session, "personal", user_id)
        if wallet is None:
            raise ResourceNotFoundError("Personal wallet not found for user")
        return wallet

    def _append_transaction(
        self,
        session: AsyncSession,
        *,
        wallet: Wallet,
        tx_type: str,
        amount: int,
        ref_request_id: str | None = None,
        operator_user_id: UUID | None = None,
        remark: str | None = None,
        gift_amount: int = 0,
        gift_grant_id: UUID | None = None,
    ) -> WalletTransaction:
        """追加一条流水记录（只追加，不修改）。

        组织钱包流水自动写入 org_id；operator_user_id 表示操作人或组织消费发起成员。
        gift_amount / gift_grant_id 默认 0/None，既有现金调用方行为不变；
        gift_balance_after / gift_held_after 始终快照当前钱包赠送池。
        """
        org_id = wallet.owner_id if wallet.owner_type == "org" else None
        tx = WalletTransaction(
            wallet_id=wallet.id,
            type=tx_type,
            amount=amount,
            gift_amount=gift_amount,
            balance_after=wallet.balance,
            held_after=wallet.held,
            gift_balance_after=wallet.gift_balance,
            gift_held_after=wallet.gift_held,
            gift_grant_id=gift_grant_id,
            ref_request_id=ref_request_id,
            org_id=org_id,
            operator_user_id=operator_user_id,
            remark=remark,
        )
        session.add(tx)
        return tx

    @staticmethod
    def _gift_first_split(amount: int, gift_available: int) -> tuple[int, int]:
        """按 赠送→现金 优先级拆分 amount，返回 (gift_part, cash_part)。

        纯整数减法，不做比例分摊：赠送池能盖住就全走赠送，盖不住的余数走现金。
        这保证「赠金先花完」且流水可按整数对账。
        """
        if amount <= 0:
            return 0, 0
        gift_part = gift_available if gift_available < amount else amount
        if gift_part < 0:
            gift_part = 0
        return gift_part, amount - gift_part

    async def _read_hold_split(
        self,
        session: AsyncSession,
        wallet_id: UUID,
        request_id: str,
        hold_amount: int,
    ) -> tuple[int, int]:
        """从 hold 流水读取跨池拆分；(gift_hold, cash_hold)。

        查不到 hold（无预扣直接结算）则 gift_hold=0，退化为纯现金。
        """
        hold_tx = await self._find_transaction(session, wallet_id, request_id, TX_HOLD)
        if hold_tx is None:
            return 0, hold_amount
        gift_hold = -int(hold_tx.gift_amount)
        if gift_hold < 0:
            gift_hold = 0
        if gift_hold > hold_amount:
            gift_hold = hold_amount
        return gift_hold, hold_amount - gift_hold

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def get_or_create_personal_wallet(
        self, session: AsyncSession, user_id: UUID
    ) -> Wallet:
        """获取或初始化用户的个人钱包（余额=0）。"""
        wallet = await self._get_wallet_by_owner(session, "personal", user_id)
        if wallet is None:
            wallet = Wallet(
                owner_type="personal",
                owner_id=user_id,
                balance=0,
                held=0,
                gift_balance=0,
                gift_held=0,
                currency="CNY",
                credit_limit=0,
            )
            session.add(wallet)
            await session.flush()
        return wallet

    async def get_or_create_org_wallet(
        self, session: AsyncSession, org_id: UUID
    ) -> Wallet:
        """获取或初始化组织钱包（余额=0）。"""
        wallet = await self._get_wallet_by_owner(session, "org", org_id)
        if wallet is None:
            wallet = Wallet(
                owner_type="org",
                owner_id=org_id,
                balance=0,
                held=0,
                gift_balance=0,
                gift_held=0,
                currency="CNY",
                credit_limit=0,
            )
            session.add(wallet)
            await session.flush()
        return wallet

    async def require_org_wallet(
        self, session: AsyncSession, org_id: UUID
    ) -> Wallet:
        wallet = await self._get_wallet_by_owner(session, "org", org_id)
        if wallet is None:
            raise ResourceNotFoundError("Organization wallet not found")
        return wallet

    async def recharge(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        amount: int,
        operator_user_id: UUID | None = None,
        remark: str | None = None,
        ref_request_id: str | None = None,
    ) -> Wallet:
        """充值：余额增加 amount（amount 必须 > 0）。

        传入 ref_request_id 时按 (wallet_id, ref_request_id, type=recharge) 幂等：
        支付回调入账应使用支付单 order_no 作为 ref_request_id。
        """
        if amount <= 0:
            from app.core.errors import InvalidRequestError
            raise InvalidRequestError("Recharge amount must be positive")

        if ref_request_id:
            existing = await self._find_transaction(
                session, wallet_id, ref_request_id, TX_RECHARGE
            )
            if existing is not None:
                wallet = await session.get(Wallet, wallet_id)
                return wallet  # type: ignore[return-value]

        wallet = await self._get_wallet_for_update(session, wallet_id)
        wallet.balance += amount
        try:
            self._append_transaction(
                session,
                wallet=wallet,
                tx_type=TX_RECHARGE,
                amount=amount,
                ref_request_id=ref_request_id,
                operator_user_id=operator_user_id,
                remark=remark or f"Recharge {amount}",
            )
            await session.flush()
        except IntegrityError:
            # 并发下唯一索引冲突：已有同 ref 的 recharge，视为幂等成功
            if ref_request_id is None:
                raise
            await session.rollback()
            wallet = await session.get(Wallet, wallet_id)
            return wallet  # type: ignore[return-value]

        await session.refresh(wallet)
        return wallet

    async def adjust(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        amount: int,
        operator_user_id: UUID | None = None,
        remark: str | None = None,
    ) -> Wallet:
        """后台调账：amount 正数为增加，负数为减少（不能使余额变为负数）。"""
        from app.core.errors import InvalidRequestError

        if amount == 0:
            raise InvalidRequestError("Adjust amount cannot be zero")

        wallet = await self._get_wallet_for_update(session, wallet_id)
        if amount < 0 and wallet.balance + amount < 0:
            raise InvalidRequestError(
                f"Adjust would result in negative balance. Current balance: {wallet.balance}"
            )

        wallet.balance += amount
        self._append_transaction(
            session,
            wallet=wallet,
            tx_type=TX_ADJUST,
            amount=amount,
            operator_user_id=operator_user_id,
            remark=remark or f"Admin adjust {amount}",
        )
        await session.flush()
        await session.refresh(wallet)
        return wallet

    async def hold(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        amount: int,
        request_id: str,
        operator_user_id: UUID | None = None,
    ) -> Wallet:
        """预扣（冻结）余额。

        - 可用额 = gift_balance + balance + credit_limit
        - 按 赠送→现金 顺序拆成 gift_hold + cash_hold，分别冻结两池
        - hold 阶段不核销 gift_grants.remaining（capture 时再 FIFO 核销）
        - 以 request_id 幂等：重复调用直接返回（不报错）
        """
        if amount <= 0:
            from app.core.errors import InvalidRequestError
            raise InvalidRequestError("Hold amount must be positive")

        # 检查同一 request 的 hold 是否已存在（幂等）
        existing = await self._find_transaction(session, wallet_id, request_id, TX_HOLD)
        if existing is not None:
            wallet = await session.get(Wallet, wallet_id)
            return wallet  # type: ignore[return-value]

        wallet = await self._get_wallet_for_update(session, wallet_id)

        available = wallet.gift_balance + wallet.balance + wallet.credit_limit
        if available < amount:
            raise InsufficientBalanceError(
                f"Insufficient balance: available={available}, required={amount}, "
                f"gift_balance={wallet.gift_balance}, balance={wallet.balance}, "
                f"credit_limit={wallet.credit_limit}"
            )

        gift_hold, cash_hold = self._gift_first_split(amount, wallet.gift_balance)
        wallet.gift_balance -= gift_hold
        wallet.gift_held += gift_hold
        wallet.balance -= cash_hold
        wallet.held += cash_hold

        try:
            self._append_transaction(
                session,
                wallet=wallet,
                tx_type=TX_HOLD,
                amount=-amount,
                gift_amount=-gift_hold,
                ref_request_id=request_id,
                operator_user_id=operator_user_id,
                remark=f"Hold for request {request_id}",
            )
            await session.flush()
        except IntegrityError:
            # 并发情况下唯一索引冲突：幂等，说明已有同 request_id 的 hold
            await session.rollback()
            wallet = await session.get(Wallet, wallet_id)
            return wallet  # type: ignore[return-value]

        await session.refresh(wallet)
        BILLING_OPS.labels(op="hold", result="ok").inc()
        HOLD_AMOUNT.labels(model="unknown").inc(amount)
        return wallet

    async def capture(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        hold_amount: int,
        actual_amount: int,
        request_id: str,
        operator_user_id: UUID | None = None,
    ) -> Wallet:
        """实扣（结算）：按真实用量扣费，释放多余冻结额度。

        - hold_amount：预扣时冻结的金额
        - actual_amount：实际应收金额
        - 以 request_id 幂等：已结算的请求直接返回，不重复扣费
        - 差额按占用逆序退（先现金后赠送），等价于用 actual 按 赠送→现金 重新分摊
        """
        if actual_amount < 0:
            from app.core.errors import InvalidRequestError
            raise InvalidRequestError("Capture amount cannot be negative")

        # 幂等检查：若已存在 capture 流水，直接返回
        existing = await self._find_transaction(session, wallet_id, request_id, TX_CAPTURE)
        if existing is not None:
            wallet = await session.get(Wallet, wallet_id)
            return wallet  # type: ignore[return-value]

        wallet = await self._get_wallet_for_update(session, wallet_id)
        gift_hold, cash_hold = await self._read_hold_split(
            session, wallet_id, request_id, hold_amount
        )

        # 先把两池的「预扣中」计数归零。钱还停在池外，下面再决定退回可用还是实扣。
        wallet.gift_held -= gift_hold
        wallet.held -= cash_hold

        gift_grant_id = None
        if actual_amount <= hold_amount:
            # 常见路径：预扣 ≥ 实收。差额按占用逆序退（先退现金、再退赠送），
            # 等价于用 actual 重新按 赠送→现金 分摊，避免「多冻的赠金退成现金」。
            release_amount = hold_amount - actual_amount
            cash_release = cash_hold if cash_hold < release_amount else release_amount
            gift_release = release_amount - cash_release
            gift_charge = gift_hold - gift_release
            cash_charge = cash_hold - cash_release
            wallet.gift_balance += gift_release
            wallet.balance += cash_release
            charged = actual_amount
            remark = f"Capture for request {request_id}"
        else:
            # 预扣不足（估算偏低或上游 usage 远超 max_tokens）：
            # 先吃完已冻的 gift_hold+cash_hold，差额再按赠送→现金从可用池补扣。
            # 授信只扩大「能不能扣」的判定，禁止把 cash balance 写成负数。
            extra = actual_amount - hold_amount
            gift_extra, extra_after_gift = self._gift_first_split(extra, wallet.gift_balance)
            cash_room = wallet.balance + wallet.credit_limit
            if cash_room < 0:
                cash_room = 0
            cash_extra = extra_after_gift if extra_after_gift < cash_room else cash_room
            # 两池都不能变负：授信只扩大可用判定，不把 cash 写成负数
            if cash_extra > wallet.balance:
                cash_extra = wallet.balance
            wallet.gift_balance -= gift_extra
            wallet.balance -= cash_extra
            gift_charge = gift_hold + gift_extra
            cash_charge = cash_hold + cash_extra
            charged = gift_charge + cash_charge
            gift_release = 0
            remark = f"Capture (over-estimate) for request {request_id}"

        assert gift_charge >= 0 and cash_charge >= 0
        assert gift_charge + cash_charge == charged
        assert wallet.gift_balance >= 0 and wallet.gift_held >= 0
        assert wallet.balance >= 0 and wallet.held >= 0

        if gift_charge > 0:
            # hold 阶段不核销批次；真正消费赠金 FIFO 发生在 capture
            from app.services.gift_service import gift_service

            allocations = await gift_service.consume(
                session, wallet=wallet, amount=gift_charge
            )
            if allocations:
                gift_grant_id = allocations[0][0]

        self._append_transaction(
            session,
            wallet=wallet,
            tx_type=TX_CAPTURE,
            amount=-charged,
            gift_amount=-gift_charge,
            gift_grant_id=gift_grant_id,
            ref_request_id=request_id,
            operator_user_id=operator_user_id,
            remark=remark,
        )
        if actual_amount <= hold_amount:
            release_amount = hold_amount - actual_amount
            if release_amount > 0:
                # 差额单独记一条 release，便于流水审计「冻了多少、退了多少」
                self._append_transaction(
                    session,
                    wallet=wallet,
                    tx_type=TX_RELEASE,
                    amount=release_amount,
                    gift_amount=gift_release,
                    ref_request_id=request_id,
                    operator_user_id=operator_user_id,
                    remark=f"Release excess hold for request {request_id}",
                )

        await session.flush()
        await session.refresh(wallet)
        BILLING_OPS.labels(op="capture", result="ok").inc()
        CAPTURE_AMOUNT.labels(model="unknown").inc(actual_amount)
        return wallet

    async def release(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        hold_amount: int,
        request_id: str,
        operator_user_id: UUID | None = None,
    ) -> Wallet:
        """全额释放预扣冻结（失败请求不计费）。

        - gift_hold / cash_hold 原路退回对应池，不核销任何批次
        - 以 request_id 幂等
        """
        if hold_amount <= 0:
            from app.core.errors import InvalidRequestError
            raise InvalidRequestError("Release amount must be positive")

        # 幂等检查
        existing = await self._find_transaction(session, wallet_id, request_id, TX_RELEASE)
        if existing is not None:
            wallet = await session.get(Wallet, wallet_id)
            return wallet  # type: ignore[return-value]

        wallet = await self._get_wallet_for_update(session, wallet_id)
        gift_hold, cash_hold = await self._read_hold_split(
            session, wallet_id, request_id, hold_amount
        )

        wallet.gift_held -= gift_hold
        wallet.gift_balance += gift_hold
        wallet.held -= cash_hold
        wallet.balance += cash_hold

        self._append_transaction(
            session,
            wallet=wallet,
            tx_type=TX_RELEASE,
            amount=hold_amount,
            gift_amount=gift_hold,
            ref_request_id=request_id,
            operator_user_id=operator_user_id,
            remark=f"Release hold for failed request {request_id}",
        )
        await session.flush()
        await session.refresh(wallet)
        BILLING_OPS.labels(op="release", result="ok").inc()
        return wallet

    async def get_charge_split(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        request_id: str,
    ) -> tuple[int, int]:
        """读 capture 流水返回 (gift_charge, cash_charge)，供用量拆分使用。

        无 capture 流水时返回 (0, 0)。
        """
        capture_tx = await self._find_transaction(
            session, wallet_id, request_id, TX_CAPTURE
        )
        if capture_tx is None:
            return 0, 0
        gift_charge = -int(capture_tx.gift_amount)
        total_charge = -int(capture_tx.amount)
        if gift_charge < 0:
            gift_charge = 0
        if gift_charge > total_charge:
            gift_charge = total_charge
        return gift_charge, total_charge - gift_charge

    async def get_transactions(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        tx_type: str | None = None,
        ref_request_id: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WalletTransaction]:
        """按钱包 ID 查询流水，支持按类型、request_id、时间过滤与分页。"""
        stmt = (
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == wallet_id)
            .order_by(WalletTransaction.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if tx_type:
            stmt = stmt.where(WalletTransaction.type == tx_type)
        if ref_request_id:
            stmt = stmt.where(WalletTransaction.ref_request_id == ref_request_id)
        if from_time is not None:
            stmt = stmt.where(WalletTransaction.created_at >= from_time)
        if to_time is not None:
            stmt = stmt.where(WalletTransaction.created_at <= to_time)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 内部工具（幂等检查）
    # ------------------------------------------------------------------

    async def _find_transaction(
        self,
        session: AsyncSession,
        wallet_id: UUID,
        request_id: str,
        tx_type: str,
    ) -> WalletTransaction | None:
        """查找是否已存在同 (wallet, request_id, type) 的流水记录。"""
        stmt = select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet_id,
            WalletTransaction.ref_request_id == request_id,
            WalletTransaction.type == tx_type,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


wallet_service = WalletService()
