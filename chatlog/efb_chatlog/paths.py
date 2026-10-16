# coding=utf-8
"""Locations and settings shared by the web service and the CLI, which run
outside EFB and so cannot ask its coordinator."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML

MODULE_ID = "specialhua.chatlog"
TELEGRAM_ID = "blueset.telegram"
WECHAT_ID = "blueset.wechat"


class Paths:
    def __init__(self, profile: Optional[str] = None):
        base = Path(os.environ.get("EFB_DATA_PATH") or "~/.ehforwarderbot").expanduser()
        self.profile = base / "profiles" / (profile or os.environ.get("EFB_PROFILE") or "default")
        self.data = self.profile / MODULE_ID
        self.data.mkdir(parents=True, exist_ok=True)
        self.archive = self.data / "archive.db"
        self.auth = self.data / "auth.db"
        self.config = self.data / "config.yaml"
        self.tgdata = self.profile / TELEGRAM_ID / "tgdata.db"
        self.telegram_config = self.profile / TELEGRAM_ID / "config.yaml"
        self.puid_map = self.profile / WECHAT_ID / "wxpy_puid.pkl"


def load_yaml(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return YAML(typ="safe").load(f) or {}


class Config:
    """``config.yaml`` of this module, plus the bot settings of the
    Telegram master channel."""

    def __init__(self, paths: Paths):
        if not paths.config.exists():
            raise SystemExit("缺少配置文件：{}（至少要写 domain）".format(paths.config))
        data = load_yaml(paths.config)
        domain = data.get("domain")
        if not isinstance(domain, str) or not domain:
            raise SystemExit("{}: domain 必须是网页的域名，例如 chat.example.com".format(paths.config))
        self.domain: str = domain
        """Also the relying party ID of passkeys: changing it voids every
        registered passkey."""
        self.origin: str = data.get("origin") or "https://{}".format(domain)
        listen = str(data.get("listen") or "127.0.0.1:8765")
        host, _, port = listen.rpartition(":")
        self.host: str = host or "127.0.0.1"
        self.port: int = int(port)
        self.session_days: float = float(data.get("session_days", 30))
        self.sync_seconds: float = float(data.get("sync_seconds", 30))
        self.tgdata_timezone: Optional[str] = data.get("tgdata_timezone")
        """Time zone of the naive times in ``tgdata.db``; the server's own by default."""

        telegram = load_yaml(paths.telegram_config)
        self.bot_token: str = telegram["token"]
        self.admin: int = int(telegram["admins"][0])
        self.bot_tokens: Dict[str, str] = {}
        """Token of each bot of the master channel by bot ID: media in
        Telegram can only be fetched by the bot that sent it."""
        tokens: List[str] = [self.bot_token]
        tokens += [i["token"] for i in telegram.get("auxiliary_bots") or []
                   if isinstance(i, dict) and isinstance(i.get("token"), str)]
        for token in tokens:
            self.bot_tokens[token.split(":", 1)[0]] = token
