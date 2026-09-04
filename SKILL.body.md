# OpenMobius-skill — Multi-School Trading Knowledge Skill

A unified skill for four interaction intents with a curated multi-school knowledge base (726 concept cards + 1282 case cards) distilled from 300+ trading videos and live lessons across 12 curated source collections.

**Core principle**: every trading-analysis claim must be grounded in (a)
visible chart evidence or (b) a retrieved knowledge-base rule. Capability
claims must be grounded in the installed inventory and declared profile
contract. **No fabrication** — when uncertain, state so explicitly.

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

This live-fetch mandate applies to current-market requests. If the user
explicitly asks to analyze OHLCV they supplied, preserve that snapshot instead
of replacing it with live data; use the parsed-data provenance footer from
`workflows/klines.md` and state that its freshness is not independently
verified.

**Control-plane exception**: a question about which analysis model/School/mode
the installed skill supports, or whether a named School *can* analyze a market,
is capability discovery rather than a request to analyze that market. Route it
to the Q&A capability-discovery branch before applying asset/timeframe rules;
do not fetch market data even when the question names an asset or timeframe.

**Capability-gate exception**: first resolve the requested market-analysis
route. If its required native analyzer/filter is unsupported, stop before any
network call or artifact generation, report the capability gap, and do not add
a fabricated freshness footer. The freshness requirements below apply only
after a market route passes that gate.

**Hard rules**:

1. **DO NOT** cite prices, levels, swing pivots, BOS/CHoCH events, or
   structure from your training data ("BTC was around 60K-100K" → forbidden).
2. **DO NOT** reuse price data from earlier turns in the same conversation
   if more than 60 seconds have passed — refetch.
3. **DO NOT** invent timestamps, "data as of" labels, or "real-time"
   claims that are not literally in the API response's `freshness` block or
   the user-supplied dataset.
4. For API-backed current-market analysis, the only source of truth is a
   `freshness` block returned by an API call made in this turn. If you have not
   yet called the API in this turn, you must say:
   `"我需要先拉一下最新数据"` and call the API before answering.

**Every market-analysis reply that proceeds past the capability gate MUST use
the matching footer**: API-backed analysis uses the freshness footer; analysis
of user-pasted OHLCV uses the parsed-data provenance footer (see
`workflows/klines.md` Step 5). A reply without the applicable footer is
incomplete.

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

## Host-neutral runtime and artifact paths

Resolve these placeholders for the current host before running a workflow.
They are documentation tokens, not literal paths or shell variables:

- `<SKILL_ROOT>` — the directory containing the loaded `SKILL.md`. Run all
  commands with this directory as the working directory so relative `scripts/`
  paths resolve without depending on the user's current directory.
- `<PYTHON>` — the Python executable selected for this installed skill. Resolve
  it from the platform/installer-managed runtime; Windows and POSIX executable
  paths differ, and a managed host may expose its own runner. Do not assume
  `python` or `python3` is available on `PATH`.
- `<TEMP_DIR>` — a writable, task-specific temporary directory created through
  the current host's temporary-directory facility. Do not assume a particular
  POSIX or Windows system path exists.
- `<USER_OUTPUT_DIR>` — a writable directory selected by the user or exposed by
  the host for durable artifacts that must be returned. Do not use a repository
  checkout or developer-machine path as the implicit output location.
- `<INPUT_IMAGE>` — the host-resolved path to the user's attached chart.

Never execute the angle-bracket placeholders literally. Quote each resolved
path according to the current command runner when it contains spaces. Command
blocks use logical argument lists and avoid shell-only pipes, heredocs, and
continuation syntax. Create JSON/text inputs with the host's file-writing tool
or a JSON serializer, then pass the resulting file path to the script.

For WorkBuddy packaging compatibility, a command line that begins with
`kb_retrieve.py` is launcher-neutral shorthand only. Before execution it
**must** be expanded to `<PYTHON> scripts/kb_retrieve.py`; never assume
`kb_retrieve.py` is on `PATH`.

## Always retrieve from the knowledge base first

The knowledge base contains rule-based identification criteria and documented pitfalls that generic training data lacks. Resolve the route and confirm its capabilities, then **retrieve within that route before synthesizing** — don't answer trading questions from memory alone and don't widen a selected school/source boundary silently.

The semantic-card retrieval mandate applies to trading-knowledge answers, not
to control-plane capability discovery. Capability discovery inspects the
installed School inventory and declared profile contract without running a
normal query/top-K search; follow the special case in `workflows/qna.md`.

## Analysis profile orchestration

First detect capability-discovery questions about the installed skill's
available models/profiles, lenses, Schools, modes, supported intents, or a
named School's market-analysis support. They remain `intent=qna`, but take
priority over the default `strict` ICT/SMC route and any asset/timeframe or
chart routing. Read both `workflows/qna.md` and
`workflows/analysis_profiles.md`, inspect the installed inventory with
`kb_retrieve.py --layer school --list-schools --format json` (expanded through
the launcher-neutral rule above),
then answer immediately from that inventory and the declared profile contract.
This is an intentionally bounded, single-agent control-plane operation: do not
delegate to a subagent/background task, recursively invoke this skill, run Git,
scan source code/cards/manifests, or verify analyzer implementations. Do not
construct or inherit an analytical School route for this branch, and stop after
the capability response.

For all normal analysis and knowledge requests, resolve a route before
retrieval, indicator calls, analysis, or drawing:

`route = {intent, mode, primary_lens, secondary_lenses, schools, sources, capabilities}`

- `intent` must be exactly one of `qna`, `analyze`, `annotate`, or `klines`.
  Do not emit aliases such as `kline_analysis`.
- `capabilities` must always be an object, never a list or string. Use the
  canonical fields and values defined in `workflows/analysis_profiles.md`.
- For the plain default route, set `exact_primary_school_filter=true`,
  `source_filter=not_requested`, `intent_supported=true`, and `reason=null`;
  set `native_market_analyzer=not_required` for Q&A or `supported` for a market
  intent. This default does not require loading the profile reference.
- `lens` (also called a profile) is an analytical methodology such as
  `ict_smc` or `chanlun`; `source` is a corpus/teacher collection such as
  `Teach-Wuyuan`. A source does not automatically select a lens.
- With **no explicit lens, school, source, or composition selector**, use
  `mode=strict`, `primary_lens=ict_smc`, and `schools=[ICT, SMC]`; retrieve
  with `--layer school --schools ICT SMC`.
- A single explicit selector is strict by default. In Phase 1, `compare` is
  supported for Q&A only; market-analysis, chart, and annotation comparisons
  fail closed before network or artifact work. `augment` gives one primary lens
  authority over bias and trade levels while secondary lenses only confirm,
  challenge, or add risk context.
- School-scoped grounding uses `school_knowledge_v2`, which omits
  cross-School fused rules that cannot be attributed. Any requested source
  uses `source_evidence_v2` with `--layer evidence --sources ...`; combine it
  with `--schools ...` for an exact intersection. Never use the fused
  canonical layer to claim strict School/source isolation.
- School/evidence queries use hard-filtered hybrid retrieval by default
  (BM25 + semantic RRF over independently embedded scoped documents). Keep
  `--search-mode auto` unless diagnosing retrieval; exact terms/aliases stay
  first and the hard School/source boundary is never widened.
- **Never silently fall back** from an explicit lens/source to `ict_smc` or to
  an unfiltered search. Check `capabilities` before doing work and report an
  unsupported or empty route plainly.
- ChanLun knowledge Q&A is supported, but this skill currently has no native
  ChanLun market-structure analyzer or overlay. Never present SMC indicator
  output as ChanLun analysis.

Read [workflows/analysis_profiles.md](workflows/analysis_profiles.md) whenever
the user names a lens/school/source, requests comparison or augmentation,
excludes a profile, or the selected capability is uncertain. Plain default
`ict_smc` requests can proceed directly to the intent workflow below.

## Market-analysis output format is mandatory

The Analyze and Kline workflows end in a synthesis step with mandatory `##`
section headings. Those headings must appear verbatim and in the specified
order. Q&A and Annotate use the output structures defined in their own workflow
documents. A capability-gap response that stops before analysis is also exempt
from the market-analysis template and freshness footer.

## Scenario Router

Pick the right sub-workflow based on the user's input. Each workflow has detailed steps in its own document:

| User input | Workflow | Document to read |
|---|---|---|
| Installed-skill capability question ("当前有哪些分析模型", "which Schools are available", "缠论能分析 BTC 1h 吗") — even with an asset/timeframe or chart reference | **Q&A capability discovery** | `workflows/qna.md` + `workflows/analysis_profiles.md` |
| Concept question, **no chart, no data, no asset name** ("什么是 FVG", "how to identify OB", "止损放哪里") | **Q&A** | `workflows/qna.md` |
| **Chart attached** + any question about it ("分析", "看一下", "走势", "where to enter", "what's happening") | **Analyze** (auto-fetches real OHLCV + annotation) | `workflows/analyze.md` |
| User explicitly asks to **draw/annotate** an image, OR follows up after analysis with "把这个标在图上" | **Annotate** | `workflows/annotate.md` |
| User pastes **OHLCV data** OR mentions **asset + timeframe by name** without chart ("BTC 1h 怎么样" / pastes CSV / "茅台日线") | **Kline analysis** (auto-generates a fresh chart PNG) | `workflows/klines.md` |

> **Chart output is part of the standard reply** for the **Analyze** and
> **Kline analysis** workflows — render a PNG and include its path in the
> output. Skip the chart step ONLY when the user explicitly opts out
> ("只要文字" / "skip chart" / "no image" / "不用画图"). For
> user-pasted OHLCV, follow the Path B exception in `workflows/klines.md` and
> never fetch a different live series merely to satisfy chart output.

**How to route**:

1. Detect capability discovery first; if matched, follow its Q&A control-plane
   special case and stop without applying an analytical route
2. Otherwise resolve the route above; load `analysis_profiles.md` when its
   trigger applies
3. Identify the user's intent in the scenario table
4. Use the `Read` tool to load the relevant workflow document (relative to this SKILL.md: `workflows/<name>.md`)
5. Follow that workflow while preserving the route's lens/source boundaries

> **Important — Analyze workflow now auto-fetches data**: If a chart is attached AND the asset/timeframe is identifiable from the chart, `analyze.md` will fetch real OHLCV from Mobius API to **complement visual analysis with precise prices**. This is on by default; user can opt out by saying "只看图不拉数据" / "skip data fetch".

> **Note**: The **Analyze** workflow already auto-generates an annotated image as its final step. You do NOT need to separately invoke Annotate after Analyze unless the user wants to re-render with different parameters (different colors, new bbox, JSON-only output, etc.).

## Two chart generation paths

When the user wants a visual chart, choose the right tool:

| Situation | Tool | Output |
|---|---|---|
| User uploaded their own chart image; wants markup ON that image | `scripts/kb_draw_annotation.py` (PIL) | Annotated copy of original image |
| No chart image, OR user wants a clean new chart | `scripts/kb_klines.py chart` + `render` | Fresh TradingView-grade chart: K-lines + structural overlays (FVG/OB rectangles, sweep lines, swing markers, trade-setup lines) |

For path #2, the typical pipeline is:

```text
# 1. Pull K-lines + auto-filled SMC structural overlay for an ict_smc route
<PYTHON> scripts/kb_klines.py chart --query "BTC" --interval 1h --limit 200 --output <TEMP_DIR>/chart.json

# 2. Optionally create a separate trade-setup JSON containing only entry/SL/
#    target hlines; do not duplicate the structural items already auto-filled.

# 3. Render PNG (add --trade-setup <TEMP_DIR>/setup.json only when one exists)
<PYTHON> scripts/kb_klines.py render --input <TEMP_DIR>/chart.json --output <USER_OUTPUT_DIR>/chart.png --theme dark --width 1400 --height 900
```

## Indicator fetching

### Default `ict_smc` profile: SMC structural indicator

For a market-analysis branch whose lens is `ict_smc`, fetch the **SMC
structural indicator** first. A request with no explicit selector creates this
default branch. Do not fetch or use SMC as structural evidence for a strict
non-`ict_smc` branch; in `augment`, keep its evidence within the labelled
secondary role assigned to that branch.

```text
<PYTHON> scripts/kb_klines.py indicators --query "BTC" --interval 1h --limit 200 --format compact
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

### Caveats (always disclose when an SMC branch is used)

- Swing pivots are confirmed only `swing_size` bars after they form
  (typically ~50 bars); recent pivots may still adjust.
- Order Blocks are reverse-engineered from later price action; a freshly
  formed OB may be revised by subsequent bars.
- FVG thresholds fire more frequently in low-volatility regimes — treat
  low-vol FVG counts with caution.
- All events are structural signals, not entry triggers. They complement
  but do not replace risk management.

### Cross-referencing the ICT/SMC knowledge base

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

```text
<PYTHON> scripts/kb_klines.py indicators --query "BTC" --interval 1h --inds "<exact-name-user-said>" --format compact
```

For multi-param indicators use the compact form `name:p1:p2` (e.g. one
positional param after the name); the server interprets the rest.

**Strict rules**:

1. **Do not pre-emptively fetch any indicator the user did not name.**
   Do not "complement the SMC reading" with another indicator on your
   own initiative.
2. **Do not suggest specific indicator names to the user.** If the user
   did not ask for an indicator, do not mention any. Within an `ict_smc`
   branch, the SMC indicator is sufficient as the structural ground truth;
   it is not a substitute for another lens's native analyzer.
3. **Text-only**: indicator output is reported in prose / tables; chart
   rendering stays structure-only (FVG/OB/Sweep overlays from the SMC
   `objects` sidecar). Do not draw oscillator-style sub-panels.

## Chart authoring (LLM responsibility is small)

For an `ict_smc` branch, `kb_klines.py chart` auto-fills
`panels[0].items` with the SMC indicator's structural overlay (BOS/CHoCH
markers, trailing-extreme labels, active
Order Blocks, active Fair Value Gaps, equal H/L, internal OBs, and mitigated
history). Premium/equilibrium/discount bands are optional and require
`--include-zones`. You do **not** author
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

## Shared Rules (apply to all workflows)

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

Use the host-neutral placeholders defined above. OpenClaw can resolve
`<SKILL_ROOT>` from `{baseDir}` and Hermes from `${HERMES_SKILL_DIR}`; on other
hosts resolve it from the loaded `SKILL.md`. Do not execute an undefined
`${SKILL_DIR}` variable or assume that a virtual environment is on `PATH`.

| Tool | Purpose |
|---|---|
| `scripts/kb_retrieve.py "<query>" --layer school --schools ICT SMC --top-k 5` | Default/School-scoped retrieval from attributable School projections |
| `scripts/kb_retrieve.py "<query>" --layer evidence --sources <SOURCE>` | Exact source-evidence retrieval; optionally combine with `--schools` and `--type` |
| `scripts/kb_klines.py resolve "<name>"` | Natural name → canonical asset spec |
| `scripts/kb_klines.py fetch --query "<name>" --interval <tf> --with-htf` | Pull real OHLCV (+ HTF) from Mobius API |
| `scripts/kb_klines.py parse --input <file>` | Parse pasted CSV/JSON/Markdown → standard OHLCV |
| `scripts/kb_klines.py analyze --input <ohlcv.json>` | Extract features (swing/FVG/OB/sweep/displacement/structure). Add `--format json` to get structured features + `suggested_overlay_items` |
| `scripts/kb_klines.py chart --query <name> --interval <tf>` | Pull K-lines and auto-fill the SMC structural overlay for an `ict_smc` route; use `--no-auto-overlay` for an empty overlay |
| `scripts/kb_klines.py render --input <panels.json> --output <png>` | Render panels JSON → PNG via Playwright + lightweight-charts (TradingView-grade chart) |
| `scripts/kb_klines.py indicators --query <name> --interval <tf>` | Default: fetch the SMC structural indicator (BOS/CHoCH, Order Blocks, FVGs, equal H/L, premium/discount zones, trailing pivot labels). Pass `--inds <exact-name>` only when the user explicitly named a specific indicator. Text output only, NOT rendered on chart. |
| `scripts/kb_draw_annotation.py --json <path>` | Render annotation JSON onto chart (PIL, for **user-uploaded** images) |
| `scripts/kb_phase_b_to_c.py --input <analysis.json> --image <png> --output <annotated.png>` | Convert analysis JSON → annotated image (one shot) |
| `scripts/build_knowledge_v2.py` | Audit/export deterministic School projections and exact-source evidence |
| `scripts/build_index.py` | Build canonical + independently embedded v2 collections; unchanged v2 documents reuse the local content cache |
| `scripts/kb_doctor.py` | Environment health check (run if anything's broken) |

Common options for `scripts/kb_retrieve.py`:
- `--top-k N` (default 5)
- `--type concept|case` (filter by card type)
- `--layer canonical|school|evidence` (`canonical` is compatibility-only for strict routing)
- `--schools <NAME...>` (multi-value OR filter; default route uses `--layer school --schools ICT SMC`)
- `--school <NAME>` (single-school compatibility form)
- `--sources <NAME...>` (exact OR filter; evidence layer only)
- `--exclude-schools <NAME...>` (hard exclusion)
- `--all-schools` (explicitly unscoped retrieval; never an automatic fallback)
- `--search-mode auto|hybrid|semantic|lexical` (`auto` uses hybrid for v2;
  lexical does not load the embedding model)
- `--max-per-canonical N` (v2 default 2; `0` disables diversity limiting)
- `--list-schools` / `--explain-scope` (no embedding model load)
- `--format markdown|json|compact`
