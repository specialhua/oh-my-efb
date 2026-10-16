# coding=utf-8
"""Sign-in with passkeys only: no password, no account name.

A device is trusted once a passkey is created on it, which takes a one-time
link sent by the bot to the admin in Telegram. Afterwards Touch ID / Face ID
(or the device PIN) on a trusted device opens a session.
"""

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL,
    name TEXT NOT NULL,
    transports TEXT,
    synced INTEGER NOT NULL,
    created REAL NOT NULL,
    last_used REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS enrollments (
    token_hash TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    used REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class AuthError(Exception):
    pass


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    challenge_seconds = 300
    enroll_seconds = 600
    touch_seconds = 300
    """A session's last use is written at most this often."""

    def __init__(self, path: Path, rp_id: str, origin: str, session_days: float):
        self.rp_id = rp_id
        self.origin = origin
        self.session_seconds = session_days * 86400
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            row = self.db.execute("SELECT value FROM meta WHERE key = 'user_id'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta (key, value) VALUES ('user_id', ?)",
                                (bytes_to_base64url(secrets.token_bytes(16)),))
            self.user_id = base64url_to_bytes(
                self.db.execute("SELECT value FROM meta WHERE key = 'user_id'").fetchone()[0])
        self.challenges: Dict[str, Tuple[bytes, str, float]] = {}
        """Pending ceremonies: challenge, kind and expiry by ID."""

    # region Challenges

    def new_challenge(self, kind: str) -> Tuple[str, bytes]:
        now = time.time()
        with self.lock:
            for key in [k for k, v in self.challenges.items() if v[2] < now]:
                del self.challenges[key]
            if len(self.challenges) > 1000:
                raise AuthError("请求过多，请稍后再试。")
            challenge_id, challenge = secrets.token_urlsafe(16), secrets.token_bytes(32)
            self.challenges[challenge_id] = (challenge, kind, now + self.challenge_seconds)
        return challenge_id, challenge

    def take_challenge(self, challenge_id: Any, kind: str) -> bytes:
        """A challenge is good for one attempt, failed or not."""
        with self.lock:
            entry = self.challenges.pop(challenge_id, None) if isinstance(challenge_id, str) else None
        if entry is None or entry[1] != kind or entry[2] < time.time():
            raise AuthError("验证已过期，请重试。")
        return entry[0]

    # endregion
    # region Enrollment

    def create_enrollment(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock, self.db:
            self.db.execute("DELETE FROM enrollments WHERE expires < ?", (now - 86400,))
            self.db.execute("INSERT INTO enrollments (token_hash, created, expires) VALUES (?, ?, ?)",
                            (digest(token), now, now + self.enroll_seconds))
        return token

    def check_enrollment(self, token: Any):
        if not isinstance(token, str) or not token:
            raise AuthError("登记链接无效。")
        with self.lock:
            row = self.db.execute("SELECT expires, used FROM enrollments WHERE token_hash = ?",
                                  (digest(token),)).fetchone()
        if row is None:
            raise AuthError("登记链接无效。")
        if row["used"] is not None:
            raise AuthError("这个登记链接已经用过了，请重新申请。")
        if row["expires"] < time.time():
            raise AuthError("登记链接已过期，请重新申请。")

    def registration_options(self, token: Any) -> Tuple[str, str]:
        self.check_enrollment(token)
        challenge_id, challenge = self.new_challenge("register")
        options = generate_registration_options(
            rp_id=self.rp_id, rp_name="EFB 聊天记录", user_id=self.user_id,
            user_name="efb", user_display_name="EFB 聊天记录", challenge=challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(i["id"]))
                                 for i in self.devices()])
        return challenge_id, options_to_json(options)

    def register(self, token: Any, challenge_id: Any, credential: Any, name: Any,
                 ip: Optional[str], user_agent: Optional[str]) -> str:
        """Trust the device of a new passkey; returns a session token."""
        self.check_enrollment(token)
        challenge = self.take_challenge(challenge_id, "register")
        try:
            verified = verify_registration_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, require_user_verification=True)
        except Exception as e:
            raise AuthError("通行密钥验证失败：{}".format(e)) from None
        name = (name if isinstance(name, str) else "").strip()[:40] or "未命名设备"
        transports = credential.get("response", {}).get("transports") if isinstance(credential, dict) else None
        credential_id = bytes_to_base64url(verified.credential_id)
        now = time.time()
        with self.lock, self.db:
            cur = self.db.execute("UPDATE enrollments SET used = ? WHERE token_hash = ? AND used IS NULL",
                                  (now, digest(token)))
            if not cur.rowcount:
                raise AuthError("这个登记链接已经用过了，请重新申请。")
            self.db.execute(
                "INSERT INTO credentials (id, public_key, sign_count, name, transports, synced, created, last_used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (credential_id, verified.credential_public_key, verified.sign_count, name,
                 json.dumps(transports) if isinstance(transports, list) else None,
                 int(verified.credential_backed_up), now, now))
        return self.new_session(credential_id, ip, user_agent)

    # endregion
    # region Sign-in

    def login_options(self) -> Tuple[str, str]:
        challenge_id, challenge = self.new_challenge("login")
        # No allowed credentials listed: the browser offers the passkeys it
        # has for this site, so nothing about them is revealed before sign-in.
        options = generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge, user_verification=UserVerificationRequirement.REQUIRED)
        return challenge_id, options_to_json(options)

    def login(self, challenge_id: Any, credential: Any, ip: Optional[str], user_agent: Optional[str]) -> str:
        challenge = self.take_challenge(challenge_id, "login")
        credential_id = credential.get("id") if isinstance(credential, dict) else None
        with self.lock:
            row = self.db.execute("SELECT * FROM credentials WHERE id = ?", (credential_id,)).fetchone() \
                if isinstance(credential_id, str) else None
        if row is None:
            raise AuthError("这个通行密钥未登记或已被移除。")
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, credential_public_key=row["public_key"],
                credential_current_sign_count=row["sign_count"], require_user_verification=True)
        except Exception as e:
            raise AuthError("通行密钥验证失败：{}".format(e)) from None
        with self.lock, self.db:
            self.db.execute("UPDATE credentials SET sign_count = ?, last_used = ? WHERE id = ?",
                            (verified.new_sign_count, time.time(), row["id"]))
        return self.new_session(row["id"], ip, user_agent)

    # endregion
    # region Sessions

    def new_session(self, credential_id: str, ip: Optional[str], user_agent: Optional[str]) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self.db.execute(
                "INSERT INTO sessions (token_hash, credential_id, created, last_seen, expires, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(token), credential_id, now, now, now + self.session_seconds, ip, (user_agent or "")[:300]))
        return token

    def session(self, token: Optional[str]) -> Optional[str]:
        """Credential ID of the session of ``token`` if it is valid."""
        if not token:
            return None
        now = time.time()
        with self.lock:
            row = self.db.execute(
                "SELECT s.credential_id, s.last_seen FROM sessions s JOIN credentials c ON c.id = s.credential_id "
                "WHERE s.token_hash = ? AND s.expires > ?", (digest(token), now)).fetchone()
            if row is None:
                return None
            if now - row["last_seen"] > self.touch_seconds:
                with self.db:
                    self.db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, digest(token)))
        return row["credential_id"]

    def logout(self, token: Optional[str]):
        if token:
            with self.lock, self.db:
                self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (digest(token),))

    def devices(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT c.id, c.name, c.synced, c.created, c.last_used, "
                "(SELECT count(*) FROM sessions s WHERE s.credential_id = c.id AND s.expires > ?) AS sessions "
                "FROM credentials c ORDER BY c.created", (time.time(),)).fetchall()
        return [dict(i) for i in rows]

    def revoke(self, credential_id: Optional[str] = None) -> int:
        """Remove a passkey and sign its sessions out; all of them without an ID."""
        with self.lock, self.db:
            if credential_id is None:
                self.db.execute("DELETE FROM sessions")
                return self.db.execute("DELETE FROM credentials").rowcount
            self.db.execute("DELETE FROM sessions WHERE credential_id = ?", (credential_id,))
            return self.db.execute("DELETE FROM credentials WHERE id = ?", (credential_id,)).rowcount

    # endregion

