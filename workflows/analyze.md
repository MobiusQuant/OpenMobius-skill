# Workflow: Chart Analysis (with auto-annotation)

For when the user **attaches a trading chart image** and asks anything about it.

Use the host-neutral placeholders from `SKILL.md`: resolve the attachment as
`<INPUT_IMAGE>`, create a task-specific `<TEMP_DIR>`, choose a writable
`<USER_OUTPUT_DIR>` for returned artifacts, and derive a filesystem-safe
`<IMAGE_STEM>` from the input name. Every `kb_retrieve.py` command is shorthand
that must be expanded to `<PYTHON> scripts/kb_retrieve.py` before execution.

## When this workflow applies

- User attaches a chart image (TradingView / MT4 / Binance / 火币 / OKX / Bybit / any exchange UI screenshot)
- User asks any of: analysis / patterns / where to enter / setup / structure / bias / risk
- Chinese triggers: "分析" / "看图" / "看一下" / "帮我看" / "入场" / "标一下" / "形态" / "走势" / "做多" / "做空" / "止损放哪"

## When NOT to use

- Pure conceptual Q&A without a chart → use `qna.md`
- Non-trading images (memes, code screenshots) → not this skill
- User explicitly wants only annotation, no analysis → use `annotate.md`

## Mandatory Workflow (route + 7 steps)

### Step 0: Resolve or inherit the analysis route

Use the route contract summarized in `SKILL.md`. Read
`workflows/analysis_profiles.md` only when the user explicitly names or
excludes a lens/School/source, requests augment/compare, or capability support
is uncertain. If a caller or an earlier workflow supplied a route, inherit its `mode`,
`primary_lens`, `secondary_lenses`, `schools`, and `sources`; set
`intent=analyze` and recompute `capabilities` for chart analysis. An explicit
lens, school, source, or mode in the current request overrides the
corresponding inherited selector. If no route or selector exists, use the
exact default route: `intent=analyze`, `mode=strict`,
`primary_lens=ict_smc`, `secondary_lenses=[]`, `schools=[ICT, SMC]`,
`sources=[]`, with `capabilities` populated by the profile capability check.

The route mode controls the whole run:

- **strict** — use the exact School projection layer for School-scoped
  grounding, or the evidence layer when a source is selected. Only the
  resulting scoped records and supported analyzers may contribute to
  conclusions.
- **augment** — `primary_lens` alone controls bias, trade levels, and the
  primary overlay. Secondary lenses/sources may only provide labelled
  confirmation, challenges, or risk context and may not overwrite it.
- **compare** — unsupported for chart/market analysis in Phase 1. Fail the
  capability gate before network calls, JSON creation, or annotation.

Before analysis, verify that the available visual/data analyzer can produce
the concepts required by the route. The current structural indicator and local
extractor provide ICT/SMC-style BOS/CHoCH/FVG/OB/liquidity evidence; their
output is not proof of a different school such as ChanLun. If the requested
lens requires unsupported signals, or the route uses `mode=compare`, fail
closed: do not issue a directional bias, setup, or annotation under that lens.
State the missing capability; do not infer a partial comparison from another
school.

A capability-gap response must still print the requested lens/schools/sources
and failed capability; because the gate precedes network calls, do not invent a
freshness footer for such a stopped run.

### Step 1: Examine the chart

Output a brief inventory before analysis:

| Field | How to read | If unreadable |
|---|---|---|
| **Asset** | Top-left ticker label (BTCUSDT / NQ1! / EURUSD / AAPL) | mark `null` |
| **Timeframe** | Top toolbar (1H / 4H / 1D / 15m) | mark `null` |
| **Visible price range** | Y-axis high/low | mark `null` |
| **Time range** | X-axis start/end | mark `null` |
| **Current price** | Right-side highlighted label on last candle | mark `null` |
| **Existing annotations** | Lines, boxes, fib levels, text labels already drawn | list them |
| **Candle pattern features** | Long wicks, displacement, consolidation, gaps | objective description only |

**Critical**: prefer `null` over guessing. Wrong prices poison the entire analysis.

### Step 1a: Detect multi-panel images

Check whether the image contains multiple chart panels (e.g. 5m + 15m side-by-side, or 2x2 multi-timeframe).

1. **Count distinct panels** — separate chart boxes with their own x/y axes and ticker labels
2. **Identify each panel's role**:
   - Same asset, different timeframes → multi-timeframe analysis (HTF/LTF alignment)
   - Different assets → inter-market / correlation (e.g. SMT divergence)
   - Same asset, same timeframe, different overlays → comparative annotation view
3. **Read Step 1 fields for each panel separately**, label them `panel_left` / `panel_right` etc.
4. **Note panel relationships** explicitly ("panel_left is 5m, panel_right is 15m, same asset ETHUSDT — multi-timeframe view")

When using multi-panel:
- HTF panel → **bias** + PD Array zones
- LTF panel → **entry trigger** (CISD / MSS / FVG)
- Confirm alignment before suggesting entry; if HTF and LTF conflict, downgrade confidence and explain

### Step 1b: Assess chart resolution

| Quality | Behavior |
|---|---|
| **High** (single panel, ≥1200px, clear y-axis) | Read exact prices; can claim "FVG at 73,250 ~ 74,180" |
| **Medium** (single panel, blurry y-axis OR multi-panel high-res) | Round to nearest 10/100/major level; add "approximate" caveat |
| **Low** (small / multi-panel low-res / dense candles) | Use **relative descriptions** ("near prior swing high"); **downgrade overall confidence to `low`**; add to missing_information: "high-resolution single-panel chart" |

**Critical**: if resolution prevents precise reading, **DO NOT fabricate exact prices**. Use approximate or relative language.

### Step 1d: Identify asset (for real-data fetch)

Try to extract from the chart:

| Field | How to read |
|---|---|
| **Symbol / ticker** | Top-left label (e.g. "BINANCE:BTCUSDT.P", "TSLA", "600519") |
| **Exchange** | Often part of the prefix (BINANCE:, BYBIT:, OKX:, NASDAQ:) |
| **Market** | Spot vs perpetual is hinted by suffix (`.P` / `PERP`) |
| **Interval** | Top toolbar selector ("1h", "4H", "1D", "5m") |

Map to Mobius API canonical form:

| Chart hint | Mobius canonical |
|---|---|
| `BINANCE:BTCUSDT.P` | `binance / perp / BTCUSDT` |
| `BINANCE:ETHUSDT` (no `.P`) | `binance / spot / ETHUSDT` |
| `BYBIT:BTCUSDT.P` | `bybit / perp / BTCUSDT` |
| `SH:600519` / `贵州茅台` | `stock / cn / 600519` |
| `HKEX:00700` / `腾讯控股` | `stock / hk / 00700` |
| `NASDAQ:AAPL` | `stock / us / AAPL` |

If you can identify symbol but **not** the exchange/market explicitly, use:

```text
<PYTHON> scripts/kb_klines.py resolve "<natural name or ticker>"
```

If asset identification fails (resolution low, no visible ticker, unrecognized) → **skip Step 1e**, proceed with visual-only analysis, and add "high-resolution single-panel chart" / "clearly visible ticker label" to `missing_information`.

### Step 1e: Fetch real OHLCV (data-grounded analysis)

If Step 1d succeeded, **fetch real data**:

```text
<PYTHON> scripts/kb_klines.py fetch --exchange <ex> --market <mkt> --symbol <sym> --interval <tf> --limit 200 --with-htf --output <TEMP_DIR>/<IMAGE_STEM>.klines.json
```

### Step 1f: Fetch SMC structural indicator (default)

The SMC indicator gives a structural ground-truth reading to complement
the visual chart analysis:

Run this only when the resolved route permits ICT/SMC structural evidence.
In `augment` mode label it as secondary when ICT/SMC is not the primary lens;
in `strict` mode never use it as a substitute for an unsupported school.

```text
<PYTHON> scripts/kb_klines.py indicators --exchange <ex> --market <mkt> --symbol <sym> --interval <tf> --limit 200 --format compact --output <TEMP_DIR>/<IMAGE_STEM>.smc.txt
```

**Read the .smc.txt** with the `Read` tool. It contains exact prices for:
- Per-bar trend bias (swing & internal)
- Active swing / internal pivots, trailing extremes with Strong/Weak labels
- `objects` sidecar: Order Blocks (active/mitigated), Fair Value Gaps,
  equal highs/lows, premium/equilibrium/discount zones, BOS/CHoCH events
- `alerts_last_bar`: structural events that fired on the latest candle

Use the field-semantics map in `SKILL.body.md` to consume each section.

### Step 1g: Verify freshness

The .smc.txt header now includes (or the JSON response has) a `freshness`
block:

```
data_source:          Mobius Quant API (api.mobiusquant.ai)
fetched_at (UTC):     2026-05-23T15:21:14Z
last_bar_open (UTC):  2026-05-23T15:00:00Z
last_bar_age:         1274s (interval=3600s, is_stale=False)
current_price:        75609.9
```

**You MUST** carry these values verbatim into the Step 6 output footer.
**Never** answer using prices, structures, or pivots from training data
or older conversation turns — Step 1e/1f re-grounds you in fresh data.

If `is_stale=True`, prepend a warning line to the final reply.

**Fallback to local extraction** (only if SMC API unreachable):

```text
<PYTHON> scripts/kb_klines.py analyze --input <TEMP_DIR>/<IMAGE_STEM>.klines.json --output <TEMP_DIR>/<IMAGE_STEM>.features.txt
```

**Sanity check** — compare data to chart:

| Check | Action |
|---|---|
| API current_price vs chart's last close (if readable) | If within 2% → OK; if > 2% → chart is likely historical, warn user and ask for time hint |
| API timeframe range vs chart's visible range | If wildly different → image likely shows different period than fetched; flag in missing_information |
| Asset on chart matches API resolve | If user-provided symbol differs from chart label → tell user |

**Opt-out**: if the user said "只看图" / "skip data" / "no API" → skip Step 1e entirely; mark `data_source: "visual_only"` in JSON.

**Fetch failure** (network error, symbol not on Mobius, 429 rate limit max retries) → log the failure, proceed with visual-only, add `data_fetch_failed: <reason>` to `missing_information`.

### Step 1c: Calibrate coordinate system (required for auto-annotation)

For EACH panel:

**`chart_bbox`** (pixel coordinates of plotting area):
- `x`: left edge of price grid (right of toolbar — NOT image edge)
- `y`: top edge of plotting area (below title/menu bar)
- `width`: from left grid edge to right edge of price grid (exclude right-side price labels if outside grid)
- `height`: top to bottom of grid (exclude bottom time-scale and indicator panels)

**`y_axis_range`**:
- `top`: highest price label
- `bottom`: lowest price label

**`theme`**: `"dark"` or `"light"` (based on chart background)

**Why**: Step 7 (auto-annotation) uses these to convert price → pixel position. Wrong bbox → annotations drawn outside chart.

**Best practices**:
- Use image dimensions (e.g. `Read` tool returns size) to bound estimates
- Conservatively estimate (smaller bbox > overshoot)
- If unsure → set `chart_bbox: null` and `y_axis_range: null` in JSON; Step 7 will skip

### Step 2: Form preliminary hypotheses

Candidate patterns from this set:

- **Patterns**: FVG / Order Block (OB) / Breaker Block / Mitigation Block / Inversion FVG (IFVG)
- **Liquidity**: Liquidity Sweep / Buy-Side Liquidity / Sell-Side Liquidity / Inducement / Stop Run
- **Structure**: BOS / CHoCH / MSS / Higher High / Lower Low
- **Confirmation**: CISD / Displacement / Imbalance / Volume Imbalance
- **Zones**: Premium/Discount / OTE (0.62-0.79) / Mean Threshold / Equilibrium
- **Timing**: Killzone / London Session / NY Open / Asia Range / Power of 3 / Silver Bullet

If a pattern looks possible but you're unsure, **still retrieve** — let the cards decide.

### Step 3: Retrieve concepts from the knowledge base

```text
kb_retrieve.py "<keywords>" --layer school --schools ICT SMC --top-k 5
```

Replace `ICT SMC` with the route's exact canonical School tags and quote tags
containing spaces. Keep the default explicit.
If `route.sources` is non-empty, switch to `--layer evidence --sources ...`
and retain the route's School filter when present. Retrieve only when the
capability check confirms that exact intersection; never post-filter an
unscoped top-K and call it source-specific.

Examples:

```text
# Generic chart with long wicks
kb_retrieve.py "long lower wick liquidity sweep reversal" --layer school --schools ICT SMC --top-k 5

# FVG + entry
kb_retrieve.py "Fair Value Gap entry OTE CISD confirmation" --layer school --schools ICT SMC --top-k 5

# Similar historical case
kb_retrieve.py "BTC 4H liquidity sweep entry reversal" --layer school --schools ICT SMC --type case --top-k 3

# Strict single-school route
kb_retrieve.py "smart money concepts market structure" --layer school --schools ICT --top-k 5
```

**Multiple retrievals are encouraged** for complex charts.

For `augment`, retrieve the primary schools first and any permitted secondary
schools separately so provenance remains visible. A `compare` market route
must already have stopped at Step 0 in Phase 1.

### Step 4: Apply rules to chart + data (dual-source)

For each retrieved card:

1. Read its `identification_rules`
2. Match each rule against **two evidence sources** (when both available):
   - **Visual evidence** from the chart (candle features, structure events)
   - **Data evidence** from the `.smc.txt` produced by Step 1f, or the
     `.features.txt` produced by the local fallback (exact prices, mitigation
     %, sweep wicks, displacement strength)
3. **Confirm** the pattern only if at least one source clearly satisfies the rule. Strong confirmation = both sources agree.
4. **Reject** if both sources fail the rule.
5. Note `common_mistakes` to avoid.

Apply those checks within the route boundary. `strict` rejects any claim whose
support is outside the selected lens. `augment` labels primary versus secondary
support. Record the school, source label, and card ID beside every accepted or
rejected knowledge claim. A `compare` market route does not reach this step in
Phase 1.

**Data takes precedence on price levels**: when the user asks "where's the FVG",
quote the exact range from the SMC output or local feature output, NOT a visual
estimate.

**Visual takes precedence on subjective features**: chart annotations / drawn lines / user notes only exist in the image.

**Conflict handling**: if the SMC/local feature output says "no FVG at level X"
but the chart visually looks like there might be one, you likely misread the
chart. Trust data; mention the conflict in the Analysis section.

**Citation format**:
- Visual: `"Rule 2 of FVG: 'high of first candle below low of third' — visible at candles 12:00 / 16:00 / 20:00 forming gap"`
- Data-grounded: `"Bullish FVG confirmed at 73,182 - 74,210 (SMC/local feature output), 33% mitigated. Rule 2 satisfied: c0.high (73182) < c2.low (74210)."`

**Reject example**: `"Rejected Order Block hypothesis — SMC/local feature output shows no displacement > 1.5× ATR in next 3 candles; Rule 3 fails"`

### Step 5: Save structured JSON (silent, NOT shown to user)

Save to: `<USER_OUTPUT_DIR>/<IMAGE_STEM>.analysis.json`

Example: `<INPUT_IMAGE>` → `<USER_OUTPUT_DIR>/eth_5m.analysis.json`

Use the `Write` tool. **DO NOT paste the JSON content in your reply.** It's for downstream tools.

JSON schema:

```json
{
  "input_image": "<INPUT_IMAGE>",
  "route": {
    "intent": "analyze",
    "mode": "strict" | "augment",
    "primary_lens": "<resolved primary profile>",
    "secondary_lenses": ["<resolved secondary profile>"],
    "schools": ["<resolved school>"],
    "sources": ["<resolved source, if constrained>"],
    "capabilities": {
      "exact_primary_school_filter": true,
      "native_market_analyzer": "supported",
      "source_filter": "not_requested",
      "intent_supported": true,
      "reason": null
    }
  },
  "knowledge_sources": [
    {"school": "<school>", "source": "<source label>", "card_id": "<card id>"}
  ],
  "asset": "<ticker or null>",
  "timeframe": "<e.g. 4H or null>",
  "visible_price_range": [<low>, <high>] | null,
  "current_price": <number or null>,

  "data_source": "visual+api" | "visual_only" | "api_only",
  "klines_json_path": "<path to .klines.json if Step 1e succeeded>" | null,
  "features_path": "<path to .smc.txt from Step 1f, or .features.txt from the local fallback>" | null,
  "data_chart_consistency": "match" | "mismatch_warn" | "n/a",

  "chart_bbox": {"x": <int>, "y": <int>, "width": <int>, "height": <int>} | null,
  "y_axis_range": {"top": <number>, "bottom": <number>} | null,
  "theme": "dark" | "light",

  "trend": "bullish" | "bearish" | "consolidating" | "uncertain",
  "bias": "long" | "short" | "neutral" | "uncertain",
  "patterns": [
    {
      "type": "FVG",
      "range": [<low>, <high>],
      "label": "<short label>",
      "confidence": "very_high" | "high" | "medium" | "low" | "very_low",
      "source_card": "<card id from retrieval>",
      "source_school": "<school>",
      "source_label": "<source collection/card label>"
    }
  ],
  "trade_setup": {
    "entry": {"price": <number>, "label": "<rationale tag>"} | null,
    "stop_loss": {"price": <number>, "label": "<...>"} | null,
    "targets": [{"price": <number>, "label": "<T1: prior high>"}]
  },
  "outcome_cases": [
    {
      "case_id": "A",
      "probability": "very_high" | "high" | "medium" | "low" | "very_low",
      "scenario": "<one-line description>",
      "trigger_signals": ["<signal1>", "<signal2>"],
      "action": "<what to do>",
      "invalidation": "<what kills this case>"
    }
  ],
  "risks": ["<risk1>", "<risk2>"],
  "confidence": "very_high" | "high" | "medium" | "low" | "very_low",
  "missing_information": ["<what's unclear>"]
}
```

**JSON rules**:
- `null` for any field you cannot determine
- Every price must appear on the chart or derive from a retrieved rule
- If too uncertain for 2+ outcome_cases, leave `outcome_cases: []`
- `chart_bbox` / `y_axis_range` `null` → Step 7 skipped

### Step 7: Generate chart image — two options

You have **two paths** to produce a chart image. Pick based on user intent and image quality.

#### Option A: Annotate user's uploaded image (PIL)

Use when the user wants markup on **their own** chart (their TradingView screenshot, existing drawings, etc.).

```text
<PYTHON> scripts/kb_phase_b_to_c.py --input <USER_OUTPUT_DIR>/<IMAGE_STEM>.analysis.json --image <INPUT_IMAGE> --output <USER_OUTPUT_DIR>/<IMAGE_STEM>.annotated.png
```

Reads `chart_bbox` / `y_axis_range` / `theme` from JSON, maps `patterns` + `trade_setup` to annotations, renders on top of the original image.

**Skip Option A if**:
- `chart_bbox` is null (couldn't calibrate)
- Multi-panel image (current version supports single-panel annotation only)

#### Option B: Generate fresh TradingView-grade chart (lightweight-charts)

Use when:
- User asked "出张图" / "画张干净的图" / "重新画一张"
- User's image is low-resolution (Step 1b judged low)
- Step 1e fetched real data (Option B is more accurate since it uses the SAME data)

```text
# 1. Pull K-lines + auto-filled SMC structural overlay
<PYTHON> scripts/kb_klines.py chart --exchange <ex> --market <mkt> --symbol <sym> --interval <tf> --limit 200 --output <TEMP_DIR>/<IMAGE_STEM>.chart.json
```

`kb_klines.py chart` auto-fills the structural overlay from the SMC
indicator — no manual `panels[0].items` authoring needed. If you have a
trade-setup (entry / SL / target) to draw, write a small JSON file with
only those hlines:

The auto-overlay is an ICT/SMC analyzer output and remains subject to Step 0.
In `strict`, do not generate it for another lens. In `augment`, it may appear
only as a labelled secondary overlay when permitted. A `compare` market route
must stop before artifact generation in Phase 1.

Using the host's file-writing tool or a JSON serializer, save this object to
`<TEMP_DIR>/<IMAGE_STEM>.setup.json`:

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

Render (pass `--trade-setup` only if you authored a setup file):

```text
<PYTHON> scripts/kb_klines.py render --input <TEMP_DIR>/<IMAGE_STEM>.chart.json --trade-setup <TEMP_DIR>/<IMAGE_STEM>.setup.json --output <USER_OUTPUT_DIR>/<IMAGE_STEM>.chart.png --theme dark --width 1400 --height 900
```

See `workflows/klines.md` Step 4 for the auto-overlay knobs
(`--max-items`, `--no-include-mitigated`, etc.) and trade-setup label
rules.

#### Skip both if

- No actionable trade_setup AND no drawable patterns
- Image is non-trading

### Step 6: Output conversational reply (5 sections, user-facing)

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

Reply structure (verbatim) in the user's language:

```markdown
## 结论 / Conclusion
- **分析视角 / Lens**: <route.primary_lens + secondary_lenses> (<route.mode>)
- **Bias**: <long-leaning / short-leaning / neutral / uncertain>
- **Confidence**: <very_high / high / medium / low / very_low>
- **操作建议 / Action**: <one-line concrete recommendation, e.g. "等 2245 retest 后做空，SL 2270，目标 2210/2200">
- **关键依据 / Key evidence (≤3)**: <bullet list of 2-3 most decisive signals>

## 分析逻辑 / Analysis

Walk through the causal chain **observation → knowledge-base rule → conclusion**:

1. **图上观察 / What's on the chart**: candle features, structural events, key levels
2. **知识库匹配 / Knowledge base hits**: which retrieved cards apply
3. **规则推导 / Rule application**: cite specific rules; show how chart features satisfy or violate them
4. **驳回的可能性 / Rejected hypotheses**: patterns that looked plausible but failed rule checks (with reason)

## 后续走势与操作 / Outcome Cases

List 2-3 plausible scenarios using the 5 probability tiers (`very_high` / `high` / `medium` / `low` / `very_low`).

For each case:
- **Case <letter> (<probability tier>)**: <scenario description>
- **触发信号 / Trigger signals**: <observable signals confirming this case>
- **操作建议 / Action**: <concrete entry/stop/target OR "观望 / wait for X">
- **失效条件 / Invalidation**: <what kills this case>

If chart is too ambiguous for 2+ scenarios, replace with:

> 当前结构信息不足以给出多场景预测：
> - 缺失：<list of missing info>
> - 建议：等 <specific signal> 后再评估

**Do NOT force scenarios when evidence is thin.**

## 风险与失效 / Risks & Invalidation

- **主要风险 / Main risks** (2-4 from retrieved cards' `common_mistakes`)
- **整体 setup 失效条件 / Overall invalidation**: <what would invalidate the entire bias>
- **监控提示 / Monitoring hints**: <e.g. "wait for NY Killzone before entry">

## 信息缺失 / Missing Information (optional, only if confidence ≤ medium)

- <list of missing pieces that would raise confidence>
```

After the 5 sections, append the **mandatory freshness footer** — values
come directly from Step 1g's API response, do NOT fabricate them:

```
📅 数据时点 / Data as of (UTC): <freshness.last_bar_open_time_utc>
🕐 当前价 / Current price:     <current_price>
📡 数据源 / Source:            Mobius Quant API → <exchange>:<market>:<symbol> @ <interval>
🔍 拉取时刻 / Fetched at (UTC): <freshness.fetched_at>
⏱️  K 线年龄 / Bar age:        <freshness.last_bar_age_seconds>s (is_stale=<freshness.is_stale>)
📂 分析数据 / Analysis JSON:   <absolute path>
🖼️ 标注图 / Annotated chart:  <absolute path>  ← only if Step 7 succeeded
📚 知识来源 / Knowledge sources: <schools/source labels + card IDs actually used>
```

If `freshness.is_stale == true`, prepend a top-level warning line at the
very start of the reply:
`⚠️ 数据可能滞后 / Stale data warning: latest <interval> bar is <age>s old.`

If Step 1e/1f was skipped (visual-only mode), omit the freshness lines
and mark the reply with `(visual-only — no live data fetched)`.

If Step 7 was skipped, omit the annotated chart line and optionally add:
`(标注图未生成：<reason, e.g. chart_bbox 无法可靠标定 / multi-panel 暂不支持>)`

## Constraints (re-emphasized)

1. **No fabrication** (shared rule)
2. **No analytical adjectives without basis** — avoid "strong support", "obvious reversal", "clear breakout" unless backed by a retrieved rule + visible evidence
3. **State uncertainty explicitly** — prefer "uncertain — <reason>"
4. **Cite the knowledge base** — every confirmed pattern references a retrieved card
5. **Multiple retrievals OK** — complex charts may need 2-3 retrieves
6. **Language rules** (shared) — Chinese prose / English technical terms
7. **Non-trading image** — say "Not a trading chart" and stop
8. **Multi-panel** — read each panel separately, label them, note relationship
9. **Resolution-aware** — downgrade to approximate / relative when low res
10. **JSON is internal** — silently save; NEVER paste in conversational reply
11. **Don't force outcome_cases** — if too ambiguous, skip Cases and explain what's needed
12. **Route fidelity** — expose lens/source provenance and fail closed when the
    requested profile lacks a native analysis capability

## Examples

### Example 1 — ETH 5m clear bearish setup (Chinese user)

User: [attaches ETH 5m chart] "查看一下这个图片，给我分析一下当前的行情"

**Internal workflow**:
1. Step 0: default strict `ict_smc` route, `schools=[ICT, SMC]`
2. Step 1: ETHUSDT.P 5m, image 1316×708, range 2200-2280, current 2226.5
3. Step 1c: chart_bbox = {x:50, y:30, w:1186, h:510}, y_axis_range = {top:2280, bottom:2200}, theme="dark"
4. Step 2: hypotheses → Liquidity Sweep at 2245, Bearish Displacement, Breaker Block
5. Step 3: `kb_retrieve.py "liquidity sweep breakdown displacement breaker 5m short setup" --layer school --schools ICT SMC`
6. Step 4: confirm all three; reject V-shape rebound (multi-candle, not 1-3 candles)
7. Step 5: silently save JSON to `<USER_OUTPUT_DIR>/eth_5m.analysis.json`
8. Step 7: run `<PYTHON> scripts/kb_phase_b_to_c.py` → saves `<USER_OUTPUT_DIR>/eth_5m.annotated.png`
9. Step 6: 5-section reply in Chinese

**Reply** (Chinese prose, English technical terms):

```markdown
## 结论 / Conclusion
- **分析视角 / Lens**: ict_smc (strict)
- **Bias**: short-leaning（短期）
- **Confidence**: medium
- **操作建议**: 等 2245 retest + LTF CISD 后做空，SL 2270，目标 2210 → 2200
- **关键依据**:
  - 21:00 sell-side Liquidity Sweep at 2245（横盘支撑被穿）
  - Bearish Displacement (巨量阴线 2245 → 2205)
  - 当前 2226.5 在 0.5 retracement 区，未明确突破 2245

## 分析逻辑 / Analysis

**图上观察**:
- 11:00-19:00 在 2245-2280 区间震荡
- 21:00 巨量阴线，2245 关键支撑被跌穿
- 22:30 触底 2205
- 当前 2226.5 反弹至 0.5 retracement

**知识库匹配**:
- Liquidity Sweep（28 视频源）: 横盘低被穿
- Displacement: 快速 expansion 收盘穿透
- Breaker Block: 跌破的 2245-2255 等待 retest 转阻力

**规则推导**:
- Liquidity Sweep Rule 1 ✓: 价格扩张跌破前低
- Displacement Rule ✓: 快速 expansion + 收盘穿透
- Breaker formation in progress: 2245-2255 待 retest 确认

**驳回**:
- ❌ V-Shape Reversal: 反弹跨多根 K 线，不符合 1-3 根快速反转标准
- ❌ FVG entry at 5m: 未明确 CISD 触发，不足以入场

## 后续走势与操作 / Outcome Cases

**Case A (high)**: 反弹至 2245 breaker 后受阻继续下跌
- **触发信号**: 1m/5m 在 2240-2245 形成 rejection wick + CISD
- **操作建议**: 空头入场 2240-2245, SL 2270, T1 2210, T2 2200
- **失效条件**: 5m 收盘上穿 2255

**Case B (medium)**: 突破 2245 后向上回测 2260-2270
- **触发信号**: 5m 收盘上穿 2245 + 量能放大
- **操作建议**: 暂停空头计划，等 2260-2270 zone 反弹结束再评估
- **失效条件**: 突破 2270

**Case C (low)**: 进入 2210-2230 range bound
- **触发信号**: 多次测试 2210/2230 双向不破，量能萎缩
- **操作建议**: 观望或 range scalp（多 2210，空 2230）
- **失效条件**: 任一侧带量突破

## 风险与失效 / Risks & Invalidation
- **主要风险**:
  - 反弹诱多陷阱（Liquidity Sweep common_mistakes #2）
  - 未等 LTF CISD 就入场（Breaker Block 规则要求）
- **整体 setup 失效**: 5m 收盘上破 2270
- **监控**: 等 5m K 线接近 2245 时的反应（rejection 还是 break-through）

📂 分析数据 / Analysis JSON: `<USER_OUTPUT_DIR>/eth_5m.analysis.json`
🖼️ 标注图 / Annotated chart: `<USER_OUTPUT_DIR>/eth_5m.annotated.png`
📚 知识来源 / Knowledge sources: ICT/SMC → Liquidity Sweep, Displacement, Breaker Block card IDs
```

### Example 2 — English user, ambiguous chart

User: [attaches BTC 1D long-range chart] "What's happening here?"

**Reply** (English, technical terms kept):

```markdown
## Conclusion
- **Bias**: uncertain
- **Confidence**: low
- **Action**: No actionable setup; wait for LTF (4H/1H) entry triggers
- **Key evidence**: Macro discount zone + VWAP overhead + mixed short/medium term momentum

## Analysis

**Chart observations**: BTC/USDT 1D, range 60K-105K, current 79.4K, VWAP 80.4K overhead.
30D +6%, 180D -15%.

**Knowledge base hits**: V-Shaped Reversal (macro form only), Premium/Discount Array, HTF/LTF Analysis.

**Rule application**: Price below EQ (82.5K) → in discount zone (long-favorable per Premium/Discount Array). V-Shape Rule 2 (1-3 candle reversal) fails for this multi-month rebound — caveat: "macro V" only.

**Rejected hypotheses**:
- ❌ FVG: daily timeframe resolution insufficient for 3-candle pattern
- ❌ Order Block: no CISD displacement visible
- ❌ Liquidity Sweep at 60K: chart doesn't show pre-Nov-2025 history to confirm

## Outcome Cases

当前结构信息不足以给出多场景预测：
- Missing: LTF (4H / 1H) charts for entry triggers + HTF (Weekly/Monthly) for macro bias
- Recommended: Drop to 4H/1H, look for CISD or MSS before considering directional bias

(Not forcing scenarios — chart genuinely too ambiguous at daily-only view.)

## Risks & Invalidation
- **Main risks**: Treating multi-month rebound as canonical V-Shape; anchoring stop to 60K macro low (poor R/R for daytrade)
- **Setup invalidation**: N/A (no setup to invalidate)

## Missing Information
- LTF (4H / 1H) for CISD / FVG / OB entry triggers
- Weekly / Monthly for macro bias
- Pre-Nov-2025 history to confirm 60K as macro swing low

📂 Analysis JSON: `<USER_OUTPUT_DIR>/btc_1d.analysis.json`
(标注图未生成：无可执行的 trade_setup，跳过自动 annotation)
```

### Example 3 — Non-chart image

User: [attaches a random meme image] "分析一下"

Reply (single line): "Not a trading chart. Please upload a candlestick / line / OHLC chart for analysis."

No JSON, no 5-section output.

### Example 4 — Multi-panel multi-timeframe

User: [attaches 5m + 15m ETHUSDT side-by-side] "帮我看一下"

Internal: detect 2 panels (panel_left=5m, panel_right=15m), assess medium-low resolution, retrieve HTF/LTF concepts, build alignment-based analysis.

Reply: same 5-section format. In Analysis section, note multi-panel + HTF/LTF alignment. In Cases, scenarios reference both timeframes. In Missing Info, mention "high-resolution single-panel HTF chart" if resolution is bottleneck. Step 7 will be skipped (multi-panel annotation not yet supported); add the "(标注图未生成)" note.
