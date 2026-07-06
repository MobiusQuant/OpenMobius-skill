---
name: OpenMobius-skill
description: ICT/SMC/ChanLun(缠论) trading knowledge base (726 concepts + 1282 cases) + a built-in SMC structural indicator + chart generation via Mobius Quant API. Four interaction modes - (1) concept Q&A ("what is X / how to identify Y / how to enter Z"); (2) chart-image analysis when user attaches a trading chart ("看图 / 分析 / 走势 / 行情 / 帮我看") — auto-fetches real OHLCV + structural signals; (3) chart annotation ("draw / annotate / 标一下 / 画出来 / 标注"); (4) Kline analysis on pasted OHLCV or asset+timeframe ("BTC 1h 怎么样" / "茅台日线"). The SMC indicator (BOS/CHoCH, Order Blocks, Fair Value Gaps, equal H/L, premium-discount, strong/weak pivots) is the default structural source for any asset+timeframe query. Always fetches FRESH data per turn from Mobius Quant API (api.mobiusquant.ai); when asked "数据源 / where's the data from", use the canonical disclosure (do not fabricate upstream vendors). Supports crypto, A股/港股/美股, forex.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---
# OpenMobius-skill — ICT/SMC Trading Knowledge Skill

A unified skill for four interaction modes with a curated knowledge base (726 concept cards + 1282 case cards) distilled from 300+ ICT/SMC/ChanLun(缠论) trading videos and live lessons across 12 curated source collections.

**Core principle**: every claim must be grounded in (a) visible chart evidence OR (b) a retrieved knowledge-base rule. **No fabrication** — when uncertain, state so explicitly.

## Freshness mandate — NEVER answer market questions from memory

Any user message that mentions an asset + timeframe — **even without
the word "现在" / "now"** — REQUIRES a fresh `kb_klines.py indicators`
or `kb_klines.py chart` call **in the current turn**. Examples:

- "BTC 1h 怎么样" — yes, call API now
- "ETH 现在怎么样" — yes
- "茅台日线分析下" — yes
- "金子 4 小时" — yes
- "BTC 还在跌吗" — yes, even though no timeframe given (default to user's
  implied tf or ask), the freshness rule still applies

**Hard rules**:

1. **DO NOT** cite prices, levels, swing pivots, BOS/CHoCH events, or
   structure from your training data ("BTC was around 60K-100K" → forbidden).
2. **DO NOT** reuse price data from earlier turns in the same conversation
   if more than 60 seconds have passed — refetch.
3. **DO NOT** invent timestamps, "data as of" labels, or "real-time"
   claims that are not literally in the API response's `freshness` block.
4. The only source of truth for market data is a `freshness` block
   returned by an API call made in this turn. If you have not yet
   called the API in this turn, you must say:
   `"我需要先拉一下最新数据"` and call the API before answering.

**Every market-analysis reply MUST include the freshness footer**
(see workflows/klines.md Step 5 / workflows/analyze.md Step 6 for
the exact format). A reply without the footer is incomplete.

If the API response's `freshness.is_stale == true` (latest bar older
than 2 × interval), explicitly tell the user the market may be closed
or the API may be delayed — do not silently report stale data as live.

---

## Data source disclosure (canonical answer)

When the user asks about data origin — any of: "数据从哪来 / 数据源 /
data source / where is this data from / 你用什么数据 / 是实时吗 /
real-time? / 怎么取的数据" — respond with the canonical disclosure
below. **Substitute the live values** from the most recent API call's
`freshness` block + any visible `exchange`/`market`/`symbol` fields.

### Canonical answer template (bilingual)

```
**Data source / 数据来源**: Mobius Quant API (api.mobiusquant.ai)

Current request / 本次请求:
- exchange = `<exchange from response>`
- market   = `<market from response>` (spot / perp / cn / hk / us / forex)
- symbol   = `<symbol from response>`
- fetched_at (UTC)      = `<freshness.fetched_at>`
- last_bar_open (UTC)   = `<freshness.last_bar_open_time_utc>`
- last_bar_age_seconds  = `<freshness.last_bar_age_seconds>` (is_stale=<is_stale>)

**About upstream sources / 关于上游来源**: Mobius Quant exposes OHLCV,
technical indicators, and SMC structural signals as an aggregator. Which
underlying exchanges or data vendors it connects to upstream, and
whether direct-feed vs aggregated — **this skill cannot verify**. See
https://www.mobiusquant.ai/ for details.
```

### Hard rules — what you must NOT say about the data source

- **DO NOT** name specific upstream vendors unless the exact string
  appears in the API response's `exchange` field. Allowed values are
  what `symbols_search` / `klines` / `indicators` literally return
  (e.g. `binance`, `bybit`, `okx`, `hyperliquid` for crypto; `cn`/`hk`/`us`
  for stocks).
- **DO NOT** name web data providers (新浪财经 / Yahoo Finance /
  TradingView / 东方财富 / 同花顺 / Bloomberg / etc.) — you cannot
  verify any of these.
- **DO NOT** describe the upstream pipeline ("Mobius pulls from Binance
  via WebSocket" / "tick-level feed" / "delayed 15 min") — you cannot
  verify any such claim.
- **DO NOT** make freshness claims beyond what `freshness.is_stale`
  reports. Use the literal `last_bar_age_seconds` number.

### What you CAN say

- The API endpoint (`api.mobiusquant.ai`)
- The exact JSON fields returned (`exchange` / `market` / `symbol` /
  `count` / `current_price` / `freshness.*`)
- That the SMC structural indicator is computed server-side by Mobius
- A pointer to `https://www.mobiusquant.ai/` for upstream details

---

## Always retrieve from the knowledge base first

The knowledge base contains rule-based identification criteria and documented pitfalls that generic training data lacks. **Always retrieve first, then synthesize** — don't answer trading questions from memory alone.

## Output format is mandatory

Every workflow ends in a synthesis step with `##` section headings (see
each `workflows/<name>.md`'s final step). **Those headings MUST appear
verbatim in your user-facing reply**, in the order specified, in the
user's language. Free-form prose that omits the headings is an
**incomplete reply** and must be revised before sending.

## Scenario Router

Pick the right sub-workflow based on the user's input. Each workflow has detailed steps in its own document:

| User input | Workflow | Document to read |
|---|---|---|
| Concept question, **no chart, no data, no asset name** ("什么是 FVG", "how to identify OB", "止损放哪里") | **Q&A** | `workflows/qna.md` |
| **Chart attached** + any question about it ("分析", "看一下", "走势", "where to enter", "what's happening") | **Analyze** (auto-fetches real OHLCV + annotation) | `workflows/analyze.md` |
| User explicitly asks to **draw/annotate** an image, OR follows up after analysis with "把这个标在图上" | **Annotate** | `workflows/annotate.md` |
| User pastes **OHLCV data** OR mentions **asset + timeframe by name** without chart ("BTC 1h 怎么样" / pastes CSV / "茅台日线") | **Kline analysis** (auto-generates a fresh chart PNG) | `workflows/klines.md` |

> **Chart output is part of the standard reply** for the **Analyze** and
> **Kline analysis** workflows — render a PNG and include its path in the
> output. Skip the chart step ONLY when the user explicitly opts out
> ("只要文字" / "skip chart" / "no image" / "不用画图").

**How to route**:

1. Read this SKILL.md to understand the routing
2. Identify which scenario the user is in
3. Use the `Read` tool to load the relevant workflow document (relative to this SKILL.md: `workflows/<name>.md`)
4. Follow that workflow's steps

> **Important — Analyze workflow now auto-fetches data**: If a chart is attached AND the asset/timeframe is identifiable from the chart, `analyze.md` will fetch real OHLCV from Mobius API to **complement visual analysis with precise prices**. This is on by default; user can opt out by saying "只看图不拉数据" / "skip data fetch".

> **Note**: The **Analyze** workflow already auto-generates an annotated image as its final step. You do NOT need to separately invoke Annotate after Analyze unless the user wants to re-render with different parameters (different colors, new bbox, JSON-only output, etc.).

## Two chart generation paths

When the user wants a visual chart, choose the right tool:

| Situation | Tool | Output |
|---|---|---|
| User uploaded their own chart image; wants markup ON that image | `scripts/kb_draw_annotation.py` (PIL) | Annotated copy of original image |
| No chart image, OR user wants a clean new chart | `scripts/kb_klines.py chart` + `render` | Fresh TradingView-grade chart: pure K-lines + knowledge-base overlays (FVG/OB rectangles, sweep lines, swing markers, trade-setup lines) |

For path #2, the typical pipeline is:

```bash
# 1. Pull pure K-lines (no indicators) → panels payload skeleton (items=[])
.venv/bin/python scripts/kb_klines.py chart \
    --query "BTC" --interval 1h --limit 200 \
    --output /tmp/chart.json

# 2. Run analyze (on fetch output) to get suggested_overlay_items
.venv/bin/python scripts/kb_klines.py fetch --query "BTC" --interval 1h --limit 200 --output /tmp/data.json
.venv/bin/python scripts/kb_klines.py analyze --input /tmp/data.json --format json --output /tmp/features.json

# 3. (LLM step) Merge selected suggested_overlay_items + your trade_setup hlines
#    into /tmp/chart.json's panels[0].items. Item types:
#      - rectangle:  FVG / Order Block / Killzone zones  (price_top + price_bottom + time_start [+ time_end])
#      - hline:      Entry / SL / Target / Swept levels
#      - markers:    Swing points, sweep candles
#    All items use style.role from this set (see "Style role reference" below).

# 4. Render PNG
.venv/bin/python scripts/kb_klines.py render \
    --input /tmp/chart.json --output /tmp/chart.png \
    --theme dark --width 1400 --height 900
```

## Indicator fetching

### Default: SMC structural indicator

For any market-analysis trigger (asset+timeframe query, pasted OHLCV, or
chart attached), fetch the **SMC structural indicator** first. It is the
default — and only — indicator that should be fetched automatically.

```bash
.venv/bin/python scripts/kb_klines.py indicators \
    --query "BTC" --interval 1h \
    --limit 200 --format compact
```

No `--inds` flag means SMC by default. The response covers, in one call:

- **Per-bar state**: swing/internal trend bias, active swing & internal
  pivots, trailing extremes (running max/min since last pivot), the SMC
  indicator's internal volatility baseline (`smc_atr200`)
- **`objects` sidecar**: structural events with full geometry, ready to
  drop straight into chart overlays
  - `swing_pivots` (HH/HL/LH/LL), `swing_structures` & `internal_structures`
    (BOS / CHoCH events with `pivot_time` + `confirm_time` + `bias`)
  - `equal_highs` / `equal_lows` (liquidity-pool levels)
  - `order_blocks_swing` / `order_blocks_internal` (each with
    `top`/`bottom`/`anchor_time`/`bias`/`status: active|mitigated`)
  - `fair_value_gaps` (same field shape as OBs)
  - `trailing_extremes`: `{top, top_label, bottom, bottom_label}` where
    the labels are one of `Strong High` / `Strong Low` / `Weak High` /
    `Weak Low`
  - `premium_zone` / `equilibrium_zone` / `discount_zone`
    (`{top, bottom}` price bands at the swing range's top/middle/bottom)
  - `alerts_last_bar`: dictionary of booleans flagging events that fired
    on the most recent candle (e.g. `swing_bullish_choch`, `equal_highs`,
    `bullish_fair_value_gap`)

### SMC field semantics (use these to structure your analysis)

Order of consultation for the 5-section output:

1. **Trend bias**: compare `smc_swing_trend` vs `smc_internal_trend`.
   Same sign = strong trend; opposite sign = potential reversal or range.
2. **Most recent structural event** (look at last entry of
   `swing_structures` / `internal_structures`): is it `kind: BOS`
   (trend continuation) or `kind: CHoCH` (trend reversal)? **CHoCH has
   higher priority** than BOS as a forward signal.
3. **Trailing extremes labels**: `Strong High` + `Weak Low` together =
   confirmed bearish structure (the high holds, the low is breakable);
   `Strong Low` + `Weak High` = confirmed bullish. A break of a `Strong`
   pivot is the structural confirmation of a reversal.
4. **Active Order Blocks**: filter `objects.order_blocks_*` by
   `status: active`. Bull OBs below price = support candidates. Bear OBs
   above price = resistance candidates. Closer to current price = more
   relevant.
5. **Active Fair Value Gaps** (same filter): three-bar imbalance regions
   that price tends to revisit / fill.
6. **Equal highs / equal lows**: stops-cluster liquidity that Smart
   Money tends to sweep before reversing.
7. **Premium / equilibrium / discount placement**: which zone is the
   current price in? Bull-favored entries are in `discount`; short-
   favored entries are in `premium`; `equilibrium` is wait-and-see.

### Caveats (always disclose in the 5-section output)

- Swing pivots are confirmed only `swing_size` bars after they form
  (typically ~50 bars); recent pivots may still adjust.
- Order Blocks are reverse-engineered from later price action; a freshly
  formed OB may be revised by subsequent bars.
- FVG thresholds fire more frequently in low-volatility regimes — treat
  low-vol FVG counts with caution.
- All events are structural signals, not entry triggers. They complement
  but do not replace risk management.

### Cross-referencing the ICT knowledge base

Each SMC field maps directly to a KB concept card. After identifying the
structural pattern, retrieve the corresponding card for rule citations:

| SMC field / event | KB concept |
|---|---|
| `swing_structures` with `kind: BOS` | `break_of_structure` |
| `swing_structures` with `kind: CHoCH` | `change_of_character` |
| `order_blocks_*` | `order_block` |
| `fair_value_gaps` | `fair_value_gap` |
| `equal_highs` / `equal_lows` | `equal_highs` / `equal_lows` |
| `premium_zone` / `discount_zone` / `equilibrium_zone` | `premium_and_discount`, `equilibrium` |
| `trailing_extremes` with Strong/Weak labels | `strong_and_weak_highs_and_lows`, `protected_high_low` |
| `smc_atr200`, `smc_volatility`, `high_vol_bar` | `displacement` |

### When the user explicitly names a specific indicator

If — and **only if** — the user's message contains a specific indicator
name (whatever the abbreviation), pass that name through as `--inds`:

```bash
.venv/bin/python scripts/kb_klines.py indicators \
    --query "BTC" --interval 1h \
    --inds "<exact-name-user-said>" \
    --format compact
```

For multi-param indicators use the compact form `name:p1:p2` (e.g. one
positional param after the name); the server interprets the rest.

**Strict rules**:

1. **Do not pre-emptively fetch any indicator the user did not name.**
   Do not "complement the SMC reading" with another indicator on your
   own initiative.
2. **Do not suggest specific indicator names to the user.** If the user
   did not ask for an indicator, do not mention any. The SMC indicator is
   sufficient as the structural ground truth.
3. **Text-only**: indicator output is reported in prose / tables; chart
   rendering stays structure-only (FVG/OB/Sweep overlays from the SMC
   `objects` sidecar). Do not draw oscillator-style sub-panels.

## Chart authoring (LLM responsibility is small)

`kb_klines.py chart` auto-fills `panels[0].items` with the SMC indicator's
structural overlay (BOS/CHoCH markers, trailing-extreme labels, active
Order Blocks, active Fair Value Gaps, equal H/L, premium/equilibrium/
discount bands, internal OBs, mitigated history). You do **not** author
rectangles, markers, or structural hlines.

**The only items the LLM ever writes** are trade-setup hlines (entry /
SL / target), passed at render time via `--trade-setup PATH`:

```json
{"items": [
  {"type": "hline", "value": 78500, "label": "Short 78500",
   "style": {"role": "entry_short", "width": 2}},
  {"type": "hline", "value": 80000, "label": "SL 80000",
   "style": {"role": "stop_loss", "dash": "dashed", "width": 2}},
  {"type": "hline", "value": 77000, "label": "T1 77000",
   "style": {"role": "target", "width": 2}}
]}
```

**Label rule**: ≤ 12 characters including the price. Put rationale
(`"entry at FVG mid"`, `"SL above 4h OB"`) in the prose reply, not in
the chart label.

**Trade-setup `style.role` values**: `entry_long`, `entry_short`,
`stop_loss`, `target`.

Skip the trade-setup file when you have no specific trade levels to draw
— the SMC structural overlay alone is a valid market chart.

## Shared Rules (apply to all three workflows)

1. **No fabrication** — every price level cited must be visible on the chart or computed from a retrieved rule applied to a visible price.
2. **Cite the knowledge base** — every confirmed pattern must reference a retrieved card. Format: `"Rule N of <concept>: '<rule text>' — visible at <evidence>"`.
3. **Language rules**:
   - Prose language matches user's input: Chinese question → Chinese prose; English → English prose
   - Technical terms stay in English regardless of prose language: FVG, Order Block, Breaker, CISD, OTE, Liquidity Sweep, Killzone, IFVG, MSS, BOS, CHoCH, Displacement, etc. Do NOT translate to "公允价值缺口" — keep "Fair Value Gap" or "FVG"
   - Numbers/prices/percentages: keep original form
4. **State uncertainty explicitly** — prefer `null` or "uncertain — <reason>" over speculation.
5. **Multiple retrievals are OK** — for complex charts or multi-concept questions, run `kb_retrieve.py` more than once with different keyword combinations.
6. **Probability tiers (5 levels, semantic only)** — use exactly these names; do NOT expose internal percentages to users:

   | Tier | 中文 | Meaning |
   |---|---|---|
   | `very_high` | 很高 | Dominant scenario; strong rule-based confirmation |
   | `high` | 较高 | Primary plausible scenario; most rules confirm |
   | `medium` | 中等 | Plausible but partial rule confirmation |
   | `low` | 较低 | Edge case; speculative |
   | `very_low` | 很低 | Tail risk; mentioned for completeness only |

7. **Non-trading content** — if the image or question is not about trading, say so and stop.

## Tools

All scripts live in `${SKILL_DIR}/scripts/` and run via `${SKILL_DIR}/.venv/bin/python`. `${SKILL_DIR}` is the directory containing this SKILL.md (typically `~/.claude/skills/OpenMobius-skill/` after install, or the repo root after `git clone`). When invoking shell commands, **always `cd "${SKILL_DIR}"` first** so relative paths resolve correctly.

| Tool | Purpose |
|---|---|
| `scripts/kb_retrieve.py "<query>" --top-k 5` | Vector retrieval from knowledge base |
| `scripts/kb_klines.py resolve "<name>"` | Natural name → canonical asset spec |
| `scripts/kb_klines.py fetch --query "<name>" --interval <tf> --with-htf` | Pull real OHLCV (+ HTF) from Mobius API |
| `scripts/kb_klines.py parse --input <file>` | Parse pasted CSV/JSON/Markdown → standard OHLCV |
| `scripts/kb_klines.py analyze --input <ohlcv.json>` | Extract features (swing/FVG/OB/sweep/displacement/structure). Add `--format json` to get structured features + `suggested_overlay_items` |
| `scripts/kb_klines.py chart --query <name> --interval <tf>` | Pull pure K-lines (no indicators) → panels payload (items empty, ready for LLM to fill with KB overlays) |
| `scripts/kb_klines.py render --input <panels.json> --output <png>` | Render panels JSON → PNG via Playwright + lightweight-charts (TradingView-grade chart) |
| `scripts/kb_klines.py indicators --query <name> --interval <tf>` | Default: fetch the SMC structural indicator (BOS/CHoCH, Order Blocks, FVGs, equal H/L, premium/discount zones, trailing pivot labels). Pass `--inds <exact-name>` only when the user explicitly named a specific indicator. Text output only, NOT rendered on chart. |
| `scripts/kb_draw_annotation.py --json <path>` | Render annotation JSON onto chart (PIL, for **user-uploaded** images) |
| `scripts/kb_phase_b_to_c.py --input <analysis.json> --image <png> --output <annotated.png>` | Convert analysis JSON → annotated image (one shot) |
| `scripts/build_index.py` | Build the vector index from `knowledge_base/{concepts,cases}/` (one-time) |
| `scripts/kb_doctor.py` | Environment health check (run if anything's broken) |

Common options for `scripts/kb_retrieve.py`:
- `--top-k N` (default 5)
- `--type concept|case` (filter by card type)
- `--school <NAME>` (e.g. `--school ICT`)
- `--format markdown|json|compact`

## Setup (one-time)

```bash
cd /path/to/OpenMobius-skill       # the skill directory
bash install.sh                  # creates .venv, installs deps, builds index, checks fonts
```

The installer auto-registers the skill with Claude Code. If anything breaks
(CJK label garbling, retrieval errors, missing index, etc.):

```bash
cd "${SKILL_DIR}" && .venv/bin/python scripts/kb_doctor.py
```

## Examples (quick reference; full examples in each workflow doc)

**Example 1 — Concept Q&A** (no chart, no asset name):
> User: "什么是 Fair Value Gap，怎么交易"
> → Read `workflows/qna.md` → run kb_retrieve → synthesize answer in Chinese with English technical terms

**Example 2 — Chart analysis** (with chart, identifiable asset):
> User: [attaches BTC 4H chart] "分析一下当前行情"
> → Read `workflows/analyze.md` → identify asset → auto-fetch real OHLCV from Mobius → 5-section reply with **precise prices** + auto-annotated image

**Example 3 — Annotation only** (follow-up):
> User: [after analysis JSON exists] "把刚才分析的画到图上"
> → Read `workflows/annotate.md` → call kb_phase_b_to_c.py → output annotated image

**Example 4 — Kline analysis** (no user-supplied chart):
> User: "BTC 1h 现在怎么样" (or pastes a CSV of OHLCV)
> → Read `workflows/klines.md` → fetch/parse → analyze (extract features) → retrieve → **generate fresh chart PNG** → 5-section reply citing precise data-grounded prices + chart path
