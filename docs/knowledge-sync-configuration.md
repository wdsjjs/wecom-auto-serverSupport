# 知识库同步配置与启动检查

本文记录知识库同步配置过程、同步命令、启动检查行为和常见故障处理。具体密钥、主机、token 只写入本机 `.env` 文件，不写入仓库。

## 1. 配置文件

同步配置写在两个本机文件里：

```text
codex-csbot-wecom/.env
wecom-gui/.env.local
```

这两个文件都包含密钥，已被 git ignore，权限应保持为 `600`，不得提交到远程。

## 2. PostgreSQL 配置

知识库后端可配置为 PostgreSQL：

```env
CSBOT_PG_DSN='postgresql://<user>:<password>@<host>:5432/<database>'
CSBOT_PG_CONNECT_TIMEOUT=8
```

`CSBOT_PG_CONNECT_TIMEOUT` 控制 PostgreSQL 连接超时秒数，避免网络或白名单异常时同步长时间卡住。

如果本机探测出现连接超时，需要检查 RDS 白名单、专有网络/VPN、DNS 解析以及本机到数据库 `5432` 端口的连通性。

## 3. 飞书配置

飞书多维表可以直接配置 app token；如果拿到的是 Wiki 链接，需要先解析 Wiki 节点：

```text
https://<tenant>.feishu.cn/wiki/<wiki_node_token>
```

链接中的最后一段是 Wiki 节点 token，不是多维表 app token。需要通过飞书 `wiki/v2/spaces/get_node` 解析出：

```text
title=AI 知识库
obj_type=bitable
obj_token=<bitable_app_token>
```

同步配置使用：

```env
FEISHU_APP_ID='<app_id>'
FEISHU_APP_SECRET='<app_secret>'
FEISHU_APP_TOKEN='<bitable_app_token>'
LARK_BITABLE_APP_TOKEN='<bitable_app_token>'
```

已验证该多维表包含当前代码内置的 AI 知识库表：

| 表名 | 行数 dry-run |
| --- | ---: |
| `1 产品发货状态` | 49 |
| `2 促单活动` | 1 |
| `3 限时通知` | 25 |
| `5 产品常规信息` | 50 |
| `6 论文表` | 253 |
| `7 L0级注意事项` | 7 |
| `10 补剂推荐` | 89 |
| `12原料专利、认证等材料` | 46 |
| `13 产品检测报告` | 254 |
| `15 发货状态通用话术库` | 7 |
| `16 异常物流话术` | 5 |

飞书 dry-run 汇总：`rows=786`。

## 4. 微伴配置

当前微伴快捷回复配置：

```env
WEIBAN_BASE_URL='https://open.weibanzhushou.com'
WEIBAN_CORP_ID='<redacted>'
WEIBAN_SECRET='<redacted>'
```

已验证 access token 获取成功，快捷回复分组接口可返回 `group_count=118`。微伴同步默认只拉一级分组，因为当前接口返回中一级分组已经包含其子分组快捷回复，继续逐个拉子分组会产生大量重复请求。

微伴完整同步已增加分组级容错：某个分组接口读取超时或失败时，该分组会进入 `skipped_groups`，其余分组继续同步，避免整次同步崩掉。上线前需要关注 `skipped_groups` 是否为空。

微伴同步已增加阶段日志，默认写入 `stderr`，最终同步结果 JSON 仍写入 `stdout`。日志不会输出 `access_token`、`WEIBAN_SECRET` 等密钥，只记录请求路径、分组 ID、分组名、分页 offset、返回数量、耗时和失败原因。

同步控制变量：

```env
WEIBAN_SYNC_LOG=1
WEIBAN_GROUP_FETCH_MODE=top_level
WEIBAN_SYNC_WORKERS=4
WEIBAN_SYNC_MAX_GROUPS=20
WEIBAN_REQUEST_TIMEOUT_SECONDS=10
WEIBAN_TOKEN_TIMEOUT_SECONDS=10
WEIBAN_REQUEST_DELAY_SECONDS=0.2
```

规则说明：

- `WEIBAN_GROUP_FETCH_MODE=top_level`：默认模式，只拉一级分组，本机验证可把 712 次分组级请求降到 118 次。
- `WEIBAN_GROUP_FETCH_MODE=all`：兼容模式，一级分组和子分组都拉取，用于排查是否有子分组内容未被父级覆盖。
- `WEIBAN_GROUP_FETCH_MODE=children_only`：只拉子分组，用于诊断父子数据差异。
- `WEIBAN_SYNC_WORKERS=4`：受控并发数，上限被代码限制为 12，避免对微伴接口造成突发压力。
- `WEIBAN_REQUEST_DELAY_SECONDS=0.2`：所有 worker 共享的请求启动间隔，多个并发 worker 也会按该间隔错峰发起 API 请求。
- `WEIBAN_SYNC_MAX_GROUPS=20`：诊断时限制拉取前 N 个分组，正式同步不设置。

快速定位命令：

```bash
cd codex-csbot-wecom
WEIBAN_GROUP_FETCH_MODE=top_level \
WEIBAN_SYNC_WORKERS=4 \
WEIBAN_SYNC_MAX_GROUPS=20 \
WEIBAN_REQUEST_TIMEOUT_SECONDS=10 \
WEIBAN_TOKEN_TIMEOUT_SECONDS=10 \
../.venv/bin/python -m csbot weiban sync --dry-run \
  > /tmp/weiban-sync.json \
  2> /tmp/weiban-sync.log

tail -n 80 /tmp/weiban-sync.log
```

如果需要关闭诊断日志：

```bash
WEIBAN_SYNC_LOG=0 ../.venv/bin/python -m csbot weiban sync --dry-run
```

当前本机诊断结论：

- token 获取正常，约数百毫秒完成。
- `/open-api/quick_reply_v3/group/list` 正常，返回 118 个一级分组。
- 旧逻辑会把一级分组和子分组都展开拉取，本次展开后为 `flattened_count=712`。
- 当前优化后默认只选 `selected_count=118` 个一级分组，并通过 `WEIBAN_SYNC_WORKERS` 进行受控并发。
- 本机完整 dry-run 使用 `WEIBAN_GROUP_FETCH_MODE=top_level`、`WEIBAN_SYNC_WORKERS=4`、`WEIBAN_REQUEST_DELAY_SECONDS=0.2`，约 27.3 秒完成，结果为 `rows=3997`、`skipped_groups=[]`。
- 许多子分组返回内容与父分组重复，日志中可看到大量 `duplicate_count`，例如父分组 `NMN` 先新增内容，后续其子分组多为重复项。
- 若日志最后停在 `api_get_started` 且长时间没有 `api_get_done/api_get_failed`，说明正在等待微伴接口响应；若出现 `db_write_started` 后长时间无 `db_write_done`，才是数据库写入卡住。

## 5. Mem0 配置

当前配置：

```env
CSBOT_MEM0_URL='http://127.0.0.1:8888'
CSBOT_MEM0_API_KEY='<redacted>'
CSBOT_MEM0_GLOBAL_USER_ID='global-kb'
```

当前本机探测结果：`127.0.0.1:8888` 连接被拒绝，说明本地 Mem0 服务尚未启动。启动 Agent 时会打印 warning，但默认不阻塞客服启动。需要严格要求 Mem0 可用时设置：

```env
WECOM_AGENT_STRICT_KNOWLEDGE_PREFLIGHT=1
```

## 6. 同步命令

检查配置：

```bash
cd codex-csbot-wecom
../.venv/bin/python -m csbot doctor
```

飞书 dry-run：

```bash
../.venv/bin/python -m csbot feishu sync --dry-run
```

微伴 dry-run：

```bash
../.venv/bin/python -m csbot weiban sync --dry-run
```

查看同步状态：

```bash
../.venv/bin/python -m csbot sync status --max-age-seconds 86400
```

启动前按数据时间自动同步：

```bash
../.venv/bin/python -m csbot sync if-stale --max-age-seconds 86400 --skip-mem0
```

全量同步：

```bash
../.venv/bin/python -m csbot sync all --progress
```

仅更新 SQL/PG，不导入 Mem0：

```bash
../.venv/bin/python -m csbot sync all --progress --skip-mem0
```

本地 Excel 兜底导入：

```bash
scripts/sync-local-knowledge.sh --vector
```

当前 `sync all` 行为：

- 飞书同步和微伴同步并行执行。
- 每个同步源默认最多重试 3 次，可用 `CSBOT_SYNC_RETRY_ATTEMPTS` 或 `--attempts` 调整。
- 重试间隔默认 2 秒，可用 `CSBOT_SYNC_RETRY_DELAY_SECONDS` 或 `--retry-delay` 调整。
- 某个源重试失败后会写入 `knowledge_sync_log`，并在返回 JSON 中标记失败。
- `kb_docs/kb_aliases` 重建依赖飞书和微伴源数据，只有两个源都成功后才执行。
- Mem0 导入依赖 KB 重建成功；启动前默认跳过 Mem0，避免本地 Mem0 未启动时阻塞客服。
- 成功后会写入 `kb_meta`：`sync.<source>.last_success_at` 和 `sync.all.last_success_at`。

启动前同步控制变量：

```env
WECOM_AGENT_KNOWLEDGE_SYNC_ON_START=1
CSBOT_SYNC_MAX_AGE_SECONDS=86400
CSBOT_SYNC_STARTUP_KB_VERSION=startup
CSBOT_SYNC_STARTUP_SKIP_MEM0=1
CSBOT_SYNC_RETRY_ATTEMPTS=3
CSBOT_SYNC_RETRY_DELAY_SECONDS=2
```

规则说明：

- `sync if-stale` 会先读取 `kb_meta.sync.all.last_success_at`。
- 如果距离上次完整成功同步不超过 86400 秒，即 1 天，直接跳过。
- 如果缺失、格式异常或超过 1 天，则触发同步。
- 启动脚本调用时带 `--non-blocking`，同步失败会记录日志并继续启动 Agent。

## 7. 启动检查

`wecom-gui/scripts/wecom-agent start` 会在启动前执行 preflight：

1. 检查 `.env.local` 是否存在。
2. 检查 Python、screen、osascript、swift 等运行依赖。
3. 检查 WeCom GUI 自动化能力。
4. 检查 PostgreSQL 连接，失败时阻塞启动。
5. 检查飞书 token 和多维表列表，失败时输出 warning。
6. 检查微伴 token，失败时输出 warning。
7. 检查 Mem0 `/openapi.json`，失败时输出 warning。
8. 检查知识库同步时间，1 天内跳过，超过 1 天执行并行同步。

启动日志头会写入：

```text
AI_CONCURRENCY=max:20 text:15 image:5
KNOWLEDGE=pg:configured feishu:configured weiban:configured mem0:configured
```

如果设置：

```env
WECOM_AGENT_STRICT_KNOWLEDGE_PREFLIGHT=1
```

则飞书、微伴、Mem0 的 warning 也会升级为启动错误。

## 8. 当前状态记录

已完成：

- 飞书凭证配置链路已跑通。
- Wiki 节点可解析为 AI 知识库 bitable app token。
- 飞书 dry-run 可读取 11 张内置知识表，共 786 行。
- 飞书知识可同步到本机 SQLite 兜底库，并重建本地 `kb_docs/kb_aliases`。
- 微伴 token 和分组接口检查链路已跑通；完整同步已支持分组级超时跳过。
- 启动脚本已增加知识库配置检查与日志提示。
- PostgreSQL 连接增加默认 8 秒超时。

待处理：

- 目标机器需要确认 PostgreSQL 网络连通性后再执行正式 PG 写入。
- 本地 Mem0 服务需要启动后才能刷新 `global-kb`。
- 微伴完整同步耗时较长，建议在 PG 网络打通后与 `sync all --progress` 一起跑。
