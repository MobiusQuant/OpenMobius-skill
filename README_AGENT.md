# Agent Install Guide for OpenMobius-skill

This file is written for **AI agents** (Claude Code / Codex / OpenClaw /
Hermes / Cursor / WorkBuddy) that the user has asked to install OpenMobius-skill on
their machine. Read it end-to-end before executing, then run the steps
in order. **Do not skip steps.**

If you are a human, see [`README.md`](./README.md) and
[`INSTALL.md`](./INSTALL.md) instead — they're optimised for human
readers.

Command blocks use POSIX syntax unless they are labelled PowerShell. On
Windows, invoke the installer with `py -3`, run installed Python tools with
`.venv\Scripts\python.exe`, and use the operating system's temporary directory
instead of a literal `/tmp` path.

---

## Trigger phrases

Run the procedure below when the user says any of (case-insensitive,
free-form, both languages):

**中文**

- 帮我装 OpenMobius / 帮我装 mobius / 帮我装 mobius-skill
- 帮我装这个交易 skill / 装一下 ICT skill
- 安装 OpenMobius-skill / 安装 mobius 交易知识

**English**

- install OpenMobius / install OpenMobius-skill
- install the mobius trading skill / install ICT skill
- set up OpenMobius-skill on this machine
- add the mobius market-analysis skill

If the user clearly intends to install but uses different wording,
also proceed.

---

## Step 1 — Pre-flight checks

Verify the prerequisites are present:

```bash
python3 --version 2>&1 || echo "MISSING: python3"
git --version    2>&1 || echo "MISSING: git"
```

On Windows PowerShell:

```powershell
py -3 --version
git --version
```

**Success criterion**

- `python3 --version` (or `py -3 --version` on Windows) reports `Python 3.10`
  or higher (3.10 / 3.11 / 3.12 /
  3.13 / 3.14 all OK).
- `git --version` reports any version.

**Failure handling**

| Symptom | Action |
|---|---|
| `python3: command not found`, or `py` is unavailable on Windows | Tell user: install Python 3.10+ from <https://www.python.org/downloads/> or via their package manager. Halt — do not proceed until user confirms install. |
| Python version < 3.10 | Tell user: upgrade Python to 3.10 or newer. Halt. |
| `git: command not found` | Tell user: install git. macOS: `xcode-select --install`; Linux: `sudo apt install git` / `sudo dnf install git`. Halt. |

For WorkBuddy 4.6.3 or later, **Settings** can detect a missing Python/Node.js
runtime and offer one-click installation. After using that feature, rerun the
version check: this Skill still requires Python 3.10 or newer.

---

## Step 2 — Choose the target agent platform

If you (the agent) already know which platform you're running on (e.g.
you're Claude Code → use `claude-code`), use it directly.

Otherwise, detect installed platforms:

```bash
[ -d ~/.claude ] && echo "found: Claude Code"
if [ -d ~/.agents ] || [ -d ~/.codex ]; then
    echo "found: Codex"
fi
[ -d ~/.openclaw ] && echo "found: OpenClaw"
[ -d ~/.hermes ] && echo "found: Hermes"
[ -d ~/.cursor ] && echo "found: Cursor"
[ -n "$OPENCLAW_STATE_DIR" ] && [ -d "$OPENCLAW_STATE_DIR" ] && \
    echo "found OpenClaw state: $OPENCLAW_STATE_DIR"
[ -n "$HERMES_HOME" ] && [ -d "$HERMES_HOME" ] && \
    echo "found Hermes home: $HERMES_HOME"
```

- **0 found** → ask user which agent they're using.
- **1 found** → use it.
- **2+ found** → ask user which one to install into (or, on Linux/macOS, offer
  `--platform all`, which covers the five hosts with official local paths).

Map the user's answer to a flag:

| Agent | `--platform` flag | Install target dir |
|---|---|---|
| Claude Code | `claude-code` | `~/.claude/skills/openmobius-skill/` |
| Codex | `codex` | `~/.agents/skills/openmobius-skill/` |
| OpenClaw *(Linux/macOS)* | `openclaw` | `<OPENCLAW_STATE_DIR or ~/.openclaw>/skills/openmobius-skill/` |
| Hermes *(Linux/macOS)* | `hermes` | `<HERMES_HOME or ~/.hermes>/skills/market-data/openmobius-skill/` |
| Cursor | `cursor` | `~/.cursor/skills/openmobius-skill/` |
| WorkBuddy | local ZIP import / marketplace | Use **Skills → Add Skill → Upload Skill**, or install a published copy from the Skill Marketplace |
| All local hosts *(Linux/macOS)* | `all` | the five local targets above; excludes WorkBuddy |

For a local-path host, save the chosen flag as `<FLAG>` for the next steps.

The current OpenClaw and Hermes host adapters/manifests target Linux and
macOS. On Windows, choose Claude Code, Codex, or Cursor explicitly. Do not use
`all`: its preflight fails rather than silently skipping unsupported hosts.

If the user chose WorkBuddy, do not invent a local target and do not confuse
WorkBuddy with CodeBuddy. Follow the matching WorkBuddy route below instead of
Steps 3–6.

### WorkBuddy installation and publishing routes

WorkBuddy's public documentation does not define a fixed filesystem directory
that a third-party installer can write to for automatic discovery. There are
three distinct routes; do not conflate them.

#### Route A — Import this repository's local ZIP

1. Tell the user that the repository's deterministic builder will create a
   local-import ZIP. WorkBuddy's Skill format currently accepts `.zip` files up
   to 3 MB; the builder enforces a conservative 3,000,000-byte ceiling.
2. Build the package:

   ```bash
   OPENMOBIUS_WORKBUDDY_SRC="$(mktemp -d "${TMPDIR:-/tmp}/openmobius-workbuddy.XXXXXX")"
   git clone --depth 1 https://github.com/MobiusQuant/OpenMobius-skill "$OPENMOBIUS_WORKBUDDY_SRC"
   cd "$OPENMOBIUS_WORKBUDDY_SRC"
   python3 scripts/build_workbuddy_package.py \
     --output /tmp/openmobius-skill-workbuddy.zip
   cd "${TMPDIR:-/tmp}"
   rm -rf -- "$OPENMOBIUS_WORKBUDDY_SRC"
   ```

   On Windows PowerShell, use `py -3` and the Windows temporary directory:

   ```powershell
   $sourceDir = Join-Path $env:TEMP ("openmobius-workbuddy-" + [guid]::NewGuid().ToString("N"))
   $archive = Join-Path $env:TEMP "openmobius-skill-workbuddy.zip"
   git clone --depth 1 https://github.com/MobiusQuant/OpenMobius-skill $sourceDir
   Set-Location $sourceDir
   py -3 scripts\build_workbuddy_package.py --output $archive
   Set-Location $env:TEMP
   Remove-Item -LiteralPath $sourceDir -Recurse -Force
   ```

3. In WorkBuddy, open **Skills → Add Skill → Upload Skill**, then drag or select
   `/tmp/openmobius-skill-workbuddy.zip` (or the `$archive` path on Windows).
   WorkBuddy automatically configures the Skill after import. If you cannot
   operate the desktop UI, hand the exact path and navigation to the user.
4. Do not claim success until the import succeeds and the Skill appears under
   **Installed**. Report the imported ZIP, not a fabricated filesystem path.
   This package is not included in `--platform all`.

#### Route B — Install a published marketplace copy

If the user identifies an existing published copy, open
**Experts · Skills · Connectors → Skills → Skill Marketplace** and click the
`+` on its card. Verify that it appears under **Installed**. Do not claim that
this repository has a marketplace listing unless the listing is actually
visible.

#### Route C — Publish for other users

Use the WorkBuddy Open Platform only when the user explicitly asks to publish
the Skill. Creation, ZIP parsing, review, and publication are separate from
local import. Report the exact state shown by the platform; a parsed or
submitted ZIP is not automatically installed locally and is not necessarily
published in the marketplace.

The ZIP contains a checksummed, Python-standard-library-only lexical corpus
that reconstructs all 2,144 School projections and 18,645 exact-source evidence
records. School
and source hard filters remain available. Canonical fused-card and vector
auto/hybrid/semantic retrieval are intentionally unavailable in the WorkBuddy
package and must fail closed; do not broaden the requested scope.
The package manifest binds the compact corpus, School registry, and alias map
by size and SHA-256. The standard retrieval CLI may therefore use its explicit
immutable-bundle read lease if a host denies first-run external lock-file
creation; it must not bypass genuine lock contention or an invalid manifest.
The ZIP neither bundles Python nor creates a venv or installs packages.
Script-backed features require Python 3.10+. WorkBuddy 4.6.3 and later can
detect a missing Python/Node.js runtime from **Settings** and offer one-click
installation, after which the Python version must still be verified. If
`<PYTHON>` cannot be resolved, report script-backed knowledge and market
operations unavailable. Q&A/text-market operations use the standard library;
PNG rendering and annotation additionally need host-provided
Playwright/Chromium or Pillow.

`python3 install.py --platform workbuddy --target-dir <path>` exists only for
developer staging and validation. It does not register a locally discoverable
WorkBuddy skill and is not the normal user installation flow.

---

## Step 3 — Tell the user what's about to happen

Before triggering the install, surface this to the user (in their
language, but the bullets must include the time and size numbers
verbatim):

> "Installing OpenMobius-skill to `<INSTALL_TARGET>`. This will take
> roughly 5–10 minutes on first run (downloads ~280 MB Playwright
> chromium and ~547 MB / 522 MiB nomic embedding weights). After verification, this
> procedure deletes its temporary source clone — your agent skills dir
> becomes the self-contained install. The vector index uses the bundled,
> verified v2 seed instead of recomputing every knowledge document."

Wait for user confirmation (Y/proceed/继续/好) before running Step 4.

---

## Step 4 — Run the installer

```bash
OPENMOBIUS_INSTALL_SRC="$(mktemp -d "${TMPDIR:-/tmp}/openmobius-install.XXXXXX")"
git clone --depth 1 https://github.com/MobiusQuant/OpenMobius-skill "$OPENMOBIUS_INSTALL_SRC"
cd "$OPENMOBIUS_INSTALL_SRC"
python3 install.py --platform <FLAG>
```

On Windows PowerShell:

```powershell
$sourceDir = Join-Path $env:TEMP ("openmobius-install-" + [guid]::NewGuid().ToString("N"))
git clone --depth 1 https://github.com/MobiusQuant/OpenMobius-skill $sourceDir
Set-Location $sourceDir
py -3 install.py --platform <FLAG>
```

**Monitor `stdout`** for these milestones (in order). Progress prefixes are
monotonic labels such as `[1]`; do not require a fixed denominator:

1. `Checking Python ≥ 3.10` (or `Python version`)
2. `Virtual env`
3. `Python dependencies`
4. `Playwright chromium`
5. `CJK fonts`
6. `Embedding model`
7. `Vector index`
8. `Skill registration` (or `Wrote .../SKILL.md`)
9. `Doctor`

The final summary should show `✓` on every line.

**Success criterion**

- `install.py` exits with code 0.
- Final summary shows `✓ Installation complete`.

**Failure handling**

| Symptom in stdout | Likely cause | Action |
|---|---|---|
| `ensurepip is not available` | Debian/Ubuntu missing `python3-venv` | Tell user: `sudo apt install python3.X-venv` (use detected X), then re-run Step 4. |
| `playwright install chromium` fails mid-download | Network or disk | Tell user: check disk space + network; optionally set `PLAYWRIGHT_BROWSERS_PATH` to a writable dir; re-run Step 4. |
| `SentenceTransformer` / `nomic-embed-text` download fails | HuggingFace.co network | Tell user: try later, or set `HF_HUB_OFFLINE=0` and `HF_ENDPOINT` to a mirror. |
| `[load] X cards have missing / stale embeddings` | KB shipped without embeddings | Re-run with the selected target's managed Python: `<target>/.venv/bin/python <target>/scripts/build_index.py --regenerate --force` (Windows: `<target>/.venv/Scripts/python.exe ...`). This is slow and unexpected on a fresh clone, so also report it as a bug. |
| `v2 records 尚未可用` / legacy manifest warning | Existing index predates School/source evidence | Run `.venv/bin/python scripts/build_index.py --upgrade`; the working index is replaced only after staged verification. |
| Index OOM on low-RAM machine | <2 GB free | Tell user: close memory-heavy programs and retry. For reduced-quality diagnostics only, run `.venv/bin/python scripts/build_index.py --v2-embedding-strategy inherit --force`. |
| Any other unexpected exit code | — | Re-run with `python3 install.py --platform <FLAG> --strict -v` to surface the real error, then report to the user. |

---

## Step 5 — Cleanup the temporary clone

After Step 4 succeeds:

```bash
cd "${TMPDIR:-/tmp}"
rm -rf -- "$OPENMOBIUS_INSTALL_SRC"
```

On Windows PowerShell, after leaving the cloned directory:

```powershell
Set-Location $env:TEMP
Remove-Item -LiteralPath $sourceDir -Recurse -Force
```

The exact local install target listed in Step 2 is self-contained — the unique
directory returned by `mktemp` is not referenced anywhere going forward and is
safely removable by its captured path.

---

## Step 6 — Verify the install

```bash
cd <EXACT_INSTALL_TARGET>
.venv/bin/python scripts/kb_doctor.py \
    --platform <FLAG> --expected-dir "$PWD"
```

On Windows PowerShell:

```powershell
Set-Location "<EXACT_INSTALL_TARGET>"
.\.venv\Scripts\python.exe scripts\kb_doctor.py `
    --platform <FLAG> --expected-dir (Get-Location).Path
```

Use the exact target for the user's platform:

| Platform | Path prefix |
|---|---|
| Claude Code | `~/.claude/skills/openmobius-skill/` |
| Codex | `~/.agents/skills/openmobius-skill/` |
| OpenClaw | `<OPENCLAW_STATE_DIR or ~/.openclaw>/skills/openmobius-skill/` |
| Hermes | `<HERMES_HOME or ~/.hermes>/skills/market-data/openmobius-skill/` |
| Cursor | `~/.cursor/skills/openmobius-skill/` |

OpenClaw and Hermes entries above apply only on Linux/macOS.

For Cursor Cloud Agents, remote SSH, and other remote environments, local
user skills under `~/.cursor/skills` are not synchronized. Install the skill
inside the repository at `.cursor/skills/openmobius-skill/` and run the doctor
from that project copy.

**Success criterion**

- All check items report ✓, except possibly `Mobius API` (optional;
  network may be down — non-fatal).

**Failure handling**

| Doctor item | Action |
|---|---|
| `Python 虚拟环境` ✗ | The venv was not created correctly. Re-run Step 4 with `--no-resume`. |
| `Python 包` ✗ | Some pip install failed. Re-run Step 4 with `--strict -v` to see which. |
| `Embedding 模型` ✗ | nomic model didn't reach cache. Re-run Step 4 — Playwright/model steps are idempotent. |
| `知识库索引` ✗ | Vector index didn't build. Run `<target>/.venv/bin/python <target>/scripts/build_index.py --force` manually. |
| `Skill 安装` ✗ | The SKILL.md was not written. Re-run Step 4 — SKILL.md generation is idempotent. |
| `Mobius API` ✗ | Non-fatal. Skill still works for concept Q&A and pasted-data analysis. |

---

## Step 7 — Report back to the user

For a local-path host, tell the user (in their language) the following, including the
specific items in **bold**:

1. **Skill installed** to `<EXACT INSTALL TARGET PATH>`.
2. The skill auto-activates for these question types:
   - ICT/SMC concept questions ("什么是 FVG", "how to identify OB")
   - Chart-image analysis (user attaches a trading chart)
   - Asset+timeframe queries ("BTC 1h 怎么样", "茅台日线分析")
   - Image annotation requests
3. **Refresh the host's skill catalog as needed.** Claude Code detects changes
   in an existing skills directory live; when the top-level directory was just
   created, or on a host that snapshots skills per session, start a new
   session or restart that host.
4. After the skill is visible, the user can try:
   - "What is Liquidity Sweep?"
   - "ETH 4h 现在怎么样"

Example user-facing message (English):

> ✓ OpenMobius-skill installed to `~/.agents/skills/openmobius-skill/`.
> The skill auto-activates on ICT/SMC trading questions, chart
> analysis, asset+timeframe queries, and image annotation. If it does not
> appear in the current session, refresh the skill catalog or start a new
> Codex session — then try asking
> "What is Fair Value Gap?" or "ETH 1h 怎么样".

For WorkBuddy, report the exact outcome: local ZIP **imported and visible under
Installed**, marketplace copy **installed**, or Open Platform submission
**parsed/submitted/under review/published**. These states are not
interchangeable. Do not report a filesystem install path; follow any reload or
activation instruction shown by WorkBuddy itself.

---

## Things you (the agent) should NOT do

- **Do not** edit or patch `install.py` to bypass errors. If a step
  fails, report it to the user and ask for guidance.
- **Do not** skip the doctor check (Step 6). It's the only way to know
  the install actually works.
- **Do not** re-install if the target dir already contains a working
  install. Use `python3 install.py --update --platform <FLAG>` instead.
- **Do not** claim a local install on an unsupported environment (iOS,
  web-only agents). Tell the user that local execution requires Python 3.10+.
- **Do not** invent a WorkBuddy filesystem directory or substitute CodeBuddy's
  behavior. Use the local ZIP import or marketplace workflow documented above.
- **Do not** use the WorkBuddy Open Platform as a synonym for local
  installation, or claim publication merely because a ZIP parsed successfully.
- **Do not** install user-global caches (chromium / nomic model) to
  unusual locations unless the user explicitly requested via
  `PLAYWRIGHT_BROWSERS_PATH` or `HF_HOME`.

---

## If the user asks to uninstall

The commands below apply to local-path hosts (`all` covers all five only on
Linux/macOS). In WorkBuddy, manage or uninstall the Skill from **Skills →
Installed**; disabling it is not the same as uninstalling it.

```bash
OPENMOBIUS_UNINSTALL_SRC="$(mktemp -d "${TMPDIR:-/tmp}/openmobius-uninstall.XXXXXX")"
git clone --depth 1 https://github.com/MobiusQuant/OpenMobius-skill "$OPENMOBIUS_UNINSTALL_SRC"
# Run exactly one of the next two commands. The second also removes shared
# caches (chromium ~280 MB, nomic weights ~547 MB / 522 MiB; other tools may share them):
python3 "$OPENMOBIUS_UNINSTALL_SRC/install.py" --uninstall --platform <FLAG>
# python3 "$OPENMOBIUS_UNINSTALL_SRC/install.py" --uninstall --platform <FLAG> --purge --yes-i-know
rm -rf -- "$OPENMOBIUS_UNINSTALL_SRC"
```

On Windows PowerShell, clone to a directory below `$env:TEMP`, then run:

```powershell
$sourceDir = Join-Path $env:TEMP ("openmobius-uninstall-" + [guid]::NewGuid().ToString("N"))
git clone --depth 1 https://github.com/MobiusQuant/OpenMobius-skill $sourceDir
py -3 (Join-Path $sourceDir "install.py") --uninstall --platform <FLAG>
# Add --purge --yes-i-know only when the user explicitly chose shared-cache removal.
Remove-Item -LiteralPath $sourceDir -Recurse -Force
```

---

## If the user asks to update

The command below applies to a local-path install. For a WorkBuddy local
import, rebuild the ZIP from current source and import the updated package
through **Skills → Add Skill → Upload Skill**. Marketplace updates and Open
Platform publication are separate workflows; follow the state and controls
shown by WorkBuddy for the relevant route.

```bash
# Update one platform (currently installed):
python3 <EXACT_INSTALL_TARGET>/install.py --update --platform <FLAG>
```

On Windows PowerShell:

```powershell
py -3 "<EXACT_INSTALL_TARGET>\install.py" --update --platform <FLAG>
```

This will:
1. Clone the latest upstream code to a fresh `/tmp` dir.
2. Re-copy source files into the install target (overwrites).
3. Re-run install steps in resume mode (skips already-done work).
4. Regenerate `SKILL.md`.
5. Clean up the `/tmp` clone.

---

## Official host specifications

Use these primary references if a host changes its discovery or manifest
rules:

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

---

## Repository URL (for fetching this file fresh)

```
https://raw.githubusercontent.com/MobiusQuant/OpenMobius-skill/main/README_AGENT.md
```

The agent may `WebFetch` this URL at the start of the install
procedure to ensure it's working from the latest instructions.
