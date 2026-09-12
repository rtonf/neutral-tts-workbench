"""One private JSON request per dedicated process; no network listener or log."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .application import StalePreviewError
from .backup_restore import BackupRestoreBusy
from .workbench import Workbench, WorkbenchError


def dispatch(request):
    if type(request) is not dict or set(request) != {"operation", "data_root", "arguments"}:
        raise WorkbenchError("The operation request is invalid.")
    if request["operation"] == "setup_tokenizer":
        from .setup import acquire_tokenizer
        return acquire_tokenizer(request["data_root"], **request["arguments"])
    if request["operation"] == "setup_environment":
        from .setup import provision_tokenizer_environment
        return provision_tokenizer_environment(request["data_root"], **request["arguments"])
    if request["operation"] == "environment":
        from .setup import environment
        if request["arguments"]:
            raise WorkbenchError("Environment check does not accept options.")
        return environment()
    workbench = Workbench(Path(request["data_root"]))
    allowed = {"configure", "startup", "preview", "generate", "play", "backup", "restore", "lifecycle"}
    if request["operation"] not in allowed or type(request["arguments"]) is not dict:
        raise WorkbenchError("The operation is unsupported.")
    value = getattr(workbench, request["operation"])(**request["arguments"])
    return asdict(value) if is_dataclass(value) else value


def main():
    try:
        raw = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise WorkbenchError("The request is too large.")
        value = dispatch(json.loads(raw))
        result = {"status": "ok", "result": value}
    except Exception as error:
        code = ("PREVIEW_CHANGED" if isinstance(error, StalePreviewError) else
                "BUSY" if isinstance(error, BackupRestoreBusy) else "REQUEST_REJECTED")
        # Exception messages from engines and operating systems may include
        # private text/paths. Show a stable action guide instead.
        result = {"status": "error", "code": code}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True, default=str).encode())
    sys.stdout.buffer.flush()
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
