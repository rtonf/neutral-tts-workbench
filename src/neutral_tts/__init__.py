"""Neutral, source-only planning and safe-output core."""

from .chunk_planner import ChunkEvidence, ChunkPlanningError, plan_chunks
from .estimation import Calibration, GenerationEstimate, NumericRange, calibration_from_receipts, estimate_from_plan, make_calibration_key, validate_calibration_key
from .filename_templates import ALLOWED_PLACEHOLDERS, DEFAULT_TEMPLATE, FilenameContext, FilenamePreview, FilenameTemplateError, choose_collision_target, preview_filename, publish_staged_wav, render_template, validate_template
from .identity import DISPLAY_NAME
from .planning import DictionaryRule, DictionarySnapshot, PausePolicy, PlanSettings, TextPlan, normalize_public_id, prepare_text_plan
from .preview import PreviewResult, pre_generation_preview
from .receipts import ActualRun, RECEIPT_SCHEMA, build_success_receipt, calibration_sample_from_receipt, parse_success_receipt

__all__ = [
    "ActualRun", "ALLOWED_PLACEHOLDERS", "Calibration", "ChunkEvidence", "ChunkPlanningError", "DEFAULT_TEMPLATE", "DISPLAY_NAME", "DictionaryRule", "DictionarySnapshot", "FilenameContext", "FilenamePreview", "FilenameTemplateError", "GenerationEstimate", "NumericRange", "PausePolicy", "PlanSettings", "PreviewResult", "RECEIPT_SCHEMA", "TextPlan", "build_success_receipt", "calibration_from_receipts", "calibration_sample_from_receipt", "choose_collision_target", "estimate_from_plan", "make_calibration_key", "validate_calibration_key", "normalize_public_id", "parse_success_receipt", "plan_chunks", "pre_generation_preview", "preview_filename", "prepare_text_plan", "publish_staged_wav", "render_template", "validate_template",
]
