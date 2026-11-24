# 部署与运维（M7）

> 最小可用拓扑：**单个 paperdb API + 单个 worker + PostgreSQL 17 + 本地卷**；Next.js 站点做 owner gate + 反向代理。
> 后端只绑 `127.0.0.1`，不对公网开端口；数据库/卷不暴露。

## 1. 后端

```bash
cd ysy-paperdb
cp .env.example .env
# 必须改：
#   PAPERDB_SERVICE_TOKEN=pdb_svc_...（openssl rand -hex 24）
#   PAPERDB_TOKEN_PEPPER=...（openssl rand -hex 32，轮换会让个人 token 全失效）
#   POSTGRES_PASSWORD=...
#   PAPERDB_PUBLIC_URL=https://shaoyuanyu.cn（用于生成 .bib 导出链接）
# 可选：OPENALEX_MAILTO / CROSSREF_MAILTO / S2_API_KEY

podman compose build
podman compose up -d
podman compose ps
curl -fsS http://127.0.0.1:9720/healthz
curl -fsS http://127.0.0.1:9720/readyz
```

`paperdb-api` 启动时跑 `alembic upgrade head`；`paperdb-worker` 等待 API healthy 后启动，处理导入/抽取/引用同步/GC。**不要只起 API 不起 worker**，否则导入会一直 pending。

导入 CCF/CAS 数据集（示例；数据集不随公共镜像发布）：

```bash
podman compose exec paperdb-api paperdb datasets import ccf --file /app/datasets/ccf-2026.json --version 2026 --valid-from 2026
podman compose exec paperdb-api paperdb datasets import cas --file /app/datasets/cas-2025.json --version 2025 --valid-from 2025
podman compose exec paperdb-api paperdb datasets list
```

生产网络：若 Next 站点与本 compose 在同一 Docker/Podman 网络，设置
`PAPERDB_NETWORK=<站点网络名>`，前端 `PAPERDB_URL=http://paperdb-api:9720`。
当前 compose 默认把 API 映射到 `127.0.0.1:9720`，只适合站点同机/Nginx 反代。

## 2. 前端（Next.js）

`.env.local` / 部署环境（**不是** `NEXT_PUBLIC_`）：

```dotenv
PAPERDB_URL=http://127.0.0.1:9720   # 或共享网络 http://paperdb-api:9720
PAPERDB_SERVICE_TOKEN=pdb_svc_...   # 与后端同一个 service token
```

```bash
pnpm install
pnpm build
pnpm start
```

`/library/**` 与 `/api/library/**` 都走站点 TOTP 会话；未登录访问 `/library` 会 302 到 `/login`。
`public/pdf.worker.min.mjs` 是 pdf.js worker，随仓库发布；升级 `pdfjs-dist` 后从
`node_modules/pdfjs-dist/build/pdf.worker.min.mjs` 重新复制。

### Nginx / 反代

```nginx
location /api/library/ {
  proxy_pass http://127.0.0.1:3000;
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  client_max_body_size 200m;
  proxy_request_buffering off;   # PDF 上传流式
  proxy_read_timeout 300s;
}
```

若使用 library 子域：保留站点 Cookie（`owner_session`），不要清空；后端只接受
`/api/library` 代理，服务 token 永远不进浏览器。

## 3. 备份 / 恢复

```bash
# DB
podman exec paperdb-pg pg_dump -U paperdb -Fc paperdb > paperdb-$(date +%F).dump
# PDF 文件卷
podman volume export paperdb_paperdb-files > paperdb-files-$(date +%F).tar

# 恢复
podman exec -i paperdb-pg pg_restore -U paperdb -d paperdb --clean --if-exists < paperdb-YYYY-MM-DD.dump
podman volume import paperdb_paperdb-files paperdb-files-YYYY-MM-DD.tar
```

`paperdb-files` 卷是内容寻址 blob；DB 里 `pdf_file` 行与 blob 必须一起恢复。
删除论文是软删；清理 blob 走 worker `gc_blob`，会先查引用计数，不要手工删卷内文件。

## 4. 升级

```bash
git pull
podman compose build
podman compose up -d          # entrypoint 自动迁移
podman compose ps
curl -fsS http://127.0.0.1:9720/readyz
```

回滚：镜像 tag + 数据库迁移不可逆时从备份恢复；发布前先 `pg_dump`。

## 5. 安全清单

- 后端端口只绑 `127.0.0.1`；无 CORS（`PAPERDB_CORS_ORIGINS` 留空）。
- 前端 `PAPERDB_SERVICE_TOKEN` 只在服务端；`/api/library` 代理先校验 owner 会话，再做路径白名单。
- PDF content 响应带 `Content-Security-Policy: sandbox`、`nosniff`，不内联 HTML。
- `PAPERDB_TOKEN_PEPPER` 轮换会让所有个人/导出 token 失效，轮换前通知自己重发。
- `PAPERDB_ENV=prod` 关闭 `/docs`；不要在生产打开。
- 备份包含未发表论文元数据/PDF，按私有数据对待。

## 6. 上线 smoke

```bash
# 后端
curl -fsS http://127.0.0.1:9720/healthz
curl -fsS -H "Authorization: Bearer $PAPERDB_SERVICE_TOKEN" http://127.0.0.1:9720/v1/meta | jq '.datasets'
# 前端（先 TOTP 登录）
curl -I https://shaoyuanyu.cn/library      # 未登录 302 /login；登录 200
# 导入
curl -fsS -X POST -H "Authorization: Bearer $PAPERDB_SERVICE_TOKEN" -H 'content-type: application/json' \
  -d '{"items":["arXiv:1706.03762"]}' http://127.0.0.1:9720/v1/imports/identifiers
# 等 worker，再查 job/imports
```

## 7. 定时任务（建议）

- 每周引用同步：`POST /v1/citations/sync`（不传 paperIds，只处理未同步；可加 cron）。
- 每日 `paperdb worker` 已常驻；GC 是队列任务，无需额外 cron。
- 备份每日一次，保留 14 天；恢复演练每季度一次。
