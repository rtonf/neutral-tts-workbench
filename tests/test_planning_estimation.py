from __future__ import annotations

import unittest

from neutral_tts.estimation import _calibration_from_samples, calibration_from_receipts, estimate_from_plan, make_calibration_key
from neutral_tts.planning import DictionaryRule, DictionarySnapshot, PausePolicy, PlanSettings, normalize_public_id, prepare_text_plan


class SpyCounter:
    def __init__(self):
        self.calls = []

    def __call__(self, value: str) -> int:
        self.calls.append(value)
        return len(value) + 1


class PlanningEstimationTests(unittest.TestCase):
    @staticmethod
    def key() -> str:
        return make_calibration_key("cpu", "balanced", "standard", "unspecified")

    def test_shared_plan_reuses_normalization_dictionary_token_chunk_and_pause_policy(self):
        counter = SpyCounter()
        dictionary = DictionarySnapshot(3, (DictionaryRule("AI", "エーアイ"),))
        plan = prepare_text_plan(
            "  AIです。\r\n次です。 ",
            counter,
            dictionary=dictionary,
            settings=PlanSettings(max_tokens=10, max_characters=20),
            pause_policy=PausePolicy("0.5", 8000),
            tokenizer_identity="tokenizer-test",
        )
        self.assertEqual(plan.source_characters, len("AIです。\n次です。"))
        self.assertIn("エーアイ", plan.synthesis_text)
        self.assertEqual(plan.sampler_call_count, plan.chunk_count)
        self.assertEqual(plan.boundary_count, max(plan.chunk_count - 1, 0))
        self.assertEqual(plan.pause_policy.samples, 4000)
        public = plan.public_payload()
        self.assertNotIn("AIです", repr(public))
        self.assertNotIn("エーアイ", repr(public))
        self.assertEqual(len(plan.plan_digest), 64)

    def test_estimate_has_no_sampler_and_reports_counts_audio_storage_staging(self):
        counter = SpyCounter()
        plan = prepare_text_plan("短い文章。次の文章。", counter, pause_policy=PausePolicy("1", 48000))
        estimate = estimate_from_plan(plan, sample_rate_range=(8000, 48000))
        self.assertEqual(estimate.confidence.state, "unknown")
        self.assertEqual(estimate.counts["tokens"], plan.total_tokens)
        self.assertEqual(estimate.counts["chunks"], plan.chunk_count)
        self.assertEqual(estimate.counts["boundaries"], plan.boundary_count)
        self.assertGreaterEqual(estimate.duration_seconds.minimum, 1.0)
        self.assertLess(estimate.wav_bytes.minimum, estimate.wav_bytes.maximum)
        self.assertGreater(estimate.staging_bytes.maximum, estimate.wav_bytes.maximum)
        self.assertIsNone(estimate.generation_seconds)

    def test_unknown_low_sample_and_calibrated_states(self):
        key = self.key()
        unknown = _calibration_from_samples(key, [])
        low = _calibration_from_samples(key, [{"status": "SUCCEEDED", "tokens": 10, "duration_seconds": 5, "generation_seconds": 20}])
        enough = _calibration_from_samples(key, [{"status": "SUCCEEDED", "tokens": 10, "duration_seconds": 5, "generation_seconds": 20}] * 5)
        self.assertEqual(unknown.state, "unknown")
        self.assertEqual(low.state, "low_sample")
        self.assertEqual(enough.state, "calibrated")
        self.assertGreaterEqual(low.duration_per_token.maximum, enough.duration_per_token.maximum)
        self.assertIsNotNone(enough.generation_seconds_per_token)

    def test_malformed_failed_and_mismatched_receipts_are_ignored(self):
        key = self.key()
        receipts = [
            {"schema_version": "neutral-tts-receipt-v1", "status": "SUCCEEDED", "calibration_key": key, "actual": {"counts": {"tokens": 10}, "audio": {"duration_seconds": 5}, "generation_seconds": 20}},
            {"schema_version": "neutral-tts-receipt-v1", "status": "FAILED", "calibration_key": key, "actual": {"counts": {"tokens": 10}, "audio": {"duration_seconds": 5}, "generation_seconds": 20}},
            {"schema_version": "neutral-tts-receipt-v1", "status": "SUCCEEDED", "calibration_key": make_calibration_key("other", "balanced", "standard", "unspecified"), "actual": {"counts": {"tokens": 99}, "audio": {"duration_seconds": 1}, "generation_seconds": 1}},
            object(),
        ]
        calibration = calibration_from_receipts(key, receipts)
        self.assertEqual(calibration.sample_count, 0)
        self.assertEqual(calibration.state, "unknown")

    def test_malformed_direct_samples_are_ignored_and_small_rates_stay_positive(self):
        key = self.key()
        samples = [None, "text", [], {"status": "SUCCEEDED", "tokens": 10, "duration_seconds": True}]
        self.assertEqual(_calibration_from_samples(key, samples).state, "unknown")
        tiny = _calibration_from_samples(key, [{"status": "SUCCEEDED", "tokens": 1_000_000_000, "duration_seconds": 1.0}])
        self.assertGreater(tiny.duration_per_token.minimum, 0)

    def test_public_identifiers_reject_paths_delimiters_spaces_and_unicode(self):
        drive_path = "C" + ":" + chr(92) + "private"
        unc_path = chr(92) * 2 + "server"
        for value in (drive_path, unc_path, "a|b", "two words", "日本語", "a..b", "x:ads"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_public_id(value)
        self.assertEqual(normalize_public_id("tokenizer-v1.2"), "tokenizer-v1.2")

    def test_pause_estimate_uses_rounded_samples_and_sample_rates_are_exact_ints(self):
        settings = PlanSettings(max_characters=4, max_tokens=100)
        counter = lambda text: len(text) + 1
        paused = prepare_text_plan("一二三四五六七八九", counter, settings=settings, pause_policy=PausePolicy("0.00002", 44100))
        baseline = prepare_text_plan("一二三四五六七八九", counter, settings=settings, pause_policy=PausePolicy("0", 44100))
        delta = paused.pause_policy.samples * paused.boundary_count / paused.pause_policy.sample_rate
        self.assertAlmostEqual(estimate_from_plan(paused).duration_seconds.minimum - estimate_from_plan(baseline).duration_seconds.minimum, delta)
        with self.assertRaises(ValueError):
            estimate_from_plan(paused, sample_rate_range=(16000.5, 48000.5))

    def test_estimate_counts_are_immutable_and_display_is_coarse(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1)
        estimate = estimate_from_plan(plan)
        with self.assertRaises(TypeError):
            estimate.counts["tokens"] = 999
        display = estimate.coarse_display_payload()
        for name in ("duration_seconds",):
            self.assertEqual(display[name]["minimum"] % 5, 0)
            self.assertEqual(display[name]["maximum"] % 5, 0)
        self.assertIsNone(display["generation_seconds"])

    def test_estimate_public_payload_is_privacy_bounded_and_repeatable(self):
        plan = prepare_text_plan("秘密ではない本文。", lambda text: len(text) + 1, tokenizer_identity="identity")
        first = estimate_from_plan(plan).public_payload()
        second = estimate_from_plan(plan).public_payload()
        self.assertEqual(first, second)
        self.assertNotIn("秘密ではない", repr(first))
        self.assertNotIn("C" + ":" + chr(92), repr(first))


if __name__ == "__main__":
    unittest.main()
