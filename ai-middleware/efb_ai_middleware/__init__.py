# coding=utf-8
"""Shared settings of the AI services used by other middlewares.

The endpoints and API keys are written once, in the config of this module
(``specialhua.ai``); a middleware that uses them only sets what is its own,
such as prompts and the model::

    from efb_ai_middleware import load_settings

    ai = load_settings()
    client = ai.chat_client(model="gpt-4o-mini", timeout=60)
    jev = ai.jev_client()  # None unless Jev is configured

A missing config file is not an error here, since a middleware may still
carry its own endpoint from before this module existed.
"""

import logging
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

from ruamel.yaml import YAML

from ehforwarderbot import Middleware, Message, coordinator
from ehforwarderbot.types import ModuleID, InstanceID
from ehforwarderbot.utils import get_base_path

from .chat import ChatClient
from .jev import DEFAULT_BASE_URL as JEV_BASE_URL, Jev, JevError

__version__ = "0.1.0"
__all__ = ["AIMiddleware", "AISettings", "ChatClient", "Jev", "JevError", "Provider", "JevSettings",
           "MODULE_ID", "DEFAULT_PROVIDER", "config_path", "load_settings"]

MODULE_ID = ModuleID("specialhua.ai")
DEFAULT_PROVIDER = "default"
"""Name of the provider written at the top level of the config."""


class Provider(NamedTuple):
    """An OpenAI-compatible chat completions service."""
    name: str
    base_url: str
    api_key: str
    model: Optional[str]
    """Model used when the caller sets none."""


class JevSettings(NamedTuple):
    api_key: str
    model: str
    base_url: str
    timeout: float


class AISettings:
    """The parsed config of :data:`MODULE_ID`."""

    def __init__(self, providers: Optional[Dict[str, Provider]] = None,
                 jev: Optional[JevSettings] = None, path: Optional[Path] = None):
        self.providers: Dict[str, Provider] = providers or {}
        self.jev = jev
        self.path = path

    def provider(self, name: Optional[str] = None) -> Provider:
        """The provider called ``name``, by default :data:`DEFAULT_PROVIDER`
        or else the only one there is."""
        if name is None and DEFAULT_PROVIDER not in self.providers and len(self.providers) == 1:
            return next(iter(self.providers.values()))
        name = name or DEFAULT_PROVIDER
        if name not in self.providers:
            if not self.providers:
                raise ValueError("{}: no AI service is configured in {}.".format(MODULE_ID, self.path))
            raise ValueError("{}: no provider \"{}\", configured: {}.".format(
                MODULE_ID, name, ", ".join(self.providers)))
        return self.providers[name]

    def chat_client(self, provider: Optional[str] = None, model: Optional[str] = None,
                    timeout: float = 180, cls: type = ChatClient, **kwargs: Any) -> Any:
        """A client of ``provider`` with ``model``, by default the model of
        the provider. ``cls`` is :class:`ChatClient` or a subclass of it,
        given ``kwargs`` beyond the connection."""
        p = self.provider(provider)
        model = model or p.model
        if not model:
            raise ValueError("{}: provider \"{}\" has no \"model\", and none is set where it is used.".format(
                MODULE_ID, p.name))
        return cls(base_url=p.base_url, api_key=p.api_key, model=model, timeout=timeout, **kwargs)

    def jev_client(self, model: Optional[str] = None, timeout: Optional[float] = None) -> Optional[Jev]:
        """A Jev client, ``None`` when Jev is not configured."""
        if self.jev is None:
            return None
        return Jev(api_key=self.jev.api_key, model=model or self.jev.model, base_url=self.jev.base_url,
                   timeout=self.jev.timeout if timeout is None else timeout)


def config_path(profile: Optional[str] = None) -> Path:
    """Path of the config file, without creating its folder as
    :func:`ehforwarderbot.utils.get_config_path` would."""
    return get_base_path() / "profiles" / (profile or coordinator.profile) / MODULE_ID / "config.yaml"


def load_settings(path: Optional[Path] = None) -> AISettings:
    """Read and check the config; empty settings when there is no file.

    Raises:
        ValueError: The config is invalid.
    """
    path = path or config_path()
    if not path.exists():
        return AISettings(path=path)
    with path.open(encoding="utf-8") as f:
        data = YAML(typ="safe").load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("{}: the config must be a mapping.".format(MODULE_ID))

    def string(entry: Dict[str, Any], key: str, where: str, required: bool) -> Optional[str]:
        value = entry.get(key)
        if (required or value is not None) and (not isinstance(value, str) or not value):
            raise ValueError("{}: \"{}{}\" must be a non-empty string.".format(MODULE_ID, where, key))
        return value

    def provider(name: str, entry: Any, where: str) -> Provider:
        if not isinstance(entry, dict):
            raise ValueError("{}: \"{}\" must be a mapping.".format(MODULE_ID, where.rstrip(".")))
        return Provider(name, string(entry, "base_url", where, True),  # type: ignore[arg-type]
                        string(entry, "api_key", where, True),  # type: ignore[arg-type]
                        string(entry, "model", where, False))

    providers: Dict[str, Provider] = {}
    if any(data.get(key) is not None for key in ("base_url", "api_key", "model")):
        providers[DEFAULT_PROVIDER] = provider(DEFAULT_PROVIDER, data, "")
    extra = data.get("providers") or {}
    if not isinstance(extra, dict):
        raise ValueError("{}: \"providers\" must be a mapping of names to services.".format(MODULE_ID))
    for name, entry in extra.items():
        name = str(name)
        if name in providers:
            raise ValueError("{}: provider \"{}\" is also written at the top level.".format(MODULE_ID, name))
        providers[name] = provider(name, entry, "providers.{}.".format(name))

    jev = None
    entry = data.get("jev")
    if entry is not None:
        if not isinstance(entry, dict):
            raise ValueError("{}: \"jev\" must be a mapping.".format(MODULE_ID))
        timeout = entry.get("timeout", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("{}: \"jev.timeout\" must be a positive number.".format(MODULE_ID))
        jev = JevSettings(string(entry, "api_key", "jev.", True),  # type: ignore[arg-type]
                          string(entry, "model", "jev.", False) or "jev-latest",
                          string(entry, "base_url", "jev.", False) or JEV_BASE_URL,
                          float(timeout))
    return AISettings(providers, jev, path)


class AIMiddleware(Middleware):
    """
    Holds the settings of the AI services for other middlewares, see
    :func:`load_settings`. Messages pass through untouched.

    Enabling it is optional, as the settings are read from its config file
    either way; enabled, an invalid config stops EFB at startup.
    """

    middleware_id: ModuleID = MODULE_ID
    middleware_name: str = "AI Settings Middleware"
    __version__: str = __version__

    def __init__(self, instance_id: Optional[InstanceID] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(self.middleware_id)
        settings = load_settings()
        self.logger.info("AI providers: %s; Jev: %s", ", ".join(settings.providers) or "none",
                         "on" if settings.jev else "off")

    def process_message(self, message: Message) -> Optional[Message]:
        return message
