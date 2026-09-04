# Privacy and Data Handling

`OpenMobius-skill` is a **local-first AI agent skill**. This document
describes every piece of data that leaves your machine, the destination,
and the purpose.

## Network endpoints

Depending on the selected install options and runtime request, the project may
contact the following services. Package managers and model/browser downloaders
can use mirrors or CDN hostnames operated by those services, so the exact CDN
hostname can change over time.

| Service / host | When | Purpose | What is sent |
|---|---|---|---|
| PyPI (`pypi.org`, `files.pythonhosted.org`, or the configured package-index mirror) | Install/update | Install declared Python dependencies from `requirements.txt` | Standard pip package-resolution and download requests |
| Hugging Face Hub (`huggingface.co` and its download/storage CDN, or a configured `HF_ENDPOINT`) | Install/update | Download the pinned open-source `nomic-embed-text-v1.5` model snapshot (Apache 2.0) when it is not already cached | Standard model-resolution and file-download requests; runtime loading uses the libraries' built-in implementation with `trust_remote_code` disabled |
| Playwright's browser download service/CDN (for example `cdn.playwright.dev` or `playwright.azureedge.net`) | Install/update | Download Chromium used for chart rendering when it is not already cached | Standard Playwright browser-download requests |
| `github.com` | Explicit `--update`, or when the user follows the documented clone workflow | Obtain the current OpenMobius-skill source | Standard Git HTTPS requests; no trading query is included |
| `api.mobiusquant.ai` | Install doctor + runtime | Health check and public symbol-resolution, OHLCV, indicator, or playbook requests | The health check sends no trading query. Runtime requests contain the explicitly requested asset, timeframe, indicator, and related public query parameters. **No authentication is required and no credentials are collected.** |

Setting `MOBIUS_API_BASE` intentionally replaces the default Mobius API host;
the scripts then contact the operator-supplied endpoint. Likewise, pip,
Hugging Face, and Playwright environment configuration can redirect their
downloads to user-selected mirrors.

The WorkBuddy builder only writes a local ZIP. Importing that ZIP through the
WorkBuddy desktop UI, or submitting it to the Open Platform for publication,
is a separate, explicit user action governed by WorkBuddy's privacy terms. The
builder excludes local indexes, embedding/model caches, environments,
credentials, and symlinked knowledge inputs. Its compact corpus contains the
project's published attributable School and source-evidence text, registry,
aliases, and schemas.

## What is stored locally

The installer writes to the selected self-contained target, standard
user-level caches, and short-lived operating-system temporary directories:

| Location | Content | Removable by |
|---|---|---|
| `<target>/` | Copied skill source and generated platform-specific `SKILL.md` | `python3 install.py --uninstall --platform <name>` |
| `<target>/.venv/` | Python virtual environment | Removed with the standard platform uninstall above |
| `<target>/knowledge_base/_index/` | Local ChromaDB vector index | Removed with the standard platform uninstall above |
| pip's configured user cache | Downloaded Python package archives/wheels, when pip caching is enabled | `python3 -m pip cache purge` (or `py -3 -m pip cache purge` on Windows) |
| `~/.cache/huggingface/hub/` (or configured Hugging Face cache) | nomic embedding model cache | `python3 install.py --uninstall --platform <name> --purge --yes-i-know` |
| Playwright browser cache *(per OS)* — `~/.cache/ms-playwright` on Linux, `~/Library/Caches/ms-playwright` on macOS, `%LOCALAPPDATA%\ms-playwright` on Windows | Chromium browser binary | The purge command above |
| `<target>/knowledge_base/` transaction directories | Staged card/index trees and rollback evidence used only during an atomic rebuild | A later build automatically verifies and rolls back an uncommitted generation or finishes cleanup for a committed one; ambiguous or altered evidence is preserved for manual review |
| POSIX: `/tmp/openmobius-build-locks-<uid>/`; Windows: `%LOCALAPPDATA%\OpenMobius\build-locks\` (home fallback only if the Known Folder API is unavailable) | Stable one-byte, path-hashed lock files used to coordinate retrieval, indexing, install/update/uninstall, and package export without storing query or knowledge content | May be deleted when no OpenMobius operation is running; otherwise left in place for safe inode-based locking |
| Other OS temporary paths | Staged installer/exporter files | Cleaned up automatically after successful operations; interrupted installer state is verified and recovered on the next matching operation, while ambiguous state is preserved for manual review |
| Builder-selected ZIP path | WorkBuddy local-import/publication package, only when the builder is run | Delete the selected ZIP after import or submission when no longer needed |

Default local targets are lowercase copied directories, not registration
symlinks: `~/.claude/skills/openmobius-skill/`,
`~/.agents/skills/openmobius-skill/`,
`<OPENCLAW_STATE_DIR or ~/.openclaw>/skills/openmobius-skill/`,
`<HERMES_HOME or ~/.hermes>/skills/market-data/openmobius-skill/`, and
`~/.cursor/skills/openmobius-skill/`. OpenClaw and Hermes targets are currently
supported on Linux/macOS. An explicit `--target-dir` replaces the default.

On Windows, use the equivalent `py -3 install.py ...` commands. A standard
uninstall removes the complete selected target, including its virtual
environment and index. `--purge --yes-i-know` additionally removes shared
model/browser caches, which other tools may also use.

## What is NOT done

- **No telemetry.** No usage analytics, crash reports, or any
  background-collected data are transmitted from your machine.
- **No background processes.** No daemons, system services, or startup
  entries are installed.
- **No system-level modifications.** No `/etc/`, no
  `/Library/LaunchAgents/`, no registry keys.
- **No automatic updates.** Version upgrades require explicit
  `python3 install.py --update` (or `py -3 install.py --update` on Windows).

## How to inspect

Before installing you can review the install steps in `install.py`. The
`--strict` flag halts on the first error (useful for CI environments).

## Reporting

For privacy or security concerns, please open an issue on the project's
GitHub repository (when public) or contact the project maintainers via
the website listed in `README.md`.
