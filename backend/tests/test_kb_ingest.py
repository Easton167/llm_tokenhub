"""知识库专项任务 4：异步 ingest 流水线与状态机。

验收：
  - 上传样例文件后经 worker 到达 pending_embedding，分块数与引擎一致
  - 解析失败 → failed 且错误可见；retry 后可重新处理
  - 同一文档重复执行不产生重复分块
  - 用户并发上限：超出的 job 保持 pending 排队
  - FOR UPDATE SKIP LOCKED：两会话不重复领取同一 job
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.constants import (
    KB_DOC_STATUS_EMBEDDING,
    KB_DOC_STATUS_FAILED,
    KB_DOC_STATUS_PARSING,
    KB_DOC_STATUS_PENDING_EMBEDDING,
    KB_DOC_STATUS_READY,
    KB_DOC_STATUS_UPLOADED,
    KB_JOB_STATUS_PENDING,
    KB_JOB_STATUS_RUNNING,
    KB_JOB_STATUS_SUCCESS,
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
)
from app.core.errors import InvalidRequestError
from app.db.models.billing import BillingRule
from app.db.models.kb import KbChunk, KbDocument, KbIngestJob, KnowledgeBase
from app.db.models.llm_model import LLMModel
from app.db.session import get_db
from app.kb import chunk, parse
from app.main import app
from app.services.kb_ingest_service import kb_ingest_service
from app.services.kb_service import kb_service
from app.storage import get_file_storage
from app.storage.local import LocalFileStorage

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kb"

# 6 类验收样例（与引擎单测同源）
SAMPLE_FILES: list[tuple[str, str]] = [
    ("sample.txt", "txt"),
    ("sample.md", "md"),
    ("sample.html", "html"),
    ("sample.csv", "csv"),
    ("sample.pdf", "pdf"),
    ("sample.docx", "docx"),
]


@pytest_asyncio.fixture
async def user_client(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient, None]:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    storage_root = tmp_path / "kb_files"
    monkeypatch.setattr(settings, "kb_storage_root", str(storage_root))
    get_file_storage.cache_clear()
    storage = LocalFileStorage(storage_root)
    kb_service.storage = storage
    kb_ingest_service.storage = storage

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()
    get_file_storage.cache_clear()


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client: AsyncClient) -> tuple[str, str]:
    email = f"kb4-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbIngest"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], email


async def _seed_embedding(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"emb-kb4-{uuid4().hex[:6]}",
        display_name="emb",
        model_type=MODEL_TYPE_EMBEDDING,
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    session.add(
        BillingRule(
            model_id=model.id,
            version=1,
            price_input=100,
            price_output=0,
            price_cached_input=0,
            price_per_call=0,
            cost_input=50,
            cost_output=0,
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            status=0,
        )
    )
    await session.flush()
    return model


def _load(name: str) -> bytes:
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture: {path}"
    return path.read_bytes()


# ---------------------------------------------------------------------------
# 状态机单元
# ---------------------------------------------------------------------------


def test_illegal_transition_rejected() -> None:
    doc = KbDocument(
        kb_id=uuid4(),
        file_name="x.txt",
        file_type="txt",
        file_size_bytes=1,
        storage_path="ab/cd/x",
        chunk_size=500,
        chunk_overlap=50,
        separators=[],
        status=KB_DOC_STATUS_UPLOADED,
    )
    with pytest.raises(InvalidRequestError):
        kb_ingest_service.transition_document(doc, KB_DOC_STATUS_PENDING_EMBEDDING)

    # ready 仅允许进入 parsing（rechunk），不可直接 embedding
    doc.status = KB_DOC_STATUS_READY
    with pytest.raises(InvalidRequestError):
        kb_ingest_service.transition_document(doc, KB_DOC_STATUS_EMBEDDING)

    kb_ingest_service.transition_document(doc, KB_DOC_STATUS_PARSING)
    assert doc.status == KB_DOC_STATUS_PARSING


def test_legal_transitions() -> None:
    doc = KbDocument(
        kb_id=uuid4(),
        file_name="x.txt",
        file_type="txt",
        file_size_bytes=1,
        storage_path="ab/cd/x",
        chunk_size=500,
        chunk_overlap=50,
        separators=[],
        status=KB_DOC_STATUS_UPLOADED,
    )
    kb_ingest_service.transition_document(doc, KB_DOC_STATUS_PARSING)
    kb_ingest_service.transition_document(doc, KB_DOC_STATUS_FAILED, error_message="坏了")
    assert doc.error_message == "坏了"
    kb_ingest_service.transition_document(doc, KB_DOC_STATUS_PARSING, clear_error=True)
    assert doc.error_message is None


# ---------------------------------------------------------------------------
# 流水线集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_six_sample_types_reach_pending_embedding(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post(
            "/kb",
            headers=headers,
            json={"name": "流水线库", "chunk_size": 500, "chunk_overlap": 50},
        )
    ).json()["id"]

    files = [
        ("files", (name, _load(name), "application/octet-stream"))
        for name, _ft in SAMPLE_FILES
    ]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents", headers=headers, files=files
    )
    assert uploaded.status_code == 201, uploaded.text
    docs = uploaded.json()
    assert len(docs) == 6
    assert all(d["status"] == "uploaded" for d in docs)
    assert all(d["progress"] == 0 for d in docs)

    # 上传后应已创建 parse job
    job_count = (
        await db_session.execute(
            select(func.count())
            .select_from(KbIngestJob)
            .where(KbIngestJob.kb_id == kb_id)
        )
    ).scalar_one()
    assert job_count == 6

    # 只跑 parse job（任务 4 停在 pending_embedding；embed 由任务 5 覆盖）
    for _ in range(6):
        job = await kb_ingest_service.process_one(db_session, max_concurrent=10)
        assert job is not None
        assert job.job_type == "parse"
        assert job.status == KB_JOB_STATUS_SUCCESS

    for doc_json, (fname, ftype) in zip(docs, SAMPLE_FILES, strict=True):
        detail = await user_client.get(
            f"/kb/documents/{doc_json['id']}", headers=headers
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["status"] == KB_DOC_STATUS_PENDING_EMBEDDING, body
        assert body["progress"] == 100
        assert body["error_message"] is None

        expected = chunk(
            parse(_load(fname), ftype), chunk_size=500, chunk_overlap=50
        )
        db_chunks = list(
            (
                await db_session.execute(
                    select(KbChunk)
                    .where(KbChunk.document_id == doc_json["id"])
                    .order_by(KbChunk.seq)
                )
            )
            .scalars()
            .all()
        )
        assert len(db_chunks) == len(expected)
        assert all(c.embedding is None for c in db_chunks)
        assert [c.content for c in db_chunks] == [c.content for c in expected]

        # parse 成功后已自动创建 embed job（仍 pending）
        embed_pending = (
            await db_session.execute(
                select(func.count())
                .select_from(KbIngestJob)
                .where(
                    KbIngestJob.document_id == doc_json["id"],
                    KbIngestJob.job_type == "embed",
                    KbIngestJob.status == KB_JOB_STATUS_PENDING,
                )
            )
        ).scalar_one()
        assert embed_pending == 1


@pytest.mark.asyncio
async def test_parse_failure_retry_and_progress(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)
    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "失败重试库"})
    ).json()["id"]

    # 上传损坏 PDF
    bad = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[
            (
                "files",
                ("坏的.pdf", _load("corrupted.pdf"), "application/pdf"),
            )
        ],
    )
    assert bad.status_code == 201
    doc_id = bad.json()[0]["id"]

    job = await kb_ingest_service.process_one(db_session, max_concurrent=10)
    assert job is not None
    assert job.status == "failed"

    detail = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == KB_DOC_STATUS_FAILED
    assert detail.json()["error_message"]
    assert "??" not in (detail.json()["error_message"] or "")

    # 非 failed 不可 retry：先上传一个成功文档验证
    ok_upload = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("ok.txt", _load("sample.txt"), "text/plain"))],
    )
    ok_id = ok_upload.json()[0]["id"]
    await kb_ingest_service.process_one(db_session, max_concurrent=10)
    deny = await user_client.post(f"/kb/documents/{ok_id}/retry", headers=headers)
    assert deny.status_code == 400

    # 把损坏文件替换为可读内容后 retry（直接改存储）
    doc = (
        await db_session.execute(select(KbDocument).where(KbDocument.id == doc_id))
    ).scalar_one()
    kb_ingest_service.storage.save(_load("sample.pdf"), key=doc.storage_path)
    # 类型仍是 pdf，内容已换成可解析 PDF
    retried = await user_client.post(f"/kb/documents/{doc_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == KB_DOC_STATUS_PARSING
    assert retried.json()["error_message"] is None

    job2 = await kb_ingest_service.process_one(db_session, max_concurrent=10)
    assert job2 is not None
    assert job2.status == KB_JOB_STATUS_SUCCESS

    final = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert final.json()["status"] == KB_DOC_STATUS_PENDING_EMBEDDING
    assert final.json()["progress"] == 100


@pytest.mark.asyncio
async def test_idempotent_rerun_no_duplicate_chunks(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)
    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "幂等库"})
    ).json()["id"]

    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("sample.md", _load("sample.md"), "text/markdown"))],
    )
    doc_id = uploaded.json()[0]["id"]
    job = await kb_ingest_service.process_one(db_session, max_concurrent=10)
    assert job is not None and job.status == KB_JOB_STATUS_SUCCESS

    n1 = (
        await db_session.execute(
            select(func.count())
            .select_from(KbChunk)
            .where(KbChunk.document_id == doc_id)
        )
    ).scalar_one()
    assert n1 > 0

    # 模拟重复执行：重置文档状态并再次跑同一流水线逻辑
    doc = (
        await db_session.execute(select(KbDocument).where(KbDocument.id == doc_id))
    ).scalar_one()
    doc.status = KB_DOC_STATUS_UPLOADED
    await db_session.flush()

    new_job = await kb_ingest_service.create_parse_job(db_session, doc=doc)
    new_job.status = KB_JOB_STATUS_RUNNING
    await db_session.flush()
    await kb_ingest_service.run_ingest_job(db_session, job=new_job)
    assert new_job.status == KB_JOB_STATUS_SUCCESS

    n2 = (
        await db_session.execute(
            select(func.count())
            .select_from(KbChunk)
            .where(KbChunk.document_id == doc_id)
        )
    ).scalar_one()
    assert n2 == n1

    kb = (
        await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one()
    assert kb.chunk_count == n2


@pytest.mark.asyncio
async def test_user_concurrency_limit_queues_jobs(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    monkeypatch.setattr(settings, "kb_max_concurrent_docs_per_user", 2)
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)
    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "并发库"})
    ).json()["id"]

    files = [
        ("files", (f"d{i}.txt", f"文档内容{i}中文".encode("utf-8"), "text/plain"))
        for i in range(3)
    ]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents", headers=headers, files=files
    )
    assert uploaded.status_code == 201
    assert len(uploaded.json()) == 3

    # 领取 2 个并保持 running（不跑完），第三个应领不到
    j1 = await kb_ingest_service.claim_next_job(db_session, max_concurrent=2)
    j2 = await kb_ingest_service.claim_next_job(db_session, max_concurrent=2)
    assert j1 is not None and j2 is not None
    assert j1.status == KB_JOB_STATUS_RUNNING
    assert j2.status == KB_JOB_STATUS_RUNNING

    j3 = await kb_ingest_service.claim_next_job(db_session, max_concurrent=2)
    assert j3 is None

    pending = (
        await db_session.execute(
            select(func.count())
            .select_from(KbIngestJob)
            .where(
                KbIngestJob.kb_id == kb_id,
                KbIngestJob.status == KB_JOB_STATUS_PENDING,
            )
        )
    ).scalar_one()
    assert pending == 1


@pytest.mark.asyncio
async def test_skip_locked_two_sessions_no_double_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两独立连接并行领取时同一 job 只被消费一次（需真实 PostgreSQL）。"""
    import asyncio

    from app.db.models.user import User

    storage_root = tmp_path / "kb_files"
    monkeypatch.setattr(settings, "kb_storage_root", str(storage_root))
    get_file_storage.cache_clear()
    storage = LocalFileStorage(storage_root)
    kb_ingest_service.storage = storage

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    job_id = None
    doc_id = None
    kb_id = None
    user_id = None
    model_id = None

    try:
        async with factory() as session:
            async with session.begin():
                model = await _seed_embedding(session)
                model_id = model.id
                user = User(
                    email=f"skiplock-{uuid4().hex[:8]}@example.com",
                    password_hash="x",
                    nickname="SkipLock",
                    status=0,
                )
                session.add(user)
                await session.flush()
                user_id = user.id

                kb = KnowledgeBase(
                    owner_user_id=user.id,
                    name="锁测试",
                    embedding_model_id=model.id,
                    embedding_dim=settings.kb_embedding_dim,
                    chunk_size=500,
                    chunk_overlap=50,
                    separators=[],
                    status="active",
                )
                session.add(kb)
                await session.flush()
                kb_id = kb.id

                key = storage.save("并发锁测试中文".encode("utf-8"))
                doc = KbDocument(
                    kb_id=kb.id,
                    file_name="a.txt",
                    file_type="txt",
                    file_size_bytes=10,
                    storage_path=key,
                    chunk_size=500,
                    chunk_overlap=50,
                    separators=[],
                    status=KB_DOC_STATUS_UPLOADED,
                )
                session.add(doc)
                await session.flush()
                doc_id = doc.id
                job = await kb_ingest_service.create_parse_job(session, doc=doc)
                await session.flush()
                job_id = job.id

        async def _claim_once() -> KbIngestJob | None:
            async with factory() as s:
                async with s.begin():
                    return await kb_ingest_service.claim_next_job(
                        s, max_concurrent=10, batch_probe=10
                    )

        r1, r2 = await asyncio.gather(_claim_once(), _claim_once())
        claimed = [j for j in (r1, r2) if j is not None]
        assert len(claimed) == 1
        assert claimed[0].id == job_id
    finally:
        async with factory() as cleanup:
            async with cleanup.begin():
                if job_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM kb_ingest_jobs WHERE id = :id"),
                        {"id": job_id},
                    )
                if doc_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM kb_documents WHERE id = :id"),
                        {"id": doc_id},
                    )
                if kb_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM knowledge_bases WHERE id = :id"),
                        {"id": kb_id},
                    )
                if user_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM users WHERE id = :id"),
                        {"id": user_id},
                    )
                if model_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM billing_rules WHERE model_id = :id"),
                        {"id": model_id},
                    )
                    await cleanup.execute(
                        text("DELETE FROM models WHERE id = :id"),
                        {"id": model_id},
                    )
        await engine.dispose()
        get_file_storage.cache_clear()
