"""Pure integrated filename and pre-generation planning preview."""

from __future__ import annotations

from dataclasses import dataclass

from .estimation import Calibration, GenerationEstimate, estimate_from_plan
from .filename_templates import FilenameContext, FilenamePreview, preview_filename
from .planning import DictionarySnapshot, PausePolicy, PlanSettings, TextPlan, prepare_text_plan


@dataclass(frozen=True, slots=True)
class PreviewResult:
    filename: FilenamePreview
    plan: TextPlan
    estimate: GenerationEstimate

    def public_payload(self) -> dict[str, object]:
        return {"filename": {"template": self.filename.template, "wav_name": self.filename.wav_name}, "plan_digest": self.plan.plan_digest, "estimate": self.estimate.public_payload()}


def pre_generation_preview(
    source_text: str,
    template: str,
    context: FilenameContext,
    token_counter,
    *,
    dictionary: DictionarySnapshot | None = None,
    settings: PlanSettings | None = None,
    pause_policy: PausePolicy | None = None,
    tokenizer_identity: str = "unspecified",
    calibration: Calibration | None = None,
    device_class: str = "unknown-device",
    sample_rate_range: tuple[int, int] = (16_000, 48_000),
) -> PreviewResult:
    plan = prepare_text_plan(source_text, token_counter, dictionary=dictionary, settings=settings, pause_policy=pause_policy, tokenizer_identity=tokenizer_identity)
    return PreviewResult(preview_filename(template, context), plan, estimate_from_plan(plan, calibration=calibration, device_class=device_class, sample_rate_range=sample_rate_range))


__all__ = ["PreviewResult", "pre_generation_preview"]
