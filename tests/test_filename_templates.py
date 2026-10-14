from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from neutral_tts.filename_templates import (
    DEFAULT_TEMPLATE,
    FilenameCollisionError,
    FilenameContext,
    FilenameTemplateError,
    _validate_wav_basename,
    choose_collision_target,
    canonical_collision_identity,
    preview_filename,
    publish_staged_wav,
    render_template,
    validate_template,
)


def context(**overrides):
    values = dict(date="20260901", time="120000", title="Title", voice="Voice", quality="balanced", reading="standard", job_id_short="abcdef12", sequence=1)
    values.update(overrides)
    return FilenameContext(**values)


def write_pcm16_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 8)


class FilenameTemplateTests(unittest.TestCase):
    def test_fixed_allowlist_and_default_have_no_voice_identifier(self):
        for name in ("date", "time", "title", "voice", "quality", "reading", "job_id_short", "sequence"):
            self.assertIn("{" + name + "}", validate_template("x-{" + name + "}"))
        self.assertNotIn("voice", DEFAULT_TEMPLATE)

    def test_rejects_unknown_positional_nested_conversion_spec_and_extension(self):
        for value in ("{other}", "{0}", "{title.name}", "{title[0]}", "{title!r}", "{title:>4}", "{title}.wav", "x.mp3", "x/y", r"x\y", "C:x", "../x", "x:ads", "\x01{x}", "\ud800{x}"):
            with self.subTest(value=value), self.assertRaises(FilenameTemplateError):
                validate_template(value)

    def test_preview_is_nfc_normalized_and_sequence_is_stable(self):
        first = preview_filename("{title}-{sequence}", context(title="e\u0301", sequence=2))
        second = preview_filename("{title}-{sequence}", context(title="é", sequence=2))
        self.assertEqual(first.wav_name, "é-002.wav")
        self.assertEqual(first.collision_identity, second.collision_identity)

    def test_values_are_safely_sanitized_and_reserved_names_do_not_escape(self):
        rendered = render_template("{title}", context(title='CON:/bad.. name. '))
        self.assertNotIn("/", rendered)
        self.assertNotIn(":", rendered)
        self.assertFalse(rendered.endswith((" ", ".")))
        self.assertNotEqual(rendered.casefold(), "con")
        for title in ("", ".", "..", "NUL", "COM1.txt", "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"):
            rendered = render_template("{title}", context(title=title))
            self.assertTrue(rendered)
            self.assertNotIn("..", rendered)
            self.assertNotIn(".", rendered)
        self.assertEqual(render_template("{title}", context(title="chapter.mp3")), "chapter_mp3")

    def test_composed_device_names_are_neutralized_before_publication(self):
        cases = (
            ("nu{title}", {"title": "l"}),
            ("co{title}", {"title": "n"}),
            ("{title}{voice}", {"title": "LP", "voice": "T1"}),
            ("{title}", {"title": "CONIN$"}),
            ("{title}", {"title": "conout$"}),
        )
        for template, values in cases:
            preview = preview_filename(template, context(**values))
            self.assertTrue(preview.base_name.startswith("_"), preview.wav_name)
            # The previewed name must be one that publication accepts.
            self.assertEqual(_validate_wav_basename(preview.wav_name), preview.wav_name)

    def test_component_and_path_limits_are_enforced(self):
        with self.assertRaises(FilenameTemplateError):
            render_template("{title}", context(title="あ" * 121))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            preview = preview_filename("{title}", context(title="ok"))
            self.assertEqual(choose_collision_target(directory, preview).name, "ok.wav")

    def test_collision_suffixes_are_deterministic_and_case_unicode_aware(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "é.wav").write_bytes(b"keep")
            (directory / "name-002.wav").write_bytes(b"keep2")
            preview = preview_filename("{title}", context(title="e\u0301"))
            # The first canonical name is occupied; -002 is occupied, then -003.
            self.assertEqual(choose_collision_target(directory, preview).name, "é-002.wav")
            name_preview = preview_filename("name", context())
            self.assertEqual(choose_collision_target(directory, name_preview).name, "name.wav")

    def test_sequence_collision_starts_from_persisted_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            preview = preview_filename("item-{sequence}", context(sequence=7))
            self.assertEqual(choose_collision_target(directory, preview).name, "item-007.wav")
            (directory / "ITEM-007.WAV").write_bytes(b"occupied")
            self.assertEqual(choose_collision_target(directory, preview).name, "item-008.wav")

    def test_publish_is_no_replace_and_preserves_staged_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged.wav"
            output = root / "output"
            output.mkdir()
            write_pcm16_wav(staged)
            destination = output / "output.wav"
            self.assertEqual(publish_staged_wav(staged, output, "output.wav"), destination)
            staged_bytes = staged.read_bytes()
            self.assertEqual(destination.read_bytes(), staged_bytes)
            with self.assertRaises(FilenameCollisionError):
                publish_staged_wav(staged, output, "OUTPUT.WAV")
            self.assertEqual(destination.read_bytes(), staged_bytes)

    def test_publish_rejects_arbitrary_destination_and_unsafe_basename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged.wav"
            output = root / "output"
            output.mkdir()
            write_pcm16_wav(staged)
            with self.assertRaises(TypeError):
                publish_staged_wav(staged, output / "legacy.wav")
            unc_name = chr(92) * 2 + "server" + chr(92) + "share.wav"
            for name in ("../escape.wav", r"..\escape.wav", "bad.txt", "foo:bar.wav", "C:bad.wav", unc_name, "two.part.wav", "CON.wav", "COM¹.wav", "LPT³.wav"):
                with self.subTest(name=name), self.assertRaises(FilenameTemplateError):
                    publish_staged_wav(staged, output, name)
            self.assertFalse((root / "escape.wav").exists())

    def test_failed_atomic_publication_never_deletes_or_creates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged.wav"
            output = root / "output"
            output.mkdir()
            write_pcm16_wav(staged)
            with mock.patch("neutral_tts.filename_templates.os.link", side_effect=OSError("unavailable")):
                with self.assertRaises(FilenameTemplateError):
                    publish_staged_wav(staged, output, "result.wav")
            self.assertTrue(staged.read_bytes().startswith(b"RIFF"))
            self.assertFalse((output / "result.wav").exists())

    def test_publish_rejects_non_wav_and_non_pcm16_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            staged = root / "not-a-wave.tmp"
            staged.write_bytes(b"plain text")
            with self.assertRaises(FilenameTemplateError):
                publish_staged_wav(staged, output, "result.wav")
            with wave.open(str(staged), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 8)
            with self.assertRaises(FilenameTemplateError):
                publish_staged_wav(staged, output, "result.wav")

    def test_symlinked_staging_or_output_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged.wav"
            output = root / "output"
            write_pcm16_wav(staged)
            output.mkdir()
            linked_file = root / "linked.wav"
            linked_output = root / "linked-output"
            try:
                linked_file.symlink_to(staged)
                linked_output.symlink_to(output, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(FilenameTemplateError):
                publish_staged_wav(linked_file, output, "result.wav")
            with self.assertRaises(FilenameTemplateError):
                publish_staged_wav(staged, linked_output, "result.wav")


if __name__ == "__main__":
    unittest.main()
