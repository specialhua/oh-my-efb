# coding=utf-8

import atexit
import logging
import threading
from typing import Any, Dict, Optional

from ruamel.yaml import YAML

from ehforwarderbot import Middleware, Message
from ehforwarderbot.types import ModuleID, InstanceID
from ehforwarderbot.utils import get_base_path, get_data_path

from .extract import PROMPT
from .flow import Todoer
from .store import Store

__version__ = "0.1.0"

DEFAULT_TIMEZONE = "Asia/Shanghai"


class TodoMiddleware(Middleware):
    """
    ``/todo`` in reply to a message in Telegram makes a to-do of it, with
    the help of a model or filled in by hand, in the private chat of the
    user with the bot; the bot reminds of it there when its time comes.
    ``/todos`` lists those not done. See :class:`.flow.Todoer`.

    The model is one of the AI middleware (``specialhua.ai``); the config of
    this middleware is optional and holds only its own settings.
    Messages pass through untouched.
    """

    middleware_id: ModuleID = ModuleID("specialhua.todo")
    middleware_name: str = "To-do Middleware"
    __version__: str = __version__

    def __init__(self, instance_id: Optional[InstanceID] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(self.middleware_id)
        # Not get_config_path(), which would make the folder of a config
        # that is optional.
        from ehforwarderbot import coordinator
        self.config_path = get_base_path() / "profiles" / coordinator.profile / self.middleware_id / "config.yaml"
        self.config_mtime: Optional[float] = None
        self.config_lock = threading.Lock()
        self.apply_config(self.load_config())

        self.store = Store(get_data_path(self.middleware_id) / "todos.db")
        self.todoer = Todoer(self, self.store)
        atexit.register(self.todoer.stop_event.set)
        self.todoer.register()

    # region Config

    def load_config(self) -> Dict[str, Any]:
        """The checked config; defaults when there is no file.

        Raises:
            ValueError: The config is invalid.
        """
        path = self.config_path
        data: Any = {}
        if path.exists():
            self.config_mtime = path.stat().st_mtime
            with path.open(encoding="utf-8") as f:
                data = YAML(typ="safe").load(f) or {}
        else:
            self.config_mtime = None
        if not isinstance(data, dict):
            raise ValueError("{}: the config must be a mapping.".format(self.middleware_id))
        for key in ("provider", "model", "prompt", "timezone"):
            value = data.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("{}: \"{}\" must be a non-empty string.".format(self.middleware_id, key))
        timeout = data.get("timeout", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("{}: \"timeout\" must be a positive number.".format(self.middleware_id))
        tz_name = data.get("timezone") or DEFAULT_TIMEZONE
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception as e:
            raise ValueError("{}: unknown \"timezone\" {}: {}".format(self.middleware_id, tz_name, e))
        return {"provider": data.get("provider"), "model": data.get("model"),
                "prompt": data.get("prompt") or PROMPT, "timeout": float(timeout),
                "tz": tz, "tz_name": tz_name}

    def apply_config(self, config: Dict[str, Any]):
        self.provider: Optional[str] = config["provider"]
        self.model: Optional[str] = config["model"]
        self.prompt: str = config["prompt"]
        self.timeout: float = config["timeout"]
        self.tz = config["tz"]
        self.tz_name: str = config["tz_name"]

    def reload_config(self):
        """Read the config again if it changed; keep the old one if the new
        one is invalid."""
        with self.config_lock:
            path = self.config_path
            mtime = path.stat().st_mtime if path.exists() else None
            if mtime == self.config_mtime:
                return
            try:
                self.apply_config(self.load_config())
                self.logger.info("Config reloaded")
            except Exception:
                self.config_mtime = mtime  # report it once
                self.logger.exception("Invalid config, keeping the previous one")

    def chat_client(self) -> Any:
        from efb_ai_middleware import load_settings
        return load_settings().chat_client(provider=self.provider, model=self.model, timeout=self.timeout)

    def ai_ready(self) -> bool:
        """Whether the AI middleware has a model for this middleware."""
        try:
            self.reload_config()
            self.chat_client()
            return True
        except Exception as e:
            self.logger.warning("No model for /todo: %s", e)
            return False

    # endregion

    def process_message(self, message: Message) -> Optional[Message]:
        return message
