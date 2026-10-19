# oh-my-efb

基于网页版微信 EH Forwarder Bot（EFB）的自用方案，把微信消息转发到 Telegram，也可以在 Telegram 里收发微信。在上游组件之外，本仓库加了一个用大模型定时总结群聊的中间件，以及一个可以搜索全部聊天记录的网页。

> **使用前提：你的微信号还能登录网页版微信。**
> 大约 2017 年之前注册的微信号才保留这个权限。扫码时如果提示“为了你的帐号安全，此微信号不能登录网页微信”，这套方案就用不了，本仓库没有别的登录方式。

## 本仓库的改动

### 群聊总结（`summary-middleware/`）

自己写的 EFB 中间件。它记录指定微信群的文字消息，交给大模型整理出重点和话题，列出每个人说了什么，以及待办，再由 Telegram bot 发出来。

- **定时汇总**：每个群可以单独设置每几天汇总一次、汇总最近几天、最多多少条。
- **随时汇总**：在群对应的 Telegram 话题里发命令，结果回复在同一个话题。命令会被拦下，不会发到微信群。
  - `/summary`：按该群配置的范围
  - `/summary 7`：最近 7 天
  - `/summary 500条`：最近 500 条
- **发到哪**：群绑定了 Telegram 群组就发到群组，走话题分流就发到它的话题，都没有则发到和 bot 的私聊。
- **接口**：任何兼容 OpenAI `/chat/completions` 的服务都能用，比如 DeepSeek 或各类中转站。地址和 key 写在下面的 AI 中间件里。
- **改配置不用重启**：配置文件保存后一分钟内自动生效，换模型、改提示词、增减群都一样。
- **数据**：消息只存文字，放在本机的 SQLite 里，按设定的天数自动清理。图片、语音等只记为 `[图片]`、`[语音]` 占位。

最小配置（`~/.ehforwarderbot/profiles/default/specialhua.summary/config.yaml`，接口地址和 key 见下一节）：

```yaml
interval_days: 3       # 每 3 天自动汇总一次
lookback_days: 3       # 汇总最近 3 天
max_messages: 10000    # 最多 10000 条

chats:
  - a1b2c3d4           # 群的 8 位 ID，在该群的话题里发 /info 可以看到；也可以直接写群名
```

总结时，群聊记录会原文发送给你配置的接口服务方。全部配置项和行为细节见 [summary-middleware/README.md](summary-middleware/README.md)。

### AI 中间件（`ai-middleware/`）

集中保存大模型接口和 Jev 的地址、API key、默认模型，群聊总结、`/ask` 以及以后用到 AI 的功能都从这里取，各自只配提示词、模型等自己的设置。可以配多个服务，用 `provider: 名字` 选。

最小配置（`~/.ehforwarderbot/profiles/default/specialhua.ai/config.yaml`）：

```yaml
base_url: https://api.deepseek.com
api_key: sk-xxxx
model: deepseek-chat
```

以前写在群聊总结配置里的 `base_url`、`api_key` 和 `jev.api_key` 仍然有效，不迁移也能照常运行。详见 [ai-middleware/README.md](ai-middleware/README.md)。

### 待办提醒（`todo-middleware/`）

回复一条消息发 `/todo`，把它设为待办，自己发的消息和转发给 bot 的消息也可以。到时间后，bot 在私聊里提醒你，附带原文，还能跳回原消息。

- **AI 提取**：模型按消息发送的时间推算“下周一”这类日期，判断开始时间、地点和备注建议；你在确认卡片上多选提醒时间（前 1 天到前 10 分钟，也可以自定义）。
- **手动填写**：在和 bot 的私聊里逐项填写。填写期间，私聊里的文字不会转到微信。
- **管理**：`/todos` 列出未完成的待办；提醒消息上可以点完成，或者推迟 10 分钟、1 小时、到明早 8 点。

详见 [todo-middleware/README.md](todo-middleware/README.md)。

### 聊天记录网页（`chatlog/`）

把 EFB 转发过的消息做成可搜索的网页，按会话浏览，也能全文搜索，按人、按类型筛选。不用密码，登记过的设备用指纹或面容登录（通行密钥），新设备要靠 bot 发到 Telegram 的一次性链接来登记。

- 消息来自 Telegram 主端的 `tgdata.db`，能导入全部历史。网页服务单独跑，后台增量同步。
- 配套的 `specialhua.chatlog` 中间件只记会话名和发言人名，补上 `tgdata.db` 缺的名字。

部署和使用见 [chatlog/README.md](chatlog/README.md)。

### `/extra` 支持中间件的命令

`telegram-master/` 里原本 `/extra` 只能列出和调用从端的附加命令，中间件的命令既不显示也调不了。现在两者都可以，总结中间件的“立即汇总”（私聊里发 `/extra` 可以找到）靠的就是这个改动。

## 组成

三个上游组件用 `git subtree` 并入，可以直接改，也能继续拉上游更新；中间件是本仓库自己的代码。

| 目录 | 内容 | 版本 | 上游 |
|---|---|---|---|
| `core/` | `ehforwarderbot` 框架 | `v2.1.1` | [ehForwarderBot/ehForwarderBot](https://github.com/ehForwarderBot/ehForwarderBot) |
| `telegram-master/` | Telegram 主端（Ovler-Young fork） | `dev-topic-custom-emoji` @ `24c1e44`，另有上面的改动 | [Ovler-Young/efb-telegram-master](https://github.com/Ovler-Young/efb-telegram-master) |
| `wechat-slave/` | 微信从端 | `v2.0.7` | [ehForwarderBot/efb-wechat-slave](https://github.com/ehForwarderBot/efb-wechat-slave) |
| `ai-middleware/` | AI 公共配置中间件（`specialhua.ai`） | 本仓库 | 无 |
| `summary-middleware/` | 群聊总结中间件（`specialhua.summary`） | 本仓库 | 无 |
| `todo-middleware/` | 待办提醒中间件（`specialhua.todo`） | 本仓库 | 无 |
| `chatlog/` | 聊天记录网页和中间件（`specialhua.chatlog`） | 本仓库 | 无 |

版本是有意固定的。

- **`core/` 停在 `v2.1.1`**：上游 master 给 `Channel` 加了抽象方法 `get_chat_member_picture`，微信从端没有实现，装 master 会在启动时直接报错。
- **`telegram-master/` 用 fork 的 `dev-topic-custom-emoji` 分支**：官方版没有话题分流（一个微信会话对应一个话题）和辅助 bot，这个分支还能把会话头像同步成话题的自定义表情。
- **`wechat-slave/` 停在 `v2.0.7`**：这是纯网页协议的版本，不含之后加入的 UOS 伪装登录补丁，升级时不要拉上游 master。

## 部署

需要 Python 3.10 或更高版本（Telegram 主端的要求）。在目标机器的 venv 里从本仓库安装：

```bash
pip install ./core
pip install ./wechat-slave
pip install ./telegram-master
pip install ./ai-middleware        # 群聊总结、待办的 AI 提取需要
pip install ./summary-middleware   # 可选
pip install ./todo-middleware      # 可选
pip install ./chatlog              # 可选，部署见 chatlog/README.md
```

EFB 本身的配置（bot token、管理员 ID、话题群等）按各组件自带的文档填写，见 [telegram-master/README.rst](telegram-master/README.rst) 和 [wechat-slave/README.rst](wechat-slave/README.rst)。

启用群聊总结，要在 `~/.ehforwarderbot/profiles/default/config.yaml` 里加上中间件，并写好上面两节的配置文件：

```yaml
middlewares:
  - specialhua.ai
  - specialhua.summary
  - specialhua.todo      # 可选：待办提醒
```

首次登录要在前台运行 `ehforwarderbot` 扫码。日常更新、备份和恢复的步骤记在本地的 `local/docs/EFB_DAILY_OPS.md`。

## 上游更新

远端已配好：

```bash
git remote -v
# upstream-core  https://github.com/ehForwarderBot/ehForwarderBot.git
# upstream-etm   https://github.com/Ovler-Young/efb-telegram-master.git
# upstream-ews   https://github.com/ehForwarderBot/efb-wechat-slave.git
```

拉某个组件的上游更新，以 Telegram 主端为例：

```bash
git subtree pull --prefix=telegram-master upstream-etm dev-topic-custom-emoji --squash
```

`telegram-master/efb_telegram_master/commands.py` 有本仓库的改动，拉取时这个文件可能需要手动合并。

微信从端只拉具体的 tag，不拉 master：

```bash
git subtree pull --prefix=wechat-slave upstream-ews v2.0.8 --squash   # 若将来有新 tag
```

## 不在仓库内的本地目录

| 目录 | 内容 | 为什么不提交 |
|---|---|---|
| `local/backups/` | `~/.ehforwarderbot` 数据目录备份包 | 含 bot token 和微信登录态 |
| `local/docs/` | 日常更新与运维手册 | 自用运维文档 |
| `local/upstream-clones/` | 并入前的三个独立 clone | 已被 subtree 取代，可删 |
| `reference/efb-telegram-master-official/` | 官方 Telegram 主端 2.3.1 | 只用来和 fork 做 diff |
