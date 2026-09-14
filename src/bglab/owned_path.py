"""Filesystem boundaries for names stored below BGLab-owned roots."""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from pathlib import Path


class OwnedPathError(ValueError):
    """Raised when a logical name cannot map to one safe owned child."""


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_PATH_SYNTAX = frozenset('/\\:<>"|?*')


def normalize_owned_component(value: str, max_length: int = 100) -> str:
    """Normalize one logical name without accepting filesystem syntax."""
    if not isinstance(value, str):
        raise OwnedPathError("owned name must be text")
    if max_length < 1:
        raise OwnedPathError("owned component length must be positive")

    normalized = unicodedata.normalize("NFKC", value)
    if not normalized or normalized != normalized.strip():
        raise OwnedPathError("owned name must not be empty or padded")
    if normalized.endswith((".", " ")):
        raise OwnedPathError("owned name must not end with dot or space")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise OwnedPathError("owned name contains a control character")
    if any(char in _PATH_SYNTAX for char in normalized):
        raise OwnedPathError("owned name contains filesystem path syntax")

    rendered: list[str] = []
    whitespace_pending = False
    for char in normalized:
        if char.isspace():
            whitespace_pending = True
            continue
        if whitespace_pending:
            rendered.append("-")
            whitespace_pending = False
        if char.isalnum() or char in "._-":
            rendered.append(char.lower())
        else:
            raise OwnedPathError("owned name contains an unsupported character")

    component = "".join(rendered)
    if len(component) > max_length:
        raise OwnedPathError("owned name is too long")
    if not component or component in {".", ".."}:
        raise OwnedPathError("owned name has no safe component")
    device_name = component.split(".", 1)[0].upper()
    if device_name in _WINDOWS_RESERVED:
        raise OwnedPathError("owned name is reserved by Windows")
    return component


def validate_exact_owned_component(value: str, max_length: int = 128) -> str:
    """Validate one portable identifier without changing its spelling."""
    if not isinstance(value, str):
        raise OwnedPathError("owned identifier must be text")
    if max_length < 1:
        raise OwnedPathError("owned component length must be positive")
    if not value or len(value) > max_length:
        raise OwnedPathError("owned identifier has invalid length")
    if any(
        not ((char.isascii() and char.isalnum()) or char in "_-")
        for char in value
    ):
        raise OwnedPathError("owned identifier contains invalid characters")
    if value.upper() in _WINDOWS_RESERVED:
        raise OwnedPathError("owned identifier is reserved by Windows")
    return value


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _validate_regular_stat(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise OwnedPathError("owned file is a symbolic link")
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if attributes & reparse_flag:
        raise OwnedPathError("owned file is a reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise OwnedPathError("owned file is not regular")
    if int(getattr(info, "st_nlink", 1) or 0) != 1:
        raise OwnedPathError("owned file has multiple hard links")


def open_owned_regular(
    path: Path,
    *,
    create: bool = False,
    read_only: bool = False,
) -> int:
    """Open one regular, single-link file and return its validated fd.

    The path is never opened with ``O_TRUNC``.  Existing path identity is
    checked before open and compared with ``fstat`` after open; a newly
    created file is validated by its returned descriptor before the caller can
    write to it.
    """
    target = Path(path)
    flags = os.O_RDONLY if read_only else os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)

    try:
        before = os.lstat(target)
    except FileNotFoundError:
        before = None

    if before is not None:
        _validate_regular_stat(before)

    fd: int
    if before is None and create:
        try:
            fd = os.open(target, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            before = os.lstat(target)
            _validate_regular_stat(before)
            fd = os.open(target, flags)
    else:
        try:
            fd = os.open(target, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OwnedPathError("owned file is a symbolic link") from exc
            raise

    try:
        after = os.fstat(fd)
        _validate_regular_stat(after)
        after_path = os.lstat(target)
        _validate_regular_stat(after_path)
        if not os.path.samestat(after_path, after):
            raise OwnedPathError("owned file changed during open")
        if before is not None and not os.path.samestat(before, after):
            raise OwnedPathError("owned file changed during open")
        return fd
    except Exception:
        os.close(fd)
        raise


def _owned_child_path(
    root: Path,
    component: str,
    *,
    reject_case_alias: bool = False,
) -> Path:
    root_path = Path(root).expanduser()
    if _is_link_or_reparse(root_path):
        raise OwnedPathError("owned root is a link or reparse point")
    root_resolved = root_path.resolve(strict=False)
    target = root_resolved / component
    if target.parent != root_resolved:
        raise OwnedPathError("owned target is not a direct child")
    if _is_link_or_reparse(target):
        raise OwnedPathError("owned target is a link or reparse point")
    resolved = target.resolve(strict=False)
    if resolved.parent != root_resolved:
        raise OwnedPathError("owned target escapes its root")
    if reject_case_alias and os.name == "nt" and root_resolved.is_dir():
        wanted = component.casefold()
        for child in root_resolved.iterdir():
            if child.name.casefold() == wanted and child.name != component:
                raise OwnedPathError("owned identifier aliases an existing child")
    return target


def owned_child(root: Path, value: str, max_length: int = 100) -> Path:
    """Return one normalized, direct, non-link child below an owned root."""
    component = normalize_owned_component(value, max_length=max_length)
    return _owned_child_path(root, component)


def exact_owned_child(root: Path, value: str, max_length: int = 128) -> Path:
    """Return one exact portable identifier below an owned root."""
    component = validate_exact_owned_component(value, max_length=max_length)
    return _owned_child_path(root, component, reject_case_alias=True)


__all__ = [
    "OwnedPathError",
    "exact_owned_child",
    "normalize_owned_component",
    "open_owned_regular",
    "owned_child",
    "validate_exact_owned_component",
]
