"""Small standard-library CLI for privacy-bounded preview and demo runs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import os
import stat
import sys

from .application import ApplicationService, PersistedSettings
from .demo_backend import DEMO_SILENCE_ONLY
from .filename_templates import DEFAULT_TEMPLATE, FilenameContext


DEMO_TOKENIZER_IDENTITY = "demo-tokenizer-v1"


def demo_token_counter(text: str) -> int:
    """Explicit deterministic demo tokenizer counter (one token per codepoint)."""

    return max(1, len(text))


def _safe_directory(path_value: str, *, label: str) -> Path:
    """Create a CLI directory and reject symlink/reparse-point components."""

    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.absolute()
    # Check the existing chain before creating anything.  The CLI only creates
    # the requested leaf, never a path that is redirected through a symlink.
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise ValueError(f"{label} is unavailable")
        cursor = parent
    try:
        for component in (cursor, *cursor.parents):
            metadata = component.lstat()
            if component.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400):
                raise ValueError(f"{label} contains a reparse point")
        if not cursor.is_dir():
            raise ValueError(f"{label} is unavailable")
        for item in reversed(missing):
            item.mkdir()
            metadata = item.lstat()
            if item.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400):
                raise ValueError(f"{label} contains a reparse point")
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    return path


def _configured_root(path_value: str | None, *, label: str) -> Path | None:
    """Normalize one explicitly supplied root without creating it."""

    if path_value is None:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.absolute()
    existing = path
    try:
        while not os.path.lexists(existing):
            parent = existing.parent
            if parent == existing:
                raise ValueError(f"{label} is unavailable")
            existing = parent
        if not existing.is_dir():
            raise ValueError(f"{label} is unavailable")
        for component in (existing, *existing.parents):
            metadata = component.lstat()
            if component.is_symlink() or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError(f"{label} contains a reparse point")
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    return path


def _text_argument(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    value = args.text
    if value is None:
        parser.error("--text or a positional text value is required")
    return value


def _build_service(args: argparse.Namespace, *, backend: object | None = None) -> ApplicationService:
    output = _safe_directory(args.output_dir, label="output directory")
    staging = _safe_directory(args.staging_dir, label="staging directory")
    now = datetime.now().astimezone()
    context = FilenameContext(
        date=args.date or now.strftime("%Y%m%d"),
        time=args.time or now.strftime("%H%M%S"),
        title=args.title or "audio",
        voice="demo",
        quality="balanced",
        reading="standard",
        job_id_short="demo",
        sequence=1,
    )
    settings = PersistedSettings(
        template=args.template,
        filename_context=context,
        tokenizer_identity=args.tokenizer_identity,
        output_directory=output,
        staging_directory=staging,
        restore_live_data_root=_configured_root(
            args.restore_live_data_root, label="restore live-data root"
        ),
        install_app_root=_configured_root(
            args.install_app_root, label="install application root"
        ),
        install_user_data_root=_configured_root(
            args.install_user_data_root, label="install user-data root"
        ),
    )
    return ApplicationService(demo_token_counter, settings, backend=backend)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neutral-tts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preview", "demo-run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("text", nargs="?", help=argparse.SUPPRESS)
        sub.add_argument("--text", dest="text_option", help="source text (never emitted in public JSON)")
        sub.add_argument("--template", default=DEFAULT_TEMPLATE)
        sub.add_argument("--title", default="audio")
        sub.add_argument("--date")
        sub.add_argument("--time")
        sub.add_argument("--tokenizer-identity", default=DEMO_TOKENIZER_IDENTITY)
        sub.add_argument("--output-dir", default="neutral-tts-output")
        sub.add_argument("--staging-dir", default="neutral-tts-staging")
        sub.add_argument("--restore-live-data-root")
        sub.add_argument("--install-app-root")
        sub.add_argument("--install-user-data-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    # argparse cannot use the same dest for positional and optional values;
    # combine them privately, with the explicit flag taking precedence.
    args.text = args.text_option if args.text_option is not None else args.text
    try:
        source_text = _text_argument(parser, args)
        if args.command == "preview":
            service = _build_service(args)
            startup = service.startup()
            preview = service.preview(source_text)
            payload = {
                "command": "preview",
                "status": "READY",
                "startup_recovery": startup.public_payload(),
                "preview": preview.public_payload(),
            }
        else:
            service = _build_service(args, backend=DEMO_SILENCE_ONLY())
            preview = service.preview(source_text)
            outcome = service.run(source_text, preview)
            payload = {
                "command": "demo-run",
                "status": "SUCCEEDED",
                "preflight": {
                    "filename": preview.public_payload()["filename"],
                    "chunks": preview.plan.chunk_count,
                    "duration_seconds": preview.estimate.coarse_display_payload()["duration_seconds"],
                    "storage_mib": preview.estimate.coarse_display_payload()["storage_mib"],
                    "staging_mib": preview.estimate.coarse_display_payload()["staging_mib"],
                },
                "outcome": outcome.public_payload(),
                "demo": {"mode": "DEMO_SILENCE_ONLY", "speech": False, "audio_kind": "silence", "tokenizer_identity": args.tokenizer_identity},
            }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        # Exception text is deliberately discarded.  Adapter and OS errors
        # can contain arbitrary source text, credentials, or absolute paths.
        payload = {
            "command": args.command,
            "status": "ERROR",
            "error": {"code": "WORKFLOW_REJECTED", "message": "The request could not be completed."},
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["DEMO_TOKENIZER_IDENTITY", "demo_token_counter", "main"]
