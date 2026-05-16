# GrowthPress AI

内容自动化数字员工 — 调研 → 撰写 → 审核 → 人工确认 → 多平台发布 → 撤销窗 全流程, 长驻 daemon (launchd) 跑.

---

## 6 模块流水线

```
   topics.yaml cron
   邮件喂选题 / 完整文章
          │
          ▼
   [m1 scout_writer]   anthropic SDK + DeepSeek 接管 + web_search 调研近 7 天
          │             生成 markdown draft + DraftSchema JSON 元数据
          ▼  state=new
   [m2 reviewer]       3 路并行: 合规短路 / 质量评分 / 平台适配
          │             flash + pro 按任务路由 (llm_router)
          ▼  state=approved
   [m3 approver]       SMTP 发 [APV-{pub_id}] 邮件 + INSERT approvals (24h 窗)
          │             审核员回信 ok/改/drop → parse_reply 决定
          ▼  state=publishing
   [m4 publisher]      并发多平台发布 + 单平台 wait_for 超时 + 落 publications
          │             插件架构 (entry_point growthpress.platforms)
          ▼  state=published
       (24h 撤销窗)    审核员回 [PUB-*] 邮件触发撤销
          │
          ▼  state=archived

   [m5 mailbox]        IMAP poller 30s 扫 INBOX, 6 通道 dispatcher
                       (APV/PUB/REJECT 回信 + 用户喂选题/喂内容 + ROUTE-* 路线管理)
```

## 行程守护 (3 层防线)

| 层 | 任务 | 周期 |
|---|---|---|
| 1 进程层 | launchd KeepAlive (crash 重启) | — |
| 2 恢复层 | `resume_pending` 启动钩子 (publishing/pending 接续) | 启动时 |
| 3 卡死层 | `watchdog_task` 扫 state 超时 (reset/alert/防循环) | 5min |
| 4 审计层 | `state_log` 每次 state 变迁记录 (debug/pending_long 时间线) | — |
| 5 看板层 | `daily_digest_task` 每天 UTC 0:00 发统计邮件 | 24h |

## 安装

需要 Python ≥ 3.11 + [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/PsChina/GrowthPress-AI.git
cd GrowthPress-AI
uv sync
```

私有 / 第三方渠道插件 (按需):

```bash
# 公开渠道直接 git+url 或 PyPI
uv pip install <channel-package>

# 私有渠道 (内部 repo) 走 ssh
uv pip install git+ssh://git@github.com/<org>/<channel-package>.git
```

## 配置

### 交互向导 (推荐)

```bash
uv run growthpress-setup
```

按 `[1/4] LLM → [2/4] SMTP → [3/4] IMAP → [4/4] 通知` 四步问完, 自动写入 `.env` (chmod 600)
+ 顺手 `cp config/topics.example.yaml → config/topics.yaml`. 密码字段用 getpass 不在
终端回显. 已有 `.env` 时显示旧值作默认 (回车保留).

或手动:

```bash
cp .env.example .env
```

填入:

| 字段 | 用途 |
|---|---|
| `LLM_API_KEY` | DeepSeek API key (默认走 DeepSeek 接管 Anthropic 协议) |
| `LLM_BASE_URL` | `https://api.deepseek.com/anthropic` (切真 Anthropic 改 `https://api.anthropic.com`) |
| `LLM_MODEL` / `LLM_MODEL_FLASH` / `LLM_MODEL_PRO` | 模型 ID (默认 `deepseek-v4-pro[1m]` / `-flash[1m]`) |
| `SMTP_HOST/PORT/USER/PASS` | 发邮件 (m3 APV / 日报). Gmail 用 App Password |
| `IMAP_HOST/PORT/USER/PASS/FOLDER` | 收邮件 (m5 mailbox), 同 Gmail 账号即可 |
| `NOTIFY_TO` | 审核员邮箱 (APV/REJECT/PUB/日报都发到这里) |

### `config/topics.yaml`

```bash
cp config/topics.example.yaml config/topics.yaml
```

改成自己的主题列表 + cron interval. 默认 `schedule.enabled=false` 保护防止启动就烧 LLM, 验过后改 `true`.

## 运行

### 单模块测试

```bash
# m1 单次调研 + 落盘 (不写 db)
uv run python -m growthpress.scout_writer "你的选题"

# m2 单次审核 (draft 须已在 db state=new)
uv run python -m growthpress.reviewer <draft_id>
```

### Daemon (8 task TaskGroup)

```bash
uv run growthpress --log-level INFO
```

启动会跑 8 个 long-running task: `db_writer / scheduler / m3_pump / m4_pump / imap_poller / pending_watch / watchdog / daily_digest`. 未配置的服务 (SMTP/IMAP) 对应 task 优雅 exit, 不挂 daemon.

启动前 `preflight` 模块会扫 `.env`:
- 缺关键字段 (`LLM_API_KEY`) 时, tty 模式询问"现在跑 growthpress-setup?", 非 tty (launchd) 仅 log warning
- 缺建议字段 (SMTP/IMAP/NOTIFY_TO) 时只 log 提示, 不阻塞

各 CLI 入口 (`m1` / `m2` / `e2e_test.py`) 也同样 preflight, 缺关键字段直接 exit 1.

### 部署 (macOS launchd)

```bash
# 改 deploy/com.growthpress.daemon.plist 里的路径 → 你的 GrowthPress-AI 绝对路径
cp deploy/com.growthpress.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.growthpress.daemon.plist

# 检查状态 / 日志
launchctl list | grep growthpress
tail -f /tmp/growthpress.out.log
tail -f /tmp/growthpress.err.log

# 关闭
launchctl unload ~/Library/LaunchAgents/com.growthpress.daemon.plist
```

## 架构

```
growthpress/
├── orchestrator.py    daemon 主入口 (TaskGroup 编排 + signal + resume_pending)
├── db.py              SQLite + WAL + 单 writer + db.transition CAS
├── core/              m0: settings / llm_client / llm_router (Task → flash/pro)
├── scout_writer/      m1: streaming + web_search + DraftSchema
├── reviewer/          m2: 合规短路 + 质量 + 平台 3 路 + 升级 hook
├── approver/          m3: APV 邮件 + 24h 窗 + reply 解析 + 4 路 dispatch
├── publisher/         m4: 多平台并发 + 单平台 wait_for + ContentType 路由
│   ├── base.py        Publisher Protocol + Content/PublishResult TypedDict
│   ├── agent.py       discover_platforms (entry_point) + LegacyImagePublisher
│   └── m4_pump.py     m4_pump_runs 调度
├── mailbox/           m5: IMAP poller + 6 通道 dispatcher + 启发式判别
├── watchdog/          行程守护第 3 层 (state 卡死检测)
└── daily_digest/      行程守护第 5 层 (24h 统计日报)
plugins/                渠道实现 (.gitignore 排除, 公开渠道白名单 unignore)
config/
├── topics.yaml         个人主题路线 (gitignore)
└── topics.example.yaml 模板
data/                   sqlite + 落盘 draft markdown (gitignore)
deploy/                 launchd plist 等部署文件
```

数据库 schema (`data/runs.db`):

```
drafts          每篇 draft 一行, state 字段是单一真相源
reviews         m2 每轮审核结果
approvals       m3 APV 邮件 + 回信状态
publications    m4 各平台发布记录
retractions     PUB 回信触发的撤销
state_log       每次 state 变迁记录 (行程守护审计)
llm_calls       LLM 调用统计 (经济性追踪)
```

## 添加新渠道

每个渠道 = 独立 pip 包, 实现 `Publisher` Protocol, 通过 entry_point 注册:

```toml
# 你的 channel-publisher/pyproject.toml
[project.entry-points."growthpress.platforms"]
yourplatform = "your_publisher.publisher:YourPublisher"
```

```python
# your_publisher/publisher.py
from growthpress.publisher import Content, ContentType, Publisher, PublishResult

class YourPublisher:
    name = "yourplatform"
    supported_types = [ContentType.IMAGE_NOTE, ContentType.VIDEO]

    async def publish(self, content: Content, *, dry_run: bool = False) -> PublishResult:
        # 实现你的发布逻辑
        ...
        return {
            "success": True, "dry_run": dry_run, "platform": self.name,
            "url": "...", "screenshot": None, "error": None,
            "error_detail": None, "elapsed_sec": 1.5,
        }
```

`uv pip install -e ./plugins/your-publisher` 后 daemon 重启自动 discover.

## License

MIT
