# 部署与运维（M7）

> 生产拓扑：**GitHub Actions → 阿里云 ACR（构建镜像）→ VPS（只拉取运行）**。
> API 只绑 `127.0.0.1:9720`，由站点 nginx 反代；数据库与文件卷不暴露。

## 1. 拓扑

```
GitHub main ──push──▶ Actions deploy.yml
                          │ 打 release-v<版本> 标签（触发 ACR 构建）
                          ▼
                      阿里云 ACR ── 私有镜像 ──▶ <REGISTRY>/<NAMESPACE>/ysy-paperdb:<版本>
                          │
                          ▼  ssh：docker login → pull → 重打 paperdb:prod → compose up
VPS  ~/paperdb/
  ├─ deploy/paperdb-compose.prod.yml   # 生产 compose（scp 同步）
  ├─ .env（600，DB 密码/服务 token/TOTP secret）
  └─ datasets/{ccf-2026.json,cas-2025.json}
容器：paperdb-paperdb-api-1 / paperdb-paperdb-worker-1 / paperdb-paperdb-db-1
卷：paperdb_paperdb-db / paperdb_paperdb-files
```

**VPS 上不构建镜像。** VPS 在国内，Docker Hub（`python:3.12-slim`）与 PyPI 直连都会卡住或截断
（实测 `docker compose --build` 跑 17 分钟后因 `watchfiles` wheel 解压错误失败）。镜像统一由 ACR
构建，VPS 只从国内仓库 `docker pull`。仓库里 `deploy/paperdb-compose.prod.yml` 的 build args
（`PIP_INDEX_URL` / `UV_INDEX_URL`）只用于本地或 CI 调试构建，把包源指向镜像站。

## 2. 发布（日常）

push 到 `main` 即自动发布；`**/*.md` 与 `adapters/**` 的改动不触发（镜像内容不变）。

1. 版本号 = `<UTC 提交日期>-<短 SHA>`，例如 `2026.11.28-f163ae1`
2. 推标签 `release-v<版本>` → ACR 构建 `ysy-paperdb:<版本>`
3. 轮询镜像就绪（上限 45 分钟；ACR 单次构建上限 30 分钟）
4. scp 生产 compose 文件到 `~/paperdb/deploy/`
5. SSH 到 VPS：`docker login` → `docker pull` → `docker tag … paperdb:prod` →
   `docker compose --env-file .env -f deploy/paperdb-compose.prod.yml up -d`（**不带 `--build`**）
6. 等 `http://127.0.0.1:9720/readyz`，再冒烟 `https://library.shaoyuanyu.cn/healthz`

手动触发与回滚：

```bash
# 重新发布当前 main
gh workflow run deploy -R shaoyuanyu/ysy-paperdb

# 回滚：填一个旧的 ACR 镜像版本，不重新构建
gh workflow run deploy -R shaoyuanyu/ysy-paperdb -f version=2026.11.27-abc1234
```

ACR 里的镜像版本永不覆写，所以回滚就是重新 pull 旧 tag。

> `--env-file .env` 不能省：compose 文件在 `deploy/` 下，项目目录随之变成 `deploy/`，
> 而变量在 `~/paperdb/.env`；不显式指定会报 `PAPERDB_SERVICE_TOKEN is missing`。

## 3. CI 配置（只列名称，值不入库）

| 类型 | 名称 | 用途 |
|---|---|---|
| variable | `REGISTRY` | ACR 公网地址 |
| variable | `IMAGE_NAMESPACE` | ACR 命名空间 |
| secret | `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` | ACR 访问凭证 |
| secret | `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` | 部署用户的 SSH 私钥 |
| secret（可选） | `AUTOMERGE_TOKEN` | 推标签用的 PAT；`GITHUB_TOKEN` 推的事件不保证送达 ACR |

ACR 仓库须与 GitHub 仓库同名（`ysy-paperdb`），绑定 `shaoyuanyu/ysy-paperdb`，
构建规则 `tags:release-v$version`、上下文 `/`、Dockerfile `Dockerfile`。

## 4. 首次部署 / 换机

VPS 上准备目录与变量（不要把 `.env` 提交进仓库）：

```bash
git clone <repo> ~/paperdb        # 或 rsync；只需要 deploy/ 与 .env
cd ~/paperdb
cp .env.example .env && chmod 600 .env
# 填：POSTGRES_PASSWORD / PAPERDB_SERVICE_TOKEN / PAPERDB_TOKEN_PEPPER / PAPERDB_TOTP_SECRET
#     PAPERDB_PUBLIC_URL=https://shaoyuanyu.cn
```

然后拉取镜像并启动：

```bash
echo "<密码>" | docker login <REGISTRY> -u <用户名> --password-stdin
docker pull <REGISTRY>/<NAMESPACE>/ysy-paperdb:<版本>
docker tag  <REGISTRY>/<NAMESPACE>/ysy-paperdb:<版本> paperdb:prod
cd ~/paperdb
docker compose --env-file .env -f deploy/paperdb-compose.prod.yml up -d
curl -fsS http://127.0.0.1:9720/readyz
```

`paperdb-api` 启动时跑 `alembic upgrade head`；`paperdb-worker` 等 API healthy 后启动，
处理导入/抽取/引用同步/GC。**不要只起 API 不起 worker**，否则导入会一直 pending。

导入 CCF/CAS 数据集（数据集不进镜像）：

```bash
docker compose --env-file .env -f deploy/paperdb-compose.prod.yml \
  exec paperdb-api paperdb datasets import ccf --file /app/datasets/ccf-2026.json --version 2026 --valid-from 2026
docker compose --env-file .env -f deploy/paperdb-compose.prod.yml \
  exec paperdb-api paperdb datasets import cas --file /app/datasets/cas-2025.json --version 2025 --valid-from 2025
```

站点与 API 同机时，前端用 `PAPERDB_URL=http://127.0.0.1:9720`（或共享 docker 网络的服务名）。

## 5. 前端（Next.js）

`.env.local` / 部署环境（**不是** `NEXT_PUBLIC_`）：

```dotenv
PAPERDB_URL=http://127.0.0.1:9720
PAPERDB_SERVICE_TOKEN=<与后端相同>
```

`/library/**` 与 `/api/library/**` 都走站点 TOTP 会话；未登录访问 `/library` 会 302 到 `/login`。

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

## 6. 备份 / 恢复

```bash
# DB（容器 paperdb-paperdb-db-1）
docker exec paperdb-paperdb-db-1 pg_dump -U paperdb -Fc paperdb > paperdb-$(date +%F).dump
# PDF 文件卷
docker run --rm -v paperdb_paperdb-files:/data -v "$PWD":/backup alpine \
  tar czf /backup/paperdb-files-$(date +%F).tar.gz -C /data .

# 恢复
docker exec -i paperdb-paperdb-db-1 pg_restore -U paperdb -d paperdb --clean --if-exists < paperdb-YYYY-MM-DD.dump
docker run --rm -v paperdb_paperdb-files:/data -v "$PWD":/backup alpine \
  tar xzf /backup/paperdb-files-YYYY-MM-DD.tar.gz -C /data
```

`paperdb_paperdb-files` 卷是内容寻址 blob；DB 里 `pdf_file` 行与 blob 必须一起恢复。
删除论文是软删；清理 blob 走 worker `gc_blob`（先查引用计数），不要手工删卷内文件。

## 7. 安全清单

- API 只绑 `127.0.0.1`；无 CORS（`PAPERDB_CORS_ORIGINS` 留空）。
- `PAPERDB_SERVICE_TOKEN` / `PAPERDB_TOKEN_PEPPER` / `POSTGRES_PASSWORD` / `PAPERDB_TOTP_SECRET`
  只存在于 VPS 的 `.env`（600）与 GitHub Secrets，**永不入库、不进镜像**。
- ACR 镜像仓库设为私有；VPS 用单独的 ACR 访问凭证登录。
- 部署用专用 SSH 密钥（`github-actions-deploy`），私钥存在 VPS 的 `~/.ssh/` 与 GitHub Secret，
  权限 600；轮换时删旧公钥即可。
- `PAPERDB_TOKEN_PEPPER` 轮换会让所有个人/导出 token 失效，轮换前先在 UI 重发。
- `PAPERDB_ENV=prod` 关闭 `/docs`；不要在生产打开。
- 备份包含未发表论文元数据/PDF，按私有数据对待。

## 8. 上线 smoke

```bash
# 进程与依赖
curl -fsS http://127.0.0.1:9720/healthz
curl -fsS http://127.0.0.1:9720/readyz
curl -fsS -H "Authorization: Bearer $PAPERDB_SERVICE_TOKEN" http://127.0.0.1:9720/v1/meta | jq '.datasets'

# 新增的 agent 读接口
curl -fsS -H "Authorization: Bearer $PAPERDB_SERVICE_TOKEN" http://127.0.0.1:9720/v1/library/stats
curl -fsS -H "Authorization: Bearer $PAPERDB_SERVICE_TOKEN" \
  "http://127.0.0.1:9720/v1/files/<fileId>/text?from=1&maxPages=1" | jq '.textStatus'
curl -fsS -o /dev/null -w '%{http_code} %{content_type}\n' -H "Authorization: Bearer $PAPERDB_SERVICE_TOKEN" \
  "http://127.0.0.1:9720/v1/files/<fileId>/pages/1/image"

# 前端（先 TOTP 登录）
curl -I https://shaoyuanyu.cn/library      # 未登录 302 /login；登录 200
```

## 9. 定时任务（建议）

- 每周引用同步：`POST /v1/citations/sync`（不传 paperIds，只处理未同步；需要 `admin` scope 的 token）。
- `paperdb-worker` 常驻；GC 是队列任务，无需额外 cron。
- 备份每日一次，保留 14 天；恢复演练每季度一次。
