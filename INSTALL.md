# Install Guide

## Prerequisites

You need **Python 3.10 or later**. The installer handles everything else.

| Platform | Install Python |
|---|---|
| **macOS** | `brew install python@3.12` &nbsp;or&nbsp; <https://www.python.org/downloads/> |
| **Ubuntu/Debian** | `sudo apt install python3.10 python3.10-venv` |
| **Fedora** | `sudo dnf install python3.10` |
| **Arch** | `sudo pacman -S python` |
| **Windows** | <https://www.python.org/downloads/> &nbsp;**☑ Add Python to PATH** |

Verify Python is installed:

```bash
python3 --version       # macOS / Linux
py --version            # Windows
```

Should print `Python 3.10.x` or higher.

## Installation model

**Each platform installs to a self-contained directory.** Source files are
**copied** (not symlinked) into the per-platform target. The clone you
start from is purely a source-bundle — once the install finishes, you can
delete the clone.

```
git clone https://github.com/MobiusQuant/OpenMobius-skill /tmp/openmobius-src
cd /tmp/openmobius-src
python install.py --platform claude-code     # → ~/.claude/skills/OpenMobius-skill/

rm -rf /tmp/openmobius-src                    # ✓ safe to delete
```

The target dir owns its own `.venv` and `_index`. Removing the target
(`python install.py --uninstall`) is a complete uninstall for that platform.

User-global caches (the open-source nomic embedding model and Playwright
chromium) stay in your OS's standard cache locations — shared across
platforms by design, so installing N platforms does not re-download them
N times.

## One-line install

| OS | Command |
|---|---|
| **macOS / Linux** | `bash install.sh` &nbsp;or&nbsp; `python3 install.py` |
| **Windows (PowerShell)** | `.\install.ps1` &nbsp;or&nbsp; `python install.py` |
| **Windows (cmd / Git Bash)** | `python install.py` |

The installer is **idempotent** — re-running skips already-done steps. First run takes 5–10 min (downloads). Subsequent runs are <30 s.

## Target agent platform

Pick your platform:

| Agent | Flag | Install path |
|---|---|---|
| **Claude Code** (default) | `--platform claude-code` (or omit) | `~/.claude/skills/OpenMobius-skill/` |
| **Codex** | `--platform codex` | `~/.codex/skills/OpenMobius-skill/` |
| **OpenClaw** | `--platform openclaw` | `~/.openclaw/skills/OpenMobius-skill/` |
| **Hermes** | `--platform hermes` | `~/.hermes/skills/market-data/OpenMobius-skill/` |

Other options:

- `--platform auto` — Detect installed agents by scanning `~/.<agent>` dirs.
- `--target-dir <path>` — Override the default install path.
- `--platform all` — Install to all four platforms in one go (each gets its own self-contained copy).

## Installing on multiple agents

```bash
python install.py --platform all
```

Each platform gets its own complete install (its own `.venv`, its own
`_index`). The shared caches (nomic ~274MB, Playwright chromium ~280MB)
are downloaded **once** and shared via your OS's user-global cache, so
installing all four platforms doesn't quadruple the download.

## What the installer does (9 steps)

| # | Step | First run | Subsequent |
|---|---|---|---|
| 0 | Stage source files → target dir (copy) | <1 s | overwrites |
| 1 | Check Python ≥ 3.10 | <1 s | <1 s |
| 2 | Create `<target>/.venv/` | ~5 s | skip if exists |
| 3 | `pip install -r requirements.txt` into `<target>/.venv/` | ~3 min | seconds (pip wheel cache) |
| 4 | Playwright chromium (~280 MB, user-global cache) | ~1 min | skip if cached |
| 5 | CJK font check (warn only) | <1 s | <1 s |
| 6 | Pre-warm nomic-embed model (~274 MB, user-global cache) | ~30 s | skip if cached |
| 7 | Build vector index from precomputed embeddings (2008 cards) | ~2 s | skip if exists |
| 8 | Generate `<target>/SKILL.md` (platform frontmatter + shared body) | <1 s | overwrites |
| 9 | Run `kb_doctor` health check | ~5 s | ~5 s |

## Common options

```bash
python install.py                  # default: auto, non-interactive, resume
python install.py --strict         # CI: fail fast, no retry
python install.py -i               # interactive: prompt y/n each step
python install.py --no-register    # don't symlink to ~/.claude/skills/
python install.py --skip-fonts     # skip CJK font check
python install.py --skip-chromium  # skip Playwright install
python install.py --skip-doctor    # skip final health check
python install.py --no-resume      # re-run every step (don't skip cached)
```

Combined example — re-build everything from scratch:

```bash
rm -rf .venv knowledge_base/_index
python install.py --no-resume
```

## Windows specifics

- `os.symlink` typically requires admin on Windows. The installer **falls back to a directory junction** (`mklink /J`) which works without admin.
- Junction target ↔ source behaves transparently for Claude Code.
- Playwright cache lives at `%LOCALAPPDATA%\ms-playwright\` (not `~/.cache/`).

## Manual install (if installer fails)

If `install.py` fails partway, you can resume by re-running it (`--resume` is default). For full manual setup:

```bash
# 1. venv
python3 -m venv .venv

# 2. dependencies
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. CJK fonts (Linux only; macOS/Windows usually bundled)
sudo apt install fonts-noto-cjk     # Debian/Ubuntu
# sudo dnf install google-noto-cjk-fonts   (Fedora)
# sudo pacman -S noto-fonts-cjk            (Arch)

# 4. Playwright chromium
.venv/bin/python -m playwright install chromium

# 5. Pre-warm embedding model
.venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
                     SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)"

# 6. Build vector index
.venv/bin/python scripts/build_index.py

# 7. Register with Claude Code (symlink)
mkdir -p ~/.claude/skills
ln -sf "$(pwd)" ~/.claude/skills/OpenMobius-skill

# 8. Verify
.venv/bin/python scripts/kb_doctor.py
```

## Smoke test (post-install)

```bash
cd ~/.claude/skills/OpenMobius-skill

# Test concept retrieval
.venv/bin/python scripts/kb_retrieve.py "what is FVG" --top-k 3

# Test Mobius API resolve
.venv/bin/python scripts/kb_klines.py resolve "比特币"

# Test full chart pipeline
.venv/bin/python scripts/kb_klines.py chart --query BTC --interval 1h --limit 100 --output /tmp/t.json
.venv/bin/python scripts/kb_klines.py render --input /tmp/t.json --output /tmp/t.png --width 1200 --height 700
ls -l /tmp/t.png
```

All four commands should succeed.

## Uninstall

The installer handles uninstall too — same `install.py`, different flag.

```bash
# Standard uninstall: remove the entire self-contained platform install,
# including its .venv and vector index
python install.py --uninstall                           # current platform (default claude-code)
python install.py --uninstall --platform codex          # specific platform
python install.py --uninstall --platform all            # all 4 platforms at once

# Full purge: also remove global caches (chromium ~280MB + nomic model ~274MB)
# WARNING: these caches may be shared by other projects on your machine!
python install.py --uninstall --purge --yes-i-know
```

Cleanup levels:

| Flag | Removes |
|---|---|
| (default) | Entire `~/.<platform>/skills/OpenMobius-skill/` target, including `.venv/` and `knowledge_base/_index/` |
| `--purge --yes-i-know` | + Playwright browser cache `chromium*` (`~/.cache/ms-playwright` Linux · `~/Library/Caches/ms-playwright` macOS · `%LOCALAPPDATA%\ms-playwright` Windows) + `~/.cache/huggingface/hub/models--nomic-*` |

`--full` is deprecated. It remains accepted for backward compatibility but
has no effect because standard uninstall already removes all per-platform
files and build artifacts.

**Not removed** (you delete manually if you want):
- The cloned repo at `<your-clone-dir>` — just `rm -rf` it

## Updating

```bash
# Full update: git pull + reinstall deps + regenerate SKILL.md
python install.py --update

# Update without pulling (you've already pulled manually)
python install.py --update --no-pull

# Update all installed platforms
python install.py --update --platform all

# Force rebuild vector index (after knowledge_base/concepts or cases changed)
python install.py --update --rebuild-index
```

`--update` runs:

1. `git pull` (unless `--no-pull`)
2. `install.py` in resume mode (skip already-done steps; auto-install new deps)
3. **Regenerate `SKILL.md`** at target (frontmatter from `platforms/<name>.yaml` + body from `SKILL.body.md`)
4. (optional) `build_index.py --force` if `--rebuild-index`
5. Run `kb_doctor.py` to verify everything works

You can also rebuild the index alone, without a full update:

```bash
.venv/bin/python scripts/build_index.py --force
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Python 3.x is too old` | Install Python 3.10+ (see Prerequisites) |
| `pip install` hangs | Try `--strict` to see real errors; check network/proxy |
| `playwright install chromium` fails | Set `PLAYWRIGHT_BROWSERS_PATH` to a writable location |
| Index build OOM (low-RAM machine) | Use `OPENAI_API_KEY` to switch to OpenAI embedding (smaller footprint) |
| Chinese labels render as boxes | Install `fonts-noto-cjk` (Linux); macOS/Windows usually bundled |
| Symlink fails on Windows | Installer auto-falls-back to junction; no admin needed |
| Skill not invoked in Claude Code | Check `~/.claude/skills/OpenMobius-skill` exists; restart Claude Code |

When in doubt: `python scripts/kb_doctor.py` reports exactly what's broken.
