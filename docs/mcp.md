# Agent 接口（MCP）

`paperdb-mcp` 把文献库后端暴露成 agent 工具。设计规则只有一条：

> **人类在前端能做的操作，agent 必须能用工具做完；做不到的，必须在本文件里写明理由。**

这条规则由 [`tests/test_mcp_coverage.py`](../tests/test_mcp_coverage.py) 强制执行——它枚举 OpenAPI 的每个操作，逐个比对 MCP 实际调用的端点，新增接口若既不包装也不登记理由，测试直接失败。覆盖情况：**117 个 API 操作，95 个由工具直接覆盖，其余 22 个在下方「刻意不开放」中逐条说明**。

工具数量：**93**，但**不一次性全部注册**——按 workflow 分成 5 组按需加载，默认只装
`import` + `read` + `organize`（61 个），外加常驻的控制工具 `toolset`（共 62）。
传输：stdio（`paperdb-mcp`），令牌用个人 token，scope 决定真实权限（`whoami` 可查）。

工具表里没有任何宿主名字：这个 server 面向任何支持 MCP 的 harness/agent，安装到具体宿主见「宿主适配（不在工具面上）」。

## 工具分组与按需加载

工具表按**工作流**分组，而不是按 HTTP 路由：一个对象的所有变更和它自己同组，所以
"这个任务需要什么" 不需要读 API 才能回答。

| 组 | 内容 | 默认 |
|---|---|---|
| `import` | 入库、元数据、PDF 挂载、导入作业、查重、富化、场馆查询（选 `venue_id` 用） | ✅ |
| `read` | 正文/页图/大纲、全文检索、列表与统计、引用图、批注读取、阅读状态、单篇 BibTeX | ✅ |
| `organize` | 标签、集合、批注写入、批量修改、回收站、删除/合并、主 PDF | ✅ |
| `workflow` | 调研工作区、项目、智能视图、自动规则 | ❌ 按需 |
| `library` | 作者记录、场馆补建与修正（写） | ❌ 按需 |

两种加载方式：

```bash
# 启动时（宿主适配器或 shell 里）：逗号分隔，或 all
PAPERDB_MCP_TOOLSETS=import,read,organize,workflow paperdb-mcp
```

```
# 会话内：随时把组加进来，返回的 addedTools 立刻可调用
toolset(op="enable", groups=["workflow"])
toolset(op="list")        # 每组的中文说明、是否已装、成员工具名
```

`toolset` 是常驻工具（不在任何组里），所以窄会话永远不会"卡住"在启动时的那几组。
启用后 server 会发 `notifications/tools/list_changed`；即使宿主没有刷新工具表，
`enable` 的返回值已经给出了新增工具名，按名字调用仍然可达。

## 导入

| 工具 | 端点 | 说明 |
|---|---|---|
| `resolve` | `POST /v1/resolve` | 只读预览：DOI/arXiv/标题/URL → 候选、置信度、CCF/CAS、重复提示 |
| `import_paper` | `POST /v1/imports/identifiers` | 单篇导入，可落集合或调研工作区 |
| `import_papers` | `POST /v1/imports/identifiers` | 批量导入（≤100），一个 job、逐项状态 |
| `import_paper_with_pdf` | `POST /v1/imports/pdf-url` | 落地页/直链 PDF → 论文 + 文件 |
| `import_pdf_file` | `POST /v1/imports/pdf` | **本地 PDF** → 论文（绝对路径，按字节去重） |
| `import_bibtex` | `POST /v1/imports/bibtex` | BibTeX（≤500 条） |
| `get_import` / `list_imports` | `GET /v1/imports/{id}`、`GET /v1/imports` | 导入进度 / 历史 |
| `cancel_import` / `retry_import_item` | `POST .../cancel`、`POST .../retry` | 取消、单项重试 |
| `get_job` | `GET /v1/jobs/{id}` | 任意后台作业状态（抽取、富化） |

### 长任务：等待是预算，不是承诺

导入是后台 job，工具调用不该被 job 卡住：

- `import_paper` / `import_papers` / `import_bibtex` 都有 `wait_seconds`，
  **默认 15 s，上限 60 s，`0` 立刻返回**；旧版 `import_papers` 的 180 s 默认值比宿主的
  整个 tool timeout 还长，已删除。
- 无论是否等到终态，返回里都有 `status`、逐项 `items`、`pendingItems`；
  还没结束时会多一个 `next`：`Call get_import(import_id="…") to continue.`
- `get_import` 是"续读"入口，**从不等待**：一次状态读 + 逐项结果，返回同样的
  `pendingItems` / `next` 结构。
- `import_paper_with_pdf` 例外：它走的是同步的 `/v1/imports/pdf-url`（抓页面 + 下 PDF），
  可能耗时数十秒，所以只用于单篇；批量请用 `import_papers`。
- 宿主侧 tool 超时与本预算的关系：DSH 的 bundle 配 `toolCallTimeoutMs: 90000`，
  最坏 60 s 等待 + 30 s 余量，不会再出现"工具超时但其实还在跑"的假失败。

## 阅读

| 工具 | 端点 | 说明 |
|---|---|---|
| `get_paper_text` | `GET /v1/files/{id}/text` | 按页窗口读正文（默认 100 页 = 整篇），`[p.N]` 标记 + `nextPage` |
| `get_paper_outline` | `GET /v1/files/{id}/outline` | 章节结构（书签 + 页码），用来只读相关几页；无书签时给回退提示 |
| `library_stats` | `GET /v1/library/stats` | 一览：总量、阅读状态分布、有 PDF/已抽文本数 |
| `get_page_image` | `GET /v1/files/{id}/pages/{page}/image` | 单页渲染 PNG（图表、公式、版式），`dpi` 72–300 |
| `search_text` | `GET /v1/search/fulltext` | 作用域：`library` / `workspace:<id>` / `paper:<ref>` / `file:<id>` |
| `search_fulltext` | 同上 | `scope="library"` 的简写，兼容旧用法 |
| `get_annotations` | `GET /v1/papers/{ref}/annotations` | 用户的高亮与批注 |
| `list_files` | `GET /v1/papers/{ref}/files` | 文件清单与 `textStatus` |
| `reindex_file` | `POST /v1/files/{id}/reindex` | 重新抽取文本 |

阅读是唯一推进到「可视化」的地方：文本层（`/text`）+ 图像层（`/image`）合起来才等价于人类在阅读器里看到的内容。

## 元数据与文件

| 工具 | 端点 | 说明 |
|---|---|---|
| `get_paper` / `list_papers` / `search_papers` | `GET /v1/papers...` | 详情 / 列表（支持 `view_id`、`filter` DSL、`sort`）/ 标题搜索 |
| `update_paper` | `PATCH /v1/papers/{ref}` | 修元数据（venue/标题/摘要/年份/kind/citekey） |
| `create_paper` | `POST /v1/papers` | 手工建条目（跳过去重与场馆归一化） |
| `bulk_update_papers` | `PATCH /v1/papers/bulk` | 批量改 ≤500 篇的标签/集合/阅读状态 |
| `enrich_paper` | `POST /v1/papers/{ref}/enrich` | 重新富化元数据 |
| `list_duplicates` | `GET /v1/papers/{ref}/duplicates` | 重复候选 |
| `attach_pdf` / `upload_pdf` | `POST .../files/from-url`、`POST .../files` | URL 挂 PDF / **本地文件**挂 PDF |
| `set_primary_file` / `delete_file` | `POST /v1/files/{id}/primary`、`DELETE /v1/files/{id}` | 主文件、删文件 |
| `get_paper_bibtex` | `GET /v1/papers/{ref}/bibtex` | 单篇 BibTeX |

## 整理

| 工具 | 端点 | 说明 |
|---|---|---|
| `add_tags` / `remove_tags` | `POST|DELETE /v1/papers/{ref}/tags` | 给论文加/去标签 |
| `create_tag` / `update_tag` / `merge_tags` / `delete_tag` | `/v1/tags...` | 标签树维护（`name` 只改显示名，路径稳定） |
| `create_collection` / `update_collection` / `move_collection` / `delete_collection` | `/v1/collections...` | 集合树维护 |
| `collection_papers` | `POST|DELETE /v1/collections/{id}/papers`、`.../reorder` | 一次调用完成集合内增删改序 |
| `get_collection_bibtex` | `GET /v1/collections/{id}/bibtex` | 集合（含子集合）导出 |
| `create_project` / `update_project` / `delete_project` | `/v1/projects...` | 项目维护 |
| `project_papers` / `project_collections` | `POST|DELETE /v1/projects/{id}/papers|collections` | 项目收论文/集合 |
| `get_project_bibtex` | `GET /v1/projects/{id}/bibtex` | 项目导出 |
| `list_trash` / `restore_paper` | `GET /v1/papers/trash`、`POST .../restore` | 回收站与恢复 |
| `create_annotation` / `update_annotation` / `delete_annotation` | `/v1/annotations...` | 写回用户阅读器里可见的高亮/批注 |

## 调研工作区

| 工具 | 说明 |
|---|---|
| `list_workspaces` / `create_workspace` / `get_workspace` / `update_workspace` | 列表、新建、详情（含论文行与 `pdfCount`/`textStatus`）、改名 |
| `add_to_workspace` / `promote_workspace_papers` / `discard_workspace_papers` / `close_workspace` | 暂存、转入库、丢弃、关闭 |

staged 论文不进出库列表与库级搜索，但 **`get_paper_text` / `search_text(scope="workspace:...")` / `get_annotations` 读得到**：隔离的是「入库」，不是「阅读」。

## 视图与自动规则

| 工具 | 说明 |
|---|---|
| `list_views` / `create_view` / `update_view` / `delete_view` | 智能视图（filter DSL + sort），列表自带 count |
| `validate_filter` | 保存前校验 filter DSL |
| `list_rules` / `create_rule` / `update_rule` / `delete_rule` / `apply_rule` | 自动规则：命中 filter 自动打标签/进集合/改状态 |

## 作者与场馆

| 工具 | 说明 |
|---|---|
| `list_authors` / `get_author` / `create_author` / `update_author` | 作者记录 |
| `list_venues` / `get_venue` / `get_venue_ranks` / `create_venue` / `update_venue` | 场馆、CCF/CAS 评级、补建场馆 |

## 宿主适配（不在工具面上）

这个 server 对宿主中立：它只说 stdio MCP，只读 `PAPERDB_MCP_*`，工具表里不出现任何
宿主名字。把 server 装进某个宿主（写宿主的配置文件、装对应 skill）是**本机 owner 动作**，
走 CLI 而不是 MCP 工具：

```bash
paperdb adapter status   --host dsh            # 离线：已装版本、逐文件漂移
paperdb adapter check    --host dsh            # 已装 vs 最新 release
paperdb adapter update   --host dsh --totp 123456
paperdb adapter rollback --host dsh --totp 123456
```

写盘和删除类操作一样需要当前 6 位 TOTP（先 `POST /v1/totp/verify` 验码，再落盘），
`--dry-run` 免验证码、不写盘。安装目录按 `--dir` → `PAPERDB_ADAPTER_DIR` → 宿主旧变量
（DSH 是 `PAPERDB_DSH_INTEGRATION_DIR`）→ 宿主默认值解析。bundle 的改动重启宿主后才生效，
skill 的改动新会话可见。发行物、校验与信任模型见 [`adapters/README.md`](../adapters/README.md)。

## 更新提示（`updateNotice`）

server 自己会检测新 release，并把「有更新」带进工具输出，让 agent 能在合适的时机问用户
要不要升级——用户不必主动问「有没有新版本」。检测保持宿主中立、且永远不拖慢工具调用：

- **频率**：查 `https://api.github.com/repos/<repo>/releases/latest` 最多每 6 小时一次
  （`PAPERDB_MCP_UPDATE_TTL` 秒数可调，`0` 整体关闭）。结果缓存为
  `~/.cache/paperdb/mcp/update-check.json`（随 `PAPERDB_MCP_CACHE_DIR`），所以 server
  重启也不会立刻重查；HTTP 404（还没有发布过 release）当作合法的「无更新」缓存整个
  TTL，网络失败则按更短的退避窗口（5–30 分钟）重试，不会打爆 GitHub。
- **触发**：缓存过期后的第一次工具调用在后台安排检查（单飞，同一时刻至多一个），
  当次调用仍用旧缓存立即返回，检查从不阻塞工具。
- **比较**：已装版本来自 bundle 渲染的 `PAPERDB_MCP_ADAPTER_VERSION`（即
  `paperdb adapter check` 比对的同一版本），缺失时退回已安装的 `paperdb` 发行版版本，
  再退回 `__version__`；都拿不到就保持沉默，绝不瞎猜。release 还必须带某个宿主的
  `paperdb-adapter-<host>-<version>.tar.gz` 资产才算可更新。
- **呈现**：有更新且已装版本落后时，下一个文本类工具结果带提示——JSON 对象结果末尾
  多一个 `updateNotice` 字段（`installed` / `latest` / `tag` / `page` / `notes` /
  `instructions`），数组或纯文本结果末尾多一行 `[paperdb-update] …`。每个 server 进程对
  同一个 latest 版本只提示一次；返回图片的工具（`get_page_image`）不带提示，顺延到
  下一个文本结果。提示里只有版本信息与中性指令，**升级动作本身仍然只走 CLI**。

agent 收到提示后的行为协议（何时问用户、等 60 秒、无回复即跳过、用户主动要求不受限）
写在宿主 skill 里（DSH 见 `adapters/dsh/skills/my-paperdb/SKILL.md` 的
「更新提示协议」一节），与本节的中性机制互为表里。

## 本机访问边界

MCP 进程以**你的用户权限**运行，所以边界不写在文档里，而是写在代码里：

| 能力 | 边界 |
|---|---|
| 读本机文件 | 只允许 PDF，且必须位于 `~/Downloads`、`~/Desktop`、`~/Documents`、系统临时目录（`/tmp`，macOS 是 `/var/folders/...`）之下；`PAPERDB_MCP_LOCAL_ROOTS`（`:` 分隔）可追加。路径先 `resolve()` 再判定，软链逃逸会被拒 |
| 写本机文件 | 只有页图缓存与更新检测缓存 `~/.cache/paperdb/mcp`（`PAPERDB_MCP_CACHE_DIR` / `PAPERDB_MCP_CACHE_MB` 控页图，默认 256 MiB 按最旧淘汰；`PAPERDB_MCP_UPDATE_TTL` 控更新检测，见上文） |
| 改文献库 | 一律走 API，权限 = 那枚 token 的 scope；删除/合并/改宿主配置要当前 6 位 TOTP |
| 装/升级宿主适配器 | 不在工具面上（`paperdb adapter` CLI），不走 MCP |

越界时错误信息会列出允许的根目录和扩展用的环境变量，方便 agent 直接转述给用户。

## 页图缓存与条件请求

页图是 `(内容 sha256, 页码, dpi)` 的纯函数，而 PDF 是内容寻址的，所以缓存键永不失效：

- **服务端**：`GET /v1/files/{id}/pages/{page}/image` 返回强 `ETag`，条件请求直接 `304`；
  渲染结果按 `(sha256, page, dpi)` 进进程内 LRU（`PAPERDB_RENDER_CACHE_MB`，默认 64，0 关闭），
  第二次读同一页不再碰 PyMuPDF。
- **MCP 侧**：`get_page_image` 把 PNG 落盘缓存，重读同一页零请求、零渲染。
- **默认 dpi 144 → 120**：整页 A4 的长边仍在多数 provider 的 1568 px 下采样阈值内，
  体积更小、上下文更省；要看小图细节再显式提高 `dpi`。

### 幂等与读链路缓存（决策）

- **导入天然幂等**：标识符命中 → link 到已有条目；PDF 字节相同 → 内容寻址直接 link；
  批量导入的 item 可按 id 重试，不会因为重跑而多出条目。
- **删除是软删除**，可从回收站恢复；`merge_papers` 会保留旧 id 的重定向。
- **读链路不加缓存**（不做 Redis / 不做查询结果缓存）：所有读直连 Postgres，
  1w–5w 条的规模下延迟可以忽略，而缓存引入的失效逻辑与"看到的不是最新"风险不成比例。
  需要省的是**渲染**（PyMuPDF 重新解析整份 PDF）和**传输**（PNG 字节），不是 SQL——
  这也正是上面那块缓存只覆盖页图的原因。这是一条明确的决策，不是遗漏。

## 危险操作

`delete_paper`、`merge_papers`、`delete_tag`、`delete_collection`、`delete_project`、`delete_view`、`delete_file` 全部要求**当前 6 位 TOTP**（`X-TOTP-Code`）。agent 必须在调用前向用户索取，不能缓存、复用或猜测。

## 刻意不开放

| 操作 | 理由 |
|---|---|
| `/v1/tokens*`、`/v1/export-tokens*`、`/v1/exports/bib/{token}` | 令牌与公开导出链接是密钥管理，只给人用 |
| `POST /v1/citations/sync` | 需要 `admin` scope；agent 令牌不带 admin（`whoami` 可确认） |
| `GET /v1/files/{id}/content` | 原始 PDF 字节；agent 用 `/text` 读文、`/image` 读图 |
| `GET /v1/files/{id}/pages/{page}/text` | 被按页窗口的 `/text` 取代 |
| `GET /v1/files/{id}/search` | 被 `search_text(scope="file:...")` 取代 |
| `GET /v1/files/{id}` | `list_files` 已返回同样的元数据 |
| `GET /v1/papers/{ref}/state` | `get_paper` 已含阅读状态 |
| `GET /v1/papers/{ref}/citations|references` | 由 `get_citations(kind=...)` 覆盖 |
| `GET /v1/views/{id}`、`GET /v1/views/{id}/count` | `list_views` 返回全部视图与计数 |
| `GET /v1/workspaces/{id}/papers` | `get_workspace` 已含论文行 |
| `DELETE /v1/papers/{ref}/collections` | 由 `bulk_update_papers(remove_collections=...)` 覆盖 |
| `GET /v1/meta`、`GET /v1/rank-datasets` | 实例诊断信息，不是文献库数据 |
| `POST /v1/totp/verify` | 由 `paperdb adapter update/rollback --totp` 在 CLI 侧验码，工具面不写宿主文件 |

## 验收

```bash
make test          # 126 passed，含 MCP 工具端到端与覆盖度守卫
make lint typecheck
```

- `tests/test_mcp_tools.py`：每个工具都通过真实 MCP 函数打真实 ASGI 应用（请求形状、别名、multipart 全部校验）。
- `tests/test_mcp_coverage.py`：API 操作 ↔ 工具覆盖比对，新增未包装接口会失败。
- `tests/test_reading.py`：按页读取、截断、`ocr_needed`、作用域搜索、页图渲染。
