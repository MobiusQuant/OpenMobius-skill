#!/usr/bin/env python3
"""Build or audit conservative School/source-evidence v2 records.

The vector-index builder imports ``build_v2_records`` directly.  This command
is primarily for provenance audits and for optionally exporting deterministic
JSONL fixtures:

    python3 scripts/build_knowledge_v2.py
    python3 scripts/build_knowledge_v2.py --json
    python3 scripts/build_knowledge_v2.py --output /tmp/openmobius-v2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


THIS_DIR = Path(__file__).resolve().parent
SKILL_DIR = THIS_DIR.parent
sys.path.insert(0, str(THIS_DIR))

from _lib.knowledge_v2 import build_v2_records, write_v2_artifacts  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic School projections and exact source evidence."
    )
    parser.add_argument(
        "--kb",
        type=Path,
        default=SKILL_DIR / "knowledge_base",
        help="Knowledge-base directory (default: bundled knowledge_base).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output directory for JSONL artifacts and manifest.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete machine-readable manifest.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = build_v2_records(args.kb)
    manifest = result.manifest()
    paths = write_v2_artifacts(result, args.output) if args.output else None
    if args.json:
        output = dict(manifest)
        if paths:
            output["artifacts"] = {key: str(value) for key, value in paths.items()}
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    collections = manifest["collections"]
    print(
        "School projections: "
        f"{collections['school_knowledge_v2']}; "
        "source evidence: "
        f"{collections['source_evidence_v2']}"
    )
    coverage = manifest["stats"]["coverage"]
    print(
        "Coverage: "
        f"definition_per_source={coverage['definition_per_source_exact_evidence']:.1%}, "
        f"concept_top_level={coverage['concept_top_level_school_projection']:.1%}, "
        f"case_content={coverage['case_content_exact_evidence']:.1%}"
    )
    skipped = manifest["stats"]["skipped"]
    if skipped:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(skipped.items()))
        print(f"Skipped: {rendered}")
    if paths:
        print(f"Artifacts: {paths['manifest'].parent}")
    else:
        print("Dry run only; pass --output to write deterministic JSONL artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
