"""Prometheus 基础指标与 /metrics 暴露。

标签 cardinality 约束：path 必须先经 ``normalize_path`` 把 UUID/长数字折成 ``{id}``，
否则每个 request_id 一条时间序列会打爆 Prometheus。

业务计数器按域分组：HTTP、chat/embedding、钱包 hold/capture、赠金、支付对账、知识库。
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# HTTP 层
HTTP_REQUESTS = Counter(
    "tokenhub_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
HTTP_LATENCY = Histogram(
    "tokenhub_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# 业务层（chat / 计费）
CHAT_REQUESTS = Counter(
    "tokenhub_chat_requests_total",
    "Chat completion requests",
    ["model", "status", "stream"],
)
CHAT_LATENCY = Histogram(
    "tokenhub_chat_request_duration_seconds",
    "Chat completion latency in seconds",
    ["model", "stream"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)
EMBEDDING_REQUESTS = Counter(
    "tokenhub_embedding_requests_total",
    "Embedding requests",
    ["model", "status"],
)
EMBEDDING_LATENCY = Histogram(
    "tokenhub_embedding_request_duration_seconds",
    "Embedding latency in seconds",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)
BILLING_OPS = Counter(
    "tokenhub_billing_ops_total",
    "Wallet billing operations",
    ["op", "result"],
)
HOLD_AMOUNT = Counter(
    "tokenhub_hold_amount_total",
    "Total hold amount (money units)",
    ["model"],
)
CAPTURE_AMOUNT = Counter(
    "tokenhub_capture_amount_total",
    "Total capture amount (money units)",
    ["model"],
)
RECONCILE_OPS = Counter(
    "tokenhub_hold_reconcile_total",
    "Hold reconcile task outcomes",
    ["action"],
)
GIFT_OPS = Counter(
    "tokenhub_gift_ops_total",
    "Gift ledger operations",
    ["op", "result"],
)
GIFT_BALANCE_TOTAL = Gauge(
    "tokenhub_gift_balance_total",
    "Platform-wide unconsumed gift credit (gift_balance + gift_held)",
)
PAYMENT_RECONCILE_OPS = Counter(
    "tokenhub_payment_reconcile_total",
    "Payment order reconcile task outcomes",
    ["action"],
)
KB_INGEST_OPS = Counter(
    "tokenhub_kb_ingest_total",
    "Knowledge base ingest worker outcomes",
    ["action"],
)
# 文档处理时长（按 job 类型与结果）；失败率 = failed / (success+failed)
KB_DOC_PROCESS_DURATION = Histogram(
    "tokenhub_kb_doc_process_duration_seconds",
    "Knowledge base document ingest job duration in seconds",
    ["job_type", "status"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)
KB_DOC_PROCESS_OPS = Counter(
    "tokenhub_kb_doc_process_total",
    "Knowledge base document ingest job outcomes (for failure rate)",
    ["job_type", "status"],
)
# 向量化 token 吞吐（kb_ingest / kb_query）
KB_EMBED_TOKENS = Counter(
    "tokenhub_kb_embed_tokens_total",
    "Knowledge base embedding input tokens",
    ["source"],
)
# 检索延迟
KB_RETRIEVAL_LATENCY = Histogram(
    "tokenhub_kb_retrieval_duration_seconds",
    "Knowledge base retrieval latency in seconds",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
# RAG 问答命中率：有命中的问答占比 = hit / (hit+miss)
KB_RAG_HIT = Counter(
    "tokenhub_kb_rag_hit_total",
    "RAG chat retrieval hit outcomes",
    ["result"],
)


def metrics_payload() -> tuple[bytes, str]:
    """返回 Prometheus 文本格式正文与 Content-Type。"""
    return generate_latest(), CONTENT_TYPE_LATEST


def normalize_path(path: str) -> str:
    """降低 cardinality：将 UUID / 长数字段折叠为占位符。"""
    parts: list[str] = []
    for part in path.split("/"):
        if not part:
            continue
        if _looks_like_id(part):
            parts.append("{id}")
        else:
            parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def _looks_like_id(value: str) -> bool:
    """路径段是否像资源 ID（UUID 或超长数字），用于降低 metrics 标签基数。"""
    if len(value) >= 32 and all(c in "0123456789abcdef-" for c in value.lower()):
        return True
    if value.isdigit() and len(value) > 6:
        return True
    return False
