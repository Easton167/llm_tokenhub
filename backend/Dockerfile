# LLM TokenHub 后端镜像。API 与 kb_worker 共用同一镜像，靠启动命令区分。
FROM python:3.11-slim

# PYTHONPATH=/app：让 app 包从工作目录解析而非 site-packages，
# 否则 config.py 的 load_dotenv 会去 site-packages 找 .env，挂载的 /app/.env 失效。
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY alembic ./alembic

RUN pip install --no-cache-dir . \
    && useradd -r -u 10001 appuser \
    && mkdir -p /app/data/kb_files \
    && chown -R appuser /app/data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
