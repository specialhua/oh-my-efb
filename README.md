# oh-my-efb

EH Forwarder Bot（微信 ↔ Telegram）的自用部署与定制开发工作区。三个组件以 `git subtree` 并入本仓库，可以直接改，也能继续拉上游更新。

## 组件与固定版本

| 目录 | 组件 | 固定在 | 上游 |
|---|---|---|---|
| `core/` | `ehforwarderbot` | `v2.1.1` | [ehForwarderBot/ehForwarderBot](https://github.com/ehForwarderBot/ehForwarderBot) |
| `telegram-master/` | `efb-telegram-master`（Ovler-Young fork） | `dev-topic-custom-emoji` @ `24c1e44` | [Ovler-Young/efb-telegram-master](https://github.com/Ovler-Young/efb-telegram-master) |
| `wechat-slave/` | `efb-wechat-slave` | `v2.0.7` | [ehForwarderBot/efb-wechat-slave](https://github.com/ehForwarderBot/efb-wechat-slave) |

另有自己写的中间件 `summary-middleware/`（`specialhua.summary`）：定时用 LLM 总结指定群聊并通过 bot 发送，配置见 [summary-middleware/README.md](summary-middleware/README.md)。它不属于 subtree，没有上游。

版本不是随便定的，三条都踩过坑：

- **core 必须停在 `v2.1.1`**。上游 master 给 `Channel` 加了抽象方法 `get_chat_member_picture`，而微信从端没实现它，装 master 会直接崩在实例化那一步。
- **微信从端必须停在 `v2.0.7`**。之后的 commit `22816de` 加了 UOS 伪装登录补丁，`client-version` 和 `extspam` 是写死的固定特征串，所有用它的客户端特征一致，极易被微信风控识别封号。`v2.0.7` 走纯网页协议，实测稳定。
- **Telegram 从端用 fork 的 `dev-topic-custom-emoji` 分支**。官方版没有话题分流（`topicassoc` 表）和辅助 bot 字段（`sender_bot_id`）；这个分支在 fork 的 `dev` 基础上还提供话题头像同步为自定义表情。

## 上游更新

远端已配好：

```bash
git remote -v
# upstream-core  https://github.com/ehForwarderBot/ehForwarderBot.git
# upstream-etm   https://github.com/Ovler-Young/efb-telegram-master.git
# upstream-ews   https://github.com/ehForwarderBot/efb-wechat-slave.git
```

拉某个组件的上游更新（以 Telegram 从端为例）：

```bash
git subtree pull --prefix=telegram-master upstream-etm dev-topic-custom-emoji --squash
```

`telegram-master/` 目前固定在 `24c1e44`，上游分支已有 8 个更新提交，按需拉取。

微信从端不要拉上游 master，否则会把 UOS 补丁带回来。要升级只能拉具体的 tag：

```bash
git subtree pull --prefix=wechat-slave upstream-ews v2.0.8 --squash   # 若将来有新 tag
```

## 部署

从本仓库安装（在目标机器的 venv 内）：

```bash
pip install ./core
pip install ./wechat-slave
pip install ./telegram-master
pip install ./summary-middleware   # 可选，群聊总结中间件
```

安装后必须验证微信从端没有 UOS 痕迹，三项都应为 `False`：

```bash
python - <<'EOF'
import pathlib, efb_wechat_slave as w
d = pathlib.Path(w.__file__).parent / "vendor" / "itchat"
login = (d / "components" / "login.py").read_text(errors="replace")
cfg   = (d / "config.py").read_text(errors="replace")
print("extspam    :", "extspam" in login)
print("mod=desktop:", "mod=desktop" in login)
print("UOS config :", "UOS_PATCH" in cfg)
EOF
```

备份、恢复、systemd 托管的完整步骤见本地的 `local/docs/EFB_VPS_REINSTALL_RUNBOOK.md`。

## 不在仓库内的本地目录

| 目录 | 内容 | 为什么不提交 |
|---|---|---|
| `local/backups/` | `~/.ehforwarderbot` 数据目录备份包 | 含 bot token 和微信登录态 |
| `local/docs/` | VPS 重装恢复手册 | 自用运维文档 |
| `local/upstream-clones/` | 并入前的三个独立 clone | 已被 subtree 取代，可删 |
| `reference/efb-telegram-master-official/` | 官方 Telegram 从端 2.3.1 | 只用来和 fork 做 diff |
