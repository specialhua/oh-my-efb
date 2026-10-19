# efb-todo-middleware

EFB 中间件：在 Telegram 里回复一条消息发 `/todo`，把它设为待办，到时间由 bot 在私聊里提醒你，提醒里附带原文，还能跳回原消息。可以让大模型自动提取时间和内容，也可以自己手动填写。

模块 ID：`specialhua.todo`

## 启用

```bash
source ~/efb-venv/bin/activate
pip install -e ~/oh-my-efb/ai-middleware     # 用 AI 提取时需要，见下文
pip install -e ~/oh-my-efb/todo-middleware
```

在 `~/.ehforwarderbot/profiles/default/config.yaml` 里加上：

```yaml
middlewares:
  - specialhua.ai
  - specialhua.todo
```

## 配置

不需要配置文件也能用。模型的地址和 key 来自 [AI 中间件](../ai-middleware/README.md)，这里只写自己的设置。需要改时，新建 `~/.ehforwarderbot/profiles/default/specialhua.todo/config.yaml`：

```yaml
# provider: vision        # 用 AI 中间件里的哪个服务，默认是它的默认服务
# model: deepseek-chat    # 换一个模型，默认用该服务的 model
# timeout: 60             # 单次请求超时（秒）
# timezone: Asia/Shanghai # 时间按哪个时区理解和显示
# prompt: |               # 覆盖内置的提取提示词，见 efb_todo_middleware/extract.py
#   ...
```

改了不用重启，下次用 AI 提取时自动重新读取。

## 用法

只有 ETM 的管理员能用。命令本身不会发到微信。

- **`/todo`**：在任何会话里回复一条消息发送，包括微信群或私聊对应的话题、绑定的群组、和 bot 的私聊。被回复的可以是别人的消息、你自己发的消息，或者转发给 bot 的消息。
- **`/todo 文字`**：回复消息时，后面的文字会作为补充说明交给 AI，比如 `/todo 提前一天提醒我`；不回复消息时，就把这段文字本身设为待办，例如 `/todo 明天下午三点开会`。
- **`/todos`**：列出未完成的待办，可以标为完成或取消。

发出 `/todo` 后，原来的会话里只会回一句“已在和 bot 的私聊中继续”，之后的步骤都在私聊里进行。

### 第一步：选择方式

私聊里会显示来源（群名或私聊名、发送人）、消息时间和原文，并问是否适合让 AI 自动提取。

### AI 提取

模型拿到原文、当前时间和这条消息的发送时间，判断事项内容、开始时间、地点和备注建议。

- “明天”“下周一”这类说法，按消息发送的时间推算。
- 没写具体时间的，由模型按事项性质判断，比如交材料按上班时间。判断不出时间时按当天 08:00 计；判断不出日期时，会请你改为手动填写。

确认卡片上会显示提取结果和模型的推算理由。提醒时间可以多选：

- 前 1 天、前 6 小时、前 3 小时、前 1 小时、前 10 分钟（已经过去的不显示）
- ➕ 自定义时间：自己输入一个时间，可以加多个

卡片上还可以改事项时间和内容。至少选一个提醒时间才能保存。

### 手动填写

bot 一次问一项，回复格式不对或时间已经过去就重新问：

1. 提醒内容（必填）
2. 事项开始时间（选填），格式为 `yyyy-mm-dd HH:mm:ss`，秒可以省略，例如 `2026-10-20 15:00`
3. 地点（选填）
4. 备注（选填）

选填项可以点“跳过”，或者发 `-`。填完后显示和 AI 模式一样的确认卡片。如果填了开始时间，会预先选上当天 08:00 作为提醒时间，你可以清空后重新选。

**填写期间你在私聊里发的文字不会转到微信**。因为 ETM 平时会把私聊里的文字转给最近联系的微信会话，所以 bot 等你输入时会先拦下这些文字。以下情况会恢复正常转发：填完显示确认卡片、点“结束填写”、发 `/cancel`，或者 10 分钟没有操作。超时之后才发出的第一条消息仍会被拦下，同时提示已恢复。

### 提醒

到了提醒时间，bot 在私聊里发消息，内容包括：提醒内容、事项时间、地点、备注、来源和原文。

- 原消息在超级群或话题里时，附带“打开原消息”的链接，并尽量直接引用那条消息；原消息在和 bot 的私聊里时，直接引用它。
- 下面的按钮：✅ 完成（剩下的提醒一起取消）、10 分钟后、1 小时后、明早 8 点。
- EFB 停机期间错过的提醒，启动后约半分钟内补发，并注明晚了多久。

## 数据

待办存在本机的 SQLite 里（`~/.ehforwarderbot/profiles/default/specialhua.todo/todos.db`），包括原文快照，所以原消息删除后提醒里仍有原文。时间统一按 UTC 时间戳保存，显示时换算成配置的时区。

还没保存的待办只放在内存里，EFB 重启或一天后失效，重新发 `/todo` 即可。

用 AI 提取时，原文、来源名称和你写的补充说明会发送给 AI 中间件里配置的服务。

## 测试

```bash
pip install -e ~/oh-my-efb/core -e ~/oh-my-efb/ai-middleware -e ~/oh-my-efb/todo-middleware pytest
python -m pytest ~/oh-my-efb/todo-middleware/tests
```
