"""知识库 CRUD、文档上传/查询/删除与配额校验。

上传成功后创建 parse ingest job；解析/分块由 kb_worker 异步执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.constants import (
    KB_CHUNK_EMBED_PENDING,
    KB_DOC_STATUS_FAILED,
    KB_DOC_STATUS_PARSING,
    KB_DOC_STATUS_PARTIAL,
    KB_DOC_STATUS_READY,
    KB_DOC_STATUS_UPLOADED,
    KB_STATUS_ACTIVE,
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
)
from app.core.errors import (
    ForbiddenError,
    InvalidRequestError,
    KbDisabledError,
    QuotaExceededError,
    ResourceNotFoundError,
)
from app.db.models.kb import KbChunk, KbDocument, KnowledgeBase
from app.db.models.llm_model import LLMModel
from app.db.models.user import Role, UserRole
from app.kb.tokens import estimate_tokens
from app.schemas.kb import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KbChunkBatchRequest,
    KbChunkCreate,
    KbChunkUpdate,
    RechunkRequest,
)
from app.services.billing_service import billing_service
from app.services.kb_file_validation import detect_and_validate_file_type
from app.services.kb_ingest_service import kb_ingest_service
from app.services.kb_settings_service import kb_settings_service
from app.services.permission_service import permission_service
from app.storage import FileStorage, get_file_storage

_WORKBENCH_DOC_STATUSES = frozenset(
    {
        KB_DOC_STATUS_READY,
        KB_DOC_STATUS_PARTIAL,
        KB_DOC_STATUS_FAILED,
    }
)

_RECHUNKABLE_DOC_STATUSES = frozenset(
    {
        KB_DOC_STATUS_READY,
        KB_DOC_STATUS_PARTIAL,
        KB_DOC_STATUS_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class KbQuotaLimits:
    max_kb_count: int
    max_docs_per_kb: int
    max_storage_bytes: int
    max_file_size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadFileInput:
    filename: str
    content: bytes


class KnowledgeBaseService:
    def __init__(self, storage: FileStorage | None = None) -> None:
        self.storage = storage or get_file_storage()

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------

    async def resolve_quota_limits(
        self, db: AsyncSession, *, user_id: UUID
    ) -> KbQuotaLimits:
        """读取用户角色配额；字段为 NULL 时用系统默认。多角色取各字段最大值（更宽松的并集）。"""
        stmt = (
            select(Role)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
        roles = list((await db.execute(stmt)).scalars().all())
        gs = await kb_settings_service.get_effective(db)

        def _pick(attr: str, default: int) -> int:
            values = [getattr(r, attr) for r in roles if getattr(r, attr) is not None]
            return max(values) if values else default

        return KbQuotaLimits(
            max_kb_count=_pick("kb_max_kb_count", settings.kb_default_max_kb_count),
            max_docs_per_kb=_pick(
                "kb_max_docs_per_kb", settings.kb_default_max_docs_per_kb
            ),
            max_storage_bytes=_pick(
                "kb_max_storage_bytes", settings.kb_default_max_storage_bytes
            ),
            max_file_size_bytes=_pick(
                "kb_max_file_size_bytes", gs.max_file_size_bytes
            ),
        )

    async def _count_user_kbs(self, db: AsyncSession, *, user_id: UUID) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(KnowledgeBase)
            .where(KnowledgeBase.owner_user_id == user_id)
        )
        return int(result.scalar_one())

    async def _sum_user_storage_bytes(self, db: AsyncSession, *, user_id: UUID) -> int:
        result = await db.execute(
            select(func.coalesce(func.sum(KnowledgeBase.storage_bytes), 0)).where(
                KnowledgeBase.owner_user_id == user_id
            )
        )
        return int(result.scalar_one())

    # ------------------------------------------------------------------
    # Embedding 模型
    # ------------------------------------------------------------------

    async def _resolve_embedding_model(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        model_alias: str | None,
    ) -> LLMModel:
        gs = await kb_settings_service.get_effective(db)
        alias = (model_alias or gs.default_embedding_model or "").strip()
        if not alias:
            raise InvalidRequestError(
                "No default embedding model configured. "
                "Set KB_DEFAULT_EMBEDDING_MODEL or pass embedding_model."
            )
        access = await permission_service.check_model_access(
            db, user_id=user_id, model_alias=alias, api_key=None
        )
        model = access.model
        if model.model_type != MODEL_TYPE_EMBEDDING:
            raise InvalidRequestError(
                f"Model '{alias}' is not an embedding model (model_type={model.model_type})"
            )
        if model.status != STATUS_ENABLED:
            raise InvalidRequestError(f"Embedding model '{alias}' is disabled")
        await billing_service.require_active_rule(db, model_id=model.id)
        return model

    # ------------------------------------------------------------------
    # 归属
    # ------------------------------------------------------------------

    async def get_kb_raw(self, db: AsyncSession, kb_id: UUID) -> KnowledgeBase | None:
        return await db.get(KnowledgeBase, kb_id)

    async def require_owned_kb(
        self, db: AsyncSession, *, user_id: UUID, kb_id: UUID
    ) -> KnowledgeBase:
        kb = await self.get_kb_raw(db, kb_id)
        if kb is None:
            raise ResourceNotFoundError("Knowledge base not found")
        if kb.owner_user_id != user_id:
            raise ForbiddenError("You do not have access to this knowledge base")
        return kb

    async def require_owned_document(
        self, db: AsyncSession, *, user_id: UUID, doc_id: UUID
    ) -> tuple[KbDocument, KnowledgeBase]:
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
        return doc, kb

    async def require_owned_chunk(
        self, db: AsyncSession, *, user_id: UUID, chunk_id: UUID
    ) -> tuple[KbChunk, KbDocument, KnowledgeBase]:
        stmt = (
            select(KbChunk)
            .where(KbChunk.id == chunk_id)
            .options(
                selectinload(KbChunk.document).selectinload(KbDocument.knowledge_base)
            )
        )
        chunk = (await db.execute(stmt)).scalar_one_or_none()
        if chunk is None:
            raise ResourceNotFoundError("Chunk not found")
        doc = chunk.document
        kb = doc.knowledge_base
        if kb.owner_user_id != user_id:
            raise ForbiddenError("You do not have access to this chunk")
        return chunk, doc, kb

    @staticmethod
    def _escape_like(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    def _ensure_workbench_doc(self, doc: KbDocument) -> None:
        if doc.status not in _WORKBENCH_DOC_STATUSES:
            raise InvalidRequestError(
                "Document must be ready / partial / failed to manage chunks "
                f"(current status={doc.status})"
            )

    # ------------------------------------------------------------------
    # 知识库 CRUD
    # ------------------------------------------------------------------

    async def list_knowledge_bases(
        self, db: AsyncSession, *, user_id: UUID
    ) -> list[KnowledgeBase]:
        stmt = (
            select(KnowledgeBase)
            .where(KnowledgeBase.owner_user_id == user_id)
            .options(selectinload(KnowledgeBase.embedding_model))
            .order_by(KnowledgeBase.updated_at.desc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def create_knowledge_base(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        body: KnowledgeBaseCreate,
    ) -> KnowledgeBase:
        limits = await self.resolve_quota_limits(db, user_id=user_id)
        current = await self._count_user_kbs(db, user_id=user_id)
        if current >= limits.max_kb_count:
            raise QuotaExceededError(
                f"Knowledge base quota exceeded (max {limits.max_kb_count})",
                code="kb_quota_exceeded",
            )

        model = await self._resolve_embedding_model(
            db, user_id=user_id, model_alias=body.embedding_model
        )

        chunk_size = body.chunk_size if body.chunk_size is not None else 500
        chunk_overlap = body.chunk_overlap if body.chunk_overlap is not None else 50
        if chunk_overlap >= chunk_size:
            raise InvalidRequestError("chunk_overlap must be less than chunk_size")

        kb = KnowledgeBase(
            owner_user_id=user_id,
            name=body.name,
            description=body.description,
            embedding_model_id=model.id,
            embedding_dim=settings.kb_embedding_dim,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=body.separators if body.separators is not None else [],
            top_k=body.top_k if body.top_k is not None else 5,
            score_threshold=(
                body.score_threshold if body.score_threshold is not None else 0.35
            ),
            status=KB_STATUS_ACTIVE,
        )
        db.add(kb)
        await db.flush()
        stmt = (
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb.id)
            .options(selectinload(KnowledgeBase.embedding_model))
        )
        return (await db.execute(stmt)).scalar_one()

    async def get_knowledge_base(
        self, db: AsyncSession, *, user_id: UUID, kb_id: UUID
    ) -> KnowledgeBase:
        await self.require_owned_kb(db, user_id=user_id, kb_id=kb_id)
        stmt = (
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
            .options(selectinload(KnowledgeBase.embedding_model))
        )
        return (await db.execute(stmt)).scalar_one()

    async def update_knowledge_base(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        kb_id: UUID,
        body: KnowledgeBaseUpdate,
    ) -> KnowledgeBase:
        kb = await self.require_owned_kb(db, user_id=user_id, kb_id=kb_id)
        data = body.model_dump(exclude_unset=True)
        if "chunk_size" in data or "chunk_overlap" in data:
            new_size = data.get("chunk_size", kb.chunk_size)
            new_overlap = data.get("chunk_overlap", kb.chunk_overlap)
            if new_overlap >= new_size:
                raise InvalidRequestError("chunk_overlap must be less than chunk_size")
        for key, value in data.items():
            setattr(kb, key, value)
        await db.flush()
        stmt = (
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb.id)
            .options(selectinload(KnowledgeBase.embedding_model))
        )
        return (await db.execute(stmt)).scalar_one()

    async def delete_knowledge_base(
        self, db: AsyncSession, *, user_id: UUID, kb_id: UUID
    ) -> None:
        kb = await self.require_owned_kb(db, user_id=user_id, kb_id=kb_id)
        # 先收集原文件路径，再删库（CASCADE 清文档/分块/任务）
        paths = list(
            (
                await db.execute(
                    select(KbDocument.storage_path).where(KbDocument.kb_id == kb.id)
                )
            )
            .scalars()
            .all()
        )
        await db.delete(kb)
        await db.flush()
        for path in paths:
            self.storage.delete(path)

    # ------------------------------------------------------------------
    # 文档
    # ------------------------------------------------------------------

    async def require_active_owned_kb(
        self, db: AsyncSession, *, user_id: UUID, kb_id: UUID
    ) -> KnowledgeBase:
        """归属校验且要求库处于 active；禁用库拒绝上传/写操作。"""
        kb = await self.require_owned_kb(db, user_id=user_id, kb_id=kb_id)
        if kb.status != KB_STATUS_ACTIVE:
            raise KbDisabledError(
                "Knowledge base is disabled and cannot accept uploads or updates"
            )
        return kb

    async def upload_documents(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        kb_id: UUID,
        files: list[UploadFileInput],
    ) -> list[KbDocument]:
        if not files:
            raise InvalidRequestError("No files uploaded")
        gs = await kb_settings_service.get_effective(db)
        if len(files) > gs.upload_batch_limit:
            raise QuotaExceededError(
                f"Batch upload limit exceeded (max {gs.upload_batch_limit} files)",
                code="batch_too_large",
            )

        kb = await self.require_active_owned_kb(db, user_id=user_id, kb_id=kb_id)
        limits = await self.resolve_quota_limits(db, user_id=user_id)

        # 先全部校验：类型 / 单文件大小；失败则不写库不存文件
        validated: list[tuple[str, str, bytes]] = []
        total_new_bytes = 0
        for f in files:
            name = f.filename or "unnamed"
            data = f.content
            size = len(data)
            if size > limits.max_file_size_bytes:
                raise QuotaExceededError(
                    f"File '{name}' exceeds max size "
                    f"({limits.max_file_size_bytes} bytes)",
                    code="file_too_large",
                )
            file_type = detect_and_validate_file_type(name, data)
            validated.append((name, file_type, data))
            total_new_bytes += size

        if kb.doc_count + len(validated) > limits.max_docs_per_kb:
            raise QuotaExceededError(
                f"Document quota exceeded for this knowledge base "
                f"(max {limits.max_docs_per_kb})",
                code="kb_doc_quota_exceeded",
            )

        used_storage = await self._sum_user_storage_bytes(db, user_id=user_id)
        if used_storage + total_new_bytes > limits.max_storage_bytes:
            raise QuotaExceededError(
                f"Storage quota exceeded (max {limits.max_storage_bytes} bytes)",
                code="kb_storage_quota_exceeded",
            )

        saved_keys: list[str] = []
        docs: list[KbDocument] = []
        try:
            for name, file_type, data in validated:
                key = self.storage.save(data)
                saved_keys.append(key)
                doc = KbDocument(
                    kb_id=kb.id,
                    file_name=name,
                    file_type=file_type,
                    file_size_bytes=len(data),
                    storage_path=key,
                    chunk_size=kb.chunk_size,
                    chunk_overlap=kb.chunk_overlap,
                    separators=list(kb.separators or []),
                    status=KB_DOC_STATUS_UPLOADED,
                )
                db.add(doc)
                docs.append(doc)

            kb.doc_count += len(docs)
            kb.storage_bytes += total_new_bytes
            await db.flush()
            for doc in docs:
                await db.refresh(doc)
                await kb_ingest_service.create_parse_job(db, doc=doc)
            return docs
        except Exception:
            for key in saved_keys:
                self.storage.delete(key)
            raise

    async def list_documents(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        kb_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KbDocument], int]:
        await self.require_owned_kb(db, user_id=user_id, kb_id=kb_id)
        page = max(1, page)
        page_size = min(max(1, page_size), 100)

        total = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(KbDocument)
                    .where(KbDocument.kb_id == kb_id)
                )
            ).scalar_one()
        )
        stmt = (
            select(KbDocument)
            .where(KbDocument.kb_id == kb_id)
            .order_by(KbDocument.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await db.execute(stmt)).scalars().all())
        return items, total

    async def get_document(
        self, db: AsyncSession, *, user_id: UUID, doc_id: UUID
    ) -> KbDocument:
        doc, _kb = await self.require_owned_document(db, user_id=user_id, doc_id=doc_id)
        return doc

    async def get_document_progress(
        self, db: AsyncSession, *, document_id: UUID
    ) -> int:
        return await kb_ingest_service.get_latest_job_progress(
            db, document_id=document_id
        )

    async def retry_document(
        self, db: AsyncSession, *, user_id: UUID, doc_id: UUID
    ) -> KbDocument:
        doc, _job = await kb_ingest_service.retry_document(
            db, user_id=user_id, doc_id=doc_id
        )
        return doc

    async def delete_document(
        self, db: AsyncSession, *, user_id: UUID, doc_id: UUID
    ) -> None:
        doc, kb = await self.require_owned_document(db, user_id=user_id, doc_id=doc_id)
        storage_path = doc.storage_path
        size = doc.file_size_bytes
        chunk_n = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(KbChunk)
                    .where(KbChunk.document_id == doc.id)
                )
            ).scalar_one()
        )

        await db.delete(doc)
        kb.doc_count = max(0, kb.doc_count - 1)
        kb.storage_bytes = max(0, kb.storage_bytes - size)
        kb.chunk_count = max(0, kb.chunk_count - chunk_n)
        await db.flush()
        self.storage.delete(storage_path)

    # ------------------------------------------------------------------
    # 分块工作台
    # ------------------------------------------------------------------

    async def list_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        doc_id: UUID,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
    ) -> tuple[list[KbChunk], int]:
        await self.require_owned_document(db, user_id=user_id, doc_id=doc_id)
        page = max(1, page)
        page_size = min(max(1, page_size), 100)

        filters = [KbChunk.document_id == doc_id]
        keyword = (q or "").strip()
        if keyword:
            filters.append(
                KbChunk.content.ilike(f"%{self._escape_like(keyword)}%", escape="\\")
            )

        total = int(
            (
                await db.execute(
                    select(func.count()).select_from(KbChunk).where(*filters)
                )
            ).scalar_one()
        )
        stmt = (
            select(KbChunk)
            .where(*filters)
            .order_by(KbChunk.seq.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await db.execute(stmt)).scalars().all())
        return items, total

    async def update_chunk(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        chunk_id: UUID,
        body: KbChunkUpdate,
    ) -> KbChunk:
        chunk, doc, _kb = await self.require_owned_chunk(
            db, user_id=user_id, chunk_id=chunk_id
        )
        self._ensure_workbench_doc(doc)
        data = body.model_dump(exclude_unset=True)
        if not data:
            raise InvalidRequestError("No fields to update")

        content_changed = False
        if "content" in data:
            new_content = data["content"]
            if not new_content or not str(new_content).strip():
                raise InvalidRequestError("content must not be empty")
            new_content = str(new_content)
            old_tokens = chunk.token_count
            chunk.content = new_content
            chunk.token_count = estimate_tokens(new_content)
            chunk.is_manually_edited = True
            doc.total_tokens = max(0, doc.total_tokens - old_tokens + chunk.token_count)
            content_changed = True

        if "is_enabled" in data:
            chunk.is_enabled = bool(data["is_enabled"])

        await db.flush()

        if content_changed:
            chunk = await kb_ingest_service.re_embed_chunk(db, chunk_id=chunk.id)

        await db.refresh(chunk)
        return chunk

    async def delete_chunk(
        self, db: AsyncSession, *, user_id: UUID, chunk_id: UUID
    ) -> None:
        chunk, doc, kb = await self.require_owned_chunk(
            db, user_id=user_id, chunk_id=chunk_id
        )
        self._ensure_workbench_doc(doc)
        tokens = chunk.token_count
        await db.delete(chunk)
        kb.chunk_count = max(0, kb.chunk_count - 1)
        doc.total_tokens = max(0, doc.total_tokens - tokens)
        await db.flush()

    async def create_chunk(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        doc_id: UUID,
        body: KbChunkCreate,
    ) -> KbChunk:
        doc, kb = await self.require_owned_document(
            db, user_id=user_id, doc_id=doc_id
        )
        self._ensure_workbench_doc(doc)

        content = body.content.strip()
        if not content:
            raise InvalidRequestError("content must not be empty")

        max_seq = (
            await db.execute(
                select(func.coalesce(func.max(KbChunk.seq), -1)).where(
                    KbChunk.document_id == doc.id
                )
            )
        ).scalar_one()
        max_seq = int(max_seq)

        if body.seq is None:
            seq = max_seq + 1
        else:
            seq = body.seq
            if seq < 0:
                raise InvalidRequestError("seq must be >= 0")
            # 指定位置及之后序号顺延
            await db.execute(
                update(KbChunk)
                .where(KbChunk.document_id == doc.id, KbChunk.seq >= seq)
                .values(seq=KbChunk.seq + 1)
            )

        token_count = estimate_tokens(content)
        chunk = KbChunk(
            document_id=doc.id,
            kb_id=doc.kb_id,
            seq=seq,
            content=content,
            token_count=token_count,
            source_location=None,
            is_enabled=True,
            is_manually_edited=True,
            embedding=None,
            embedding_status=KB_CHUNK_EMBED_PENDING,
        )
        db.add(chunk)
        kb.chunk_count += 1
        doc.total_tokens += token_count
        await db.flush()

        chunk = await kb_ingest_service.re_embed_chunk(db, chunk_id=chunk.id)
        await db.refresh(chunk)
        return chunk

    async def batch_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        body: KbChunkBatchRequest,
    ) -> int:
        action = body.action
        # 去重并保持顺序
        seen: set[UUID] = set()
        chunk_ids: list[UUID] = []
        for cid in body.chunk_ids:
            if cid not in seen:
                seen.add(cid)
                chunk_ids.append(cid)

        stmt = (
            select(KbChunk)
            .where(KbChunk.id.in_(chunk_ids))
            .options(
                selectinload(KbChunk.document).selectinload(KbDocument.knowledge_base)
            )
        )
        rows = list((await db.execute(stmt)).scalars().all())
        if len(rows) != len(chunk_ids):
            raise ResourceNotFoundError("One or more chunks not found")

        for chunk in rows:
            kb = chunk.document.knowledge_base
            if kb.owner_user_id != user_id:
                raise ForbiddenError("You do not have access to this chunk")
            self._ensure_workbench_doc(chunk.document)

        affected = 0
        if action == "enable":
            for chunk in rows:
                if not chunk.is_enabled:
                    chunk.is_enabled = True
                    affected += 1
            await db.flush()
            return affected

        if action == "disable":
            for chunk in rows:
                if chunk.is_enabled:
                    chunk.is_enabled = False
                    affected += 1
            await db.flush()
            return affected

        if action == "delete":
            # 按文档聚合统计冗余
            by_doc: dict[UUID, list[KbChunk]] = {}
            for chunk in rows:
                by_doc.setdefault(chunk.document_id, []).append(chunk)

            for doc_chunks in by_doc.values():
                doc = doc_chunks[0].document
                kb = doc.knowledge_base
                tokens = sum(c.token_count for c in doc_chunks)
                for c in doc_chunks:
                    await db.delete(c)
                kb.chunk_count = max(0, kb.chunk_count - len(doc_chunks))
                doc.total_tokens = max(0, doc.total_tokens - tokens)
                affected += len(doc_chunks)
            await db.flush()
            return affected

        raise InvalidRequestError(f"Unsupported batch action: {action}")

    async def rechunk_document(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        doc_id: UUID,
        body: RechunkRequest,
    ) -> KbDocument:
        if not body.confirm:
            raise InvalidRequestError(
                "rechunk requires confirm=true "
                "(will delete existing chunks including manual edits and re-bill embedding)"
            )

        doc, _kb = await self.require_owned_document(
            db, user_id=user_id, doc_id=doc_id
        )
        if doc.status not in _RECHUNKABLE_DOC_STATUSES:
            raise InvalidRequestError(
                "Only ready / partial / failed documents can be rechunked "
                f"(current status={doc.status})"
            )

        new_size = body.chunk_size if body.chunk_size is not None else doc.chunk_size
        new_overlap = (
            body.chunk_overlap
            if body.chunk_overlap is not None
            else doc.chunk_overlap
        )
        if new_overlap >= new_size:
            raise InvalidRequestError("chunk_overlap must be less than chunk_size")

        doc.chunk_size = new_size
        doc.chunk_overlap = new_overlap
        if body.separators is not None:
            doc.separators = list(body.separators)

        kb_ingest_service.transition_document(
            doc, KB_DOC_STATUS_PARSING, clear_error=True
        )
        await kb_ingest_service.create_rechunk_job(db, doc=doc)
        await db.flush()
        await db.refresh(doc)
        return doc


kb_service = KnowledgeBaseService()
