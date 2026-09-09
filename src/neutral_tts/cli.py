"""Standard-library CLI for preview, demo, and host-injected real runs."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .application import (
    ApplicationService,
    PersistedSettings,
    UnsupportedLifecycleIntegrationError,
)
from .demo_backend import DEMO_SILENCE_ONLY
from .filename_templates import DEFAULT_TEMPLATE, FilenameContext
from .estimation import Calibration, calibration_from_receipts, make_calibration_key
from .planning import PausePolicy, PlanSettings, normalize_public_id
from .playback import WinsoundPlayback


DEMO_TOKENIZER_IDENTITY = "demo-tokenizer-v1"


@dataclass(frozen=True, slots=True)
class RealRuntimeBinding:
    """Public-safe identities plus lazy host objects for one real run.

    A machine-local host reads and verifies the private runtime configuration,
    then returns this binding.  Constructing the binding must not create an
    inference runtime; only ``backend.generate`` may do that.
    """

    token_counter: Callable[[str], int]
    backend: object
    tokenizer_identity: str
    runtime_config_digest: str
    voice_rights_manifest_sha256: str
    device_class: str = "unknown-device"

    def __post_init__(self) -> None:
        if not callable(self.token_counter):
            raise TypeError("real token counter must be callable")
        object.__setattr__(
            self,
            "tokenizer_identity",
            normalize_public_id(
                self.tokenizer_identity, label="tokenizer identity"
            ),
        )
        object.__setattr__(
            self,
            "device_class",
            normalize_public_id(self.device_class, label="device class"),
        )
        for value in (
            self.runtime_config_digest,
            self.voice_rights_manifest_sha256,
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("real runtime binding digest is invalid")


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


def _safe_private_file(path_value: str, *, label: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).absolute()
    else:
        path = path.absolute()
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError(f"{label} is unavailable")
        for component in path.parents:
            item = component.lstat()
            if component.is_symlink() or bool(
                getattr(item, "st_file_attributes", 0) & 0x400
            ):
                raise ValueError(f"{label} contains a reparse point")
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    return path


def _text_argument(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    value = args.text
    if value is None:
        parser.error("--text or a positional text value is required")
    return value


def _build_service(
    args: argparse.Namespace,
    *,
    backend: object | None = None,
    token_counter: Callable[[str], int] = demo_token_counter,
    tokenizer_identity: str | None = None,
    history_enabled: bool = False,
    runtime_config_digest: str | None = None,
    voice_rights_manifest_sha256: str | None = None,
    device_class: str = "unknown-device",
    calibration: Calibration | None = None,
) -> ApplicationService:
    if any(
        value is not None
        for value in (
            args.restore_live_data_root,
            args.install_app_root,
            args.install_user_data_root,
        )
    ):
        raise UnsupportedLifecycleIntegrationError()
    output = _safe_directory(args.output_dir, label="output directory")
    staging = _safe_directory(args.staging_dir, label="staging directory")
    now = datetime.now().astimezone()
    context = FilenameContext(
        date=args.date or now.strftime("%Y%m%d"),
        time=args.time or now.strftime("%H%M%S"),
        title=args.title or "audio",
        voice="neutral" if history_enabled else "demo",
        quality="balanced",
        reading="standard",
        job_id_short=uuid.uuid4().hex[:8] if history_enabled else "demo",
        sequence=1,
    )
    settings = PersistedSettings(
        template=args.template,
        filename_context=context,
        plan_settings=PlanSettings(
            quality="balanced",
            reading="standard",
            max_tokens=256,
            max_characters=144,
        ),
        pause_policy=PausePolicy(args.boundary_pause_seconds, 48_000),
        tokenizer_identity=tokenizer_identity or args.tokenizer_identity,
        device_class=device_class,
        calibration=calibration,
        sample_rate_range=(48_000, 48_000) if history_enabled else (16_000, 48_000),
        output_directory=output,
        staging_directory=staging,
        history_enabled=history_enabled,
        runtime_config_digest=runtime_config_digest,
        voice_rights_manifest_sha256=voice_rights_manifest_sha256,
    )
    return ApplicationService(token_counter, settings, backend=backend)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neutral-tts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def operational_arguments(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--template", default=DEFAULT_TEMPLATE)
        sub.add_argument("--title", default="audio")
        sub.add_argument("--date")
        sub.add_argument("--time")
        sub.add_argument("--tokenizer-identity", default=DEMO_TOKENIZER_IDENTITY)
        sub.add_argument("--boundary-pause-seconds", default="0")
        sub.add_argument("--output-dir", default="neutral-tts-output")
        sub.add_argument("--staging-dir", default="neutral-tts-staging")
        lifecycle_help = (
            "reserved lifecycle setting; rejected by this application host"
        )
        sub.add_argument("--restore-live-data-root", help=lifecycle_help)
        sub.add_argument("--install-app-root", help=lifecycle_help)
        sub.add_argument("--install-user-data-root", help=lifecycle_help)

    for command in ("preview", "demo-run", "real-run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("text", nargs="?", help=argparse.SUPPRESS)
        sub.add_argument(
            "--text",
            dest="text_option",
            help="source text (never emitted in public JSON)",
        )
        operational_arguments(sub)
        if command == "real-run":
            sub.add_argument("--runtime-config", required=True)
    for command in ("startup", "history", "play"):
        sub = subparsers.add_parser(command)
        operational_arguments(sub)
        if command == "startup":
            sub.add_argument("--runtime-config", required=True)
        if command in {"history", "play"}:
            sub.add_argument("--run-id", required=command == "play")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    real_runtime_factory: Callable[[Path], RealRuntimeBinding] | None = None,
    playback: WinsoundPlayback | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    # argparse cannot use the same dest for positional and optional values;
    # combine them privately, with the explicit flag taking precedence.
    if args.command in {"preview", "demo-run", "real-run"}:
        args.text = args.text_option if args.text_option is not None else args.text
    try:
        source_text = (
            _text_argument(parser, args)
            if args.command in {"preview", "demo-run", "real-run"}
            else None
        )
        if args.command == "preview":
            service = _build_service(args)
            startup = service.startup()
            assert source_text is not None
            preview = service.preview(source_text)
            payload = {
                "command": "preview",
                "status": "READY",
                "startup_recovery": startup.public_payload(),
                "preview": preview.public_payload(),
            }
        elif args.command == "demo-run":
            service = _build_service(args, backend=DEMO_SILENCE_ONLY())
            assert source_text is not None
            preview = service.preview(source_text)
            outcome = service.run(source_text, preview)
            display = preview.estimate.coarse_display_payload()
            payload = {
                "command": "demo-run",
                "status": "SUCCEEDED",
                "preflight": {
                    "filename": preview.public_payload()["filename"],
                    "chunks": preview.plan.chunk_count,
                    "duration_seconds": display["duration_seconds"],
                    "storage_mib": display["storage_mib"],
                    "staging_mib": display["staging_mib"],
                },
                "outcome": outcome.public_payload(),
                "demo": {
                    "mode": "DEMO_SILENCE_ONLY",
                    "speech": False,
                    "audio_kind": "silence",
                    "tokenizer_identity": args.tokenizer_identity,
                },
            }
        elif args.command == "real-run":
            if real_runtime_factory is None:
                raise RuntimeError("a machine-local runtime host is required")
            runtime_config = _safe_private_file(
                args.runtime_config, label="runtime configuration"
            )
            binding = real_runtime_factory(runtime_config)
            if not isinstance(binding, RealRuntimeBinding):
                raise TypeError("runtime host returned an invalid binding")
            history_reader = _build_service(
                args,
                token_counter=binding.token_counter,
                tokenizer_identity=binding.tokenizer_identity,
                history_enabled=True,
                runtime_config_digest=binding.runtime_config_digest,
                voice_rights_manifest_sha256=(
                    binding.voice_rights_manifest_sha256
                ),
                device_class=binding.device_class,
            )
            verified_history = tuple(
                record
                for record in history_reader.history_records()
                if (
                    record.runtime_config_digest == binding.runtime_config_digest
                    and record.voice_rights_manifest_sha256
                    == binding.voice_rights_manifest_sha256
                )
            )
            calibration_key = make_calibration_key(
                binding.device_class,
                "balanced",
                "standard",
                binding.tokenizer_identity,
            )
            calibration = calibration_from_receipts(
                calibration_key,
                [record.receipt for record in verified_history],
            )
            service = _build_service(
                args,
                backend=binding.backend,
                token_counter=binding.token_counter,
                tokenizer_identity=binding.tokenizer_identity,
                history_enabled=True,
                runtime_config_digest=binding.runtime_config_digest,
                voice_rights_manifest_sha256=(
                    binding.voice_rights_manifest_sha256
                ),
                device_class=binding.device_class,
                calibration=calibration,
            )
            assert source_text is not None
            preview = service.preview(source_text)
            outcome = service.run(source_text, preview)
            display = preview.estimate.coarse_display_payload()
            payload = {
                "command": "real-run",
                "status": "SUCCEEDED",
                "preflight": {
                    "filename": preview.public_payload()["filename"],
                    "chunks": preview.plan.chunk_count,
                    "duration_seconds": display["duration_seconds"],
                    "storage_mib": display["storage_mib"],
                    "staging_mib": display["staging_mib"],
                },
                "outcome": outcome.public_payload(),
            }
        else:
            if args.command == "startup":
                _safe_private_file(args.runtime_config, label="runtime configuration")
            service = _build_service(args, history_enabled=True)
            if args.command == "startup":
                payload = {
                    "command": "startup",
                    "status": "SUCCEEDED",
                    "startup_recovery": service.startup().public_payload(),
                }
            else:
                records = service.history_records(args.run_id)
                if args.command == "play":
                    if len(records) != 1:
                        raise RuntimeError("verified history record is unavailable")
                    (playback or WinsoundPlayback()).play(records[0])
                payload = {
                    "command": args.command,
                    "status": "SUCCEEDED",
                    "records": [record.public_payload() for record in records],
                }
                if args.command == "play":
                    payload["played"] = True
        print(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except UnsupportedLifecycleIntegrationError as exc:
        payload = {
            "command": args.command,
            "status": "ERROR",
            "error": {
                "code": "UNSUPPORTED_LIFECYCLE_INTEGRATION",
                "message": str(exc),
            },
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 2
    except Exception:
        # Exception text is deliberately discarded.  Adapter and OS errors
        # can contain arbitrary source text, credentials, or absolute paths.
        payload = {
            "command": args.command,
            "status": "ERROR",
            "error": {
                "code": "WORKFLOW_REJECTED",
                "message": "The request could not be completed.",
            },
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "DEMO_TOKENIZER_IDENTITY",
    "RealRuntimeBinding",
    "demo_token_counter",
    "main",
]
