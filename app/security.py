"""Session tokens, password checks and outbound-URL safety."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

SESSION_SECONDS = 30 * 24 * 3600


def eq(a: str, b: str) -> bool:
    """Constant-time comparison that never raises on non-ASCII input."""
    return hmac.compare_digest(a.encode("utf-8", "ignore"), b.encode("utf-8", "ignore"))


def load_secret(data_dir: Path) -> bytes:
    p = Path(data_dir) / "secret.key"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p.read_bytes()


def make_token(secret: bytes, password: str, ttl: int = SESSION_SECONDS) -> str:
    # Bound to the password: changing APP_PASSWORD invalidates old sessions.
    exp = str(int(time.time()) + ttl)
    sig = hmac.new(secret, f"{exp}|{password}".encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}.{sig}".encode()).decode()


def check_token(secret: bytes, password: str, token: str) -> bool:
    try:
        exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".", 1)
        if int(exp) < time.time():
            return False
        good = hmac.new(secret, f"{exp}|{password}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode(), good.encode())
    except Exception:
        return False


class LoginThrottle:
    """Lock a client out for a minute after 5 wrong passwords."""

    def __init__(self, limit=5, window=60):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, key: str) -> bool:
        hits = [t for t in self.fails.get(key, []) if t > time.time() - self.window]
        self.fails[key] = hits
        return len(hits) >= self.limit

    def fail(self, key: str):
        self.fails.setdefault(key, []).append(time.time())

    def reset(self, key: str):
        self.fails.pop(key, None)


class UnsafeURL(ValueError):
    pass


def check_url(url: str, allow_private: bool = False) -> str:
    """Allow only http(s) URLs that do not point at private/loopback addresses."""
    u = urlparse((url or "").strip())
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise UnsafeURL("Enter a valid http(s) URL")
    if allow_private:
        return url.strip()
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"Cannot resolve host: {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UnsafeURL("URLs pointing to private or local addresses are blocked")
    return url.strip()
