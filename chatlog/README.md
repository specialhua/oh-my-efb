# 聊天记录网页（`chatlog/`）

把 EFB 转发过的所有消息做成一个可以搜索的网页：微信那边收到的，加上你从 Telegram 发回微信的。登录不用密码，只有登记过的设备能打开，打开时用指纹或面容（通行密钥 / WebAuthn）验证一下。

## 组成

| 部分 | 跑在哪 | 做什么 |
|---|---|---|
| 网页服务 `efb-chatlog serve` | 单独的进程 | 网页、接口、通行密钥登录；后台每 30 秒从 `tgdata.db` 增量同步一次消息 |
| 中间件 `specialhua.chatlog` | EFB 进程内 | 只记下经过的消息的会话名和发言人名，不改消息、不发消息、不向微信拉列表 |

消息正文来自 Telegram 主端的 `tgdata.db`（`msglog` 表，两个方向的消息都在）。这个库不存发言人名字，所以：

- **历史消息**：会话名从 `slavechatinfo` 和微信从端的 `wxpy_puid.pkl` 补，按 2026-09 的备份算，约 94% 的消息能对上会话名，群里约 55% 能对上发言人，其余显示 8 位 ID。
- **启用中间件之后**：会话名、群昵称、备注名都实时记下来，新消息不再缺名字。

网页服务和 EFB 分开跑，网页随便改、随便重启，都不会碰到微信登录。

数据放在 `~/.ehforwarderbot/profiles/default/specialhua.chatlog/`：

- `archive.db`：消息副本和全文索引，删掉后会从 `tgdata.db` 重新导入
- `auth.db`：登记的设备和登录会话，删掉等于注销所有设备
- `config.yaml`：配置

114 万条消息的 `archive.db` 约 500MB，首次导入约半分钟。

## 部署

需要 SQLite 3.34 或更高版本（全文索引用的 trigram 分词）。Ubuntu 22.04、Debian 12 及以后的版本自带的就够。

1. 装进 EFB 的 venv：

   ```bash
   source ~/efb-venv/bin/activate
   pip install -e ~/oh-my-efb/chatlog
   ```

2. 写配置 `~/.ehforwarderbot/profiles/default/specialhua.chatlog/config.yaml`：

   ```yaml
   domain: chat.example.com     # 网页的域名。通行密钥绑定在这个域名上，改了以后所有设备都要重新登记
   # listen: 127.0.0.1:8765     # 网页服务监听的地址
   # session_days: 30           # 登录一次管多少天
   # tgdata_timezone: Asia/Shanghai  # tgdata.db 里时间的时区，默认取服务器的时区
   ```

   bot token 和管理员 ID 直接读 Telegram 主端的配置，不用再填。

3. 导入历史：

   ```bash
   efb-chatlog sync
   ```

4. 用 systemd 跑网页服务，写入 `/etc/systemd/system/efb-chatlog.service`：

   ```ini
   [Unit]
   Description=EFB chat log web
   After=network.target

   [Service]
   ExecStart=/root/efb-venv/bin/efb-chatlog serve
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   sudo systemctl enable --now efb-chatlog
   ```

5. 反向代理加 HTTPS。以 Caddy 为例，它会自动申请证书：

   ```
   chat.example.com {
       reverse_proxy 127.0.0.1:8765
   }
   ```

   通行密钥只能在 HTTPS 和配置的域名下使用，所以只能通过这个域名访问。

6. 启用中间件（可选，但不启用的话新消息也没有发言人名字）。在 `~/.ehforwarderbot/profiles/default/config.yaml` 里加一行：

   ```yaml
   middlewares:
     - specialhua.summary
     - specialhua.chatlog
   ```

   这一步要重启 EFB，微信有可能要求重新扫码，最好和其他更新攒在一起做。

## 登记设备

1. 在要登记的设备上打开网页，点“登记这台设备”。
2. bot 会在 Telegram 私聊里发给你一条登记链接，10 分钟内有效，只能用一次。消息里写着申请来源的 IP 和浏览器，如果不是你本人操作，忽略它即可。
3. 在**这台设备**上打开那条链接，起个名字，再用指纹或面容创建通行密钥。

在服务器上运行 `efb-chatlog enroll` 也会发出同样的链接。

以后登录只需要点“使用通行密钥登录”。

苹果设备的通行密钥会经 iCloud 钥匙串同步。Mac 上登记过以后，同一 Apple ID 的 iPhone 通常可以直接登录，设备列表里会标出“已同步”。

## 管理

- 网页右上角的“设备”里可以看到所有设备、移除设备、退出登录。移除一个设备后，用它登录的会话立刻失效。
- 命令行：

  ```bash
  efb-chatlog devices        # 列出设备
  efb-chatlog revoke <ID>    # 移除一个设备
  efb-chatlog revoke all     # 全部移除（手机丢了的时候用）
  efb-chatlog sync           # 手动同步一次
  ```

## 搜索

- 3 个字及以上的词走全文索引，毫秒级。1 到 2 个字的词逐条匹配，百万条约 0.3 秒。
- 多个词用空格隔开，要求同时出现。
- `from:名字` 按发言人筛选，匹配昵称、群昵称和备注；`from:我` 只看自己发的。
- 链接消息的标题和摘要也会被搜到。
- 消息区顶部可以选起止日期，只搜这段时间的消息（含结束那天）。不输入关键词、只选日期时，会按时间倒序列出这段时间的消息；在某个会话里选日期就只看这个会话。
- 搜索结果点“查看上下文”可以跳到原会话，前后各 30 条，上下滚动会继续加载。

## 安全

- 不用密码，也没有账号名。登录只认登记过的通行密钥，并且每次都要求设备本地验证（指纹、面容或设备密码）。
- 登记链接、会话令牌在服务器上都只存哈希。每个登录 challenge 只能用一次，5 分钟后过期。
- Cookie 设置为 `__Host-` 前缀，并带 `HttpOnly`、`Secure`、`SameSite=Strict`。所有写操作都会校验 `Origin`。
- 页面带严格的 CSP（脚本只认每次随机生成的 nonce），并禁止被嵌进其他页面。
- 媒体文件由服务器通过 Bot API 代取。只有图片、音频、视频按原类型返回，其余文件一律作为下载，并加上 sandbox。
- 登录和申请登记都有频率限制。每登记一台新设备，bot 都会通知你。
- 服务器上的 `archive.db` 和 `auth.db` 是明文。服务器本身的安全仍然要靠你自己。

## 会话分类

会话分成群聊、私聊、公众号、系统四类，依据按可靠程度依次是：

1. 中间件在消息经过时记下的类型。微信从端把公众号也当作私聊，靠它带的公众号标记分出来。
2. Telegram 主端 `slavechatinfo` 里记的类型，公众号同样从它保存的会话对象里分出来。
3. 两处都没有时，看发言人：出现过群成员（发言人和会话不是同一个 ID）就是群聊，只有对方本人就是私聊，只有自己发过的消息算未知，只在“全部”里出现。

会话和消息旁边另外标着它在 Telegram 里的位置：Bot 私聊、话题、绑定群。这只是投递到哪里，和群聊还是私聊无关。没用话题分流以前，没绑定的群和私聊都发在 Bot 私聊里。

## 限制

- 只有经过 EFB 转发的消息。Telegram 里不经过 EFB 的普通聊天不在这里。
- 图片、视频等要靠 Telegram 的 `file_id` 实时取回，超过 20MB 的文件 Bot API 下载不了。
- 动态贴纸（`.tgs`）只显示占位文字。
