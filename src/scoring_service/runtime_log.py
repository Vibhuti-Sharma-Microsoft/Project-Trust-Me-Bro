from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self
from urllib.parse import unquote_plus

_REDACTED = "[REDACTED]"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CREDENTIAL_KEYS = {
    "authorization", "proxyauthorization", "apikey", "xapikey",
    "ocpapimsubscriptionkey", "subscriptionkey", "accesstoken", "refreshtoken",
    "idtoken", "authtoken", "authenticationtoken", "bearertoken", "apitoken",
    "sessiontoken", "token", "clientsecret", "password", "passwd", "pwd",
    "privatekey", "secret", "secretkey", "signingkey", "accountkey",
    "sharedaccesskey", "sharedaccesssignature", "connectionstring",
    "credential", "credentials", "cookie", "setcookie", "sessioncookie",
    "sessionid", "sastoken", "sig", "signature", "xamzsignature",
    "xamzsecuritytoken", "xgoogsignature", "awssecretaccesskey",
}
_CREDENTIAL_SUFFIXES = (
    "authorization", "apikey", "accesstoken", "refreshtoken", "clientsecret",
    "password", "passwd", "privatekey", "secretkey", "connectionstring",
)
_PRIVATE_REASONING_KEYS = {
    "scratchpad", "hiddenreasoning", "privatereasoning", "chainofthought",
    "reasoningcontent", "reasoningtrace", "thinking",
}
_BEARER = re.compile(r"(?i)\b((?:Bearer|Basic)[ \t]+)[A-Za-z0-9._~+/-]+=*")
_AUTH_HEADER = re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:[ \t]*)[^\r\n]*")
_USERINFO = re.compile(r"""(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/?#<>"']+@""")
_QUERY = re.compile(r"""([?&;])([A-Za-z0-9_.%~-]+)=([^&;#\s<>"']*)""")
_ASSIGNMENT = re.compile(
    r"""(?<![\w])(?P<quote>["']?)(?P<key>[A-Za-z_][A-Za-z0-9_.%-]*)(?P=quote)"""
    r"""(?P<separator>\s*[:=]\s*)(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&<>"'{}\[\]]+)"""
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----"
    r".*?-----END (?P=kind)-----",
    re.DOTALL,
)


class RuntimeLogError(RuntimeError):
    """The runtime journal could not safely create or persist an event."""


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unquote_plus(key).lower())


def _credential_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _CREDENTIAL_KEYS or normalized.endswith(_CREDENTIAL_SUFFIXES)


def _validate_json(value: Any, ancestors: set[int]) -> None:
    kind = type(value)
    if value is None or kind in (bool, int):
        return
    if kind is str:
        value.encode("utf-8")
        return
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON scalar")
        return
    if kind not in (list, dict):
        raise TypeError("Unsupported JSON value")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("Cyclic JSON value")
    ancestors.add(identity)
    try:
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise TypeError("JSON object keys must be strings")
            for key in value:
                _validate_json(key, ancestors)
            children = value.values()
        else:
            children = value
        for child in children:
            _validate_json(child, ancestors)
    finally:
        ancestors.remove(identity)


def _sanitize_text(text: str) -> str:
    if text.lstrip().startswith(("{", "[", '"')):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, (dict, list)):
                _validate_json(decoded, set())
                sanitized = _sanitize(decoded)
                return (
                    text if sanitized == decoded
                    else json.dumps(sanitized, ensure_ascii=False, allow_nan=False)
                )
            if isinstance(decoded, str):
                sanitized = _sanitize_text(decoded)
                return text if sanitized == decoded else json.dumps(sanitized, ensure_ascii=False)
    text = _PRIVATE_KEY.sub(_REDACTED, text)
    text = _AUTH_HEADER.sub(lambda match: match[1] + _REDACTED, text)
    text = _USERINFO.sub(lambda match: match[1] + _REDACTED + "@", text)
    text = _BEARER.sub(lambda match: match[1] + _REDACTED, text)
    text = _QUERY.sub(
        lambda match: (
            match[1] + match[2] + "=" + _REDACTED if _credential_key(match[2]) else match[0]
        ),
        text,
    )

    def assignment(match: re.Match[str]) -> str:
        if not _credential_key(match["key"]):
            return match[0]
        value = match["value"]
        replacement = value[0] + _REDACTED + value[0] if value[0] in "\"'" else _REDACTED
        return match["quote"] + match["key"] + match["quote"] + match["separator"] + replacement

    return _ASSIGNMENT.sub(assignment, text)


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            clean_key = _sanitize_text(key)
            if clean_key in sanitized:
                raise ValueError("Redaction would produce ambiguous JSON keys")
            sanitized[clean_key] = (
                _REDACTED
                if _credential_key(key) or _normalized_key(key) in _PRIVATE_REASONING_KEYS
                else _sanitize(child)
            )
        return sanitized
    return value


def _safe_destination(path: Path) -> Path:
    if ".." in path.parts or (path.drive and not path.is_absolute()):
        raise RuntimeLogError("Runtime log path must not traverse parent directories")
    absolute = path if path.is_absolute() else Path.cwd() / path
    if os.name == "nt":
        if str(absolute).startswith(("\\\\?\\", "\\\\.\\")):
            raise RuntimeLogError("Runtime log device paths are not permitted")
        if any(
            ":" in part or part.endswith((".", " ")) or Path(part).is_reserved()
            for part in absolute.parts[1:]
        ):
            raise RuntimeLogError("Runtime log path has an unsafe Windows component")
    for parent in reversed(absolute.parents):
        metadata = parent.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise RuntimeLogError("Runtime log parents must be real directories, not links or reparse points")
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return absolute
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT:
        raise RuntimeLogError("Runtime log destination must not be a link or reparse point")
    raise RuntimeLogError("Runtime log destination already exists; refusing to overwrite")


def _open_posix(path: Path) -> int:
    # Relative, no-follow traversal pins the actual parent rather than trusting
    # a check-then-open path that could be redirected by a concurrent rename.
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow_flag = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(directory_flag, int) or not isinstance(nofollow_flag, int):
        raise RuntimeLogError("This platform cannot safely open a no-follow runtime log")
    flags = os.O_RDONLY | directory_flag | nofollow_flag
    directories = [os.open(path.anchor, flags)]
    descriptor: int | None = None
    primary: BaseException | None = None
    try:
        for part in path.parts[1:-1]:
            directories.append(os.open(part, flags, dir_fd=directories[-1]))
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow_flag, 0o600, dir_fd=directories[-1])
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_error: OSError | None = None
        for parent in reversed(directories):
            try:
                os.close(parent)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            if primary is not None:
                primary.add_note("A runtime log directory descriptor also failed to close.")
            else:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        cleanup_error.add_note("The runtime log descriptor also failed to close.")
                raise RuntimeLogError("Could not release the runtime log directory") from cleanup_error
    if descriptor is None:
        raise RuntimeLogError("Runtime log file descriptor was not opened")
    return descriptor


def _open_windows(path: Path) -> int:
    if sys.platform != "win32":
        raise RuntimeLogError("Windows runtime log creation requires Windows")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    information = kernel.GetFileInformationByHandleEx
    information.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    information.restype = wintypes.BOOL

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    invalid_handle = ctypes.c_void_p(-1).value
    directories: list[Any] = []
    output: Any = None
    descriptor: int | None = None
    primary: BaseException | None = None
    try:
        for parent in reversed(path.parents):
            # Hold each no-follow directory handle without write/delete sharing
            # until CREATE_NEW completes, preventing ancestor redirection.
            handle = create_file(str(parent), 0x80, 0x1, None, 3, 0x02000000 | 0x00200000, None)
            if handle == invalid_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            directories.append(handle)
            attributes = AttributeTag()
            if not information(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes.attributes & _REPARSE_POINT or not attributes.attributes & 0x10:
                raise RuntimeLogError("Runtime log parent is not a safe directory")
        output = create_file(str(path), 0x40000000, 0x1, None, 1, 0x80 | 0x00200000, None)
        if output == invalid_handle:
            output = None
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = msvcrt.open_osfhandle(output, os.O_WRONLY | os.O_BINARY)
        output = None
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_error: OSError | None = None
        if output is not None:
            if not close_handle(output):
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
        for handle in reversed(directories):
            if not close_handle(handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if cleanup_error is not None:
            if primary is not None:
                primary.add_note("A runtime log path handle also failed to close.")
            else:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        cleanup_error.add_note("The runtime log descriptor also failed to close.")
                raise RuntimeLogError("Could not release the runtime log path handles") from cleanup_error
    if descriptor is None:
        raise RuntimeLogError("Runtime log file descriptor was not opened")
    return descriptor


class RuntimeJournal:
    """Exclusive, flushed UTF-8 JSONL journal of supplied semantic evaluator events.

    The parent directory must already exist. Construction opens only ``path``;
    no start/stop events, parent directories, or sidecar files are synthesized.
    Sequences start at one and follow the locked write order. Pass JSON values
    (including timestamp strings), not model objects or transport responses.

    Redaction targets explicit credentials, conventional credential strings and
    explicitly named private reasoning fields. It is not a general PII scrubber,
    does not inspect environment secrets, and does not identify arbitrary secrets
    or hidden reasoning in prose. Callers must supply semantic judgments only.
    """

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._failed = False
        self._sequence = 0
        descriptor: int | None = None
        try:
            self.path = _safe_destination(Path(path))
            descriptor = _open_windows(self.path) if os.name == "nt" else _open_posix(self.path)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeLogError("Runtime log destination is not a regular file")
            self._stream: BinaryIO = os.fdopen(descriptor, "wb", buffering=0)
            descriptor = None
        except BaseException as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    exc.add_note("The runtime log descriptor also failed to close during open cleanup.")
            if isinstance(exc, (OSError, ValueError)):
                raise RuntimeLogError("Could not exclusively open the runtime log") from exc
            raise

    def __enter__(self) -> Self:
        with self._lock:
            if self._closed:
                raise RuntimeLogError("Runtime journal is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except RuntimeLogError:
            if exc is None:
                raise
            exc.add_note("The runtime journal also failed to close; the original exception is preserved.")

    def event(self, event_name: str, case_id: str | None = None, **details: Any) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeLogError("Runtime journal is closed")
            if self._failed:
                raise RuntimeLogError("Runtime journal is unusable after an earlier write failure")
            if type(event_name) is not str or not event_name.strip():
                raise RuntimeLogError("Runtime event name must be a nonempty string")
            if case_id is not None and type(case_id) is not str:
                raise RuntimeLogError("Runtime event case_id must be a string or null")
            try:
                record = {
                    "sequence": self._sequence + 1,
                    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "event": event_name,
                    "case_id": case_id,
                    "details": details,
                }
                _validate_json(record, set())
                encoded = (json.dumps(_sanitize(record), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            except (TypeError, ValueError, RecursionError, OverflowError) as exc:
                raise RuntimeLogError("Runtime event must contain only finite, serializable UTF-8 JSON values") from exc
            start: int | None = None
            try:
                start = self._stream.tell()
                if self._stream.write(encoded) != len(encoded):
                    raise OSError("Incomplete runtime log write")
                self._stream.flush()
            except (OSError, ValueError) as exc:
                self._failed = True
                failure = RuntimeLogError("Could not persist the runtime event")
                if start is not None:
                    try:
                        self._stream.seek(start)
                        self._stream.truncate()
                        self._stream.flush()
                    except (OSError, ValueError):
                        failure.add_note("The incomplete event could not be rolled back; the journal is unusable.")
                raise failure from exc
            self._sequence += 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stream.close()
            except (OSError, ValueError) as exc:
                raise RuntimeLogError("Could not close the runtime log") from exc
