"""Single-owner product host for settings, preview, generation and recovery.

Only this process sees local paths. The desktop invokes it over a private pipe.
Persistent categories contain JSON; transient recovery work and WAVs are excluded
from backup. Restored history is re-admitted by the existing journal/WAV verifier.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .application import ApplicationService, PersistedSettings, StalePreviewError, DEMO_SILENCE_ONLY
from .backup_restore import (_DataRootOwner, _atomic_runtime_json, _create_backup_artifact_owned,
                             _reconcile_restore_owned, _restore_backup_result_owned)
from .cli import _safe_directory, demo_token_counter, MAX_INPUT_CHARACTERS
from .filename_templates import FilenameContext, DEFAULT_TEMPLATE
from .fs_safety import DirectoryGuard, assert_safe_descendant
from .local_runtime import _read, _object, MAX_CONFIG_BYTES, load_local_runtime
from .planning import DictionaryRule, DictionarySnapshot, PausePolicy, PlanSettings
from .tokenizer_runtime import digest_object, load_tokenizer


class WorkbenchError(ValueError):
    pass


def write_private(path, value):
    with DirectoryGuard(path.parent) as guard:
        assert_safe_descendant(guard, path, allow_missing_leaf=True)
        _atomic_runtime_json(path, value, guard)


def read_private(path, limit=MAX_CONFIG_BYTES):
    return _object(_read(path, limit=limit))


class Workbench:
    def __init__(self, root: Path):
        self.root = _safe_directory(str(root), label="application data")
        self.config_path = self.root / "settings" / "app.json"
        self.dictionary_path = self.root / "dictionary" / "rules.json"
        self.snapshot_path = self.root / "history" / "completed.json"
        self.state_path = self.root / "active-recovery.json"
        self.ticket_path = self.root / "preview.json"

    @contextmanager
    def owned(self):
        with _DataRootOwner(self.root):
            result = _reconcile_restore_owned(self.root)
            if result.terminal_state.endswith(("_PENDING", "_UNVERIFIED")):
                raise WorkbenchError("Restore recovery requires attention.")
            for name in ("settings", "dictionary", "history", "work"):
                _safe_directory(str(self.root / name), label="application category")
            yield

    def configuration(self):
        return read_private(self.config_path) if self.config_path.exists() else None

    def configure(self, values, rules):
        required = {"output_directory", "tokenizer_config", "runtime_config", "title", "template", "pause_seconds"}
        if type(values) is not dict or set(values) != required:
            raise WorkbenchError("Settings fields are invalid.")
        if any(type(v) is not str for v in values.values()):
            raise WorkbenchError("Settings must contain text values.")
        if len(values["title"]) > 80 or len(values["template"]) > 256:
            raise WorkbenchError("The title or template is too long.")
        if PausePolicy(values["pause_seconds"], 48000).seconds > 10:
            raise WorkbenchError("Use at most 10 seconds between chunks.")
        if type(rules) is not list or len(rules) > 256:
            raise WorkbenchError("Dictionary size is unsupported.")
        dictionary = DictionarySnapshot(0, tuple(DictionaryRule(**r) for r in rules))
        if any(len(r.surface) > 128 or len(r.reading) > 256 for r in dictionary.rules):
            raise WorkbenchError("Dictionary entries exceed the supported size.")
        output = _safe_directory(values["output_directory"], label="output")
        if self.root == output or self.root in output.parents or output in self.root.parents:
            raise WorkbenchError("Output and application data must be separate.")
        with self.owned():
            previous = self.configuration()
            if previous and previous["output_directory"] != str(output) and self.snapshot_path.exists():
                if read_private(self.snapshot_path, 8 * 1024**2).get("runs"):
                    raise WorkbenchError("Existing history belongs to this output folder. Use a separate data folder for another destination.")
            saved = dict(values, output_directory=str(output))
            # Validate the filename before committing either settings record.
            service = self._service(saved, dictionary, self._new_context(saved), demo=True)
            service.preview("setup")
            write_private(self.dictionary_path, {"revision": 0, "rules": [asdict(r) for r in dictionary.rules]})
            write_private(self.config_path, saved)
        return {"configured": True, "output_directory": str(output)}

    def _dictionary(self):
        if not self.dictionary_path.exists():
            return DictionarySnapshot(0)
        value = read_private(self.dictionary_path)
        return DictionarySnapshot(value["revision"], tuple(DictionaryRule(**r) for r in value["rules"]))

    @staticmethod
    def _new_context(config):
        now = datetime.now().astimezone()
        return FilenameContext(now.strftime("%Y%m%d"), now.strftime("%H%M%S"),
                               title=config["title"], voice="neutral", quality="balanced",
                               reading="standard", job_id_short=uuid.uuid4().hex[:8])

    def _recovery_root(self):
        state = read_private(self.state_path) if self.state_path.exists() else None
        if state is None or state.get("state") == "restoring":
            name = uuid.uuid4().hex
            recovery = _safe_directory(str(self.root / "work" / name), label="recovery workspace")
            # Retain the previous workspace. No WAV or interrupted work is deleted.
            if state is not None and self.snapshot_path.exists():
                snapshot = read_private(self.snapshot_path, 8 * 1024**2)
                if set(snapshot) != {"schema_version", "runs"} or snapshot["schema_version"] != "neutral-history-snapshot-v1":
                    raise WorkbenchError("History snapshot schema is invalid.")
                runs = snapshot["runs"]
                if type(runs) is not list or len(runs) > 128:
                    raise WorkbenchError("History snapshot is oversized.")
                seen = set()
                for run in runs:
                    if type(run) is not dict or set(run) != {"run_id", "journal", "record"}:
                        raise WorkbenchError("History snapshot record is invalid.")
                    rid = run["run_id"]
                    if not isinstance(rid, str) or not re.fullmatch(r"app-[0-9a-f]{32}", rid) or rid in seen:
                        raise WorkbenchError("History snapshot identity is invalid.")
                    seen.add(rid)
                    for category, key in (("journals", "journal"), ("history", "record")):
                        folder = _safe_directory(str(recovery / category), label="restored history")
                        from .resilience import _atomic_json
                        with DirectoryGuard(folder) as guard:
                            _atomic_json(folder / (rid + ".json"), run[key], guard)
            write_private(self.state_path, {"state": "active", "directory": name})
            return recovery
        if set(state) != {"state", "directory"} or state["state"] != "active" or not re.fullmatch(r"[0-9a-f]{32}", state["directory"]):
            raise WorkbenchError("Recovery selection is invalid.")
        return _safe_directory(str(self.root / "work" / state["directory"]), label="recovery workspace")

    def _service(self, config, dictionary, context, *, binding=None, demo=False, counter=None):
        recovery = self._recovery_root()
        identity = "demo-tokenizer-v1" if demo else binding.tokenizer_identity
        settings = PersistedSettings(
            template=config["template"], filename_context=context,
            plan_settings=PlanSettings(), pause_policy=PausePolicy(config["pause_seconds"], 48000),
            dictionary=dictionary, tokenizer_identity=identity,
            output_directory=Path(config["output_directory"]), staging_directory=recovery,
            sample_rate_range=(48000, 48000), history_enabled=True,
            runtime_config_digest=digest_object({"config": config, "tokenizer": identity}),
            voice_rights_manifest_sha256=digest_object({"demo": True}) if demo else getattr(binding, "voice_rights_manifest_sha256", "0" * 64),
        )
        return ApplicationService(counter or (demo_token_counter if demo else binding.token_counter), settings,
                                  backend=DEMO_SILENCE_ONLY() if demo else getattr(binding, "backend", None))

    def _history_service(self, config):
        # History recovery does not load the tokenizer, inference code or weights.
        return self._service(config, self._dictionary(), self._new_context(config), demo=True)

    def _snapshot(self, service):
        records = service.history_records()
        recovery = self._recovery_root()
        runs = [{"run_id": r.run_id,
                 "journal": read_private(recovery / "journals" / (r.run_id + ".json")),
                 "record": read_private(recovery / "history" / (r.run_id + ".json"))} for r in records]
        if len(runs) > 128:
            raise WorkbenchError("This candidate supports backup of up to 128 verified history records.")
        write_private(self.snapshot_path, {"schema_version": "neutral-history-snapshot-v1", "runs": runs})
        return records

    @staticmethod
    def _text(text):
        if type(text) is not str or not text.strip() or len(text) > MAX_INPUT_CHARACTERS:
            raise WorkbenchError("Enter between 1 and 4096 characters.")

    def _bounded_dictionary(self, text):
        from .planning import _apply_dictionary, _normalize_text
        dictionary = self._dictionary()
        if len(_apply_dictionary(_normalize_text(text), dictionary)) > 16384:
            raise WorkbenchError("Dictionary expansion exceeds 16384 characters.")
        return dictionary

    def preview(self, text, *, demo=False):
        self._text(text)
        with self.owned():
            config = self.configuration()
            if config is None:
                raise WorkbenchError("Complete initial setup first.")
            binding = None if demo else load_tokenizer(Path(config["tokenizer_config"]))
            context = self._new_context(config)
            service = self._service(config, self._bounded_dictionary(text), context, binding=binding, demo=demo)
            service.startup()
            preview = service.preview(text)
            ticket = {"schema_version": "neutral-preview-ticket-v1", "context": asdict(context),
                      "demo": demo, "config": digest_object(config),
                      "tokenizer_config": None if demo else binding.config_digest,
                      "source": preview.source_digest, "settings": preview.settings_digest,
                      "roots": preview.root_identity_digest, "preview": preview.public_payload()}
            ticket["runtime_bytes"] = self._runtime_bytes(config) if not demo else None
            if not demo:
                from .environment_binding import environment_digest
                ticket["environment"] = environment_digest()
            write_private(self.ticket_path, ticket)
            return {"preview": preview.public_payload(), "demo": demo,
                    "destination": str(Path(config["output_directory"]) / preview.candidate_wav_name)}

    def generate(self, text, *, demo=False):
        self._text(text)
        with self.owned():
            config = self.configuration()
            if len(self._history_service(config).history_records()) >= 128:
                raise WorkbenchError("This candidate supports 128 completed records per data folder.")
            ticket = read_private(self.ticket_path)
            if ticket.get("schema_version") != "neutral-preview-ticket-v1" or ticket["demo"] != demo or ticket["config"] != digest_object(config):
                raise StalePreviewError("Preview again after changing settings.")
            preview_binding = None if demo else load_tokenizer(Path(config["tokenizer_config"]))
            if not demo and preview_binding.config_digest != ticket["tokenizer_config"]:
                raise StalePreviewError("Tokenizer configuration changed.")
            if not demo and self._runtime_bytes(config) != ticket.get("runtime_bytes"):
                raise StalePreviewError("Runtime configuration changed.")
            if not demo:
                from .environment_binding import environment_digest
                if environment_digest() != ticket.get("environment"):
                    raise StalePreviewError("Installed code or native dependencies changed.")
            context = FilenameContext(**ticket["context"])
            service = self._service(config, self._bounded_dictionary(text), context, binding=preview_binding, demo=demo)
            fresh = service.preview(text)
            if any((fresh.source_digest != ticket["source"], fresh.settings_digest != ticket["settings"],
                    fresh.root_identity_digest != ticket["roots"], fresh.public_payload() != ticket["preview"])):
                raise StalePreviewError("The input, dictionary, tokenizer or destination changed. Preview again.")
            if not demo:
                binding = load_local_runtime(Path(config["runtime_config"]))
                if binding.tokenizer_identity != preview_binding.tokenizer_identity:
                    raise StalePreviewError("Preview and generation tokenizers differ.")
                # Recount via the runtime binding before admission, preserving the
                # exact preview settings and then supplying the verified rights hash.
                from dataclasses import replace
                settings = replace(service._current_settings(),
                                   runtime_config_digest=binding.runtime_config_digest,
                                   voice_rights_manifest_sha256=binding.voice_rights_manifest_sha256)
                service = ApplicationService(binding.token_counter, settings, binding.backend)
                admitted = service.preview(text)
                if admitted.public_payload() != fresh.public_payload():
                    raise StalePreviewError("The real runtime plan differs from the preview.")
                fresh = admitted
            try:
                outcome = service.run(text, fresh)
            finally:
                self._snapshot(self._history_service(config))
            return {"outcome": outcome.public_payload(), "destination": str(Path(config["output_directory"]) / outcome.published_wav_name)}

    @staticmethod
    def _runtime_bytes(config):
        path = config["runtime_config"]
        return hashlib.sha256(_read(Path(path), limit=MAX_CONFIG_BYTES)).hexdigest() if path else None

    def startup(self):
        with self.owned():
            config = self.configuration()
            if config is None:
                return {"configured": False, "history": []}
            service = self._history_service(config)
            recovery = service.startup().public_payload()
            records = self._snapshot(service)
            return {"configured": True, "settings": config, "dictionary": [asdict(r) for r in self._dictionary().rules],
                    "recovery": recovery, "history": [r.public_payload() for r in records]}

    def play(self, run_id, playback=None):
        with self.owned():
            records = self._history_service(self.configuration()).history_records(run_id)
            if len(records) != 1:
                raise WorkbenchError("The selected WAV is missing or changed.")
            if playback is None:
                from .cli import WinsoundPlayback
                playback = WinsoundPlayback()
            playback.play(records[0])
            return {"played": True}

    def backup(self, archive):
        with self.owned():
            self._snapshot(self._history_service(self.configuration()))
            artifact = _create_backup_artifact_owned(self.root, Path(archive))
            return {"sha256": artifact.archive_sha256, "bytes": artifact.archive_size,
                    "excludes": ["output_wav", "external_assets", "transient_recovery", "preview", "logs"]}

    def restore(self, archive):
        with self.owned():
            from .backup_restore import _read_archive
            _manifest, contents = _read_archive(Path(archive))
            incoming = _object(contents.get("settings/app.json", b"{}"))
            current = self.configuration()
            if incoming.get("output_directory") != current["output_directory"]:
                raise WorkbenchError("Restore must retain the selected WAV destination; review a different destination in setup first.")
            self._snapshot(self._history_service(self.configuration()))
            # This marker is outside the backed-up categories. After a process
            # death, reconcile the restore first, then reconstruct from whichever
            # category snapshot was verified. Previous workspaces remain intact.
            write_private(self.state_path, {"state": "restoring"})
            result = _restore_backup_result_owned(Path(archive), self.root)
            if result.terminal_state.endswith(("_PENDING", "_UNVERIFIED")):
                raise WorkbenchError("Restore recovery requires attention.")
            service = self._history_service(self.configuration())
            recovery = service.startup().public_payload()
            records = self._snapshot(service)
            return {"state": result.terminal_state, "recovery": recovery,
                    "history": [r.public_payload() for r in records]}

    def lifecycle(self, operation, app_root, layout_root=None):
        from . import install_contract as install
        with self.owned():
            actions = {"install": install.install_layout, "update": install.upgrade_layout,
                       "rollback": install.rollback_upgrade, "uninstall": install.uninstall_owned}
            action = actions[operation]
            args = [Path(app_root), self.root]
            if operation in {"install", "update"}:
                args.insert(0, Path(layout_root))
            return action(*args)
