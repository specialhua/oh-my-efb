# coding=utf-8
"""``efb-chatlog``: run the web service and look after it."""

import argparse
import logging
import sys
import time
from datetime import datetime


def main(argv=None):
    parser = argparse.ArgumentParser(prog="efb-chatlog", description="EFB 聊天记录网页")
    parser.add_argument("-p", "--profile", help="EFB 配置档名，默认 default 或 $EFB_PROFILE")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动网页服务（含后台同步）")
    sub.add_parser("sync", help="从 tgdata.db 同步一次，首次即导入全部历史")
    sub.add_parser("enroll", help="通过 Telegram bot 给管理员发送登记新设备的链接")
    sub.add_parser("devices", help="列出已登记的设备")
    revoke = sub.add_parser("revoke", help="移除设备并注销它的会话")
    revoke.add_argument("device", help="设备 ID（见 devices），或 all 移除全部")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .paths import Config, Paths
    paths = Paths(args.profile)

    if args.command == "sync":
        from .sync import Syncer
        config = Config(paths)
        started = time.time()

        def progress(done: int, total: int):
            print("\r已读取 {}/{} 行".format(done, total), end="", file=sys.stderr, flush=True)

        count = Syncer(paths, config.tgdata_timezone).run(progress)
        print("\n新增 {} 条，用时 {:.1f} 秒".format(count, time.time() - started))
        return

    config = Config(paths)
    from .auth import Auth
    auth = Auth(paths.auth, config.domain, config.origin, config.session_days)

    if args.command == "serve":
        import uvicorn
        from .web import create_app
        # Behind a reverse proxy on the same host: trust its X-Forwarded-For.
        uvicorn.run(create_app(paths, config), host=config.host, port=config.port, proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1", access_log=False)
    elif args.command == "enroll":
        from .web import send_enrollment
        send_enrollment(config, auth, "申请来源：服务器命令行")
        print("登记链接已发到 Telegram，{} 分钟内有效。".format(auth.enroll_seconds // 60))
    elif args.command == "devices":
        devices = auth.devices()
        if not devices:
            print("还没有登记任何设备。")
        for i in devices:
            print("{}  {}  登记于 {}  最近使用 {}  会话 {}{}".format(
                i["id"], i["name"], datetime.fromtimestamp(i["created"]).strftime("%Y-%m-%d %H:%M"),
                datetime.fromtimestamp(i["last_used"]).strftime("%Y-%m-%d %H:%M") if i["last_used"] else "-",
                i["sessions"], "  (iCloud 等同步)" if i["synced"] else ""))
    elif args.command == "revoke":
        count = auth.revoke(None if args.device == "all" else args.device)
        print("已移除 {} 个设备。".format(count))


if __name__ == "__main__":
    main()
