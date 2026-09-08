"""系统级配置模型（单例行）。"""

from sqlalchemy import BigInteger, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class KbGlobalSettings(Base, TimestampMixin):
    """知识库全局配置单例（id 固定为 1）；NULL 字段回退环境变量默认值。"""

    __tablename__ = "kb_global_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_kb_global_settings_singleton"),
        CheckConstraint(
            "max_file_size_bytes IS NULL OR max_file_size_bytes > 0",
            name="ck_kb_gs_max_file_size",
        ),
        CheckConstraint(
            "upload_batch_limit IS NULL OR upload_batch_limit > 0",
            name="ck_kb_gs_upload_batch",
        ),
        CheckConstraint(
            "embedding_batch_size IS NULL OR embedding_batch_size > 0",
            name="ck_kb_gs_embed_batch",
        ),
        CheckConstraint(
            "embedding_max_concurrent IS NULL OR embedding_max_concurrent > 0",
            name="ck_kb_gs_embed_concurrent",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upload_batch_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_batch_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_max_concurrent: Mapped[int | None] = mapped_column(Integer, nullable=True)
