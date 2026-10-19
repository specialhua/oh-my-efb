# coding=utf-8

import json
import logging
import time
from typing import Any, Dict, Tuple

import requests

from .chat import post

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"


class JevError(RuntimeError):
    """A request that Jev rejected, or that kept failing after retries."""


class Jev:
    """Client of the TypeSafe System One endpoint, which answers typed
    questions (noul / choice / score) about a state with Jev.

    See https://docs.typesafe.ai/api.
    """

    attempts = 4
    retry_wait = 2
    """Seconds before the first retry; doubled on each further one."""
    retry_statuses = {408, 429, 500, 502, 503, 504, 529}

    def __init__(self, api_key: str, model: str = "jev-latest",
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 60):
        self.url = base_url.rstrip("/") + "/systemone"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

    def ask(self, state: Any, questions: Dict[str, dict]) -> Tuple[Dict[str, dict], Dict[str, int]]:
        """Evaluate ``questions`` (keyed by an ID of the caller's choosing)
        against ``state``. Returns the answers under the same keys, and the
        token usage of the request."""
        payload = {"state": state, "model": self.model, "questions": questions}
        error: Exception = JevError("no attempt made")
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(self.retry_wait * 2 ** (attempt - 1))
            try:
                status, body = self.post(payload)
            except (requests.RequestException, TimeoutError) as e:
                error = e
                self.logger.warning("Jev attempt %s failed: %s", attempt + 1, e)
                continue
            if status < 400:
                data = json.loads(body)
                return data["answers"], data.get("usage") or {}
            error = JevError("HTTP {}: {}".format(status, body[:300].decode("utf-8", "replace")))
            if status not in self.retry_statuses:
                raise error
            self.logger.warning("Jev attempt %s failed: %s", attempt + 1, error)
        raise error

    def post(self, payload: dict) -> Tuple[int, bytes]:
        """POST to the endpoint and read the whole response within ``timeout``
        seconds, the same way as :meth:`.ChatClient.post`."""
        return post(self.url, self.api_key, payload, self.timeout)
