#!/usr/bin/env python3
"""Build a deterministic compact Skill ZIP package for hosted cloud agents.

Supports xAI's hosted **Grok Bot** (``--platform grok-bot``) and Meta's hosted
**Muse** assistant (``--platform muse``).

The archive follows the portable Agent Skills directory layout
(``openmobius-skill/SKILL.md`` plus ``workflows/``, ``scripts/``, and
``knowledge_base/``) and fits comfortably under a 3 MB ceiling by encoding all
2,144 attributable School projections and all 18,645 exact-source evidence
records into the checksummed ``knowledge_v2.compact.json`` corpus. Hosted cloud
sandboxes with Python 3.10+ can run full BM25 + exact-alias retrieval and live
Mobius Quant API queries using the Python standard library alone, without
creating a virtual environment or downloading ~830 MB of embedding/browser
artifacts. Canonical fused-card and vector routes fail closed cleanly.

This command builds a local ``.zip`` archive only; it does not upload, import,
or activate the package in any cloud service.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


THIS_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = THIS_DIR.parent
if str(DEFAULT_SOURCE) not in sys.path:
    sys.path.insert(0, str(DEFAULT_SOURCE))

from scripts._lib.build_lock import (  # noqa: E402
    BuildLockUnavailable,
    INSTALL_GENERATION_MARKER,
    READ_ONLY_BUNDLE_MARKER,
    encode_read_only_bundle_marker,
    knowledge_base_build_lock,
)
from scripts._lib.compact_v2 import (  # noqa: E402
    COMPACT_V2_FILENAME,
    encode_compact_v2,
)
from scripts._lib.knowledge_v2 import BuildResult, build_v2_records  # noqa: E402
from scripts._lib.retriever import assert_readable_generation  # noqa: E402

SKILL_SLUG = "openmobius-skill"
ARCHIVE_ROOT = PurePosixPath(SKILL_SLUG)
HOSTED_MAX_ARCHIVE_BYTES = 3_000_000
HOSTED_PLATFORM_LABELS: dict[str, str] = {
    "grok-bot": "Grok Bot",
    "muse": "Muse",
}
SUPPORTED_HOSTED_PLATFORMS = tuple(HOSTED_PLATFORM_LABELS)
HOSTED_TOP_LEVEL_ENTRIES = frozenset({
    "SKILL.md",
    "LICENSE",
    "ATTRIBUTION.md",
    "PRIVACY.md",
    "workflows",
    "knowledge_base",
    "scripts",
    "templates",
})
TREE_MAPPINGS = (
    ("workflows", ARCHIVE_ROOT / "workflows"),
    ("knowledge_base", ARCHIVE_ROOT / "knowledge_base"),
    ("scripts", ARCHIVE_ROOT / "scripts"),
    ("templates", ARCHIVE_ROOT / "templates"),
)
TOP_LEVEL_FILES = (
    ("LICENSE", ARCHIVE_ROOT / "LICENSE"),
    ("ATTRIBUTION.md", ARCHIVE_ROOT / "ATTRIBUTION.md"),
    ("PRIVACY.md", ARCHIVE_ROOT / "PRIVACY.md"),
)
HOSTED_ATTRIBUTION_PATH = ARCHIVE_ROOT / "ATTRIBUTION.md"
HOSTED_PRIVACY_PATH = ARCHIVE_ROOT / "PRIVACY.md"
HOSTED_KB_README_PATH = ARCHIVE_ROOT / "knowledge_base" / "README.md"
HOSTED_KB_SCHEMAS_README_PATH = (
    ARCHIVE_ROOT / "knowledge_base" / "schemas" / "README.md"
)
EXCLUDED_PARTS = frozenset({
    ".git",
    ".venv",
    "venv",
    ".cache",
    "cache",
    ".pytest_cache",
    "__pycache__",
    "_index",
    "_embedding_cache",
    "embedding_seed_v2",
    "node_modules",
})
EXCLUDED_PART_PREFIXES = (
    "._index.build-",
    "._index.backup-",
    "._cards.build-",
    "._cards.backup-",
    ".embedding_seed_v2.build-",
    ".embedding_seed_v2.backup-",
)
EXCLUDED_NAMES = frozenset({
    ".ds_store",
    ".netrc",
    ".npmrc",
    ".pypirc",
    INSTALL_GENERATION_MARKER,
    "build_hosted_package.py",
    "build_workbuddy_package.py",
    "credentials.json",
    "secret.json",
    "secrets.json",
    "token.json",
    "tokens.json",
})
OMITTED_KB_DIRECTORIES = frozenset({"cases", "concepts"})
OMITTED_KB_FILES = frozenset({
    "_merge_report.json",
    COMPACT_V2_FILENAME,
    "index.json",
})
OMITTED_SCRIPT_FILES = frozenset({
    "_lib/embedder.py",
    "_lib/embedding_cache.py",
    "build_hosted_package.py",
    "build_index.py",
    "build_knowledge_v2.py",
    "build_workbuddy_package.py",
    "evaluate_retrieval.py",
    "export_v2_embedding_seed.py",
    "kb_doctor.py",
})
SECRET_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class PackageEntry:
    source: Path
    archive_path: PurePosixPath
    transform: str = "raw"


def _render_corpus_counts(text: str, result: BuildResult | None) -> str:
    if result is None:
        return text
    return text.replace(
        "2,144",
        f"{len(result.school_records):,}",
    ).replace(
        "18,645",
        f"{len(result.evidence_records):,}",
    )


def _strip_yaml_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
        index += 1
    return value.rstrip()


def _parse_yaml_string(value: str, field: str) -> str:
    value = _strip_yaml_comment(value).strip()
    if not value or value[0] in "|>":
        raise ValueError(f"hosted frontmatter field {field!r} must be a single-line string")
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError(f"hosted frontmatter field {field!r} must be a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"invalid quoted hosted frontmatter field {field!r}")
        inner = value[1:-1]
        if "'" in inner.replace("''", ""):
            raise ValueError(f"invalid quoted hosted frontmatter field {field!r}")
        return inner.replace("''", "'")
    return value


def validate_hosted_frontmatter(frontmatter: str) -> dict[str, str]:
    """Validate the portable Agent Skills frontmatter for a hosted package."""
    fields: dict[str, str] = {}
    for line_number, raw_line in enumerate(frontmatter.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line != raw_line.lstrip() or raw_line.strip() == "---":
            raise ValueError(
                f"unsupported hosted frontmatter at line {line_number}"
            )
        key, separator, raw_value = raw_line.partition(":")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(f"invalid hosted frontmatter at line {line_number}")
        if key in fields:
            raise ValueError(f"duplicate hosted frontmatter field {key!r}")
        fields[key] = _parse_yaml_string(raw_value, key)

    for required in ("name", "description"):
        if not fields.get(required, "").strip():
            raise ValueError(f"missing required hosted frontmatter field: {required}")
    name = fields["name"]
    description = fields["description"]
    if name != SKILL_SLUG or not SKILL_NAME_RE.fullmatch(name):
        raise ValueError(
            f"hosted frontmatter name must match archive slug {SKILL_SLUG!r}"
        )
    if len(description) > 1024:
        raise ValueError("hosted frontmatter description exceeds 1024 characters")
    return fields


def _compact_workflow_notice(brand: str) -> str:
    return (
        f"> **{brand} compact-corpus override:** this hosted package contains all "
        "2,144 School projections and all 18,645 exact-source evidence records in a "
        "checksummed Python-standard-library lexical corpus. Always pass "
        "`--search-mode lexical` with `--layer school` or `--layer evidence` and hard "
        "School/source/type/exclusion filters. Canonical fused-card and vector "
        "auto/hybrid/semantic modes are omitted and must fail closed rather than being "
        "approximated or widened.\n"
    )


def _localize_markdown(text: str, *, brand: str) -> str:
    """Adapt instructions for zero-venv hosted cloud compact packages."""
    text = text.replace(".venv/bin/python", "python3")
    text = text.replace(r".venv\Scripts\python.exe", "python3")
    text = text.replace(".venv/bin/pip", "python3 -m pip")
    text = text.replace(".venv/bin/playwright", "python3 -m playwright")
    text = text.replace(r".venv\Scripts\pip.exe", "python3 -m pip")
    text = text.replace(r".venv\Scripts\playwright.exe", "python3 -m playwright")
    text = text.replace(
        "<PYTHON> scripts/kb_retrieve.py",
        "<PYTHON> scripts/kb_retrieve.py --search-mode lexical",
    )
    text = text.replace(
        "python3 scripts/kb_retrieve.py",
        "python3 scripts/kb_retrieve.py --search-mode lexical",
    )
    text = re.sub(
        r"(?m)^(\s*)kb_retrieve\.py\b(?!\s+--search-mode\b)",
        r"\1kb_retrieve.py --search-mode lexical",
        text,
    )
    text = text.replace(
        "`scripts/kb_retrieve.py \"",
        "`scripts/kb_retrieve.py --search-mode lexical \"",
    )
    text = text.replace(
        "`kb_retrieve.py \"",
        "`kb_retrieve.py --search-mode lexical \"",
    )
    text = text.replace(
        "--search-mode lexical --search-mode lexical",
        "--search-mode lexical",
    )
    text = text.replace(
        "- School-scoped grounding uses `school_knowledge_v2`, which omits\n"
        "  cross-School fused rules that cannot be attributed. Any requested source\n"
        "  uses `source_evidence_v2` with `--layer evidence --sources ...`; combine it\n"
        "  with `--schools ...` for an exact intersection. Never use the fused\n"
        "  canonical layer to claim strict School/source isolation.",
        "- School-scoped grounding uses compact `school_knowledge_v2`, which omits\n"
        "  cross-School fused rules that cannot be attributed. Requested sources use\n"
        "  compact `source_evidence_v2` with `--layer evidence --sources ...`; combine\n"
        "  it with `--schools ...` for an exact intersection. The fused canonical\n"
        "  layer remains unavailable and must fail closed.",
    )
    text = text.replace(
        "- School/evidence queries use hard-filtered hybrid retrieval by default\n"
        "  (BM25 + semantic RRF over independently embedded scoped documents). Keep\n"
        "  `--search-mode auto` unless diagnosing retrieval; exact terms/aliases stay\n"
        "  first and the hard School/source boundary is never widened.",
        f"- In this {brand} hosted package, School/evidence queries use explicit\n"
        "  hard-filtered lexical retrieval (`--search-mode lexical`, BM25 + exact\n"
        "  aliases). Vector auto/hybrid/semantic modes are unavailable; hard\n"
        "  School/source boundaries are never widened.",
    )
    text = text.replace(
        "- `--search-mode auto|hybrid|semantic|lexical` (`auto` uses hybrid for v2;\n"
        "  lexical does not load the embedding model)",
        f"- `--search-mode lexical` (required in this {brand} hosted package; vector\n"
        "  auto/hybrid/semantic modes require a separately managed local index)",
    )
    text = text.replace(
        "- `--layer canonical|school|evidence` (`canonical` is compatibility-only for strict routing)",
        "- `--layer school` or `--layer evidence` (the canonical fused-card layer is unavailable in this package)",
    )
    text = text.replace(
        "| `canonical` | `knowledge_base` | Backward-compatible fused-card exploration only; never use it to claim strict School/source isolation |",
        f"| `canonical` | — | Unavailable in this {brand} hosted package; fused-card exploration requires a local-host checkout |",
    )
    for maintainer_row in (
        "| `scripts/build_knowledge_v2.py` | Audit/export deterministic School projections and exact-source evidence |\n",
        "| `scripts/build_index.py` | Build canonical + independently embedded v2 collections; unchanged v2 documents reuse the local content cache |\n",
        "| `scripts/kb_doctor.py` | Environment health check (run if anything's broken) |\n",
    ):
        text = text.replace(maintainer_row, "")
    text = text.replace("`SKILL.body.md`", "the loaded root `SKILL.md`")
    text = text.replace("SKILL.body.md", "the loaded root SKILL.md")
    return text


def _localize_hosted_resource(
    text: str,
    archive_path: PurePosixPath,
    *,
    brand: str,
    corpus_result: BuildResult | None = None,
) -> str:
    if archive_path == HOSTED_ATTRIBUTION_PATH:
        text = (
            f"# Third-Party Components and Attribution — {brand} hosted package\n\n"
            "OpenMobius-skill is licensed under Apache 2.0; see `LICENSE` in this\n"
            "directory. This hosted package does not create a virtual environment,\n"
            "install Python packages, or download model/browser weights.\n\n"
            "The Nomic `nomic-ai/nomic-embed-text-v1.5` model, vector seeds, and\n"
            "Chroma index are omitted; this package uses deterministic BM25 + exact-alias\n"
            "lexical retrieval over the bundled compact v2 corpus (2,144 School\n"
            "projections and 18,645 exact-source evidence records).\n\n"
            "TradingView's `lightweight-charts` JavaScript library (Apache 2.0,\n"
            "<https://github.com/tradingview/lightweight-charts>) is included for chart\n"
            "rendering when the cloud host provides Playwright + Chromium.\n"
        )
    elif archive_path == HOSTED_PRIVACY_PATH:
        text = (
            f"# Privacy — {brand} hosted ZIP package\n\n"
            f"This packaged Skill runs inside the {brand} cloud sandbox or workspace.\n"
            "It bundles instructions, workflows, schemas, aliases, and a checksummed\n"
            "compact School/source-evidence corpus. No credentials, virtual environments,\n"
            "local vector indexes, or model caches are included.\n\n"
            "Knowledge retrieval reads only bundled files. Live market-data requests\n"
            "contact `api.mobiusquant.ai` (or an operator-selected `MOBIUS_API_BASE`)\n"
            "with the requested public asset, timeframe, and indicator parameters. No\n"
            "authentication is required by default; if `MOBIUS_API_TOKEN` is explicitly\n"
            "configured, it is sent as a Bearer header for higher rate limits.\n"
        )
    elif archive_path == HOSTED_KB_README_PATH:
        text = (
            f"# knowledge_base/ — {brand} hosted compact package\n\n"
            "```\n"
            "knowledge_base/\n"
            f"├── {COMPACT_V2_FILENAME} # 2,144 School + 18,645 evidence records\n"
            "├── schools.json                   # School aliases and capabilities\n"
            "├── term_aliases.json              # exact term/alias lookup\n"
            "└── schemas/                       # published v2 schemas\n"
            "```\n\n"
            f"This {brand} package deliberately omits `_index/`, `_embedding_cache/`,\n"
            "`embedding_seed_v2/`, and canonical fused cards. Run retrieval with\n"
            "`python3 scripts/kb_retrieve.py --search-mode lexical`.\n"
        )
    elif archive_path == HOSTED_KB_SCHEMAS_README_PATH:
        text = (
            f"# Knowledge model v2 — {brand} hosted compact package\n\n"
            "This package carries a checksummed compact representation that reconstructs\n"
            "all 2,144 `school_knowledge_v2` projections and all 18,645\n"
            "`source_evidence_v2` records for Python-standard-library-only lexical\n"
            "retrieval. Canonical fused cards and vector indexes are omitted and must\n"
            "fail closed.\n"
        )

    text = _localize_markdown(text, brand=brand)
    if (
        len(archive_path.parts) >= 3
        and archive_path.parts[0] == SKILL_SLUG
        and archive_path.parts[1] == "workflows"
    ):
        text = _compact_workflow_notice(brand) + "\n" + text
    return _render_corpus_counts(text, corpus_result)


def _localize_python(payload: bytes, source: Path, *, brand: str) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Python source is not UTF-8: {source}") from exc
    if source.name == "retriever.py":
        local_index_hint = (
            "            if self.layer == \"canonical\":\n"
            "                hint = \"请先跑：.venv/bin/python scripts/build_index.py\"\n"
            "            else:\n"
            "                hint = (\n"
            "                    \"该知识层需要 v2 索引；请跑：\"\n"
            "                    \".venv/bin/python scripts/build_index.py \"\n"
            "                    f\"--kb {shlex.quote(str(self.kb_dir))} --upgrade\\n\"\n"
            "                    f\"所需 collection: '{self.collection_name}'\"\n"
            "                )"
        )
        hosted_hint = (
            "            hint = (\n"
            f"                \"This {brand} compact package supports only explicit \"\n"
            "                \"lexical School/evidence retrieval (--search-mode lexical); \"\n"
            "                \"vector index recovery requires a full local-host checkout.\"\n"
            "            )"
        )
        if local_index_hint not in text:
            raise ValueError(
                "retriever index hint changed; update hosted package localization"
            )
        text = text.replace(local_index_hint, hosted_hint)
        text = text.replace(
            "请先跑：.venv/bin/python scripts/build_index.py",
            f"This {brand} package has no vector index; use --search-mode lexical",
        )
        text = text.replace(
            "scripts/build_index.py --force 重建",
            "a separate local-host source checkout to rebuild the index",
        )
        text = text.replace(
            "scripts/build_index.py 操作。",
            f"local-host recovery（本 {brand} package 不提供）。",
        )
        if "scripts/build_index.py" in text:
            raise ValueError(
                "hosted retriever still references an omitted index builder"
            )
    text = text.replace(".venv/bin/python", "python3")
    text = text.replace(r".venv\Scripts\python.exe", "python3")
    text = text.replace(".venv/bin/pip", "python3 -m pip")
    text = text.replace(".venv/bin/playwright", "python3 -m playwright")
    text = text.replace(r".venv\Scripts\pip.exe", "python3 -m pip")
    text = text.replace(r".venv\Scripts\playwright.exe", "python3 -m playwright")
    text = text.replace(
        "或运行 `python scripts/kb_doctor.py` 做完整环境体检。",
        "请确认云端运行环境提供可用的 CJK 字体。",
    )
    text = "".join(
        line.replace("SKILL.body.md", "the loaded root SKILL.md")
        if line.lstrip().startswith("#")
        else line
        for line in text.splitlines(keepends=True)
    )
    return text.encode("utf-8")


def compose_hosted_skill(
    source_root: Path,
    platform_name: str,
    *,
    corpus_result: BuildResult | None = None,
) -> str:
    """Compose hosted-platform frontmatter + compact-mode shared instructions."""
    if platform_name not in HOSTED_PLATFORM_LABELS:
        raise ValueError(
            f"unsupported hosted platform {platform_name!r}; expected one of "
            f"{SUPPORTED_HOSTED_PLATFORMS}"
        )
    brand = HOSTED_PLATFORM_LABELS[platform_name]
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(
            f"source root must be a regular directory, not a symlink: {source_root}"
        )
    source_root = source_root.resolve()
    frontmatter_path = source_root / "platforms" / f"{platform_name}.yaml"
    body_path = source_root / "SKILL.body.md"
    for source_input in (frontmatter_path, body_path):
        if source_input.is_symlink() or not source_input.is_file():
            raise ValueError(
                f"hosted composition input must be a regular file: {source_input}"
            )
        if not source_input.resolve().is_relative_to(source_root):
            raise ValueError(
                f"hosted composition input escapes source root: {source_input}"
            )
    if corpus_result is None:
        kb_dir = source_root / "knowledge_base"
        with knowledge_base_build_lock(kb_dir, mode="read"):
            assert_readable_generation(kb_dir)
            corpus_result = build_v2_records(kb_dir)
    frontmatter = frontmatter_path.read_text(encoding="utf-8").rstrip()
    validate_hosted_frontmatter(frontmatter)
    body = body_path.read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError("SKILL.body.md must not be empty")
    if not body.startswith("\n"):
        body = "\n" + body
    body = _localize_markdown(body, brand=brand)
    portability_note = (
        f"\n> **{brand} hosted compact-package mode:** This <3 MB cloud-ready archive "
        "carries a verified compact corpus containing all 2,144 School projections "
        "and 18,645 exact-source evidence records, while omitting canonical fused "
        "cards, `.venv/`, and local vector-index weights. Resolve `<PYTHON>` to the "
        "cloud sandbox's Python 3.10+ interpreter (typically `python3`). Knowledge "
        "retrieval MUST use explicit `--layer school` or `--layer evidence` with "
        "`--search-mode lexical` (Python-stdlib BM25 + exact aliases + hard "
        "School/source filters); canonical and vector modes fail closed. Live OHLCV "
        "and SMC structural indicators via `scripts/kb_klines.py` require only the "
        "Python standard library and outbound HTTPS to `api.mobiusquant.ai`. PNG "
        "chart rendering additionally requires host Playwright + Chromium, and image "
        "annotation requires Pillow; verify availability before running an artifact "
        "step and disclose cleanly when the cloud container omits them. If this Skill "
        "is attached to a chat-only session with no Python execution tool, report "
        "script-backed local knowledge-base operations unavailable rather than "
        "fabricating School rules from memory.\n"
    )
    body = portability_note + body
    return _render_corpus_counts(f"---\n{frontmatter}\n---{body}", corpus_result)


def _is_excluded(relative_path: Path) -> bool:
    parts = tuple(part.casefold() for part in relative_path.parts)
    name = relative_path.name.casefold()
    has_cache_dir = any(
        part in EXCLUDED_PARTS
        or part.endswith("_cache")
        or part.endswith("-cache")
        or part.startswith(EXCLUDED_PART_PREFIXES)
        for part in parts
    )
    is_env_file = name == ".env" or name == ".envrc" or name.startswith(".env.")
    return (
        has_cache_dir
        or name in EXCLUDED_NAMES
        or is_env_file
        or relative_path.suffix.casefold() in {".pyc", ".pyo", *SECRET_SUFFIXES}
    )


def _entry_transform(archive_path: PurePosixPath) -> str:
    if archive_path.suffix.lower() == ".md":
        return "markdown"
    if (
        len(archive_path.parts) > 2
        and archive_path.parts[1] == "scripts"
        and archive_path.suffix.lower() == ".py"
    ):
        return "python"
    return "raw"


def _tree_entries(
    source_root: Path,
    relative_root: str,
    archive_root: PurePosixPath,
    *,
    excluded_sources: frozenset[Path] = frozenset(),
) -> Iterable[PackageEntry]:
    root = source_root / relative_root
    if root.is_symlink() or not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in excluded_sources or not resolved.is_relative_to(source_root):
            continue
        relative = path.relative_to(source_root)
        if _is_excluded(relative):
            continue
        if relative_root == "knowledge_base":
            kb_relative = relative.relative_to("knowledge_base")
            if (
                kb_relative.parts[0] in OMITTED_KB_DIRECTORIES
                or kb_relative.as_posix() in OMITTED_KB_FILES
            ):
                continue
        if (
            relative_root == "scripts"
            and relative.relative_to("scripts").as_posix() in OMITTED_SCRIPT_FILES
        ):
            continue
        archive_path = archive_root / relative.relative_to(relative_root)
        yield PackageEntry(path, archive_path, _entry_transform(archive_path))


def _validate_archive_path(archive_path: PurePosixPath) -> None:
    path_text = archive_path.as_posix()
    raw_parts = path_text.split("/")
    unsafe = (
        not path_text
        or archive_path.is_absolute()
        or "\\" in path_text
        or "\x00" in path_text
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(":" in part for part in raw_parts)
        or any(any(ord(char) < 32 for char in part) for part in raw_parts)
    )
    if unsafe:
        raise ValueError(f"unsafe archive path: {path_text}")
    if raw_parts[0] != SKILL_SLUG or len(raw_parts) < 2:
        raise ValueError(f"archive path escapes the skill root: {path_text}")
    if raw_parts[1] not in HOSTED_TOP_LEVEL_ENTRIES:
        raise ValueError(f"unsupported hosted package top-level entry: {path_text}")


def _validate_inventory(entries: Iterable[PackageEntry]) -> None:
    paths = [ARCHIVE_ROOT / "SKILL.md", *(entry.archive_path for entry in entries)]
    folded: dict[str, PurePosixPath] = {}
    for archive_path in paths:
        _validate_archive_path(archive_path)
        key = archive_path.as_posix().casefold()
        if key in folded:
            raise ValueError(
                "duplicate hosted archive path: "
                f"{folded[key].as_posix()} and {archive_path.as_posix()}"
            )
        folded[key] = archive_path

    all_paths = set(folded)
    for _key, archive_path in folded.items():
        parts = archive_path.as_posix().split("/")
        for end in range(1, len(parts)):
            if "/".join(parts[:end]).casefold() in all_paths:
                raise ValueError(
                    f"archive file/directory path collision: {archive_path.as_posix()}"
                )


def package_entries(
    source_root: Path,
    *,
    excluded_sources: Iterable[Path] = (),
) -> list[PackageEntry]:
    """Return the deterministic, symlink-free hosted package inventory."""
    source_root = Path(source_root)
    if source_root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {source_root}")
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    for relative in (
        Path("knowledge_base/schools.json"),
        Path("knowledge_base/term_aliases.json"),
    ):
        required = source_root / relative
        if required.is_symlink() or not required.is_file():
            raise ValueError(
                f"required hosted knowledge input must be a regular file: {required}"
            )
        if not required.resolve().is_relative_to(source_root):
            raise ValueError(f"hosted knowledge input escapes source root: {required}")
    excluded = frozenset(path.resolve() for path in excluded_sources)
    entries: list[PackageEntry] = []
    for name, archive_path in TOP_LEVEL_FILES:
        path = source_root / name
        if path.is_file() and not path.is_symlink():
            entries.append(
                PackageEntry(path, archive_path, _entry_transform(archive_path))
            )
    for name, archive_root in TREE_MAPPINGS:
        entries.extend(
            _tree_entries(
                source_root,
                name,
                archive_root,
                excluded_sources=excluded,
            )
        )
    entries.append(
        PackageEntry(
            source_root / "knowledge_base",
            ARCHIVE_ROOT / "knowledge_base" / COMPACT_V2_FILENAME,
            "compact_v2",
        )
    )
    entries.append(
        PackageEntry(
            source_root / "knowledge_base",
            ARCHIVE_ROOT / "knowledge_base" / READ_ONLY_BUNDLE_MARKER,
            "read_only_bundle_marker",
        )
    )
    entries.sort(key=lambda entry: entry.archive_path.as_posix())
    _validate_inventory(entries)
    return entries


def _write_bytes(
    archive: zipfile.ZipFile,
    archive_path: PurePosixPath,
    payload: bytes,
    *,
    executable: bool = False,
) -> None:
    _validate_archive_path(archive_path)
    info = zipfile.ZipInfo(archive_path.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, payload)


def _entry_payload(
    entry: PackageEntry,
    *,
    brand: str,
    corpus_result: BuildResult | None = None,
) -> bytes:
    if entry.transform == "compact_v2":
        result = corpus_result or build_v2_records(entry.source)
        return encode_compact_v2(result)
    if entry.transform == "read_only_bundle_marker":
        result = corpus_result or build_v2_records(entry.source)
        payloads = {
            COMPACT_V2_FILENAME: encode_compact_v2(result),
            "schools.json": (entry.source / "schools.json").read_bytes(),
            "term_aliases.json": (entry.source / "term_aliases.json").read_bytes(),
        }
        return encode_read_only_bundle_marker(payloads)
    payload = entry.source.read_bytes()
    if entry.transform == "raw":
        return payload
    if entry.transform == "python":
        return _localize_python(payload, entry.source, brand=brand)
    if entry.transform == "markdown":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Markdown resource is not UTF-8: {entry.source}") from exc
        return _localize_hosted_resource(
            text,
            entry.archive_path,
            brand=brand,
            corpus_result=corpus_result,
        ).encode("utf-8")
    raise ValueError(f"unknown hosted package transform: {entry.transform}")


def _validate_output_location(
    source_root: Path,
    output: Path,
    platform_name: str,
) -> None:
    if output.suffix.casefold() != ".zip":
        raise ValueError(f"hosted package output must use a .zip suffix: {output}")
    try:
        relative = output.relative_to(source_root)
    except ValueError:
        return
    packaged_roots = {source for source, _destination in TREE_MAPPINGS}
    protected_files = {
        "SKILL.body.md",
        f"platforms/{platform_name}.yaml",
        *(source for source, _destination in TOP_LEVEL_FILES),
    }
    if (
        relative.as_posix() in protected_files
        or (relative.parts and relative.parts[0] in packaged_roots)
    ):
        raise ValueError(
            f"output would overwrite or enter packaged source data: {output}"
        )


def build_package(
    source_root: Path,
    output: Path,
    *,
    platform_name: str,
    force: bool = False,
) -> dict:
    """Create a deterministic hosted compact ZIP package and return its summary."""
    if platform_name not in HOSTED_PLATFORM_LABELS:
        raise ValueError(
            f"unsupported hosted platform {platform_name!r}; expected one of "
            f"{SUPPORTED_HOSTED_PLATFORMS}"
        )
    brand = HOSTED_PLATFORM_LABELS[platform_name]
    source_root = Path(source_root)
    if source_root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {source_root}")
    source_root = source_root.resolve()
    requested_output = Path(output)
    if requested_output.is_symlink():
        raise ValueError(
            f"hosted package output must not be a symlink: {requested_output}"
        )
    output = requested_output.parent.resolve() / requested_output.name
    with knowledge_base_build_lock(source_root / "knowledge_base", mode="read"):
        assert_readable_generation(source_root / "knowledge_base")
        corpus_result = build_v2_records(source_root / "knowledge_base")
        with knowledge_base_build_lock(output, mode="write"):
            if output.is_symlink():
                raise ValueError(
                    f"hosted package output must not be a symlink: {output}"
                )
            _validate_output_location(source_root, output, platform_name)
            if output.exists() and not force:
                raise FileExistsError(
                    f"output already exists (use --force): {output}"
                )
            output.parent.mkdir(parents=True, exist_ok=True)

            skill_md = compose_hosted_skill(
                source_root,
                platform_name,
                corpus_result=corpus_result,
            ).encode("utf-8")
            entries = package_entries(source_root, excluded_sources=(output,))
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{output.name}.",
                    suffix=".tmp",
                    dir=output.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                with zipfile.ZipFile(
                    temporary_path,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as archive:
                    _write_bytes(archive, ARCHIVE_ROOT / "SKILL.md", skill_md)
                    for entry in entries:
                        mode = entry.source.stat().st_mode
                        _write_bytes(
                            archive,
                            entry.archive_path,
                            _entry_payload(
                                entry,
                                brand=brand,
                                corpus_result=corpus_result,
                            ),
                            executable=(
                                entry.transform == "python"
                                and bool(mode & stat.S_IXUSR)
                            ),
                        )
                archive_size = temporary_path.stat().st_size
                if archive_size > HOSTED_MAX_ARCHIVE_BYTES:
                    raise ValueError(
                        f"{brand} ZIP package exceeds the 3 MB size limit: "
                        f"{archive_size} bytes > {HOSTED_MAX_ARCHIVE_BYTES} bytes"
                    )
                if output.exists() and not force:
                    raise FileExistsError(
                        f"output already exists (use --force): {output}"
                    )
                if output.is_symlink():
                    raise ValueError(
                        f"hosted package output must not be a symlink: {output}"
                    )
                temporary_path.replace(output)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    return {
        "platform": platform_name,
        "output": str(output),
        "archive_root": ARCHIVE_ROOT.as_posix(),
        "files": len(entries) + 1,
        "bytes": output.stat().st_size,
        "max_bytes": HOSTED_MAX_ARCHIVE_BYTES,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic <3 MB compact OpenMobius-skill ZIP archive "
            "for xAI's hosted Grok Bot (--platform grok-bot) or Meta's hosted "
            "Muse assistant (--platform muse); this command only writes the "
            "local archive and does not upload or import it"
        ),
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=SUPPORTED_HOSTED_PLATFORMS,
        help="Target hosted agent platform (grok-bot or muse)",
    )
    parser.add_argument("--output", required=True, help="Destination local .zip path")
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="OpenMobius source root (default: repository root)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output archive",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = build_package(
            Path(args.source),
            Path(args.output),
            platform_name=args.platform,
            force=args.force,
        )
    except (
        BuildLockUnavailable,
        FileExistsError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
