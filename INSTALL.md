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

**Each installer-managed local platform uses a self-contained directory.**
Source files are **copied** (not symlinked) into the per-platform target. The clone you
start from is purely a source-bundle — once the install finishes, you can
delete the clone.

```
OPENMOBIUS_SRC="$(mktemp -d "${TMPDIR:-/tmp}/openmobius-src.XXXXXX")"
git clone https://github.com/MobiusQuant/OpenMobius-skill "$OPENMOBIUS_SRC"
cd "$OPENMOBIUS_SRC"
python3 install.py --platform claude-code    # → ~/.claude/skills/openmobius-skill/

cd "${TMPDIR:-/tmp}"
rm -rf -- "$OPENMOBIUS_SRC"                   # ✓ exact mktemp directory only
```

The target dir owns its own `.venv` and `_index`. Removing the target
(`python3 install.py --uninstall`) is a complete uninstall for that platform.

User-global caches (the open-source nomic embedding model and Playwright
chromium) stay in your OS's standard cache locations — shared across
platforms by design, so installing N platforms does not re-download them
N times.

Network access is used for dependency/model/browser downloads, the optional
Mobius API doctor check (`api.mobiusquant.ai`), and `github.com` when
`--update` obtains fresh source. See [PRIVACY.md](./PRIVACY.md) for the full
service, data, and cache breakdown.

## One-line install

| OS | Command |
|---|---|
| **macOS / Linux** | `bash install.sh` &nbsp;or&nbsp; `python3 install.py` |
| **Windows (PowerShell)** | `.\install.ps1` &nbsp;or&nbsp; `py -3 install.py` |
| **Windows (cmd)** | `py -3 install.py` |

The installer is **idempotent** — re-running skips already-done steps. First run takes 5–10 min (downloads). Subsequent runs are <30 s.

## Target agent platform

Pick your platform:

| Agent | Flag | Install path / setup route |
|---|---|---|
| **Claude Code** (default) | `--platform claude-code` (or omit) | `~/.claude/skills/openmobius-skill/` |
| **Codex** | `--platform codex` | `~/.agents/skills/openmobius-skill/` |
| **OpenClaw** *(Linux/macOS)* | `--platform openclaw` | `<OPENCLAW_STATE_DIR or ~/.openclaw>/skills/openmobius-skill/` |
| **Hermes** *(Linux/macOS)* | `--platform hermes` | `<HERMES_HOME or ~/.hermes>/skills/market-data/openmobius-skill/` |
| **Cursor** | `--platform cursor` | `~/.cursor/skills/openmobius-skill/` |
| **WorkBuddy** | local ZIP import / marketplace | `Skills → Add Skill → Upload Skill`; published copies install from the marketplace |

Other options:

- `--platform auto` — Detect supported local agent roots.
- `--target-dir <path>` — Override the default install path.
- `--platform all` — On Linux/macOS, install to all five platforms with
  documented local paths in one go. It intentionally excludes WorkBuddy.
  On Windows this preflight fails; select Claude Code, Codex, or Cursor
  explicitly instead.

If set, `OPENCLAW_STATE_DIR` replaces `~/.openclaw` as OpenClaw's state root,
and `HERMES_HOME` replaces `~/.hermes` as Hermes's root. The skill slug and
installed directory name are always lowercase: `openmobius-skill`.

## Installing on multiple agents

```bash
python3 install.py --platform all
```

This command is supported on Linux/macOS. On Windows, select Claude Code,
Codex, or Cursor explicitly because the current OpenClaw and Hermes adapters
do not declare Windows support.

Each platform gets its own complete install (its own `.venv`, its own
`_index`). The shared caches (nomic weights ~547 MB / 522 MiB, Playwright chromium ~280MB)
are downloaded **once** and shared via your OS's user-global cache, so
installing all five local-path platforms doesn't multiply the download.

### Cursor Cloud and remote environments

`~/.cursor/skills/openmobius-skill/` is a user-level skill on the current
machine. Cursor does not synchronize local user skills to Cloud Agents,
remote SSH sessions, or other remote environments. For those environments,
install or copy the skill into the repository at
`.cursor/skills/openmobius-skill/` so it travels with that project.

### WorkBuddy installation routes

WorkBuddy is a separate product from CodeBuddy. Its public documentation does
not define a fixed filesystem directory that a third-party installer can write
to for automatic discovery, so this project does not invent a target and
`--platform all` does not include it.

For a package built from this repository, create the ZIP below. WorkBuddy's
Skill format currently accepts `.zip` packages up to 3 MB; this builder
enforces a conservative 3,000,000-byte ceiling and atomically preserves an
existing output if that check fails:

```bash
python3 scripts/build_workbuddy_package.py \
  --output /tmp/openmobius-skill-workbuddy.zip
```

Then open **Skills → Add Skill → Upload Skill** in WorkBuddy and drag or select
the resulting ZIP. WorkBuddy configures the Skill after import. Do not report
the local installation as complete until the import succeeds and the Skill is
visible under **Installed**.

For a Skill that is already published, open
**Experts · Skills · Connectors → Skills → Skill Marketplace** and click the
`+` on its card. Publishing a new or updated Skill for other users is a
separate WorkBuddy Open Platform workflow involving creation, ZIP parsing,
review, and publication. An accepted Open Platform submission is not evidence
of a local installation or completed marketplace publication.

The generated package stores a checksummed compact representation of all 2,144
School projections and 18,645 exact-source evidence records. Its
Python-standard-library-only runtime supports `--layer school` or
`--layer evidence` with
`--search-mode lexical` and hard School/source filters. It omits the canonical
fused-card layer and all vector/model artifacts, so canonical and
auto/hybrid/semantic retrieval fail closed in WorkBuddy.
Its read-only manifest binds the compact corpus, School registry, and alias map
by size and SHA-256, so the normal CLI remains usable when the host denies
first-run creation of an external generation-lock file.
The ZIP does not bundle Python, create a virtual environment, or install
dependencies. Script-backed features require Python 3.10+. WorkBuddy 4.6.3
and later can detect missing Python/Node.js from **Settings** and offer
one-click installation; verify that the installed Python still meets this
Skill's 3.10+ minimum. Without a usable Python launcher, script-backed
knowledge and market operations are unavailable. Q&A and text-market output
otherwise use the standard library; PNG rendering and image annotation
require host-provided Playwright/Chromium and Pillow.

The installer also accepts `--platform workbuddy --target-dir <path>` for
developer staging and validation only. That command does not create a
locally discoverable WorkBuddy installation; normal users should use the local
ZIP import or marketplace route above.

## Installation stages

| # | Step | First run | Subsequent |
|---|---|---|---|
| 0 | Stage source files → target dir (copy) | <1 s | overwrites |
| 1 | Check Python ≥ 3.10 | <1 s | <1 s |
| 2 | Create `<target>/.venv/` | ~5 s | skip if exists |
| 3 | `pip install -r requirements.txt` into `<target>/.venv/` | ~3 min | seconds (pip wheel cache) |
| 4 | Playwright chromium (~280 MB, user-global cache) | ~1 min | skip if cached |
| 5 | CJK font check (warn only) | <1 s | <1 s |
| 6 | Pre-warm pinned nomic-embed weights (~547 MB / 522 MiB, user-global cache) | ~30 s | skip if cached |
| 7 | Build/verify canonical + independently embedded School/evidence collections | release-seed index build; only local misses are embedded | fingerprint + seed/cache check; only misses are embedded |
| 8 | Generate `<target>/SKILL.md` (platform frontmatter + shared body) | <1 s | overwrites |
| 9 | Run `kb_doctor` health check | ~5 s | ~5 s |

## Common options

```bash
python3 install.py                  # default: Claude Code, non-interactive, resume
python3 install.py --strict         # CI: fail fast, no retry
python3 install.py -i               # deprecated compatibility flag; warns and remains non-interactive
python3 install.py --no-register    # don't generate the platform SKILL.md
python3 install.py --skip-fonts     # skip CJK font check
python3 install.py --skip-chromium  # skip Playwright install
python3 install.py --skip-doctor    # skip final health check
python3 install.py --no-resume      # re-run every stage (don't skip cached)
```

The commands above use POSIX notation. On Windows, replace the leading
`python3` with `py -3` (or invoke `.\install.ps1` with the same flags).
`-i` remains accepted only for command-line compatibility: it prints a warning
and continues non-interactively.

Combined example — re-build everything from scratch:

```bash
rm -rf .venv knowledge_base/_index
python3 install.py --no-resume
```

## Windows specifics

- The installer copies the source bundle into the selected target, so skill
  installation does not require symlink privileges.
- Playwright cache lives at `%LOCALAPPDATA%\ms-playwright\` (not `~/.cache/`).
- The current OpenClaw and Hermes adapters/manifests are Linux/macOS-only.
  On Windows, install for Claude Code, Codex, or Cursor explicitly;
  `--platform all`, `--platform openclaw`, and `--platform hermes` fail
  preflight.

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
                     SentenceTransformer(\
                       'nomic-ai/nomic-embed-text-v1.5', \
                       revision='e9b6763023c676ca8431644204f50c2b100d9aab', \
                       trust_remote_code=False)"

# 6. Build vector index
.venv/bin/python scripts/build_index.py

# 7. Register this manual checkout with Claude Code (symlink)
mkdir -p ~/.claude/skills
ln -sf "$(pwd)" ~/.claude/skills/openmobius-skill

# 8. Verify
.venv/bin/python scripts/kb_doctor.py
```

## Smoke test (post-install)

```bash
cd ~/.claude/skills/openmobius-skill

# Test the default strict ICT/SMC knowledge route
.venv/bin/python scripts/kb_retrieve.py "what is FVG" \
  --layer school --schools ICT SMC --top-k 3

# Test an exact School/source intersection without loading the embedder
.venv/bin/python scripts/kb_retrieve.py "Order Block" \
  --layer evidence --schools SMC --sources Teach-Wuyuan --explain-scope

# Test Mobius API resolve
.venv/bin/python scripts/kb_klines.py resolve "比特币"

# Test full chart pipeline
.venv/bin/python scripts/kb_klines.py chart --query BTC --interval 1h --limit 100 --output /tmp/t.json
.venv/bin/python scripts/kb_klines.py render --input /tmp/t.json --output /tmp/t.png --width 1200 --height 700
ls -l /tmp/t.png
```

Each smoke test above should succeed.

## Uninstall

The installer handles uninstall too — same `install.py`, different flag.

```bash
# Standard uninstall: remove the entire self-contained platform install,
# including its .venv and vector index
python3 install.py --uninstall                           # current platform (default claude-code)
python3 install.py --uninstall --platform codex          # specific platform
python3 install.py --uninstall --platform all            # Linux/macOS: all 5 local hosts

# Full purge: also remove global caches (chromium ~280MB + nomic weights ~547 MB / 522 MiB)
# WARNING: these caches may be shared by other projects on your machine!
python3 install.py --uninstall --purge --yes-i-know
```

Cleanup levels:

| Flag | Removes |
|---|---|
| (default) | Entire selected `openmobius-skill/` target, including `.venv/` and `knowledge_base/_index/` |
| `--purge --yes-i-know` | + Playwright browser cache `chromium*` (`~/.cache/ms-playwright` Linux · `~/Library/Caches/ms-playwright` macOS · `%LOCALAPPDATA%\ms-playwright` Windows) + `~/.cache/huggingface/hub/models--nomic-*` |

`--full` is deprecated. It remains accepted for backward compatibility but
has no effect because standard uninstall already removes all per-platform
files and build artifacts.

**Not removed** (you delete manually if you want):
- The cloned repo at `<your-clone-dir>` — just `rm -rf` it

## Updating

```bash
# Full update: fetch a fresh upstream copy + reinstall deps + regenerate SKILL.md
python3 install.py --update

# Update without pulling (you've already pulled manually)
python3 install.py --update --no-pull

# Update all five installed local-path platforms
python3 install.py --update --platform all

# Force rebuild vector index (after knowledge_base/concepts or cases changed)
python3 install.py --update --rebuild-index
```

`--update` runs:

1. Clone a fresh upstream copy into a temporary directory (unless `--no-pull`)
2. `install.py` in resume mode (skip already-done steps; auto-install new deps)
3. **Regenerate `SKILL.md`** at target (frontmatter from `platforms/<name>.yaml` + body from `SKILL.body.md`)
4. Check the index manifest and safely add/rebuild v2 collections when needed;
   `--rebuild-index` requests a staged full replacement
5. Run `kb_doctor.py` to verify everything works

You can also rebuild the index alone, without a full update:

```bash
.venv/bin/python scripts/build_index.py --force
```

To upgrade a legacy index only when its v2 fingerprint is missing or stale:

```bash
.venv/bin/python scripts/build_index.py --upgrade
```

The canonical collection loads its bundled vectors. School/evidence records
use independent vectors from the verified, sharded release seed first; only
locally changed or missing documents are embedded and cached under
`knowledge_base/_embedding_cache/`. Unchanged upgrades normally reuse the seed
or cache without loading the model for documents. Index construction happens
in a staging directory, so an embedding or validation failure leaves the live
index intact.

For low-memory diagnostics only, `--v2-embedding-strategy inherit` reuses
canonical parent vectors. This is a reduced-quality compatibility path, not the
release default.

## Troubleshooting

Run the doctor from the installed copy so it validates that copy's manifest,
lowercase slug, dependencies, and index. You can make the target check
explicit:

```bash
cd ~/.claude/skills/openmobius-skill
.venv/bin/python scripts/kb_doctor.py \
  --platform claude-code --expected-dir "$PWD"
```

| Symptom | Fix |
|---|---|
| `Python 3.x is too old` | Install Python 3.10+ (see Prerequisites) |
| `pip install` hangs | Try `--strict` to see real errors; check network/proxy |
| `playwright install chromium` fails | Set `PLAYWRIGHT_BROWSERS_PATH` to a writable location |
| First native v2 build OOM (low-RAM machine) | Close memory-heavy apps and retry; for reduced-quality diagnostics use `scripts/build_index.py --v2-embedding-strategy inherit --force` |
| Chinese labels render as boxes | Install `fonts-noto-cjk` (Linux); macOS/Windows usually bundled |
| Skill not invoked in Claude Code | Check `~/.claude/skills/openmobius-skill` exists. Claude Code watches an existing skills directory live; if that top-level directory was newly created, start a new session. |
| Skill not found by Codex | Check `~/.agents/skills/openmobius-skill` exists; restart Codex |
| OpenClaw uses an unexpected path | Check `OPENCLAW_STATE_DIR`; without it the target is `~/.openclaw/skills/openmobius-skill` |
| Hermes uses an unexpected path | Check `HERMES_HOME`; without it the target is `~/.hermes/skills/market-data/openmobius-skill` |
| Cursor Cloud/remote misses the user skill | Install it in project `.cursor/skills/openmobius-skill`; remote environments do not receive local `~/.cursor/skills` |
| WorkBuddy cannot find a local folder | Do not guess a local directory. Generate the ZIP, then use **Skills → Add Skill → Upload Skill** and confirm it appears under **Installed** |
| WorkBuddy rejects an oversized ZIP | Rebuild with the current builder; it enforces the documented 3 MB limit and reports the exact byte count |

When in doubt: `python3 scripts/kb_doctor.py` reports exactly what's broken.

## Official skill specifications

- Common format: [Agent Skills specification](https://agentskills.io/specification)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Codex skills](https://learn.chatgpt.com/docs/build-skills)
- [OpenClaw skills](https://docs.openclaw.ai/tools/skills)
- [Hermes skill guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/creating-skills.md)
- [Cursor skills](https://cursor.com/docs/skills)
- [WorkBuddy local Skill guide](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)
- [WorkBuddy Skill format and marketplace](https://open.workbuddy.cn/docs/skill)
- [WorkBuddy Open Platform](https://open.workbuddy.cn/docs/what-is-open-platform)
- [WorkBuddy changelog](https://www.workbuddy.cn/docs/workbuddy/Changelog)
