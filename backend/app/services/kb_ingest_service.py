"""知识库异步 ingest：状态机、parse/embed job、向量化计费串联与幂等。

向量化复用 internal embedding 网关（个人钱包 hold/capture/release，source=kb_ingest）。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.constants import (
    KB_CHUNK_EMBED_FAILED,
    KB_CHUNK_EMBED_PENDING,
    KB_CHUNK_EMBED_SUCCESS,
    KB_DOC_STATUS_CHUNKING,
    KB_DOC_STATUS_EMBEDDING,
    KB_DOC_STATUS_FAILED,
    KB_DOC_STATUS_INSUFFICIENT_BALANCE,
    KB_DOC_STATUS_PARSING,
    KB_DOC_STATUS_PARTIAL,
    KB_DOC_STATUS_PENDING_EMBEDDING,
    KB_DOC_STATUS_READY,
    KB_DOC_STATUS_UPLOADED,
    KB_JOB_STATUS_FAILED,
    KB_JOB_STATUS_PENDING,
    KB_JOB_STATUS_RUNNING,
    KB_JOB_STATUS_SUCCESS,
    KB_JOB_TYPE_EMBED,
    KB_JOB_TYPE_PARSE,
    KB_JOB_TYPE_RECHUNK,
    USAGE_SOURCE_KB_INGEST,
)
from app.core.errors import (
    ForbiddenError,
    InsufficientBalanceError,
    InvalidRequestError,
    ResourceNotFoundError,
)
from app.core.logging import get_logger
from app.db.models.kb import KbChunk, KbDocument, KbIngestJob, KnowledgeBase
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.kb.chunking import chunk
from app.kb.errors import KbEngineError
from app.kb.parse import parse
from app.services.billing_service import billing_service
from app.services.embedding_gateway import (
    InternalCaller,
    estimate_embedding_input_tokens,
    internal_embedding_service,
)
from app.services.wallet_service import wallet_service
from app.storage import FileStorage, get_file_storage

logger = get_logger(__name__)

_ALLOWED_DOC_TRANSITIONS: dict[str, frozenset[str]] = {
    KB_DOC_STATUS_UPLOADED: frozenset({KB_DOC_STATUS_PARSING}),
    KB_DOC_STATUS_PARSING: frozenset(
        {KB_DOC_STATUS_CHUNKING, KB_DOC_STATUS_FAILED}
    ),
    KB_DOC_STATUS_CHUNKING: frozenset(
        {KB_DOC_STATUS_PENDING_EMBEDDING, KB_DOC_STATUS_FAILED}
    ),
    KB_DOC_STATUS_PENDING_EMBEDDING: frozenset(
        {
            KB_DOC_STATUS_EMBEDDING,
            KB_DOC_STATUS_INSUFFICIENT_BALANCE,
        }
    ),
    KB_DOC_STATUS_INSUFFICIENT_BALANCE: frozenset(
        {KB_DOC_STATUS_PENDING_EMBEDDING, KB_DOC_STATUS_EMBEDDING}
    ),
    KB_DOC_STATUS_EMBEDDING: frozenset(
        {
            KB_DOC_STATUS_READY,
            KB_DOC_STATUS_PARTIAL,
            KB_DOC_STATUS_FAILED,
        }
    ),
    KB_DOC_STATUS_PARTIAL: frozenset(
        {KB_DOC_STATUS_EMBEDDING, KB_DOC_STATUS_PARSING}
    ),
    KB_DOC_STATUS_FAILED: frozenset(
        {KB_DOC_STATUS_PARSING, KB_DOC_STATUS_PENDING_EMBEDDING}
    ),
    # ready 仅允许 rechunk 回到 parsing
    KB_DOC_STATUS_READY: frozenset({KB_DOC_STATUS_PARSING}),
}

_IN_PROGRESS_DOC_STATUSES = frozenset(
    {
        KB_DOC_STATUS_PARSING,
        KB_DOC_STATUS_CHUNKING,
        KB_DOC_STATUS_EMBEDDING,
    }
)

_RETRYABLE_DOC_STATUSES = frozenset(
    {
        KB_DOC_STATUS_FAILED,
        KB_DOC_STATUS_INSUFFICIENT_BALANCE,
        KB_DOC_STATUS_PARTIAL,
    }
)


class KnowledgeBaseIngestService:
    def __init__(self, storage: FileStorage | None = None) -> None:
        self.storage = storage or get_file_storage()

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def transition_document(
        self,
        doc: KbDocument,
        new_status: str,
        *,
        error_message: str | None = None,
        clear_error: bool = False,
    ) -> None:
        """按 PRD 状态机流转；非法流转抛 InvalidRequestError。"""
        allowed = _ALLOWED_DOC_TRANSITIONS.get(doc.status, frozenset())
        if new_status not in allowed:
            raise InvalidRequestError(
                f"Illegal document status transition: {doc.status} -> {new_status}"
            )
        doc.status = new_status
        if clear_error:
            doc.error_message = None
        if error_message is not None:
            doc.error_message = error_message

    # ------------------------------------------------------------------
    # Job 创建与幂等键
    # ------------------------------------------------------------------

    @staticmethod
    def build_idempotency_key(doc_id: UUID, job_type: str, generation: int) -> str:
        return f"{doc_id}:{job_type}:{generation}"

    @staticmethod
    def build_embed_hold_request_id(idempotency_key: str) -> str:
        """文档级向量化预扣幂等键（wallet_transactions.ref_request_id）。"""
        return f"kb-embed:{idempotency_key}"

    async def _next_job_generation(
        self, db: AsyncSession, *, document_id: UUID, job_type: str
    ) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(KbIngestJob)
            .where(
                KbIngestJob.document_id == document_id,
                KbIngestJob.job_type == job_type,
            )
        )
        return int(result.scalar_one())

    async def create_parse_job(
        self, db: AsyncSession, *, doc: KbDocument
    ) -> KbIngestJob:
        """为文档创建 parse job（上传后或失败重试）。"""
        generation = await self._next_job_generation(
            db, document_id=doc.id, job_type=KB_JOB_TYPE_PARSE
        )
        job = KbIngestJob(
            job_type=KB_JOB_TYPE_PARSE,
            status=KB_JOB_STATUS_PENDING,
            progress=0,
            document_id=doc.id,
            kb_id=doc.kb_id,
            idempotency_key=self.build_idempotency_key(
                doc.id, KB_JOB_TYPE_PARSE, generation
            ),
            retry_count=0,
        )
        db.add(job)
        await db.flush()
        return job

    async def create_embed_job(
        self, db: AsyncSession, *, doc: KbDocument
    ) -> KbIngestJob:
        """创建 embed job；幂等键按文档 embed 代数递增。"""
        generation = await self._next_job_generation(
            db, document_id=doc.id, job_type=KB_JOB_TYPE_EMBED
        )
        idem = self.build_idempotency_key(doc.id, KB_JOB_TYPE_EMBED, generation)
        job = KbIngestJob(
            job_type=KB_JOB_TYPE_EMBED,
            status=KB_JOB_STATUS_PENDING,
            progress=0,
            document_id=doc.id,
            kb_id=doc.kb_id,
            hold_ref_request_id=self.build_embed_hold_request_id(idem),
            idempotency_key=idem,
            retry_count=0,
        )
        db.add(job)
        await db.flush()
        return job

    async def create_rechunk_job(
        self, db: AsyncSession, *, doc: KbDocument
    ) -> KbIngestJob:
        """创建 rechunk job（覆盖既有分块含人工编辑，随后自动排队 embed）。"""
        generation = await self._next_job_generation(
            db, document_id=doc.id, job_type=KB_JOB_TYPE_RECHUNK
        )
        job = KbIngestJob(
            job_type=KB_JOB_TYPE_RECHUNK,
            status=KB_JOB_STATUS_PENDING,
            progress=0,
            document_id=doc.id,
            kb_id=doc.kb_id,
            idempotency_key=self.build_idempotency_key(
                doc.id, KB_JOB_TYPE_RECHUNK, generation
            ),
            retry_count=0,
        )
        db.add(job)
        await db.flush()
        return job

    async def retry_document(
        self, db: AsyncSession, *, user_id: UUID, doc_id: UUID
    ) -> tuple[KbDocument, KbIngestJob]:
        """failed / insufficient_balance / partial 可重试。"""
        stmt = (
            select(KbDocument)
            .where(KbDocument.id == doc_id)
            .options(selectinload(KbDocument.knowledge_base))
        )
        doc = (await db.execute(stmt)).scalar_one_or_none()
        if doc is None:
            raise ResourceNotFoundError("Document not found")
        kb = doc.knowledge_base
        if kb.owner_user_id != user_id:
            raise ForbiddenError("You do not have access to this document")

        if doc.status not in _RETRYABLE_DOC_STATUSES:
            raise InvalidRequestError(
                "Only failed / insufficient_balance / partial documents "
                f"can be retried (current status={doc.status})"
            )

        chunk_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(KbChunk)
                    .where(KbChunk.document_id == doc.id)
                )
            ).scalar_one()
        )

        # 解析/分块阶段失败：无分块 → 重建 parse；否则（含 embed 全失败）走 embed
        if doc.status == KB_DOC_STATUS_FAILED and chunk_count == 0:
            self.transition_document(doc, KB_DOC_STATUS_PARSING, clear_error=True)
            job = await self.create_parse_job(db, doc=doc)
        elif doc.status == KB_DOC_STATUS_INSUFFICIENT_BALANCE:
            self.transition_document(
                doc, KB_DOC_STATUS_PENDING_EMBEDDING, clear_error=True
            )
            job = await self.create_embed_job(db, doc=doc)
        elif doc.status == KB_DOC_STATUS_PARTIAL:
            # 进入 embedding 由 run_embed_job 负责；此处只排队
            doc.error_message = None
            job = await self.create_embed_job(db, doc=doc)
        else:
            # failed 且已有分块：回到 pending_embedding 再向量化
            self.transition_document(
                doc, KB_DOC_STATUS_PENDING_EMBEDDING, clear_error=True
            )
            job = await self.create_embed_job(db, doc=doc)

        await db.flush()
        await db.refresh(doc)
        return doc, job

    async def get_latest_job_progress(
        self, db: AsyncSession, *, document_id: UUID
    ) -> int:
        """取当前文档所有「已开始」job 的最高进度。

        同一事务内多个 job 的时间戳可能完全相同（测试环境尤甚），因此不依赖
        created_at/updated_at 排序，而是直接取 MAX(progress)，保证成功 job
        的 100 始终优先于失败 job 的中间值。
        """
        stmt = (
            select(func.max(KbIngestJob.progress))
            .where(
                KbIngestJob.document_id == document_id,
                KbIngestJob.status != KB_JOB_STATUS_PENDING,
            )
        )
        result = (await db.execute(stmt)).scalar_one_or_none()
        if result is not None:
            return int(result)
        # 全部 job 仍在排队
        return 0

    # ------------------------------------------------------------------
    # 并发控制
    # ------------------------------------------------------------------

    async def count_user_in_progress(
        self, db: AsyncSession, *, user_id: UUID
    ) -> int:
        """统计用户当前处理中文档数（parsing/chunking/embedding）。"""
        result = await db.execute(
            select(func.count())
            .select_from(KbDocument)
            .join(KnowledgeBase, KnowledgeBase.id == KbDocument.kb_id)
            .where(
                KnowledgeBase.owner_user_id == user_id,
                KbDocument.status.in_(tuple(_IN_PROGRESS_DOC_STATUSES)),
            )
        )
        return int(result.scalar_one())

    async def count_user_running_jobs(
        self, db: AsyncSession, *, user_id: UUID
    ) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(KbIngestJob)
            .join(KnowledgeBase, KnowledgeBase.id == KbIngestJob.kb_id)
            .where(
                KnowledgeBase.owner_user_id == user_id,
                KbIngestJob.status == KB_JOB_STATUS_RUNNING,
            )
        )
        return int(result.scalar_one())

    @staticmethod
    def _advisory_lock_key(user_id: UUID) -> int:
        return int.from_bytes(user_id.bytes[:4], "big", signed=False) % (2**31)

    async def claim_next_job(
        self,
        db: AsyncSession,
        *,
        batch_probe: int | None = None,
        max_concurrent: int | None = None,
    ) -> KbIngestJob | None:
        """用 FOR UPDATE SKIP LOCKED 领取一个可执行的 pending job。"""
        probe = batch_probe if batch_probe is not None else settings.kb_worker_batch_size
        limit = (
            max_concurrent
            if max_concurrent is not None
            else settings.kb_max_concurrent_docs_per_user
        )

        # 向量化为低优先级：同批 pending 中 parse/rechunk 先行，embed 靠后
        priority = case(
            (KbIngestJob.job_type == KB_JOB_TYPE_EMBED, 1),
            else_=0,
        )
        stmt = (
            select(KbIngestJob)
            .where(KbIngestJob.status == KB_JOB_STATUS_PENDING)
            .order_by(priority.asc(), KbIngestJob.created_at.asc())
            .limit(max(1, probe))
            .with_for_update(skip_locked=True)
        )
        jobs = list((await db.execute(stmt)).scalars().all())
        for job in jobs:
            kb = await db.get(KnowledgeBase, job.kb_id)
            if kb is None:
                job.status = KB_JOB_STATUS_FAILED
                job.error_message = "Knowledge base missing"
                continue

            await db.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": self._advisory_lock_key(kb.owner_user_id)},
            )

            in_progress = await self.count_user_in_progress(
                db, user_id=kb.owner_user_id
            )
            running_jobs = await self.count_user_running_jobs(
                db, user_id=kb.owner_user_id
            )
            doc = await db.get(KbDocument, job.document_id)
            already_counted = (
                doc is not None and doc.status in _IN_PROGRESS_DOC_STATUSES
            )
            slots_used = max(in_progress, running_jobs)
            if not already_counted and slots_used >= limit:
                continue

            job.status = KB_JOB_STATUS_RUNNING
            job.progress = 0
            job.error_message = None
            await db.flush()
            return job
        return None

    # ------------------------------------------------------------------
    # Parse 流水线
    # ------------------------------------------------------------------

    async def _clear_document_chunks(
        self, db: AsyncSession, *, doc: KbDocument, kb: KnowledgeBase
    ) -> None:
        """幂等重做：先清本阶段产物（分块）。"""
        old_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(KbChunk)
                    .where(KbChunk.document_id == doc.id)
                )
            ).scalar_one()
        )
        if old_count:
            await db.execute(delete(KbChunk).where(KbChunk.document_id == doc.id))
            kb.chunk_count = max(0, kb.chunk_count - old_count)
            await db.flush()

    async def _execute_parse_and_chunk(
        self,
        db: AsyncSession,
        *,
        job: KbIngestJob,
        doc: KbDocument,
        kb: KnowledgeBase,
    ) -> int:
        """解析文件 → 分块落库 → pending_embedding → 创建 embed job。

        调用前文档须已处于 parsing（或由本方法从 uploaded/failed 转入）。
        返回新分块数量。
        """
        if doc.status in (KB_DOC_STATUS_UPLOADED, KB_DOC_STATUS_FAILED):
            self.transition_document(doc, KB_DOC_STATUS_PARSING, clear_error=True)
        elif doc.status != KB_DOC_STATUS_PARSING:
            raise InvalidRequestError(
                f"Document not ready for parse/rechunk job (status={doc.status})"
            )

        job.progress = 10
        await db.flush()

        with self.storage.open(doc.storage_path) as fh:
            file_bytes = fh.read()

        parsed = parse(file_bytes, doc.file_type)
        job.progress = 40
        await db.flush()

        self.transition_document(doc, KB_DOC_STATUS_CHUNKING)
        job.progress = 50
        await db.flush()

        separators = list(doc.separators) if doc.separators else None
        chunks = chunk(
            parsed,
            chunk_size=doc.chunk_size,
            chunk_overlap=doc.chunk_overlap,
            separators=separators,
        )

        await self._clear_document_chunks(db, doc=doc, kb=kb)

        total_tokens = 0
        for c in chunks:
            total_tokens += c.token_count
            db.add(
                KbChunk(
                    document_id=doc.id,
                    kb_id=doc.kb_id,
                    seq=c.index,
                    content=c.content,
                    token_count=c.token_count,
                    source_location=c.source_location.to_dict() or None,
                    is_enabled=True,
                    is_manually_edited=False,
                    embedding=None,
                    embedding_status=KB_CHUNK_EMBED_PENDING,
                )
            )

        doc.total_tokens = total_tokens
        kb.chunk_count += len(chunks)
        job.progress = 90
        await db.flush()

        self.transition_document(doc, KB_DOC_STATUS_PENDING_EMBEDDING)
        job.status = KB_JOB_STATUS_SUCCESS
        job.progress = 100
        job.error_message = None
        await db.flush()

        await self.create_embed_job(db, doc=doc)
        return len(chunks)

    async def run_parse_job(self, db: AsyncSession, *, job: KbIngestJob) -> None:
        """执行 parse job：解析 → 分块落库 → pending_embedding → 创建 embed job。"""
        if job.job_type != KB_JOB_TYPE_PARSE:
            raise InvalidRequestError(
                f"run_parse_job expects parse job, got {job.job_type}"
            )

        stmt = (
            select(KbDocument)
            .where(KbDocument.id == job.document_id)
            .options(selectinload(KbDocument.knowledge_base))
        )
        doc = (await db.execute(stmt)).scalar_one_or_none()
        if doc is None:
            job.status = KB_JOB_STATUS_FAILED
            job.error_message = "Document not found"
            await db.flush()
            return

        kb = doc.knowledge_base
        try:
            n = await self._execute_parse_and_chunk(db, job=job, doc=doc, kb=kb)
            logger.info(
                "kb_ingest_success",
                job_id=str(job.id),
                document_id=str(doc.id),
                chunk_count=n,
                total_tokens=doc.total_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_parse_job(db, job=job, doc=doc, exc=exc)
            raise

    async def run_rechunk_job(self, db: AsyncSession, *, job: KbIngestJob) -> None:
        """执行 rechunk：清除旧分块（含人工编辑）→ 按文档快照参数重新分块并排队向量化。"""
        if job.job_type != KB_JOB_TYPE_RECHUNK:
            raise InvalidRequestError(
                f"run_rechunk_job expects rechunk job, got {job.job_type}"
            )

        stmt = (
            select(KbDocument)
            .where(KbDocument.id == job.document_id)
            .options(selectinload(KbDocument.knowledge_base))
        )
        doc = (await db.execute(stmt)).scalar_one_or_none()
        if doc is None:
            job.status = KB_JOB_STATUS_FAILED
            job.error_message = "Document not found"
            await db.flush()
            return

        kb = doc.knowledge_base
        try:
            n = await self._execute_parse_and_chunk(db, job=job, doc=doc, kb=kb)
            logger.info(
                "kb_rechunk_success",
                job_id=str(job.id),
                document_id=str(doc.id),
                chunk_count=n,
                total_tokens=doc.total_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_parse_job(db, job=job, doc=doc, exc=exc)
            raise

    async def _fail_parse_job(
        self,
        db: AsyncSession,
        *,
        job: KbIngestJob,
        doc: KbDocument,
        exc: BaseException,
    ) -> None:
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        if isinstance(exc, KbEngineError):
            message = exc.message

        try:
            if doc.status == KB_DOC_STATUS_UPLOADED:
                self.transition_document(doc, KB_DOC_STATUS_PARSING)
            if doc.status in (KB_DOC_STATUS_PARSING, KB_DOC_STATUS_CHUNKING):
                self.transition_document(
                    doc, KB_DOC_STATUS_FAILED, error_message=message
                )
            else:
                doc.status = KB_DOC_STATUS_FAILED
                doc.error_message = message
        except InvalidRequestError:
            doc.status = KB_DOC_STATUS_FAILED
            doc.error_message = message

        job.status = KB_JOB_STATUS_FAILED
        job.error_message = message
        job.retry_count = int(job.retry_count or 0) + 1
        await db.flush()

        logger.warning(
            "kb_ingest_failed",
            job_id=str(job.id),
            document_id=str(doc.id),
            error=message,
            retry_count=job.retry_count,
        )

    # ------------------------------------------------------------------
    # Embed 流水线（计费串联）
    # ------------------------------------------------------------------

    async def _load_chunks_needing_embed(
        self, db: AsyncSession, *, document_id: UUID
    ) -> list[KbChunk]:
        stmt = (
            select(KbChunk)
            .where(
                KbChunk.document_id == document_id,
                KbChunk.embedding_status.in_(
                    (KB_CHUNK_EMBED_PENDING, KB_CHUNK_EMBED_FAILED)
                ),
            )
            .order_by(KbChunk.seq.asc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def run_embed_job(self, db: AsyncSession, *, job: KbIngestJob) -> None:
        """执行 embed job：文档级 hold → 批量向量化 → capture/release → 状态机。"""
        if job.job_type != KB_JOB_TYPE_EMBED:
            raise InvalidRequestError(
                f"run_embed_job expects embed job, got {job.job_type}"
            )

        stmt = (
            select(KbDocument)
            .where(KbDocument.id == job.document_id)
            .options(
                selectinload(KbDocument.knowledge_base).selectinload(
                    KnowledgeBase.embedding_model
                )
            )
        )
        doc = (await db.execute(stmt)).scalar_one_or_none()
        if doc is None:
            job.status = KB_JOB_STATUS_FAILED
            job.error_message = "Document not found"
            await db.flush()
            return

        kb = doc.knowledge_base
        owner = await db.get(User, kb.owner_user_id)
        if owner is None:
            job.status = KB_JOB_STATUS_FAILED
            job.error_message = "Knowledge base owner missing"
            await db.flush()
            return

        model: LLMModel | None = kb.embedding_model
        if model is None:
            model = await db.get(LLMModel, kb.embedding_model_id)
        if model is None:
            job.status = KB_JOB_STATUS_FAILED
            job.error_message = "Embedding model missing"
            doc.status = KB_DOC_STATUS_FAILED
            doc.error_message = job.error_message
            await db.flush()
            return

        hold_request_id = job.hold_ref_request_id or self.build_embed_hold_request_id(
            job.idempotency_key
        )
        job.hold_ref_request_id = hold_request_id
        hold_amount = 0
        wallet = None
        held = False

        try:
            if doc.status == KB_DOC_STATUS_INSUFFICIENT_BALANCE:
                self.transition_document(
                    doc, KB_DOC_STATUS_PENDING_EMBEDDING, clear_error=True
                )

            if doc.status not in (
                KB_DOC_STATUS_PENDING_EMBEDDING,
                KB_DOC_STATUS_PARTIAL,
                KB_DOC_STATUS_EMBEDDING,
            ):
                raise InvalidRequestError(
                    f"Document not ready for embed job (status={doc.status})"
                )

            chunks = await self._load_chunks_needing_embed(db, document_id=doc.id)
            if not chunks:
                # 已全部成功：幂等短路，不预扣
                if doc.status == KB_DOC_STATUS_PENDING_EMBEDDING:
                    self.transition_document(doc, KB_DOC_STATUS_EMBEDDING)
                elif doc.status == KB_DOC_STATUS_PARTIAL:
                    self.transition_document(doc, KB_DOC_STATUS_EMBEDDING)
                if doc.status == KB_DOC_STATUS_EMBEDDING:
                    self.transition_document(doc, KB_DOC_STATUS_READY, clear_error=True)
                job.status = KB_JOB_STATUS_SUCCESS
                job.progress = 100
                await db.flush()
                return

            billing_rule = await billing_service.require_active_rule(
                db, model_id=model.id
            )
            # 预扣按分块 token 合计（与落库 token_count 对齐；亦可用内容估算兜底）
            token_total = sum(max(0, c.token_count) for c in chunks)
            if token_total <= 0:
                token_total = estimate_embedding_input_tokens(
                    [c.content for c in chunks]
                )

            hold_amount = billing_service.estimate_hold_amount(
                billing_rule, token_total, max_output_tokens=0
            )
            wallet = await wallet_service.get_or_create_personal_wallet(db, owner.id)

            if hold_amount > 0:
                try:
                    await wallet_service.hold(
                        db,
                        wallet_id=wallet.id,
                        amount=hold_amount,
                        request_id=hold_request_id,
                        operator_user_id=owner.id,
                    )
                    held = True
                except InsufficientBalanceError:
                    # partial 重试余额不足：先回到 pending_embedding 再记 insufficient
                    if doc.status == KB_DOC_STATUS_PARTIAL:
                        doc.status = KB_DOC_STATUS_PENDING_EMBEDDING
                        doc.error_message = None
                    if doc.status == KB_DOC_STATUS_EMBEDDING:
                        doc.status = KB_DOC_STATUS_PENDING_EMBEDDING
                    if doc.status == KB_DOC_STATUS_PENDING_EMBEDDING:
                        self.transition_document(
                            doc,
                            KB_DOC_STATUS_INSUFFICIENT_BALANCE,
                            error_message="Insufficient wallet balance for embedding",
                        )
                    else:
                        doc.status = KB_DOC_STATUS_INSUFFICIENT_BALANCE
                        doc.error_message = (
                            "Insufficient wallet balance for embedding"
                        )
                    job.status = KB_JOB_STATUS_FAILED
                    job.error_message = "insufficient_balance"
                    await db.flush()
                    logger.info(
                        "kb_embed_insufficient_balance",
                        job_id=str(job.id),
                        document_id=str(doc.id),
                        required=hold_amount,
                    )
                    return

            # hold 成功后再进入 embedding
            if doc.status == KB_DOC_STATUS_PENDING_EMBEDDING:
                self.transition_document(doc, KB_DOC_STATUS_EMBEDDING, clear_error=True)
            elif doc.status == KB_DOC_STATUS_PARTIAL:
                self.transition_document(doc, KB_DOC_STATUS_EMBEDDING, clear_error=True)
            await db.flush()

            caller = InternalCaller(user=owner, source=USAGE_SOURCE_KB_INGEST)
            from app.services.kb_settings_service import kb_settings_service

            gs = await kb_settings_service.get_effective(db)
            batch_size = max(1, gs.embedding_batch_size)
            success_tokens = 0
            success_count = 0
            fail_count = 0
            fail_messages: list[str] = []
            total = len(chunks)

            for batch_idx in range(0, total, batch_size):
                batch = chunks[batch_idx : batch_idx + batch_size]
                texts = [c.content for c in batch]
                batch_req_id = f"{hold_request_id}:batch:{batch_idx // batch_size}"
                try:
                    result = await internal_embedding_service.embed_texts(
                        db,
                        caller=caller,
                        model_alias=model.alias,
                        texts=texts,
                        request_id=batch_req_id,
                        settle=False,
                        expected_dim=kb.embedding_dim,
                    )
                    for chunk_row, vec in zip(
                        batch, result.embeddings, strict=True
                    ):
                        chunk_row.embedding = vec
                        chunk_row.embedding_status = KB_CHUNK_EMBED_SUCCESS
                        success_count += 1
                    success_tokens += result.input_tokens
                except Exception as batch_exc:  # noqa: BLE001
                    msg = (
                        getattr(batch_exc, "message", None)
                        or str(batch_exc)
                        or batch_exc.__class__.__name__
                    )
                    fail_messages.append(msg)
                    for chunk_row in batch:
                        chunk_row.embedding = None
                        chunk_row.embedding_status = KB_CHUNK_EMBED_FAILED
                        fail_count += 1

                done = min(batch_idx + batch_size, total)
                job.progress = min(99, int(done * 100 / total))
                await db.flush()

            # 文档级结算：按成功分块实际 token 实扣（含一次 per_call）；全失败释放
            actual_charge = 0
            if success_count > 0:
                actual_charge = billing_service.calculate_charge_amount(
                    billing_rule, success_tokens, 0, 0
                )

            if held and hold_amount > 0 and wallet is not None:
                if success_count == 0:
                    await wallet_service.release(
                        db,
                        wallet_id=wallet.id,
                        hold_amount=hold_amount,
                        request_id=hold_request_id,
                        operator_user_id=owner.id,
                    )
                else:
                    await wallet_service.capture(
                        db,
                        wallet_id=wallet.id,
                        hold_amount=hold_amount,
                        actual_amount=actual_charge,
                        request_id=hold_request_id,
                        operator_user_id=owner.id,
                    )
                held = False

            if fail_count == 0:
                self.transition_document(doc, KB_DOC_STATUS_READY, clear_error=True)
                job.status = KB_JOB_STATUS_SUCCESS
                job.progress = 100
                job.error_message = None
            elif success_count == 0:
                err = "; ".join(fail_messages[:3]) or "All embedding batches failed"
                self.transition_document(
                    doc, KB_DOC_STATUS_FAILED, error_message=err
                )
                job.status = KB_JOB_STATUS_FAILED
                job.error_message = err
                job.retry_count = int(job.retry_count or 0) + 1
            else:
                err = (
                    f"{fail_count}/{total} chunks failed embedding"
                    + (f": {fail_messages[0]}" if fail_messages else "")
                )
                self.transition_document(
                    doc, KB_DOC_STATUS_PARTIAL, error_message=err
                )
                job.status = KB_JOB_STATUS_SUCCESS
                job.progress = 100
                job.error_message = err

            await db.flush()
            logger.info(
                "kb_embed_finished",
                job_id=str(job.id),
                document_id=str(doc.id),
                status=doc.status,
                success_count=success_count,
                fail_count=fail_count,
                charge_amount=actual_charge,
            )
        except Exception as exc:  # noqa: BLE001
            if held and hold_amount > 0 and wallet is not None:
                try:
                    await wallet_service.release(
                        db,
                        wallet_id=wallet.id,
                        hold_amount=hold_amount,
                        request_id=hold_request_id,
                        operator_user_id=owner.id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "kb_embed_release_failed",
                        job_id=str(job.id),
                        request_id=hold_request_id,
                    )
            message = (
                getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
            )
            try:
                if doc.status == KB_DOC_STATUS_PENDING_EMBEDDING:
                    self.transition_document(doc, KB_DOC_STATUS_EMBEDDING)
                if doc.status == KB_DOC_STATUS_EMBEDDING:
                    self.transition_document(
                        doc, KB_DOC_STATUS_FAILED, error_message=message
                    )
                else:
                    doc.status = KB_DOC_STATUS_FAILED
                    doc.error_message = message
            except InvalidRequestError:
                doc.status = KB_DOC_STATUS_FAILED
                doc.error_message = message
            job.status = KB_JOB_STATUS_FAILED
            job.error_message = message
            job.retry_count = int(job.retry_count or 0) + 1
            await db.flush()
            raise

    async def re_embed_chunk(
        self, db: AsyncSession, *, chunk_id: UUID
    ) -> KbChunk:
        """单分块重嵌入（内容变更后调用）；增量计费，供任务 7 使用。"""
        stmt = (
            select(KbChunk)
            .where(KbChunk.id == chunk_id)
            .options(
                selectinload(KbChunk.document)
                .selectinload(KbDocument.knowledge_base)
                .selectinload(KnowledgeBase.embedding_model)
            )
        )
        chunk_row = (await db.execute(stmt)).scalar_one_or_none()
        if chunk_row is None:
            raise ResourceNotFoundError("Chunk not found")

        doc = chunk_row.document
        kb = doc.knowledge_base
        owner = await db.get(User, kb.owner_user_id)
        if owner is None:
            raise ResourceNotFoundError("Knowledge base owner missing")

        model = kb.embedding_model
        if model is None:
            model = await db.get(LLMModel, kb.embedding_model_id)
        if model is None:
            raise InvalidRequestError("Embedding model missing")

        caller = InternalCaller(user=owner, source=USAGE_SOURCE_KB_INGEST)
        req_id = f"kb-reembed:{chunk_row.id}:{uuid.uuid4()}"
        chunk_row.embedding_status = KB_CHUNK_EMBED_PENDING
        await db.flush()

        try:
            result = await internal_embedding_service.embed_texts(
                db,
                caller=caller,
                model_alias=model.alias,
                texts=[chunk_row.content],
                request_id=req_id,
                settle=True,
                expected_dim=kb.embedding_dim,
            )
            chunk_row.embedding = result.embeddings[0]
            chunk_row.embedding_status = KB_CHUNK_EMBED_SUCCESS
            # 同步 token_count（编辑后可能已变）
            if chunk_row.token_count <= 0:
                chunk_row.token_count = result.input_tokens
            await db.flush()
            return chunk_row
        except Exception:
            chunk_row.embedding = None
            chunk_row.embedding_status = KB_CHUNK_EMBED_FAILED
            await db.flush()
            raise

    # ------------------------------------------------------------------
    # Worker 入口
    # ------------------------------------------------------------------

    async def run_ingest_job(self, db: AsyncSession, *, job: KbIngestJob) -> None:
        if job.job_type == KB_JOB_TYPE_PARSE:
            await self.run_parse_job(db, job=job)
        elif job.job_type == KB_JOB_TYPE_RECHUNK:
            await self.run_rechunk_job(db, job=job)
        elif job.job_type == KB_JOB_TYPE_EMBED:
            await self.run_embed_job(db, job=job)
        else:
            raise InvalidRequestError(
                f"Unsupported ingest job type: {job.job_type}"
            )

    async def process_one(
        self,
        db: AsyncSession,
        *,
        max_concurrent: int | None = None,
        batch_probe: int | None = None,
    ) -> KbIngestJob | None:
        """领取并执行一个 job；无可用 job 返回 None。"""
        job = await self.claim_next_job(
            db, batch_probe=batch_probe, max_concurrent=max_concurrent
        )
        if job is None:
            return None
        try:
            await self.run_ingest_job(db, job=job)
        except Exception:  # noqa: BLE001 — 已在 run_* 内落状态
            pass
        return job


kb_ingest_service = KnowledgeBaseIngestService()
