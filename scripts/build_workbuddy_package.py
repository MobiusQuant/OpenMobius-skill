#!/usr/bin/env python3
"""Build a local WorkBuddy Skill ZIP package.

The package follows WorkBuddy's documented ``skills/<name>/SKILL.md`` layout
and can be imported explicitly in the WorkBuddy desktop UI or submitted to the
Open Platform for publication. It contains a checksummed compact
School/evidence corpus and only the runtime files usable in that constrained
package, not local build products such as a virtual environment, Chroma
database, embedding model, or maintainer-only index tools. The finished ZIP is
rejected above WorkBuddy's documented 3 MB limit. This command does not upload,
import, install, submit, or publish the package.
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
ARCHIVE_ROOT = PurePosixPath("skills") / SKILL_SLUG
WORKBUDDY_MAX_ARCHIVE_BYTES = 3_000_000
WORKBUDDY_RESOURCE_DIRS = frozenset({"references", "scripts", "templates"})
WORKBUDDY_REQUIRED_FIELDS = (
    "description",
    "description_zh",
    "description_en",
    "version",
    "author",
)
TREE_MAPPINGS = (
    ("references", ARCHIVE_ROOT / "references"),
    ("workflows", ARCHIVE_ROOT / "references" / "workflows"),
    ("knowledge_base", ARCHIVE_ROOT / "references" / "knowledge_base"),
    ("scripts", ARCHIVE_ROOT / "scripts"),
    ("templates", ARCHIVE_ROOT / "templates"),
)
REFERENCE_FILES = (
    ("LICENSE", ARCHIVE_ROOT / "references" / "legal" / "LICENSE"),
    (
        "ATTRIBUTION.md",
        ARCHIVE_ROOT / "references" / "legal" / "ATTRIBUTION.md",
    ),
    ("PRIVACY.md", ARCHIVE_ROOT / "references" / "legal" / "PRIVACY.md"),
)
WORKBUDDY_ATTRIBUTION_PATH = (
    ARCHIVE_ROOT / "references" / "legal" / "ATTRIBUTION.md"
)
WORKBUDDY_PRIVACY_PATH = ARCHIVE_ROOT / "references" / "legal" / "PRIVACY.md"
WORKBUDDY_KB_README_PATH = (
    ARCHIVE_ROOT / "references" / "knowledge_base" / "README.md"
)
WORKBUDDY_KB_SCHEMAS_README_PATH = (
    ARCHIVE_ROOT / "references" / "knowledge_base" / "schemas" / "README.md"
)
WORKBUDDY_COMPACT_NOTICE = (
    "> **WorkBuddy compact-corpus override:** this package contains all 2,144 "
    "School projections and all 18,645 exact-source evidence records in a "
    "size-limited lexical corpus. Use `--layer school` or `--layer evidence` "
    "with `--search-mode lexical` and hard School/source/type/exclusion filters. "
    "The canonical "
    "fused-card layer and vector auto/hybrid/semantic retrieval are omitted; "
    "those routes must fail closed rather than being approximated or widened.\n"
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
    "build_workbuddy_package.py",
    "credentials.json",
    "secret.json",
    "secrets.json",
    "token.json",
    "tokens.json",
})
WORKBUDDY_OMITTED_KB_DIRECTORIES = frozenset({"cases", "concepts"})
WORKBUDDY_OMITTED_KB_FILES = frozenset({
    "_merge_report.json",
    COMPACT_V2_FILENAME,
    "index.json",
})
WORKBUDDY_OMITTED_SCRIPT_FILES = frozenset({
    "_lib/embedder.py",
    "_lib/embedding_cache.py",
    "build_index.py",
    "build_knowledge_v2.py",
    "evaluate_retrieval.py",
    "export_v2_embedding_seed.py",
    "kb_doctor.py",
})
SECRET_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$",
)


@dataclass(frozen=True)
class PackageEntry:
    source: Path
    archive_path: PurePosixPath
    transform: str = "raw"


def _render_corpus_counts(text: str, result: BuildResult | None) -> str:
    """Render current corpus counts in generated, package-only prose."""
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


def _parse_yaml_scalar(value: str, field: str) -> object:
    value = _strip_yaml_comment(value).strip()
    if not value or value[0] in "|>":
        raise ValueError(f"WorkBuddy frontmatter field {field!r} must be a string")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid quoted WorkBuddy frontmatter field {field!r}",
            ) from exc
        if not isinstance(parsed, str):
            return parsed
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"invalid quoted WorkBuddy frontmatter field {field!r}")
        inner = value[1:-1]
        if "'" in inner.replace("''", ""):
            raise ValueError(f"invalid quoted WorkBuddy frontmatter field {field!r}")
        return inner.replace("''", "'")
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?", value, re.I):
        return float(value) if any(char in value.casefold() for char in ".e") else int(value)
    return value


def validate_workbuddy_frontmatter(frontmatter: str) -> dict[str, object]:
    """Validate the flat YAML fields required by a WorkBuddy Skill package."""
    fields: dict[str, object] = {}
    for line_number, raw_line in enumerate(frontmatter.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line != raw_line.lstrip() or raw_line.strip() == "---":
            raise ValueError(
                f"unsupported WorkBuddy frontmatter at line {line_number}",
            )
        key, separator, raw_value = raw_line.partition(":")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(
                f"invalid WorkBuddy frontmatter at line {line_number}",
            )
        if key in fields:
            raise ValueError(f"duplicate WorkBuddy frontmatter field {key!r}")
        fields[key] = _parse_yaml_scalar(raw_value, key)

    missing = [
        field
        for field in ("name", *WORKBUDDY_REQUIRED_FIELDS)
        if not isinstance(fields.get(field), str)
        or not str(fields[field]).strip()
    ]
    if missing:
        raise ValueError(
            "missing required WorkBuddy frontmatter field(s): " + ", ".join(missing),
        )
    name = str(fields["name"])
    description = str(fields["description"])
    version = str(fields["version"])
    if name != SKILL_SLUG or not SKILL_NAME_RE.fullmatch(name):
        raise ValueError(
            f"WorkBuddy frontmatter name must match archive slug {SKILL_SLUG!r}",
        )
    if len(description) > 1024:
        raise ValueError("WorkBuddy frontmatter description exceeds 1024 characters")
    if not SEMVER_RE.fullmatch(version):
        raise ValueError("WorkBuddy frontmatter version must be semantic versioning")
    return fields


def _localize_markdown(text: str) -> str:
    """Map source-only paths to the resource locations in a WorkBuddy ZIP."""
    for source_dir, resource_dir in (
        ("workflows", "workflows"),
        ("knowledge_base", "knowledge_base"),
    ):
        text = re.sub(
            rf"(?<![A-Za-z0-9_/@.-])(?:references/)?{source_dir}/",
            f"@references/{resource_dir}/",
            text,
        )
    text = text.replace(".venv/bin/python", "python3")
    text = text.replace(r".venv\Scripts\python.exe", "python3")
    text = text.replace(".venv/bin/pip", "python3 -m pip")
    text = text.replace(".venv/bin/playwright", "python3 -m playwright")
    text = text.replace(r".venv\Scripts\pip.exe", "python3 -m pip")
    text = text.replace(r".venv\Scripts\playwright.exe", "python3 -m playwright")
    # The ZIP package deliberately excludes the local Chroma index and
    # embedding seed. Make every executable School-retrieval example opt in to
    # the dependency-free compact/BM25 path; unavailable layers fail closed.
    text = text.replace(
        "python3 scripts/kb_retrieve.py",
        "python3 scripts/kb_retrieve.py --search-mode lexical",
    )
    text = re.sub(
        r"(?m)^(\s*)kb_retrieve\.py\b",
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
        "`scripts/` and\n`.venv/` resolve correctly",
        "`scripts/` resolves correctly",
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
        "- In this WorkBuddy package, School/evidence queries use explicit hard-filtered\n"
        "  lexical retrieval (BM25 + exact aliases). Vector auto/hybrid/semantic modes\n"
        "  are unavailable; hard School/source boundaries are never widened.",
    )
    text = text.replace(
        "- `--search-mode auto|hybrid|semantic|lexical` (`auto` uses hybrid for v2;\n"
        "  lexical does not load the embedding model)",
        "- `--search-mode lexical` (required in this WorkBuddy package; vector\n"
        "  auto/hybrid/semantic modes require a separately managed local index)",
    )
    text = text.replace(
        "- `--layer canonical|school|evidence` (`canonical` is compatibility-only for strict routing)",
        "- `--layer school` or `--layer evidence` (the canonical fused-card layer is unavailable in this package)",
    )
    text = text.replace(
        "- Join multiple concepts with spaces to let vector search match related clusters",
        "- Join multiple concepts with spaces to improve lexical matching",
    )
    text = text.replace(
        "| `canonical` | `knowledge_base` | Backward-compatible fused-card exploration only; never use it to claim strict School/source isolation |",
        "| `canonical` | — | Unavailable in this WorkBuddy package; fused-card exploration requires a separately managed local-host checkout |",
    )
    text = text.replace(
        "An installation with only the legacy canonical collection\n"
        "remains usable for compatibility, but cannot satisfy a strict School/source\n"
        "route and must report that capability gap.",
        "This WorkBuddy package contains the attributable School and exact-source evidence\n"
        "layers but no legacy canonical collection. A canonical request must report that\n"
        "capability gap rather than approximating it.",
    )
    for maintainer_row in (
        "| `scripts/build_knowledge_v2.py` | Audit/export deterministic School projections and exact-source evidence |\n",
        "| `scripts/build_index.py` | Build canonical + independently embedded v2 collections; unchanged v2 documents reuse the local content cache |\n",
        "| `scripts/kb_doctor.py` | Environment health check (run if anything's broken) |\n",
    ):
        text = text.replace(maintainer_row, "")
    # ``SKILL.body.md`` is a repository-only composition input and is not
    # included in the WorkBuddy archive. Workflows should point readers back
    # to the already-loaded root instructions instead of leaving a dangling
    # source-tree reference.
    text = text.replace("`SKILL.body.md`", "the loaded root `SKILL.md`")
    text = text.replace("SKILL.body.md", "the loaded root SKILL.md")
    return text


def _localize_workbuddy_resource(
    text: str,
    archive_path: PurePosixPath,
    corpus_result: BuildResult | None = None,
) -> str:
    """Remove source-distribution claims that are false inside the ZIP package."""
    if archive_path == WORKBUDDY_ATTRIBUTION_PATH:
        text = (
            "# Third-Party Components and Attribution — WorkBuddy package\n\n"
            "OpenMobius-skill is licensed under Apache 2.0; see `LICENSE` in this\n"
            "directory. This package does not create an environment, install packages, or\n"
            "download a model/browser. The full local-host distribution can optionally use\n"
            "sentence-transformers (Apache-2.0), chromadb (Apache-2.0), NumPy\n"
            "(BSD-3-Clause), einops (MIT), Pillow (MIT-CMU), Playwright\n"
            "(Apache-2.0), and openai-python (Apache-2.0); none of those Python packages is\n"
            "redistributed in this ZIP.\n\n"
            "The Nomic `nomic-ai/nomic-embed-text-v1.5` model (Apache-2.0) and derived\n"
            "embedding seed are also omitted. **This WorkBuddy package deliberately omits**\n"
            "all vectors and the local Chroma index and makes no semantic-ranking claim.\n"
            "Chromium (BSD-style license) is not downloaded or bundled; chart rendering is\n"
            "available only if the host already provides Playwright and Chromium.\n\n"
            "The archive does bundle TradingView's `lightweight-charts` JavaScript library\n"
            "under Apache 2.0: <https://github.com/tradingview/lightweight-charts>. It is\n"
            "used locally to render K-line images.\n\n"
            "The source repository's 726 concept and 1,282 case cards are original\n"
            "schema-structured paraphrases of public educational material. They are not\n"
            "duplicated here; the checksummed compact corpus losslessly reconstructs their\n"
            "2,144 attributable School projections and 18,645 exact-source evidence\n"
            "records, preserving declared source-collection metadata while skipping\n"
            "content whose provenance cannot be assigned without inference.\n\n"
            "Model card: <https://huggingface.co/nomic-ai/nomic-embed-text-v1.5>.\n"
            "Chromium license: <https://chromium.googlesource.com/chromium/src/+/main/LICENSE>.\n"
        )

    if archive_path == WORKBUDDY_PRIVACY_PATH:
        text = (
            "# Privacy — WorkBuddy ZIP package\n\n"
            "The packaged Skill runs scripts locally in the WorkBuddy host. It contains\n"
            "bundled instructions, workflows, schemas, aliases, and a checksummed\n"
            "compact School/source-evidence corpus. It does not contain credentials, a\n"
            "virtual environment, local indexes, embedding/model caches, or installer\n"
            "state.\n\n"
            "Knowledge retrieval reads only bundled files. Market-data requests contact\n"
            "`api.mobiusquant.ai` (or an operator-selected `MOBIUS_API_BASE`) with the\n"
            "explicitly requested public asset, timeframe, indicator, and related query\n"
            "parameters; no authentication or credentials are collected. Chart rendering\n"
            "and annotation, when supported by host-provided dependencies, process data\n"
            "locally.\n\n"
            "The builder writes the ZIP to the path selected by its operator. Importing\n"
            "it through WorkBuddy's desktop UI, or submitting it to the Open Platform for\n"
            "publication, is a separate explicit action governed by WorkBuddy's privacy\n"
            "terms. Delete the local archive when it is no longer needed.\n"
        )

    if archive_path == WORKBUDDY_KB_README_PATH:
        tree_start = text.find("```\nknowledge_base/\n")
        tree_end = text.find("\n```", tree_start)
        if tree_start < 0 or tree_end < 0:
            raise ValueError(
                "knowledge_base/README.md inventory changed; update WorkBuddy localization",
            )
        compact_tree = (
            "```\n"
            "knowledge_base/\n"
            f"├── {COMPACT_V2_FILENAME} # 2,144 School + 18,645 evidence records\n"
            "├── schools.json                   # School aliases and capabilities\n"
            "├── term_aliases.json              # exact term/alias lookup\n"
            "└── schemas/                       # published v2 schemas\n"
            "```"
            + "\n\n> **WorkBuddy package note:** the ZIP deliberately omits `_index/`,\n"
            "  `_embedding_cache/`, `embedding_seed_v2/`, and canonical fused cards.\n"
            "  The verified compact corpus reconstructs the complete attributable School\n"
            "  and exact-source evidence layers for hard-filtered lexical search."
        )
        text = text[:tree_start] + compact_tree + text[tree_end + 4 :]

        section_start = text.find("## Build or upgrade\n")
        section_end = text.find("\n## Attribution\n", section_start)
        if section_start < 0 or section_end < 0:
            raise ValueError(
                "knowledge_base/README.md build section changed; update WorkBuddy localization",
            )
        package_runtime = (
            "## Runtime retrieval in this WorkBuddy package\n\n"
            "The package runtime does not contain or build a Chroma vector index, embedding\n"
            "cache, release seed, or canonical fused cards. Use the bundled deterministic\n"
            "compact/BM25 fallback with hard School and exact-source filters:\n\n"
            "```bash\n"
            ".venv/bin/python scripts/kb_retrieve.py \"Order Block\" \\\n"
            "  --layer school --schools ICT SMC --top-k 3\n"
            "```\n\n"
            "Do not run the local-host index builder, embedding-seed exporter, or semantic\n"
            "benchmark inside this package. Those maintainer workflows require a separately\n"
            "managed source checkout and runtime. Canonical and vector retrieval are\n"
            "unsupported here and must fail closed.\n"
        )
        text = text[:section_start] + package_runtime + text[section_end:]
        text = text.replace(
            "See `../ATTRIBUTION.md` for the project's full third-party attribution.",
            "See `@references/legal/ATTRIBUTION.md` for the project's full third-party "
            "attribution.",
        )

    if archive_path == WORKBUDDY_KB_SCHEMAS_README_PATH:
        text = (
            "# Knowledge model v2 — WorkBuddy package\n\n"
            "This package carries a checksummed compact representation that reconstructs\n"
            "all 2,144 `school_knowledge_v2` projections and all 18,645\n"
            "`source_evidence_v2` records for Python-standard-library-only lexical\n"
            "retrieval. Original `concepts/*.json`, `cases/*.json`, embedding seeds,\n"
            "vector indexes, and maintainer build tools are intentionally not bundled.\n\n"
            "The two JSON Schema files in this directory publish the full School and\n"
            "source-evidence payload contracts. Runtime records preserve their original\n"
            "parent `file_path` and exact source metadata, even though those source cards\n"
            "are not duplicated in the package. The canonical fused-card layer and vector\n"
            "retrieval are unavailable here and must fail closed.\n\n"
            "The attribution rules remain conservative: source evidence requires an\n"
            "exact declared School/source mapping, ambiguous fused material is skipped,\n"
            "and the legacy card-School-only exception is limited to the declared\n"
            "ChanLun import shape.\n"
        )

    text = _localize_markdown(text)
    if tuple(archive_path.parts[:3]) == (
        "skills",
        SKILL_SLUG,
        "references",
    ) and len(archive_path.parts) > 3 and archive_path.parts[3] == "workflows":
        text = WORKBUDDY_COMPACT_NOTICE + "\n" + text
    return _render_corpus_counts(text, corpus_result)


def _localize_python(payload: bytes, source: Path) -> bytes:
    """Point generated WorkBuddy scripts at references/knowledge_base/."""
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
        workbuddy_hint = (
            "            hint = (\n"
            "                \"This WorkBuddy compact package supports only explicit \"\n"
            "                \"lexical School/evidence retrieval; index recovery requires \"\n"
            "                \"a separate local-host source checkout.\"\n"
            "            )"
        )
        if local_index_hint not in text:
            raise ValueError(
                "retriever index hint changed; update WorkBuddy localization"
            )
        text = text.replace(local_index_hint, workbuddy_hint)
        text = text.replace(
            "请先跑：.venv/bin/python scripts/build_index.py",
            "This WorkBuddy package has no vector index; use explicit lexical mode",
        )
        text = text.replace(
            "scripts/build_index.py --force 重建",
            "a separate local-host source checkout to rebuild the index",
        )
        text = text.replace(
            "scripts/build_index.py 操作。",
            "local-host recovery（本 WorkBuddy package 不提供）。",
        )
        if "scripts/build_index.py" in text:
            raise ValueError(
                "WorkBuddy retriever still references an omitted index builder"
            )
    text = text.replace(
        'SKILL_DIR / "knowledge_base"',
        'SKILL_DIR / "references" / "knowledge_base"',
    )
    text = text.replace(
        "SKILL_DIR / 'knowledge_base'",
        "SKILL_DIR / 'references' / 'knowledge_base'",
    )
    text = text.replace(".venv/bin/python", "python3")
    text = text.replace(r".venv\Scripts\python.exe", "python3")
    text = text.replace(".venv/bin/pip", "python3 -m pip")
    text = text.replace(".venv/bin/playwright", "python3 -m playwright")
    text = text.replace(r".venv\Scripts\pip.exe", "python3 -m pip")
    text = text.replace(r".venv\Scripts\playwright.exe", "python3 -m playwright")
    text = text.replace(
        "或运行 `python scripts/kb_doctor.py` 做完整环境体检。",
        "请按上述 host 提示安装可用的 CJK 字体。",
    )
    text = "".join(
        line.replace("SKILL.body.md", "the loaded root SKILL.md")
        if line.lstrip().startswith("#")
        else line
        for line in text.splitlines(keepends=True)
    )
    return text.encode("utf-8")


def compose_workbuddy_skill(
    source_root: Path,
    *,
    corpus_result: BuildResult | None = None,
) -> str:
    """Compose WorkBuddy-localized frontmatter with the shared instructions."""
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(
            f"source root must be a regular directory, not a symlink: {source_root}"
        )
    source_root = source_root.resolve()
    frontmatter_path = source_root / "platforms" / "workbuddy.yaml"
    body_path = source_root / "SKILL.body.md"
    for source_input in (frontmatter_path, body_path):
        if source_input.is_symlink() or not source_input.is_file():
            raise ValueError(
                "WorkBuddy composition input must be a regular file: "
                f"{source_input}"
            )
        if not source_input.resolve().is_relative_to(source_root):
            raise ValueError(
                f"WorkBuddy composition input escapes source root: {source_input}"
            )
    if corpus_result is None:
        kb_dir = source_root / "knowledge_base"
        with knowledge_base_build_lock(kb_dir, mode="read"):
            assert_readable_generation(kb_dir)
            corpus_result = build_v2_records(kb_dir)
    frontmatter = frontmatter_path.read_text(encoding="utf-8").rstrip()
    validate_workbuddy_frontmatter(frontmatter)
    body = body_path.read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError("SKILL.body.md must not be empty")
    if not body.startswith("\n"):
        body = "\n" + body
    # Keep the source tree portable and localize only the generated artifact.
    body = _localize_markdown(body)
    portability_note = (
        "\n> **WorkBuddy package mode:** WorkBuddy accepts ZIP packages up to 3 MB, so "
        "this archive carries a verified compact corpus containing all 2,144 School "
        "projections and 18,645 exact-source evidence records, while omitting canonical "
        "fused cards and local vector-index artifacts. Knowledge retrieval must use "
        "explicit `--layer school` or `--layer evidence` with `--search-mode lexical` "
        "(BM25 + exact aliases + hard School/source filters). Canonical and vector "
        "modes are unsupported and "
        "must fail closed; do not approximate them or claim vector-semantic "
        "ranking. The host must provide Python 3.10+; this package does not create a "
        "virtual environment or install packages. If no usable Python launcher can be "
        "resolved, report script-backed knowledge and market operations unavailable. "
        "API/OHLCV "
        "steps use Python's standard library. PNG rendering additionally requires "
        "Playwright + Chromium, and image annotation requires Pillow; check those "
        "host capabilities before an artifact step, fail closed when absent, and "
        "do not install packages without user/administrator authorization.\n"
    )
    body = portability_note + body
    return _render_corpus_counts(
        f"---\n{frontmatter}\n---{body}",
        corpus_result,
    )


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
        and archive_path.parts[2] == "scripts"
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
                kb_relative.parts[0] in WORKBUDDY_OMITTED_KB_DIRECTORIES
                or kb_relative.as_posix() in WORKBUDDY_OMITTED_KB_FILES
            ):
                continue
        if (
            relative_root == "scripts"
            and relative.relative_to("scripts").as_posix()
            in WORKBUDDY_OMITTED_SCRIPT_FILES
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
    if tuple(raw_parts[:2]) != ("skills", SKILL_SLUG) or len(raw_parts) < 3:
        raise ValueError(f"archive path escapes the WorkBuddy skill root: {path_text}")
    if raw_parts[2] == "SKILL.md":
        if len(raw_parts) != 3:
            raise ValueError(f"invalid WorkBuddy entry path: {path_text}")
    elif raw_parts[2] not in WORKBUDDY_RESOURCE_DIRS:
        raise ValueError(f"unsupported WorkBuddy top-level entry: {path_text}")


def _validate_inventory(entries: Iterable[PackageEntry]) -> None:
    paths = [ARCHIVE_ROOT / "SKILL.md", *(entry.archive_path for entry in entries)]
    folded: dict[str, PurePosixPath] = {}
    for archive_path in paths:
        _validate_archive_path(archive_path)
        key = archive_path.as_posix().casefold()
        if key in folded:
            raise ValueError(
                "duplicate WorkBuddy archive path: "
                f"{folded[key].as_posix()} and {archive_path.as_posix()}",
            )
        folded[key] = archive_path

    all_paths = set(folded)
    for key, archive_path in folded.items():
        parts = archive_path.as_posix().split("/")
        for end in range(1, len(parts)):
            if "/".join(parts[:end]).casefold() in all_paths:
                raise ValueError(
                    f"archive file/directory path collision: {archive_path.as_posix()}",
                )


def package_entries(
    source_root: Path,
    *,
    excluded_sources: Iterable[Path] = (),
) -> list[PackageEntry]:
    """Return the deterministic, symlink-free WorkBuddy package inventory."""
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
                f"required WorkBuddy knowledge input must be a regular file: {required}"
            )
        if not required.resolve().is_relative_to(source_root):
            raise ValueError(f"WorkBuddy knowledge input escapes source root: {required}")
    excluded = frozenset(path.resolve() for path in excluded_sources)
    entries: list[PackageEntry] = []
    for name, archive_path in REFERENCE_FILES:
        path = source_root / name
        if path.is_file() and not path.is_symlink():
            entries.append(
                PackageEntry(path, archive_path, _entry_transform(archive_path)),
            )
    for name, archive_root in TREE_MAPPINGS:
        entries.extend(
            _tree_entries(
                source_root,
                name,
                archive_root,
                excluded_sources=excluded,
            ),
        )
    entries.append(
        PackageEntry(
            source_root / "knowledge_base",
            ARCHIVE_ROOT
            / "references"
            / "knowledge_base"
            / COMPACT_V2_FILENAME,
            "compact_v2",
        )
    )
    entries.append(
        PackageEntry(
            source_root / "knowledge_base",
            ARCHIVE_ROOT
            / "references"
            / "knowledge_base"
            / READ_ONLY_BUNDLE_MARKER,
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
    path_text = archive_path.as_posix()
    info = zipfile.ZipInfo(path_text, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, payload)


def _entry_payload(
    entry: PackageEntry,
    *,
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
            "term_aliases.json": (
                entry.source / "term_aliases.json"
            ).read_bytes(),
        }
        return encode_read_only_bundle_marker(payloads)
    payload = entry.source.read_bytes()
    if entry.transform == "raw":
        return payload
    if entry.transform == "python":
        return _localize_python(payload, entry.source)
    if entry.transform == "markdown":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Markdown resource is not UTF-8: {entry.source}") from exc
        return _localize_workbuddy_resource(
            text,
            entry.archive_path,
            corpus_result,
        ).encode("utf-8")
    raise ValueError(f"unknown WorkBuddy package transform: {entry.transform}")


def _validate_output_location(source_root: Path, output: Path) -> None:
    if output.suffix.casefold() != ".zip":
        raise ValueError(f"WorkBuddy package output must use a .zip suffix: {output}")
    try:
        relative = output.relative_to(source_root)
    except ValueError:
        return
    packaged_roots = {source for source, _destination in TREE_MAPPINGS}
    protected_files = {
        "SKILL.body.md",
        "platforms/workbuddy.yaml",
        *(source for source, _destination in REFERENCE_FILES),
    }
    if (
        relative.as_posix() in protected_files
        or (relative.parts and relative.parts[0] in packaged_roots)
    ):
        raise ValueError(f"output would overwrite or enter packaged source data: {output}")


def build_package(source_root: Path, output: Path, *, force: bool = False) -> dict:
    """Create a deterministic WorkBuddy ZIP package and return its summary."""
    source_root = Path(source_root)
    if source_root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {source_root}")
    source_root = source_root.resolve()
    requested_output = Path(output)
    if requested_output.is_symlink():
        raise ValueError(
            f"WorkBuddy package output must not be a symlink: {requested_output}"
        )
    # Resolve only the parent. Resolving an existing leaf symlink would turn
    # the later atomic replace into an overwrite of the link target.
    output = requested_output.parent.resolve() / requested_output.name
    with knowledge_base_build_lock(
        source_root / "knowledge_base",
        mode="read",
    ):
        assert_readable_generation(source_root / "knowledge_base")
        corpus_result = build_v2_records(source_root / "knowledge_base")
        # Serialize producers of the same destination independently of their
        # source roots. This closes the force=False check/replace race while
        # retaining atomic replacement for force=True.
        with knowledge_base_build_lock(output, mode="write"):
            if output.is_symlink():
                raise ValueError(
                    f"WorkBuddy package output must not be a symlink: {output}"
                )
            _validate_output_location(source_root, output)
            if output.exists() and not force:
                raise FileExistsError(
                    f"output already exists (use --force): {output}"
                )
            output.parent.mkdir(parents=True, exist_ok=True)

            skill_md = compose_workbuddy_skill(
                source_root,
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
                            _entry_payload(entry, corpus_result=corpus_result),
                            executable=(
                                entry.transform == "python"
                                and bool(mode & stat.S_IXUSR)
                            ),
                        )
                archive_size = temporary_path.stat().st_size
                if archive_size > WORKBUDDY_MAX_ARCHIVE_BYTES:
                    raise ValueError(
                        "WorkBuddy ZIP package exceeds the documented 3 MB size "
                        f"limit: {archive_size} bytes > "
                        f"{WORKBUDDY_MAX_ARCHIVE_BYTES} bytes"
                    )
                if output.exists() and not force:
                    raise FileExistsError(
                        f"output already exists (use --force): {output}"
                    )
                if output.is_symlink():
                    raise ValueError(
                        f"WorkBuddy package output must not be a symlink: {output}"
                    )
                temporary_path.replace(output)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    return {
        "output": str(output),
        "archive_root": ARCHIVE_ROOT.as_posix(),
        "files": len(entries) + 1,
        "bytes": output.stat().st_size,
        "max_bytes": WORKBUDDY_MAX_ARCHIVE_BYTES,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a local WorkBuddy skills/<name>/SKILL.md ZIP package for "
            "explicit UI import or Open Platform publication submission; this "
            "command does not upload, import, install, submit, or publish it"
        ),
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
