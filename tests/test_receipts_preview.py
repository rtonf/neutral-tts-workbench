from __future__ import annotations

import unittest
from copy import deepcopy

from neutral_tts.estimation import calibration_from_receipts, estimate_from_plan
from neutral_tts.filename_templates import FilenameContext
from neutral_tts.planning import PausePolicy, PlanSettings, prepare_text_plan
from neutral_tts.preview import pre_generation_preview
from neutral_tts.receipts import ActualRun, RECEIPT_SCHEMA, build_success_receipt, calibration_sample_from_receipt


class TokenCounterSpy:
    def __init__(self):
        self.calls = 0

    def __call__(self, text: str) -> int:
        self.calls += 1
        return len(text) + 1


class SamplerSpy:
    def __init__(self):
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("sampler must not be called during preview")


class ReceiptPreviewTests(unittest.TestCase):
    def test_integrated_preview_uses_persisted_context_and_shared_plan_without_sampler(self):
        counter = TokenCounterSpy()
        sampler = SamplerSpy()
        result = pre_generation_preview(
            "本文です。",
            "{date}-{job_id_short}",
            FilenameContext("20260901", "120000", job_id_short="abc123"),
            counter,
            pause_policy=PausePolicy("0", 48000),
            tokenizer_identity="test-tokenizer",
        )
        self.assertEqual(result.filename.wav_name, "20260901-abc123.wav")
        self.assertEqual(result.plan.plan_digest, result.estimate.plan_digest)
        self.assertIsNone(result.estimate.generation_seconds)
        self.assertGreater(counter.calls, 0)
        self.assertEqual(sampler.calls, 0)
        self.assertNotIn("本文です", repr(result.public_payload()))

    def test_success_receipt_keeps_estimate_and_records_actual_relation(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1, pause_policy=PausePolicy("0", 48000))
        estimate = estimate_from_plan(plan)
        actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, estimate.duration_seconds.minimum, int(estimate.wav_bytes.minimum), staging_bytes_peak=int(estimate.staging_bytes.minimum))
        receipt = build_success_receipt(plan, estimate, actual)
        self.assertEqual(receipt["schema_version"], RECEIPT_SCHEMA)
        self.assertEqual(receipt["status"], "SUCCEEDED")
        self.assertEqual(receipt["estimate"]["plan_digest"], plan.plan_digest)
        self.assertEqual(receipt["actual"]["counts"]["tokens"], plan.total_tokens)
        self.assertEqual(receipt["estimate_relation"], "unknown")
        self.assertNotIn("本文です", repr(receipt))
        self.assertNotIn("absolute", repr(receipt).lower())

    def test_actual_outside_calibrated_range_does_not_rewrite_estimate(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1)
        initial = estimate_from_plan(plan, device_class="cpu")
        sample_actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, 1.0, 100, generation_seconds=2.0)
        sample_receipt = build_success_receipt(plan, initial, sample_actual)
        calibration = calibration_from_receipts(initial.confidence.key, [deepcopy(sample_receipt) for _ in range(5)])
        estimate = estimate_from_plan(plan, calibration=calibration, device_class="cpu")
        actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, estimate.duration_seconds.maximum * 10, int(estimate.wav_bytes.maximum * 10))
        receipt = build_success_receipt(plan, estimate, actual)
        self.assertEqual(receipt["estimate_relation"], "outside_range")
        self.assertEqual(receipt["estimate"], estimate.public_payload())

    def test_calibration_sample_requires_success_schema_and_key(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1)
        estimate = estimate_from_plan(plan)
        actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, 2.0, 100, generation_seconds=3.0)
        valid = build_success_receipt(plan, estimate, actual)
        key = estimate.confidence.key
        self.assertIsNotNone(calibration_sample_from_receipt(valid, key))
        failed = deepcopy(valid)
        failed["status"] = "FAILED"
        self.assertIsNone(calibration_sample_from_receipt(failed, key))
        self.assertIsNone(calibration_sample_from_receipt(valid, "bad"))
        incomplete = deepcopy(valid)
        del incomplete["plan"]
        self.assertIsNone(calibration_sample_from_receipt(incomplete, key))

    def test_receipt_parser_rejects_count_drift_and_unexpected_fields(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1)
        estimate = estimate_from_plan(plan)
        actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, 2.0, 100)
        valid = build_success_receipt(plan, estimate, actual)
        for mutation in ("actual", "estimate", "extra"):
            candidate = deepcopy(valid)
            if mutation == "actual":
                candidate["actual"]["counts"]["tokens"] += 1
            elif mutation == "estimate":
                candidate["estimate"]["counts"]["chunks"] += 1
            else:
                candidate["raw_text"] = "must not be accepted"
            with self.subTest(mutation=mutation):
                self.assertIsNone(calibration_sample_from_receipt(candidate, estimate.confidence.key))

    def test_receipt_parser_rejects_forged_chunk_spans(self):
        plan = prepare_text_plan(
            "一二三四五六七八九", lambda text: len(text) + 1,
            settings=PlanSettings(max_characters=4),
        )
        estimate = estimate_from_plan(plan)
        actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, 2.0, 100)
        valid = build_success_receipt(plan, estimate, actual)
        self.assertGreater(len(valid["plan"]["chunks"]), 1)
        for mutation in ("first_start", "gap", "short_end"):
            candidate = deepcopy(valid)
            if mutation == "first_start":
                candidate["plan"]["chunks"][0]["start"] = 1
            elif mutation == "gap":
                candidate["plan"]["chunks"][1]["start"] += 1
            else:
                candidate["plan"]["chunks"][-1]["end"] -= 1
            with self.subTest(mutation=mutation):
                self.assertIsNone(calibration_sample_from_receipt(candidate, estimate.confidence.key))

    def test_receipt_without_generation_time_calibrates_duration_only(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1)
        estimate = estimate_from_plan(plan)
        actual = ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, 2.0, 100)
        receipt = build_success_receipt(plan, estimate, actual)
        sample = calibration_sample_from_receipt(receipt, estimate.confidence.key)
        self.assertIsNotNone(sample)
        self.assertIsNone(sample["generation_seconds"])
        calibration = calibration_from_receipts(estimate.confidence.key, [receipt])
        self.assertEqual(calibration.state, "low_sample")
        self.assertIsNone(calibration.generation_seconds_per_token)

    def test_actual_run_and_receipt_plan_integrity_are_strict(self):
        for arguments in (
            (True, 1, 0, 48000, 1.0, 44),
            (1, 1, 0, 48000, True, 44),
            (1, 1, 0, 48000, 1.0, 43),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ActualRun(*arguments)
        with self.assertRaises(ValueError):
            ActualRun(1, 1, 0, 48000, 1.0, 44, staging_bytes_peak=1.5)
        first = prepare_text_plan("最初です。", lambda text: len(text) + 1)
        second = prepare_text_plan("別です。", lambda text: len(text) + 1)
        estimate = estimate_from_plan(first)
        actual = ActualRun(second.total_tokens, second.chunk_count, second.boundary_count, 48000, 1.0, 100)
        with self.assertRaises(ValueError):
            build_success_receipt(second, estimate, actual)

    def test_preview_accepts_matching_device_calibration(self):
        plan = prepare_text_plan("本文です。", lambda text: len(text) + 1)
        estimate = estimate_from_plan(plan, device_class="cpu")
        receipt = build_success_receipt(plan, estimate, ActualRun(plan.total_tokens, plan.chunk_count, plan.boundary_count, 48000, 2.0, 100))
        calibration = calibration_from_receipts(estimate.confidence.key, [receipt])
        result = pre_generation_preview(
            "本文です。", "preview", FilenameContext("20260901", "120000"),
            lambda text: len(text) + 1, calibration=calibration, device_class="cpu",
        )
        self.assertEqual(result.estimate.confidence.key, estimate.confidence.key)


if __name__ == "__main__":
    unittest.main()
