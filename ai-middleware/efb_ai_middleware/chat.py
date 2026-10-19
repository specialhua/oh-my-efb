# coding=utf-8

import json
import logging
import re
import time
from typing import Any, Dict, List, Tuple, Union

import requests


class ChatClient:
    """Client of an OpenAI-compatible chat completions endpoint."""

    attempts = 3
    retry_wait = 5

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

    def complete(self, system: str, user: Union[str, List[Dict[str, Any]]]) -> str:
        """``user`` is text, or a list of content parts, e.g. with an image."""
        error: Exception = RuntimeError("no attempt made")
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(self.retry_wait)
            try:
                self.logger.info("Completion attempt %s: %s chars", attempt + 1, len(system) + len(str(user)))
                status, body = self.post({"model": self.model, "temperature": 0.3,
                                          "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}]})
                if status >= 400:
                    raise RuntimeError("HTTP {}: {}".format(status, body[:300].decode("utf-8", "replace")))
                text = json.loads(body)["choices"][0]["message"]["content"]
                # Some relays leak the reasoning of thinking models into the content.
                text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
                if not text:
                    raise RuntimeError("empty completion")
                return text
            except Exception as e:  # network, HTTP, malformed body
                error = e
                self.logger.warning("Completion attempt %s failed: %s", attempt + 1, e)
        raise error

    def post(self, payload: dict) -> Tuple[int, bytes]:
        """POST to the endpoint and read the whole response within ``timeout``
        seconds. The timeout of requests only bounds each read, so a relay
        that keeps sending chunks (e.g. keep-alive padding) would otherwise
        hold the request open indefinitely."""
        return post(self.url, self.api_key, payload, self.timeout)


def post(url: str, api_key: str, payload: dict, timeout: float) -> Tuple[int, bytes]:
    """See :meth:`ChatClient.post`."""
    deadline = time.monotonic() + timeout
    with requests.post(url, headers={"Authorization": "Bearer " + api_key},
                       json=payload, timeout=timeout, stream=True) as resp:
        body = bytearray()
        for chunk in resp.iter_content(8192):
            body += chunk
            if time.monotonic() > deadline:
                raise TimeoutError("no complete response in {:g} s".format(timeout))
        return resp.status_code, bytes(body)
