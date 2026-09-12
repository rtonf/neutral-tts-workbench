from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import runpy
import sys
import tempfile
import unittest
import warnings
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from neutral_tts.cli import main
from neutral_tts.local_runtime import LocalRuntimeConfig, LocalRuntimeError, load_local_runtime
from tests.test_irodori_backend import FakeRuntimeTokenizer, FakeTensor, FakeTokenizer


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LocalRuntimeTests(unittest.TestCase):
    def fixture(self, root):
        assets = root / "assets"
        assets.mkdir()
        inventory = []
        for relative, role in (
            ("tts/model.safetensors", "model"), ("codec/weights.pth", "codec"),
            ("tts/tokenizer/tokenizer.json", "tokenizer"),
            ("tts/tokenizer/tokenizer_config.json", "tokenizer"),
            *(("watermark/" + name, "watermark") for name in
              ("hparams.yaml", "enc_c.ckpt", "dec_c.ckpt", "dec_m_0.ckpt")),
        ):
            target = assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = b"synthetic fixture; never deserialized"
            target.write_bytes(data)
            inventory.append({"path": relative, "role": role, "size": len(data), "sha256": sha(data)})
        reference = root / "reference.wav"
        with wave.open(str(reference), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b"\x01\x00" * 480)
        references = [{"path": str(reference), "sha256": sha(reference.read_bytes())}]
        evidence = root / "evidence.txt"
        evidence.write_bytes(b"synthetic fixture only")
        rights = {
            "schema_version": "neutral-local-rights-v1",
            "asset_inventory_sha256": sha(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()),
            "references": references,
            **{scope: {"reviewed": True, "evidence_sha256": sha(b"synthetic fixture only"),
                       "evidence_path": str(evidence), "reviewed_use": scope, "unresolved": []}
               for scope in ("local_execution", "recording_use", "voice_replication", "generated_audio_use")},
        }
        rights_path = root / "rights.json"
        rights_path.write_text(json.dumps(rights), encoding="utf-8")
        config = {
            "schema_version": "neutral-local-runtime-v1", "asset_root": str(assets),
            "assets": inventory, "model": "tts/model.safetensors", "codec": "codec/weights.pth",
            "tokenizer": "tts/tokenizer", "watermark": "watermark", "device": "cpu",
            "precision": "fp32", "vocab_size": 1024, "bos_token_id": 1,
            "rights_manifest": str(rights_path), "rights_manifest_sha256": sha(rights_path.read_bytes()),
            "references": references,
        }
        config_path = root / "runtime.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path, config, rights_path, rights

    def fake_packages(self, events, *, fail=False, ready=True):
        class Watermarker:
            def __init__(self, *, device):
                self.model = self._load_backend(device=device, model_type="44.1k")

            @property
            def ready(self):
                return ready and self.model is not None

        def watermark(**kwargs):
            events.append(("watermark", kwargs))
            return object()

        def synthesize(request, *, log_fn):
            events.append(("synthesize", request))
            if fail:
                print("private sampler data")
                raise RuntimeError("private sampler failure")
            audio = FakeTensor([[0.25, -0.25] * 20])
            return SimpleNamespace(audio=audio, audios=[audio], sample_rate=48000)

        def from_key(key):
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"], "1")
            events.append(("load", key))
            return SimpleNamespace(
                tokenizer=FakeRuntimeTokenizer(), watermarker=engine.SilentCipherWatermarker(device=key.codec_device),
                synthesize=synthesize, unload=lambda: events.append(("unload", None)),
            )

        engine = SimpleNamespace(
            SilentCipherWatermarker=Watermarker, RuntimeKey=SimpleNamespace,
            SamplingRequest=SimpleNamespace, InferenceRuntime=SimpleNamespace(from_key=from_key),
        )

        def tokenizer(*args, **kwargs):
            self.assertEqual(kwargs["local_files_only"], True)
            self.assertEqual(kwargs["trust_remote_code"], False)
            events.append(("tokenizer", None))
            return FakeTokenizer()

        packages = {
            "irodori_tts.inference_runtime": engine,
            "silentcipher": SimpleNamespace(get_model=watermark),
            "transformers": SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer)),
        }
        return lambda name: packages[name]

    def test_binding_is_lazy_and_checks_bytes_without_importing_runtime(self):
        with tempfile.TemporaryDirectory() as name:
            path, *_ = self.fixture(Path(name))
            with patch("importlib.import_module", side_effect=AssertionError("unexpected import")):
                binding = load_local_runtime(path)
            from neutral_tts.environment_binding import environment_digest
            self.assertEqual(binding.runtime_config_digest, sha((sha(path.read_bytes()) + environment_digest()).encode()))

    def test_module_entrypoint_generates_with_the_default_host(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path, *_ = self.fixture(root)
            arguments = ["neutral-tts", "real-run", "--runtime-config", str(path),
                         "--text", "synthetic", "--output-dir", str(root / "out"),
                         "--staging-dir", str(root / "stage")]
            output = io.StringIO()
            with patch.object(sys, "argv", arguments), warnings.catch_warnings():
                # The test module has already imported cli to exercise its
                # callable entry point; runpy deliberately creates __main__.
                warnings.simplefilter("ignore", RuntimeWarning)
                with patch("importlib.import_module", side_effect=self.fake_packages([])):
                    with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                        runpy.run_module("neutral_tts.cli", run_name="__main__", alter_sys=True)
            self.assertEqual(exited.exception.code, 0, output.getvalue())
            self.assertEqual(len(list((root / "out").glob("*.wav"))), 1)

    def test_console_default_host_generates_then_history_restart_without_model_load(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path, _, _, _ = self.fixture(root)
            events = []
            common = ["--output-dir", str(root / "output"), "--staging-dir", str(root / "staging")]
            environment = dict(os.environ)
            disabled = logging.root.manager.disable
            output = io.StringIO()
            with patch("importlib.import_module", side_effect=self.fake_packages(events)):
                with redirect_stdout(output):
                    code = main(["real-run", "--runtime-config", str(path), "--text", "synthetic input", *common])
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "SUCCEEDED")
            self.assertNotIn(str(root), output.getvalue())
            self.assertNotIn("synthetic input", output.getvalue())
            self.assertEqual([e[0] for e in events].count("load"), 1)
            self.assertEqual(events[-1][0], "unload")
            self.assertEqual(dict(os.environ), environment)
            self.assertEqual(logging.root.manager.disable, disabled)
            saved = list((root / "output").glob("*.wav"))
            self.assertEqual(len(saved), 1)
            with patch("importlib.import_module", side_effect=AssertionError("unexpected reload")):
                for command in (["startup", "--runtime-config", str(path)], ["history"]):
                    capture = io.StringIO()
                    with redirect_stdout(capture):
                        self.assertEqual(main([*command, *common]), 0)
                    payload = json.loads(capture.getvalue())
                    if command == ["history"]:
                        self.assertEqual(len(payload["records"]), 1)

    def test_failure_or_unavailable_watermark_cleans_runtime_and_publishes_no_wav(self):
        for fail, ready in ((True, True), (False, False)):
            with self.subTest(fail=fail, ready=ready), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                path, *_ = self.fixture(root)
                events, out, err = [], io.StringIO(), io.StringIO()
                environment = dict(os.environ)
                with patch("importlib.import_module", side_effect=self.fake_packages(events, fail=fail, ready=ready)):
                    with redirect_stdout(out), redirect_stderr(err):
                        code = main(["real-run", "--runtime-config", str(path), "--text", "synthetic",
                                     "--output-dir", str(root / "out"), "--staging-dir", str(root / "stage")])
                self.assertEqual(code, 2)
                self.assertEqual(events[-1][0], "unload")
                self.assertEqual(list((root / "out").glob("*.wav")), [])
                self.assertNotIn("private", out.getvalue() + err.getvalue())
                self.assertEqual(dict(os.environ), environment)

    def test_changed_assets_reference_rights_and_config_reject_before_runtime_import(self):
        for kind in ("model", "reference", "rights", "config"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                path, config, rights_path, _ = self.fixture(root)
                binding = load_local_runtime(path)
                target = {"model": root / "assets" / config["model"], "reference": root / "reference.wav",
                          "rights": rights_path, "config": path}[kind]
                target.write_bytes(target.read_bytes() + b"changed")
                with patch("importlib.import_module", side_effect=AssertionError("unexpected import")):
                    with self.assertRaises((LocalRuntimeError, ValueError)):
                        binding.backend._host.runtime_resources.verify()

    def test_schema_path_escape_unknown_assets_missing_consent_and_bad_reference_rejected(self):
        mutations = (
            lambda config, rights, root: config.update(model="../outside.safetensors"),
            lambda config, rights, root: config.update(vocab_size=True),
            lambda config, rights, root: config.update(device="external-device"),
            lambda config, rights, root: config["assets"].append(dict(config["assets"][0])),
            lambda config, rights, root: rights["voice_replication"].update(reviewed=False),
            lambda config, rights, root: (root / "assets/tts/tokenizer/extra.py").write_text("synthetic"),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                path, config, rights_path, rights = self.fixture(root)
                mutation(config, rights, root)
                rights_path.write_text(json.dumps(rights), encoding="utf-8")
                config["rights_manifest_sha256"] = sha(rights_path.read_bytes())
                path.write_text(json.dumps(config), encoding="utf-8")
                with patch("importlib.import_module", side_effect=AssertionError("unexpected import")):
                    with self.assertRaises(LocalRuntimeError):
                        LocalRuntimeConfig(path)
