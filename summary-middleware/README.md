# efb-summary-middleware

EFB 中间件：记录指定群聊的消息，每 X 天把最近 Y 天、最多 Z 条聊天记录交给 OpenAI 兼容接口总结，由 Telegram 主端的 bot 发到管理员（`admins` 的第一个）私聊。

模块 ID：`specialhua.summary`

## 启用

```bash
source ~/efb-venv/bin/activate
pip install -e ~/oh-my-efb/summary-middleware
```

在 `~/.ehforwarderbot/profiles/default/config.yaml` 里加上：

```yaml
middlewares:
  - specialhua.summary
```

## 配置

`~/.ehforwarderbot/profiles/default/specialhua.summary/config.yaml`，含 API key，权限设为 600：

```yaml
base_url: https://example.com/v1   # 写到 /v1 为止，会自动拼上 /chat/completions
api_key: sk-xxxx
model: gpt-4o-mini

# 默认值，对下面所有群生效
interval_days: 3       # X：每 X 天自动汇总一次
lookback_days: 3       # Y：只取最近 Y 天的消息
max_messages: 10000    # Z：最多取最近 Z 条

chats:                 # 群名、备注名或 chat ID，任一匹配即可
  - 某个群             # 只写名字：全部用默认值
  - name: 另一个群     # 写成映射：单独覆盖 X/Y/Z 中的任意几项
    interval_days: 5
    lookback_days: 5
    max_messages: 2000
  - name: 第三个群
    lookback_days: null   # 写 null 取消默认的天数限制，只按条数取

# 以下可选
# prompt: |            # 覆盖内置的系统提示词
#   ...
# max_chars: 40000     # 单次请求的聊天记录字符上限，超出则分段总结再合并
# timeout: 180         # 单次请求超时（秒）
```

每个群必须有 `interval_days`，并且 `lookback_days` 和 `max_messages` 至少有一个（来自默认值或自己的设置都算）。没有配置文件或这些条件不满足时，EFB 会在启动时直接报错退出。改配置后需要重启。

## 行为

- **周期**：每个群各自计时，从首次启动起每满 `interval_days` 天汇总一次。上次汇总时间按群存在数据库里，重启不会打乱周期；停机期间错过的会在启动后约一分钟补发。
- **窗口**：每次取“最近 `lookback_days` 天之内”的消息，其中最多取最新的 `max_messages` 条；两项只设一项时就只按那一项取。实际记录不够时有多少总结多少。不记录哪些消息已经总结过，所以窗口比周期长时相邻两次会重叠，比周期短时中间会有消息不被总结。
- **手动触发**：Telegram 里发 `/extra` 找到“立即汇总”对应的命令（形如 `/1_summary`）。不带参数汇总所有群，`/1_summary 群名` 只汇总一个（群名按配置里写的填）。窗口与定时汇总相同，不影响定时周期。
- **输出**：一条 Telegram 消息；超过长度上限时消息只放前半部分，完整版作为 `.html` 附件跟在后面。
- **失败**：单次请求自动重试 3 次；定时汇总失败后每小时再试一次，只在第一次失败时通知。
- **非文本消息**：记为 `[图片]`、`[语音]` 等占位；链接保留标题、描述和地址。
- **数据**：消息存在同目录的 `messages.db`。设了 `lookback_days` 的群保留 `lookback_days + 7` 天；只设 `max_messages` 的群保留最新的 `max_messages` 条。只有启用之后收到的消息才会被记录。

聊天记录会原文发送给 `base_url` 指向的服务。
