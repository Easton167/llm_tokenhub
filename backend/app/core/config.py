"""应用配置：从 .env 与环境变量加载，进程内单例缓存。

加载顺序：
  1. 模块 import 时 ``load_dotenv(backend/.env)``，把拆分的 postgresql_* / redis_*
     拼成默认连接串（方便本地开发填分项，不必写完整 URL）。
  2. ``Settings`` 再读环境变量；``DATABASE_URL`` / ``REDIS_URL`` 若存在则覆盖默认串。
  3. ``get_settings()`` 用 ``lru_cache`` 做成进程单例；测试可 ``cache_clear()``。

生产必须替换的密钥：``api_key_pepper``、``channel_key_encryption_key``、``jwt_secret``。
金额：``money_scale`` 定义 1 元对应多少最小单位，钱包/计费全部用整数。
"""

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import os
from dotenv import load_dotenv
import pathlib

# 优先加载 backend/.env，供下方默认连接串拼接使用
load_dotenv(pathlib.Path(__file__).parent.parent.parent / ".env")
postgresql_url = (
    f"postgresql+asyncpg://{os.getenv('postgresql_user')}:"
    f"{os.getenv('postgresql_password')}@{os.getenv('postgresql_host')}:"
    f"{os.getenv('postgresql_port')}/{os.getenv('postgresql_database')}"
)
redis_url = f"redis://{os.getenv('redis_user')}:{os.getenv('redis_password')}@{os.getenv('redis_host')}:{os.getenv('redis_port')}/0"


class Settings(BaseSettings):
    """运行时配置项；可通过 DATABASE_URL / REDIS_URL 等环境变量覆盖默认值。

    ``extra="ignore"``：.env 里允许有本类未声明的键（例如拆分的 postgresql_user），
    避免 pydantic 因未知字段报错。
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "LLM TokenHub"
    debug: bool = False

    database_url: str = Field(
        default=postgresql_url,
        validation_alias="DATABASE_URL",
    )
    redis_url: str = Field(
        default=redis_url,
        validation_alias="REDIS_URL",
    )

    # 平台 API Key 哈希盐；上游渠道 Key 的 AES 加密密钥（生产环境必须替换）
    api_key_pepper: str = Field(default="change-me-in-production", validation_alias="API_KEY_PEPPER")
    channel_key_encryption_key: str = Field(
        default="change-me-in-production-32bytes!!",
        validation_alias="CHANNEL_KEY_ENCRYPTION_KEY",
    )

    default_request_timeout_ms: int = Field(default=120_000, validation_alias="DEFAULT_REQUEST_TIMEOUT_MS")
    default_max_retries: int = Field(default=2, validation_alias="DEFAULT_MAX_RETRIES")
    route_cache_ttl_seconds: int = Field(default=60, validation_alias="ROUTE_CACHE_TTL_SECONDS")
    auth_cache_ttl_seconds: int = Field(default=300, validation_alias="AUTH_CACHE_TTL_SECONDS")
    # 预扣款（hold）超时后自动释放的秒数
    hold_expire_seconds: int = Field(default=3600, validation_alias="HOLD_EXPIRE_SECONDS")
    # 金额整数化倍数：1 元 = money_scale 最小单位（默认 1 元 = 10000，即 0.0001 元精度）
    money_scale: int = Field(default=10_000, validation_alias="MONEY_SCALE")
    # 业务时区：赠金到期日取该时区当日 23:59:59 再转 UTC，避免跨时区提前失效
    business_timezone: str = Field(default="Asia/Shanghai", validation_alias="BUSINESS_TIMEZONE")
    # 单笔赠送上限（最小货币单位）；默认 1 万元
    gift_max_amount_per_grant: int = Field(
        default=100_000_000, validation_alias="GIFT_MAX_AMOUNT_PER_GRANT"
    )
    # 单操作人当日累计赠送上限（最小货币单位，按业务时区自然日）；默认 10 万元
    gift_max_amount_per_operator_daily: int = Field(
        default=1_000_000_000, validation_alias="GIFT_MAX_AMOUNT_PER_OPERATOR_DAILY"
    )
    # 支付单默认过期分钟数（created 超时后回调不入账）
    payment_order_expire_minutes: int = Field(
        default=30, validation_alias="PAYMENT_ORDER_EXPIRE_MINUTES"
    )
    # Mock 支付渠道验签密钥（仅测试/本地）
    mock_payment_secret: str = Field(
        default="mock-payment-secret", validation_alias="MOCK_PAYMENT_SECRET"
    )
    # Stripe 沙箱/生产密钥（不落库；空字符串表示未配置）
    stripe_secret_key: str = Field(default="", validation_alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str = Field(
        default="", validation_alias="STRIPE_WEBHOOK_SECRET"
    )
    # 对外可访问的 API 根（用于拼装 Stripe webhook notify_url，如 https://api.example.com）
    payment_public_base_url: str = Field(
        default="http://localhost:8000", validation_alias="PAYMENT_PUBLIC_BASE_URL"
    )
    # 支付对账：扫描 created 且 created_at 早于 now-该秒数 的订单（掉单补查）
    payment_reconcile_stale_seconds: int = Field(
        default=300, validation_alias="PAYMENT_RECONCILE_STALE_SECONDS"
    )

    # 前台用户会话 JWT（与平台 API Key 分离）
    jwt_secret: str = Field(default="change-me-jwt-secret-in-production", validation_alias="JWT_SECRET")
    jwt_expire_minutes: int = Field(default=60 * 24, validation_alias="JWT_EXPIRE_MINUTES")
    # 管理端固定账号（环境变量配置；与用户注册体系分离）
    admin_username: str = Field(default="admin", validation_alias="ADMIN_USERNAME")
    admin_password: str = Field(default="admin123", validation_alias="ADMIN_PASSWORD")
    # 前端 CORS 来源（逗号分隔）；空字符串表示不启用 CORS 中间件
    cors_origins: str = Field(default="http://localhost:8088", validation_alias="CORS_ORIGINS")

    # 知识库向量列维度：pgvector HNSW 索引要求固定维度，首期全平台 embedding 维度统一；
    # 修改后需重建 kb_chunks 向量列与索引（见 docs/DATABASE_DESIGN.md 7.3）
    kb_embedding_dim: int = Field(default=1536, validation_alias="KB_EMBEDDING_DIM")
    # 原文件本地存储根目录（LocalFileStorage）；生产可改为绝对路径
    kb_storage_root: str = Field(default="./data/kb_files", validation_alias="KB_STORAGE_ROOT")
    # 系统默认 embedding 模型 alias（创建知识库时使用；须 model_type=embedding 且已配置计费）
    kb_default_embedding_model: str = Field(
        default="", validation_alias="KB_DEFAULT_EMBEDDING_MODEL"
    )
    # 角色配额字段为 NULL 时的系统默认值（见 docs/prd/knowledge-base.md「计费与配额」）
    kb_default_max_kb_count: int = Field(default=5, validation_alias="KB_DEFAULT_MAX_KB_COUNT")
    kb_default_max_docs_per_kb: int = Field(
        default=100, validation_alias="KB_DEFAULT_MAX_DOCS_PER_KB"
    )
    kb_default_max_storage_bytes: int = Field(
        default=1 * 1024**3, validation_alias="KB_DEFAULT_MAX_STORAGE_BYTES"
    )
    kb_default_max_file_size_bytes: int = Field(
        default=20 * 1024**2, validation_alias="KB_DEFAULT_MAX_FILE_SIZE_BYTES"
    )
    # 单次批量上传文件数上限
    kb_upload_batch_limit: int = Field(default=10, validation_alias="KB_UPLOAD_BATCH_LIMIT")
    # 同一用户同时处理中的文档数上限（parsing/chunking/embedding）
    kb_max_concurrent_docs_per_user: int = Field(
        default=2, validation_alias="KB_MAX_CONCURRENT_DOCS_PER_USER"
    )
    # ingest worker 空闲轮询间隔（秒）
    kb_worker_poll_interval_seconds: float = Field(
        default=2.0, validation_alias="KB_WORKER_POLL_INTERVAL_SECONDS"
    )
    # worker 每轮探测/处理的 pending job 上限
    kb_worker_batch_size: int = Field(default=10, validation_alias="KB_WORKER_BATCH_SIZE")
    # job 失败后允许的最大重试次数（API retry 重建 job；计数写入 retry_count）
    kb_max_retry: int = Field(default=3, validation_alias="KB_MAX_RETRY")
    # 向量化每批合并的分块条数（内部 embedding 批量请求）
    kb_embedding_batch_size: int = Field(
        default=16, validation_alias="KB_EMBEDDING_BATCH_SIZE"
    )
    # 平台级内部向量化上游并发上限（低优先级，避免与在线 chat 争抢渠道 RPM/TPM）
    kb_embedding_max_concurrent: int = Field(
        default=2, validation_alias="KB_EMBEDDING_MAX_CONCURRENT"
    )


@lru_cache
def get_settings() -> Settings:
    """解析并缓存 Settings。进程内只构建一次，避免重复读盘与校验。"""
    return Settings()


# 业务代码统一 ``from app.core.config import settings``，不要每次 new Settings()
settings = get_settings()
