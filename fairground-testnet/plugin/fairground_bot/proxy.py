"""HTTP proxy parsing for multi-account Fairground execution.

Supported input formats (all become http://user:pass@host:port):

- ``ip:port:user:pass``  (preferred bulk format)
- ``user:pass@ip:port``
- ``http://user:pass@ip:port``
- ``http://ip:port`` (auth-less; discouraged but accepted)

Proxies are mandatory for volume-farm execution. Ambient HTTP(S)_PROXY is never
used — each account binds its own explicit proxy URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import quote, urlparse


class ProxyError(ValueError):
    """Raised when a proxy string is missing, malformed, or unsafe."""


_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                      r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class ParsedProxy:
    """A validated HTTP proxy with a ready-to-use URL."""

    host: str
    port: int
    username: str | None = field(repr=False)
    password: str | None = field(repr=False)
    url: str = field(repr=False)

    def redacted(self) -> str:
        if self.username:
            return f"http://***:***@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"


def _validate_host(host: str) -> str:
    candidate = host.strip().lower()
    if not candidate or len(candidate) > 253:
        raise ProxyError("Proxy host is empty or too long")
    if candidate in {"localhost", "127.0.0.1", "::1"}:
        raise ProxyError("Loopback proxies are not allowed for farm accounts")
    if not (_IPV4_RE.fullmatch(candidate) or _HOST_RE.fullmatch(candidate)):
        raise ProxyError(f"Proxy host is invalid: {host!r}")
    return candidate


def _validate_port(raw: object) -> int:
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ProxyError("Proxy port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ProxyError("Proxy port must be between 1 and 65535")
    return port


def _build_url(host: str, port: int, username: str | None, password: str | None) -> str:
    if username is not None and password is not None:
        user = quote(username, safe="")
        pwd = quote(password, safe="")
        return f"http://{user}:{pwd}@{host}:{port}"
    if username is not None or password is not None:
        raise ProxyError("Proxy username and password must both be set or both omitted")
    return f"http://{host}:{port}"


def parse_proxy(raw: str) -> ParsedProxy:
    """Parse one proxy line into a validated ``ParsedProxy``."""

    text = (raw or "").strip()
    if not text or text.startswith("#"):
        raise ProxyError("Proxy line is empty")
    if any(ch in text for ch in " \t\r\n"):
        raise ProxyError("Proxy line must not contain whitespace")

    # Full URL form.
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"}:
            raise ProxyError("Only HTTP proxies are supported")
        if not parsed.hostname or parsed.port is None:
            raise ProxyError("Proxy URL must include host and port")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ProxyError("Proxy URL must not include path/query/fragment")
        host = _validate_host(parsed.hostname)
        port = _validate_port(parsed.port)
        username = parsed.username
        password = parsed.password
        url = _build_url(host, port, username, password)
        return ParsedProxy(host=host, port=port, username=username, password=password, url=url)

    # user:pass@host:port
    if "@" in text:
        auth, _, endpoint = text.rpartition("@")
        if not auth or not endpoint:
            raise ProxyError("Proxy auth section is malformed")
        user, sep, password = auth.partition(":")
        if not sep or not user or not password:
            raise ProxyError("Proxy auth must be user:pass")
        host_raw, sep, port_raw = endpoint.rpartition(":")
        if not sep:
            raise ProxyError("Proxy endpoint must be host:port")
        host = _validate_host(host_raw)
        port = _validate_port(port_raw)
        url = _build_url(host, port, user, password)
        return ParsedProxy(host=host, port=port, username=user, password=password, url=url)

    # ip:port:user:pass  (preferred bulk format; host may be hostname)
    parts = text.split(":")
    if len(parts) == 4:
        host = _validate_host(parts[0])
        port = _validate_port(parts[1])
        user = parts[2]
        password = parts[3]
        if not user or not password:
            raise ProxyError("Proxy username and password must be non-empty")
        url = _build_url(host, port, user, password)
        return ParsedProxy(host=host, port=port, username=user, password=password, url=url)

    if len(parts) == 2:
        host = _validate_host(parts[0])
        port = _validate_port(parts[1])
        url = _build_url(host, port, None, None)
        return ParsedProxy(host=host, port=port, username=None, password=None, url=url)

    raise ProxyError(
        "Unsupported proxy format; use ip:port:user:pass "
        "(example: 209.166.17.158:6319:eomflbtx:7mq2smuvjflp)"
    )
