"""Windows read leases prevent admitted model files from changing during use."""
import os
from contextlib import ExitStack

from .fs_safety import DirectoryGuard, FilesystemBoundaryError, is_reparse, _windows_final_path, _normalized_final


class AssetLease:
    def __init__(self, paths):
        self.paths = tuple(paths)
        self.stack = ExitStack()

    def __enter__(self):
        if os.name != "nt":
            raise FilesystemBoundaryError("The fixed inference asset lease requires Windows.")
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        try:
            for parent in sorted({parent for path in self.paths for parent in path.parents}, key=str):
                self.stack.enter_context(DirectoryGuard(parent))
            for path in self.paths:
                if not path.is_file() or is_reparse(path):
                    raise FilesystemBoundaryError("A leased asset is unavailable or redirected.")
                # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
                # Existing writable handles also prevent admission.
                handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise FilesystemBoundaryError("An asset is being modified or cannot be leased.")
                self.stack.callback(close, handle)
                if _windows_final_path(handle) != _normalized_final(path):
                    raise FilesystemBoundaryError("A leased asset target changed.")
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)
