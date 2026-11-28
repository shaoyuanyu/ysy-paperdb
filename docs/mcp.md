# Agent 接口（MCP）

`paperdb-mcp` 把文献库后端暴露成 agent 工具。设计规则只有一条：

> **人类在前端能做的操作，agent 必须能用工具做完；做不到的，必须在本文件里写明理由。**

这条规则由 [`tests/test_mcp_coverage.py`](../tests/test_mcp_coverage.py) 强制执行——它枚举 OpenAPI 的每个操作，逐个比对 MCP 实际调用的端点，新增接口若既不包装也不登记理由，测试直接失败。覆盖情况：**116 个 API 操作，95 个由工具直接覆盖，其余 21 个在下方「刻意不开放」中逐条说明**。

工具数量：**93**。传输：stdio（`paperdb-mcp`），令牌用个人 token，scope 决定真实权限（`whoami` 可查）。

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

## 验收

```bash
make test          # 126 passed，含 MCP 工具端到端与覆盖度守卫
make lint typecheck
```

- `tests/test_mcp_tools.py`：每个工具都通过真实 MCP 函数打真实 ASGI 应用（请求形状、别名、multipart 全部校验）。
- `tests/test_mcp_coverage.py`：API 操作 ↔ 工具覆盖比对，新增未包装接口会失败。
- `tests/test_reading.py`：按页读取、截断、`ocr_needed`、作用域搜索、页图渲染。
