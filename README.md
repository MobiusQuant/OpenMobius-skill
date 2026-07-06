<div align="center">

# OpenMobius-skill

**ICT/SMC trading-knowledge skill for AI coding agents**

A `SKILL.md`-standard skill bringing 2008 curated knowledge cards, real-time
market data, technical indicators, and chart generation to
**Claude Code**, **Codex**, **OpenClaw**, and **Hermes**.

<br>

[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](https://www.python.org/)
[![Website](https://img.shields.io/badge/🌐_website-mobiusquant.ai-FF7E29.svg)](https://www.mobiusquant.ai/)
[![Platforms](https://img.shields.io/badge/platforms-Claude_Code_·_Codex_·_OpenClaw_·_Hermes-9333ea.svg)](#platform-support)
[![Knowledge Base](https://img.shields.io/badge/knowledge_base-2008_cards-22c55e.svg)](#features)

[English](./README.md) · [中文](./README.zh.md) · 📜 [Changelog](./CHANGELOG.md)

</div>

---

> **🤖 AI agents** — when a user asks you to install this skill,
> follow [`README_AGENT.md`](./README_AGENT.md) for the step-by-step
> procedure (pre-flight checks, install command, verification, error
> handling).

---

## Contributors

Thanks to the people who help improve OpenMobius-skill:

<a href="https://github.com/xwzbupt"><img src="https://github.com/xwzbupt.png" width="64" height="64" alt="@xwzbupt"/></a>

- [@xwzbupt](https://github.com/xwzbupt)

---

## Overview

<div align="center">
  <img src="./docs/assets/demo.gif" alt="OpenMobius-skill demo" width="780">
  <br>
  <sub>Works on <b>Claude Code</b>, <b>Codex</b>, <b>OpenClaw</b>, and <b>Hermes</b>.</sub>
</div>

<br>

Drop this skill into your AI coding agent and ask trading questions in plain
language. The skill grounds every answer in a curated knowledge base + real
market data — no hallucinated price levels, no generic "looks bearish"
hand-waving.

| You ask | The skill does |
|---|---|
| *"What is Fair Value Gap, how to trade it?"* | Vector-retrieves FVG concept card + related (CISD / OTE / Premium-Discount) — answers with cited rules from the knowledge base |
| *Attach a BTCUSDT 1h chart + "analyze this"* | Identifies asset → fetches real OHLCV → extracts FVG / OB / sweep / displacement → outputs 5-section reply with **exact prices** + auto-annotated PNG |
| *"How is BTC 1h looking?"* (no chart) | Live data fetch + built-in SMC structural indicator (BOS/CHoCH, Order Blocks, FVGs, equal H/L, premium-discount zones, strong/weak pivots) → KB-grounded analysis |
| *"What's <indicator> on BTC?"* (user names a specific indicator) | Pass-through to the indicator API — no auto-fetch of indicators the user did not name |
| *Paste a CSV of OHLCV* | Parses → local structural extraction (fallback when API unavailable) → KB cross-reference → 5-section reply |
| *"Generate a chart with my entry/SL/target"* | Rendered chart via Playwright + lightweight-charts |

---

## Quick start

```bash
git clone https://github.com/MobiusQuant/OpenMobius-skill.git /tmp/openmobius-src
cd /tmp/openmobius-src
python install.py --platform claude-code      # or codex / openclaw / hermes / all

rm -rf /tmp/openmobius-src                     # ✓ clone is ephemeral; safe to delete
```

The installer copies source files into `~/.claude/skills/OpenMobius-skill/`
(or your chosen platform's skills dir), then in that directory:

1. Creates `.venv/` and installs dependencies
2. Downloads Playwright chromium (~280 MB, into your OS's user-global cache)
3. Downloads `nomic-embed-text-v1.5` model (~274 MB, into your HuggingFace cache)
4. Loads precomputed embeddings → builds vector index (~2 s)
5. Generates the platform-specific `SKILL.md`
6. Runs a health check

Each platform install is **self-contained**: it owns its own `.venv` and
`_index`. The clone is just a one-shot source bundle.

**First run**: ~5–10 min · **Subsequent runs** (`python install.py --update`): <1 min

After install, in your AI agent just ask:

```
"What is Liquidity Sweep"
[attach chart] "analyze this setup"
"How is ETH 4h looking, give me a chart"
"BTC 1h structure check"
```

> **Prerequisites**: Python 3.10+. See [INSTALL.md](./INSTALL.md) for details.

---

## Platform support

```bash
python install.py --platform <name>
```

<div align="center">

| Platform | Flag | Default install path |
|:---|:---|:---|
| **Claude Code** | `--platform claude-code` *(default)* | `~/.claude/skills/OpenMobius-skill/` |
| **Codex** | `--platform codex` | `~/.codex/skills/OpenMobius-skill/` |
| **OpenClaw** | `--platform openclaw` | `~/.openclaw/skills/OpenMobius-skill/` |
| **Hermes** | `--platform hermes` | `~/.hermes/skills/market-data/OpenMobius-skill/` |
| Auto-detect | `--platform auto` | scans `~/.<agent>` dirs |
| All four | `--platform all` | loops through all |

</div>

Each platform install is fully **self-contained** (its own `.venv`, its own
`_index`). The nomic model and Playwright chromium live in your OS's
user-global cache, shared across platforms — so installing on N platforms
doesn't N× the download.

---

## Features

### Knowledge base — 726 concepts + 1282 cases

Distilled from 300+ teaching videos and live lessons across 12 curated
source collections, cross-merged with model-audited term fusion and
content-level deduplication. Coverage spans 13 schools — ICT, SMC, Price
Action, Indicator-Based, ChanLun (缠论), Risk Management, Order Flow and
more. Each concept card carries: identification rules, trading
implications, common mistakes, related concepts, per-source definitions.
Each case card carries: market context, key observation, analysis steps,
lessons, and source time-range provenance. Retrieved via local ChromaDB +
multilingual `nomic-embed-text-v1.5` — no API key needed for retrieval.

### Real-time data + 60+ indicators

Crypto (Binance, Bybit, OKX, Hyperliquid), China A-shares, Hong Kong stocks,
US stocks, forex. Each indicator carries built-in analysis dimensions
(`summary_focus`) that the agent reads to structure its answer rather than
dumping raw numbers.

### Two chart-generation paths

| Path | Method | Output |
|---|---|---|
| Annotate user's image | PIL | Annotated copy preserving the original chart |
| Generate fresh chart | lightweight-charts in headless chromium | New K-lines + FVG/OB rectangles + sweep lines + swing markers |

### Auto-invoked by description matching

The `SKILL.md` description field triggers on natural-language questions. The
skill routes to one of four workflows:
[Q&A](./workflows/qna.md) ·
[analyze](./workflows/analyze.md) ·
[annotate](./workflows/annotate.md) ·
[klines](./workflows/klines.md).

---

## Roadmap

**Knowledge base**

- **ICT/SMC coverage completion** — Rounds 1–2 distilled the ICT trunk plus
  SMC supplements from 300+ teaching videos; upcoming rounds complete ICT
  sub-schools (Inner Circle Mentorship, Silver Bullet, Power of 3 variants)
  and full SMC coverage.
- **Fundamental knowledge base** — interpretation methodologies for news,
  policy reads, economic releases (CPI / NFP / FOMC) and earnings seasons.
- **Multi-school expansion** — ChanLun (缠论) landed in v0.3.0; next up:
  Wyckoff (volume/price action), VSA, Volume Profile / Market Profile, and
  classic Price Action (Al Brooks style).

**Indicators & tools**

- **Expanded SMC indicator coverage** — the built-in SMC structural
  indicator covers BOS/CHoCH, Order Blocks, FVGs, equal H/L, premium-
  discount zones and strong/weak pivot labels today. Upcoming: Killzone
  windows, Stop Run / Inducement events, and per-event probability
  scoring as computable signals.

**Access surfaces**

- **Non-CLI entry points** — chat-bot integrations for users who don't run a
  coding agent, so the knowledge base is reachable without the CLI.

---

## Architecture

```
OpenMobius-skill/
├── SKILL.md                          # main entry (LLM reads this)
├── SKILL.body.md                     # shared body (platform-neutral)
├── platforms/                        # per-platform frontmatter
│   └── claude-code.yaml / codex.yaml / openclaw.yaml / hermes.yaml
├── workflows/                        # detailed sub-workflows
│   └── qna.md / analyze.md / annotate.md / klines.md
├── scripts/                          # CLI tools
│   ├── kb_retrieve.py                # local vector retrieval
│   ├── kb_klines.py                  # API client + feature extraction
│   ├── kb_draw_annotation.py         # PIL annotation
│   ├── kb_phase_b_to_c.py            # analysis JSON → annotated PNG
│   ├── build_index.py                # build vector index
│   ├── kb_doctor.py                  # env health check
│   ├── chart_render/                 # lightweight-charts + headless chromium
│   └── _lib/                         # embedder + retriever
├── knowledge_base/                   # 726 concepts + 1282 cases
├── install.py                        # cross-platform installer
└── README.md / INSTALL.md
```

---

## Update / Uninstall

```bash
# Update
python install.py --update
python install.py --update --rebuild-index    # also rebuild vector index

# Uninstall (soft — remove platform registration only)
python install.py --uninstall
python install.py --uninstall --platform all  # all platforms

# Uninstall fully (.venv + index too)
python install.py --uninstall --full

# Full purge (also delete shared chromium + nomic caches — these may be
# used by other projects on your machine, so confirm before running)
python install.py --uninstall --purge --yes-i-know
```

See [INSTALL.md](./INSTALL.md) for all flags.

---

## Troubleshooting

```bash
.venv/bin/python scripts/kb_doctor.py
```

Reports the state of: venv, deps, nomic model, vector index, CJK fonts,
skill registration, API connectivity.

Common issues:

| Symptom | Fix |
|---|---|
| Chinese labels render as boxes | Install `fonts-noto-cjk` (Linux); macOS/Windows usually bundled |
| API request fails | Check network; see `api.mobiusquant.ai/api/health` |
| Skill not auto-invoking in Claude Code | Check `~/.claude/skills/OpenMobius-skill` exists; restart agent |
| `chroma.sqlite3` not found | `.venv/bin/python scripts/build_index.py` |

---

## License

Apache 2.0 — see [LICENSE](./LICENSE).
Third-party components: see [ATTRIBUTION.md](./ATTRIBUTION.md).

## Contributing

Issues and PRs welcome at
<https://github.com/MobiusQuant/OpenMobius-skill/issues>.

<div align="center">
<sub>Built for AI coding agents · Apache 2.0</sub>
</div>
