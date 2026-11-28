---
name: my-paperdb
description: Import papers into the paperdb literature library from any starting point — DOI, arXiv ID, title, BibTeX, PDF, or any paper landing-page URL (even pages with no visible DOI, or pages that only describe a paper). Also triggers when the user asks to look up or find papers — present the results, then offer to add some or all to the library. Batch-friendly — identifier lists go in one request; for many paper pages, fan out parallel subagents to import concurrently.
whenToUse: When the user wants to add a paper (or a batch) to the literature library, shares a paper link and wants it imported, asks to save/fetch a paper's PDF, wants a paper's metadata, venue, or CCF/CAS rating checked, or asks to search/look up papers (after presenting results, offer to import some or all of them).
---

# Paper import (paperdb literature library)

The backend resolves every input through Crossref / OpenAlex / Semantic Scholar /
DBLP / arXiv / ACL Anthology / OpenReview and normalizes venues against the
local CCF 2026 + CAS 2025 datasets, so never hand-build paper metadata from a
web page — always go through the tools.

## Tools

### 工具是按组加载的（先看这里）

工具表按 workflow 分组，**默认只注册 `import`（入库）+ `read`（阅读/检索）+
`organize`（标签/集合/批注/批量/删除/合并）**。下列两组默认不在工具表里：

| 组 | 内容 | 何时需要 |
|---|---|---|
| `workflow` | 调研工作区、项目、智能视图、自动规则 | 暂存批量调研、建项目、存 filter 视图、配自动规则 |
| `library` | 作者记录、场馆补建与修正（写） | 改作者信息、补建/修正场馆 |

需要时先开组；返回的 `addedTools` **立刻可调用**（即使宿主没刷新工具表）：

```
toolset(op="enable", groups=["workflow"])   # 或 ["library"]
toolset(op="list")                          # 每组说明 + 是否已装 + 成员工具名
```

遇到"工具不存在"先 `toolset(op="list")` 看是不是没开组，不要直接告诉用户做不到。
下面工具表里这两组的工具：`list_workspaces`..`apply_rule`（workflow）、
`list_authors`..`update_venue`（library）；完整清单从 `toolset(op="list")` 取。

### 工具表

The paperdb MCP server (server name `paperdb`) provides:

| Tool | Use |
|---|---|
| `mcp__paperdb__resolve` | Read-only preview of any query (DOI / arXiv / title / URL). Returns candidate + confidence + venue/CCF/CAS + `duplicateOf` (identifier match) + `suggestedDuplicateOf` (fuzzy match: exact title + year + first author) + `pdfUrls` + warnings. Never writes. |
| `mcp__paperdb__import_paper` | Import one paper (query: identifier, title, or landing-page URL). Optional `tags`, `collection_id`, `on_duplicate` ("link" default / "skip" / "create"), `wait_seconds` (default 15, max 60, 0 = return now). Returns the paper when it finishes inside the budget, else per-item state + `next`. |
| `mcp__paperdb__import_papers` | Import a list of items (DOIs / arXiv IDs / titles / page URLs) as ONE job, up to 100. Prefer this over looping `import_paper`. Waits up to `wait_seconds` (default 15, max 60); if the job is still running the result has `next` — continue with `get_import`. |
| `mcp__paperdb__import_paper_with_pdf` | Landing-page or direct-PDF URL → paper **and** its PDF. Re-runs link to the existing paper; identical PDF bytes always link to the existing entry. |
| `mcp__paperdb__import_bibtex` | BibTeX string, up to 500 entries. |
| `mcp__paperdb__search_papers` / `get_paper` | Check what is already in the library (by title / citekey / id). |
| `mcp__paperdb__update_paper` | Repair imported metadata in place: `venue_id`, `title`, `abstract`, `year`, `kind`, `citekey`. Pass only what changes. |
| `mcp__paperdb__attach_pdf` | Attach a PDF (direct URL) to an existing paper. Never creates a paper, so it cannot duplicate. |
| `mcp__paperdb__list_venues` | Find the canonical venue id (and its CCF/CAS) to pass to `update_paper`. |
| `mcp__paperdb__list_files` | See which PDFs a paper actually has. |
| `mcp__paperdb__add_tags` / `add_to_collection` / `list_collections` | Organize after import. |
| `mcp__paperdb__create_collection` | Create a collection when the user chooses "keep in a collection" and no existing one fits. |
| `mcp__paperdb__list_workspaces` / `create_workspace` / `get_workspace` / `update_workspace` | Research workspaces (staging area): list, open a new one, inspect what is pending, rename. |
| `mcp__paperdb__add_to_workspace` | Stage papers that are already in the library/staging into a workspace. |
| `mcp__paperdb__promote_workspace_papers` / `discard_workspace_papers` / `close_workspace` | End-of-research decision: move papers into the library (optionally a collection), drop them, or close the workspace. |
| `mcp__paperdb__get_import` | Poll a running import job. |

**Browsing (the web UI's full read surface):**

| Tool | Use |
|---|---|
| `mcp__paperdb__list_papers` | Filter the library like the web UI: `q`, `collection_id`, `tag`, `venue_id`, `kind`, `year`, `status`, `starred`, `unfiled`, `cursor` pagination. |
| `mcp__paperdb__list_tags` | Full hierarchical tag tree with paper counts. |
| `mcp__paperdb__list_projects` / `get_project` | Project workspaces and their papers. |
| `mcp__paperdb__get_citations` | Citation graph: `references` (papers it cites) / `citations` (papers citing it). |
| `mcp__paperdb__search_text` | Full-text search with explicit scope: `library` / `workspace:<id>` / `paper:<ref>` / `file:<id>`. Scoped search reaches staged papers. |
| `mcp__paperdb__search_fulltext` | Shorthand for `search_text(scope="library")`. |
| `mcp__paperdb__get_paper_text` | Read a paper's body text page by page (`[p.N]` markers, `nextPage` to continue). |
| `mcp__paperdb__get_page_image` | Render one PDF page as an image — figures, tables, equations, layout. |
| `mcp__paperdb__get_paper_outline` | Section structure (bookmarks + pages) — read only the pages you need. |
| `mcp__paperdb__library_stats` | One-call overview: totals, reading-status spread, how much is readable. |
| `mcp__paperdb__upload_pdf` / `import_pdf_file` | Attach a local PDF to a paper / import a paper straight from a local PDF (absolute path). |
| `mcp__paperdb__get_annotations` | The owner's highlights and notes (quote, comment, page). |
| `mcp__paperdb__reindex_file` | Re-run text extraction for one PDF when `textStatus` is `failed`. |
| `mcp__paperdb__set_reading_state` | Update reading status/starred/priority/rating/progress. |
| `mcp__paperdb__get_paper_bibtex` | A paper's BibTeX entry. |

**整理与维护（agent 同样可写）：**

| 工具 | 用途 |
|---|---|
| `create_tag` / `update_tag` / `merge_tags` / `delete_tag` / `remove_tags` | 标签：建、改显示名、合并路径、删、从论文移除 |
| `update_collection` / `move_collection` / `delete_collection` / `collection_papers` / `get_collection_bibtex` | 集合：改名、移动/重排、批量增减论文、导出 .bib |
| `create_project` / `update_project` / `delete_project` / `project_papers` / `project_collections` / `get_project_bibtex` | 项目：建改删、收论文与集合、导出 .bib |
| `create_paper` / `bulk_update_papers` | 手工建条目；一次批量改 ≤500 篇（标签/集合/阅读状态） |
| `list_trash` / `restore_paper` | 回收站与误删恢复 |
| `list_duplicates` / `enrich_paper` | 查重候选；重新富化元数据 |
| `list_views` / `validate_filter` / `create_view` / `update_view` / `delete_view` | 智能视图：先验证 filter DSL 再保存 |
| `list_rules` / `create_rule` / `update_rule` / `delete_rule` / `apply_rule` | 自动规则：按 filter 自动打标签/进集合/改状态 |
| `create_annotation` / `update_annotation` / `delete_annotation` | 读 PDF 时留下的高亮与批注（rects 为 `{x,y,w,h}`，0–1） |
| `set_primary_file` / `delete_file` | 主 PDF 切换、删除 PDF |
| `list_imports` / `cancel_import` / `retry_import_item` / `get_job` | 导入历史、取消、单项重试、查作业状态 |
| `list_authors` / `get_author` / `create_author` / `update_author` / `get_venue` / `get_venue_ranks` / `create_venue` / `update_venue` | 作者与场馆记录维护 |

`list_papers` 还支持 `view_id` / `filter`（filter DSL 对象）/ `sort`（added/year/title/updated），
与前端用的是同一套筛选语义。

**引用同步**（`POST /citations/sync`）需要 `admin` scope，当前 agent 令牌没有；
需要同步时请用户在前端触发，或换一个带 admin 的令牌。

**Destructive operations (TOTP-gated):**

- `delete_paper`, `merge_papers`, `delete_tag`, `delete_collection`,
  `delete_project`, `delete_view` and `delete_file` require a current 6-digit TOTP code — the same code the user's
  authenticator shows for the site login. Always ask the user for the code
  immediately before calling (codes expire in ~30s); never fabricate, reuse
  old codes, or retry with guessed codes. A `totp_invalid` error means the
  code expired — ask the user for a fresh one. Deletion is soft (the owner
  can restore), but treat it as irreversible in conversation.
- If the MCP tools are not connected, tell the user to restart/repair the
  MCP connection. Do NOT call the HTTP API directly with tokens from
  configuration files: the service token bypasses the TOTP gate and must
  never be used by an agent.

## Import ladder

1. **User shares a URL** (paper page, Google Scholar link, repository page):
   `import_paper_with_pdf(url)`. The backend fetches the page, extracts
   `citation_*` meta / og:title / H1 / PDF links, resolves via the aggregators,
   and downloads the PDF. Works even when the page shows no DOI.
2. **User gives an identifier or title**: `import_paper(query)` — DOI,
   `arXiv:2601.01234`, DBLP/ACL/OpenReview URL, or a plain title.
3. **User pastes BibTeX**: `import_bibtex(text)`.
4. **User has a PDF file on disk**: use `upload_pdf(paper_ref, path)` when the
   paper is already in the library, or `import_pdf_file(path)` to create the
   paper from the PDF itself — both take an absolute host path (the MCP server
   runs locally, so it can read it). Prefer these over asking the user to drag
   the file into the web UI. **Local reads are confined to PDFs under
   `~/Downloads`, `~/Desktop`, `~/Documents`, `/tmp`** (a user can extend that
   with `PAPERDB_MCP_LOCAL_ROOTS`): if the path is outside, say so and ask the
   user to move the file or set the variable — do not work around it. If the
   file is reachable over http(s) instead, `attach_pdf(paper_ref, url)` /
   `import_paper_with_pdf(url)` still work. Never `curl` the API with a token
   from configuration.
5. **The source is a WeChat article / tweet / blog post describing a paper**:
   read the text, extract the paper's title (and authors/venue if stated), then
   `import_paper("<title>")`. Never import the article itself.
6. **A batch of identifiers** (one per line): `import_papers(items=[...])` —
   one job with per-item status and retry; poll with `get_import`. If the
   batch may not be kept, pass `workspace_id=...` so the papers land in a
   research workspace and stay out of the library. For many paper *pages* see
   the parallelism section below.

## Lookup flow: search, then offer to import

When the user asks to find or look up papers (a topic, an author, "papers
like X", a reading list):

1. Search — web search for discovery, `resolve` to preview specific
   candidates (read-only, returns title/venue/CCF/authors/duplicates).
2. Present the results compactly: title, venue + CCF/CAS, year.
3. **Proactively ask** whether to add some or all of them to the library
   (全部还是其中几篇), and — when the batch is exploratory — whether they go
   into a collection or into a research workspace (see the section below).
   Do not import silently during a lookup, and do not make the user ask
   "能不能加进文献库" themselves.
4. Import the chosen subset via the ladder; report citekey/venue/CCF per
   paper and call out duplicates.

## 批量调研：先问“集合还是调研工作区”

Deep research / 批量搜寻 / “先找一批看看” 时，结果默认不要直接堆进文献库。
调研工作区（research workspace）是项目、集合的同级结构，但它是**暂存区**：
里面的论文 `staged=true`，不出现在文献库列表、搜索和 `.bib` 导出里，直到被转入。

1. **先问，不要替用户决定**：这批文献是「加入某个集合」，还是「先放进调研工作区」？
   - **推荐调研工作区**：用于探索性 deep research、批量几十篇、或用户明确说“还不确定要不要留”。
   - 若用户选集合：用 `list_collections` 推荐 1–2 个最贴合的（说明理由，例如主题/年份）；
     没有合适的，征得同意后用 `create_collection` 新建。
   - 1–3 篇且明确要留下时，直接进集合/文献库即可，不必建工作区。
2. **建工作区**：`create_workspace("2026-11 <主题> 调研")`，名称带主题与时间，便于事后辨认。
3. **导入**：`import_papers(items=[...], workspace_id=...)`；
   单篇页面用 `import_paper_with_pdf(url, workspace_id=...)`。
4. **汇报**：`get_workspace` 复核，告诉用户「N 篇暂存于『<名称>』，不在文献库」，
   列出标题/venue/CCF，并说明“确认后我可以转入文献库或丢弃”。
5. **收尾**：用户挑完后 `promote_workspace_papers(workspace_id, paper_refs, collection_id=...)`
   转入（带集合则一并归档），其余 `discard_workspace_papers`；最后再 `get_workspace`
   确认 `pendingCount == 0`。工作区不再需要时用 `close_workspace`（尚未处理的论文可一并丢弃）。
6. **查看暂存论文**：`list_papers(workspace_id=...)` 或 `include_staged=true`；
   默认 `list_papers` / `search_fulltext` / 库级 `.bib` 导出都不会返回它们，
   但 `search_text(scope="workspace:<id>")` 和 `get_paper_text` **可以**读。

## 读论文：定位 → 读页 → 对齐用户标记

0. **先确认能不能读**：`list_files(paper_ref)` 看 `textStatus`。
   - `extracted` 可读；`pending` 稍等再试；
   - `failed` → `reindex_file(file_id)` 重试；
   - `ocr_needed` = 扫描件/纯图 PDF，后端不做 OCR，直接告诉用户"只能看 PDF 本身，
     库里没有可检索文字"；
   - 列表为空 = 没有 PDF，先走上面的 PDF acquisition。
1. **定位**：`search_text(q, scope=...)`。scope 的选择决定能不能看到调研工作区：
   - `library`（默认）只搜已入库论文；
   - `workspace:<id>` 搜整个调研工作区，**包含尚未转入的 staged 论文**；
   - `paper:<ref>` / `file:<id>` 单篇 / 单个 PDF。
   命中带 `paperId`/`fileId`/`page`/`snippet`；`context_chars` 调片段长度（默认 400），
   `max_per_paper` 防止一篇论文占满结果。
2. **读**：先 `get_paper_outline(paper_ref)` 看章节（有书签时能直接定位页码），
   再 `get_paper_text(paper_ref, page_from, page_to)` 读需要的几页。不带范围时默认
   **一次读完整篇**（窗口按页数计，默认 100 页），超过 100 页才截断，按 `nextPage` 续读。
   100 页约 30 万字符、很吃 token —— 长论文优先用 outline + 页范围。
3. **看图**：正文里出现 "see Figure 3 / Table 2 / Eq. (5)"，或数字/版式对不上时，
   用 `get_page_image(file_id, page)` 把那一页渲染成图再看。纯文本读不到图、表和公式，
   这一条是"像人一样读"的另一半。`dpi` 平时用 96–144，放大单个小图再提到 200+。
4. **对齐用户**：`get_annotations(paper_ref)` 拿到用户自己的高亮与批注。
   用户问"我之前标的那篇"或让你基于他关注的点总结时，先读批注再读正文。
   你写回的标注会出现在用户的阅读器里（`create_annotation`，rects 用 `{x,y,w,h}`，0–1）。

**先看规模**：动手前用 `library_stats()` 拿总量、未读分布和可读性
（`withPdf` / `textIndexed`）——「80 篇未读、只有 40 篇有正文」会直接改变你的做法。

**staged 不等于不可读**：调研工作区里的论文不在文献库，但 `get_paper_text`、
`search_text(scope="workspace:...")`、`get_annotations` 都读得到。列表/导出层面的
隔离，不是阅读层面的隔离——不要因为 `list_papers` 默认看不到就放弃读。

## Batches and parallelism

- **Identifier/title lists**: always ONE `import_papers(items=[...])` call —
  the backend processes them as a single job with per-item statuses; parallel
  agents add nothing here.
- **Many paper page URLs** (each `import_paper_with_pdf` is a synchronous
  page-fetch + metadata resolve + PDF download, seconds to tens of seconds
  each): when the harness provides subagent or workflow tools, fan out ONE
  subagent per paper to import concurrently. Each subagent: resolve → check
  the candidate matches → `import_paper_with_pdf` → return citekey, venue,
  CCF, duplicates. Without subagent support, import sequentially.
- After a parallel fan-out, verify the result with `list_papers` (count /
  newest entries) and summarize: imported, duplicates, failures.

## Verify before reporting success

After any import, report to the user: **citekey, title, venue (abbrev + full
name), CCF level/CAS zone, whether the PDF was stored**. Rules:

- Resolve first when there is any doubt. Check that the candidate's title
  matches the user's intent before importing; if it clearly differs or
  `confidence` is low, show the candidate and ask instead of importing.
- `duplicateOf` in resolve output is not an error — the paper is already in
  the library. Report the existing entry (citekey + link by id). Newly
  resolved identifiers are merged into the existing entry automatically.
- `suggestedDuplicateOf` (with a "possible duplicate in library" warning)
  means a fuzzy match: importing with the default `on_duplicate="link"`
  links to it (the item's `error` field carries the evidence note, e.g.
  "linked by exact-title+year(0)+first-author match; +2 identifier(s)
  merged"); pass `on_duplicate="create"` only when the user deliberately
  wants a separate entry. When `strong` is false, ask the user before
  importing.
- Import items with status `duplicate` or `skipped` may carry an
  informational `error` note (link evidence / identifier merge) — it is not
  a failure.
- A "conference paper" with no venue match lands as a provisional venue and
  the item is flagged for cleanup — mention this to the user.
- Import failures are per-item (retryable via the web UI); surface the
  `error` text, do not silently retry with a different query.

## PDF acquisition (paper in library, no file yet)

1. **`attach_pdf(paper_ref, url)` first** — the paper already exists, so this is
   the only path that cannot create a duplicate. Direct links: arXiv →
   `https://arxiv.org/pdf/<id>`, ACL → `https://aclanthology.org/<id>.pdf`.
2. `import_paper_with_pdf` with the landing-page URL also attaches the file,
   but it re-resolves metadata; use it when you are not sure the paper is in
   the library.
3. OpenAlex OA locations: `curl -s "https://api.openalex.org/works/doi:<doi>"`
   → `best_oa_location.pdf_url` (also `open_access.oa_url`), then `attach_pdf`.
4. Paywalled publisher sites (IEEE Xplore, ACM DL, Springer, Elsevier):
   do **not** try to scrape or authenticate. Tell the user to download the PDF
   themselves (institutional access) and then hand the file back for upload
   (path 4 above).

## 维护：升级 paperdb 的 MCP / skill

这套集成（这份 skill + 启动 `paperdb-mcp` 的 DSH adapter）随
`shaoyuanyu/ysy-paperdb` 的 GitHub Release 一起发布。**升级不在工具面上**——工具表
对宿主中立，安装/升级是本机 owner 动作，走 CLI：

```bash
paperdb adapter status   --host dsh            # 离线：已装版本、逐文件漂移
paperdb adapter check    --host dsh            # 已装 vs 最新 release
paperdb adapter update   --host dsh --totp 123456
paperdb adapter rollback --host dsh --totp 123456
```

规则：

- **写盘前必须向用户要当前 6 位 TOTP**（和删除类操作同一条规则，绝不臆造、复用或猜测）。
  用户问"有没有新版本"时只跑 `check`，不要主动 `update`。
- `--dry-run` 免验证码、不写盘，适合先展示将会改动哪些文件。
- `paperdb` 不在 PATH 时，用与 `paperdb-mcp` 同目录的那个可执行文件（DSH 下通常是
  `<repo>/.venv/bin/paperdb`）。
- 更新 bundle 后本轮会话仍跑旧工具，**告诉用户重启宿主**；skill 的改动对新会话生效。
- 安装目录按 `--dir` → `PAPERDB_ADAPTER_DIR` → 宿主旧变量（DSH 是
  `PAPERDB_DSH_INTEGRATION_DIR`）→ 宿主默认值解析。升级会覆盖本地对
  `SKILL.md` / `cordis.patch.yml` 的手改（旧文件在 `.paperdb-adapter/backups/`）。

## 更新提示协议：工具输出里的 `updateNotice`

服务器自己会检测新 release：查 GitHub 最多每 6 小时一次（`PAPERDB_MCP_UPDATE_TTL`
可调，0 关闭），结果缓存在 `~/.cache/paperdb/mcp/update-check.json`，**不是每次工具
调用都查**，检查也从不阻塞工具返回。有新版本且已装版本落后时（已装版本来自 bundle
渲染的 `PAPERDB_MCP_ADAPTER_VERSION`），下一个文本类工具结果会带提示：JSON 对象
结果末尾多一个 `updateNotice` 字段（含 `installed` / `latest` / `page` / `notes` /
`instructions`），数组或纯文本结果末尾多一行 `[paperdb-update] …`。每个服务器进程对
同一个 latest 版本只提示一次；`get_page_image` 这类返回图片的工具不带提示，会顺延到
下一个文本结果。**提示不是错误**——照常完成当前工具步骤，不要当失败处理。

收到提示后的协议（服务器提示的场景，用户没有主动要求更新）：

1. 完成当前工具步骤后**立即**处理，不要拖到任务结束。在回复文本里直接问用户：
   「paperdb 有新版本 `<latest>`（当前 `<installed>`），要现在更新吗？」可附
   `page` 链接和 `notes` 要点。**不要用 ask_user_question**（它会无限期阻塞）。
2. 询问后调用 `wait_agent(timeout_ms=60000)` 等待用户回复（最多 60 秒）：
   - 用户同意 → 走上面的 TOTP 更新流程（要当前 6 位码 → `paperdb adapter update
     --host dsh --totp <code>` → 提醒重启宿主）；
   - 用户拒绝 → 跳过，继续原任务，本会话不再问；
   - 超时（`timedOut: true`）→ 视为未回复，**直接跳过并继续原任务**，不要再问、
     不要再等；若用户稍后才回复同意，仍然可以执行更新。
3. 子代理（subagent / workflow fan-out）没有与用户对话的通道：收到提示只在汇报里
   提一句「检测到 paperdb 更新 `<latest>`」，等待与询问由主会话决定。
4. **用户主动要求的更新不受 60 秒限制**：「帮我更新 paperdb」「看看有没有新版本」
   时，先 `paperdb adapter check --host dsh`，再按用户意愿走 TOTP 更新流程——等用户
   提供验证码时可以一直等（这是用户发起的，不存在跳过）。

## Hard-won facts

- **DBLP is behind an anti-bot challenge** (Anubis, JS proof-of-work). The
  backend degrades gracefully to OpenAlex. Never fetch dblp.org pages
  directly from the agent.
- NDSS-style symposium pages have **no DOI and no `citation_*` meta tags**;
  the title search path handles them (og:title → OpenAlex), which is why
  `import_paper_with_pdf` works there.
- Landing pages of freshly accepted papers may not be in any aggregator yet;
  the backend still imports from the page itself (title/authors/PDF link,
  provisional venue). Warn that CCF rating may be missing until the venue
  matches.
- One paper per `import_paper` call; use `import_papers` for many.
- `import_paper_with_pdf` uses the PDF's own content to dedupe: identical bytes
  link to the existing paper, and the title is taken from the page layout (not
  from a proceedings running head), so ACL/NDSS PDFs no longer create
  junk entries.
- A research-workspace paper is hidden from `list_papers` / `search_fulltext` /
  library `.bib` export by default, but it is still **readable**: scoped search
  and `get_paper_text` reach staged papers. Importing a paper that is already
  in the library with `workspace_id` does **not** hide it — only new or already
  staged papers are staged.
- A PDF whose pages extracted to nothing is `ocr_needed`, not `extracted`:
  paperdb has no OCR, so an empty read is a property of the file, not a bug.
