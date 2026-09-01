"""Small no-follow filesystem primitives used by lifecycle contracts.

The public contracts operate on local directories that may be observed by
other processes.  A preflight-only pathname check is insufficient on Windows:
the checked directory can otherwise be renamed and replaced by a junction.
``DirectoryGuard`` keeps a non-delete-sharing directory handle open for the
duration of a critical operation and proves that the pathname still denotes
the same non-reparse directory before each mutation.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO


_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SEP = chr(92)


class FilesystemBoundaryError(RuntimeError):
    """A trusted local filesystem boundary changed or contains a link."""


def _identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _normalized_final(path: str | Path) -> str:
    value = os.path.normcase(os.path.abspath(os.fspath(path)))
    extended = _SEP * 2 + "?" + _SEP
    if value.startswith(extended + "UNC" + _SEP):
        value = _SEP * 2 + value[8:]
    elif value.startswith(extended):
        value = value[4:]
    return value.rstrip(_SEP + "/")


def _windows_final_path(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    needed = function(handle, None, 0, 0)
    if not needed:
        raise OSError(ctypes.get_last_error(), "cannot resolve filesystem handle")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = function(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise OSError(ctypes.get_last_error(), "cannot resolve filesystem handle")
    return _normalized_final(buffer.value)


class DirectoryGuard:
    """Hold and revalidate one existing, absolute, non-reparse directory."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise FilesystemBoundaryError("guarded directory must be absolute")
        self.path = path.absolute()
        if not self.path.is_dir() or is_reparse(self.path):
            raise FilesystemBoundaryError("guarded directory is unavailable or redirected")
        self.identity = _identity(self.path)
        self.fd: int | None = None
        self._win_handle: int | None = None
        try:
            if os.name == "nt":
                self._win_handle = self._open_windows_directory(self.path)
                self.final_path = _windows_final_path(self._win_handle)
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                self.fd = os.open(self.path, flags)
                self.final_path = _normalized_final(self.path.resolve(strict=True))
            self.assert_unchanged()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _open_windows_directory(path: Path) -> int:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        file_read_attributes = 0x0080
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        open_existing = 3
        backup_semantics = 0x02000000
        open_reparse_point = 0x00200000
        handle = create(
            str(path),
            file_read_attributes,
            file_share_read | file_share_write,
            None,
            open_existing,
            backup_semantics | open_reparse_point,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "cannot guard directory")
        return int(handle)

    def assert_unchanged(self) -> None:
        try:
            if not self.path.is_dir() or is_reparse(self.path):
                raise FilesystemBoundaryError("guarded directory was redirected")
            if _identity(self.path) != self.identity:
                raise FilesystemBoundaryError("guarded directory identity changed")
            resolved = _normalized_final(self.path.resolve(strict=True))
            if resolved != self.final_path:
                raise FilesystemBoundaryError("guarded directory target changed")
        except FilesystemBoundaryError:
            raise
        except OSError as exc:
            raise FilesystemBoundaryError("guarded directory became unavailable") from exc

    def contains_final(self, candidate: str | Path) -> bool:
        candidate_key = _normalized_final(candidate)
        try:
            return os.path.commonpath((self.final_path, candidate_key)) == self.final_path
        except ValueError:
            return False

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None
        if self._win_handle is not None:
            import ctypes

            handle, self._win_handle = self._win_handle, None
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)

    def __enter__(self) -> "DirectoryGuard":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def assert_safe_descendant(
    guard: DirectoryGuard, candidate: Path, *, allow_missing_leaf: bool = False
) -> Path:
    """Prove the existing candidate chain is below the unchanged guard."""

    guard.assert_unchanged()
    absolute = candidate.absolute()
    try:
        relative = absolute.relative_to(guard.path)
    except ValueError as exc:
        raise FilesystemBoundaryError("candidate escaped its guarded root") from exc
    current = guard.path
    parts = relative.parts[:-1] if allow_missing_leaf and not os.path.lexists(absolute) else relative.parts
    for index, part in enumerate(parts):
        current = current / part
        if not os.path.lexists(current):
            if allow_missing_leaf and index == len(parts) - 1:
                break
            raise FilesystemBoundaryError("candidate parent is absent")
        if is_reparse(current):
            raise FilesystemBoundaryError("candidate contains a reparse point")
        if current != absolute and not current.is_dir():
            raise FilesystemBoundaryError("candidate parent is not a directory")
    if os.path.lexists(absolute):
        resolved = _normalized_final(absolute.resolve(strict=True))
        if not guard.contains_final(resolved):
            raise FilesystemBoundaryError("candidate resolved outside its guarded root")
    guard.assert_unchanged()
    return absolute


def make_directories(guard: DirectoryGuard, directory: Path) -> Path:
    """Create missing descendants one component at a time and revalidate each."""

    absolute = directory.absolute()
    try:
        relative = absolute.relative_to(guard.path)
    except ValueError as exc:
        raise FilesystemBoundaryError("directory escaped its guarded root") from exc
    current = guard.path
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            current.mkdir()
        if not current.is_dir() or is_reparse(current):
            raise FilesystemBoundaryError("created directory was redirected")
        assert_safe_descendant(guard, current)
    return absolute


def open_exclusive_binary(guard: DirectoryGuard, target: Path) -> BinaryIO:
    """Create an empty file below a guarded parent and verify before writing."""

    absolute = target.absolute()
    assert_safe_descendant(guard, absolute, allow_missing_leaf=True)
    parent = absolute.parent
    with DirectoryGuard(parent) as parent_guard:
        guard.assert_unchanged()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            if parent_guard.fd is not None and os.open in os.supports_dir_fd:
                fd = os.open(absolute.name, flags, 0o600, dir_fd=parent_guard.fd)
            else:
                fd = os.open(absolute, flags, 0o600)
        except OSError as exc:
            raise FilesystemBoundaryError("exclusive file creation failed") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise FilesystemBoundaryError("created payload is not a regular file")
            if os.name == "nt":
                import msvcrt

                final_path = _windows_final_path(msvcrt.get_osfhandle(fd))
            else:
                descriptor_path = Path("/proc/self/fd") / str(fd)
                final_path = _normalized_final(
                    descriptor_path.resolve(strict=True) if descriptor_path.exists() else absolute.resolve(strict=True)
                )
            if not guard.contains_final(final_path) or not parent_guard.contains_final(final_path):
                raise FilesystemBoundaryError("created payload resolved outside its guarded root")
            guard.assert_unchanged()
            parent_guard.assert_unchanged()
            return os.fdopen(fd, "wb", closefd=True)
        except BaseException:
            os.close(fd)
            raise


__all__ = [
    "DirectoryGuard",
    "FilesystemBoundaryError",
    "assert_safe_descendant",
    "is_reparse",
    "make_directories",
    "open_exclusive_binary",
]
