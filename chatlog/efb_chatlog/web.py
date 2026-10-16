# coding=utf-8
"""The web service: a single page, a JSON API behind passkey sign-in, and
the sync from ``tgdata.db`` in a background thread."""

import html
import logging
import mimetypes
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import telegram
from .archive import Archive, parse_cursor
from .auth import Auth, AuthError
from .paths import Config, Paths
from .sync import Syncer

logger = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("static") / "index.html"


class RateLimit:
    """At most ``count`` events in ``seconds`` per key."""

    def __init__(self, count: int, seconds: float):
        self.count = count
        self.seconds = seconds
        self.events: Dict[str, Deque[float]] = {}
        self.lock = threading.Lock()

    def hit(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            if len(self.events) > 10000:
                self.events.clear()
            events = self.events.setdefault(key, deque())
            while events and events[0] < now - self.seconds:
                events.popleft()
            if len(events) >= self.count:
                return False
            events.append(now)
            return True


def send_enrollment(config: Config, auth: Auth, requester: str) -> None:
    """Send the admin a one-time link to trust a device."""
    token = auth.create_enrollment()
    # In the fragment, so the token never reaches a server log or a referrer.
    link = "{}/#enroll={}".format(config.origin, token)
    telegram.send_message(config.bot_token, config.admin, (
        "🔐 <b>EFB 聊天记录：登记新设备</b>\n\n{}\n\n"
        "在要登记的设备上打开链接，用指纹或面容创建通行密钥。链接 {} 分钟内有效，只能用一次。\n"
        '<a href="{}">登记这台设备</a>\n\n'
        "如果不是你本人操作，忽略这条消息即可。").format(
        html.escape(requester), auth.enroll_seconds // 60, html.escape(link)))


def create_app(paths: Paths, config: Config, sync: bool = True) -> FastAPI:
    auth = Auth(paths.auth, config.domain, config.origin, config.session_days)
    archive = Archive(paths.archive)
    secure = config.origin.startswith("https://")
    cookie = "__Host-chatlog" if secure else "chatlog"
    login_limit = RateLimit(20, 300)
    enroll_limit = RateLimit(3, 3600)
    enroll_ip_limit = RateLimit(1, 60)
    page = PAGE.read_text(encoding="utf-8")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    if sync:
        syncer = Syncer(paths, config.tgdata_timezone)

        def sync_loop():
            while True:
                try:
                    syncer.run()
                except Exception:
                    logger.exception("Sync from tgdata.db failed")
                time.sleep(config.sync_seconds)

        threading.Thread(target=sync_loop, name="chatlog sync", daemon=True).start()

    # region Plumbing

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Cookies are SameSite=Strict already; this also stops a request
        # from another site that carries no cookie from starting a ceremony.
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") != config.origin:
            return JSONResponse({"error": "来源不受信任。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, e: AuthError):
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.exception_handler(telegram.TelegramError)
    async def telegram_error(request: Request, e: telegram.TelegramError):
        logger.warning("Telegram: %s", e)
        return JSONResponse({"error": "Telegram 请求失败：{}".format(e)}, status_code=502)

    def client(request: Request) -> str:
        return request.client.host if request.client else "?"

    def signed_in(request: Request) -> str:
        credential_id = auth.session(request.cookies.get(cookie))
        if credential_id is None:
            raise HTTPException(401, "需要登录。")
        return credential_id

    def set_session(response: Response, token: str):
        response.set_cookie(cookie, token, max_age=int(config.session_days * 86400), httponly=True,
                            secure=secure, samesite="strict", path="/")

    @app.get("/", response_class=HTMLResponse)
    def index():
        nonce = secrets.token_urlsafe(16)
        csp = ("default-src 'none'; script-src 'nonce-{0}'; style-src 'nonce-{0}'; connect-src 'self'; "
               "img-src 'self' https: data:; media-src 'self'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'").format(nonce)
        return HTMLResponse(page.replace("__NONCE__", nonce), headers={"Content-Security-Policy": csp})

    # endregion
    # region Sign-in

    @app.get("/api/me")
    def me(request: Request):
        credential_id = auth.session(request.cookies.get(cookie))
        return {"signed_in": credential_id is not None, "device": credential_id}

    @app.post("/api/login/options")
    def login_options(request: Request):
        if not login_limit.hit(client(request)):
            raise HTTPException(429, "尝试太频繁，请稍后再试。")
        challenge_id, options = auth.login_options()
        return Response('{{"challenge":"{}","options":{}}}'.format(challenge_id, options),
                        media_type="application/json")

    @app.post("/api/login")
    def login(request: Request, response: Response, body: Dict[str, Any] = Body(...)):
        token = auth.login(body.get("challenge"), body.get("credential"), client(request),
                           request.headers.get("user-agent"))
        set_session(response, token)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(cookie))
        response.delete_cookie(cookie, path="/", secure=secure, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/enroll/request")
    def enroll_request(request: Request):
        ip = client(request)
        if not enroll_ip_limit.hit(ip) or not enroll_limit.hit("all"):
            raise HTTPException(429, "申请太频繁，请稍后再试。")
        send_enrollment(config, auth, "申请来源：{}\n浏览器：{}".format(
            ip, (request.headers.get("user-agent") or "未知")[:200]))
        return {"ok": True}

    @app.post("/api/enroll/options")
    def enroll_options(body: Dict[str, Any] = Body(...)):
        challenge_id, options = auth.registration_options(body.get("token"))
        return Response('{{"challenge":"{}","options":{}}}'.format(challenge_id, options),
                        media_type="application/json")

    @app.post("/api/enroll")
    def enroll(request: Request, response: Response, body: Dict[str, Any] = Body(...)):
        token = auth.register(body.get("token"), body.get("challenge"), body.get("credential"),
                              body.get("name"), client(request), request.headers.get("user-agent"))
        set_session(response, token)
        try:
            telegram.send_message(config.bot_token, config.admin, "✅ EFB 聊天记录：已登记新设备「{}」（{}）。".format(
                html.escape(str(body.get("name") or "未命名设备")[:40]), html.escape(client(request))))
        except telegram.TelegramError as e:
            logger.warning("Failed to report the new device: %s", e)
        return {"ok": True}

    @app.get("/api/devices")
    def devices(current: str = Depends(signed_in)):
        return {"devices": [dict(i, current=i["id"] == current) for i in auth.devices()]}

    @app.delete("/api/devices/{credential_id}")
    def revoke(credential_id: str, _: str = Depends(signed_in)):
        if not auth.revoke(credential_id):
            raise HTTPException(404, "没有这个设备。")
        return {"ok": True}

    # endregion
    # region Archive

    @app.get("/api/stats")
    def stats(_: str = Depends(signed_in)):
        result = archive.stats()
        try:
            result["efb_active"] = max(paths.tgdata.stat().st_mtime,
                                       Path(str(paths.tgdata) + "-wal").stat().st_mtime
                                       if Path(str(paths.tgdata) + "-wal").exists() else 0)
        except OSError:
            result["efb_active"] = None
        result["devices"] = len(auth.devices())
        return result

    @app.get("/api/chats")
    def chats(_: str = Depends(signed_in)):
        return {"chats": archive.chats()}

    def cursor(value: Optional[str]):
        try:
            return parse_cursor(value)
        except ValueError:
            raise HTTPException(400, "游标无效。") from None

    def type_list(value: Optional[str]):
        return [i for i in (value or "").split(",") if i]

    @app.get("/api/messages")
    def messages(chat: str, before: Optional[str] = None, after: Optional[str] = None,
                 around: Optional[int] = None, types: Optional[str] = None, _: str = Depends(signed_in)):
        try:
            return archive.messages(chat, before=cursor(before), after=cursor(after), around=around,
                                    types=type_list(types))
        except LookupError:
            raise HTTPException(404, "没有这条消息。") from None

    @app.get("/api/search")
    def search(q: str = "", chat: Optional[str] = None, sender: Optional[str] = None,
               types: Optional[str] = None, before: Optional[str] = None, since: Optional[float] = None,
               until: Optional[float] = None, _: str = Depends(signed_in)):
        if not q.strip() and not sender and not types and since is None and until is None:
            raise HTTPException(400, "请输入要搜索的内容。")
        return archive.search(q.strip()[:200], chat=chat or None, sender=(sender or "").strip() or None,
                              types=type_list(types), before=cursor(before), since=since, until=until)

    @app.get("/api/media/{message_id}")
    def media(message_id: int, _: str = Depends(signed_in)):
        row = archive.file_of(message_id)
        if row is None or not row["file_id"]:
            raise HTTPException(404, "没有可显示的文件。")
        token = config.bot_tokens.get(row["bot_id"] or "", config.bot_token)
        size, content = telegram.open_file(token, row["file_id"])
        media_type = row["mime"] or mimetypes.guess_type("x." + (row["type"] or "").lower())[0] \
            or "application/octet-stream"
        # Files come from anyone in a chat: only media is shown in the page,
        # anything else (an HTML file, say) is a download, and sandboxed.
        headers = {"Cache-Control": "private, max-age=604800",
                   "Content-Security-Policy": "sandbox; default-src 'none'"}
        if not media_type.startswith(("image/", "video/", "audio/")) or "svg" in media_type:
            media_type = "application/octet-stream"
            headers["Content-Disposition"] = "attachment"
        if size:
            headers["Content-Length"] = str(size)
        return StreamingResponse(content, media_type=media_type, headers=headers)

    # endregion

    return app
