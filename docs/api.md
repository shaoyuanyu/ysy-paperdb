# paperdb · API 契约

> 配套文档：[总览与决策](README.md) · [数据模型](data-model.md) · [前端集成](frontend.md)
> 状态：v0.1 待评审。实现以 FastAPI 生成的 OpenAPI 3.1 为准；本文是**设计契约**，不是最终字段清单。

---

## 1. 总则

| 项 | 约定 |
|---|---|
| Base URL | `https://library.shaoyuanyu.cn`（生产）；`http://127.0.0.1:9720`（本机） |
| 版本 | 路径版本 `/v1`；破坏性变更才升 `/v2` |
| 内容类型 | 请求/响应 `application/json; charset=utf-8`；文件上传 `multipart/form-data`；`.bib` 为 `application/x-bibtex` |
| 字段命名 | JSON 用 camelCase；数据库 snake_case；Pydantic alias 转换 |
| ID | UUID v4 字符串；论文也接受 citekey 作为 `{id}`（`GET /papers/yu2026x`） |
| 时间 | ISO 8601 UTC（`2026-11-24T13:05:00Z`）；前端按 `Asia/Shanghai` 展示 |
| 空值 | 缺省字段返回 `null`，不省略；数组返回 `[]` |
| 认证 | `Authorization: Bearer <token>`（`.bib` 导出除外，见 §5.17） |
| 文档 | `GET /openapi.json`；`/docs` 仅在 `PAPERDB_ENV=dev` 开启 |
| 追踪 | 响应头 `X-Request-Id`；日志同 ID |

---

## 2. 认证与令牌

### 2.1 三类 token

| 类型 | 前缀 | 归属 | 用途 | 生命周期 |
|---|---|---|---|---|
| service | `pdb_svc_` | 环境变量 `PAPERDB_SERVICE_TOKEN` | Next 服务端 → API；拥有全部 scope | 长期，轮换需重启 |
| personal | `pdb_pat_` | `api_token` 表 | 个人脚本、CLI、以后的前端/插件 | 可过期、可撤销 |
| export | `pdb_ext_` | `export_token` 表 | `.bib` URL，只读、限定范围 | 可撤销，URL 即凭证 |

- 后端**没有登录页、不发 cookie、不做 session**。
- 站点 `/[locale]/library/*` 的门槛由 Next 的 `requireOwner()`（TOTP）负责；浏览器永远拿不到 service token。
- 个人 token 由 service token（`admin` scope）通过 `POST /v1/tokens` 创建，明文只返回一次。
- export token 由 `POST /v1/export-tokens` 创建，只能访问 `/v1/exports/bib/{token}.bib`。

### 2.2 请求头

```http
Authorization: Bearer pdb_pat_7f3a9c2e_<32-byte-secret>
X-Request-Id: 可选，客户端自带
Idempotency-Key: 可选，仅 POST /papers、POST /imports/*
If-Match: "3"            # 可选，乐观锁
```

### 2.3 存储与校验

- 明文 token = `前缀 + 8 位随机 prefix + "_" + 32 字节 base32 secret`。
- 只存 `sha256(secret + PAPERDB_TOKEN_PEPPER)`，比较用 `hmac.compare_digest`。
- 前缀用于索引定位，前缀冲突即拒绝创建。
- 日志/审计只记 `prefix`，绝不记明文。
- `last_used_at` 每次成功认证更新（可合并为异步，避免写放大）。

### 2.4 Scope 与端点

| Scope | 允许 |
|---|---|
| `papers:read` | 所有 GET（papers/authors/venues/collections/tags/views/projects/citations/search/meta） |
| `papers:write` | papers/identifiers/artifacts/relations/state 的写操作、resolve、imports |
| `organize:write` | collections/tags/views/projects/auto-rules 的写操作 |
| `pdf:read` | 文件元数据与 bytes |
| `pdf:write` | 上传/删除/重抽取 |
| `annotations:write` | 批注写操作 |
| `exports:read` | 直接下载 `.bib`/CSL-JSON、创建 export token |
| `admin` | token 管理、数据集导入、全量重建索引、job 管理 |

- service token 隐式拥有全部 scope。
- 个人 token 默认 `papers:read,papers:write,organize:write,pdf:read,pdf:write,annotations:write,exports:read`；CLI 可只发 `papers:read,exports:read`。
- 403 返回 `code=insufficient_scope`，并在 `detail` 里给出缺少的 scope。

### 2.5 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `PAPERDB_DATABASE_URL` | ✓ | `postgresql+asyncpg://...` |
| `PAPERDB_SERVICE_TOKEN` | ✓ | `pdb_svc_...`，至少 32 字节随机 |
| `PAPERDB_TOKEN_PEPPER` | ✓ | 32 字节随机；轮换会让旧 token 全失效 |
| `PAPERDB_STORAGE_BACKEND` | ✓ | `local` / `s3` / `oss` |
| `PAPERDB_STORAGE_ROOT` | local 必填 | 如 `/data/files` |
| `PAPERDB_MAX_UPLOAD_MB` | | 默认 200 |
| `PAPERDB_CORS_ORIGINS` | | 逗号分隔；默认空 = 关闭 CORS |
| `PAPERDB_PUBLIC_URL` | | 生成 `.bib` 私密 URL 的基地址，如 `https://library.shaoyuanyu.cn` |
| `PAPERDB_ENV` | | `dev` / `prod` |
| `CROSSREF_MAILTO` | 建议 | Crossref polite pool |
| `OPENALEX_MAILTO` | 建议 | OpenAlex polite pool |
| `S2_API_KEY` | | 没有则 1 rps |
| `ARXIV_USER_AGENT` | 建议 | 带联系邮箱 |
| `S3_*` / `OSS_*` | | 切云存储时用 |

---

## 3. 通用协议

### 3.1 分页

- 列表统一 `limit`（1–200，默认 50）+ `cursor`。
- 响应：

```json
{
  "items": [ ... ],
  "nextCursor": "eyJrIjpbMTczMDAwMDAwMCwiOWY...",
  "hasMore": true
}
```

- `cursor` 是 base64url 的不透明串（内含排序键 + id，服务端校验）；客户端不得解析或构造。
- 需要总数时显式 `count=true`（代价高，列表页默认不请求；侧边栏用 `/v1/views/{id}/count`）。

### 3.2 筛选与排序

- 简单参数：`q`、`view`、`collection`、`sub`、`tag`、`status`、`ccf`、`cas`、`yearFrom`、`yearTo`、`kind`、`venueId`、`authorId`、`hasPdf`、`unfiled`、`hasCode`、`starred`、`sort`。
- `q` **始终生效**（标题/摘要/作者等**元数据**，不含 PDF 正文）；`filter` 只接管其余结构化参数，二者可同时使用。PDF 正文搜索走 `GET /v1/search/fulltext`。
- `collection` 默认含子集合（`sub=0` 时只看该集合本身）；`view` 与 `filter` 互斥，`view` 优先取其保存的条件。
- 复杂条件：`filter=<base64url(JSON)>`（无 padding），见 §6；与其余简单结构化参数同时出现时 **filter 优先**，但这些参数不报错、直接忽略。服务端解码后校验 JSON，上限 8 KB；URL 用 base64url 避免转义/长度问题。
- `sort` 语法：`field:asc|desc`，多字段逗号分隔；允许字段：`createdAt`、`updatedAt`、`year`、`title`、`citekey`、`venue`、`ccf`、`cas`、`rating`、`priority`、`citedBy`。默认 `createdAt:desc,id:desc`。

### 3.3 错误

`application/problem+json`，RFC 9457：

```json
{
  "type": "https://paperdb.local/errors/validation",
  "title": "Validation failed",
  "status": 422,
  "detail": "filter.rules[2].field is not filterable",
  "instance": "/v1/papers",
  "code": "invalid_filter",
  "errors": [{"path": "filter.rules[2].field", "message": "not filterable"}],
  "requestId": "01J..."
}
```

| HTTP | code | 场景 |
|---|---|---|
| 400 | `bad_request` | 参数无法解析 |
| 401 | `unauthorized` | 缺 token / token 无效 / 已撤销 |
| 403 | `insufficient_scope` | scope 不足 |
| 404 | `not_found` | 资源不存在（含撤销的 export token） |
| 405 | `method_not_allowed` | |
| 409 | `version_conflict` / `duplicate` | 乐观锁 / 标识符冲突 |
| 410 | `gone` | 已删除且不可恢复 |
| 413 | `payload_too_large` | 上传超限 |
| 415 | `unsupported_media_type` | 非 PDF / 非 multipart |
| 422 | `validation_failed` / `invalid_filter` / `invalid_citekey` | |
| 429 | `rate_limited` | 带 `Retry-After` |
| 500 | `internal` | 不暴露 SQL/堆栈 |
| 502 | `upstream_failed` | 元数据源不可达（仅 resolve/import） |
| 503 | `not_ready` | DB/存储未就绪 |

### 3.4 并发

- `paper.version` 整数，每次写 +1。
- 请求可带 `If-Match: "3"`；不匹配 → 409 `version_conflict`，响应带当前 `version`。
- 不传 `If-Match` 时 last-write-wins（单用户可接受），但 import/merge 内部仍用行锁。

### 3.5 幂等

- `POST /v1/papers`、`POST /v1/imports/*` 支持 `Idempotency-Key`。
- 服务端存 `idempotency_key(actor, key)` → 响应快照 24h；`actor` = token prefix 或 `svc`，并记录 request hash。重复请求返回同一响应（含 `Idempotency-Replayed: true`）；同 key 不同 body → 409 `idempotency_conflict`。
- `resolve` 是只读的，不需要幂等键。

### 3.6 限流

| 范围 | 默认 |
|---|---|
| 普通 API | 600 req/min/token |
| `POST /v1/resolve` | 30 req/min/token |
| 上传 | 10 req/min/token |
| `.bib` 导出 | 120 req/min/export-token |
| 单 IP | Nginx 层 `limit_req` 兜底 |

超限返回 429 + `Retry-After`（秒）。限流状态不持久化；进程重启清零可接受。

---

## 4. 资源表示

### 4.1 PaperSummary（列表项）

```json
{
  "id": "0f1e...",
  "kind": "conference",
  "title": "Interpretability of ...",
  "citekey": "yu2026interpretability",
  "year": 2026,
  "venue": {
    "id": "...",
    "kind": "conference",
    "abbrev": "NeurIPS",
    "fullName": "Conference on Neural Information Processing Systems",
    "edition": {"id": "...", "year": 2026, "track": "main"}
  },
  "evaluation": {
    "ccf": {"level": "A", "field": "人工智能", "datasetVersion": "2026", "fallback": false},
    "cas": null
  },
  "authors": [
    {"id": "...", "displayName": "Shaoyuan Yu", "position": 1, "isMe": true},
    {"id": "...", "displayName": "Jane Doe", "position": 2, "isMe": false}
  ],
  "authorCount": 4,
  "state": {"status": "queued", "priority": 2, "rating": null, "starred": true},
  "counts": {"pdfFiles": 1, "annotations": 12, "citedBy": 34},
  "tags": ["topic/llm", "status:needs-repro"],
  "collections": ["...uuid..."],
  "createdAt": "2026-11-01T08:00:00Z",
  "updatedAt": "2026-11-20T10:00:00Z",
  "version": 3
}
```

- `venue.abbrev` 可能为 `null`（CCF 46 条空缩写）；前端展示与 BibTeX/citekey 必须回退 `fullName` / `shortid`，不得输出空字符串。

### 4.2 PaperDetail（详情）

在 Summary 基础上增加：

```json
{
  "abstract": "...",
  "publicationDate": "2026-12-10",
  "volume": null, "issue": null, "pages": "1--15",
  "institution": null,
  "language": "en",
  "authorKeywords": ["interpretability"],
  "userLockedFields": ["title", "venueId"],
  "urls": {"project": "https://...", "slides": "https://..."},
  "identifiers": [
    {"scheme": "doi", "value": "10.xxxx/yyy", "isPrimary": true},
    {"scheme": "arxiv", "value": "2601.01234", "isPrimary": false}
  ],
  "authors": [
    {"id": "...", "displayName": "Shaoyuan Yu", "familyName": "Yu", "givenName": "Shaoyuan",
     "position": 1, "isCorresponding": true, "equalContribution": false,
     "affiliation": "Peking University", "orcid": null, "isMe": true}
  ],
  "venue": {"id": "...", "kind": "conference", "abbrev": "NeurIPS", "fullName": "...",
            "edition": {"id": "...", "year": 2026, "track": "main"},
            "ccfField": "人工智能", "ccfFieldKey": "人工智能"},
  "artifacts": [
    {"id": "...", "kind": "code", "url": "https://github.com/...", "host": "github",
     "label": "官方实现", "isOfficial": true}
  ],
  "relations": [
    {"fromPaperId": "...", "toPaperId": "...", "type": "preprint_of", "source": "auto"}
  ],
  "files": [
    {"id": "...", "isPrimary": true, "byteSize": 7340032, "pageCount": 18,
     "textStatus": "extracted", "originalFilename": "paper.pdf"}
  ],
  "citationCounts": {"openalex": 34, "s2": 31, "asOf": "2026-11-20"}
}
```

### 4.3 其他对象

- `Venue`：`id, kind, abbrev (可为 null), fullName, parentId, dblpKey, issnPrint, issnOnline, homepageUrl, ccfField, ccfFieldKey, status, identifiers[], aliases[], editions[], evaluation`。
- `Edition`：`id, venueId, year, ordinal, track, dblpKey, location, startDate, endDate, websiteUrl`。
- `Rank`：`venueId, scheme, datasetVersion, fieldKind, fieldName, fieldNameEn, fieldKey, level, isTop, isPrimary, rankInField, totalInField`。
- `Author`：`id, displayName, familyName, givenName, orcid, dblpPid, s2AuthorId, openalexId, homepageUrl, isMe, aliases[], paperCount`。
- `Collection`：`id, parentId, name, slug, description, position, paperCount, children[]`（tree 端点）。
- `Tag`：`id, path, namespace, name, parentId, color, paperCount, children[]`。
- `SavedView`：`id, name, filter, sort, columns, viewMode, isPinned, position, count?`。
- `Project`：`id, name, description, citekeyTemplate, exportIncludeAbstract, collections[], pinnedPapers[], paperCount`。
- `Annotation`：`id, paperId, pdfFileId, kind, page, rects, quote, prefix, suffix, comment, color, anchorStatus, createdAt, updatedAt`。

---

## 5. 端点

> 下表 `R` = 需要 `papers:read`，`W` = `papers:write`，`O` = `organize:write`，`P` = `pdf:read/write`，`A` = `annotations:write`，`X` = `exports:read`，`AD` = `admin`。所有端点默认还要有效 token。
>
> **路由声明顺序**：FastAPI 按注册顺序匹配。`/papers/bulk`、`/papers/bulk/state`、`/tags/tree`、`/collections/tree`、`/views/validate`、`/venues/editions/{editionId}` 这类静态段必须写在对应的 `/{id}` 之前，否则会被当作 id。

### 5.1 系统

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/healthz` | — | 进程存活，无 DB 依赖 |
| GET | `/readyz` | — | DB + 存储可用性 |
| GET | `/v1/meta` | R | 版本、数据集、计数、存储用量、job backlog |
| GET | `/v1/library/stats` | R | 文献库一览：总量、阅读状态分布、可读性（有 PDF / 已抽文本）、集合/标签/项目/工作区计数 |
| GET | `/openapi.json` | — | OpenAPI 3.1 |

`GET /v1/library/stats` 响应示例（agent 用来"一眼看清库里有什么"）：

```json
{
  "papers": 128,
  "staged": 12,
  "withPdf": 96,
  "textIndexed": 90,
  "byStatus": {"unread": 80, "queued": 6, "reading": 2, "read": 34, "skimmed": 6},
  "collections": 9,
  "tags": 42,
  "projects": 3,
  "workspaces": 3
}
```

`papers` 只统计已入库（`staged=false`、未删）；staged 论文单列在 `staged`。
`withPdf` / `textIndexed` 是"可读性"的两个口径——有 PDF、且文本已抽取。

`GET /v1/meta` 响应示例：

```json
{
  "version": "0.1.0",
  "datasets": [
    {"scheme": "ccf", "version": "2026", "label": "CCF 推荐目录 2026（第七版）",
     "sourceUrl": "https://...", "checksum": "sha256:...", "importedAt": "..."},
    {"scheme": "cas", "version": "2025", "label": "中科院分区 2025 升级版",
     "sourceUrl": "https://...", "checksum": "sha256:...", "importedAt": "..."}
  ],
  "counts": {"papers": 10342, "venues": 1204, "authors": 48771, "files": 8210},
  "venues": {"unmatched": 81, "missingIssn": 81},
  "storage": {"backend": "local", "bytesUsed": 73400320000, "files": 8210, "blobs": 8000},
  "jobs": {"pending": 3, "running": 1, "failed24h": 0}
}
```

- `storage.bytesUsed` 必须按 `(storage_backend, storage_key)` 去重求和；`files` 是 `pdf_file` 行数，`blobs` 是不同物理文件数。
- `venues.unmatched` / `missingIssn` 来自 CCF↔CAS 归并报告，设置页用它展示"还有多少待 crosswalk"，**不通过 `/admin` 上传**（CSV 用 CLI 导入）。

### 5.2 解析与入库

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v1/resolve` | W | 只读预览：输入 DOI/arXiv/URL/标题，返回候选与冲突 |
| POST | `/v1/papers` | W | 创建（可直接用 resolve 结果） |
| GET | `/v1/papers` | R | 列表/搜索/视图；默认排除调研工作区中暂存的论文（`includeStaged=true` 或 `workspace=<id>` 可指定） |
| GET | `/v1/papers/{id}` | R | 详情（uuid 或 citekey） |
| PATCH | `/v1/papers/{id}` | W | 局部更新 |
| DELETE | `/v1/papers/{id}` | W | 软删 |
| POST | `/v1/papers/{id}/restore` | W | 恢复 |
| POST | `/v1/papers/{id}/merge` | W | 合并到另一篇 |
| POST | `/v1/papers/{id}/enrich` | W | 重新拉取元数据（`{sources?, overwriteLocked?}`），异步 job；默认不覆盖 `userLockedFields` |
| POST | `/v1/papers/bulk/enrich` | W | `{ids[]}` 批量重新拉取，上限 500 |
| GET | `/v1/papers/{id}/duplicates` | R | 疑似重复候选 |
| PATCH | `/v1/papers/bulk` | W | 批量更新（状态/标签/集合/字段）。**路由声明必须在 `/{id}` 之前**，否则 `bulk` 会被当作 id |
| POST | `/v1/papers/bulk/delete` | W | `{ids[]}` 软删，上限 500；返回逐条结果 |
| GET | `/v1/papers/{id}/bibtex` | R | 单条 BibTeX 文本 |
| GET | `/v1/papers/{id}/identifiers` | R | 标识符列表 |
| POST | `/v1/papers/{id}/identifiers` | W | 添加标识符 |
| PATCH | `/v1/papers/{id}/identifiers/{identifierId}` | W | `{isPrimary?, value?}`；设主时同 scheme 其他行自动取消 |
| DELETE | `/v1/papers/{id}/identifiers/{identifierId}` | W | 删除标识符 |
| PUT | `/v1/papers/{id}/authors` | W | **整体替换**作者列表与顺序（不是 PATCH） |
| GET | `/v1/papers/{id}/artifacts` | R | artifact 列表 |
| POST | `/v1/papers/{id}/artifacts` | W | 添加 artifact |
| PATCH | `/v1/artifacts/{artifactId}` | W | 改 label/official |
| DELETE | `/v1/artifacts/{artifactId}` | W | 删除 artifact |
| GET | `/v1/papers/{id}/relations` | R | 版本链关系 |
| POST | `/v1/papers/{id}/relations` | W | 建立关系（预印本↔正式版） |
| DELETE | `/v1/papers/{id}/relations/{relationId}` | W | 删除关系 |

> 作者、标识符、artifact 不走 `PATCH /papers/{id}` 的嵌套写：它们各自是子资源，避免"读-改-写整篇论文"把并发编辑吞掉。`PUT /authors` 是**替换**语义，请求体必须给出完整有序列表：

```json
{
  "authors": [
    {"authorId": "...", "position": 1, "isCorresponding": true, "affiliation": "PKU"},
    {"displayName": "Jane Doe", "position": 2, "equalContribution": true}
  ]
}
```

合并请求体：

```json
{
  "into": "<survivorPaperId>",
  "fields": {"title": "source", "abstract": "target", "venue": "target"},
  "deleteSource": true
}
```

- `fields` 不传时按"目标优先、空值补源"的默认策略。
- 合并完成后，旧 ID 的 `GET /papers/{oldId}` 返回 **308** + `Location: /v1/papers/{newId}`；`merge_redirect` 记录映射，不物理删源行。

`POST /v1/resolve` 请求：

```json
{
  "input": "https://arxiv.org/abs/2601.01234",
  "kind": "auto",
  "options": {"withCitations": false, "preferredSources": ["crossref", "arxiv", "dblp"]}
}
```

响应：

```json
{
  "input": {"raw": "...", "kind": "arxiv", "normalized": "2601.01234"},
  "candidates": [
    {
      "confidence": 0.97,
      "sources": ["arxiv", "dblp", "openalex"],
      "paper": {
        "kind": "preprint",
        "title": "...",
        "abstract": "...",
        "year": 2026,
        "authors": [{"displayName": "Shaoyuan Yu", "dblpPid": "...", "orcid": null}],
        "identifiers": [
          {"scheme": "arxiv", "value": "2601.01234", "isPrimary": true},
          {"scheme": "dblp", "value": "journals/corr/abs-2601-01234"}
        ],
        "venue": null,
        "venueSuggestion": {"venueId": "...", "abbrev": "NeurIPS", "year": 2026, "track": "main",
                            "confidence": 0.9, "evidence": "dblp"}
      },
      "evaluation": {"ccf": {"level": "A", "field": "人工智能", "datasetVersion": "2026"}, "cas": null},
      "duplicateOf": null,
      "warnings": ["arXiv 预印本，未检测到正式发表版本"]
    }
  ]
}
```

`duplicateOf` 非空时，前端应提示"已在库中"并给跳转；确认后仍可创建（用于版本链）。

`POST /v1/papers` 最小请求：

```json
{
  "kind": "conference",
  "title": "...",
  "abstract": "...",
  "year": 2026,
  "citekey": "yu2026interpretability",
  "venueId": "...",
  "venueEditionId": "...",
  "authors": [
    {"authorId": "...", "position": 1, "isCorresponding": true},
    {"displayName": "Jane Doe", "position": 2, "affiliation": "MIT"}
  ],
  "identifiers": [{"scheme": "doi", "value": "10.xxxx/yyy", "isPrimary": true}],
  "artifacts": [{"kind": "code", "url": "https://github.com/..."}],
  "state": {"status": "queued", "priority": 2},
  "collectionIds": ["..."],
  "tags": ["status:needs-review"]
}
```

- `venueId` 与 `venueEditionId` 必须一致；只给 `venueEditionId` 时自动推导 `venueId`。
- 作者可给 `authorId`（已存在）或 `displayName`（创建 provisional 作者）。
- 未给 `citekey` 时按模板生成；冲突自动加后缀，响应里返回最终值。
- 标识符与库内已有论文冲突 → 409 `duplicate`，响应带 `existingPaperId`。
- `PATCH /papers/{id}` 修改的字段自动加入 `userLockedFields`；`enrich` 默认跳过这些字段，需显式 `overwriteLocked=true` 才覆盖。

### 5.3 作者

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/authors` | R | `q`、`isMe`、分页 |
| POST | `/v1/authors` | W | 手动创建作者（M1；`isMe` 唯一） |
| GET | `/v1/authors/{id}` | R | 详情 + 统计 |
| PATCH | `/v1/authors/{id}` | W | 改名/加 ID/设 `isMe` |
| POST | `/v1/authors/{id}/merge` | W | 合并 |
| GET | `/v1/authors/{id}/papers` | R | 该作者论文（按年份倒序） |
| POST | `/v1/authors/{id}/aliases` | W | 加别名 |
| DELETE | `/v1/authors/{id}/aliases/{aliasId}` | W | 删别名 |

### 5.4 venue / edition / 评价

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/venues` | R | `q`、`kind`、`ccf`、`cas`、`parentId` |
| POST | `/v1/venues` | W | 手动建（含 alias） |
| GET | `/v1/venues/{id}` | R | 含 editions 与 evaluation |
| PATCH | `/v1/venues/{id}` | W | |
| POST | `/v1/venues/{id}/merge` | W | 合并重复 venue |
| GET | `/v1/venues/{id}/editions` | R | |
| POST | `/v1/venues/{id}/editions` | W | |
| PATCH | `/v1/venues/editions/{editionId}` | W | |
| GET | `/v1/venues/{id}/ranks` | R | `scheme`、`datasetVersion` |
| GET | `/v1/venues/{id}/papers` | R | |
| GET | `/v1/venues/{id}/aliases` | R | |
| POST | `/v1/venues/{id}/aliases` | W | |
| DELETE | `/v1/venues/{id}/aliases/{aliasId}` | W | |
| GET | `/v1/rank-datasets` | R | 已导入的数据集版本 |
| POST | `/v1/admin/datasets/import` | AD | 导入/替换数据集 |

`GET /v1/venues/{id}` 响应节选：

```json
{
  "id": "...",
  "kind": "journal",
  "abbrev": "TPAMI",
  "fullName": "IEEE Transactions on Pattern Analysis and Machine Intelligence",
  "dblpKey": "journals/pami",
  "ccfField": "人工智能",
  "status": "confirmed",
  "identifiers": [
    {"source": "dblp", "value": "journals/pami", "isPrimary": true},
    {"source": "issn", "value": "0162-8828", "isPrimary": false}
  ],
  "editions": [],
  "evaluation": {
    "ccf": {"level": "A", "datasetVersion": "2026", "field": "人工智能", "fieldKey": "人工智能"},
    "cas": {"level": "1", "isTop": true, "datasetVersion": "2025",
            "majorField": "计算机科学", "minorFields": [
              {"fieldName": "计算机：人工智能", "fieldNameEn": "Computer Science, Artificial Intelligence",
               "level": "1", "rank": 3, "total": 196}
            ]}
  },
  "paperCount": 128
}
```

### 5.5 集合

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/collections/tree` | R | 返回整棵树 + 计数 |
| POST | `/v1/collections` | O | `{parentId?, name, description?}` |
| PATCH | `/v1/collections/{id}` | O | 改名/描述 |
| DELETE | `/v1/collections/{id}` | O | `mode=promote|discard` |
| POST | `/v1/collections/{id}/move` | O | `{parentId, position}` |
| POST | `/v1/collections/{id}/papers` | O | `{paperIds[], position?}` 批量加入 |
| DELETE | `/v1/collections/{id}/papers` | O | `{paperIds[]}` 批量移除 |
| POST | `/v1/collections/{id}/reorder` | O | `{paperIds[]}` 手动排序 |

### 5.6 标签

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/tags/tree` | R | `namespace` 过滤 |
| POST | `/v1/tags` | O | `{path, color?, description?}` |
| PATCH | `/v1/tags/{id}` | O | 改展示 `name`/`color`/`description`；**`path` 不可变**（视图/规则引用它） |
| POST | `/v1/tags/{id}/merge` | O | `{intoTagId}`；迁移 paper_tag 并重写 saved_view/auto_rule 中的路径引用 |
| DELETE | `/v1/tags/{id}` | O | 移除标签与关联 |

### 5.7 智能视图与自动规则

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/views` | R | 列表 |
| POST | `/v1/views` | O | 创建 |
| GET | `/v1/views/{id}` | R | |
| PATCH | `/v1/views/{id}` | O | |
| DELETE | `/v1/views/{id}` | O | |
| POST | `/v1/views/validate` | R | 校验 filter DSL，不落库 |
| GET | `/v1/views/{id}/count` | R | 快速计数 |
| GET | `/v1/auto-rules` | R | |
| POST | `/v1/auto-rules` | O | `{name, filter, actions, runOn}` |
| PATCH | `/v1/auto-rules/{id}` | O | |
| DELETE | `/v1/auto-rules/{id}` | O | |
| POST | `/v1/auto-rules/{id}/apply` | O | `{paperIds?}`（≤500，同步）或省略=全库回放（异步 job，返回 jobId） |

### 5.8 项目

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/projects` | R | |
| POST | `/v1/projects` | O | `{name, description?, citekeyTemplate?}` |
| GET | `/v1/projects/{id}` | R | 含集合、置顶、计数 |
| PATCH | `/v1/projects/{id}` | O | |
| DELETE | `/v1/projects/{id}` | O | |
| POST | `/v1/projects/{id}/collections` | O | `{collectionIds[]}` |
| DELETE | `/v1/projects/{id}/collections` | O | `{collectionIds[]}` |
| POST | `/v1/projects/{id}/papers` | O | `{paperIds[]}` 置顶/追加 |
| DELETE | `/v1/projects/{id}/papers` | O | `{paperIds[]}` |
| GET | `/v1/projects/{id}/papers` | R | 并集分页（含子集合） |
| POST | `/v1/projects/{id}/export-tokens` | X | 创建该项目的 `.bib` token |

### 5.8a 调研工作区（Research workspace，暂存区）

批量调研的落点：工作区里的论文 `paper.staged=true`，默认不出现在文献库列表/搜索/`.bib` 导出中；
调研结束后逐篇 promote（转入文献库，可同时加入集合）或 discard（软删）。

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/workspaces` | R | 打开的工作区 + 计数（`include_closed=true` 含已关闭） |
| POST | `/v1/workspaces` | O | `{name, description?}`；打开状态下名称唯一 |
| GET | `/v1/workspaces/{id}` | R | 详情 + 论文（`pending_only`、`limit`） |
| PATCH | `/v1/workspaces/{id}` | O | `{name?, description?}` |
| GET | `/v1/workspaces/{id}/papers` | R | 紧凑论文列表 |
| POST | `/v1/workspaces/{id}/papers` | O | `{paperRefs[]}` 暂存（staged） |
| POST | `/v1/workspaces/{id}/promote` | O | `{paperRefs[], collectionId?}` 转入文献库 |
| POST | `/v1/workspaces/{id}/discard` | O | `{paperRefs[]}` 丢弃（暂存论文软删） |
| POST | `/v1/workspaces/{id}/close` | O | `{discardUnpromoted?}` 关闭工作区 |

导入时通过 `options.defaultWorkspaceId` 直接落入工作区（论文创建即 staged）；已存在于文献库的论文不会被隐藏。

### 5.9 阅读状态与批量

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/papers/{id}/state` | R | 缺省返回默认值 |
| PATCH | `/v1/papers/{id}/state` | W | `{status?, priority?, rating?, progressPage?, starred?, archived?}` |
| PATCH | `/v1/papers/bulk/state` | W | `{ids[], patch}` |

`PATCH /v1/papers/bulk` 请求：

```json
{
  "ids": ["...", "..."],
  "patch": {
    "addTags": ["topic/llm"],
    "removeTags": ["status:stale"],
    "addCollections": ["..."],
    "removeCollections": ["..."],
    "set": {"year": 2025, "kind": "conference"},
    "state": {"status": "read", "rating": 5}
  }
}
```

响应：

```json
{"updated": 18, "failed": [{"id": "...", "code": "not_found"}]}
```

批量上限 500；超过请拆批。字段 `set` 只允许白名单（title/abstract/year/kind/venueId/venueEditionId/volume/issue/pages/urls），改 citekey 必须走单条 PATCH。

### 5.10 文件

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v1/papers/{id}/files` | P | multipart，字段 `file`、`isPrimary?` |
| POST | `/v1/papers/{id}/files/from-url` | P | JSON `{url, filename?}`；服务端下载 PDF 挂到该论文（不改元数据，不会新建条目） |
| GET | `/v1/papers/{id}/files` | P | 文件列表 |
| GET | `/v1/files/{fileId}` | P | 元数据 |
| GET | `/v1/files/{fileId}/content` | P | PDF 字节，支持 `Range` |
| DELETE | `/v1/files/{fileId}` | P | 软删；若是主文件则自动提升下一个；返回 `{deleted, primaryPromotedTo, gcJobId}`，物理 blob 由 `gc_blob` 延迟 7 天回收 |
| POST | `/v1/files/{fileId}/primary` | P | 设为主文件 |
| GET | `/v1/files/{fileId}/pages/{page}/text` | P | 单页文本 |
| GET | `/v1/files/{fileId}/pages/{page}/image` | P | 单页渲染为 PNG，`dpi` 72–300（默认 144）；读图表/公式/版式 |
| GET | `/v1/files/{fileId}/outline` | P | PDF 书签树（章节标题 + 页码）；无书签返回空数组，不是错误 |
| POST | `/v1/files/{fileId}/reindex` | P | 重新抽取（异步 job） |

上传响应：

```json
{
  "id": "...",
  "paperId": "...",
  "sha256": "...",
  "byteSize": 7340032,
  "pageCount": null,
  "isPrimary": true,
  "textStatus": "pending",
  "jobId": "..."
}
```

`GET /v1/files/{id}/outline` 返回 `{fileId, pageCount, sections:[{level,title,page}]}`。
**空 `sections` 是正常结果**——大多论文没有书签；调用方应回退到全文检索定位章节，
不要把它当成解析失败。

`GET /v1/files/{id}/pages/{page}/image` 用于读出文本层丢掉的东西（图、表、公式、双栏版式）：

- 返回 `image/png`，`dpi` 72–300；`page` 越界或 PDF 加密返回 422 `invalid_page`；
- 鉴权用 `pdf:read`，与 `/content` 相同，不允许匿名访问。

`GET /v1/files/{id}/content` 必须支持：

- `Range: bytes=0-65535` → 206 + `Content-Range` + `Accept-Ranges: bytes`；
- `HEAD` 返回 `Content-Length`；
- 响应头 `Content-Type: application/pdf`、`Content-Disposition: inline`、`X-Content-Type-Options: nosniff`、`Content-Security-Policy: sandbox`、`Cache-Control: private, no-store`；
- 不允许任何匿名访问；Next 代理必须原样转发这些头（安全加固）。

### 5.11 批注

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/papers/{id}/annotations` | R | `page` 过滤；按页/创建时间排序 |
| POST | `/v1/papers/{id}/annotations` | A | |
| PATCH | `/v1/annotations/{id}` | A | 改 comment/color/rects |
| DELETE | `/v1/annotations/{id}` | A | 软删 |

创建请求：

```json
{
  "pdfFileId": "...",
  "kind": "highlight",
  "page": 5,
  "rects": [{"x": 0.112, "y": 0.234, "w": 0.53, "h": 0.018}],
  "quote": "We propose ...",
  "prefix": "In this paper, ",
  "suffix": " and evaluate ...",
  "comment": "核心方法在这里",
  "color": "yellow"
}
```

- `rects` 是相对页宽高的 0–1 浮点，最多 64 个矩形。
- `quote` 上限 4000 字符；`comment` 上限 10000。
- 同一页重复高亮不合并，由用户决定。

### 5.12 引用与相关

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/papers/{id}/references` | R | 论文引用的作品（库内 + stub），分页 |
| GET | `/v1/papers/{id}/citations` | R | 引用该论文的作品 |
| GET | `/v1/papers/{id}/related` | R | 共引/耦合/同 venue 的 Top N |
| POST | `/v1/citations/sync` | AD | `{paperIds?}`，异步 job |

`GET /v1/papers/{id}/references` 响应项：

```json
{
  "inLibrary": true,
  "paper": {"id": "...", "title": "...", "year": 2024, "citekey": "..."},
  "external": null,
  "source": "openalex"
}
```

```json
{
  "inLibrary": false,
  "paper": null,
  "external": {
    "id": "...", "title": "...", "year": 2019,
    "doi": "10.xxxx/...", "openalexId": "W...", "citedByCount": 120
  },
  "source": "openalex"
}
```

### 5.13 导入与 job

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v1/imports/identifiers` | W | `{items:[...], options?}`；item 可以是 DOI/arXiv/URL/标题（URL 会抓落地页提取标识符/标题/PDF 链接） |
| POST | `/v1/imports/bibtex` | W | multipart `file` |
| POST | `/v1/imports/pdf` | W | multipart `file` + 可选 `paperId` |
| POST | `/v1/imports/pdf-url` | W | `{url, filename?}`：落地页或直链 PDF → 论文 + PDF；无可达 PDF 时仅导入元数据并带 warning |
| GET | `/v1/imports` | R | 最近任务 |
| GET | `/v1/imports/{id}` | R | 状态与统计 |
| GET | `/v1/imports/{id}/items` | R | 逐条状态，分页 |
| POST | `/v1/imports/{id}/items/{itemId}/retry` | W | 单条重试 |
| POST | `/v1/imports/{id}/cancel` | W | |
| GET | `/v1/jobs/{id}` | R | 通用 job 状态 |

`resolve` 候选与 `POST /v1/imports/pdf-url` 的输入约定：任意论文落地页 URL（会议/期刊/仓库页）
走 `ingest/page.py` 的提取阶梯——`citation_*` meta → og:title（剥站名后缀）→ `<title>`/H1 →
正文 DOI/arXiv 正则与 `.pdf` 链接收割；页面元数据只作为识别桥与最后回退（`source="page"`，
字段优先级最低），权威字段仍来自聚合源。候选新增 `pdfUrls` 字段（页面上发现的开放获取 PDF
链接，按优先级排序）。

### 5.14 破坏性操作的 TOTP 门控

实体级删除与合并（`DELETE /v1/papers/{ref}`、`POST /v1/papers/{ref}/merge`、
`DELETE /v1/files/{id}`、`DELETE /v1/collections/{id}`、`DELETE /v1/tags/{id}`、
`DELETE /v1/views/{id}`、`DELETE /v1/projects/{id}`、`DELETE /v1/tokens/{id}`）对
**个人令牌**（agent 路径）要求请求头 `X-TOTP-Code: <6 位验证码>`：

- 验证参数与站点登录一致（RFC 6238，SHA1/6 位/30 秒，±1 步窗口）；密钥经
  `PAPERDB_TOTP_SECRET` 注入（与站点 `TOTP_SECRET` 同值）。
- **service token（Next 服务端代理）豁免**：其密钥只存在于服务端，不经手模型。
- 密钥未配置时 fail-closed（503 `totp_not_configured`）；缺码 428 `totp_required`；
  错误/过期 403 `totp_invalid`；连续 5 次失败锁定 15 分钟。
- 关联级移除（`DELETE /papers/{ref}/tags`、`/collections`、项目成员）不设门：可重加，
  无数据损失。
- 论文/合集删除本身是软删（`deleted_at` + restore 端点），TOTP 是操作前的用户批准步骤。

`POST /v1/imports/identifiers` 请求：

```json
{
  "items": ["10.1145/1234567", "arXiv:2601.01234", "https://aclanthology.org/2024.acl-long.1/"],
  "options": {
    "defaultCollectionId": "...",
    "defaultTags": ["status:needs-review"],
    "autoCreateMissingVenues": true,
    "onDuplicate": "link"
  }
}
```

`onDuplicate` 语义（默认 `link`）：

| 值 | 标识符精确命中 | 强模糊命中（规范标题精确 + 年份 ±1 + 一作不矛盾） |
|---|---|---|
| `link` | 链接已有条目，并把新解析出的标识符合并进去（跨版本场景：库里是 DOI 版，导入 arXiv 版后条目同时持有两个标识符） | 同样链接 + 合并；item 的 `error` 字段带证据说明（如 `linked by exact-title+year(0)+first-author match; +1 identifier(s) merged`） |
| `skip` | 标记 skipped，不链接不合并 | 只按建议处理（照常新建，resolve 端已有 warning） |
| `create` | 强制新建（用户刻意保留两个独立条目时用） | 同左 |

resolve 候选新增 `suggestedDuplicateOf`（`paperId`/`citekey`/`year`/`yearDelta`/`authorMatch`/`strong`/`evidence`）与
`possible duplicate in library: …` warning；命中条件为规范标题精确匹配（标识符为空的库内条目——PDF 回退导入、旧数据——靠它兜住）。
标题搜索结果带相关性护栏：查询与结果标题无可信对应（精确/近似/词覆盖）时按无结果处理，
避免"任意文本都能搜出某篇论文"的误导入。

响应 `202 Accepted`：

```json
{
  "importId": "...",
  "jobId": "...",
  "status": "pending",
  "items": 3
}
```

`GET /v1/imports/{id}`：

```json
{
  "id": "...",
  "kind": "identifiers",
  "status": "running",
  "stats": {"total": 3, "created": 1, "duplicates": 1, "failed": 0, "pending": 1, "skipped": 0},
  "createdAt": "...",
  "startedAt": "...",
  "finishedAt": null
}
```

### 5.14 搜索

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/search` | R | 跨实体联合搜索（元数据，不含 PDF 正文） |
| GET | `/v1/search/fulltext` | R | PDF 正文搜索；`q` 必填，可用 `workspaceId` / `paperId` / `fileId` 限定作用域 |
| GET | `/v1/files/{fileId}/search` | P | 单个 PDF 内搜索；阅读器工具栏用 |
| GET | `/v1/files/{fileId}/text` | P | 按页返回抽取文本；agent 阅读正文的入口 |

`GET /v1/search` 响应：

```json
{
  "query": "retrieval augmented",
  "papers": [{"id": "...", "title": "...", "snippet": "…retrieval augmented…", "score": 0.82}],
  "authors": [{"id": "...", "displayName": "...", "paperCount": 12}],
  "venues": [{"id": "...", "abbrev": "...", "kind": "conference"}],
  "tags": [{"id": "...", "path": "topic/rag"}]
}
```

- `types` 可选 `papers,authors,venues,tags`；默认 papers + authors。
- 每个类型最多 10 条；论文搜索走 `paper.search_tsv` + trigram 融合，**只覆盖标题/摘要/作者**。

`GET /v1/search/fulltext` 参数：

| 参数 | 说明 |
|---|---|
| `q` | 必填，1–200 字符 |
| `limit` | 1–100，默认 20 |
| `workspaceId` | 限定一个调研工作区；**包含尚未转入的 staged 论文** |
| `paperId` / `fileId` | 限定单篇 / 单个 PDF；同样包含 staged |
| `includeStaged` | 库级搜索是否包含 staged，默认 `false` |
| `contextChars` | 片段半径（命中词前后各取多少字符），默认 240，范围 60–2000 |
| `maxPerPaper` | 每篇最多保留几条命中，默认不限，1–20 |

`workspaceId` / `paperId` / `fileId` 互斥，同时给多个返回 422 `ambiguous_scope`。

响应（命中为扁平列表，按论文创建时间倒序、页号升序）：

```json
{
  "query": "speculative decoding",
  "hits": [
    {"paperId": "...", "paperTitle": "...", "fileId": "...", "page": 5, "snippet": "…speculative decoding…"}
  ],
  "hasMore": false
}
```

- `snippet` 返回**纯文本**（无 HTML/`<mark>`），前端按查询词自行高亮，禁止 `innerHTML` 渲染未转义文本。
- `GET /v1/files/{fileId}/search?q=...` 返回同样的 `hits` 结构，只查该文件；同样支持 `contextChars`。

#### 5.14.1 按页读取正文

`GET /v1/files/{fileId}/text?from=1&maxPages=100`

**窗口按页数计**，不按字符：默认一次读完整篇（至多 100 页），没有"字不够就读不完"的问题。

| 参数 | 说明 |
|---|---|
| `from` / `to` | 页范围；`from` 默认 1，`to` 省略表示由 `maxPages` 决定窗口终点 |
| `maxPages` | 页数窗口，默认 100，范围 1–500 —— 主限制 |
| `maxChars` | 可选字符预算，默认不设、范围 1000–2000000；只作二次保护，不会扩大页窗口 |

```json
{
  "fileId": "...",
  "textStatus": "extracted",
  "pageCount": 16,
  "pages": [{"page": 1, "text": "...", "charCount": 1820}],
  "truncated": false,
  "nextPage": null
}
```

- 16 页的论文默认一次返回全部 16 页；超过 100 页（或显式给了 `to`）时 `truncated=true`、`nextPage` 指向续读页。
- 至少返回一页，即使调用方把 `maxChars` 设得比单页还小 —— 否则只会拿到空结果。
- `textStatus` 区分"这页没有文字"与"文本还没抽取"：`pending` / `extracted` / `ocr_needed`（扫描件或纯图 PDF，未做 OCR）/ `failed`。

**语义边界**：`GET /v1/papers?q=` 只搜元数据；要搜 PDF 正文必须走 `fulltext` 端点。前端把两者分开展示（"文献"与"正文命中"），避免用户误以为列表搜索覆盖全文。

**可见性**：库级 `fulltext` 默认排除 staged（调研工作区）论文；一旦用 `workspaceId` / `paperId` / `fileId` 显式限定作用域，就视为论文所有者读取自己的材料，**staged 论文同样被搜索和读取**。列表/导出的隔离不等于阅读的隔离。

### 5.15 引用图

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/papers/{id}/graph` | R | 返回 `{nodes[], edges[]}`；v1 只支持 `depth=1`，节点上限 100 |
| GET | `/v1/graph` | R | 按集合/视图/标签构图，上限 200 节点 |

节点：

```json
{"id": "...", "kind": "paper", "title": "...", "year": 2024, "citekey": "...",
 "inLibrary": true, "ccf": "A", "degree": 7}
```

边：

```json
{"source": "...", "target": "...", "type": "cites", "sourceOfData": "openalex"}
```

### 5.16 导出（需要认证的直接下载）

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/exports/bib` | X | `scopeType=library|collection|project|view`、`scopeId`、`citekeyTemplate?` |
| POST | `/v1/exports/bib` | X | 任意选择导出：`{paperIds[], citekeyTemplate?}`，上限 500，返回 `application/x-bibtex` |
| GET | `/v1/exports/csl` | X | 同上，返回 CSL-JSON |
| POST | `/v1/export-tokens` | X | 创建 `.bib` 私密 URL |
| GET | `/v1/export-tokens` | X | 列表（只显示 prefix） |
| DELETE | `/v1/export-tokens/{id}` | X | 撤销 |

`POST /v1/export-tokens`：

```json
{
  "name": "Overleaf-KDD2027",
  "scopeType": "project",
  "scopeId": "...",
  "citekeyTemplate": "{first_author_family}{year}{title_word:1}",
  "includeAbstract": false
}
```

响应（**明文 token 只出现一次**）：

```json
{
  "id": "...",
  "name": "Overleaf-KDD2027",
  "url": "https://library.shaoyuanyu.cn/v1/exports/bib/pdb_ext_7f3a9c2e_<secret>.bib",
  "scopeType": "project",
  "citekeyTemplate": "...",
  "createdAt": "..."
}
```

### 5.17 `.bib` 导出（无 Authorization）

```http
GET /v1/exports/bib/pdb_ext_7f3a9c2e_<secret>.bib
```

| 项 | 约定 |
|---|---|
| 认证 | URL 中的 export token；无 header、无 cookie |
| 失败 | 无效/撤销 → **404**（不暴露 token 是否存在） |
| 响应 | `200`，`Content-Type: application/x-bibtex; charset=utf-8` |
| 缓存 | `Cache-Control: private, no-cache`；`ETag: "<sha256(body)>"`；**只支持 `If-None-Match`**，不承诺 `Last-Modified`（成员/模板/scope 变化无法从 paper.updated_at 推导） |
| 条件请求 | 支持 `If-None-Match` → 304 |
| 排序 | 按 citekey 升序，行尾 `\n`，UTF-8 |
| 限流 | 120 req/min/token |
| 访问记录 | `access_count` + `last_accessed_at`，不记 IP 明文（可选哈希） |

BibTeX 生成规则见 §8。

### 5.18 Token 管理

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/me` | — | 当前 token 的 kind/name/scopes |
| POST | `/v1/tokens` | AD | 创建个人 token |
| GET | `/v1/tokens` | AD | 列表（prefix + lastUsedAt） |
| DELETE | `/v1/tokens/{id}` | AD | 撤销 |

### 5.19 全局设置

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/settings` | R | 返回全部 key/value（不含密钥） |
| PATCH | `/v1/settings` | AD | 局部更新，如 `{"citekey.template": "{first_author_family}{year}{title_word:1}"}` |

允许的 key 见 data-model.md §4.23；未知 key → 422。citekey 模板保存前用样例渲染校验，非法模板拒绝。

### 5.20 管理与统计

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v1/admin/stats` | AD | 库增长、源成功率、job 失败率 |
| GET | `/v1/admin/jobs` | AD | `status`、`kind` 过滤 |
| POST | `/v1/admin/jobs/{id}/retry` | AD | |
| POST | `/v1/admin/reindex` | AD | 重建 FTS/trigram |
| POST | `/v1/admin/datasets/import` | AD | 见 §5.4 |
| POST | `/v1/admin/citations/sync` | AD | 全量/增量 |

---

## 6. Filter DSL

用于 `saved_view.filter`、`auto_rule.filter`、`GET /v1/papers?filter=`。**唯一**的筛选语言，前端筛选器、视图、自动规则共用。

### 6.1 结构

```json
{
  "version": 1,
  "match": "all",
  "rules": [
    {"field": "year", "op": "between", "value": [2020, 2026]},
    {"field": "venue.ccf.level", "op": "in", "value": ["A"]},
    {"field": "tags", "op": "has_all", "value": ["topic/llm"]},
    {"field": "status", "op": "in", "value": ["queued", "reading"]},
    {"field": "hasPdf", "op": "eq", "value": true}
  ],
  "groups": [
    {
      "match": "any",
      "rules": [
        {"field": "venue.abbrev", "op": "in", "value": ["NeurIPS", "ICML", "ICLR"]},
        {"field": "venue.cas.level", "op": "in", "value": ["1"]}
      ]
    }
  ]
}
```

语义：`(rules 按 match 合并) AND (每个 group 按自己的 match 合并，group 之间 AND)`；v1 不支持 group 嵌套。

### 6.2 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` / `abstract` | text | 支持 `contains` / `starts_with` |
| `year` | int | |
| `kind` | enum | paper_kind |
| `venue.id` / `venue.abbrev` / `venue.kind` | text/enum | |
| `venue.ccf.level` | enum | A/B/C |
| `venue.ccf.field` | text | 10 领域；传短键（`人工智能`）或全称均可 |
| `venue.cas.level` | enum | 1–4 |
| `venue.cas.top` | bool | |
| `venue.cas.field` | text | 大类/小类名 |
| `venue.edition.year` / `venue.edition.track` | int/enum | |
| `author.id` / `author.name` | text | |
| `author.isMe` | bool | 本人参与 |
| `tag` | text | 单值；`tags` 用于数组 |
| `collection` / `collections` | uuid | |
| `status` | enum | reading_status |
| `priority` / `rating` | int | |
| `starred` | bool | |
| `archived` | bool | 默认 false（不返回归档）；`true` 只看归档 |
| `hasPdf` | bool | |
| `unfiled` | bool | 未归入任何集合（虚拟 Inbox） |
| `hasCode` | bool | 有 `artifact.kind='code'` |
| `addedAt` / `updatedAt` | date | `between` / `gte` / `lte` |
| `citedBy` | int | 最新 OpenAlex 快照 |
| `doi` / `arxiv` | text | 精确/前缀 |

### 6.3 操作符

`eq`、`ne`、`in`、`not_in`、`between`、`gte`、`lte`、`contains`、`starts_with`、`has_any`、`has_all`、`exists`、`is_null`。

限制：最多 50 条规则、value 数组最多 200 项、字符串 512 字符、不允许未知字段/操作符 → 422 `invalid_filter`。

### 6.4 编译

- 服务端白名单映射到 SQLAlchemy 表达式；所有值绑定参数。
- 文本搜索走 `websearch_to_tsquery` + trigram；`contains` 对大小写不敏感。
- CCF/CAS 条件 join `venue_rank` 时必须复用详情的 `resolve_rank`（按论文 `year` + `valid_from/valid_to` 选版本，回退时标 `fallback`）；**不能**简单取 `max(dataset_version)`，否则列表与筛选会不一致。
- 空 `rules` + 空 `groups` = 全部（但视图不应保存空条件，UI 需拦截）。

---

## 7. citekey 模板

- 默认：`{first_author_family}{year}{title_word:1}` → `yu2026interpretability`。
- 项目/导出 token 可覆盖；创建论文时也可显式给 `citekey`。
- token 列表见 data-model.md §6.4；修饰 `|ascii`、`|lower`、`|trunc:N`。
- 渲染后只保留 `[A-Za-z0-9]`，小写；空则 `paper{year}`；冲突 `a/b/c`。
- 单条 PATCH 改 citekey 时校验唯一并写 `citekey_history`；批量导入不因 citekey 冲突失败，自动消解。

---

## 8. BibTeX 导出规则

### 8.1 条目类型

| paper.kind | BibTeX |
|---|---|
| conference | `@inproceedings` |
| journal | `@article` |
| preprint | `@misc`（`eprint` + `archivePrefix={arXiv}`） |
| thesis | `@phdthesis` |
| book_chapter | `@incollection` |
| tech_report | `@techreport` |

### 8.2 字段

公共：`title`、`author`、`year`、`doi`、`url`。

| 场景 | 字段 |
|---|---|
| 会议 | `booktitle = {<venue.abbrev ?? venue.full_name> <edition.year>}`，有 `pages` 则带上；Findings/Workshop 用 `booktitle = {<abbrev 或全称> <year> <track>}`；空缩写不得输出 `{ 2026}` |
| 期刊 | `journal = {<venue.full_name>}`、`volume`、`number`、`pages` |
| 预印本 | `eprint`、`archivePrefix = {arXiv}`、`primaryClass`（有则） |
| 学位论文 | `school = {<institution>}` |
| 全部 | `abstract` 仅当导出配置开启 |

### 8.3 转义与格式

- 特殊字符：`&`→`\&`、`%`→`\%`、`#`→`\#`、`_`→`\_`；`{}` 保留但需平衡；Unicode 原样输出（要求 biber/UTF-8）。
- 作者：有 `family/given` 时 `Family, Given`，否则 `display_name`；` and ` 连接。
- `pages` 的 `-` 统一为 `--`。
- 缩进两空格；字段顺序固定（title/author/booktitle|journal/year/volume/number/pages/doi/url/eprint/archivePrefix/abstract），保证 diff 稳定。
- 不输出 `@comment` 头（兼容性优先）。
- 空字段省略；`url` 优先 DOI 链接。

### 8.4 排序与缓存

- 按 citekey 升序。
- `ETag = sha256(body)`：任何内容变化（增删条目、改字段、改 citekey 模板、改 scope）都会变；仅访问计数变化不触发。`If-None-Match` 命中返回 304。

---

## 9. CSL-JSON

`GET /v1/exports/csl` 返回数组：

```json
[
  {
    "id": "yu2026interpretability",
    "type": "paper-conference",
    "title": "...",
    "author": [{"family": "Yu", "given": "Shaoyuan"}, {"literal": "Jane Doe"}],
    "issued": {"date-parts": [[2026]]},
    "container-title": "NeurIPS 2026",
    "page": "1--15",
    "DOI": "10.xxxx/yyy",
    "URL": "https://...",
    "abstract": "..."
  }
]
```

- 类型映射：conference→`paper-conference`、journal→`article-journal`、preprint→`article`、thesis→`thesis`、book_chapter→`chapter`。
- 不实现 CSL 格式化；格式化交给 citeproc/Pandoc/Zotero。

---

## 10. OpenAPI 与前端类型

- FastAPI 自动生成 `/openapi.json`；CI 校验可生成、无 500。
- 前端 `pnpm gen:library-api`：用 `openapi-typescript` 读取后端 `openapi.json`（CI 产物或本地运行实例），生成 `lib/library/schema.d.ts` 并提交。
- 后端字段改名 = 前端类型变更；CI 里跑一次 `gen` 并 `git diff --exit-code`，防止漂移。
- 生成物只含类型，不含请求封装；封装在 `lib/library/client.ts`（见 frontend.md）。

---

## 11. 版本化与兼容

- `/v1` 内只做**加法**：新增字段、新增端点、新增可选参数。
- 删除/改名/语义变更 → `/v2`；旧版本至少保留 6 个月。
- 弃用：响应头 `Deprecation: true` + `Sunset: <HTTP-date>` + `Link: <successor>; rel="successor-version"`。
- 客户端应忽略未知字段。

---

## 12. 安全清单（实现时逐条打勾）

- [ ] 所有写端点校验 token scope + 资源归属（单用户，但仍校验软删/合并重定向）。
- [ ] 后端完全忽略 `Cookie`；library 子域在 Nginx 层剥离 Cookie，避免站点 session 泄露到 API 日志/上游。
- [ ] Next 代理只透传 allowlist 路径（无 `/admin/*`），不安全方法校验 Origin/Sec-Fetch-Site。
- [ ] token 比较常量时间；错误信息不区分"不存在"与"无权限"（除 401/403 语义）。
- [ ] PDF 字节端点校验 token + 文件属于该库；转发时不缓存到 CDN。
- [ ] 上传 magic bytes = `%PDF-`；大小限制；文件名仅用于展示。
- [ ] `storage_key` 服务端生成；禁止 `..`、绝对路径、用户输入拼接。
- [ ] 导出 token 只读、范围校验、撤销即时生效；日志只记 prefix。
- [ ] 过滤 DSL 白名单编译；禁止原生 SQL 拼接。
- [ ] 外部抓取设置超时/重试/退避/User-Agent；不把上游 HTML 直接回显。
- [ ] 错误响应不泄露 SQL、堆栈、文件路径。
- [ ] `Idempotency-Key` 作用域 = token；快照 24h 后清理。
- [ ] CORS 默认关闭；若开启，不允许 `*` + credentials。
- [ ] Nginx 层限制请求体、超时与 IP 频率；只暴露 `/v1` 与健康检查。
