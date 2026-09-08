# 系统架构

> 返回 [PRD 索引](../PRD.md)

---

## 技术选型与理由

| 层 | 选型 | 理由 |
| --- | --- | --- |
| Web 框架 | **FastAPI** | 原生 `async/await` 适配高并发 IO 密集（转发上游）；对 **SSE/streaming** 支持友好（`StreamingResponse`）；Pydantic 强类型校验；自动生成 OpenAPI 文档便于联调。对比：Django 偏重、同步为主，DRF 适合 CRUD 但流式弱；Flask 生态成熟但异步与类型校验需大量手工。**推荐 FastAPI**。 |
| ASGI 服务器 | Uvicorn + Gunicorn（多 worker） | 生产环境多进程 + 异步事件循环，充分利用多核。 |
| 数据库 | **PostgreSQL** | 事务能力强（计费/扣费需强一致）；JSONB 存灵活配置；丰富索引；`SELECT ... FOR UPDATE` 行锁满足钱包并发扣费。 |
| ORM | SQLAlchemy 2.0（async）+ Alembic | 异步 ORM + 迁移管理。 |
| 缓存/限流/分布式锁 | Redis | 渠道健康状态、限流计数、API Key 鉴权缓存、预扣冻结的分布式锁。 |
| 异步任务 | Celery / APScheduler | 健康检查、用量聚合报表、对账。 |
| HTTP 客户端 | httpx（async，连接池） | 转发上游请求，支持流式。 |
| 前端-后台 | **React + TypeScript + Ant Design Pro** | 中后台组件齐全，开发效率高。 |
| 前端-对话前台 | React + TS + SSE/fetch stream | 流式渲染对话。 |
| 可观测性 | Prometheus + Grafana + OpenTelemetry + 结构化日志 | 指标、链路、日志三位一体。 |

## 分层架构图

```mermaid
flowchart TB
    subgraph clientLayer [客户端层]
        sdk["OpenAI SDK / HTTP 客户端"]
        chatUI["对话前台 Web"]
        adminUI["管理后台 Web"]
    end

    subgraph gatewayLayer [网关接入层]
        lb["负载均衡 / Nginx"]
        apiGw["API 网关 (FastAPI)"]
        authMw["鉴权中间件 (API Key 校验)"]
        rateLimit["限流 / 配额中间件"]
    end

    subgraph coreLayer [核心服务层]
        unifiedApi["统一调用服务 (OpenAI 兼容)"]
        router["路由与负载均衡引擎"]
        adapter["协议适配器层 (Provider Adapters)"]
        billing["计费与扣费服务"]
        permission["权限与授权服务"]
        walletSvc["钱包服务"]
        adminSvc["管理服务 (渠道/模型/用户/角色)"]
    end

    subgraph dataLayer [数据与中间件层]
        pg[("PostgreSQL")]
        redis[("Redis")]
        mq["消息队列 / 任务"]
    end

    subgraph upstreamLayer [上游厂商]
        openai["OpenAI"]
        anthropic["Anthropic"]
        google["Google"]
        others["阿里 / 字节 / DeepSeek ..."]
    end

    sdk --> lb
    chatUI --> lb
    adminUI --> lb
    lb --> apiGw --> authMw --> rateLimit
    rateLimit --> unifiedApi
    rateLimit --> adminSvc
    unifiedApi --> permission
    unifiedApi --> billing
    unifiedApi --> router
    router --> adapter
    adapter --> openai
    adapter --> anthropic
    adapter --> google
    adapter --> others
    billing --> walletSvc
    permission --> pg
    walletSvc --> pg
    billing --> pg
    adminSvc --> pg
    router --> redis
    authMw --> redis
    rateLimit --> redis
    billing --> mq
    mq --> pg
```

## 核心数据流（统一调用，简述）

```mermaid
flowchart LR
    a["请求进入"] --> b["API Key 鉴权"]
    b --> c["解析模型别名 + 权限校验"]
    c --> d["计费预扣 (冻结额度)"]
    d --> e["路由选渠道+Key"]
    e --> f["适配器转换并转发上游"]
    f --> g["流式/非流式返回用户"]
    g --> h["统计实际用量"]
    h --> i["实扣结算 + 释放冻结"]
    i --> j["写用量日志/流水"]
```

## 厂商接入可扩展性（适配器插件化）

每个 Provider 实现统一适配器接口（如 `BaseAdapter`），包含：请求转换、响应/流式转换、错误码归一、用量解析。新增厂商只需新增一个适配器实现并在数据库注册 Provider 类型，无需改动核心路由与计费逻辑。

```mermaid
classDiagram
    class BaseAdapter {
        +build_request(std_req) provider_req
        +parse_response(provider_resp) std_resp
        +parse_stream(chunk) std_chunk
        +parse_usage(resp) Usage
        +normalize_error(err) StdError
    }
    BaseAdapter <|-- OpenAIAdapter
    BaseAdapter <|-- AnthropicAdapter
    BaseAdapter <|-- AliyunAdapter
    BaseAdapter <|-- DeepSeekAdapter
    BaseAdapter <|-- GeminiAdapter
```
