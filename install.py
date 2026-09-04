#!/usr/bin/env python3
"""OpenMobius-skill — cross-platform installer.

Runs on macOS / Linux / Windows. Requires only Python 3.10+ pre-installed.

What this script does:
  - Copies the runtime payload into the selected agent skill directory when
    installing outside the source checkout.
  - Creates a Python virtual environment at <skill-dir>/.venv/
  - Installs declared dependencies from requirements.txt (third-party
    OSS libraries: sentence-transformers, chromadb, playwright, etc. —
    see ATTRIBUTION.md for full list)
  - Downloads the open-source `nomic-ai/nomic-embed-text-v1.5` embedding
    model (Apache 2.0) from HuggingFace Hub's official URL.
  - Builds a local ChromaDB vector index of the bundled knowledge-base
    JSON cards.
  - Generates the platform-specific SKILL.md in the selected skill directory
    (e.g. ~/.claude/skills/openmobius-skill/).
  - Runs a health check.

What this script does NOT do:
  - No unrelated network traffic. Selected steps may contact the package index
    configured for pip (normally pypi.org / files.pythonhosted.org),
    huggingface.co and its model-download CDN, Playwright's browser-download
    hosts, api.mobiusquant.ai (doctor connectivity check), and github.com
    (--update). These are documented in PRIVACY.md.
  - No intentional persistent project data outside <skill-dir>. Dependency
    tools may use their configured pip/Hugging Face caches, Playwright's
    per-OS browser cache, and short-lived OS temporary directories, as
    documented in PRIVACY.md.
  - No background processes, daemons, startup entries, or system hooks.
  - No collection or transmission of user data, credentials, or telemetry.

Usage:
  python3 install.py                  # default (Windows: py -3 install.py)
  python3 install.py --strict         # CI mode: fail fast
  python3 install.py -i               # deprecated compatibility no-op
  python3 install.py --resume         # skip already-done steps (default ON)
  python3 install.py --no-register    # skip platform SKILL.md generation
  python3 install.py -v               # verbose
  python3 install.py --uninstall      # see install.py --uninstall --help
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from scripts._lib.build_lock import (
    BuildLockUnavailable,
    INSTALL_GENERATION_MARKER,
    knowledge_base_build_lock,
    knowledge_base_lock_root,
)


# ============================================================================
# Constants & platform detection
# ============================================================================

DISPLAY_NAME = "OpenMobius-skill"
SKILL_SLUG = "openmobius-skill"

# Backward-compatible module name used by callers when constructing an install
# directory. It intentionally points at the standards-compliant slug now.
SKILL_NAME = SKILL_SLUG
LEGACY_SKILL_NAME = DISPLAY_NAME

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LIN = platform.system() == "Linux"

# SOURCE_DIR is where this install.py lives (the clone). Read-only; we copy
# from here into the per-platform install target. Never rebound.
SOURCE_DIR = Path(__file__).resolve().parent

# SKILL_DIR is the install *target* — venv, _index, and SKILL.md live here.
# Defaults to SOURCE_DIR (in-place install). For standalone install (the
# common case: clone is ephemeral, target is the agent's skills dir),
# _rebind_paths_to(target) re-points the install-dir-relative globals below.
SKILL_DIR = SOURCE_DIR

VENV_DIR = SKILL_DIR / ".venv"
VENV_PY  = (VENV_DIR / ("Scripts" if IS_WIN else "bin") /
            ("python.exe" if IS_WIN else "python"))
VENV_PIP = (VENV_DIR / ("Scripts" if IS_WIN else "bin") /
            ("pip.exe" if IS_WIN else "pip"))
INDEX_FILE    = SKILL_DIR / "knowledge_base" / "_index" / "chroma.sqlite3"
PLATFORMS_DIR = SKILL_DIR / "platforms"
SKILL_BODY_MD = SKILL_DIR / "SKILL.body.md"

# ─── Multi-platform skill registration ──────────────────────────────────────
@dataclass(frozen=True)
class PlatformSpec:
    """Install and discovery contract for one supported agent host."""

    display_name: str
    default_root: Optional[tuple[str, ...]]
    skill_parent: tuple[str, ...] = ("skills",)
    detect_root: Optional[tuple[str, ...]] = None
    root_env: Optional[str] = None
    auto_detect: bool = True
    legacy_root: Optional[tuple[str, ...]] = None
    legacy_skill_parent: tuple[str, ...] = ("skills",)
    explicit_target_help: Optional[str] = None
    supported_host_systems: Optional[tuple[str, ...]] = None

    @staticmethod
    def _home(home: Optional[Path]) -> Path:
        return Path.home() if home is None else Path(home)

    def _configured_root(
        self,
        *,
        home: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional[Path]:
        env = os.environ if environ is None else environ
        if self.root_env:
            configured = env.get(self.root_env, "").strip()
            if configured:
                return Path(configured).expanduser()
        if self.default_root is None:
            return None
        return self._home(home).joinpath(*self.default_root)

    def default_target(
        self,
        *,
        home: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional[Path]:
        root = self._configured_root(home=home, environ=environ)
        if root is None:
            return None
        return root.joinpath(*self.skill_parent, SKILL_SLUG)

    def detection_root(
        self,
        *,
        home: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional[Path]:
        if not self.auto_detect:
            return None
        env = os.environ if environ is None else environ
        if self.root_env and env.get(self.root_env, "").strip():
            return self._configured_root(home=home, environ=env)
        parts = self.detect_root if self.detect_root is not None else self.default_root
        if parts is None:
            return None
        return self._home(home).joinpath(*parts)

    def legacy_target(
        self,
        *,
        home: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional[Path]:
        if self.legacy_root is None:
            return None

        # OpenClaw and Hermes relocate both their current and historical skill
        # directories when their documented state/home variable is set. Do not
        # apply this to Codex: its current target is under ~/.agents while the
        # historical uppercase target deliberately remains under ~/.codex.
        env = os.environ if environ is None else environ
        if self.root_env and env.get(self.root_env, "").strip():
            root = self._configured_root(home=home, environ=env)
        else:
            root = self._home(home).joinpath(*self.legacy_root)
        if root is None:
            return None
        return root.joinpath(
            *self.legacy_skill_parent,
            LEGACY_SKILL_NAME,
        )


WORKBUDDY_TARGET_HELP = (
    "WorkBuddy does not publish a fixed filesystem directory that a third-party "
    "installer can write to for automatic discovery. Pass --target-dir only "
    "to create an explicitly selected developer staging tree. For local use, "
    "build the ZIP and import it through Skills > Add Skill > Upload Skill; "
    "Open Platform submission is a separate publishing workflow. "
    "The installer will not guess a ~/.workbuddy path."
)


# Single source of truth for choices, display names, targets and detection.
# WorkBuddy is explicit-only because its public docs do not define a fixed
# third-party-writable automatic-discovery target. Its explicit target is a
# developer staging tree, not a WorkBuddy installation.
PLATFORM_REGISTRY: dict[str, PlatformSpec] = {
    "claude-code": PlatformSpec(
        display_name="Claude Code",
        default_root=(".claude",),
        legacy_root=(".claude",),
    ),
    "codex": PlatformSpec(
        display_name="Codex",
        default_root=(".agents",),
        detect_root=(".codex",),
        legacy_root=(".codex",),
    ),
    "openclaw": PlatformSpec(
        display_name="OpenClaw",
        default_root=(".openclaw",),
        root_env="OPENCLAW_STATE_DIR",
        legacy_root=(".openclaw",),
        supported_host_systems=("Linux", "Darwin"),
    ),
    "hermes": PlatformSpec(
        display_name="Hermes",
        default_root=(".hermes",),
        skill_parent=("skills", "market-data"),
        root_env="HERMES_HOME",
        legacy_root=(".hermes",),
        legacy_skill_parent=("skills", "market-data"),
        supported_host_systems=("Linux", "Darwin"),
    ),
    "cursor": PlatformSpec(
        display_name="Cursor",
        default_root=(".cursor",),
    ),
    "workbuddy": PlatformSpec(
        display_name="WorkBuddy",
        default_root=None,
        auto_detect=False,
        explicit_target_help=WORKBUDDY_TARGET_HELP,
    ),
}

PLATFORM_NAMES = tuple(PLATFORM_REGISTRY)
PLATFORM_DISPLAY_NAMES = {
    name: spec.display_name for name, spec in PLATFORM_REGISTRY.items()
}

# Backward-compatible public mapping. It contains only hosts with a documented
# default local automatic-discovery target; WorkBuddy developer staging must
# always be resolved explicitly.
PLATFORM_DEFAULTS = {
    name: target
    for name, spec in PLATFORM_REGISTRY.items()
    if (target := spec.default_target()) is not None
}
DEFAULT_TARGET_PLATFORMS = tuple(PLATFORM_DEFAULTS)


def _rebind_paths_to(target: Path) -> None:
    """Re-point install-dir-relative globals to a new target.

    Called when running a standalone install (target != SOURCE_DIR).
    User-global caches (HF_HUB_CACHE, PW_CACHE, NOMIC_CACHE_DIR) are NOT
    rebound — they're shared across platforms by design.
    """
    global SKILL_DIR, VENV_DIR, VENV_PY, VENV_PIP
    global INDEX_FILE, PLATFORMS_DIR, SKILL_BODY_MD
    SKILL_DIR     = target
    VENV_DIR      = SKILL_DIR / ".venv"
    VENV_PY       = (VENV_DIR / ("Scripts" if IS_WIN else "bin") /
                     ("python.exe" if IS_WIN else "python"))
    VENV_PIP      = (VENV_DIR / ("Scripts" if IS_WIN else "bin") /
                     ("pip.exe" if IS_WIN else "pip"))
    INDEX_FILE    = SKILL_DIR / "knowledge_base" / "_index" / "chroma.sqlite3"
    PLATFORMS_DIR = SKILL_DIR / "platforms"
    SKILL_BODY_MD = SKILL_DIR / "SKILL.body.md"


# Files/dirs copied from SOURCE_DIR to TARGET_DIR during standalone install.
COPY_ENTRIES = [
    # core (required at runtime)
    "agents", "scripts", "workflows", "knowledge_base", "platforms", "evals", "docs",
    "SKILL.body.md", "requirements.txt", "install.py",
    # docs (useful in-target for reference / re-update)
    "README.md", "README.zh.md", "INSTALL.md",
    "ATTRIBUTION.md", "PRIVACY.md", "LICENSE",
    # shell wrappers
    "install.sh", "install.ps1",
    # gitignore (for completeness)
    ".gitignore",
]
# Excluded from copy (artifacts / per-install rebuilds / secrets)
COPY_EXCLUDE = {
    "__pycache__", ".venv", "_index", "_embedding_cache",
    ".env.local", ".env.local.example",
    INSTALL_GENERATION_MARKER,
    ".DS_Store", ".git", "node_modules", ".pytest_cache",
}
COPY_EXCLUDE_PREFIXES = (
    "._index.build-",
    "._index.backup-",
    "._cards.build-",
    "._cards.backup-",
    ".embedding_seed_v2.build-",
    ".embedding_seed_v2.backup-",
)

INSTALL_STATE_FILE = ".openmobius-install.json"
INSTALL_STATE_VERSION = 1
STAGING_STATE_FILE = ".openmobius-staging.json"
STAGING_STATE_VERSION = 1
INSTALL_GENERATION_MARKER_VERSION = 1
GENERATED_MANAGED_FILES = {"SKILL.md"}


def _path_exists(path: Path) -> bool:
    """Return True for regular paths and dangling symlinks."""
    return path.exists() or path.is_symlink()


def _is_copy_excluded(name: str) -> bool:
    return (
        name in COPY_EXCLUDE
        or name.endswith(".pyc")
        or name.startswith(COPY_EXCLUDE_PREFIXES)
    )


def _validate_release_source(src: Path) -> list[str]:
    """Validate and inventory the complete release-owned source payload.

    Installer payloads never contain symlinks. Following one while copying a
    freshly cloned release could copy data outside the checkout, so any link
    is a hard failure. All declared top-level entries are required: silently
    accepting a partial checkout would otherwise make an update delete valid
    installed files.
    """
    missing = [entry for entry in COPY_ENTRIES if not _path_exists(src / entry)]
    if missing:
        raise FileNotFoundError(
            "incomplete installer source; missing release entries: "
            + ", ".join(missing)
        )

    owned_files: list[str] = []

    def visit(path: Path, relative: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"release payload contains a symlink: {relative}")
        if path.is_dir():
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if _is_copy_excluded(child.name):
                    continue
                visit(child, relative / child.name)
            return
        if not path.is_file():
            raise ValueError(f"release payload contains a non-regular file: {relative}")
        owned_files.append(relative.as_posix())

    for entry in COPY_ENTRIES:
        visit(src / entry, Path(entry))
    return sorted(owned_files)


def _load_install_state(target: Path, *, strict: bool = False) -> Optional[dict]:
    marker = target / INSTALL_STATE_FILE
    if not _path_exists(marker):
        return None
    try:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("marker is not a regular file")
        state = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("marker root is not an object")
        if state.get("schema_version") != INSTALL_STATE_VERSION:
            raise ValueError("unsupported marker schema")
        if state.get("skill") != SKILL_SLUG:
            raise ValueError("marker belongs to another skill")
        owned = state.get("owned_files")
        if not isinstance(owned, list) or not all(
            isinstance(value, str) and value for value in owned
        ):
            raise ValueError("owned_files is invalid")
        for value in owned:
            candidate = Path(value)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"unsafe owned path: {value!r}")
        return state
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if strict:
            raise ValueError(f"invalid installer ownership marker {marker}: {exc}") from exc
        return None


def _skill_frontmatter_identifies_target(target: Path) -> bool:
    skill_file = target / "SKILL.md"
    try:
        if skill_file.is_symlink() or not skill_file.is_file():
            return False
        text = skill_file.read_text(encoding="utf-8")[:16384]
    except (OSError, UnicodeError):
        return False
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---", 4)
    if end < 0:
        return False
    frontmatter = text[4:end]
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\s]+)", frontmatter)
    return bool(match and match.group(1) in {SKILL_SLUG, LEGACY_SKILL_NAME})


def _target_is_owned_install(target: Path) -> bool:
    """Recognize current marker-based and pre-marker OpenMobius installs."""
    marker = target / INSTALL_STATE_FILE
    if _path_exists(marker):
        return _load_install_state(target, strict=False) is not None
    return _skill_frontmatter_identifies_target(target)


def _write_install_state(
    staging: Path,
    *,
    platform_name: str,
    owned_files: list[str],
) -> None:
    state = {
        "schema_version": INSTALL_STATE_VERSION,
        "skill": SKILL_SLUG,
        "platform": platform_name,
        "owned_files": sorted(set(owned_files) | GENERATED_MANAGED_FILES),
    }
    marker = staging / INSTALL_STATE_FILE
    marker.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability where the platform supports it."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_install_generation_marker(
    staging: Path,
    target: Path,
    *,
    platform_name: str,
    generation_id: str,
) -> None:
    """Stage a durable fail-closed marker before new cards become live."""
    marker = staging / "knowledge_base" / INSTALL_GENERATION_MARKER
    if _path_exists(marker):
        raise RuntimeError(f"unexpected installer generation marker: {marker}")
    payload = {
        "schema_version": INSTALL_GENERATION_MARKER_VERSION,
        "skill": SKILL_SLUG,
        "platform": platform_name,
        "target": str(target),
        "generation_id": generation_id,
    }
    with marker.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(marker.parent)


def _read_install_generation_marker(target: Path) -> dict:
    marker = target / "knowledge_base" / INSTALL_GENERATION_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"installer generation marker is not a regular file: {marker}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid installer generation marker {marker}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != INSTALL_GENERATION_MARKER_VERSION
        or payload.get("skill") != SKILL_SLUG
        or payload.get("target") != str(target)
        or payload.get("platform") not in PLATFORM_REGISTRY
        or not isinstance(payload.get("generation_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", payload["generation_id"])
    ):
        raise ValueError(f"installer generation marker has invalid identity: {marker}")
    return payload


def _installer_operation_resource(target: Path) -> Path:
    """Return a stable logical resource key for one managed target."""
    target = target.resolve(strict=False)
    return target.parent / f".{target.name}.openmobius-install-operation"


def _complete_install_generation(
    target: Path,
    *,
    expected_generation_id: str,
) -> bool:
    """Remove the pending marker only after a structurally verified v2 index."""
    target = target.resolve(strict=False)
    kb_dir = target / "knowledge_base"
    marker = kb_dir / INSTALL_GENERATION_MARKER
    if not _path_exists(marker):
        raise ValueError(f"installer generation marker is missing: {marker}")
    with knowledge_base_build_lock(
        _installer_operation_resource(target),
        mode="write",
    ):
        with knowledge_base_build_lock(kb_dir, mode="write"):
            marker_payload = _read_install_generation_marker(target)
            if marker_payload["generation_id"] != expected_generation_id:
                raise ValueError(
                    "installer generation changed before completion; refusing "
                    "to clear another operation's fail-closed marker"
                )
            database = kb_dir / "_index" / "chroma.sqlite3"
            manifest_path = kb_dir / "_index" / "index_manifest.json"
            if (
                database.is_symlink()
                or not database.is_file()
                or manifest_path.is_symlink()
                or not manifest_path.is_file()
            ):
                raise ValueError(
                    "refusing to complete install generation without a regular "
                    "Chroma database and v2 index manifest"
                )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid installed index manifest: {exc}") from exc
            sha256 = re.compile(r"^[0-9a-f]{64}$")
            if (
                not isinstance(manifest, dict)
                or manifest.get("manifest_version") != 2
                or manifest.get("index_schema_version") != 3
                or not sha256.fullmatch(
                    str(manifest.get("v2_input_fingerprint", ""))
                )
                or not sha256.fullmatch(
                    str(manifest.get("canonical_input_fingerprint", ""))
                )
            ):
                raise ValueError(
                    "refusing to complete install generation with an unverifiable "
                    "v2 index manifest"
                )
            collections = manifest.get("collections")
            if not isinstance(collections, dict):
                raise ValueError(
                    "refusing to complete install generation without v2 "
                    "collection metadata"
                )
            for collection_name, layer in (
                ("school_knowledge_v2", "school"),
                ("source_evidence_v2", "evidence"),
            ):
                details = collections.get(collection_name)
                if (
                    not isinstance(details, dict)
                    or details.get("schema_version") != 3
                    or details.get("layer") != layer
                    or details.get("created") is not True
                    or not isinstance(details.get("count"), int)
                    or details["count"] <= 0
                ):
                    raise ValueError(
                        "refusing to complete install generation without a "
                        f"populated {collection_name} collection"
                    )

            # The index builder validates Chroma identities before promotion.
            # Independently reject a truncated/corrupt SQLite file at this
            # final installer boundary using only Python's standard library.
            import sqlite3  # noqa: PLC0415

            connection = None
            try:
                connection = sqlite3.connect(
                    database.resolve().as_uri() + "?mode=ro",
                    uri=True,
                )
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
            except sqlite3.Error as exc:
                raise ValueError(
                    "refusing to complete install generation with an invalid "
                    f"Chroma database: {exc}"
                ) from exc
            finally:
                if connection is not None:
                    connection.close()
            if quick_check != ("ok",):
                raise ValueError(
                    "refusing to complete install generation with a corrupt "
                    f"Chroma database: {quick_check!r}"
                )
            marker.unlink()
            _fsync_directory(kb_dir)
    return True


def _write_staging_state(
    staging: Path,
    target: Path,
    transaction_id: str,
    *,
    preserved_paths: tuple[Path, ...] = (),
) -> None:
    (staging / STAGING_STATE_FILE).write_text(
        json.dumps(
            {
                "schema_version": STAGING_STATE_VERSION,
                "skill": SKILL_SLUG,
                "target": str(target),
                "transaction_id": transaction_id,
                "preserved_paths": [path.as_posix() for path in preserved_paths],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_staging_state(path: Path, target: Path) -> Optional[dict]:
    marker = path / STAGING_STATE_FILE
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    transaction_id = state.get("transaction_id")
    preserved_paths = state.get("preserved_paths")
    if (
        state.get("schema_version") != STAGING_STATE_VERSION
        or state.get("skill") != SKILL_SLUG
        or state.get("target") != str(target)
        or not isinstance(transaction_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", transaction_id)
        or not isinstance(preserved_paths, list)
    ):
        return None
    for value in preserved_paths:
        if not isinstance(value, str) or not value:
            return None
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            return None
    return state


def _owned_paths_from_state(target: Path) -> Optional[set[str]]:
    state = _load_install_state(target, strict=True)
    if state is None:
        return None
    return set(state["owned_files"])


def _collect_preserved_paths(target: Path, old_owned: Optional[set[str]]) -> list[Path]:
    """Find target content not owned by a release update.

    New marker-based installs preserve user-added files even when they live
    below a managed directory. Pre-marker installs conservatively mirror the
    declared COPY_ENTRIES, while still preserving top-level user content and
    the documented runtime directories (.venv, _index, _embedding_cache).
    """
    managed_roots = set(COPY_ENTRIES) | {INSTALL_STATE_FILE, *GENERATED_MANAGED_FILES}
    preserved: list[Path] = []

    def has_owned_descendant(relative: Path) -> bool:
        prefix = relative.as_posix().rstrip("/") + "/"
        return bool(old_owned and any(value.startswith(prefix) for value in old_owned))

    def visit(path: Path, relative: Path) -> None:
        # A preserved runtime directory may legitimately contain interpreter
        # symlinks. Its root itself must still be a real directory.
        if relative == Path("knowledge_base") / INSTALL_GENERATION_MARKER:
            # This is installer-owned recovery state, never user content. A
            # retry stages a fresh marker for the new generation.
            return
        excluded = _is_copy_excluded(path.name)
        if path.is_symlink():
            raise ValueError(f"installed target contains a symlink: {relative}")
        if excluded:
            preserved.append(relative)
            return
        if relative.as_posix() == "SKILL.md":
            # Keep the last working registration during dependency/index work;
            # register_skill() replaces it after all preceding steps succeed.
            preserved.append(relative)
            return

        if old_owned is None:
            if relative.parts[0] not in managed_roots:
                preserved.append(relative)
                return
            if path.is_dir():
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    visit(child, relative / child.name)
            return

        rel_text = relative.as_posix()
        if path.is_file():
            if rel_text not in old_owned and rel_text != INSTALL_STATE_FILE:
                preserved.append(relative)
            return
        if not path.is_dir():
            raise ValueError(f"installed target contains a non-regular file: {relative}")
        if rel_text not in old_owned and not has_owned_descendant(relative):
            preserved.append(relative)
            return
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            visit(child, relative / child.name)

    for child in sorted(target.iterdir(), key=lambda item: item.name):
        if child.name == INSTALL_STATE_FILE:
            continue
        visit(child, Path(child.name))
    return preserved


def _copy_payload_into_staging(src: Path, staging: Path) -> int:
    def _ignore(_dir, names):
        return [name for name in names if _is_copy_excluded(name)]

    for entry in COPY_ENTRIES:
        src_path = src / entry
        dst_path = staging / entry
        if src_path.is_dir():
            shutil.copytree(src_path, dst_path, ignore=_ignore)
        else:
            shutil.copy2(src_path, dst_path)
    return len(COPY_ENTRIES)


def _new_backup_path(target: Path, transaction_id: str) -> Path:
    candidate = target.parent / f".{target.name}.backup-{transaction_id}"
    if _path_exists(candidate):
        raise FileExistsError(f"refusing to reuse installer backup path: {candidate}")
    return candidate


def _transaction_id(path: Path, kind: str, target: Path) -> Optional[str]:
    prefix = f".{target.name}.{kind}-"
    if not path.name.startswith(prefix):
        return None
    value = path.name[len(prefix):]
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def _recover_copy_transaction(target: Path) -> None:
    """Recover or fail closed on an interrupted directory replacement."""
    stages = sorted(target.parent.glob(f".{target.name}.stage-*"))
    backups = sorted(target.parent.glob(f".{target.name}.backup-*"))
    if len(stages) > 1 or len(backups) > 1:
        raise RuntimeError(
            "multiple installer transaction artifacts require manual review: "
            + ", ".join(str(path) for path in stages + backups)
        )

    if backups:
        backup = backups[0]
        transaction_id = _transaction_id(backup, "backup", target)
        if transaction_id is None or not _target_is_owned_install(backup):
            raise RuntimeError(f"unverified installer backup requires manual review: {backup}")
        matching_stage = stages[0] if stages else None
        if matching_stage is not None:
            state = _read_staging_state(matching_stage, target)
            if not state or state["transaction_id"] != transaction_id:
                raise RuntimeError(
                    f"installer staging/backup transaction mismatch: "
                    f"{matching_stage}, {backup}"
                )

        if not _path_exists(target):
            if matching_stage is None:
                raise RuntimeError(
                    f"installer backup has no matching transaction journal; "
                    f"manual review required: {backup}"
                )
            assert state is not None
            # A crash may have happened between any two preserved-path
            # renames. Infer each move from the two transaction directories,
            # put it back, then restore the now-complete old target.
            for value in reversed(state["preserved_paths"]):
                relative = Path(value)
                old_path = backup / relative
                staged_path = matching_stage / relative
                old_exists = _path_exists(old_path)
                staged_exists = _path_exists(staged_path)
                if old_exists == staged_exists:
                    raise RuntimeError(
                        f"cannot recover preserved installer path {relative}: "
                        f"expected it in exactly one transaction directory"
                    )
                if staged_exists:
                    old_path.parent.mkdir(parents=True, exist_ok=True)
                    staged_path.replace(old_path)
            backup.replace(target)
            shutil.rmtree(matching_stage)
            return

        live_state = _read_staging_state(target, target)
        if (
            not live_state
            or live_state["transaction_id"] != transaction_id
            or not _target_is_owned_install(target)
            or matching_stage is not None
        ):
            raise RuntimeError(
                f"both live target and backup exist without a verified commit; "
                f"manual review required: {target}, {backup}"
            )
        for value in live_state["preserved_paths"]:
            relative = Path(value)
            if not _path_exists(target / relative) or _path_exists(backup / relative):
                raise RuntimeError(
                    f"committed installer transaction has inconsistent preserved "
                    f"path {relative}; manual review required"
                )
        # The staged directory was promoted and retains the matching commit
        # marker; finish the interrupted old-backup cleanup.
        shutil.rmtree(backup)
        (target / STAGING_STATE_FILE).unlink()
        return

    if _path_exists(target) and (target / STAGING_STATE_FILE).is_file():
        # Fresh-install promotion, or cleanup completed immediately before a
        # crash. With no backup, the verified live marker is authoritative.
        if not _read_staging_state(target, target) or not _target_is_owned_install(target):
            raise RuntimeError(f"invalid live installer transaction marker: {target}")
        (target / STAGING_STATE_FILE).unlink()

    if stages:
        state = _read_staging_state(stages[0], target)
        if not state:
            raise RuntimeError(
                f"unverified installer staging directory requires manual review: {stages[0]}"
            )
        raise RuntimeError(
            f"interrupted installer staging directory requires manual review: {stages[0]}"
        )


def _validate_preserved_path_conflicts(
    staging: Path,
    preserved: list[Path],
) -> None:
    for relative in preserved:
        new_path = staging / relative
        if _path_exists(new_path):
            raise FileExistsError(
                f"user-owned path conflicts with new release payload: {relative}"
            )


def _rollback_preserved_moves(
    staging: Path,
    backup: Path,
    preserved: list[Path],
) -> None:
    for relative in reversed(preserved):
        staged_path = staging / relative
        backup_path = backup / relative
        if _path_exists(staged_path) and not _path_exists(backup_path):
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            staged_path.replace(backup_path)


def _promote_staged_target(staging: Path, target: Path) -> None:
    """Atomically switch a staged payload into place, rolling back on error."""
    state = _read_staging_state(staging, target)
    if not state:
        raise RuntimeError(f"staging directory has no valid transaction marker: {staging}")
    transaction_id = state["transaction_id"]
    if not _path_exists(target):
        staging.replace(target)
        (target / STAGING_STATE_FILE).unlink()
        return

    backup = _new_backup_path(target, transaction_id)
    target.replace(backup)
    preserved = [Path(value) for value in state["preserved_paths"]]
    try:
        for relative in preserved:
            old_path = backup / relative
            new_path = staging / relative
            if not _path_exists(old_path) or _path_exists(new_path):
                raise RuntimeError(
                    f"preserved installer path changed during promotion: {relative}"
                )
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.replace(new_path)
        staging.replace(target)
    except BaseException as promotion_error:
        try:
            _rollback_preserved_moves(staging, backup, preserved)
            if not _path_exists(target) and _path_exists(backup):
                backup.replace(target)
        except BaseException as rollback_error:
            # User/runtime data may now be split between staging and backup.
            # Keep both trees for deterministic recovery/manual inspection;
            # copy_source_to_target deliberately refuses to delete staging
            # while the matching backup still exists.
            raise RuntimeError(
                "installer promotion and rollback both failed; recovery "
                f"evidence was retained at {staging} and {backup}: "
                f"{rollback_error}"
            ) from promotion_error
        raise

    # Keep the commit marker until the complete old target is gone. If the
    # process stops here, _recover_copy_transaction can unambiguously finish.
    shutil.rmtree(backup)
    (target / STAGING_STATE_FILE).unlink()


def copy_source_to_target(
    src: Path,
    dst: Path,
    *,
    platform_name: str = "unknown",
) -> int:
    """Copy clone source into the per-platform target install dir.

    Returns the number of top-level entries copied. The live directory is not
    touched until a complete payload has been staged. Release-owned files are
    mirrored (so upstream deletions disappear), while runtime state and
    user-owned files are snapshotted across the atomic directory replacement.
    """
    src = src.resolve()
    dst = dst.expanduser().resolve(strict=False)
    if dst == src or dst.is_relative_to(src) or src.is_relative_to(dst):
        raise ValueError(
            f"unsafe copy target overlaps the source checkout: {dst}"
        )
    # A package/update must see one complete source generation and must not
    # replace a target while any retriever or builder is using its generation.
    # The lock files live outside both trees, so directory promotion cannot
    # replace the inode carrying the lease.
    with ExitStack() as leases:
        leases.enter_context(
            knowledge_base_build_lock(
                _installer_operation_resource(dst),
                mode="write",
            )
        )
        if (src / "knowledge_base").is_dir():
            leases.enter_context(
                knowledge_base_build_lock(
                    src / "knowledge_base",
                    mode="read",
                )
            )
        leases.enter_context(
            knowledge_base_build_lock(
                dst / "knowledge_base",
                mode="write",
            )
        )

        dst.parent.mkdir(parents=True, exist_ok=True)
        _recover_copy_transaction(dst)
        if _path_exists(dst):
            if dst.is_symlink() or not dst.is_dir():
                raise ValueError(f"install target must be a real directory: {dst}")
            if any(dst.iterdir()) and not _target_is_owned_install(dst):
                raise ValueError(
                    f"refusing to overwrite non-empty directory without verified "
                    f"OpenMobius ownership: {dst}"
                )

        owned_files = _validate_release_source(src)
        old_owned = _owned_paths_from_state(dst) if _path_exists(dst) else None
        preserved = (
            _collect_preserved_paths(dst, old_owned)
            if _path_exists(dst)
            else []
        )

        staging = Path(
            tempfile.mkdtemp(prefix=f".{dst.name}.stage-", dir=dst.parent)
        )
        transaction_id = _transaction_id(staging, "stage", dst)
        if transaction_id is None:  # pragma: no cover - tempfile contract guard
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"unexpected installer staging name: {staging}")
        try:
            _write_staging_state(staging, dst, transaction_id)
            copied = _copy_payload_into_staging(src, staging)
            _write_install_state(
                staging,
                platform_name=platform_name,
                owned_files=owned_files,
            )
            _write_install_generation_marker(
                staging,
                dst,
                platform_name=platform_name,
                generation_id=transaction_id,
            )
            _validate_preserved_path_conflicts(staging, preserved)
            _write_staging_state(
                staging,
                dst,
                transaction_id,
                preserved_paths=tuple(preserved),
            )
            _promote_staged_target(staging, dst)
            return copied
        finally:
            matching_backup = dst.parent / (
                f".{dst.name}.backup-{transaction_id}"
            )
            if _path_exists(staging) and not _path_exists(matching_backup):
                shutil.rmtree(staging, ignore_errors=True)

NOMIC_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
NOMIC_MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"


def _huggingface_hub_cache(
    *,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Match huggingface_hub's HF_HUB_CACHE > HF_HOME > home precedence."""
    env = os.environ if environ is None else environ
    configured_hub = env.get("HF_HUB_CACHE", "").strip()
    if configured_hub:
        return Path(configured_hub).expanduser()
    configured_home = env.get("HF_HOME", "").strip()
    hf_home = (
        Path(configured_home).expanduser()
        if configured_home
        else (Path.home() if home is None else Path(home)) / ".cache" / "huggingface"
    )
    return hf_home / "hub"


HF_HUB_CACHE = _huggingface_hub_cache()
NOMIC_CACHE_DIR = HF_HUB_CACHE / f"models--{NOMIC_MODEL_ID.replace('/', '--')}"

def _default_playwright_cache() -> Path:
    """Default Playwright browser cache directory, per OS convention."""
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"  # Linux / *BSD


PW_CACHE = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or _default_playwright_cache())

PYTHON_INSTALL_HINTS = {
    "Darwin":  "brew install python@3.12  (or https://www.python.org/downloads/)",
    "Linux":   "sudo apt install python3.10 python3.10-venv  (Debian/Ubuntu)\n"
               "  sudo dnf install python3.10                  (Fedora/RHEL)\n"
               "  sudo pacman -S python                        (Arch)",
    "Windows": "https://www.python.org/downloads/   (☑ 'Add Python to PATH')",
}

CJK_FONT_PATHS = {
    "Linux":   [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    ],
    "Darwin":  [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ],
    "Windows": [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttf",
    ],
}

CJK_INSTALL_HINTS = {
    "Linux":   "Debian/Ubuntu: sudo apt install fonts-noto-cjk\n"
               "  Fedora/RHEL:   sudo dnf install google-noto-cjk-fonts\n"
               "  Arch:          sudo pacman -S noto-fonts-cjk",
    "Darwin":  "macOS 通常自带 PingFang.ttc — 检查 /System/Library/Fonts/",
    "Windows": "Windows 通常自带 msyh.ttc — 检查 C:\\Windows\\Fonts\\",
}


# ============================================================================
# UI helpers
# ============================================================================

_USE_COLOR = sys.stdout.isatty() and not IS_WIN
if IS_WIN:
    # Try to enable VT processing on Windows 10+
    try:
        import ctypes  # noqa: PLC0415
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        _USE_COLOR = True
    except Exception:  # noqa: BLE001
        _USE_COLOR = False


def _c(code: str) -> str:
    return code if _USE_COLOR else ""


GREEN  = _c("\033[32m")
YELLOW = _c("\033[33m")
RED    = _c("\033[31m")
CYAN   = _c("\033[36m")
DIM    = _c("\033[2m")
BOLD   = _c("\033[1m")
RESET  = _c("\033[0m")

_step_num = 0


def banner() -> None:
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"{BOLD}  {DISPLAY_NAME} — installer{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"  Skill:    {SKILL_DIR}")
    print(f"  Platform: {platform.system()} ({platform.release()})")
    print(f"  Python:   {sys.version.split()[0]} ({sys.executable})")
    print()


def step(title: str) -> None:
    global _step_num
    _step_num += 1
    print(f"{CYAN}[{_step_num}]{RESET} {BOLD}{title}{RESET}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET}  {msg}")


def fail(msg: str, fix: Optional[str] = None) -> None:
    print(f"  {RED}✗{RESET} {msg}", file=sys.stderr)
    if fix:
        for line in fix.strip().splitlines():
            print(f"    {YELLOW}↳{RESET} {line}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"  {DIM}…{RESET} {msg}")


# ============================================================================
# Retry helper
# ============================================================================

def with_retry(func: Callable, retries: int = 3, label: str = "operation") -> bool:
    """Run func() with exponential backoff retry. Returns True/False."""
    for attempt in range(1, retries + 1):
        try:
            func()
            return True
        except Exception as e:  # noqa: BLE001
            if attempt >= retries:
                fail(f"{label} failed after {retries} attempts: {e}")
                return False
            wait = min(30, 2 ** attempt)
            warn(f"{label} attempt {attempt}/{retries} failed: {e}; retry in {wait}s")
            time.sleep(wait)
    return False


def run_cmd(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """run subprocess with streaming output to terminal."""
    return subprocess.run(cmd, check=True, **kw)


# ============================================================================
# Step 1: Python version check
# ============================================================================

def check_python_version() -> bool:
    step("Checking Python version")
    major, minor = sys.version_info.major, sys.version_info.minor
    if major != 3 or minor < 10:
        fail(
            f"Python {major}.{minor} is too old. Need Python 3.10 or later.",
            "Install Python 3.10+:\n" + PYTHON_INSTALL_HINTS.get(platform.system(),
                                                                  "https://www.python.org/downloads/"),
        )
        return False
    # Ensure venv module is present
    try:
        import venv  # noqa: F401, PLC0415
    except ImportError:
        fail(
            "Python `venv` module missing.",
            "Ubuntu/Debian: sudo apt install python3.10-venv",
        )
        return False
    ok(f"Python {major}.{minor}.{sys.version_info.micro} (venv module available)")
    return True


# ============================================================================
# Step 2: Virtual environment
# ============================================================================

def _venv_pip_works(venv_py: Path) -> bool:
    """Return True iff `<venv_py> -m pip --version` succeeds. Catches the
    common Debian/Ubuntu case where venv was 'created' but ensurepip
    is missing (so .venv/bin/python exists but pip is unavailable)."""
    try:
        subprocess.run(
            [str(venv_py), "-m", "pip", "--version"],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


_VENV_INSTALL_HINT = {
    "Linux": (
        "Likely missing python3-venv. On Debian/Ubuntu/WSL run:\n"
        "  sudo apt update && sudo apt install -y python3-venv\n"
        "  # or pin the minor version, e.g.:\n"
        "  sudo apt install -y python3.12-venv\n"
        "On Fedora/RHEL:\n"
        "  sudo dnf install -y python3-virtualenv\n"
        "Then DELETE the broken .venv and re-run:\n"
        f"  rm -rf .venv\n"
        "  python3 install.py"
    ),
    "Darwin": (
        "Reinstall Python from python.org or:\n"
        "  brew reinstall python@3.12\n"
        "Then: rm -rf .venv && python3 install.py"
    ),
    "Windows": (
        "Reinstall Python from python.org (ensure venv module is included),\n"
        "then: rmdir /s .venv && python install.py"
    ),
}


def ensure_venv(resume: bool) -> bool:
    step("Creating virtual environment (.venv/)")
    sys_name = platform.system()
    install_hint = _VENV_INSTALL_HINT.get(sys_name, _VENV_INSTALL_HINT["Linux"])

    # Case 1: .venv exists. Validate it's actually usable (pip works).
    if VENV_PY.is_file():
        if _venv_pip_works(VENV_PY):
            ok(".venv exists and pip works, reusing")
            return True
        # Broken venv (no pip) — common when python3-venv package missing
        fail(
            f".venv exists at {VENV_DIR} but pip is not available — broken venv.",
            install_hint,
        )
        return False

    # Case 2: .venv missing — create it.
    try:
        info(f"Running: {sys.executable} -m venv {VENV_DIR}")
        run_cmd([sys.executable, "-m", "venv", str(VENV_DIR)])
    except subprocess.CalledProcessError as e:
        fail(f"venv creation failed (exit {e.returncode}).", install_hint)
        return False
    if not VENV_PY.is_file():
        fail(f"Expected {VENV_PY} not found after venv creation.", install_hint)
        return False
    # Even if the command succeeded, pip might be unavailable on some systems
    if not _venv_pip_works(VENV_PY):
        fail(
            f"venv created but pip is unavailable — {VENV_PY} -m pip failed.",
            install_hint,
        )
        return False
    ok(f"Created {VENV_DIR}")
    return True


# ============================================================================
# Step 3: pip install dependencies
# ============================================================================

def install_deps(strict: bool) -> bool:
    step("Installing Python dependencies (requirements.txt)")
    req = SKILL_DIR / "requirements.txt"
    if not req.is_file():
        fail(f"requirements.txt not found: {req}")
        return False

    # Upgrade pip first
    info("Upgrading pip ...")
    try:
        run_cmd([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    except subprocess.CalledProcessError:
        warn("pip upgrade failed (continuing)")

    # Install requirements with retries (pip 内部也有 retry，但层叠保险)
    retries = 1 if strict else 3

    def _install():
        info(f"Running: pip install -r {req.name}")
        run_cmd([
            str(VENV_PY), "-m", "pip", "install",
            "-r", str(req),
            "--retries", "5", "-q",
        ])

    if not with_retry(_install, retries=retries, label="pip install"):
        return False
    ok("Dependencies installed")
    return True


# ============================================================================
# Step 4: Playwright chromium
# ============================================================================

def install_chromium(strict: bool, resume: bool) -> bool:
    step("Installing Playwright chromium")
    # Check cache first
    if PW_CACHE.is_dir():
        cached = list(PW_CACHE.glob("chromium-*")) + list(PW_CACHE.glob("chromium_headless_shell-*"))
        if cached and resume:
            ok(f"chromium already cached ({len(cached)} bundle): {PW_CACHE}")
            return True
        if cached:
            ok(f"chromium already cached ({len(cached)} bundle)")
            return True

    info("Downloading chromium (~280MB, first-time only) ...")
    retries = 1 if strict else 3

    def _install():
        run_cmd([str(VENV_PY), "-m", "playwright", "install", "chromium"])

    if not with_retry(_install, retries=retries, label="chromium download"):
        return False
    ok("chromium installed")
    return True


# ============================================================================
# Step 5: CJK font check (warn-only)
# ============================================================================

def check_cjk_fonts() -> bool:
    step("Checking CJK fonts (for Chinese chart labels)")
    candidates = CJK_FONT_PATHS.get(platform.system(), [])
    found = [p for p in candidates if Path(p).is_file()]
    if found:
        ok(f"CJK font found: {Path(found[0]).name}")
        return True
    warn("No CJK font detected — Chinese labels will render as boxes")
    hint = CJK_INSTALL_HINTS.get(platform.system(), "Install a CJK font manually")
    for line in hint.splitlines():
        print(f"    {DIM}{line}{RESET}")
    # Don't block install
    return True


# ============================================================================
# Step 6: Pre-warm embedding model
# ============================================================================

def prewarm_embedding_model(strict: bool, resume: bool) -> bool:
    step(
        f"Pre-warming embedding model ({NOMIC_MODEL_ID}"
        f"@{NOMIC_MODEL_REVISION[:12]}, ~547 MB / 522 MiB weights)"
    )
    # 1) Cache check
    if NOMIC_CACHE_DIR.is_dir() and resume:
        snapshot = NOMIC_CACHE_DIR / "snapshots" / NOMIC_MODEL_REVISION
        if snapshot.is_dir():
            weights = list(snapshot.glob("*.safetensors")) + list(
                snapshot.glob("pytorch_model.bin")
            )
            if weights:
                ok(f"Pinned model already cached: {snapshot}")
                return True

    # 2) Download from HuggingFace via sentence-transformers
    retries = 1 if strict else 3

    def _prewarm():
        run_cmd([
            str(VENV_PY), "-c",
            f"from sentence_transformers import SentenceTransformer; "
            f"SentenceTransformer('{NOMIC_MODEL_ID}', "
            f"revision='{NOMIC_MODEL_REVISION}', trust_remote_code=False)",
        ])

    if not with_retry(_prewarm, retries=retries, label="model download"):
        return False
    ok("Embedding model ready")
    return True


# ============================================================================
# Step 7: Build vector index
# ============================================================================

def build_index(resume: bool, force: bool = False) -> bool:
    step("Building vector index")
    index_exists = INDEX_FILE.is_file()
    if index_exists and not force:
        size_mb = INDEX_FILE.stat().st_size / 1024 / 1024
        info(
            f"Index exists ({size_mb:.1f} MB); checking for a safe v2 upgrade"
        )

    command = [str(VENV_PY), str(SKILL_DIR / "scripts" / "build_index.py")]
    if force:
        command.append("--force")
        info("Running a staged full rebuild; the working index remains recoverable")
    elif index_exists:
        command.append("--upgrade")
    else:
        info(
            "Running build_index.py with bundled canonical embeddings; "
            "native v2 vectors use the release seed before computing misses"
        )
    try:
        run_cmd(command)
    except subprocess.CalledProcessError as e:
        fail(f"build_index failed: {e}")
        return False
    if not INDEX_FILE.is_file():
        fail(f"Expected {INDEX_FILE} not found after build")
        return False
    ok(f"Index ready: {INDEX_FILE}")
    return True


# ============================================================================
# Step 8: Register to platform-specific skills directory
# ============================================================================

def _load_platform_frontmatter(platform_name: str) -> str:
    """Read platforms/<name>.yaml as raw text (without YAML parsing dependency).

    The file contains *just* the body of a YAML frontmatter — we wrap it with
    '---' delimiters when composing SKILL.md.
    """
    p = PLATFORMS_DIR / f"{platform_name}.yaml"
    if not p.is_file():
        raise FileNotFoundError(
            f"Platform frontmatter missing: {p}. "
            f"Known platforms: {sorted(PLATFORM_REGISTRY)}"
        )
    return p.read_text(encoding="utf-8").rstrip()


def _compose_skill_md(platform_name: str) -> str:
    """Build a full SKILL.md = '---\\n<platform frontmatter>\\n---\\n<body>'."""
    fm = _load_platform_frontmatter(platform_name)
    if not SKILL_BODY_MD.is_file():
        raise FileNotFoundError(
            f"SKILL.body.md not found at {SKILL_BODY_MD}. "
            f"Run from a complete repo checkout."
        )
    body = SKILL_BODY_MD.read_text(encoding="utf-8")
    if not body.startswith("\n"):
        body = "\n" + body
    return f"---\n{fm}\n---{body}"


def register_skill(no_register: bool, platform_name: str) -> bool:
    """Write SKILL.md (platform-specific frontmatter + shared body) to SKILL_DIR.

    Source files have already been copied to SKILL_DIR by main() (or this is
    an in-place install where source==target). This step only writes the
    SKILL.md that is specific to the chosen agent platform.
    """
    if no_register:
        step("Skipping SKILL.md generation (--no-register)")
        return True

    step(f"Generating SKILL.md (platform={platform_name})")
    try:
        skill_md = _compose_skill_md(platform_name)
        (SKILL_DIR / "SKILL.md").write_text(skill_md, encoding="utf-8")
        ok(f"Wrote {SKILL_DIR / 'SKILL.md'}  ({platform_name} frontmatter)")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"Failed to write SKILL.md: {e}")
        return False


# ============================================================================
# Uninstall / Update modes
# ============================================================================

class PlatformTargetError(ValueError):
    """Raised when a safe install target cannot be selected."""


def _resolve_target(platform_name: str, target_dir: Optional[Path]) -> Optional[Path]:
    """Resolve a target without applying legacy-install safety checks.

    The function retains its historical Optional return contract for callers
    that only need to inspect defaults. Mutating operations must call
    `_resolve_target_for_operation` instead.
    """
    if target_dir is not None:
        return Path(target_dir).expanduser()
    spec = PLATFORM_REGISTRY.get(platform_name)
    if spec is None:
        return None
    return spec.default_target()


def _legacy_target_issue(
    platform_name: str,
    *,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Describe a legacy uppercase install that needs explicit resolution."""
    spec = PLATFORM_REGISTRY.get(platform_name)
    if spec is None:
        return None
    legacy = spec.legacy_target(home=home, environ=environ)
    current = spec.default_target(home=home, environ=environ)
    if legacy is None or current is None or not (legacy.exists() or legacy.is_symlink()):
        return None

    if current.exists() or current.is_symlink():
        detail = (
            f"both the current target {current} and legacy target {legacy} exist"
        )
    else:
        detail = f"a legacy install exists at {legacy}"
    return (
        f"[{platform_name}] {detail}. The standards-compliant target is "
        f"{current}. Refusing to modify either path automatically. Move or "
        "remove the legacy install yourself, or pass --target-dir with the "
        "exact directory you intentionally want to manage."
    )


def _resolve_target_for_operation(
    platform_name: str,
    target_dir: Optional[Path],
    *,
    operation: str = "install",
    allow_managed_source_update: bool = False,
) -> Path:
    """Resolve a mutation target and reject ambiguous or broad paths."""
    if operation not in {"install", "update", "uninstall"}:
        raise ValueError(f"unknown installer operation: {operation}")
    target = _resolve_target(platform_name, target_dir)
    if target is None:
        spec = PLATFORM_REGISTRY.get(platform_name)
        if spec and spec.explicit_target_help:
            raise PlatformTargetError(spec.explicit_target_help)
        raise PlatformTargetError(
            f"No install target is configured for platform {platform_name!r}. "
            "Pass --target-dir explicitly."
        )

    # An explicit directory is an intentional override. In particular it is
    # the only supported way to manage a legacy path; never delete/move one as
    # a side effect of resolving a new default.
    if target_dir is None:
        issue = _legacy_target_issue(platform_name)
        if issue:
            raise PlatformTargetError(issue)

    # Resolve parent symlinks before comparing paths. For uninstalling a link,
    # retain the lexical link itself so _remove_path() unlinks it rather than
    # deleting its referent; install/update never accept a symlink target.
    lexical = target.expanduser().absolute()
    target_is_link = lexical.is_symlink()
    resolved = lexical.resolve(strict=False)
    if target_is_link and operation in {"install", "update"}:
        raise PlatformTargetError(
            f"[{platform_name}] refusing to {operation} through symlink target: {lexical}"
        )
    safety_path = lexical if target_is_link else resolved

    anchor = Path(safety_path.anchor)
    home = Path.home().resolve(strict=False)
    tmp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    source = SOURCE_DIR.resolve(strict=False)
    cwd = Path.cwd().resolve(strict=False)

    unsafe_exact: set[Path] = {anchor, home, tmp_root}
    # Reject agent/state containers themselves. Only a directory below the
    # documented skills parent may be an install target.
    for spec in PLATFORM_REGISTRY.values():
        configured_root = spec._configured_root()
        if configured_root is not None:
            unsafe_exact.add(configured_root.resolve(strict=False))
        default_target = spec.default_target()
        if default_target is not None:
            unsafe_exact.add(default_target.parent.resolve(strict=False))
        detection_root = spec.detection_root()
        if detection_root is not None:
            unsafe_exact.add(detection_root.resolve(strict=False))

    # Defense in depth for explicit absolute paths outside the user's home.
    for broad_name in (
        "bin", "boot", "dev", "etc", "home", "lib", "lib64", "media",
        "mnt", "opt", "proc", "root", "run", "sbin", "srv", "sys",
        "tmp", "usr", "var",
        "Windows", "Program Files", "Program Files (x86)", "Users",
    ):
        unsafe_exact.add(anchor / broad_name)

    if safety_path in unsafe_exact:
        raise PlatformTargetError(
            f"[{platform_name}] refusing unsafe broad {operation} target: {lexical}"
        )

    lock_root = knowledge_base_lock_root().resolve(strict=False)
    if safety_path.is_relative_to(lock_root) or lock_root.is_relative_to(safety_path):
        raise PlatformTargetError(
            f"[{platform_name}] refusing {operation} target that overlaps the "
            f"cross-process lock root: {lexical}"
        )

    # The source checkout may be installed in place for development, but it
    # must never be an update/uninstall target and no ancestor may be managed.
    if safety_path == source:
        managed_self_update = bool(
            operation == "update"
            and allow_managed_source_update
            and not target_is_link
            and _load_install_state(source, strict=False) is not None
        )
        if (operation != "install" and not managed_self_update) or target_is_link:
            raise PlatformTargetError(
                f"[{platform_name}] refusing to {operation} the source checkout: {source}"
            )
    elif safety_path.is_relative_to(source):
        raise PlatformTargetError(
            f"[{platform_name}] target is inside the source checkout: {lexical}"
        )
    elif source.is_relative_to(safety_path):
        raise PlatformTargetError(
            f"[{platform_name}] target contains the source checkout: {lexical}"
        )
    elif safety_path == cwd and cwd != source:
        raise PlatformTargetError(
            f"[{platform_name}] refusing to {operation} the current working directory: {cwd}"
        )

    return lexical if target_is_link else resolved


def _remove_path(p: Path) -> bool:
    """Remove a path (file / dir / symlink). Returns True if anything removed."""
    if not p.exists() and not p.is_symlink():
        return False
    try:
        if p.is_symlink() or p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)
        return True
    except OSError as e:
        warn(f"Failed to remove {p}: {e}")
        return False


def cmd_uninstall(platforms: list[str], target_dir: Optional[Path],
                  full: bool, purge: bool, yes_i_know: bool) -> int:
    """Uninstall local-path copies or remove explicit WorkBuddy staging.

    Each platform's target directory is fully self-contained (it owns its own
    `.venv` and `_index`), so removing the target dir is a complete uninstall
    for that platform.

    --purge --yes-i-know: also delete user-global caches (Playwright chromium
        ~280MB and the nomic embedding weights ~547 MB / 522 MiB). Other tools / projects
        on your machine may share these — only purge if you are sure.
    --full: deprecated backward-compatibility flag. It has no effect because
        the default uninstall already removes the self-contained target,
        including .venv and _index.
    """
    if purge and not yes_i_know:
        fail("--purge needs --yes-i-know (removes global caches that other "
             "projects may share: Playwright browser cache + "
             "~/.cache/huggingface/hub/models--nomic-*/)")
        return 2

    workbuddy_staging = platforms == ["workbuddy"]
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    if workbuddy_staging:
        print(f"{BOLD}  {DISPLAY_NAME} — WorkBuddy staging remover{RESET}")
    else:
        print(f"{BOLD}  {DISPLAY_NAME} — uninstaller{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"  Platforms: {', '.join(platforms)}")
    print(f"  Mode:      {'PURGE (with global cache)' if purge else 'STANDARD'}")
    print()

    overall_ok = True

    # ── 1. Per-platform: rm -rf target dir (self-contained) ─────────────────
    for pname in platforms:
        try:
            target = _resolve_target_for_operation(
                pname,
                target_dir,
                operation="uninstall",
            )
        except PlatformTargetError as exc:
            fail(str(exc))
            overall_ok = False
            continue
        if pname == "workbuddy":
            step(f"[{pname}] Removing developer staging tree {target}")
        else:
            step(f"[{pname}] Removing {target}")
        try:
            with knowledge_base_build_lock(
                _installer_operation_resource(target),
                mode="write",
            ):
                with knowledge_base_build_lock(
                    target / "knowledge_base",
                    mode="write",
                ):
                    if not target.exists() and not target.is_symlink():
                        if pname == "workbuddy":
                            info("no developer staging tree, nothing to remove")
                        else:
                            info("not installed, nothing to remove")
                        continue
                    if not _target_is_owned_install(target):
                        fail(
                            f"[{pname}] refusing to remove a directory without "
                            f"verified OpenMobius ownership: {target}"
                        )
                        overall_ok = False
                        continue
                    if _remove_path(target):
                        ok(f"Removed {target}")
                    else:
                        overall_ok = False
        except BuildLockUnavailable as exc:
            fail(f"[{pname}] install target is busy; target was not removed: {exc}")
            overall_ok = False

    if full:
        warn("--full is deprecated and has no effect; standard uninstall "
             "already removes the entire target directory, including .venv "
             "and the built vector index")

    # ── 2. --purge: user-global caches (shared across platforms) ────────────
    if purge:
        step("Purging user-global caches (Playwright chromium + nomic model)")
        if PW_CACHE.exists():
            chromium_dirs = (
                list(PW_CACHE.glob("chromium-*"))
                + list(PW_CACHE.glob("chromium_headless_shell-*"))
            )
            for d in chromium_dirs:
                if _remove_path(d):
                    ok(f"Removed {d}")
        if NOMIC_CACHE_DIR.exists():
            if _remove_path(NOMIC_CACHE_DIR):
                ok(f"Removed {NOMIC_CACHE_DIR}")

    # ── 3. Final note ───────────────────────────────────────────────────────
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    if overall_ok:
        if workbuddy_staging:
            print(f"{GREEN}{BOLD}  ✓ WorkBuddy developer staging removed{RESET}")
        else:
            print(f"{GREEN}{BOLD}  ✓ Uninstall complete{RESET}")
    else:
        if workbuddy_staging:
            print(
                f"{YELLOW}{BOLD}  ⚠ WorkBuddy staging removal finished "
                f"with some issues{RESET}"
            )
        else:
            print(f"{YELLOW}{BOLD}  ⚠ Uninstall finished with some issues{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    if not purge:
        print(f"  {DIM}User-global caches kept{RESET} (Playwright chromium / nomic). "
              f"Other tools may need them; pass --purge --yes-i-know to remove.")
    launcher = "py -3" if IS_WIN else "python3"
    if workbuddy_staging:
        print(
            f"  {DIM}Re-create staging:{RESET}  {launcher} install.py "
            "--platform workbuddy --target-dir <path>"
        )
        print(
            f"  {DIM}WorkBuddy status:{RESET} removing staging did not uninstall "
            "an imported or marketplace Skill. Manage that under Skills > Installed."
        )
    else:
        print(f"  {DIM}Re-install:{RESET}  {launcher} install.py [--platform <name>]")
    print()
    return 0 if overall_ok else 1


REPO_URL = "https://github.com/MobiusQuant/OpenMobius-skill.git"


def _clone_fresh_to_tmp() -> Optional[Path]:
    """Clone the upstream repo into a fresh /tmp directory, return its path.

    Returns None on failure.
    """
    import tempfile  # noqa: PLC0415
    tmp_root = Path(tempfile.mkdtemp(prefix="openmobius-update-"))
    step(f"Cloning fresh source to {tmp_root}")
    try:
        run_cmd(["git", "clone", "--depth", "1", REPO_URL, str(tmp_root / "src")])
        ok(f"Cloned {REPO_URL} → {tmp_root}/src")
        return tmp_root / "src"
    except subprocess.CalledProcessError as e:
        fail(f"git clone failed: {e}")
        shutil.rmtree(tmp_root, ignore_errors=True)
        return None


def cmd_update(platforms: list[str], target_dir: Optional[Path],
               no_pull: bool, rebuild_index: bool,
               args: argparse.Namespace) -> int:
    """Update local-path installs or an explicit WorkBuddy staging tree.

    Flow:
      1. Clone the upstream repo into a fresh /tmp dir (unless --no-pull,
         in which case the source is the current SOURCE_DIR — useful when
         user has already pulled their clone manually).
      2. For each requested platform whose target dir exists, copy the
         updated source files into the target, then re-run install steps
         in resume mode (skips already-done work). SKILL.md is regenerated.
      3. Clean up the /tmp clone.

    --no-pull: don't fetch upstream; use current SOURCE_DIR as the source.
    --rebuild-index: force the vector index rebuild step.
    """
    global SOURCE_DIR
    saved_source = SOURCE_DIR
    tmp_source: Optional[Path] = None

    workbuddy_staging = platforms == ["workbuddy"]
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    if workbuddy_staging:
        print(f"{BOLD}  {DISPLAY_NAME} — WorkBuddy staging updater{RESET}")
    else:
        print(f"{BOLD}  {DISPLAY_NAME} — updater{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"  Platforms: {', '.join(platforms)}")
    print()

    # ── 1. Decide the update source ─────────────────────────────────────────
    if no_pull:
        info(f"--no-pull: using current source at {SOURCE_DIR}")
    else:
        tmp_source = _clone_fresh_to_tmp()
        if tmp_source is None:
            fail("Could not obtain fresh source. Re-run with --no-pull to "
                 "use the current local source instead.")
            return 1
        # Mutate SOURCE_DIR so _run_single_install's copy_source_to_target
        # uses the fresh code.
        SOURCE_DIR = tmp_source

    overall_ok = True
    try:
        # ── 2. Per-platform update ──────────────────────────────────────────
        for pname in platforms:
            try:
                target = _resolve_target_for_operation(
                    pname,
                    target_dir,
                    operation="update",
                )
            except PlatformTargetError as exc:
                fail(str(exc))
                overall_ok = False
                continue
            if not target.exists():
                if pname == "workbuddy":
                    info(
                        f"[{pname}] no developer staging tree at {target}; "
                        "creating one"
                    )
                else:
                    info(f"[{pname}] not installed at {target}; running fresh install")
            print()
            if pname == "workbuddy":
                print(
                    f"{CYAN}{BOLD}━━━ Updating WorkBuddy developer staging "
                    f"@ {target} ━━━{RESET}"
                )
            else:
                print(f"{CYAN}{BOLD}━━━ Updating {pname} @ {target} ━━━{RESET}")
            args.platform = pname
            # Rebuilds are staged by build_index.py and promoted only after
            # all collections verify. Never delete the working index here.
            if rebuild_index:
                idx = target / "knowledge_base" / "_index"
                info(f"--rebuild-index: will safely replace {idx}")
            rc = _run_single_install(args, operation="update")
            if rc != 0:
                overall_ok = False
    finally:
        SOURCE_DIR = saved_source
        if tmp_source is not None:
            shutil.rmtree(tmp_source.parent, ignore_errors=True)
            info(f"Cleaned up {tmp_source.parent}")

    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    if overall_ok:
        if workbuddy_staging:
            print(f"{GREEN}{BOLD}  ✓ WorkBuddy staging update complete{RESET}")
        else:
            print(f"{GREEN}{BOLD}  ✓ Update complete{RESET}")
    else:
        if workbuddy_staging:
            print(
                f"{YELLOW}{BOLD}  ⚠ WorkBuddy staging update finished "
                f"with issues{RESET}"
            )
        else:
            print(f"{YELLOW}{BOLD}  ⚠ Update finished with issues{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    return 0 if overall_ok else 1


def cmd_install_all(platforms: list[str], args: argparse.Namespace) -> int:
    """Install to multiple platforms in sequence (heavy steps run only once)."""
    print()
    print(f"{BOLD}Installing for {len(platforms)} platforms: {', '.join(platforms)}{RESET}")
    print()
    # Save args.platform; we'll loop
    overall_ok = True
    saved_platform = args.platform
    for idx, pname in enumerate(platforms, 1):
        print()
        print(f"{CYAN}{BOLD}━━━ [{idx}/{len(platforms)}] Platform: {pname} ━━━{RESET}")
        args.platform = pname
        # 复用 main 的剩下逻辑 — 这里走单 platform 的常规 install
        rc = _run_single_install(args)
        if rc != 0:
            overall_ok = False
    args.platform = saved_platform
    return 0 if overall_ok else 1


def _resolve_install_target(
    args: argparse.Namespace,
    *,
    operation: str = "install",
) -> Path:
    """Determine the directory where this install should land.

    Priority:
      1. --target-dir <path>  (explicit override)
      2. Registry default    (standard agent skills dir)

    Platforms without a documented local automatic-discovery default
    (currently WorkBuddy) must use an explicit target. For WorkBuddy that
    target is developer staging only. A known platform is never silently
    installed into the source clone.
    """
    explicit = Path(args.target_dir).expanduser() if args.target_dir else None
    return _resolve_target_for_operation(
        args.platform,
        explicit,
        operation=operation,
    )


def _run_single_install(
    args: argparse.Namespace,
    *,
    operation: str = "install",
) -> int:
    """Run a local-path install or explicit WorkBuddy developer staging.

    Layout:
      1. Decide target dir (agent skills dir, by default).
      2. If target != SOURCE_DIR, copy source files into target, then rebind
         module-level path globals so subsequent steps operate on target.
      3. Run the install steps (venv / deps / chromium / model / index /
         SKILL.md / doctor) against the target. Each platform install is
         fully self-contained — the source clone is never referenced again
         and may be deleted after install.
    """
    global _step_num
    _step_num = 0   # reset step counter for each platform

    try:
        target = _resolve_install_target(args, operation=operation)
    except PlatformTargetError as exc:
        fail(str(exc))
        return 2
    in_place = target == SOURCE_DIR.resolve()

    try:
        with knowledge_base_build_lock(
            _installer_operation_resource(target),
            mode="write",
        ):
            return _run_single_install_under_operation_lock(
                args,
                target=target,
                in_place=in_place,
            )
    except BuildLockUnavailable as exc:
        fail(f"Install target is busy; no concurrent operation was started: {exc}")
        return 1


def _run_single_install_under_operation_lock(
    args: argparse.Namespace,
    *,
    target: Path,
    in_place: bool,
) -> int:
    """Execute one install while the caller owns the target operation lock."""

    print()
    target_label = (
        "Developer staging target" if args.platform == "workbuddy" else "Install target"
    )
    print(f"  {target_label} : {target}")
    print(f"  Source clone   : {SOURCE_DIR}{'  (in-place)' if in_place else ''}")
    print()

    # Step 0: copy source → target (skipped when in-place)
    generation_id: Optional[str] = None
    if not in_place:
        step(f"Staging source files → {target}")
        try:
            n = copy_source_to_target(
                SOURCE_DIR,
                target,
                platform_name=args.platform,
            )
            ok(f"Copied {n} top-level entries (excluding .venv, _index, .git, secrets)")
        except Exception as e:  # noqa: BLE001
            fail(f"Failed to stage source files: {e}")
            return 1
        # Rebind install-dir-relative globals so subsequent steps see target.
        _rebind_paths_to(target)
        try:
            generation_id = _read_install_generation_marker(target)["generation_id"]
        except (OSError, ValueError) as exc:
            fail(f"Failed to validate staged install generation: {exc}")
            return 1

    results: dict[str, bool] = {}

    results["Python version"] = check_python_version()
    if not results["Python version"]:
        return 1
    results["Virtual env"]    = ensure_venv(args.resume)
    if not results["Virtual env"]:
        # venv broken — everything after pip is useless; stop now
        print_summary(results, all_ok=False,
                      platform_name=args.platform, target_dir=target)
        return 1
    results["Python dependencies"]    = install_deps(args.strict)
    if not args.skip_chromium:
        results["Playwright chromium"] = install_chromium(args.strict, args.resume)
    if not args.skip_fonts:
        results["CJK fonts"]           = check_cjk_fonts()
    results["Embedding model"]        = prewarm_embedding_model(args.strict, args.resume)
    results["Vector index"]           = build_index(
        args.resume,
        force=bool(getattr(args, "rebuild_index", False)),
    )
    if results["Vector index"] and not in_place:
        try:
            assert generation_id is not None
            _complete_install_generation(
                target,
                expected_generation_id=generation_id,
            )
        except (BuildLockUnavailable, OSError, ValueError) as exc:
            fail(
                "Index was built, but the guarded install generation could "
                f"not be completed: {exc}"
            )
            results["Vector index"] = False
    results["Skill registration"]     = register_skill(
        no_register=args.no_register,
        platform_name=args.platform,
    )
    if not args.skip_doctor:
        results["Doctor"] = run_doctor(
            platform_name=args.platform,
            expected_dir=target,
        )

    all_ok = all(results.values())
    print_summary(results, all_ok=all_ok,
                  platform_name=args.platform, target_dir=target)
    return 0 if all_ok else 1


def detect_platforms() -> list[str]:
    """Scan for known platform dirs that exist on this machine."""
    detected: list[str] = []
    for name, spec in PLATFORM_REGISTRY.items():
        root = spec.detection_root()
        if root is not None and root.is_dir():
            detected.append(name)
    return detected


# ============================================================================
# Final step: Run kb_doctor
# ============================================================================

def run_doctor(
    platform_name: Optional[str] = None,
    expected_dir: Optional[Path] = None,
) -> bool:
    step("Running environment doctor (kb_doctor)")
    doctor = SKILL_DIR / "scripts" / "kb_doctor.py"
    if not doctor.is_file():
        warn(f"{doctor} not found; installation cannot be verified")
        return False
    command = [str(VENV_PY), str(doctor)]
    if platform_name:
        command.extend(["--platform", platform_name])
    if expected_dir is not None:
        command.extend(["--expected-dir", str(Path(expected_dir).resolve())])
    try:
        result = subprocess.run(command, check=False)
    except Exception as e:  # noqa: BLE001
        warn(f"doctor crashed: {e}")
        return False
    if result.returncode != 0:
        warn(f"kb_doctor reported failures (exit code {result.returncode})")
        return False
    return True


# ============================================================================
# Final summary
# ============================================================================

def print_summary(results: dict, all_ok: bool, platform_name: str = "claude-code",
                  target_dir: Optional[Path] = None) -> None:
    workbuddy_staging = platform_name == "workbuddy"
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    if all_ok:
        if workbuddy_staging:
            print(f"{GREEN}{BOLD}  ✓ WorkBuddy staging validation complete{RESET}")
        else:
            print(f"{GREEN}{BOLD}  ✓ Installation complete{RESET}")
    else:
        if workbuddy_staging:
            print(
                f"{YELLOW}{BOLD}  ⚠ WorkBuddy staging validation "
                f"finished with issues{RESET}"
            )
        else:
            print(f"{YELLOW}{BOLD}  ⚠ Installation finished with issues{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    for name, ok_ in results.items():
        mark = f"{GREEN}✓{RESET}" if ok_ else f"{RED}✗{RESET}"
        print(f"  {mark} {name}")

    if all_ok:
        installed_at = target_dir or PLATFORM_DEFAULTS.get(platform_name)
        if workbuddy_staging:
            if installed_at:
                print(f"  {DIM}Staged at:{RESET} {installed_at}")
            print(
                f"  {DIM}Status:{RESET} this did not import, install, or publish "
                "the Skill in WorkBuddy."
            )
            print(
                f"  {DIM}Next:{RESET} build the ZIP, then import it with "
                "Skills > Add Skill > Upload Skill."
            )
            launcher = "py -3" if IS_WIN else "python3"
            archive = Path(tempfile.gettempdir()) / "openmobius-skill-workbuddy.zip"
            print(
                f"    {launcher} scripts/build_workbuddy_package.py "
                f"--output \"{archive}\""
            )
        else:
            agent_name = PLATFORM_DISPLAY_NAMES.get(platform_name, platform_name)
            print(f"  {DIM}Next:{RESET} use the skill in {agent_name} (no cd needed).")
            if installed_at:
                print(f"  {DIM}Installed to:{RESET} {installed_at}")
        print(f"  {DIM}Local test:{RESET}")
        rel_py = ".venv\\Scripts\\python.exe" if IS_WIN else ".venv/bin/python"
        print(f"    cd \"{SKILL_DIR}\"")
        print(f"    {rel_py} scripts/kb_retrieve.py \"what is FVG\"")
        print()
        # Cross-platform install hint
        other_platforms = [p for p in PLATFORM_DEFAULTS if p != platform_name]
        if other_platforms:
            print(f"  {DIM}Install to another platform:{RESET}")
            launcher = "py -3" if IS_WIN else "python3"
            print(f"    {launcher} install.py --platform <{' | '.join(other_platforms)}>")
            print()


# ============================================================================
# Main
# ============================================================================


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser from the platform registry."""
    parser = argparse.ArgumentParser(
        description=f"{DISPLAY_NAME} cross-platform installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Mode (mutually exclusive) — default is install
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true",
                      help="Remove the entire platform install, or an explicit "
                           "WorkBuddy staging tree (use --purge to also remove "
                           "shared caches)")
    mode.add_argument("--update", action="store_true",
                      help="Update the skill or explicit WorkBuddy staging tree "
                           "(fetch upstream + re-install --resume + regenerate "
                           "SKILL.md)")

    parser.add_argument("--strict", action="store_true",
                        help="CI mode: fail fast on first error, no retry")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Deprecated compatibility no-op; installation is "
                             "non-interactive")
    parser.add_argument(
        "--platform", default="claude-code",
        choices=[*PLATFORM_NAMES, "auto", "all"],
        help="Target agent platform (default: claude-code; "
             "'auto' = detect installed agents; "
             f"'all' = apply to all {len(DEFAULT_TARGET_PLATFORMS)} platforms "
             "with documented local targets; WorkBuddy is excluded and "
             "requires --target-dir only for developer staging)",
    )
    parser.add_argument(
        "--target-dir", default=None,
        help="Override install path (default: platform-specific, e.g. "
             "~/.claude/skills/openmobius-skill; required only for explicit "
             "WorkBuddy developer staging)",
    )
    parser.add_argument("--no-register", action="store_true",
                        help="Skip platform SKILL.md generation")
    parser.add_argument("--resume", action="store_true", default=True,
                        help="Skip already-done steps (default ON)")
    parser.add_argument("--no-resume", action="store_false", dest="resume",
                        help="Re-do every step")
    parser.add_argument("--skip-fonts", action="store_true",
                        help="Skip CJK font check")
    parser.add_argument("--skip-chromium", action="store_true",
                        help="Skip Playwright chromium install")
    parser.add_argument("--skip-doctor", action="store_true",
                        help="Skip final kb_doctor health check")
    parser.add_argument("-v", "--verbose", action="store_true")

    # Uninstall-specific options
    parser.add_argument("--full", action="store_true",
                        help="(deprecated no-op) standard uninstall already removes "
                             ".venv and the built vector index")
    parser.add_argument("--purge", action="store_true",
                        help="(uninstall) also remove global caches "
                             "(chromium ~280MB + nomic weights ~547 MB / 522 MiB). "
                             "Use only if no other skill needs them. "
                             "Requires --yes-i-know.")
    parser.add_argument("--yes-i-know", action="store_true",
                        help="(uninstall --purge) confirmation: yes, I understand global "
                             "caches affect other projects.")

    # Update-specific options
    parser.add_argument("--no-pull", action="store_true",
                        help="(update) skip the upstream fetch; only re-install "
                             "+ regenerate SKILL.md")
    parser.add_argument("--rebuild-index", action="store_true",
                        help="(update) force rebuild of vector index "
                             "(only needed when knowledge_base/{concepts,cases} changed)")

    return parser


def _auto_detection_hint() -> str:
    """Return the current registry-derived roots considered by auto mode."""
    roots: list[str] = []
    for spec in PLATFORM_REGISTRY.values():
        root = spec.detection_root()
        if root is not None:
            roots.append(str(root))
    return " / ".join(roots)


def _preflight_targets(
    platforms: list[str],
    target_dir: Optional[Path],
    *,
    operation: str = "install",
    allow_managed_source_update: bool = False,
) -> bool:
    """Reject unresolved or ambiguous targets before any mutation starts."""
    host_system = platform.system()
    for platform_name in platforms:
        spec = PLATFORM_REGISTRY[platform_name]
        supported = spec.supported_host_systems
        if supported is not None and host_system not in supported:
            supported_display = ", ".join(
                "macOS" if item == "Darwin" else item for item in supported
            )
            fail(
                f"[{platform_name}] {spec.display_name} supports installation "
                f"only on {supported_display}; current host is {host_system}."
            )
            return False
        try:
            _resolve_target_for_operation(
                platform_name,
                target_dir,
                operation=operation,
                allow_managed_source_update=allow_managed_source_update,
            )
        except PlatformTargetError as exc:
            fail(str(exc))
            return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve --platform auto
    if args.platform == "auto":
        detected = detect_platforms()
        if not detected:
            warn("--platform auto: no known platform dirs found "
                 f"({_auto_detection_hint()}). Defaulting to claude-code.")
            args.platform = "claude-code"
        elif len(detected) == 1:
            args.platform = detected[0]
            ok(f"--platform auto: detected {args.platform}")
        else:
            print(f"\n  Multiple platforms detected: {', '.join(detected)}")
            launcher = "py -3" if IS_WIN else "python3"
            print(f"  Pick one with: {launcher} install.py --platform <name>")
            print(f"  Or apply to all: {launcher} install.py --platform all\n")
            return 2

    # Resolve --platform all
    target_dir_arg = Path(args.target_dir).expanduser() if args.target_dir else None
    if args.platform == "all":
        if target_dir_arg is not None:
            fail("--platform all is incompatible with --target-dir")
            return 2
        platforms_to_apply = list(DEFAULT_TARGET_PLATFORMS)
    else:
        platforms_to_apply = [args.platform]

    operation = "uninstall" if args.uninstall else "update" if args.update else "install"
    if not _preflight_targets(
        platforms_to_apply,
        target_dir_arg,
        operation=operation,
        # A managed installed copy is self-contained, so its own install.py
        # may bootstrap a normal update. cmd_update clones fresh source before
        # any mutation. --no-pull deliberately retains the stricter guard.
        allow_managed_source_update=bool(args.update and not args.no_pull),
    ):
        return 2

    # ── Mode dispatch ───────────────────────────────────────────────────────
    if args.uninstall:
        return cmd_uninstall(
            platforms=platforms_to_apply,
            target_dir=target_dir_arg,
            full=args.full,
            purge=args.purge,
            yes_i_know=args.yes_i_know,
        )
    if args.update:
        return cmd_update(
            platforms=platforms_to_apply,
            target_dir=target_dir_arg,
            no_pull=args.no_pull,
            rebuild_index=args.rebuild_index,
            args=args,
        )

    # ── Install ─────────────────────────────────────────────────────────────
    banner()
    if args.interactive:
        warn("-i / --interactive: per-step prompts have been removed in the "
             "standalone-install rewrite. Continuing non-interactively.")

    if args.platform == "all":
        return cmd_install_all(platforms_to_apply, args)
    return _run_single_install(args)


if __name__ == "__main__":
    sys.exit(main())
