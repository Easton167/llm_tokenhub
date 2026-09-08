# 阶段 8 实施细则：知识库与 RAG——文件流水线 + 向量检索

> 配套 `docs/learning_plan.md` 的「阶段 8」。阅读模式字母见 [README](./README.md#二四种读代码的方式后面每条读单都会标)。
> 前置：阶段 4（网关链路）、阶段 6（双资金池）、阶段 7（后台任务）都已过关。**知识库把这三块全部复用了一遍**，前面没学扎实这里会很痛苦。
> 产出物：`docs/learning/notes/stage-8.md`，含 1 张手画状态机 + 1 份 SQL 取证记录 + 2 份故障演练记录 + 1 份断点向量化轨迹。

**本阶段一句话**：这是一次**综合复习**。适配器、hold/capture/release、双资金池、后台任务、幂等——前七个阶段学的东西在这条链路上全部再走一遍，只是换了个业务场景。所以读的时候要不断问自己：「这一段我在哪儿见过？」

---

## 零、先建立整体地图（15 分钟，读完再动代码）

### 0.1 一份文件的一生

```
上传          →  解析      →  分块        →  向量化              →  可检索
uploaded      →  parsing   →  chunking    →  pending_embedding   →  ready
（存文件）       （提文本）    （切片）       →  embedding
                                            （花钱调 embedding）
```

三个岔路：

```
余额不够   →  insufficient_balance   （充值后可 retry）
部分失败   →  partial                （retry 只重试失败的块）
彻底失败   →  failed                 （可 retry）
```

九个状态的完整清单在 `app/db/models/kb.py:85-88` 的 CheckConstraint 里。

### 0.2 三个「其实你已经学过」

| 知识库里的东西 | 你在哪学过 |
| --- | --- |
| 向量化调用上游 embedding 模型 | 阶段 4：路由 → 适配器 → 转发 |
| 向量化要先预扣再实扣，赠金优先 | 阶段 5 + 6：hold / capture / release + 双池 |
| worker 消费 job 队列、幂等键、重试 | 阶段 7：后台任务（这里更进一步，多了 `SKIP LOCKED`） |

**唯一真正全新的东西只有两个**：文件解析分块（纯逻辑，不碰数据库）和向量检索（pgvector）。

### 0.3 先读 PRD

`docs/prd/knowledge-base.md` 精读三节：功能设计、**文档处理状态机**、数据库概览。
`docs/AI_TASK_BREAKDOWN_KB.md` 第 16 节：「为什么向量化计费必须独立成任务」——这一节回答了本阶段最重要的架构问题。

---

## 一、怎么读

### 读单总览

| # | 内容 | 模式 | 预算 | 核心问题 |
| --- | --- | --- | --- | --- |
| D1 | PRD + 任务拆解 | Q | 50 min | 设计意图 |
| D2 | 五个测试文件 | T | 70 min | 行为契约 |
| D3 | `models/kb.py` + 迁移 009 | Q | 50 min | 四张表 + pgvector |
| D4 | `app/storage/` | S+Q | 25 min | 文件存哪、路径怎么定 |
| D5 | `kb_service.py` + 文件校验 + `api/kb.py` | Q | 60 min | 归属隔离、配额、类型嗅探 |
| D6 | `app/kb/`（纯逻辑层） | Q | 60 min | 解析与分块 |
| D7 | `kb_ingest_service.py` + `kb_worker.py` | **Q** | **90 min** | **状态机 + 队列，本阶段核心** |
| D8 | `embedding_gateway.py` | Q | 50 min | 内部调用方怎么复用网关 |
| D9 | `retrieval_service.py` + RAG 编排 | Q | 50 min | 检索与问答 |

---

### D1 · 设计意图（模式 Q，50 分钟）

带着三个问题读 PRD 和任务拆解：

1. **为什么向量化计费必须独立成一个 job，不能在上传接口里同步做？**
   （`docs/AI_TASK_BREAKDOWN_KB.md` 第 16 节。提示：一份 100 页 PDF 切出 800 个块，调 embedding 要几十秒到几分钟。HTTP 请求扛不住；而且中途失败了，用户的钱怎么办？）
2. **为什么向量化要走内部网关，而不是直接 httpx 调上游？**
   （提示：直连就意味着重新实现一遍路由、重试、计费、用量记录。**复用 = 一处修好处处生效**）
3. **文档状态机为什么要有 `pending_embedding` 和 `insufficient_balance` 两个独立状态？**
   （提示：一个是「等着被处理」，一个是「处理不下去了但不是你的错」。合并成 `failed` 会怎样？用户根本不知道该充值还是该重传）

---

### D2 · 先读测试（模式 T，70 分钟）

五个文件，按这个顺序读，每个只看函数名 + 关键 assert。

先跑一遍确认环境：

```powershell
cd d:\LLM_TokenHub\backend
pytest tests/test_kb_parse_chunk.py tests/test_kb_api.py -q
```

#### `tests/test_kb_parse_chunk.py`（纯逻辑，最好懂，从这里入门）

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_parse_md_heading_hierarchy` | `55-66` | 标题层级会被保留成 `heading_path` |
| `test_scanned_pdf_raises_unparsable` | `134-138` | 扫描件（无文本层）抛 `unparsable_document` |
| `test_chunk_deterministic` | `167-173` | **同输入同参数，结果必须一致** |
| `test_chunk_token_not_exceed_size` | `176-196` | 每块 token ≤ chunk_size |
| `test_chunk_overlap_shared_suffix_prefix` | `198-222` | 相邻块共享重叠区 |
| `test_chunk_param_boundaries` | `233-247` | 参数边界 |

`test_chunk_deterministic` 值得单独想：**为什么分块必须是确定性的？**（重新分块时，如果结果每次都不同，用户会发现自己手工编辑过的块莫名其妙变了；而且没法做增量对比）

#### `tests/test_kb_api.py`（接口与隔离）

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_sniff_rejects_exe_renamed_as_pdf` | `150-153` | **exe 改名成 pdf 会被识破** |
| `test_kb_crud_and_isolation` | `181-228` | A 的库 B 访问 403 |
| `test_reject_spoofed_extension_no_side_effects` | `360-394` | **拒绝时不留任何 DB 记录和文件** |
| `test_quota_kb_count_exceeded` | `398-413` | 库数量配额 |
| `test_quota_file_size_and_docs_no_write` | `417-469` | **配额拒绝时不写库** |

「拒绝时不留痕迹」这条在两个测试里被反复断言。想想为什么这件事值得写两条测试守着——（校验失败但文件已经落盘 = 存储泄漏；DB 有孤儿记录 = 配额被白白占用）

#### `tests/test_kb_ingest.py`（状态机与队列，**本阶段最重要**）

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_illegal_transition_rejected` | `151-172` | **非法状态流转直接抛异常** |
| `test_six_sample_types_reach_pending_embedding` | `200-286` | 6 种文件类型都能走通 |
| `test_parse_failure_retry_and_progress` | `290-352` | failed → retry → 继续走 |
| `test_idempotent_rerun_no_duplicate_chunks` | `356-412` | **重跑不产生重复分块** |
| `test_user_concurrency_limit_queues_jobs` | `416-458` | 单用户并发上限 |
| `test_skip_locked_two_sessions_no_double_claim` | `462-542` | **两个 worker 不会抢到同一个 job** |

最后两条是本阶段的技术核心，D7 会详细讲。

#### `tests/test_kb_embed.py`（计费，阶段 6 的复习）

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_embed_success_ready_usage_and_wallet` | `268-414` | 成功链路：ready + `source=kb_ingest` + hold/capture |
| `test_insufficient_balance_no_charge_then_retry` | `418-482` | **余额不足零扣费，充值后 retry 能继续** |
| `test_partial_retry_only_failed_chunks` | `486-623` | partial 重试只补失败的块 |
| `test_repeat_embed_idempotent_no_double_charge` | `627-699` | **重复 embed 不二次扣费** |
| `test_all_embed_fail_releases_hold` | `776-827` | 全失败 → release，余额不变 |

**这五条和阶段 4 `test_openai_api.py` 里的计费用例是同构的**。对着看一遍，你会发现是同一套模式换了个场景。

#### `tests/test_kb_chunks.py`（分块工作台）

扫读即可，重点两条：`test_edit_chunk_reembed_billing_and_hit`（`379`，编辑分块会**触发重新向量化并扣费**）、`test_rechunk_requires_confirm_and_clears_manual_edits`（`668`，重新分块要二次确认，会清掉人工编辑）。

---

### D3 · 四张表与 pgvector（模式 Q，50 分钟）

#### 3.1 表结构（`app/db/models/kb.py`）

| 表 | 类 | 行号 | 一句话 |
| --- | --- | --- | --- |
| `knowledge_bases` | `KnowledgeBase` | `34-77` | 一个库：归属、embedding 模型、分块默认参数、检索默认参数 |
| `kb_documents` | `KbDocument` | `80-117` | 一份文档：文件信息、状态、分块参数**快照** |
| `kb_chunks` | `KbChunk` | `120-164` | 一个分块：内容、向量、启用状态 |
| `kb_ingest_jobs` | `KbIngestJob` | `167-204` | 一个处理任务：类型、状态、幂等键 |

三个设计细节值得单独想：

1. **`kb_documents` 存了分块参数快照**（`:104-106`），而库上也有一份默认值（`:61-63`）。为什么要冗余？
   （提示：用户改了库的默认 chunk_size，已经处理过的文档**不应该**跟着变。快照 = 冻结当时的处理口径）
2. **`kb_chunks` 有 `is_manually_edited`**（`:158`）。它存在的意义是什么？
   （rechunk 时要提醒用户「你的人工编辑会丢」——`test_kb_chunks.py:668` 那条测试）
3. **`kb_ingest_jobs.idempotency_key` 是全表 unique**（`:200`），不是 `(document_id, key)` 组合唯一。想想为什么可以这么做。（因为 key 本身已经包含了 doc_id，见 D7.3）

#### 3.2 向量列与 HNSW 索引（`:159-161`、`:135-140`）

```python
embedding = mapped_column(Vector(settings.kb_embedding_dim))   # 默认 1536，config.py:99

Index("ix_kb_chunks_embedding_hnsw", "embedding",
      postgresql_using="hnsw",
      postgresql_ops={"embedding": "vector_cosine_ops"})
```

需要补的概念（够用就行，别陷进去）：

- **embedding**：把一段文本变成一个 1536 维的浮点数向量。语义相近的文本，向量方向也相近。
- **cosine 距离**：衡量两个向量方向差异，0 = 完全同向，2 = 完全反向。`score = 1 - distance`。
- **HNSW**：一种近似最近邻索引。**「近似」两个字很重要**——它不保证找到绝对 top-k，用一点准确率换几个数量级的速度。没有它，10 万个块的检索要全表扫描算 10 万次距离。

问题：为什么用 cosine 而不是欧氏距离？（提示：embedding 关心的是**语义方向**，不是向量长度。一段话重复三遍，向量会变长但语义没变）

#### 3.3 迁移 `alembic/versions/009_kb_tables_pgvector.py`

| 段 | 行号 | 内容 |
| --- | --- | --- |
| **启用扩展** | `33` | `CREATE EXTENSION IF NOT EXISTS vector` |
| 建四张表 | `35-211` | 注意 `:148` 的向量列、`:163-169` 的 HNSW 索引 |
| 扩展既有表 | `214-228` | `request_logs.source` / `usage_logs.source` / roles 四列配额 |
| downgrade | `231-245` | 按依赖倒序删表；**不删 vector 扩展**（`:245`） |

两个值得学的点：

1. **`:214-221` 给既有的 `request_logs` / `usage_logs` 加了 `source` 列，默认 `'api'`**。这就是阶段 7 D3 里 `test_admin_request_logs_allow_null_api_key_id` 那条测试的来源——知识库引入了 `source=kb_ingest` 这种没有 API Key 的用量记录。**一个新模块给旧表加了一个维度，旧接口就得跟着适配**。
2. **downgrade 不删 vector 扩展**（`:245`，注释说明保留无副作用）。为什么？（扩展可能被其他东西用着；而且删扩展会级联删掉所有 vector 类型的列。**回滚时宁可留下无害的残留，也不要做不可逆的破坏**）

---

### D4 · 文件存储抽象（模式 S + Q，25 分钟）

`app/storage/base.py` 四个方法，扫一眼就够：

```python
save(data: bytes, *, key: str | None = None) -> str   # :13-14
open(key: str) -> BinaryIO                            # :17-18
delete(key: str) -> None                              # :21-22   幂等
exists(key: str) -> bool                              # :25-26
```

**这四个方法的签名是照着 S3 设计的**，本地实现只是当前的一种。想想：如果哪天换成 S3，业务代码要改吗？（不用——这就是抽象层存在的意义，和阶段 4 的适配器、阶段 5 的支付抽象是同一个套路。**本项目第三次出现这个模式了**）

`local.py` 只精读一个函数：

```python
def generate_key():                                   # :28-31
    token = secrets.token_hex(16)
    return f"{token[:2]}/{token[2:4]}/{token}"
```

两个设计点：

1. **用随机标识而不是原文件名**。为什么？（三个理由：文件名会重复；文件名可能含路径穿越字符 `../`；**文件名本身可能泄漏隐私**，比如「2026年离职名单.xlsx」）
2. **两级子目录分片**。为什么不全扔在一个目录里？（单目录几十万文件时，文件系统的目录项查找会显著变慢，`ls` 直接卡死）

还有一个安全细节：`:19-26` 拒绝 `..` 路径穿越。**这是信任边界上的输入校验**，不能省。

---

### D5 · API 层：归属、配额、类型嗅探（模式 Q，60 分钟）

#### 5.1 路由表（`app/api/kb.py`，S 模式扫一遍）

前缀 `/kb`，**全部用 JWT 鉴权**（`Depends(get_user_context)`），不是 API Key。

| Method | Path | 行号 | 说明 |
| --- | --- | --- | --- |
| GET / POST | `/kb` | `95-111` | 列表 / 创建 |
| GET / PATCH / DELETE | `/kb/{kb_id}` | `230-260` | 库的增删改查 |
| POST / GET | `/kb/{kb_id}/documents` | `263-298` | 上传 / 列表 |
| GET / DELETE | `/kb/documents/{doc_id}` | `114-141` | 文档详情（含进度）/ 删除 |
| POST | `/kb/documents/{doc_id}/retry` | `124-131` | **重试** |
| GET / POST | `/kb/documents/{doc_id}/chunks` | `144-181` | 分块列表 / 手工新增 |
| POST | `/kb/documents/{doc_id}/rechunk` | `184-194` | 重新分块 |
| PATCH / DELETE | `/kb/chunks/{chunk_id}` | `197-217` | 编辑 / 删除分块 |
| POST | `/kb/chunks/batch` | `220-227` | 批量启用/禁用/删除 |
| POST | `/kb/{kb_id}/hit-test` | `301-351` | **命中测试** |

问题：为什么知识库用 JWT 而 `/v1/chat/completions` 用 API Key？（回到阶段 3：API Key 是给**程序**调的网关，JWT 是给**人**用的控制台。知识库管理是控制台功能）

#### 5.2 归属隔离（`kb_service.py`）

三个 `require_*` 函数，是所有接口的第一道关：

| 函数 | 行号 | 校验什么 |
| --- | --- | --- |
| `require_owned_kb` | `175-183` | `owner_user_id == user_id`，否则 403 |
| `require_owned_document` | `185-199` | 文档 → 库 → owner |
| `require_owned_chunk` | `201-218` | 分块 → 文档 → 库 → owner |

**注意：知识库只有个人归属，没有组织维度**（`models/kb.py:35` 的注释明说）。这和钱包（个人 + 组织双主体）形成对比。

在笔记里想一下：如果要加组织知识库，需要改哪些地方？（这可能是你阶段 9 的选题）

#### 5.3 配额（`kb_service.py`）

| 场景 | 行号 | 错误码 |
| --- | --- | --- |
| 库数量 | `257-263` | `kb_quota_exceeded` |
| 单次上传批量 | `378-383` | `batch_too_large` |
| 单文件大小 | `395-400` | `file_too_large` |
| 单库文档数 | `405-410` | `kb_doc_quota_exceeded` |
| 用户总存储 | `412-417` | `kb_storage_quota_exceeded` |

配额来源 `resolve_quota_limits`（`:92-119`）：读角色配额，NULL 用系统默认，**多角色取各字段 max**。

问题：多角色为什么取 max 而不是 min？（角色是**授予**权限的，不是限制。一个用户同时是「普通用户」和「VIP」，应该享受 VIP 的额度）

#### 5.4 真实类型嗅探（`kb_file_validation.py`，本节重点）

**为什么不能只看扩展名**：把 `virus.exe` 改名成 `report.pdf` 上传，解析器会拿它当 PDF 处理。

`sniff_file_type`（`:56-81`）看的是**文件头的魔数**：

| 特征 | 判定 | 行号 |
| --- | --- | --- |
| `%PDF` | pdf | `61-62` |
| **`MZ`** | **None（直接拒绝，这是 Windows 可执行文件）** | `64-65` |
| `PK\x03\x04` | zip 家族 → 再进 `_sniff_ooxml` 区分 docx/xlsx | `66-67`、`43-53` |
| 能解码成文本 | 再看是不是 HTML | `69-74` |

最后 `detect_and_validate_file_type`（`:84-95`）做的事很简单但很关键：

```
claimed（扩展名说的） 必须 == sniffed（内容真的是的）
```

**不只是「嗅探出是什么就按什么处理」，而是「两者必须一致」**。想清楚区别：如果只按嗅探结果处理，用户传一个 `.txt` 结尾的 PDF 也会被接受——这不是安全问题，但会让「文件类型」这个字段变得不可信。

支持 7 种类型，清单在 `app/core/constants.py:139-158`。

---

### D6 · 纯逻辑层：解析与分块（模式 Q，60 分钟）

**这一层完全不碰数据库、不碰网络**，是整个项目里最容易单独测试的部分——所以 `test_kb_parse_chunk.py` 也是最好读的测试文件。

#### 6.1 解析（`app/kb/parse.py` + `parsers.py`）

统一入口是 `parse(file_bytes, file_type)`（`parse.py:35-59`），按类型分发到 7 个解析器：

| 类型 | 函数 | `parsers.py` 行号 |
| --- | --- | --- |
| txt / md | `parse_txt` / `parse_md` | `19-21` / `24-72` |
| html / csv | `parse_html` / `parse_csv` | `75-83` / `86-95` |
| xlsx / docx | `parse_xlsx` / `parse_docx` | `98-133` / `136-201` |
| pdf | `parse_pdf` | `204-252` |

解析结果不是一坨字符串，而是**结构化元素列表**（`types.py`）：

```python
ElementKind = heading | paragraph | table_row        # types.py:10-13
SourceLocation(page, heading_path, row_index, sheet) # types.py:17-35
```

**保留结构是后面分块能「结构优先」的前提**，也是检索结果能告诉用户「这段话来自第 12 页、三级标题 X 下面」的前提。

两个错误分支值得记：扫描件 PDF（无文本层）抛 `UnparsableDocumentError`（`parsers.py:250-251`），空内容抛 `EmptyDocumentError`（`parse.py:57-58`）。错误类型清单在 `errors.py:15-40`，四个，都带 `code`。

#### 6.2 分块（`chunking.py`，本节重点）

入口 `chunk(parsed_doc, chunk_size, chunk_overlap, separators)`（`:37-69`），docstring（`:45-48`）写了三层策略：

```
1. 结构单元（标题/段落/表格行）贪心合并       _elements_to_units + _pack_atoms  :90-173
2. 超长单元按 separators 二次切分             _split_text                       :238-266
3. 相邻块加 token 重叠                        _apply_overlap                    :176-235
```

**为什么要这三层，各自解决什么问题：**

| 层 | 解决的问题 |
| --- | --- |
| 结构优先 | 不要在一个段落中间切断。一句话被切成两半，两半都检索不出来 |
| 二次切分 | 但有些段落本身就超长（比如一整章没分段），必须硬切 |
| 重叠 | 切口附近的语义会被切断，重叠让切口内容在**两个块里都出现一次** |

**结构优先最漂亮的一处**是标题处理（`:118-121`）：标题不单独成块，而是作为前缀**并入后面的正文**：

```
"第三章 计费规则 / 3.1 预扣" + 正文内容
```

想清楚为什么——（一个只有标题的块，检索出来毫无信息量；而正文块带上标题上下文，语义完整度大幅提升）

参数边界（`:15-18`、`:72-87`）：

```
chunk_size    ∈ [100, 2000]
chunk_overlap ∈ [0, 200]  且必须 < chunk_size
```

这三条约束在**数据库层也有一份**（`models/kb.py:40-46`）。想想为什么要重复校验。（应用层给用户友好报错，数据库层防止绕过应用层的写入。**和阶段 5 「行锁 + 应用检查 + CheckConstraint」三重防护是同一个思路**）

默认分隔符（`:21-34`）值得看一眼：`"\n\n"` → `"\n"` → `"。！？；"` → `". ! ? ; "` → `" "` → `""`。**中文标点排在英文前面**，最后的 `""` 是兜底硬切。这是给中文场景做过调优的证据。

---

### D7 · 状态机与队列：本阶段核心（模式 Q，90 分钟）

`app/services/kb_ingest_service.py` 是本阶段最长也最重要的文件。**不要从头读到尾**，按下面五块切开读。

#### 7.1 状态机（`:62-139`）

允许的流转写在一张表里 `_ALLOWED_DOC_TRANSITIONS`（`:62-94`），所有流转都必须走 `transition_document`（`:121-139`）。

**把「合法流转」写成数据而不是散落在 if-else 里，是状态机的正确写法。** 好处：

- 非法流转会立刻抛异常（`test_kb_ingest.py:151-172` 守着这条）
- 新增状态时只改一张表
- 这张表本身就是文档

在笔记里**手画状态机图**（这是本阶段第一个必做产出）。至少包含：

```
uploaded → parsing → chunking → pending_embedding → embedding → ready
                                       ↓                  ↓
                            insufficient_balance      partial / failed
              failed ←──────（任一环节异常）
ready → parsing （rechunk，:93）
```

#### 7.2 job 领取：`SKIP LOCKED`（`claim_next_job`，`:348-407`）

**核心就在 `:373` 一行**：`FOR UPDATE SKIP LOCKED`。

先理解问题：三个 worker 实例同时轮询 `kb_ingest_jobs` 表找 pending 的任务。不加保护的话，三个都会拿到同一条，同一份文档被处理三遍。

三种方案对比：

| 方案 | 问题 |
| --- | --- |
| 什么都不做 | 重复处理 |
| `FOR UPDATE`（不加 SKIP LOCKED） | 正确，但 worker B、C 会**阻塞等待** worker A 提交。三个 worker 变成串行 |
| **`FOR UPDATE SKIP LOCKED`** | 被别人锁住的行**直接跳过**，去拿下一条。三个 worker 各拿各的，真并行 |

**这是「用 Postgres 当任务队列」的标准姿势**，值得单独记进笔记。它让你在不引入 Redis/RabbitMQ/Celery 的情况下拿到一个够用的队列——非常符合「能不加依赖就不加」的原则。

`test_kb_ingest.py:462-542` 用两个数据库连接真实验证了这件事：两个 session 同时 claim，只有一个成功。**这条测试的写法本身也值得学**（怎么在测试里构造真实并发）。

#### 7.3 幂等键的设计（`:146-165`）

```python
build_idempotency_key(doc_id, job_type, generation) = f"{doc_id}:{job_type}:{generation}"
build_embed_hold_request_id(key)                    = f"kb-embed:{key}"
```

`generation` 是「同 doc + 同 job_type 的历史 job 计数」（`:154-165`）。

**为什么要有 generation 这一维？** 想一个场景：文档 parse 失败了，用户点 retry。如果幂等键只是 `{doc_id}:parse`，第二次 job 会因为唯一约束建不出来——**重试功能直接废掉**。加上 generation，第一次是 `...:parse:0`，重试是 `...:parse:1`，既能重试又保证了「同一次尝试不会被重复执行」。

这是幂等设计里很容易踩的坑：**幂等的粒度是「一次尝试」，不是「一个对象」。**

#### 7.4 三种幂等保护（对照阶段 4/5/6 的模式看）

| 保护 | 位置 | 防什么 |
| --- | --- | --- |
| job 幂等键唯一约束 | `models/kb.py:200` | 同一次尝试建两个 job |
| **重跑 parse 先清旧分块** | `_clear_document_chunks`，`:413-429` | 重复分块（`test:356-412`） |
| **embed 前检查有无待处理块** | `:697-708` | 全部 success 时**直接 ready，不预扣不扣费**（`test:627-699`） |

第三条最值得学：**幂等的最高境界是「发现没事可做就什么都不做」**，而不是「做完了再发现重复」。它省掉了一次预扣、一次上游调用、一次实扣。

#### 7.5 向量化 job 的资金链路（`run_embed_job`，`:697-869`）

**这一段是阶段 6 的实战复习**，逐步对照：

```
1. 检查有无 pending/failed 分块  →  没有就直接 ready（:697-708）
2. 估算 token → 文档级 hold      →  余额不足 → insufficient_balance（:743-747）
3. 分批调 embedding              →  每批 request_id = f"{hold_request_id}:batch:{i}"（:785）
4. 全成功 → capture              →  部分失败 → partial（:846-869）
                                 →  全失败 → release（test:776-827）
```

**注意第 2 步是「文档级」hold，不是每批一个 hold**。想清楚为什么：一份文档切 800 块分 20 批，如果每批一个 hold，就是 20 次行锁、20 条流水，而且中间失败时状态极其难收拾。**一次 hold 覆盖整个文档，是把「资金原子性」的边界画在了业务对象上。**

失败状态的三分法也要记：

| 状态 | 含义 | retry 行为 |
| --- | --- | --- |
| `insufficient_balance` | 钱不够，**没扣任何钱** | 充值后 retry 继续 |
| `partial` | 部分块失败了 | retry **只重试失败的块**（`test:486-623`） |
| `failed` | 彻底失败 | retry 重来 |

#### 7.6 worker 入口（`app/tasks/kb_worker.py`）

```powershell
python -m app.tasks.kb_worker              # --once，处理一批后退出
python -m app.tasks.kb_worker --loop       # 常驻轮询
python -m app.tasks.kb_worker --loop --batch-size 5 --poll-interval 2
```

| 内容 | 行号 |
| --- | --- |
| loop 模式：`while True` + 空闲 sleep | `101-113` |
| once 模式 | `122-134` |
| 参数定义 | `137-165` |
| 指标打点 | `66-77`、`84` |

**worker 本身非常薄**（十几行主逻辑），真正的活全在 `kb_ingest_service.process_one`（`:992-1009`）里。这是好的分层：worker 只管「循环 + 进程生命周期」，业务逻辑留在 service 里，于是 service 可以被测试直接调用，不需要起进程。

注意：**worker 层不做失败重试**（`:72-77` 只统计），重试要靠用户点 retry 或建新 job。上游 embedding 的网络重试在更下层（`embedding_gateway.py:41`、`:146`，最多 3 次）。

**三层重试的边界要分清**：网络抖动 → 网关层重试；业务失败 → 用户 retry；worker → 什么都不重试。

---

### D8 · 内部调用方复用网关（模式 Q，50 分钟）

`app/services/embedding_gateway.py` 回答本阶段第二个核心问题：**知识库怎么「假装成一个用户」来调用自己的网关**。

#### 8.1 `InternalCaller`（`:76-85`）

```python
class InternalCaller:
    user: User
    source: str          # "kb_ingest" 或 "kb_query"
```

就这么简单——一个用户 + 一个来源标记。构造点：

| 场景 | 位置 | source |
| --- | --- | --- |
| 文档向量化 | `kb_ingest_service.py:771` | `kb_ingest` |
| 检索时给 query 向量化 | `retrieval_service.py:111` | `kb_query` |

**`source` 这个字段就是阶段 7 D3 那条测试的由来**：它最终会写进 `usage_logs.source`（`:276`、`:423-434`），让用量能区分「用户直接调的」和「知识库内部调的」。

#### 8.2 复用了哪些东西（`_call_upstream_embeddings`，`:148-247`）

逐个对照阶段 4 学的：

| 复用的东西 | 行号 |
| --- | --- |
| `routing_service.pick_route` 选渠道 | `148-153` |
| `get_adapter` 拿适配器 | `162` |
| `adapter.build_embeddings_url / post / parse` | `173-247` |
| 并发闸（Semaphore） | `179` |
| 上游重试 | `41`、`146` |

**一行新的转发代码都没有。** 这就是 D1 问题 2 的答案。

#### 8.3 两种结算模式（`settle` 参数）

| 模式 | 用在哪 | 计费边界 |
| --- | --- | --- |
| `settle=True` | 单次请求（如检索时的 query 向量化） | 网关内部 hold（`:303-309`）→ capture（`:369-377`）/ release（`:329-336`） |
| **`settle=False`** | 文档级批量向量化 | **只调上游 + 写日志，钱包由 `kb_ingest_service` 在文档级管**（`:267-268`） |

`settle=False` 这个开关就是 D7.5 「文档级 hold」的实现方式。想清楚这个参数存在的必要性——（如果网关强制每次调用都结算，就没办法把 20 批合成一次 hold）

#### 8.4 幂等键格式汇总（抄进笔记）

```
文档级 hold  :  kb-embed:{doc_id}:embed:{generation}
每批调用     :  {hold_request_id}:batch:{i}
单块重向量化 :  kb-reembed:{chunk_id}:{uuid4}
```

问题：为什么单块 re_embed 用了 uuid（**不幂等**），前两个却是确定性的？（提示：re_embed 是用户每次编辑分块都会触发一次的，两次编辑本来就该扣两次钱。**幂等不是越多越好，要看业务语义**）

---

### D9 · 检索与 RAG（模式 Q，50 分钟）

#### 9.1 检索（`retrieval_service.py`）

流程（`:69-151`）：

```
1. 校验参数（query / top_k / threshold）              :69-75
2. 归属校验 → 他人的库 403，且不产生 embedding 费用    :100、:177-178
3. 过滤 active 库                                     :102-104
4. 按 embedding_model 分组，同模型的 query 只 embed 一次  :106-133
5. 每个库各查一次，合并排序取全局 top_k                :135-151
```

**第 2 步的顺序很讲究：先鉴权再花钱。** 如果反过来，攻击者可以拿别人的 kb_id 疯狂请求，虽然拿不到结果但能把你的 embedding 账单打爆。这是「鉴权要放在消耗资源之前」的实例。

**第 4 步是个小优化**：查 3 个库如果都用同一个 embedding 模型，query 只需要向量化一次。想清楚为什么不能所有库共用一次——（不同库可能配了不同的 embedding 模型，向量空间不同，不能混用）

向量查询（`:192-212`）：

```python
distance = KbChunk.embedding.cosine_distance(query_embedding)   # :192
score    = 1.0 - distance                                       # :218
# 阈值转换：score >= threshold  ⇔  distance <= 1 - threshold     # :193-194
.order_by(distance.asc()).limit(top_k)                          # :211-212
```

过滤条件（`:203-208`）值得逐条看：`kb_id` + `is_enabled=True` + `embedding_status='success'` + `embedding IS NOT NULL` + 文档状态 `ready/partial`。

**为什么 `partial` 的文档也能被检索？**（部分块成功了就该能用。让用户因为 800 块里有 3 块失败就完全用不了，是很糟的产品决策）

**注意 `:193-194` 那个阈值转换**：过滤条件写在 `distance` 上而不是 `score` 上。为什么？（提示：`score = 1 - distance` 是算出来的表达式，写在 WHERE 里 Postgres 用不上 HNSW 索引。**必须让过滤条件保持索引能用的形态**——这是向量检索的常见性能陷阱）

#### 9.2 RAG 编排（`rag_chat_service.py` + `api/chat_rag.py`）

`prepare` 的流程（`:216-309`）：

```
没传 kb_ids           → 原样透传，退化成普通 chat      :232-238
归属校验              → resolve_owned_kb_ids          :240-242
提取最新用户问题      → _extract_latest_user_question :245
检索                  → retrieval_service.search      :264-271
上下文预算截断        → apply_context_budget          :273-275
命中 → 注入 system prompt                             :289-295
未命中 → strict：不调模型 / fallback：换个 prompt      :311-345
```

**上下文预算**（`compute_context_budget`，`:81-86`）：

```python
min(模型上下文长度 × 40%, 4000)
```

这个 40% 是个拍脑袋的经验值，但背后的道理是硬的：检索结果不能把上下文吃光，得给对话历史和模型输出留地方。

**strict / fallback 两种未命中策略**（`:311-345`）是本节最该想的产品设计题：

| 策略 | 行为 | 适合什么场景 |
| --- | --- | --- |
| strict | **不调模型**，直接返回固定文案 | 企业内部知识问答——宁可说「不知道」，也不要模型编 |
| fallback | 换一个 prompt 照常调模型 | 通用助手——知识库只是补充 |

strict 模式还顺带省了一次模型调用的钱（`chat_rag.py:157-162`）。

路由 `POST /chat/kb-completions`（`chat_rag.py:102-193`）有个反常识的设计：**JWT 鉴权（`:106`）+ 额外要 `X-Api-Key`（`:164-169`）**。想想为什么要两个——（JWT 证明「你是谁、这些库是不是你的」，API Key 用来走计费链路。**身份和账单是两件事**）

流结束时追加 citations 事件（`_wrap_chat_stream_with_citations`，`:87-99`）：引用信息在流的**最后**才发。为什么不能一开始就发？（因为要等模型回答完，才知道……其实这里是因为 SSE 流是单向的，citations 作为附加事件挂在流尾最省事。想想有没有更好的做法）

---

## 二、怎么做

> 所有演练都需要 worker 在跑。开两个终端：一个 `uvicorn`，一个 `python -m app.tasks.kb_worker --loop`。

### 任务 1 · 跟着一份文档走完状态机（必做，约 90 分钟）

#### Step 1：准备

```powershell
# 终端 1
cd d:\LLM_TokenHub\backend; .\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload

# 终端 2
cd d:\LLM_TokenHub\backend; .\.venv\Scripts\Activate.ps1
python -m app.tasks.kb_worker --loop --poll-interval 2
```

先建一个知识库（需要 JWT），记下 `kb_id`。

#### Step 2：上传两份文件

一份**中文 Markdown**（带多级标题，至少 3000 字），一份 **PDF**。

```powershell
curl.exe -X POST http://localhost:8000/kb/<kb_id>/documents `
  -H "Authorization: Bearer <JWT>" -F "files=@test.md" -F "files=@test.pdf"
```

#### Step 3：每隔两秒查一次状态，记录每一次流转

```powershell
curl.exe http://localhost:8000/kb/documents/<doc_id> -H "Authorization: Bearer <JWT>"
```

填这张表（**这是本阶段第一个产出**）：

| 时刻 | status | progress | 触发它的代码位置 |
| --- | --- | --- | --- |
| 上传完 | `uploaded` | | `kb_service.py:368-449` |
| | `parsing` | | |
| | `chunking` | | |
| | `pending_embedding` | | |
| | `embedding` | | |
| | `ready` | | |

「触发它的代码位置」这一列必须自己去 `kb_ingest_service.py` 里找，不能空着。

#### Step 4：对照 PRD 状态机图

打开 `docs/prd/knowledge-base.md` 的状态机图，**逐个状态确认你观察到的和图上一致**。不一致的地方记下来（可能是文档过时了，也可能是你看漏了）。

#### Step 5：看分块质量（顺带验证 D6）

```powershell
curl.exe "http://localhost:8000/kb/documents/<doc_id>/chunks?limit=5" -H "Authorization: Bearer <JWT>"
```

观察三件事：

- [ ] 分块开头是不是带了标题前缀？（D6.2 结构优先）
- [ ] 相邻两块有没有重叠内容？（overlap）
- [ ] 中文有没有被切在句子中间？（分隔符里的中文标点起作用了吗）

### 任务 2 · SQL 取证：这次处理留下了什么（必做，约 45 分钟）

**跑之前先预测每张表会有几行**，写在笔记里，再跑。

```sql
-- 1. 文档
SELECT id, file_name, file_type, status, total_tokens, chunk_size, chunk_overlap
FROM kb_documents WHERE kb_id = '<kb_id>';

-- 2. 分块（向量列很长，只看长度和状态）
SELECT seq, LEFT(content, 40) AS head, token_count, embedding_status,
       is_enabled, (embedding IS NOT NULL) AS has_vector, source_location
FROM kb_chunks WHERE document_id = '<doc_id>' ORDER BY seq LIMIT 10;

-- 3. job（注意 idempotency_key 的格式）
SELECT job_type, status, progress, idempotency_key, hold_ref_request_id, retry_count
FROM kb_ingest_jobs WHERE document_id = '<doc_id>' ORDER BY created_at;

-- 4. 用量（source 应该是 kb_ingest）
SELECT request_id, model_id, input_tokens, charge_amount,
       gift_charge_amount, cash_charge_amount, source
FROM usage_logs WHERE source = 'kb_ingest' ORDER BY created_at DESC LIMIT 10;

-- 5. 钱包流水（用 job 的 hold_ref_request_id 反查）
SELECT type, amount, gift_amount, balance_after, gift_balance_after, ref_request_id
FROM wallet_transactions
WHERE ref_request_id LIKE 'kb-embed:%' ORDER BY created_at DESC LIMIT 10;
```

对着结果回答（**每条都要有答案**）：

- [ ] job 的 `idempotency_key` 长什么样？和 D7.3 的公式对得上吗？generation 是几？
- [ ] `hold_ref_request_id` 和钱包流水的 `ref_request_id` 是什么关系？
- [ ] 钱包流水里有几条？hold / capture / release 各几条？**为什么是文档级一条而不是每批一条？**（D7.5）
- [ ] `usage_logs` 里有几条？和批次数对得上吗？为什么用量是每批一条、钱包流水是每文档一条？
- [ ] 如果你钱包里有赠金，`gift_charge_amount` 是多少？**向量化也会先花赠金吗？**（阶段 6 的口径在这里同样生效）
- [ ] `source_location` 字段里存了什么？它是哪一层产出的？（D6.1）

### 任务 3 · 故障演练 A：余额不足（必做，约 40 分钟）

**这是阶段 6「零扣费」承诺在新场景下的验证。**

#### Step 1：清零

后台把该用户的现金池和赠送池都清零（`adjust` + 撤回赠金批次）。

```sql
SELECT balance, held, gift_balance, gift_held FROM wallets WHERE owner_id = '<user_id>';
-- 四个字段都应该是 0
```

#### Step 2：上传文档

观察状态：应该停在 **`insufficient_balance`**，不是 `failed`。

#### Step 3：验证「零扣费」

```sql
-- 应该是空的：没有任何 kb-embed 的钱包流水
SELECT * FROM wallet_transactions WHERE ref_request_id LIKE 'kb-embed:%'
  AND created_at > now() - interval '10 minutes';

-- 也不应该有 usage_log
SELECT * FROM usage_logs WHERE source = 'kb_ingest'
  AND created_at > now() - interval '10 minutes';
```

#### Step 4：充值后 retry

```powershell
curl.exe -X POST http://localhost:8000/kb/documents/<doc_id>/retry -H "Authorization: Bearer <JWT>"
```

观察它从 `insufficient_balance` 重新走到 `ready`。

在笔记里回答：

- [ ] retry 之后 job 表多了几行？新 job 的 `idempotency_key` 和旧的差在哪？（generation！）
- [ ] 代码里「余额不足」是在哪一行被捕获并转成 `insufficient_balance` 的？（`kb_ingest_service.py:743-747`）
- [ ] 对照 `test_kb_embed.py:418-482`，你的观察和测试断言一致吗？

### 任务 4 · 故障演练 B：两个 worker 抢同一个 job（必做，约 30 分钟）

#### Step 1：同时起两个 worker

```powershell
# 终端 2 和终端 3 各跑一个
python -m app.tasks.kb_worker --loop --poll-interval 1
```

#### Step 2：一次上传 5 份文件

观察两个终端的日志：**每份文档只会被其中一个 worker 处理**。

#### Step 3：找到代码依据

在笔记里写清楚：

- [ ] `SKIP LOCKED` 在哪一行？（`kb_ingest_service.py:373`）
- [ ] 如果去掉 `SKIP LOCKED` 只留 `FOR UPDATE`，两个 worker 会怎样？（D7.2 那张对比表）
- [ ] 如果两个都去掉呢？
- [ ] 读一遍 `test_kb_ingest.py:462-542`，它是怎么在测试里造出「两个连接同时 claim」的？把这个手法记下来。

### 任务 5 · 断点跟踪一次向量化 job（必做，约 60 分钟）

打这五个断点，跑 `python -m app.tasks.kb_worker --once`（用 `.vscode/launch.json` 里的调试配置，把 module 改成 `app.tasks.kb_worker`）：

| # | 位置 | 看什么 |
| --- | --- | --- |
| 1 | `kb_ingest_service.py:373` 附近 | claim 到的 job：id / type / idempotency_key |
| 2 | `kb_ingest_service.py:697` | 有几个 pending 块？会不会走「直接 ready」的短路？ |
| 3 | `kb_ingest_service.py` hold 处 | 预扣金额怎么估的？token 数从哪来？ |
| 4 | `embedding_gateway.py:148-162` | **选中了哪个渠道、哪个适配器**（阶段 4 的复习） |
| 5 | `kb_ingest_service.py` capture 处 | 实扣多少？和预扣差多少？差额去哪了？ |

记录这张表（**本阶段最后一个产出**）：

| 步骤 | 金额 / 数值 | 钱包四字段 |
| --- | --- | --- |
| 预扣前 | — | |
| hold 之后 | 预扣 ___ | |
| 每批调用（记 2 批） | 批 0 token ___ / 批 1 token ___ | 不变？ |
| capture 之后 | 实扣 ___ | |

**加分题**：在第 4 个断点处停住，直接杀掉 worker 进程。然后：

- [ ] 文档停在什么状态？job 停在什么状态？
- [ ] 钱包里的钱怎么样了？（hold 了但没 capture 没 release ——**这不就是阶段 5 学的悬挂 hold 吗**）
- [ ] 谁会来收拾它？跑一下 `python -m app.tasks.reconcile_holds --expire-seconds 1 --dry-run` 看它认不认得这笔。
- [ ] 用户点 retry 会发生什么？会重复扣费吗？依据在哪？

---

## 三、过关自测

1. 不看文档画出文档处理状态机，说清每个状态由哪段代码流转、失败时停在哪。（D7.1 / 任务 1）
2. 向量化的钱是怎么扣的？为什么复用网关内部链路而不是直连上游？（D1 / D8）
3. 同一个 job 被两个 worker 同时拉到会发生什么？代码靠什么避免？说出文件行号。（D7.2 / 任务 4）
4. 幂等键为什么要带 generation？不带会导致什么功能失效？（D7.3）
5. 为什么是「文档级 hold」而不是「每批 hold」？（D7.5 / 任务 2）
6. `insufficient_balance`、`partial`、`failed` 三个状态有什么区别？retry 的行为分别是什么？（D7.5 / 任务 3）
7. 为什么不能只看扩展名判断文件类型？`detect_and_validate_file_type` 除了嗅探还多做了一件什么事？（D5.4）
8. 分块的三层策略分别解决什么问题？标题为什么不单独成块？（D6.2）
9. 检索的过滤条件为什么写在 `distance` 上而不是 `score` 上？（D9.1）
10. RAG 未命中时 strict 和 fallback 分别怎么处理？各适合什么产品场景？（D9.2）
11. 检索为什么要「先鉴权再向量化」？顺序反了有什么风险？（D9.1）

---

## 四、这个阶段常见的坑

- **忘了起 worker**：文档永远停在 `uploaded`，然后你开始怀疑人生。演练前先确认终端 2 在跑。
- **以为 worker 会自动重试失败的 job**：不会（`kb_worker.py:72-77` 只统计）。三层重试的边界见 D7.6。
- **在 `kb_chunks` 上直接 `SELECT *`**：1536 维向量会刷屏。永远显式列字段，向量列只查 `IS NOT NULL`。
- **把 HNSW 当精确检索**：它是**近似**最近邻，理论上可能漏掉真正的 top-1。对内部知识库够用，但要知道这个前提。
- **过滤条件写在 `1 - distance` 上**：索引失效，查询从毫秒变成秒。D9.1 那个细节不是抠字眼。
- **拿别人的 kb_id 测试 403**：会 403，但**注意验证它没有产生 embedding 费用**（`retrieval_service.py:100`）。鉴权顺序是这里的考点。
- **改了库的 chunk_size 就期望旧文档跟着变**：文档存的是快照（`models/kb.py:104-106`）。要变得 rechunk。
- **忽略 `source` 字段**：`usage_logs.source` 区分了 `api` / `kb_ingest` / `kb_query`。做用量统计时不分来源，数字会莫名其妙对不上。
