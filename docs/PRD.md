# 大模型聚合平台（LLM Gateway / AI 中台）产品需求文档（PRD）

| 项目 | 内容 |
| --- | --- |
| 文档名称 | 大模型聚合平台 PRD |
| 版本 | v1.3 |
| 状态 | 待评审 |
| 编写日期 | 2026-06-24 |
| 修订日期 | 2026-08-16（新增赠送余额 / 双资金池，见 [D4–D7](./prd/risks.md#已确认的产品决策)） |
| 适用范围 | 产品、研发、测试、运营、商业化团队 |
| 文档结构 | 主索引 + 模块文档，见 [`docs/prd/`](./prd/) |

---

## 使用说明

- **按模块阅读**：产品/后台菜单对应下方「产品模块」一节，一模块一文。
- **权威来源**：产品需求以本索引下各子文档为准；工程落地见关联设计文档。
- **关联文档**：
  - [技术方案](./TECH_DESIGN.md)
  - [MVP 范围](./MVP_SCOPE.md)
  - [二期范围](./PHASE2_SCOPE.md)
  - [数据库落地设计](./DATABASE_DESIGN.md)
  - [API 契约](./API_CONTRACT.md)
  - [AI 编码任务拆分](./AI_TASK_BREAKDOWN.md)
  - [AI 编码任务拆分（二期）](./AI_TASK_BREAKDOWN_PHASE2.md)
  - [AI 编码任务拆分（知识库专项）](./AI_TASK_BREAKDOWN_KB.md)
  - [AI 编码任务拆分（赠送余额专项）](./AI_TASK_BREAKDOWN_GIFT.md)
  - [口径变更影响说明](./GIFT_CALIBER_CHANGE.md)
  - [验证计划](./VALIDATION_PLAN.md)
  - [编码与文案约定](./CODING_CONVENTIONS.md)（UTF-8 / 中文防损坏）

---

## 目录

### 一、基础

| 文档 | 说明 |
| --- | --- |
| [产品概述](./prd/overview.md) | 背景、定位、目标、用户与核心价值 |
| [名词术语表](./prd/glossary.md) | 统一术语定义 |
| [系统架构](./prd/architecture.md) | 技术选型、分层、数据流、适配器 |

### 二、产品模块（一模块一文）

> 与前台菜单 / 后台管理菜单对齐，迭代时按模块阅读即可。

| 模块 / 菜单 | 文档 | 说明 |
| --- | --- | --- |
| 渠道管理 | [channels.md](./prd/channels.md) | Provider、渠道、多 Key、健康检查与熔断 |
| 模型管理 | [models.md](./prd/models.md) | 模型别名、渠道映射、可见性 |
| 计费规则 | [billing.md](./prd/billing.md) | 定价维度、倍率、精度、示例与对账 |
| 用户与权限 | [users-permissions.md](./prd/users-permissions.md) | 账号、API Key、角色、模型授权矩阵 |
| 组织管理 | [organizations.md](./prd/organizations.md) | 组织、成员、额度与组织角色 |
| 钱包与流水 | [wallet.md](./prd/wallet.md) | 个人/组织钱包、赠送余额、预扣实扣、扣费主体 |
| 统一调用网关 | [gateway.md](./prd/gateway.md) | OpenAI 兼容入口、路由与故障转移 |
| 用户端（对话） | [chat.md](./prd/chat.md) | Chat、Key、个人钱包与用量 |
| 知识库 | [knowledge-base.md](./prd/knowledge-base.md) | 文件上传解析、分块调整、向量检索、Chat RAG 问答（规划中） |
| 管理后台 | [admin.md](./prd/admin.md) | 后台菜单、运营看板、审计 |

### 三、横切设计

| 文档 | 说明 |
| --- | --- |
| [核心业务流程](./prd/workflows.md) | 统一调用、预扣实扣、组织扣费时序 |
| [数据库设计](./prd/database.md) | ER 与核心表（落地详见 DATABASE_DESIGN） |
| [关键 API](./prd/api.md) | OpenAI 兼容与管理端示例（落地详见 API_CONTRACT） |
| [非功能性需求](./prd/nfr.md) | 性能、安全、可观测、扩展、容灾 |

### 四、规划与风险

| 文档 | 说明 |
| --- | --- |
| [MVP 与迭代规划](./prd/mvp-roadmap.md) | 一 / 二 / 三期（落地详见 MVP_SCOPE） |
| [风险与决策清单](./prd/risks.md) | 假设、已确认决策、主要风险 |

---

> 后续技术方案、DDL、适配器接口等以 `TECH_DESIGN.md` 及专项设计文档为准。
