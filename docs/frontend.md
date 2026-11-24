# paperdb · 前端集成

> 配套文档：[总览与决策](README.md) · [数据模型](data-model.md) · [API 契约](api.md)
> 目标仓库：`ysy-homepage-web`（Next.js 15 App Router）。**只新增 `/library`，不改现有页面。**

---

## 1. 约束与原则

1. **不改老页面**：`/publications`、`/ccf`、`/cas`、`/venues`、`/deadlines` 保持原样；文献库是新增入口。唯一"旧文件改动"是导航项与文案。
2. **复用站点既有能力**：TOTP 登录态（`lib/auth/owner.ts`）、next-intl、主题、shadcn/Base UI 组件、字体与配色。
3. **后端无 UI**：所有界面、表单、错误提示、任务进度都在前端；后端只出 JSON 与 PDF 字节。
4. **浏览器不碰 token**：页面走 Server Component 直连后端；交互走 Next Route Handler 代理注入 service token。
5. **桌面优先，手机可浏览**：桌面是主力（宽表格、键盘流、PDF 分栏）；手机只保证"查得到、读得了、标已读"。
6. **URL 是状态**：筛选、排序、视图、集合、页码游标都可分享/刷新恢复；不做隐式全局状态。
7. **私有页面不索引**：`robots: { index: false }`，且不进入 sitemap。

---

## 2. 路由与文件

```text
app/[locale]/library/
├── layout.tsx                     # requireOwner + LibraryShell（侧边栏/顶栏/命令面板）
├── page.tsx                       # 列表（默认视图；?view=&collection=&tag=&q=…）
├── papers/[id]/page.tsx           # 详情（uuid 或 citekey）
├── projects/[id]/page.tsx         # 项目工作区（集合范围/置顶/导出）
├── import/page.tsx                # 导入向导
├── settings/page.tsx              # 设置（token/导出/citekey/存储/数据集）
└── graph/page.tsx                 # 引用图（可选；未做则隐藏入口）
app/api/library/[...path]/route.ts # 代理：owner 校验 + service token + 流式转发
components/library/…               # 业务组件
lib/library/
├── server.ts                      # server-only fetch（service token）
├── client.ts                      # 浏览器 fetch 封装（走 /api/library）
├── config.ts                      # 环境变量读取与校验
├── types.ts                       # 视图模型（日期字符串、UI 枚举）
├── filters.ts                     # URL ↔ Filter DSL 映射
├── labels.ts                      # CCF 领域/CAS 学科/状态的中英展示
├── schema.d.ts                    # openapi-typescript 生成（提交）
└── keys.ts                        # React Query keys
messages/{zh,en}.json              # 新增 library 命名空间 + nav.library
```

导航改动：`components/layout/owner-nav-item.tsx` 增加主人专属入口；`app/[locale]/…` 无需改 middleware。

---

## 3. 认证与数据流

### 3.1 页面（Server Component）

```tsx
// app/[locale]/library/layout.tsx
import { requireOwner } from "@/lib/auth/owner";

export const dynamic = "force-dynamic";

export default async function LibraryLayout({ children }: { children: React.ReactNode }) {
  await requireOwner();
  // Shell 首屏数据只取一次；客户端 Query 用 initialData 接管，变更后 invalidate
  const [tree, views, projects, meta] = await Promise.all([
    paperdb.collectionTree(),
    paperdb.listViews(),
    paperdb.listProjects(),
    paperdb.meta(),
  ]);
  return (
    <LibraryShell tree={tree} views={views} projects={projects} meta={meta}>
      {children}
    </LibraryShell>
  );
}
```

- `requireOwner()` 未登录 → `/login`；与 `/mail`、`/ideas` 同一道门。
- 每个页面**自己再 `requireOwner()` 一次**（防御性；layout 不覆盖 route handler 与未来的并行路由）。
- 页面 `generateMetadata` 合并 `robots: { index: false }`。

```tsx
// app/[locale]/library/page.tsx（骨架）
import { requireOwner } from "@/lib/auth/owner";
import { paperdb } from "@/lib/library/server";
import { parseListParams } from "@/lib/library/filters";

export const dynamic = "force-dynamic";

export default async function LibraryPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  await requireOwner();
  const params = parseListParams(await searchParams);
  const firstPage = await paperdb.listPapers(params);
  return <LibraryList initialPage={firstPage} params={params} />;
}
```

### 3.2 server-only 客户端

```ts
// lib/library/server.ts
import "server-only";
import { PAPERDB_URL, PAPERDB_SERVICE_TOKEN } from "./config";

export class PaperdbError extends Error {
  constructor(public status: number, public code: string, public body: unknown) {
    super(`${status} ${code}`);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${PAPERDB_URL}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${PAPERDB_SERVICE_TOKEN}`,
      "content-type": "application/json",
      ...init.headers,
    },
    cache: "no-store",
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new PaperdbError(res.status, body?.code ?? "upstream_error", body);
  }
  return res.json() as Promise<T>;
}

export const paperdb = {
  meta: () => request<Meta>("/v1/meta"),
  listPapers: (p: ListParams) => request<PaperPage>(`/v1/papers?${toQuery(p)}`),
  getPaper: (id: string) => request<PaperDetail>(`/v1/papers/${encodeURIComponent(id)}`),
  // …
};
```

- `cache: "no-store"`：私有数据，绝不能进 Next 数据缓存。
- `lib/library/config.ts` 顶部 `import "server-only"`；变量名不得以 `NEXT_PUBLIC_` 开头，否则会打进浏览器 bundle。
- 超时 15s（列表/详情）；PDF 流不走这里。
- 错误在页面级 `error.tsx` 转成友好文案 + requestId，不把后端原文直接抛给用户。

### 3.3 浏览器 → Next 代理

浏览器的所有读写都走 `/api/library/...`，**不直连** `library.shaoyuanyu.cn`：

```ts
// app/api/library/[...path]/route.ts
import { NextRequest, NextResponse } from "next/server";
import { isOwner } from "@/lib/auth/owner";
import { PAPERDB_URL, PAPERDB_SERVICE_TOKEN } from "@/lib/library/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const PASS_HEADERS = [
  "content-type",
  "content-length",
  "content-range",
  "accept-ranges",
  "etag",
  "content-disposition",
  "cache-control",
  "x-content-type-options",
  "content-security-policy",
];

/** 浏览器可达的后端前缀：不含 /admin/*；/tokens 与 /export-tokens 仅供设置页 */
const ALLOWED = [
  /^papers(\/|$)/, /^authors(\/|$)/, /^venues(\/|$)/, /^collections(\/|$)/,
  /^tags(\/|$)/, /^views(\/|$)/, /^projects(\/|$)/, /^imports(\/|$)/,
  /^files(\/|$)/, /^annotations(\/|$)/, /^artifacts(\/|$)/, /^search(\/|$)/, /^exports\/(bib|csl)(\/|$)/,
  /^settings$/, /^meta$/, /^me$/, /^tokens(\/|$)/, /^export-tokens(\/|$)/,
];

async function proxy(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  if (!(await isOwner())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const { path } = await params;
  if (!path.length || path.some((s) => s === ".." || s.includes("/"))) {
    return NextResponse.json({ error: "bad_path" }, { status: 400 });
  }
  const joined = path.join("/");
  if (!ALLOWED.some((re) => re.test(joined))) {
    return NextResponse.json({ error: "forbidden_path" }, { status: 403 });
  }
  // 不安全方法必须有同源证据（Origin / Sec-Fetch-Site）；GET/HEAD 不要求
  if (!["GET", "HEAD"].includes(req.method)) {
    const site = req.headers.get("sec-fetch-site");
    const origin = req.headers.get("origin");
    const host = req.headers.get("host");
    const proto = req.headers.get("x-forwarded-proto") ?? "https";
    if ((site && site !== "same-origin") || (origin && origin !== `${proto}://${host}`)) {
      return NextResponse.json({ error: "cross_origin" }, { status: 403 });
    }
  }
  const target = `${PAPERDB_URL}/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;

  const headers: Record<string, string> = {
    authorization: `Bearer ${PAPERDB_SERVICE_TOKEN}`,
  };
  for (const h of ["content-type", "range", "if-none-match", "accept"]) {
    const v = req.headers.get(h);
    if (v) headers[h] = v;
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
      // @ts-expect-error Node fetch 流式 body 需要 duplex
      duplex: "half",
      cache: "no-store",
      signal: AbortSignal.timeout(300_000),
    });
  } catch {
    return NextResponse.json({ error: "paperdb_unreachable" }, { status: 502 });
  }

  const out = new Headers();
  for (const h of PASS_HEADERS) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  if (!out.has("x-content-type-options")) out.set("x-content-type-options", "nosniff");
  return new NextResponse(upstream.body, { status: upstream.status, headers: out });
}

export const GET = proxy;
export const HEAD = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
```

关键点：

- **必须导出所有方法**（曾漏 `PUT` 导致 405，见 `app/api/mail/[...path]/route.ts` 注释）。
- 上传走 `req.body` 流式转发（不要 `arrayBuffer()`，200 MB PDF 会吃内存）；`duplex: "half"` + `content-length` 可选。
- PDF 的 `Range`/`Content-Range`/`Accept-Ranges`/`ETag` 必须原样透传，否则 pdf.js 的分片与缓存失效。
- 不转发浏览器 `authorization`、`cookie`；只注入 service token。
- 路径段 `encodeURIComponent`，拒绝 `..`/`/`，避免 SSRF/路径穿越。
- **路径 allowlist**（代码里的 `ALLOWED`）：`/v1/admin/*` 永不透传；`/tokens`、`/export-tokens` 只给设置页用（v1 为了让设置页能建 token 而保留；更严的做法是改 Next Server Action + 一次性兑换，v1.5 再评估）；`/settings` 允许读写（仍是 owner-gated，且有不安全方法 Origin 校验）。
- 对 `POST/PUT/PATCH/DELETE` 校验 `Origin`（或 `Sec-Fetch-Site: same-origin`）必须等于站点自身源；站点 session cookie 是 `HttpOnly; SameSite=Lax`（`app/api/auth/totp/route.ts`），跨站 POST 本就带不上 cookie，但 `library.shaoyuanyu.cn` 与站点同站（same-site），仍需要这一层防同站子域伪造。
- 代理响应的 `content-security-policy: sandbox` 与 `nosniff` 是 PDF 安全加固的一部分，**不能省**。

### 3.4 浏览器 fetch 封装

```ts
// lib/library/client.ts
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/library/${path}`, {
    ...init,
    headers: { ...(init.body ? { "content-type": "application/json" } : {}), ...init.headers },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(res.status, body?.code ?? "error", body);
  }
  return res.json() as Promise<T>;
}
```

上传用 `XMLHttpRequest` 以获得进度：

```ts
export function uploadPdf(paperId: string, file: File, onProgress: (pct: number) => void) {
  return new Promise<UploadResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/library/papers/${paperId}/files`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => (xhr.status < 300 ? resolve(JSON.parse(xhr.responseText)) : reject(new Error(`HTTP ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("network"));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}
```

### 3.5 错误处理约定

| 场景 | UI |
|---|---|
| 后端 401（service token 失效） | 整页提示"文献库令牌无效/已撤销"，附 requestId；不登出站点、不跳 `/login` |
| 后端 502/超时 | "文献库服务不可达"，重试按钮；保留当前列表缓存 |
| 404 论文 | `notFound()` → 站点 404 |
| 409 重复 | 对话框："已在库中，去查看 / 仍然创建（版本链）" |
| 409 版本冲突 | Toast + 自动重取最新版本，提示"已刷新，请重试" |
| 422 filter | Toast + 保留上一次有效结果 |
| 429 | Toast + 倒计时（Retry-After） |
| PDF 404/410 | 阅读器空态："文件缺失，请重新上传" |

---

## 4. 页面设计

### 4.1 LibraryShell

- **左侧边栏（208px，可折叠到 56px）**：
  - 顶部"添加文献"主按钮（打开单条解析对话框 / 跳导入页）。
  - 快速入口：全部、Inbox（= 未归入任何集合，虚拟视图）、待读、在读、已读、星标、最近添加。
  - 集合树（可展开、计数、右键/省略号菜单：新建子集合、重命名、移动、删除）。
  - 智能视图列表（置顶在前，右键：编辑、复制、删除）。
  - 项目列表（+ 新建）。
  - 标签入口（打开标签面板/抽屉，树形、计数）。
  - 底部：设置、存储用量（`meta.storage`）。
- **顶栏（sticky，位于站点 header 下）**：面包屑/当前范围、全局搜索框（`/` 聚焦；可切「文献 / 正文」两种模式）、视图切换（表格/卡片）、筛选按钮、列设置、命令面板提示 `⌘K`。
- **宽度**：与站点栏线一致，根容器 `w-full mx-auto max-w-6xl px-4 sm:px-6`；不要为文献库破例放宽。1152px 下 = 侧栏 208 + 内容 ~900，表格默认只显示 8 列，其余进"列设置"。
- **高度**：沿用邮件页的站点外壳计算 `lg:h-[calc(100dvh_-_3.5rem_-_1px_-_4.0625rem)] lg:overflow-hidden`（header 3.5rem + 1px 边框 + footer 4.0625rem）；列表/详情在内部滚动。窄屏保持自然文档流。
- 移动端：侧边栏收进 `Sheet`；顶栏只留搜索、筛选、菜单。

### 4.2 列表页

```text
┌ 站点 header ─────────────────────────────────────────────────────────────┐
│ ☰  文献库      [ 搜索标题 / 作者 / 摘要…        ]   [筛选] [列] [⌘K]      │
├──────────────┬───────────────────────────────────────────────────────────┤
│ + 添加文献    │ ☐  标题                    作者        venue     CCF  CAS  │
│ 全部   1,234 │ ☐  Interpretability of …   Yu et al.  NeurIPS   A    —   │
│ Inbox     12 │ ☐  Retrieval-Augmented …   Gao et al. ACL       A    1区  │
│ 待读      34 │ ☐  Scaling Laws …          Chen et al. ICML     A    —   │
│ 在读       5 │ ☐  …                                                        │
│ 已读     486 │                                                            │
│ ★ 星标    27 │  （虚拟滚动，40px 行高）                                   │
│ 集合      ▾  │                                                            │
│  ├ RAG    56 │                                                            │
│  └ 评估   31 │                                                            │
│ 视图      ▾  │                                                            │
│ 项目      ▾  │                                                            │
│ 设置 / 用量  │                                                            │
└──────────────┴───────────────────────────────────────────────────────────┘
```

URL 示例：`/library?collection=<id>&ccf=A&status=queued&sort=year:desc`。

**默认列**（可在列设置里增减/调宽，持久化到 localStorage）：

| 列 | 内容 |
|---|---|
| 选择框 | 批量 |
| 标题 | 单行截断；有 PDF 图标、批注计数；citekey 默认不显示 |
| 作者 | 前 3 位 + `et al.`；本人加粗/标记 |
| venue | 缩写（为空时用全称）+ 年份；会议显示 edition track（Findings/Workshop） |
| CCF | `A/B/C` 徽章；tooltip 显示 `CCF 2026`；`fallback` 时加 `*` 并提示按最近目录 |
| CAS | `1区 Top` 徽章；tooltip 显示 `CAS 2025`；会议/未收录显示 `—`（说明分区表仅期刊） |
| 状态 | 圆点/下拉快速切换 |
| 优先级（可选） | 小旗/数字 |
| 评分（可选） | 1–5 星（可点） |
| 标签 | 前 3 个 + `+n` |
| 添加时间 | 相对时间 |
| citekey（可选） | mono 小号；默认关闭 |

- **行高默认 48px**：标题单行截断，作者/venue 同行；citekey 默认不进列（列设置可开）。"舒适"模式 64px 才显示两行标题。虚拟滚动，overscan 10；1w 条不卡。
- **点击行** → 详情；`Cmd/Ctrl+点击` 新标签页。
- 顶栏搜索切到"正文"模式时，结果区改为按论文分组的全文命中（标题 + `p.5` 片段），点击打开详情并跳到对应页；退出模式回到普通列表。
- **选择**：点击 checkbox；Shift 范围选择；`x` 切换当前行；全选当前页。
- **批量条**（底部浮动）：已选 N 项；状态、优先级、加入集合、加/删标签、导出 BibTeX（`POST /v1/exports/bib`，选中 id）、删除、清除。
- **快速筛选 chips**：`CCF-A`、`有 PDF`、`待读`、`星标`、`今年`、`有代码`；可叠加。
- **高级筛选**（Popover）：字段 + 操作符 + 值；"保存为视图"。
- **空态**：
  - 全库为空：大号引导"粘贴 DOI / arXiv / 链接开始"，附导入按钮与示例。
  - 筛选为空："没有匹配的文献"，清空筛选。
- **加载**：首屏 skeleton 6 行；翻页底部 spinner；切换筛选保留旧数据 + 顶部细进度条（避免闪烁）。
- **移动端**：卡片列表（标题 2 行、venue+年份、状态/评分一行、标签一行）；批量操作收进底部菜单；不做虚拟滚动（分批 30 条加载）。

### 4.3 详情页

```text
┌ ← 论文标题 ………………………………………………………  [★] [状态▾] [导出 BibTeX] [⋯] ┐
├────────────────────────────┬──────────────────────────────────────────────┤
│ 信息  引用  批注  文件  版本 │ [◀] [5 / 18] [▶]  [−] [120%] [+] [适宽] [🔍] │
│                            │ ┌──────────────────────────────────────────┐ │
│ venue  NeurIPS 2025        │ │                                          │ │
│ year   2025                │ │  PDF canvas（高亮层叠加）                 │ │
│ CCF    A（2026）           │ │                                          │ │
│ CAS    —（会议无分区）      │ │   ████ 高亮 ████                        │ │
│ authors  …                 │ └──────────────────────────────────────────┘ │
│ abstract …                 │ 批注侧栏（可收起）                            │
│ identifiers DOI / arXiv    │  p.5  “We propose …”  核心方法             │
│ artifacts  code / data     │  p.9  “…”                                   │
└────────────────────────────┴──────────────────────────────────────────────┘
```

布局（≥1280）：`左 360px 元数据 | 右 PDF`（PDF 弹性，最小 480px）；1024–1280 折叠为 `信息 / PDF` 两个 Tab；<1024 全屏单栏。

**头部**：返回、标题（可编辑）、citekey（点击复制；编辑需二次确认并显示 old→new 对照与"旧 .tex 不会自动更新"提示）、外部链接（DOI/arXiv/项目页）、操作（星标、状态、导出 BibTeX、更多：刷新元数据、合并、删除、重抽取）。

**左侧信息区**（Tabs）：

1. **信息**：venue/edition/track、年份、卷期页、作者列表（可拖拽排序、标通讯/共同一作/本人）、摘要（可折叠）、标识符（增删、设主）、artifact（代码/数据/模型，按 host 显示图标）、URL。
2. **引用**：参考文献 / 被引 / 相关文献三个子列表；每条显示"库内/库外"，库外有"加入库"；被引/相关带计数与来源（OpenAlex/S2）。
3. **批注**：按页分组；点击跳页；可编辑评论、改色、删除；顶部"导出 Markdown"（v1.5）。
4. **文件**：文件列表、主文件标记、上传/设为主文件/显式删除（"替换"= 上传新文件并设主，旧版本保留）、全文抽取状态、重新抽取、页数/大小。
5. **版本**：预印本↔正式版关系；"关联版本"选择器；自动检测建议。

**右侧 PDF**：见 §4.4；没有 PDF 时显示"上传 PDF"与"仅元数据"空态。

**内联编辑**：字段点击变输入框（title/abstract 用 textarea + 自动保存防抖 600ms）；保存失败回滚并 toast。`If-Match` 用当前 `version`；409 时刷新并提示。

**合并**：更多 → 合并 → 搜索目标 → 预览字段选择（每个字段选保留哪边）→ 确认；成功后跳转 survivor 并 toast。

### 4.4 PDF 阅读器

**技术**：`pdfjs-dist` + 自绘 canvas/text/highlight 三层，动态 import、`ssr: false`。

**加载**：

```ts
const pdf = await getDocument({
  url: `/api/library/files/${fileId}/content`,
  rangeChunkSize: 64 * 1024,
  withCredentials: false, // 仅影响跨域；同源请求本来就会带 cookie
  cMapUrl: "/pdf/cmaps/",
  cMapPacked: true,
}).promise;
```

- 多文件时工具栏左侧有"版本选择器"（主文件/其他版本）；批注列表只显示当前文件的批注，切换文件时重新拉取。
- `pdf.worker.min.mjs` 由脚本复制到 `public/pdf/`（`scripts/copy-pdf-assets.mjs`，在 `predev`/`prebuild` 执行）；`GlobalWorkerOptions.workerSrc = "/pdf/pdf.worker.min.mjs"`。避免 Turbopack 对 `?url` 的不确定性。
- 中文/日文 PDF 需要 cmaps：把 `pdfjs-dist/cmaps` 复制到 `public/pdf/cmaps/`。
- 渲染尺寸按容器宽度与 `devicePixelRatio`；`canvas.width = viewport.width * dpr`，CSS 宽为 `viewport.width`。
- 页面容器 `position: relative`；高亮层用百分比定位：

```tsx
<div
  className="absolute rounded-[2px]"
  style={{
    left: `${r.x * 100}%`, top: `${r.y * 100}%`,
    width: `${r.w * 100}%`, height: `${r.h * 100}%`,
    background: "color-mix(in oklab, var(--highlight) 35%, transparent)",
  }}
/>
```

缩放/换设备只改容器尺寸，百分比自动跟随，**不需要重算坐标**。

**选择 → 高亮**：
1. 监听 text layer 的 `mouseup`（触屏 `touchend`）。
2. 取 `window.getSelection()` 的 range，按 `getClientRects()` 与页面 canvas 的 `getBoundingClientRect()` 求相对坐标，clamp 到 `[0,1]`。
3. 取 `range.toString()` 作为 `quote`；从 text layer 文本取前后 32 字符作为 `prefix/suffix`。
4. 浮动工具条：颜色（黄/绿/蓝/粉/紫）、添加评论；保存 `POST /papers/{id}/annotations`。
5. 失败：保留本地临时高亮 + toast，允许重试；不丢用户选择。

**工具栏**：页码/总页、上一页/下一页、缩放（- / % / +）、适合宽度、适合页面、**单页/连续/双页**切换、**深色反色**开关、旋转（可选）、搜索、批注侧栏开关。阅读器内搜索优先用 pdf.js 本地（`getTextContent()`/`PDFFindController`，即时）；跨库正文搜索走 `GET /v1/search/fulltext`。

**双页与反色**：
- 双页：容器宽度 ≥ 960px 时并排渲染 `2n-1 / 2n`，跨页批注按页拆开；窄屏自动退回单页。
- 反色：对 canvas 与高亮层同时加 `filter: invert(1) hue-rotate(180deg)`，文字层保持可选中；状态存 localStorage（`library:pdf:invert`），不写后端。
- 快捷键：`d` 切换双页，`i` 切换反色（输入框聚焦时不拦截）。

**键盘**：`←/→` 翻页，`+/-` 缩放，`f` 适合宽度，`d` 双页，`i` 反色，`Esc` 关闭浮层；输入框聚焦时不拦截。

**阅读状态联动**：首次打开 PDF 时，若状态是 `unread`/`queued` → 自动置 `reading` 并更新 `last_opened_at`；手动设为 `read`/`skimmed` 后不再自动回退。滚动时防抖 2s 上报 `progressPage`，用于"继续阅读"。

**移动**：单页模式，横向滑动翻页；缩放用工具栏（不依赖双指，避免与浏览器手势冲突）；高亮可创建，编辑评论只读（v1）。

**性能**：
- 只渲染当前页 ±1；滚动模式用 IntersectionObserver 懒渲染。
- 页面 canvas 缓存上限 5 页，超出释放。
- `pdf.destroy()` 在卸载/切换文件时调用，避免内存泄漏（iOS Safari 尤其重要）。

### 4.5 导入页

三个 Tab：

1. **标识符/链接**：textarea，一行一个；支持 DOI、arXiv、URL、标题；实时统计行数；选项（默认集合、默认标签、重复策略：跳过/关联/新建版本）；提交后跳任务详情。
2. **BibTeX**：拖拽 `.bib` 或选择文件；解析后显示条目数与前 5 条预览；同样选项。
3. **PDF**：拖拽多个 PDF；每个文件一个上传项；能从首页抽 DOI 自动解析，否则建"仅元数据 + 文件"条目进 Inbox；显示上传进度与抽取状态。

**任务进度**：轮询 `GET /imports/{id}`（1.5s，指数退避到 5s，终态停止）；展示 `stats` 与逐条列表（成功/重复/失败 + 错误 + 重试）；失败项支持"全部重试"。

**单条添加**（列表页"添加文献"）：输入 → `POST /resolve` → 候选卡片（来源、置信度、venue 建议、CCF/CAS、重复提示）→ 编辑字段 → 创建。用于"读完摘要再决定收不收"。

### 4.6 项目工作区

路由 `/library/projects/[id]`，与列表页共享 `PaperTable`，scope 固定为项目：

- **头部**：项目名、描述（可编辑）、归档按钮、"导出 `.bib`"（创建/复制 export token）。
- **集合范围**：chips 列出关联集合（可增删，含子集合提示）；"把当前筛选结果加入项目"。
- **置顶论文**：手动排序（v1 上/下移即可，拖拽 v1.5）；置顶优先于集合并集展示。
- **全部论文**：项目并集分页列表；列与批量操作复用列表页。
- **导出设置**：citekey 模板（项目级覆盖）、是否含 abstract、export token 列表（创建/撤销/访问次数）。
- **统计**：总数、按 CCF/CAS/年份的分布（轻量条形图，可选）。

### 4.7 设置页

分区：

1. **数据集**：CCF/CAS 版本、导入时间、checksum、来源链接；`meta.venues.unmatched/missingIssn` 显示待归并数量；"如何更新/crosswalk"说明（v1 只读，CSV 用 CLI 导入，不开放浏览器上传）。
2. **citekey**：全局模板输入 + 实时样例预览；非法 token 高亮；保存到 `/v1/settings`。
3. **API Token**：列表（name/prefix/scopes/lastUsedAt）；创建对话框（选 scope、过期时间）；明文只显示一次，带复制按钮；撤销二次确认。
4. **导出链接**：scope（库/集合/项目/视图）、citekey 模板、是否含摘要；创建后显示完整 URL（只显示一次）；列表有复制/撤销/访问次数。
5. **存储**：backend、已用空间、文件数；"切换 OSS"说明（v1.5）。
6. **导入默认值**：默认集合、默认标签、重复策略。
7. **运维信息（只读）**：失败 job 数、存储用量、数据集版本；重建索引/清理 job 走 CLI，浏览器不暴露 `/admin/*`。

### 4.8 命令面板（⌘K）

- 使用现有 `components/ui/command.tsx`（cmdk）。
- 分组：文献（元数据搜索，最多 8 条）、正文命中（`/v1/search/fulltext`，最多 5 条，显示"标题 · p.5"）、作者、venue、视图、动作（去导入、去设置、新建集合）。
- 正文命中与文献命中分开展示，避免把 PDF 正文结果混进元数据列表。
- 输入防抖 150ms；`Enter` 打开；`⌘Enter` 新标签；最近打开（localStorage）优先展示。
- 全局快捷键：`⌘K` / `Ctrl+K` 打开；`/` 聚焦列表搜索（在输入框外时）；`g l` 跳列表？可选，v1 只做 `⌘K`。

### 4.9 引用图（可选，允许降级）

- `/library/graph?focus=<paperId>&depth=1`；Canvas + `d3-force`（仅此页动态加载）。
- 节点 ≤ 200；库内实心、库外空心；颜色按 CCF；大小按度数；点击打开详情/加入库。
- 顶部："以此为中心"、"展开一层"、图例、导出 PNG（可选）。
- 降级方案：若工期紧，v1 只做详情页"引用/被引/相关"列表，图页面隐藏入口，路由保留。

---

## 5. 状态、URL 与缓存

### 5.1 URL 参数

| 参数 | 说明 |
|---|---|
| `q` | 关键词 |
| `view` | 保存视图 id |
| `collection` | 集合 id；默认含子集合，`sub=0` 只看本级 |
| `tag` | 标签 path（可重复） |
| `status` | 状态（可重复） |
| `ccf` / `cas` | 等级/分区（可重复） |
| `year` | `2020-2026` 或单值 |
| `hasPdf` / `starred` / `hasCode` | `1` |
| `sort` | `field:asc,field:desc` |
| `layout` | `table` / `cards` |
| `cursor` | 翻页游标（可选；无限滚动时由客户端维护，不写 URL 以免历史爆炸） |

- URL 更新用 `router.replace(url, { scroll: false })`；搜索输入防抖 250ms。
- 客户端**不要直接 `useSearchParams()`**（Next 15 对静态渲染要求 Suspense 边界）；由 Server Component 解析 `searchParams` 后作为 props 下传，客户端只负责 `router.replace`。若某组件确实要读，包一层 `<Suspense>`。
- 浏览器前进/后退恢复状态；分享 URL 可复现筛选。
- 复杂筛选（高级面板）序列化为 `filter=<base64url(JSON)>`，只接管结构化条件；关键词 `q` 始终独立生效（两者不互斥）。

### 5.2 React Query

- 新增依赖 `@tanstack/react-query`；在 `LibraryShell` 内包 `QueryClientProvider`（客户端组件）。
- Query keys：`['papers', params]`、`['paper', id]`、`['annotations', paperId]`、`['tree']`、`['views']`、`['tags']`、`['meta']`、`['projects']`。
- 默认 `staleTime: 30_000`、`retry: 1`、`refetchOnWindowFocus: false`（私库，避免无意义请求）。
- 列表 `useInfiniteQuery`，`getNextPageParam` 用 `nextCursor`；首屏 `initialData` 来自 Server Component（仅当参数与首屏一致，否则忽略并在客户端拉取）。
- 乐观更新：
  - 状态/星标/评分：先改 cache，再发 PATCH；失败回滚 + toast。
  - 批注创建/删除：临时 id，成功后替换；失败删除临时项。
  - 标签/集合变更：成功后 invalidate `papers` 与 `tree`（计数）。
- 不引入 Redux/Zustand；跨页面的少量 UI 状态用 URL 或组件内 state。

### 5.3 轮询

- 导入任务、PDF 抽取状态：`refetchInterval` 条件函数（1.5s → 5s，终态 `false`）。
- 命令面板搜索：防抖 + `AbortController` 取消上一次。

---

## 6. 组件清单

| 组件 | 职责 |
|---|---|
| `LibraryShell` | 侧边栏 + 顶栏 + QueryProvider + 命令面板 + 移动端 Sheet |
| `LibrarySidebar` | 快速入口、集合树、视图、项目、标签入口、设置 |
| `CollectionTree` | 递归树、展开状态（localStorage）、计数、菜单 |
| `TagTree` | 同上，按 namespace 分组 |
| `ViewList` | 视图项、置顶、编辑/删除 |
| `ProjectList` | 项目项、新建 |
| `LibraryTopbar` | 面包屑、搜索框、布局切换、筛选、列设置 |
| `FilterBar` | chips + 高级筛选 Popover + 保存视图 |
| `PaperTable` | 表头/列设置/虚拟滚动/选择/Shift 范围 |
| `PaperRow` | 单行渲染（memo） |
| `PaperCard` | 移动端卡片 |
| `PaperBadges` | CCF/CAS/状态/PDF/artifact 徽章 |
| `BulkBar` | 批量操作 |
| `CollectionPicker` | 单条/批量加入集合（树选择 + 搜索） |
| `TagEditor` | 命名空间标签选择/新建/移除 |
| `InlineEdit` | 文本/textarea/选择器的内联编辑 + 防抖保存 |
| `PaperHeader` | 详情头部与操作 |
| `MetadataPanel` | 信息 Tab |
| `AuthorEditor` | 作者排序、通讯/共同一作、本人 |
| `IdentifierEditor` | 标识符增删/设主 |
| `ArtifactList` | code/data/model 链接 |
| `CitationList` | 参考文献/被引/相关 |
| `VersionPanel` | 预印本/正式版关系 |
| `PdfPane` | 阅读器容器、工具栏、懒加载 |
| `PdfCanvas` / `PdfTextLayer` / `HighlightLayer` | 渲染三层 |
| `SelectionToolbar` | 高亮/评论浮层 |
| `AnnotationSidebar` | 按页分组、跳转、编辑 |
| `UploadDropzone` | 拖拽上传 + 进度 |
| `ImportWizard` | 三个 Tab + 选项 + 任务进度 |
| `JobProgress` | 轮询 + 逐条状态 + 重试 |
| `SettingsPanels` | 数据集/citekey/token/导出/存储/默认值 |
| `TokenDialog` | 创建 token，明文只显示一次 |
| `ExportLinkDialog` | 创建/复制/撤销 `.bib` URL |
| `CommandMenu` | ⌘K |
| `EmptyState` / `ErrorState` / `InlineError` | 空/错/局部错误 |

复用现有 `components/ui/*`：`button`、`input`、`textarea`、`select`、`popover`、`dialog`、`sheet`、`tabs`、`badge`、`tooltip`、`command`、`scroll-area`、`toast`、`skeleton`、`confirm-dialog`、`search-input`、`field`、`spinner`、`empty`。缺 `table` 时手写 `<table>` 语义结构（虚拟滚动下用 div + role 也可以，但优先原生 table + sticky header）。

---

## 7. 依赖

| 依赖 | 用途 | 备注 |
|---|---|---|
| `@tanstack/react-query` | 服务端状态、缓存、乐观更新 | 新增 |
| `@tanstack/react-virtual` | 万条列表虚拟滚动 | 新增；不引入完整 table 库 |
| `pdfjs-dist` | PDF 渲染 | 新增；锁版本，worker/cmaps 复制到 `public/pdf/` |
| `d3-force` | 引用图布局（仅 graph 页） | 可选；动态 import |
| `date-fns` | 相对时间 | 已有 |
| `cmdk` | 命令面板 | 已有（`components/ui/command.tsx`） |
| `lucide-react` | 图标 | 已有 |
| `sonner`? | toast | 已有自研 `components/ui/toast.tsx`，沿用 |

不新增：UI 框架、CSS-in-JS、状态库、表格全家桶、拖拽库（v1 用"移动到"对话框；v1.5 再评估 dnd-kit）。

---

## 8. i18n

- 命名空间 `library`，中英各一份；key 结构：

```json
{
  "library": {
    "nav": "文献库",
    "title": "文献库",
    "list": {"searchPlaceholder": "搜索标题、作者、摘要…", "empty": "还没有文献", "selected": "{count} 项已选"},
    "detail": {"overview": "信息", "citations": "引用", "annotations": "批注", "files": "文件", "versions": "版本"},
    "pdf": {"page": "第 {page} / {total} 页", "fitWidth": "适合宽度"},
    "import": {"title": "导入文献", "identifiers": "标识符 / 链接", "bibtex": "BibTeX", "pdf": "PDF"},
    "settings": {"tokens": "API Token", "exports": "导出链接", "datasets": "评价数据"},
    "status": {"unread": "未读", "queued": "待读", "reading": "在读", "read": "已读", "skimmed": "略读"},
    "errors": {"unreachable": "文献库服务不可达", "tokenInvalid": "文献库令牌无效"}
  }
}
```

- 数据里的中文：CCF 10 领域与 CAS 大类/小类在英文界面需要映射（复用 `lib/data/venue.ts` 的 `FIELD_EN` 思路，前端放 `lib/library/labels.ts`）；映射不到就显示原文，不丢信息。
- CCF/CAS 徽章文案：中文 `CCF A` / `中科院 1 区 Top`；英文 `CCF A` / `CAS Q1 Top`。
- 日期：`Intl.RelativeTimeFormat` / `date-fns`，中文 `2026年11月`，英文 `Nov 2026`。
- 数字：`Intl.NumberFormat`（引用数 1.2k 等）。

---

## 9. 响应式与移动

| 断点 | 行为 |
|---|---|
| ≥1280 | 侧边栏 + 列表全列 + 详情分栏（元数据/PDF） |
| 1024–1279 | 侧边栏可折叠；详情改为 Tab；默认隐藏低优先列（评分/添加时间） |
| 768–1023 | 侧边栏抽屉；列表卡片模式；批量条收进底部菜单 |
| <768 | 单列卡片；顶栏简化；PDF 全屏；批注只读评论编辑 |

- 触控目标 ≥ 40px；表格行在移动端不显示 hover 操作，改长按/省略号。
- 不允许横向溢出：表格在窄屏切换卡片；长标题/作者 `line-clamp`；citekey `truncate`。
- iOS Safari：PDF 用单页模式 + 限制 canvas 缓存；避免 `100vh`，用 `100dvh`。

---

## 10. 性能预算

| 指标 | 目标 |
|---|---|
| `/library` 首次 JS（gz，不含共享 shell） | < 250 KB |
| PDF 相关 chunk | 懒加载，不计入首屏 |
| 列表首屏（50 行）服务端 p95 | < 300 ms |
| 搜索 p95 | < 200 ms |
| 筛选切换（客户端） | < 100 ms 出骨架，< 500 ms 完成 |
| 滚动 | 60 fps（虚拟化 + memo 行） |
| 命令面板打开 | < 50 ms（本地最近项先出） |
| PDF 首页渲染 | < 1 s（局域网，20 页内） |

手段：虚拟滚动、行 memo、`useDeferredValue` 搜索、图片/图标按需、pdf.js 动态 import、避免整页 RSC 重取（客户端查询）。

---

## 11. 可访问性

- 虚拟列表用 `<div role="grid" aria-rowcount>` + 行 `role="row"`/`aria-rowindex` + 单元格 `role="gridcell"`（原生 table 与虚拟滚动/sticky header 组合过于别扭）；行可选、`aria-selected`；checkbox 有 label。
- 键盘可达：Tab 顺序、焦点环、Esc 关闭 Popover/Dialog、`⌘K` 打开命令面板。
- 颜色不是唯一信息：CCF/CAS 徽章带文字；阅读状态带文字标签。
- 高亮颜色对比度：在浅色/深色主题下都 ≥ 3:1（文字另算）。
- 动态保存状态用 `aria-live="polite"`（"已保存"）；错误用 `role="alert"`。
- PDF 阅读器提供"下载/新标签页打开"兜底（若 canvas 渲染失败）。

---

## 12. 测试

### 12.1 Playwright（`e2e/library.spec.ts`）

用 `page.route("**/api/library/**")` stub 后端，避免 e2e 依赖真实库：

1. 未登录访问 `/library` → 跳 `/login`。
2. 登录（复用 smoke.spec 的 TOTP 辅助）后列表渲染 20 条 stub、徽章正确。
3. 搜索输入 → URL `?q=` 更新、请求参数正确（断言 route 拦截到的 URL）。
4. 快速 chip `CCF-A` → 请求 filter 含 `venue.ccf.level in ["A"]`。
5. 选择 3 行 → 批量条出现 → 改状态 → PATCH 请求体正确 → 行状态更新。
6. 详情：Tab 切换、citekey 复制（clipboard stub）、批注列表渲染。
7. 导入：粘贴 2 个标识符 → 选项 → 提交 → 进度轮询 → 成功/失败行。
8. 设置：创建 token → 明文弹窗只出现一次 → 关闭后列表只有 prefix。
9. `⌘K` 打开命令面板，输入关键词出现分组。
10. 移动 viewport（390×844）：列表为卡片、无横向滚动（`scrollWidth <= clientWidth`）。
11. PDF 路由的 Range：stub 返回 206 + `Content-Range`，断言 pdf 容器出现页码。

### 12.2 手测清单

- 深/浅色主题下徽章、批注颜色、表格斑马纹。
- 中/英文切换，长英文文案不溢出（尤其顶栏、按钮）。
- 1w 条 stub 数据滚动与筛选。
- 上传 100 MB PDF（慢网限速）不中断、进度准确。
- 断网后操作：toast 报错、数据不脏、重试成功。
- 双标签页同时改同一论文：409 提示与刷新。
- iOS Safari：PDF 翻页、缩放、旋转后高亮位置。

---

## 13. 与现有代码的衔接

| 文件 | 改动 |
|---|---|
| `components/layout/owner-nav-item.tsx` | 新增主人专属"文献库"入口（建议 **icon-only** `BookMarked`，`aria-label` + tooltip；移动端 Sheet 内显示文字）。顶栏在 1024–1279px 已接近宽度上限（见文件注释与 `site-header.tsx`），加文字项很可能在英文下溢出；实现后必须在 1024/1280 英文登录态实测。 |
| `messages/zh.json` / `en.json` | `nav.library`；整个 `library` 命名空间。 |
| `e2e/smoke.spec.ts` | 不加导航断言（避免拖慢 CD）；新建独立 `library.spec.ts`。 |
| `scripts/e2e-select.mjs` | 把 `components/library/**`、`app/[locale]/library/**`、`lib/library/**` 映射到 `library.spec.ts`（否则改动不会触发该用例）。 |
| `next.config.ts` | 通常无需改；若 pdf.js 打包报错，加 `serverExternalPackages`/`transpilePackages`。 |
| `package.json` | 新增依赖与 `gen:library-api`、`copy-pdf-assets` 脚本。 |
| `app/api/library/[...path]/route.ts` | 新建；仿 `app/api/mail/[...path]/route.ts`，补 `HEAD`、`Range`、流式上传。 |
| `app/[locale]/library/layout.tsx` | `requireOwner()` + `dynamic = "force-dynamic"`。 |
| `lib/i18n/navigation.ts` | 无需改；`Link` 已支持 locale。 |
| `app/sitemap.ts` | **不要**把 `/library` 加进 sitemap；页面 metadata 已 `noindex`。 |
| 部署 Nginx（仓库外） | 站点 `/api/library/` 放宽到 200m / 300s / `proxy_request_buffering off`；library 子域 `proxy_set_header Cookie "";`；`PAPERDB_URL` 用共享 Docker 网络名，不能写 `127.0.0.1` |
| `middleware.ts` | 无需改；`/library` 走默认 locale 路由。 |

**不要做**：把 `publications.yaml` 迁到文献库、把 `/ccf` `/cas` 改为后端取数、在站点根布局加全局 Provider（只包 library 子树）。

---

## 14. 前端实施顺序

1. **骨架**：代理路由 + server client + layout/guard + 空列表页 + 导航入口 + i18n key。
2. **列表**：筛选/排序/URL、虚拟滚动、徽章、批量条（先用 stub/真实 M2 数据）。
3. **详情**：元数据展示与内联编辑、作者/标识符、引用列表、文件列表。
4. **导入**：标识符 + BibTeX + PDF 上传、任务进度。
5. **PDF**：阅读器、全文搜索、批注创建/列表。
6. **设置**：token、导出链接、citekey、数据集、存储。
7. **命令面板**、移动端适配、错误态/空态打磨。
8. **图谱**（若时间允许）：d3-force + 邻接，否则隐藏入口。

每步都保持"可上线"：列表与导入先跑通，PDF 阅读器不应阻塞日常录入。


---

## 15. M5 实现状态（2026-11-25）

已完成并验证（`pnpm build` / `tsc` / `eslint`，Playwright 截图）：

- `/library` 列表：搜索、状态/类型筛选、集合/标签/视图/项目入口、批量改状态/打标签、文件/批注计数、分页。
- `/library/papers/[ref]` 详情：卷期页/摘要/作者/CCF/CAS/标识符/标签/集合、状态/评分/星标、上传 PDF、BibTeX 下载、批注列表。
- `/library/import`：标识符批量导入、BibTeX 上传、任务轮询与逐条结果；最近导入。
- `/library/settings`：服务状态、个人 token 创建/撤销、库级导出链接、数据集/存储。
- `/library/collections`：建集合、树、删除（promote）。
- `/library/papers/[ref]/read`：pdf.js 双页（默认）/单页、页码跳转、深色反色（默认开）、批注叠层、PDF 内全文搜索。
- 代理 `app/api/library/[...path]/route.ts`：owner-only、路径白名单、同源写请求校验、Range/HEAD、服务 token 仅服务端。
- 导航：主人专属「文献库」入口；`messages` 增加 `library` 命名空间。
- 依赖：`@tanstack/react-query`（为后续视图状态预留）、`@tanstack/react-virtual`（列表虚拟化预留）、`pdfjs-dist`；`public/pdf.worker.min.mjs` 由 pdfjs-dist 复制。

### 已知差距

- 列表暂未接虚拟滚动（`PaperTable` 一次渲染一页 ≤50 条），搜索/分页已可用。
- 批注创建 UI 尚未接文本选择（后端 API 已就绪，详情/阅读器能展示与跳转）；下一步 M5b 补 selection → rects。
- 视图/项目创建 UI 未做（API 已就绪），当前只读入口。
- 代理上传当前 `arrayBuffer` 缓冲；200MB 慢网需改为 streaming（Nginx `proxy_request_buffering off` 已写）。
- `/library` metadata `noindex` 尚未加，部署前补。
- 命令面板/移动端底部批量条未做。
