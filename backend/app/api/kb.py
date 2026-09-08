"""用户端知识库与文档 API（JWT 鉴权，按 owner_user_id 隔离）。"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.db.models.kb import KbChunk, KbDocument, KnowledgeBase
from app.db.session import get_db
from app.schemas.auth import UserContext
from app.schemas.kb import (
    HitTestHitOut,
    HitTestOut,
    HitTestRequest,
    KbChunkBatchOut,
    KbChunkBatchRequest,
    KbChunkCreate,
    KbChunkListOut,
    KbChunkOut,
    KbChunkUpdate,
    KbDocumentListOut,
    KbDocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
    RechunkRequest,
)
from app.services.kb_service import UploadFileInput, kb_service
from app.services.retrieval_service import retrieval_service

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


def _kb_out(kb: KnowledgeBase) -> KnowledgeBaseOut:
    alias = kb.embedding_model.alias if kb.embedding_model is not None else None
    return KnowledgeBaseOut(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        embedding_model_id=kb.embedding_model_id,
        embedding_model=alias,
        embedding_dim=kb.embedding_dim,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        separators=kb.separators or [],
        top_k=kb.top_k,
        score_threshold=kb.score_threshold,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        storage_bytes=kb.storage_bytes,
        status=kb.status,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


async def _doc_out(db: AsyncSession, doc: KbDocument) -> KbDocumentOut:
    progress = await kb_service.get_document_progress(db, document_id=doc.id)
    return KbDocumentOut(
        id=doc.id,
        kb_id=doc.kb_id,
        file_name=doc.file_name,
        file_type=doc.file_type,
        file_size_bytes=doc.file_size_bytes,
        status=doc.status,
        error_message=doc.error_message,
        total_tokens=doc.total_tokens,
        progress=progress,
        chunk_size=doc.chunk_size,
        chunk_overlap=doc.chunk_overlap,
        separators=doc.separators or [],
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _chunk_out(chunk: KbChunk) -> KbChunkOut:
    return KbChunkOut(
        id=chunk.id,
        document_id=chunk.document_id,
        kb_id=chunk.kb_id,
        seq=chunk.seq,
        content=chunk.content,
        token_count=chunk.token_count,
        source_location=chunk.source_location,
        is_enabled=chunk.is_enabled,
        is_manually_edited=chunk.is_manually_edited,
        embedding_status=chunk.embedding_status,
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBaseOut]:
    rows = await kb_service.list_knowledge_bases(db, user_id=ctx.user_id)
    return [_kb_out(kb) for kb in rows]


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    kb = await kb_service.create_knowledge_base(db, user_id=ctx.user_id, body=body)
    return _kb_out(kb)


@router.get("/documents/{doc_id}", response_model=KbDocumentOut)
async def get_document(
    doc_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbDocumentOut:
    doc = await kb_service.get_document(db, user_id=ctx.user_id, doc_id=doc_id)
    return await _doc_out(db, doc)


@router.post("/documents/{doc_id}/retry", response_model=KbDocumentOut)
async def retry_document(
    doc_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbDocumentOut:
    doc = await kb_service.retry_document(db, user_id=ctx.user_id, doc_id=doc_id)
    return await _doc_out(db, doc)


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await kb_service.delete_document(db, user_id=ctx.user_id, doc_id=doc_id)
    return Response(status_code=204)


@router.get("/documents/{doc_id}/chunks", response_model=KbChunkListOut)
async def list_document_chunks(
    doc_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=200),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbChunkListOut:
    items, total = await kb_service.list_chunks(
        db,
        user_id=ctx.user_id,
        doc_id=doc_id,
        page=page,
        page_size=page_size,
        q=q,
    )
    return KbChunkListOut(
        items=[_chunk_out(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/documents/{doc_id}/chunks", response_model=KbChunkOut, status_code=201
)
async def create_document_chunk(
    doc_id: UUID,
    body: KbChunkCreate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbChunkOut:
    chunk = await kb_service.create_chunk(
        db, user_id=ctx.user_id, doc_id=doc_id, body=body
    )
    return _chunk_out(chunk)


@router.post("/documents/{doc_id}/rechunk", response_model=KbDocumentOut)
async def rechunk_document(
    doc_id: UUID,
    body: RechunkRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbDocumentOut:
    doc = await kb_service.rechunk_document(
        db, user_id=ctx.user_id, doc_id=doc_id, body=body
    )
    return await _doc_out(db, doc)


@router.patch("/chunks/{chunk_id}", response_model=KbChunkOut)
async def update_chunk(
    chunk_id: UUID,
    body: KbChunkUpdate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbChunkOut:
    chunk = await kb_service.update_chunk(
        db, user_id=ctx.user_id, chunk_id=chunk_id, body=body
    )
    return _chunk_out(chunk)


@router.delete("/chunks/{chunk_id}", status_code=204)
async def delete_chunk(
    chunk_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await kb_service.delete_chunk(db, user_id=ctx.user_id, chunk_id=chunk_id)
    return Response(status_code=204)


@router.post("/chunks/batch", response_model=KbChunkBatchOut)
async def batch_chunks(
    body: KbChunkBatchRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbChunkBatchOut:
    affected = await kb_service.batch_chunks(db, user_id=ctx.user_id, body=body)
    return KbChunkBatchOut(action=body.action, affected=affected)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    kb = await kb_service.get_knowledge_base(db, user_id=ctx.user_id, kb_id=kb_id)
    return _kb_out(kb)


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    kb_id: UUID,
    body: KnowledgeBaseUpdate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    kb = await kb_service.update_knowledge_base(
        db, user_id=ctx.user_id, kb_id=kb_id, body=body
    )
    return _kb_out(kb)


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await kb_service.delete_knowledge_base(db, user_id=ctx.user_id, kb_id=kb_id)
    return Response(status_code=204)


@router.post("/{kb_id}/documents", response_model=list[KbDocumentOut], status_code=201)
async def upload_documents(
    kb_id: UUID,
    files: list[UploadFile] = File(...),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[KbDocumentOut]:
    payloads: list[UploadFileInput] = []
    for f in files:
        content = await f.read()
        payloads.append(
            UploadFileInput(filename=f.filename or "unnamed", content=content)
        )
    docs = await kb_service.upload_documents(
        db, user_id=ctx.user_id, kb_id=kb_id, files=payloads
    )
    return [await _doc_out(db, d) for d in docs]


@router.get("/{kb_id}/documents", response_model=KbDocumentListOut)
async def list_documents(
    kb_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> KbDocumentListOut:
    items, total = await kb_service.list_documents(
        db, user_id=ctx.user_id, kb_id=kb_id, page=page, page_size=page_size
    )
    return KbDocumentListOut(
        items=[await _doc_out(db, d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{kb_id}/hit-test", response_model=HitTestOut)
async def hit_test(
    kb_id: UUID,
    body: HitTestRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> HitTestOut:
    """命中测试：参数缺省取库默认检索参数；query 向量化按 embedding 计费。"""
    from app.core.constants import KB_STATUS_ACTIVE
    from app.core.errors import KbDisabledError

    kb = await kb_service.require_owned_kb(db, user_id=ctx.user_id, kb_id=kb_id)
    if kb.status != KB_STATUS_ACTIVE:
        raise KbDisabledError(
            "Knowledge base is disabled and cannot be searched"
        )
    top_k = body.top_k if body.top_k is not None else kb.top_k
    score_threshold = (
        body.score_threshold
        if body.score_threshold is not None
        else kb.score_threshold
    )
    result = await retrieval_service.search(
        db,
        user=ctx.user,
        kb_ids=[kb_id],
        query=body.query,
        top_k=top_k,
        score_threshold=score_threshold,
    )
    return HitTestOut(
        query=body.query.strip(),
        top_k=top_k,
        score_threshold=score_threshold,
        hits=[
            HitTestHitOut(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                document_name=h.document_name,
                content=h.content,
                score=h.score,
                seq=h.seq,
                source_location=h.source_location,
                token_count=h.token_count,
            )
            for h in result.chunks
        ],
        estimated_charge_amount=result.charge_amount,
        request_id=result.request_ids[0] if result.request_ids else None,
        input_tokens=result.input_tokens,
    )
