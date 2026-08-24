#!/usr/bin/env python3
"""OpenMobius-skill — cross-platform installer.

Runs on macOS / Linux / Windows. Requires only Python 3.10+ pre-installed.

What this script does:
  - Creates a Python virtual environment at <skill-dir>/.venv/
  - Installs declared dependencies from requirements.txt (third-party
    OSS libraries: sentence-transformers, chromadb, playwright, etc. —
    see ATTRIBUTION.md for full list)
  - Downloads the open-source `nomic-ai/nomic-embed-text-v1.5` embedding
    model (Apache 2.0) from HuggingFace Hub's official URL.
  - Builds a local ChromaDB vector index of the bundled knowledge-base
    JSON cards.
  - Optionally registers a SKILL.md symlink into your AI coding agent's
    standard skills directory (e.g. ~/.claude/skills/OpenMobius-skill/).
  - Runs a health check.

What this script does NOT do:
  - No requests to any host other than: pypi.org (pip installs),
    huggingface.co (model download), and playwright.azureedge.net
    (chromium for chart rendering). These are documented in PRIVACY.md.
  - No file writes outside <skill-dir>, the venv, ~/.cache/huggingface,
    Playwright's per-OS browser cache (~/.cache/ms-playwright on Linux,
    ~/Library/Caches/ms-playwright on macOS, %LOCALAPPDATA%\\ms-playwright
    on Windows), and the chosen ~/.<agent>/skills/ symlink.
  - No background processes, daemons, startup entries, or system hooks.
  - No collection or transmission of user data, credentials, or telemetry.

Usage:
  python install.py                  # default
  python install.py --strict         # CI mode: fail fast
  python install.py -i               # interactive: prompt y/n each step
  python install.py --resume         # skip already-done steps (default ON)
  python install.py --no-register    # don't symlink into ~/.<agent>/skills/
  python install.py -v               # verbose
  python install.py --uninstall      # see install.py --uninstall --help
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional


# ============================================================================
# Constants & platform detection
# ============================================================================

SKILL_NAME = "OpenMobius-skill"

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
# Each agent platform has its own conventional skill directory.
# Override with --target-dir if your setup differs.
PLATFORM_DEFAULTS = {
    "claude-code": Path.home() / ".claude"   / "skills" / SKILL_NAME,
    "codex":       Path.home() / ".codex"    / "skills" / SKILL_NAME,
    "openclaw":    Path.home() / ".openclaw" / "skills" / SKILL_NAME,
    # Hermes organizes by category; this skill fits market-data.
    "hermes":      Path.home() / ".hermes"   / "skills" / "market-data" / SKILL_NAME,
}


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
    "scripts", "workflows", "knowledge_base", "platforms",
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
    "__pycache__", ".venv", "_index",
    ".env.local", ".env.local.example",
    ".DS_Store", ".git", "node_modules", ".pytest_cache",
}


def copy_source_to_target(src: Path, dst: Path) -> int:
    """Copy clone source into the per-platform target install dir.

    Returns the number of top-level entries copied. Idempotent — overwrites
    existing files. Skips build artifacts, secrets, and per-install dirs.
    """
    dst.mkdir(parents=True, exist_ok=True)

    def _ignore(_dir, names):
        return [n for n in names if n in COPY_EXCLUDE or n.endswith(".pyc")]

    copied = 0
    for entry in COPY_ENTRIES:
        src_path = src / entry
        if not src_path.exists():
            continue
        dst_path = dst / entry
        if src_path.is_dir():
            shutil.copytree(src_path, dst_path, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src_path, dst_path)
        copied += 1
    return copied

NOMIC_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
HF_HUB_CACHE = Path(
    os.environ.get("HF_HOME") or
    (Path.home() / ".cache" / "huggingface")
) / "hub"
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
_total_steps = 9


def banner() -> None:
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"{BOLD}  OpenMobius-skill — installer{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"  Skill:    {SKILL_DIR}")
    print(f"  Platform: {platform.system()} ({platform.release()})")
    print(f"  Python:   {sys.version.split()[0]} ({sys.executable})")
    print()


def step(title: str) -> None:
    global _step_num
    _step_num += 1
    print(f"{CYAN}[{_step_num}/{_total_steps}]{RESET} {BOLD}{title}{RESET}")


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
    step(f"Pre-warming embedding model ({NOMIC_MODEL_ID}, ~274MB)")
    # 1) Cache check
    if NOMIC_CACHE_DIR.is_dir() and resume:
        snaps = list((NOMIC_CACHE_DIR / "snapshots").glob("*")) if (NOMIC_CACHE_DIR / "snapshots").is_dir() else []
        if snaps:
            weights = list(snaps[0].glob("*.safetensors")) + list(snaps[0].glob("pytorch_model.bin"))
            if weights:
                ok(f"Model already cached: {snaps[0]}")
                return True

    # 2) Download from HuggingFace via sentence-transformers
    retries = 1 if strict else 3

    def _prewarm():
        run_cmd([
            str(VENV_PY), "-c",
            f"from sentence_transformers import SentenceTransformer; "
            f"SentenceTransformer('{NOMIC_MODEL_ID}', trust_remote_code=True)",
        ])

    if not with_retry(_prewarm, retries=retries, label="model download"):
        return False
    ok("Embedding model ready")
    return True


# ============================================================================
# Step 7: Build vector index
# ============================================================================

def build_index(resume: bool) -> bool:
    step("Building vector index")
    if INDEX_FILE.is_file():
        size_mb = INDEX_FILE.stat().st_size / 1024 / 1024
        ok(f"Index exists ({size_mb:.1f} MB), skipping (use --force to rebuild)")
        return True

    info("Running build_index.py — this takes ~30s for 964 cards")
    try:
        run_cmd([str(VENV_PY), str(SKILL_DIR / "scripts" / "build_index.py")])
    except subprocess.CalledProcessError as e:
        fail(f"build_index failed: {e}")
        return False
    if not INDEX_FILE.is_file():
        fail(f"Expected {INDEX_FILE} not found after build")
        return False
    ok(f"Index built: {INDEX_FILE}")
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
            f"Known platforms: {sorted(PLATFORM_DEFAULTS.keys())}"
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

def _resolve_target(platform_name: str, target_dir: Optional[Path]) -> Optional[Path]:
    if target_dir is not None:
        return target_dir
    return PLATFORM_DEFAULTS.get(platform_name)


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
    """Uninstall the skill from one or more platforms.

    Each platform's target directory is fully self-contained (it owns its own
    `.venv` and `_index`), so removing the target dir is a complete uninstall
    for that platform.

    --purge --yes-i-know: also delete user-global caches (Playwright chromium
        ~280MB and the nomic embedding model ~274MB). Other tools / projects
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

    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"{BOLD}  OpenMobius-skill — uninstaller{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"  Platforms: {', '.join(platforms)}")
    print(f"  Mode:      {'PURGE (with global cache)' if purge else 'STANDARD'}")
    print()

    overall_ok = True

    # ── 1. Per-platform: rm -rf target dir (self-contained) ─────────────────
    for pname in platforms:
        target = _resolve_target(pname, target_dir)
        if target is None:
            warn(f"[{pname}] no target resolved, skipping")
            overall_ok = False
            continue
        step(f"[{pname}] Removing {target}")
        if not target.exists() and not target.is_symlink():
            info("not installed, nothing to remove")
            continue
        if _remove_path(target):
            ok(f"Removed {target}")
        else:
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
        print(f"{GREEN}{BOLD}  ✓ Uninstall complete{RESET}")
    else:
        print(f"{YELLOW}{BOLD}  ⚠ Uninstall finished with some issues{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    if not purge:
        print(f"  {DIM}User-global caches kept{RESET} (Playwright chromium / nomic). "
              f"Other tools may need them; pass --purge --yes-i-know to remove.")
    print(f"  {DIM}Re-install:{RESET}  python install.py [--platform <name>]")
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
    """Update each already-installed platform with the latest upstream code.

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

    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    print(f"{BOLD}  OpenMobius-skill — updater{RESET}")
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
            target = _resolve_target(pname, target_dir)
            if target is None:
                warn(f"[{pname}] no target resolved, skipping")
                continue
            if not target.exists():
                info(f"[{pname}] not installed at {target}; running fresh install")
            print()
            print(f"{CYAN}{BOLD}━━━ Updating {pname} @ {target} ━━━{RESET}")
            args.platform = pname
            # If --rebuild-index, blow away the target's _index so build_index
            # treats it as fresh.
            if rebuild_index:
                idx = target / "knowledge_base" / "_index"
                if idx.exists():
                    info(f"--rebuild-index: removing {idx}")
                    _remove_path(idx)
            rc = _run_single_install(args)
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
        print(f"{GREEN}{BOLD}  ✓ Update complete{RESET}")
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


def _resolve_install_target(args: argparse.Namespace) -> Path:
    """Determine the directory where this install should land.

    Priority:
      1. --target-dir <path>            (explicit override)
      2. PLATFORM_DEFAULTS[--platform]  (standard agent skills dir)
      3. SOURCE_DIR                     (in-place install — last resort)
    """
    if args.target_dir:
        return Path(args.target_dir).expanduser().resolve()
    if args.platform in PLATFORM_DEFAULTS:
        return PLATFORM_DEFAULTS[args.platform].resolve()
    return SOURCE_DIR.resolve()


def _run_single_install(args: argparse.Namespace) -> int:
    """Run a normal install for the current args.platform.

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

    target = _resolve_install_target(args)
    in_place = target == SOURCE_DIR.resolve()

    print()
    print(f"  Install target : {target}")
    print(f"  Source clone   : {SOURCE_DIR}{'  (in-place)' if in_place else ''}")
    print()

    # Step 0: copy source → target (skipped when in-place)
    if not in_place:
        step(f"Staging source files → {target}")
        try:
            n = copy_source_to_target(SOURCE_DIR, target)
            ok(f"Copied {n} top-level entries (excluding .venv, _index, .git, secrets)")
        except Exception as e:  # noqa: BLE001
            fail(f"Failed to stage source files: {e}")
            return 1
        # Rebind install-dir-relative globals so subsequent steps see target.
        _rebind_paths_to(target)

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
    results["Vector index"]           = build_index(args.resume)
    results["Skill registration"]     = register_skill(
        no_register=args.no_register,
        platform_name=args.platform,
    )
    if not args.skip_doctor:
        results["Doctor"]              = run_doctor()

    all_ok = all(results.values())
    print_summary(results, all_ok=all_ok,
                  platform_name=args.platform, target_dir=target)
    return 0 if all_ok else 1


def detect_platforms() -> list[str]:
    """Scan for known platform dirs that exist on this machine."""
    detected = []
    if (Path.home() / ".claude").is_dir():
        detected.append("claude-code")
    if (Path.home() / ".codex").is_dir():
        detected.append("codex")
    if (Path.home() / ".openclaw").is_dir():
        detected.append("openclaw")
    if (Path.home() / ".hermes").is_dir():
        detected.append("hermes")
    return detected


# ============================================================================
# Step 9: Run kb_doctor
# ============================================================================

def run_doctor() -> bool:
    step("Running environment doctor (kb_doctor)")
    doctor = SKILL_DIR / "scripts" / "kb_doctor.py"
    if not doctor.is_file():
        warn(f"{doctor} not found, skipping")
        return True
    try:
        # Don't propagate exit code — doctor reports issues but install is already done
        subprocess.run([str(VENV_PY), str(doctor)], check=False)
    except Exception as e:  # noqa: BLE001
        warn(f"doctor crashed: {e}")
    return True


# ============================================================================
# Final summary
# ============================================================================

def print_summary(results: dict, all_ok: bool, platform_name: str = "claude-code",
                  target_dir: Optional[Path] = None) -> None:
    print()
    print(f"{DIM}{'═' * 64}{RESET}")
    if all_ok:
        print(f"{GREEN}{BOLD}  ✓ Installation complete{RESET}")
    else:
        print(f"{YELLOW}{BOLD}  ⚠ Installation finished with issues{RESET}")
    print(f"{DIM}{'═' * 64}{RESET}")
    for name, ok_ in results.items():
        mark = f"{GREEN}✓{RESET}" if ok_ else f"{RED}✗{RESET}"
        print(f"  {mark} {name}")

    if all_ok:
        agent_name = {
            "claude-code": "Claude Code",
            "codex":       "Codex",
            "openclaw":    "OpenClaw",
            "hermes":      "Hermes",
        }.get(platform_name, platform_name)
        installed_at = target_dir or PLATFORM_DEFAULTS.get(platform_name)
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
            print(f"    python install.py --platform <{' | '.join(other_platforms)}>")
            print()


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenMobius-skill cross-platform installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Mode (mutually exclusive) — default is install
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true",
                      help="Remove the entire platform install (use --purge "
                           "to also remove shared caches)")
    mode.add_argument("--update", action="store_true",
                      help="Update the skill (git pull + re-install --resume + regenerate SKILL.md)")

    parser.add_argument("--strict", action="store_true",
                        help="CI mode: fail fast on first error, no retry")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Prompt y/n before each step")
    parser.add_argument(
        "--platform", default="claude-code",
        choices=["claude-code", "codex", "openclaw", "hermes", "auto", "all"],
        help="Target agent platform (default: claude-code; "
             "'auto' = detect installed agents; 'all' = apply to all 4 platforms)",
    )
    parser.add_argument(
        "--target-dir", default=None,
        help="Override install path (default: platform-specific, e.g. ~/.claude/skills/OpenMobius-skill)",
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
                             "(chromium ~280MB + nomic model ~274MB). "
                             "Use only if no other skill needs them. "
                             "Requires --yes-i-know.")
    parser.add_argument("--yes-i-know", action="store_true",
                        help="(uninstall --purge) confirmation: yes, I understand global "
                             "caches affect other projects.")

    # Update-specific options
    parser.add_argument("--no-pull", action="store_true",
                        help="(update) skip git pull; only re-install + regenerate SKILL.md")
    parser.add_argument("--rebuild-index", action="store_true",
                        help="(update) force rebuild of vector index "
                             "(only needed when knowledge_base/{concepts,cases} changed)")

    args = parser.parse_args()

    # Resolve --platform auto
    if args.platform == "auto":
        detected = detect_platforms()
        if not detected:
            warn("--platform auto: no known platform dirs found "
                 "(.claude / .codex / .openclaw / .hermes). Defaulting to claude-code.")
            args.platform = "claude-code"
        elif len(detected) == 1:
            args.platform = detected[0]
            ok(f"--platform auto: detected {args.platform}")
        else:
            print(f"\n  Multiple platforms detected: {', '.join(detected)}")
            print(f"  Pick one with: python install.py --platform <name>")
            print(f"  Or apply to all: python install.py --platform all\n")
            return 2

    # Resolve --platform all
    target_dir_arg = Path(args.target_dir).expanduser() if args.target_dir else None
    if args.platform == "all":
        if target_dir_arg is not None:
            fail("--platform all is incompatible with --target-dir")
            return 2
        platforms_to_apply = list(PLATFORM_DEFAULTS.keys())
    else:
        platforms_to_apply = [args.platform]

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
