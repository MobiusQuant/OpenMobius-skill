# Workflow: Kline Data Analysis (no chart required)

For when the user provides **structured K-line data** (pasted) or just **asset name + timeframe**, without necessarily attaching a chart image.

Use the host-neutral placeholders from `SKILL.md`. Create one task-specific
`<TEMP_DIR>` for intermediate inputs/results and choose `<USER_OUTPUT_DIR>` for
the chart returned to the user. Every `kb_retrieve.py` command is shorthand
that must be expanded to `<PYTHON> scripts/kb_retrieve.py` before execution.

## When this workflow applies

- User pastes OHLCV data (CSV / JSON / Markdown table / TradingView export)
- User mentions an asset + timeframe by name without a chart: "BTC 1h 怎么样" / "茅台日线分析下" / "ETH 5m 现在能做空吗"
- User explicitly asks for "real data" / "实时数据" / "拉一下数据"

## When NOT to use

- User attached a chart and asked for analysis → use `analyze.md` (which itself auto-fetches data as Step 1e)
- Pure concept question with no asset / no data → use `qna.md`
- User wants annotation → use `annotate.md`

## The data sources

This workflow accepts **two data sources**:

| Source | When |
|---|---|
| **A. Mobius API fetch** | User gave asset name (e.g. "BTC 1h") — call API to get real OHLCV |
| **B. User-pasted data** | User pasted CSV/JSON/table — parse it locally |

Both result in standard OHLCV → feed to feature extractor → 5-section output.

## Mandatory Workflow (route + 4 steps)

### Step 0: Resolve or inherit the analysis route

Use the route contract summarized in `SKILL.md`. Read
`workflows/analysis_profiles.md` only when the user explicitly names or
excludes a lens/School/source, requests augment/compare, or capability support
is uncertain. If a caller or previous workflow supplied a route, inherit its `mode`,
`primary_lens`, `secondary_lenses`, `schools`, and `sources`; set
`intent=klines` and recompute `capabilities` for data-driven market analysis.
An explicit lens, school, source, or mode in the current request overrides the
corresponding inherited selector. If no route or selector exists, use
`intent=klines`, `mode=strict`, `primary_lens=ict_smc`,
`secondary_lenses=[]`, `schools=[ICT, SMC]`, `sources=[]`, with
`capabilities` populated by the profile capability check.

- **strict** — use the exact School projection layer for School-scoped
  grounding, or the evidence layer when a source is selected. Only those
  scoped records and compatible analyzers may drive the result.
- **augment** — `primary_lens` alone controls bias, trade levels, and the
  primary chart. Secondary lenses/sources may only add labelled confirmation,
  challenges, or risk context and cannot override it.
- **compare** — unsupported for data-driven market analysis in Phase 1. Fail
  the capability gate before data acquisition or artifact generation.

Perform an analyzer-capability gate before producing market conclusions. The
current indicator and local extractor implement ICT/SMC-style structure. They
must not be presented as ChanLun, Wyckoff, Elliott Wave, or another unsupported
lens. In `strict`, fail closed with a capability-gap response. Any market
`compare` route also fails closed in Phase 1; do not produce a partial
comparison. `augment` may use the structural output only as labelled secondary
evidence when the profile permits it.
A stopped capability-gap response still names the requested
lens/schools/sources and failed capability. Because this gate precedes data
acquisition, do not fabricate freshness or chart artifacts for that response.

### Step 1: Acquire the requested data

For a supported current-market route that passed Step 0 — **even if you
answered the same asset recently in this conversation** — call the API fresh.
Never reuse older-than-current-turn prices. If the user explicitly supplied
OHLCV for analysis, preserve that snapshot and use Path B instead.

**Path A — fetch from API**:

```text
<PYTHON> scripts/kb_klines.py fetch --query "<asset natural name, e.g. '比特币' or 'BTC' or 'ETH' or '茅台'>" --interval <1m|5m|15m|30m|1h|4h|1d> --limit 200 --with-htf --output <TEMP_DIR>/<symbol>_<interval>.json
```

If the API is unreachable, fall back to Path B (ask user to paste OHLCV).

- `--query` accepts Chinese natural names ("比特币" "以太坊" "茅台") + English aliases ("BTC" "ETH" "TSLA")
- `--with-htf` auto-fetches one timeframe up for HTF bias (1h → 4h, 5m → 15m, etc.)
- API supports: crypto (binance/bybit/okx/hyperliquid), stocks (cn/hk/us), forex

If user gave a canonical symbol directly (e.g. "BTCUSDT spot"), use:
```text
<PYTHON> scripts/kb_klines.py fetch --exchange binance --market spot --symbol BTCUSDT --interval 1h ...
```

**Path B — parse user-pasted data**:

Use the `Write` tool to save the user's text verbatim to a temporary input file;
do not interpolate untrusted text into a shell command. Then run:

```text
<PYTHON> scripts/kb_klines.py parse --input <TEMP_DIR>/<symbol>_<interval>_user_input.txt --symbol <symbol> --interval <tf> --output <TEMP_DIR>/<symbol>_<interval>.json
```

The parser auto-detects: JSON array (binance-style), CSV with header, Markdown table, whitespace-separated table.

**If parser fails** (rare unusual format): convert the user's text into a JSON
object array `[{"time": ..., "open": ..., "high": ..., "low": ...,
"close": ..., "volume": ...}]`, save it through the host's file-writing tool,
and pass that file with `--input`. Do not interpolate user data into a pipe or
shell command.

### Step 1b: Verify freshness for Path A

Every API response (`fetch` / `indicators` / `chart`) now includes a
top-level `freshness` block:

```json
{
  "fetched_at":             "2026-05-23T15:21:14Z",
  "last_bar_open_time_utc": "2026-05-23T15:00:00Z",
  "last_bar_age_seconds":   1274,
  "interval_seconds":       3600,
  "is_stale":               false,
  "data_source":            "Mobius Quant API (api.mobiusquant.ai)"
}
```

You MUST:

1. **Read and remember** the `freshness` values for the current asset.
2. **Quote them verbatim** in the Step 5 output footer (do not paraphrase
   timestamps, do not invent "as of right now" wording).
3. **If `is_stale == true`** (latest bar is older than 2 × interval),
   tell the user explicitly: "数据可能滞后 / data may be stale — market
   could be closed or API delayed."
4. **Never** answer using prices, structures, or pivots from your training
   data or from older turns in the conversation. Step 2 (SMC indicator
   fetch) re-grounds you in fresh data; do not narrate ahead of it.

Path B has no API `freshness` block. Preserve `time_range_ms`, `current_price`,
and `count` from the parser output, label the data as user-provided, and state
that recency and upstream provenance were not independently verified.

### Step 2: Obtain ICT/SMC structural features

For Path A, run the server-side analyzer only when permitted by the resolved
route and the capability gate above. Its output is authoritative structural
data for ICT/SMC routes, not a universal implementation of every school.

```text
<PYTHON> scripts/kb_klines.py indicators --query "<asset>" --interval <tf> --limit 200 --format compact --output <TEMP_DIR>/<symbol>_smc.txt
```

This is the structural source of truth for the analysis. Output covers:
- **Per-bar state** (last row of `data`): swing/internal trend bias,
  active swing & internal pivots, trailing extremes
- **`objects` sidecar**: swing pivots (HH/HL/LH/LL), BOS/CHoCH events
  (`swing_structures` / `internal_structures`), Order Blocks (with
  `status: active|mitigated`), Fair Value Gaps (same status field),
  equal highs/lows, premium/equilibrium/discount zones, trailing
  extremes with `Strong High` / `Weak Low` / etc. labels
- **`alerts_last_bar`**: boolean dict of structural events that fired
  on the latest candle

Read the output via `Read` — it's compact text, ~30-80 lines. Use the
field-semantics map in `SKILL.body.md` to consume each section.

### Step 2B: Local feature extraction

For user-pasted Path B data, or if the SMC API is unreachable (network error /
5xx), run:

```text
<PYTHON> scripts/kb_klines.py analyze --input <TEMP_DIR>/<symbol>_<interval>.json --output <TEMP_DIR>/<symbol>_features.txt
```

This computes BOS/CHoCH / Order Blocks / Fair Value Gaps / Liquidity Sweeps /
Displacement locally from OHLCV. State explicitly that the **structure** came
from local extraction; separately identify whether the OHLCV came from Mobius
or the user.

If HTF was fetched, the local analyze output includes both **primary**
and **HTF** sections.

### Step 3: Retrieve knowledge base concepts

Based on what the feature extractor surfaced, retrieve relevant ICT/SMC concepts:

```text
kb_retrieve.py "<keywords>" --layer school --schools ICT SMC --top-k 5
```

Replace `ICT SMC` with the resolved route's exact canonical School tags and
quote tags containing spaces. The default schools must always be passed
explicitly.
If `route.sources` is non-empty, switch to `--layer evidence --sources ...`
and keep the route's School filter when present. Retrieve only when that exact
intersection is available in `route.capabilities`; do not post-filter a
broader top-K.

Pick keywords from the surfaced features:
- If sweeps detected → "liquidity sweep reversal CISD"
- If displacement detected → "displacement order block continuation"
- If FVG untested → "fair value gap mitigation entry"
- HTF/LTF align question → "HTF LTF alignment bias"

Multiple retrievals encouraged when several patterns surface.

For `augment`, retrieve primary and secondary schools separately and keep
their provenance distinct. A `compare` market route must already have stopped
at Step 0 in Phase 1.

### Step 4: Generate a fresh chart image (default ON)

For API-backed Path A, **this step is REQUIRED** for asset + timeframe queries
(e.g. "BTC 1h 怎么样", "ETH 4h 现在如何", "茅台日线分析"). A Path A answer
without a chart is **incomplete** — users expect to see the K-lines plus the
FVG/OB/sweep overlays that the analysis cites.

Skip this step ONLY if the user explicitly opts out
("只要文字" / "skip chart" / "不用画图" / "no image").

For user-pasted Path B, do not call `chart` merely to produce an image: that
command fetches a live series that may differ from the supplied snapshot.
Render only when a panels payload has been constructed from the supplied
candles; otherwise state `Chart: not generated for user-pasted snapshot` in the
Path B footer. Never label a live chart as if it depicted the pasted data.

Build the chart from K-lines plus structural overlays. Do not render oscillator
sub-panels; the structural overlay is derived from the SMC `objects` sidecar
for an ICT/SMC-capable route.

**Generate the chart in one command**: `kb_klines.py chart` now auto-fills
the structural overlay from the SMC indicator (BOS/CHoCH markers,
trailing-extreme labels, active Order Blocks, active Fair Value Gaps, equal
H/L, internal OBs, and mitigated OBs/FVGs as muted history) — no manual JSON
authoring needed. Premium/equilibrium/discount bands are optional and require
`--include-zones`.

This auto-overlay is valid only for an ICT/SMC-capable route. In `augment`,
label it as secondary if ICT/SMC is not primary. A `compare` market route must
stop before artifact generation in Phase 1. Never use the auto-overlay to fill
an unsupported lens.

```text
<PYTHON> scripts/kb_klines.py chart --query "<asset>" --interval <tf> --limit 200 --output <TEMP_DIR>/<sym>_chart.json
```

**Trade-setup**: if you have entry / SL / target prices to draw, write a
small JSON file and pass it at render time. **You only ever author hline
items** — the structural geometry is handled for you.

Using the host's file-writing tool or a JSON serializer, save this object to
`<TEMP_DIR>/<sym>_setup.json`:

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

Available `style.role` values for trade-setup hlines: `entry_long`,
`entry_short`, `stop_loss`, `target`.

**Label rule**: ≤ 12 chars including the price (e.g. `"Short 78500"`,
`"SL 80000"`, `"T1 77000"`). Put rationale ("Entry at FVG mid", "SL
above 4h OB") in the prose reply, not in the chart label.

**Skip the trade-setup file** when you have no specific trade levels to
draw; the chart still renders with the SMC structural overlay alone.

**Fallback behavior**: if the SMC API is unreachable, the chart command
returns with empty items and logs a warning. You can either accept a
plain K-line chart, OR (if you ran a local `analyze` in Step 2) drop in
its `suggested_overlay_items` via the trade-setup JSON.

**Auto-overlay knobs** (defaults shown):
- `--max-items 400` — safety cap for auto-filled overlay items
- `--no-auto-overlay` — disable SMC fetch (items will be empty)
- `--no-include-mitigated` / `--no-include-internal` — turn off the matching
  item category
- `--include-zones` — opt in to premium/equilibrium/discount bands (default
  OFF)

Then render to PNG (pass `--trade-setup` only if you authored a setup file):

```text
<PYTHON> scripts/kb_klines.py render --input <TEMP_DIR>/<sym>_chart.json --trade-setup <TEMP_DIR>/<sym>_setup.json --output <USER_OUTPUT_DIR>/<sym>_chart.png --theme dark --width 1400 --height 900
```

Output is a TradingView-grade PNG ready to include in your reply.

### Step 5: Synthesize + output

**Output format is MANDATORY.** The reply MUST use the four section
headings below **verbatim**, in this exact order, in the user's language:

- `## 结论 / Conclusion`
- `## 分析逻辑 / Analysis`
- `## 后续走势与操作 / Outcome Cases`
- `## 风险与失效 / Risks & Invalidation`

A fifth section `## 信息缺失 / Missing Information` is optional and only
added when confidence ≤ medium.

Free-form prose without these `##` headings is an **incomplete reply** and
must be rejected before sending to the user.

The template below is the only acceptable structure (this workflow has no
image input, so the auto-annotation step from `analyze.md` is omitted):

```markdown
## 结论 / Conclusion
- **分析视角 / Lens**: <route.primary_lens + secondary_lenses> (<route.mode>)
- **Bias**: <long-leaning / short-leaning / neutral / uncertain>
- **Confidence**: <very_high / high / medium / low / very_low>
- **操作建议 / Action**: <one-line concrete recommendation with PRECISE prices from data>
- **关键依据 / Key evidence (≤3)**: <bullet list of 2-3 most decisive signals>

## 分析逻辑 / Analysis

1. **数据观察 / What's in the data**: cite specific price levels from feature summary
2. **HTF/LTF 对齐 / HTF-LTF alignment** (if HTF available): bias confirmation
3. **知识库匹配 / Knowledge base hits**: which retrieved cards apply
4. **规则推导 / Rule application**: cite specific rule + EXACT data evidence
5. **驳回的可能性 / Rejected hypotheses**

## 后续走势与操作 / Outcome Cases

2-3 scenarios using 5-tier probability. Each case has:
- **Case <letter> (<probability>)**: <scenario>
- **触发信号 / Trigger signals**: <observable signals>
- **操作建议 / Action**: <concrete entry/stop/target with PRECISE prices>
- **失效条件 / Invalidation**: <what kills this case>

## 风险与失效 / Risks & Invalidation
- **主要风险 / Main risks** (from cards' common_mistakes)
- **整体失效 / Overall invalidation**: <precise price level>
- **监控提示 / Monitoring hints**

## 信息缺失 / Missing Information (optional, only if confidence ≤ medium)
```

After the 5 sections, append the footer matching the data path.

For **Path A**, append the mandatory freshness footer. Values come directly
from the API response's `freshness` block; do not fabricate them:

```
📅 数据时点 / Data as of (UTC): <freshness.last_bar_open_time_utc>
🕐 当前价 / Current price:     <current_price>
📡 数据源 / Source:            Mobius Quant API → <exchange>:<market>:<symbol> @ <interval>, <count> candles
🔍 拉取时刻 / Fetched at (UTC): <freshness.fetched_at>
⏱️  K 线年龄 / Bar age:        <freshness.last_bar_age_seconds>s (is_stale=<freshness.is_stale>)
📂 结构摘要 / Structure:       <path to SMC output, or local features.txt if fallback>
🖼️ 行情图 / Chart:            <path to rendered PNG>   ← REQUIRED unless user opted out
📚 知识来源 / Knowledge sources: <schools/source labels + card IDs actually used>
```

If `freshness.is_stale == true`, also add a top-level warning line at
the start of the reply:

```
⚠️ 数据可能滞后 / Stale data warning: latest <interval> bar is
   <last_bar_age_seconds>s old (>2× interval). Market may be closed or
   API delayed; treat prices as last-known, not live.
```

For **Path B**, do not invent API freshness fields. Append this parsed-data
provenance footer using exact values from `parsed.json`:

```
📅 用户数据范围 / Supplied range (ms): <time_range_ms[0]> → <time_range_ms[1]>
🕐 最后收盘 / Last supplied close:      <current_price>
📡 数据源 / Source:                       User-provided OHLCV (not independently verified), <count> candles @ <interval>
⏱️  新鲜度 / Freshness:                    unavailable — no Mobius API freshness block
📂 结构摘要 / Structure:                <path to local features.txt>
🖼️ 行情图 / Chart:                     <path if rendered; otherwise state not generated>
📚 知识来源 / Knowledge sources:       <schools/source labels + card IDs actually used>
```

The applicable footer is non-negotiable. A reply missing API freshness for
Path A, or pretending Path B has API-verified freshness, is incomplete and must
be revised before sending.

## Key advantages over visual-only

In this workflow, prices are **exact** (from real data), not estimated. Reflect this in the output:

| Visual-only (analyze.md without API) | Data-grounded (this workflow) |
|---|---|
| "FVG around 73K-74.5K" | "Bullish FVG 73,182 - 74,210, 33% mitigated (touched 73,495 once)" |
| "swing high near 95K" | "Swing high at 95,847 (12 bars / 12h ago)" |
| "long lower wick" | "Sell-side sweep at 94,100 with 187 wick, closed 23 above sweep level" |
| "looks like an OB up there" | "Bearish OB at 81,092-81,329, next displacement 3.47× ATR, untested" |

Use these precise numbers in your reply — they're the core value-add.

## Constraints

1. **No fabrication** (shared rule) — every price you cite must appear in the feature summary
2. **Cite the knowledge base** — every confirmed pattern references a retrieved card
3. **Language** (shared rule) — Chinese prose / English technical terms
4. **State the data source explicitly** — tell the user whether you used API data or parsed data
5. **If parse failed** — tell the user the format issue and ask for one of: CSV with header / JSON / Markdown table
6. **API failure handling** — if fetch returns error, tell the user and ask if they want to paste data manually instead
7. **Route fidelity** — state the lens/mode and cited school/source for every
   conclusion; never backfill an unsupported school with SMC analyzer output

## Examples

### Example 1 — Pure asset name

User: "BTC 1h 现在怎么样"

Steps:
1. Resolve default strict `ict_smc` route with `schools=[ICT, SMC]`.
2. `<PYTHON> scripts/kb_klines.py fetch --query "BTC" --interval 1h --limit 200 --with-htf --output <TEMP_DIR>/btc.json`
3. `<PYTHON> scripts/kb_klines.py indicators --query "BTC" --interval 1h --limit 200 --format compact --output <TEMP_DIR>/btc_smc.txt` (default = SMC)
4. Read `<TEMP_DIR>/btc_smc.txt`
5. Retrieve relevant concepts (e.g. `kb_retrieve.py "liquidity sweep FVG market structure" --layer school --schools ICT SMC`)
6. Output 5-section reply with **exact** prices from the SMC objects sidecar,
   plus `Lens: ict_smc (strict)` and the knowledge school/source/card IDs used.

### Example 2 — Pasted CSV

User: "我有一段 ETH 5m 的数据：
```
time,open,high,low,close,volume
2026-05-17 10:00,2240,2245,2235,2241,1234
...
```
帮我分析一下"

Steps:
1. Save the pasted text with the host's file-writing tool to `<TEMP_DIR>/eth_paste.csv`
2. `<PYTHON> scripts/kb_klines.py parse --input <TEMP_DIR>/eth_paste.csv --symbol ETHUSDT --interval 5m --output <TEMP_DIR>/eth.json`
3. SMC indicator API works on a live symbol+timeframe, not on pasted data. Fall back to local extraction:
   `<PYTHON> scripts/kb_klines.py analyze --input <TEMP_DIR>/eth.json --output <TEMP_DIR>/eth_features.txt`
4. Read features + retrieve + 5-section output. State explicitly that the structure came from local extraction.
5. **NOTE**: user-pasted data has no HTF fetched (since asset may be from any source); state this and offer to fetch HTF from Mobius if applicable

### Example 3 — Stock

User: "茅台日线最近怎么样"

Steps:
1. `<PYTHON> scripts/kb_klines.py fetch --query "茅台" --interval 1d --limit 100 --with-htf` → may return 600519 (stock:cn)
2. `--with-htf` for daily → no HTF (1d is top), skip
3. Continue as Example 1

### Example 4 — Parse failed

User: "数据：[some malformed text]"

If parser returns "无法识别格式":
- You (the LLM) extract structure: "I see these are OHLC values for ETH. Let me convert..."
- Build a JSON object array yourself: `[{"time": ..., "open": ..., ...}]`
- Save it to `<TEMP_DIR>/normalized.json` with the host's file-writing tool,
  then run `<PYTHON> scripts/kb_klines.py parse --input <TEMP_DIR>/normalized.json --symbol ETH --interval 5m --output <TEMP_DIR>/eth.json`
- Continue as Example 2

## Tool reference

```text
# Resolve natural name only
<PYTHON> scripts/kb_klines.py resolve "比特币"

# Fetch with HTF
<PYTHON> scripts/kb_klines.py fetch --query "BTC" --interval 1h --with-htf --output <TEMP_DIR>/x.json

# Fetch with canonical symbol
<PYTHON> scripts/kb_klines.py fetch --exchange binance --market perp --symbol BTCUSDT --interval 1h ...

# Parse pasted text
<PYTHON> scripts/kb_klines.py parse --input <file> --symbol <s> --interval <tf> --output <json>

# Fetch SMC structural indicator (default — primary structural source)
<PYTHON> scripts/kb_klines.py indicators --query "<name>" --interval <tf> --format compact --output <smc.txt>

# Local fallback: extract features from OHLCV when API unreachable
<PYTHON> scripts/kb_klines.py analyze --input <json> --output <features.txt>
```

Resolve `<PYTHON>`, `<TEMP_DIR>`, and `<USER_OUTPUT_DIR>` as defined in the
loaded `SKILL.md`, and run commands from `<SKILL_ROOT>`. OpenClaw can resolve
that root from `{baseDir}` and Hermes from `${HERMES_SKILL_DIR}`; other hosts
must resolve the loaded file directly. Never execute any placeholder literally.
