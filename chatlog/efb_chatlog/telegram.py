# coding=utf-8
"""The few Bot API calls of the web service. It never polls for updates:
the bots belong to EFB, which does."""

from typing import Iterator, Tuple

import requests

API = "https://api.telegram.org"
MAX_DOWNLOAD = 20 * 1024 * 1024
"""Largest file the Bot API lets a bot download."""


class TelegramError(Exception):
    pass


def call(token: str, method: str, **params) -> dict:
    try:
        response = requests.post("{}/bot{}/{}".format(API, token, method), json=params, timeout=20)
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise TelegramError("{} failed: {}".format(method, type(e).__name__)) from None
    if not data.get("ok"):
        raise TelegramError("{} failed: {}".format(method, data.get("description")))
    return data["result"]


def send_message(token: str, chat_id: int, text: str):
    call(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
         link_preview_options={"is_disabled": True})


def open_file(token: str, file_id: str) -> Tuple[int, Iterator[bytes]]:
    """Size and content of a file sent by the bot of ``token``."""
    info = call(token, "getFile", file_id=file_id)
    if not info.get("file_path"):
        raise TelegramError("file is too large for the Bot API")
    try:
        response = requests.get("{}/file/bot{}/{}".format(API, token, info["file_path"]), stream=True, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        raise TelegramError("download failed: {}".format(type(e).__name__)) from None
    return int(info.get("file_size") or 0), response.iter_content(64 * 1024)
