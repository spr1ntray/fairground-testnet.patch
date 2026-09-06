"""Compact, coloured and secret-safe runtime logging for the IDE console."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import stat
import sys
import threading
import unicodedata
from typing import TextIO


_RESET = "\033[0m"
_DIM = "\033[90m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_MAGENTA = "\033[95m"

_LEVELS = {
    "INFO": ("INFO", _CYAN),
    "SUCCESS": ("SUCCESS", _GREEN),
    "WARNING": ("WARNING", _YELLOW),
    "ERROR": ("ERROR", _RED),
    "ACTION": ("ACTION", _MAGENTA),
}
_MARKERS = {
    "•": "INFO",
    "✓": "SUCCESS",
    "!": "WARNING",
    "-": "ERROR",
    "×": "ERROR",
}
_LEGACY = re.compile(r"^\[([•✓!\-×])\]\s*(.*)$")
_PROXY_URL_AUTH = re.compile(r"(?i)(https?://)[^\s:/@]+:[^\s@]+@")
_PROXY_HOST = (
    r"(?:\d{1,3}(?:\.\d{1,3}){3}|"
    r"(?=[A-Za-z0-9.-]*[A-Za-z])[A-Za-z0-9]"
    r"(?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
)
_PROXY_PLAIN = re.compile(
    rf"(?i)(?<![A-Za-z0-9.-])({_PROXY_HOST}:\d{{1,5}}):"
    r"[^:\s]+:[^\s]+"
)
_PROXY_AT_AUTH = re.compile(
    rf"(?i)(?<![A-Za-z0-9._%+-])[^\s:/@]+:(?!//)[^\s@]+@"
    rf"(?={_PROXY_HOST}:\d{{1,5}}(?:\b|$))"
)
_BEARER = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)authorization(?P=key_quote)\s*[:=]\s*"
    r"(?P<value_quote>[\"']?)bearer\s+"
    r")"
    r"[^\"'\s,;}\]]+"
)
_COOKIE = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)(?:cookie|set-cookie)(?P=key_quote)\s*[:=]\s*"
    r")"
    r"(?P<value>"
    r"\"(?:\\.|[^\"\\])*\"|"
    r"'(?:\\.|[^'\\])*'|"
    r".*"
    r")"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)"
    r"(?:api[_-]?token|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"adspower[_-]?api[_-]?key|adspower[_-]?key|"
    r"password|passwd|passphrase|private[_ -]?key(?:[_ -]?hex)?|secret(?:[_-]?key)?"
    r")"
    r"(?P=key_quote)\s*[:=]\s*"
    r")"
    r"(?P<value>"
    r"\"(?:\\.|[^\"\\])*\"|"
    r"'(?:\\.|[^'\\])*'|"
    r"[^\s)}\]]+"
    r")"
)
_BARE_64HEX = re.compile(
    r"(?i)(?<![0-9a-f])(?:0x)?[0-9a-f]{64}(?![0-9a-f])"
)
# Public EVM addresses — never scrub (they are not secrets).
_EVM_ADDRESS = re.compile(r"(?i)(?:0x)?[0-9a-f]{40}")
# AdsPower / generic long API tokens (hex or base64-ish).
# Do NOT put '=' in the body class — that would swallow `wallet=0x…` assignments.
# Allow optional base64 padding only at the end.
_LONG_API_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_+/-])"
    r"[A-Za-z0-9_+/-]{32,254}={0,2}"
    r"(?![A-Za-z0-9_+/=-])"
)
# Non-Bearer Authorization values only — Bearer is handled above and must keep
# the "Bearer <redacted>" shape for structured logs / tests.
_AUTH_HEADER = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)authorization(?P=key_quote)\s*[:=]\s*"
    r"(?P<value_quote>[\"']?)"
    r")"
    r"(?!bearer\b)"
    r"[^\"'\s,;}\]]+"
)


def _supports_colour(stream: TextIO) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR") not in {None, "", "0"}:
        return True
    pycharm = os.environ.get("PYCHARM_HOSTED") not in {None, "", "0"}
    vscode = (
        os.environ.get("VSCODE_PID") not in {None, "", "0"}
        or os.environ.get("TERM_PROGRAM", "").lower() == "vscode"
    )
    return bool(
        getattr(stream, "isatty", lambda: False)() or pycharm or vscode
    )


def _escape_controls(value: str) -> str:
    rendered: list[str] = []
    for char in value:
        if char == "\n":
            rendered.append(" ↳ ")
        elif char in {"\r", "\t"}:
            rendered.append(" ")
        elif unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}:
            width = 4 if ord(char) <= 0xFFFF else 8
            rendered.append(f"\\u{ord(char):0{width}x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def _replace_sensitive_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        replacement = f"{value[0]}<redacted>{value[-1]}"
    else:
        replacement = "<redacted>"
    return f"{match.group('prefix')}{replacement}"


def redact(message: object) -> str:
    """Remove credential shapes and terminal controls before any output."""

    text = _escape_controls(str(message))
    text = _PROXY_URL_AUTH.sub(r"\1***:***@", text)
    text = _PROXY_AT_AUTH.sub("***:***@", text)
    text = _PROXY_PLAIN.sub(r"\1:***:***", text)
    text = _BEARER.sub(r"\g<prefix><redacted>", text)
    text = _AUTH_HEADER.sub(r"\g<prefix><redacted>", text)
    text = _COOKIE.sub(_replace_sensitive_assignment, text)
    text = _SENSITIVE_ASSIGNMENT.sub(_replace_sensitive_assignment, text)
    text = _BARE_64HEX.sub("<redacted-64hex>", text)
    # Last pass: long tokens that look like API keys (AdsPower etc.).
    # Preserve public 40-hex addresses; scrub private 64-hex and opaque secrets.
    def _scrub_long_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.isdigit():
            return token
        # Already-redacted placeholders from earlier passes.
        if token.startswith("<redacted"):
            return token
        # Wallet addresses are 40 hex — leave them (public).
        if _EVM_ADDRESS.fullmatch(token):
            return token
        if re.fullmatch(r"(?i)(?:0x)?[0-9a-f]{64}", token):
            return "<redacted-64hex>"
        # AdsPower keys / long secrets (body without trailing = padding).
        body = token.rstrip("=")
        if len(body) >= 32:
            return "<redacted-token>"
        return token

    return _LONG_API_TOKEN.sub(_scrub_long_token, text)


class HumanLogger:
    """Thread-safe console logger with a seven-day plain-text audit log."""

    def __init__(
        self,
        *,
        log_dir: Path | str = "logs",
        stream: TextIO | None = None,
        colour: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.colour = _supports_colour(self.stream) if colour is None else bool(colour)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.lstat(self.log_dir)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("Log directory must be a real local directory")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise OSError("Log directory must be owned by the current user")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        directory_fd = os.open(self.log_dir, flags)
        try:
            opened = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError("Log directory must be a real local directory")
            if hasattr(os, "geteuid") and opened.st_uid != os.geteuid():
                raise OSError("Log directory must be owned by the current user")
            if (metadata.st_dev, metadata.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise OSError("Log directory changed while opening")
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)
        self._lock = threading.RLock()
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now().timestamp() - timedelta(days=7).total_seconds()
        for path in self.log_dir.glob("fairground_*.log"):
            try:
                metadata = os.lstat(path)
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_nlink == 1
                    and (
                        not hasattr(os, "geteuid")
                        or metadata.st_uid == os.geteuid()
                    )
                    and metadata.st_mtime < cutoff
                ):
                    path.unlink()
            except OSError:
                continue

    def _file_path(self, now: datetime) -> Path:
        return self.log_dir / f"fairground_{now:%Y-%m-%d}.log"

    @staticmethod
    def _split_legacy(message: str) -> tuple[str, str, str]:
        level = "INFO"
        match = _LEGACY.match(message.strip())
        if match:
            level = _MARKERS[match.group(1)]
            message = match.group(2).strip()
        scope = "SYSTEM"
        if " | " in message:
            candidate, detail = message.split(" | ", 1)
            if candidate.strip():
                scope = candidate.strip()
                message = detail.strip()
        return level, scope, message

    def __call__(self, message: str) -> None:
        level, scope, detail = self._split_legacy(message)
        self.log(level, detail, scope=scope)

    def log(self, level: str, message: object, *, scope: str = "SYSTEM") -> None:
        safe_level = level.upper() if level.upper() in _LEVELS else "INFO"
        label, colour = _LEVELS[safe_level]
        safe_scope = redact(scope)[:24]
        safe_message = redact(message)
        now = datetime.now()
        plain = (
            f"{now:%H:%M:%S} | {label:<7} | {safe_scope:<24} | {safe_message}"
        )
        if self.colour:
            rendered = (
                f"{_DIM}{now:%H:%M:%S}{_RESET} | "
                f"{colour}{label:<7}{_RESET} | "
                f"{_CYAN}{safe_scope:<24}{_RESET} | "
                f"{colour if safe_level in {'SUCCESS', 'WARNING', 'ERROR'} else ''}"
                f"{safe_message}{_RESET if safe_level in {'SUCCESS', 'WARNING', 'ERROR'} else ''}"
            )
        else:
            rendered = plain
        with self._lock:
            print(rendered, file=self.stream, flush=True)
            try:
                path = self._file_path(now)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                flags |= getattr(os, "O_NONBLOCK", 0)
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(path, flags, 0o600)
                try:
                    metadata = os.fstat(fd)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or (
                            hasattr(os, "geteuid")
                            and metadata.st_uid != os.geteuid()
                        )
                    ):
                        raise OSError("Unsafe log artifact")
                    os.fchmod(fd, 0o600)
                except BaseException:
                    os.close(fd)
                    raise
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(f"{now:%Y-%m-%d} {plain}\n")
            except OSError:
                # Logging must never prevent a safety or close path.
                pass

    def info(self, message: object, *, scope: str = "SYSTEM") -> None:
        self.log("INFO", message, scope=scope)

    def action(self, message: object, *, scope: str = "SYSTEM") -> None:
        self.log("ACTION", message, scope=scope)

    def success(self, message: object, *, scope: str = "SYSTEM") -> None:
        self.log("SUCCESS", message, scope=scope)

    def warning(self, message: object, *, scope: str = "SYSTEM") -> None:
        self.log("WARNING", message, scope=scope)

    def error(self, message: object, *, scope: str = "SYSTEM") -> None:
        self.log("ERROR", message, scope=scope)

    def section(self, title: str) -> None:
        self.info(f"{'─' * 12} {title.strip()} {'─' * 12}")
