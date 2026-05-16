---
name: growthpress-post
description: 用 GrowthPress 工具腰带把一篇内容(图文笔记 / 文章 / 短笔记)发到指定社交平台 (当前: 小红书 image_note, 后续会加掘金/CSDN/知乎/微博等). 整个工作流由 Claude Code 主对话驱动 — Claude 自己做调研/找图/下载/写文, 然后调 `python -m growthpress draft new` + `python -m growthpress draft publish-now` 真发. **触发场景**: 用户在 GrowthPress-AI 仓库里说 "发一篇 xxx 到 小红书 / 掘金 / 知乎"、"用 GrowthPress 发 xxx"、"测试 GrowthPress 工作流"、给一个主题让你写完直接发, 或显式 /post 命令. **关键**: 真发到用户公开账号是不可逆动作, 真发前必须 dry_run 看预览截图 + 让用户最终确认. 图源必须 CC0 (Unsplash / Pexels / Pixabay) 或用户自己提供, 不可用普通网页抓的图 (版权 + 肖像权风险). 标题/正文长度按目标平台约束 (小红书标题 ≤20 字).
---

# growthpress-post — 用 GrowthPress 发一篇内容

## 工作流总览

```
用户 → Claude (我)
        │
        ├─ WebSearch          找候选含图页面
        ├─ WebFetch           抽页面里图直链
        ├─ Bash curl          下载到 data/test-images/<batch>/
        ├─ Read 图            肉眼筛, 不合适重新换图
        ├─ 写文                自己生成短文 (基于调研)
        ├─ Bash 调 cli:       python -m growthpress draft new …
        ├─ Bash 调 cli:       python -m growthpress draft publish-now <id> --platforms xiaohongshu (dry_run 先)
        ├─ Read 预览截图       检查标题/正文/图序/字数
        └─ AskUserQuestion    最终确认真发? → 真发 (--real)
```

## 触发判定

| 用户说 | 触发 |
|---|---|
| "发一篇 xxx 到小红书" | ✓ 走全流程 |
| "用 GrowthPress 测试 xxx" | ✓ 走全流程 |
| "/post 关于 xxx" | ✓ 走全流程 |
| 给一个主题 + cwd 在 GrowthPress-AI | ✓ 默认按本 skill 走 |
| 仅讨论 GrowthPress 代码 (不发) | ✗ 不触发 |

## 关键约束 (违反不要做)

| 约束 | 原因 |
|---|---|
| 图源**只用** CC0 (Unsplash / Pexels / Pixabay) 或用户自己提供 | 普通网页抓的图侵犯版权 + 真人肖像无 model release |
| 真人肖像图发用户公开账号前 **告知风险** | Unsplash License 允许商用但**不豁免**肖像权 |
| 真发前**必须** dry_run 看预览截图 | xhs publisher 截图含红圈定位发布按钮, 提前发现标题超字 / 排版异常 |
| 真发前**必须** AskUserQuestion 让用户最终确认 | 不可逆 — 出现在用户公开账号, 推送可能已到达 |
| 小红书标题 ≤ 20 字 | publisher 不会自动截断, dry_run 截图右上角会显示 N/20 |
| 小红书正文 ≤ 1000 字 | 同上 |
| 小红书 image_note 图片 1-9 张 | publisher 校验, 缺图直接 `invalid_input` |
| 不用 LLM 生图 / Python 调 LLM 找图 | 当前架构是 Claude 主对话用前端工具做这事, 别走回头路 |

## 一定要避免的回头路

- ❌ 用 `growthpress.scout_writer.run()` — **已删, 历史错误方向**
- ❌ 用 `growthpress.reviewer.review()` — **已删**, 审核交给 Claude 自己判断, 或者纯规则
- ❌ Python 内部调 DeepSeek / Anthropic LLM 找文字 / 找图 — **错方向**
- ❌ `scripts/e2e_test.py` — **已删**, 是测旧 m1/m2/m3/m4 链路的
- ✓ Claude (我) 用 WebSearch + WebFetch + Bash curl 自己做这些事
- ✓ Python 只作为工具腰带, 通过 `python -m growthpress …` 提供 CLI

## 详细步骤

### Step 1: 用户给主题后, 先 WebSearch 找候选含图页面

```
WebSearch query="<topic> stock photo unsplash CC0"
```

或更具体, 比如 `WebSearch query="<topic> pexels free image"`.

**输出**: 几个候选页面 URL.

### Step 2: WebFetch 抽页面里的图直链

挑 1-2 个最有希望的页面, WebFetch 让 LLM 提取里面图直链:

```
WebFetch url="https://unsplash.com/s/photos/<keyword>"
         prompt="列出页面里前 6 张照片的图片直链 URL (images.unsplash.com 开头的 .jpg/.png 直链). 每行一个 URL, 不要其他文字, 不要 srcset, 只要每张图的主 src"
```

**预期输出**: 6 个 `https://images.unsplash.com/photo-XXX?fm=jpg&...` 直链.

### Step 3: curl 下载到本地

```bash
mkdir -p data/test-images/<topic-slug> && cd data/test-images/<topic-slug>
curl -sL -o 0.jpg "<url1>"
curl -sL -o 1.jpg "<url2>"
curl -sL -o 2.jpg "<url3>"
file *.jpg     # 验证是真 JPEG
```

- 用 `topic-slug` 当目录名 (例: `weekend-coffee`), 不同 topic 不要混
- 至少 1 张, 最多 9 张 (小红书 image_note 上限)
- 文件名 `0.jpg / 1.jpg / 2.jpg`, 数字小的会作为封面

### Step 4: Read 图肉眼筛

Claude 用 Read 工具看每张图, 筛掉:
- 主题不匹配的 (例: 找"咖啡馆"出了"咖啡豆"特写, 主题偏)
- 真人正脸明显侵权感的 (即使 CC0)
- 角度/构图怪的 (例: 玻璃反光重)
- 重复/相似的 (3 张图风格差异要够)

筛掉后如不够数量, 回 Step 1-3 重新换图源 (改 query).

### Step 5: 写短文

Claude 直接写 markdown 短文 — 基于调研内容, 不调外部 LLM:

| 平台 | 标题字数 | 正文字数 | 风格 |
|---|---|---|---|
| 小红书 image_note | ≤20 字 | ≤1000 字, 理想 200-500 | 短/口语/emoji/段落 ≤4 行/末尾 tag |
| 掘金 article | 主标题 + 副标 | 1500-5000 字 | 技术深度/代码块/小节 |
| 知乎专栏 | 类似掘金 | 同上 | 略学术 |

写完先存到一个临时文件 (例 `data/test-images/<batch>/note.txt`) 备查.

### Step 6: 落 draft (dry_run 准备)

```bash
python -m growthpress draft new \
    --title "<标题>" \
    --body "$(cat data/test-images/<batch>/note.txt | sed -n '/^body:/,$ p' | tail -n +2)" \
    --topic "<原始 topic>" \
    --media data/test-images/<batch>/1.jpg \
    --media data/test-images/<batch>/2.jpg \
    --media data/test-images/<batch>/0.jpg \
    --state approved
```

- `--state approved` 跳过审批环节 (Claude 自己已经审过)
- 输出 `draft_id=<8-hex>`, 记住给下一步用

### Step 7: dry_run 验证 publisher 拼通 + 出预览截图

```bash
python -m growthpress draft publish-now <draft_id> --platforms xiaohongshu
# 不加 --real, dry_run 模式: publisher 跑一遍只出截图, 不动 state, 不污染 publications 表
```

- 大约 25-30 秒 (Playwright 起浏览器 + 上传图 + 截图)
- 输出 `screenshot=<path>`, 用 Read 读取截图
- draft.state 保持 `approved` 不变 (Step 6 落库时的状态)

### Step 8: Read 预览截图 + 检查

读截图, 检查:
- ✓ 标题字数 (右上角 N/20, 不能超)
- ✓ 正文排版 (emoji / 段落 / tag 是否完整)
- ✓ 图序 (封面应该是最主题的那张)
- ✓ "发布"按钮红圈定位正确 (publisher 红圈应该叠在"发布"按钮上, 不是别的)

发现问题 → 回 Step 5-6 改, 再 dry_run. 没问题 → 进 Step 9.

### Step 9: 发 APV 审批邮件 (离线确认, 推荐)

**默认走邮件确认**, 不用 AskUserQuestion 强求用户在终端在场.

```bash
python -m growthpress draft send-apv <draft_id>
```

- 调用 `growthpress.approver.send_approval` 走 SMTP 发 [APV-xxx] 邮件给 `NOTIFY_TO`
- 邮件含: 标题 / 摘要 / 正文预览 / 计划平台 / 失效时间 (24h) / 回信操作说明
- draft.state: `approved` → `pending_human` (CAS 锁住, daemon 后续按回信解锁)

**Claude 工作流到此结束**, 退出. 后续闭环由 daemon 自动处理:

| 用户回信 | mailbox 解析 | 后续 |
|---|---|---|
| `ok` (或 `ok xiaohongshu juejin`) | Approve | drafts.state → publishing → m4_pump 60s 内拉起真发 → PUB 邮件通知结果 |
| `改 <意见>` | Reject | drafts.state → revising, 用户下次主动找 Claude 改 |
| `drop` | Drop | drafts.state → archived |
| 24h 无回 | (pending_watch) | 提醒 / 转 pending_long |

**前置**: daemon 必须起着 (`uv run growthpress` 一个终端, 或 launchd 装好)
否则 APV 能发但回信没人接, draft 卡 pending_human.

### Step 9 备选: AskUserQuestion (终端在线场景)

用户明确说"我在线确认" / "不发邮件" / 当前在 Claude 终端不想等邮件:

```
AskUserQuestion:
  "下面内容可以真发么?"
  options:
    - "可以真发 (Recommended)"
    - "改标题/正文"
    - "换图"
    - "取消"
```

答"可以真发" → 直接 Step 10. 不走 send-apv (跳过 daemon, 同步真发).
答"取消" → 停, draft 留 state=approved (后续可重发).

### Step 10: 真发 (只在备选 Step 9 选了"在线 + 真发"时跑)

```bash
python -m growthpress draft publish-now <draft_id> --platforms xiaohongshu --real
```

- 加 `--real` 才是真发, state → publishing → published / human_queue
- 预期 30s 完成, success=True / url 包含 `published=true`
- 输出 `after` 截图 (Read 一下看是否回到空白上传页 — 这是发布成功标志)
- 让用户去自己平台账号最终确认笔记真的挂上去了

**正常走 Step 9 邮件路径时, 不跑这步** — 真发由 daemon m4_pump 在用户回 "ok" 后触发, 见 Step 9 表.

## CLI 速查

```bash
python -m growthpress platforms                    # 列已装 publisher
python -m growthpress draft list                   # 最近 20 draft
python -m growthpress draft list --state published # 只看已发
python -m growthpress draft show <id>              # 看详情 + state_log
python -m growthpress draft new ...                # 落库
python -m growthpress draft publish-now <id> ...   # 同步真发 (Claude 用这个)
python -m growthpress draft publish <id> ...       # 异步入队, daemon 拉 (后台用)
python -m growthpress --json ...                   # 任何子命令加 --json 出 JSON
```

## 失败/异常

| 现象 | 解决 |
|---|---|
| publisher 返 `success=False, error=invalid_input` 缺图 | 检查 media 文件实际存在, 路径绝对化 |
| publisher 返 `error=session_expired` | 用户 session 失效, 让 ta 跑 `xhs-publisher login` 重登 |
| dry_run 截图里"发布"按钮红圈位置不对 | xhs 改版了, publisher 需要更新选择器 (报告) |
| 标题/正文字数超限 | 改后重 dry_run, 不要硬发 (平台可能拒) |

## 记忆 / 案例

每次跑完一次完整工作流, 如果有可复用 pattern 或新踩坑, 提议 `/collect` 到项目 dev-cases.
