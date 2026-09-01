#!/usr/bin/env python3
"""Build a fresh manifest-bound source-only test layout.

This command exercises the install contract.  It does not create a launcher,
installer, executable, model bundle, runtime bundle, or public release.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from neutral_tts.install_contract import (  # noqa: E402
    DEMO_VERIFICATION_SCOPE,
    InstallContractError,
    create_test_layout,
)


class LayoutSpecificationError(ValueError):
    """The explicit positive source-layout specification is invalid."""


def load_specification(path: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    """Load an exact positive path list and immutable source identity."""

    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LayoutSpecificationError("layout specification is unreadable") from exc
    if not isinstance(value, dict) or set(value) != {
        "app_root_name", "candidate_id", "files", "schema_version", "source"
    }:
        raise LayoutSpecificationError("layout specification shape is not exact")
    if value.get("schema_version") != "neutral-test-layout-input-v1":
        raise LayoutSpecificationError("layout specification schema is unsupported")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"commit", "tree"}:
        raise LayoutSpecificationError("layout source identity is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise LayoutSpecificationError("layout source list is empty")
    included: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "role"}:
            raise LayoutSpecificationError("layout source entry shape is invalid")
        path_value = item.get("path")
        role = item.get("role")
        if not isinstance(path_value, str) or not isinstance(role, str) or path_value in included:
            raise LayoutSpecificationError("layout source entry is invalid or duplicated")
        included[path_value] = role
    facts = {
        "app_root_name": value.get("app_root_name"),
        "candidate_id": value.get("candidate_id"),
        "source_commit": source.get("commit"),
        "source_tree": source.get("tree"),
    }
    if not all(isinstance(item, str) for item in facts.values()):
        raise LayoutSpecificationError("layout identity fields must be strings")
    return included, facts


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--destination-root", required=True)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    try:
        included, facts = load_specification(args.spec)
        manifest = create_test_layout(
            Path(args.source_root).absolute(),
            Path(args.destination_root).absolute(),
            included,
            candidate_id=facts["candidate_id"],
            source_commit=facts["source_commit"],
            source_tree=facts["source_tree"],
            app_root_name=facts["app_root_name"],
        )
        result: dict[str, Any] = {
            "schema_version": "neutral-test-layout-build-result-v1",
            "status": "ok",
            "candidate_id": manifest["candidate_id"],
            "verification_scope": DEMO_VERIFICATION_SCOPE,
            "file_count": manifest["file_count"],
            "files_sha256": manifest["files_sha256"],
            "external_payload_count": 0,
        }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (InstallContractError, LayoutSpecificationError, OSError):
        print(
            json.dumps(
                {
                    "schema_version": "neutral-test-layout-build-result-v1",
                    "status": "error",
                    "code": "SOURCE_LAYOUT_REJECTED",
                    "message": "The source-only test layout could not be built.",
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
