# efb-ai-middleware

EFB 中间件：集中保存大模型接口和 Jev 的公共配置（地址、API key、默认模型），给其他中间件复用。用到 AI 的功能（群聊总结 `/summary`、`/ask`，以及以后的新功能）只需要在自己的配置里写提示词、模型这类自己的设置，不用再各写一遍地址和 key。

模块 ID：`specialhua.ai`

## 启用

```bash
source ~/efb-venv/bin/activate
pip install -e ~/oh-my-efb/ai-middleware
```

在 `~/.ehforwarderbot/profiles/default/config.yaml` 里加上，放在用到它的中间件前面：

```yaml
middlewares:
  - specialhua.ai
  - specialhua.summary
```

不加也能用：其他中间件会直接读这个模块的配置文件。加上的好处是配置有错时 EFB 启动就报错，日志里也会列出读到了哪些服务。

## 配置

`~/.ehforwarderbot/profiles/default/specialhua.ai/config.yaml`，含 API key，权限设为 600：

```yaml
# 默认服务：任何兼容 OpenAI /chat/completions 的接口
base_url: https://api.deepseek.com   # 写到 /v1 为止，会自动拼上 /chat/completions
api_key: sk-xxxx
model: deepseek-chat                 # 默认模型，用到的地方没写 model 时用它；可省略

# providers:                         # 可选：再加几个服务，用到的地方写 provider: 名字 来选
#   vision:
#     base_url: https://example.com/v1
#     api_key: sk-yyyy
#     model: gpt-4o-mini

# jev:                               # 可选：TypeSafe 的 Jev，群聊总结用它给消息分类
#   api_key: ts-xxxx
#   model: jev-latest                # 默认 jev-latest
#   # base_url: https://api.typesafe.ai/v1
#   # timeout: 60
```

- 顶层的 `base_url`、`api_key`、`model` 是名为 `default` 的服务。只写了 `providers` 而且只有一个时，它就是默认服务。
- 改了这个文件不用重启：群聊总结每分钟检查一次，这个文件或它自己的配置变了都会重新读取。

## 给其他中间件用

```python
from efb_ai_middleware import load_settings

ai = load_settings()                     # 没有配置文件时返回空设置，不报错
client = ai.chat_client(provider="vision", model="gpt-4o-mini", timeout=60)
text = client.complete("系统提示词", "用户消息")
jev = ai.jev_client()                    # 没配 Jev 时为 None
```

`chat_client` 默认用默认服务和它的 `model`；`cls=` 可以换成 `ChatClient` 的子类。请求失败自动重试 3 次，整个响应必须在 `timeout` 秒内收完。
