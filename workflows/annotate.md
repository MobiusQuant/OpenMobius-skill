# Workflow: Chart Annotation

For when the user wants visual markup on a chart image — either standalone or as a follow-up to a prior analysis.

Use the host-neutral placeholders from `SKILL.md`: resolve the attachment as
`<INPUT_IMAGE>`, create `<TEMP_DIR>`, and choose `<USER_OUTPUT_DIR>` for the
returned PNG. In Path A, `<ANALYSIS_JSON>` is the host-resolved existing
analysis artifact. Every `kb_retrieve.py` command is shorthand that must be
expanded to `<PYTHON> scripts/kb_retrieve.py` before execution.

## When this workflow applies

- User attached a chart AND explicitly wants drawing: "draw / annotate / mark / highlight / overlay / 标一下 / 画出来 / 标注 / 在图上标 / 出张图 / 帮我画"
- Follow-up after `analyze.md` produced an analysis with `trade_setup` and user says "把这个画在图上" / "重新画一下" / "换个颜色"

## When NOT to use

- User attached a chart + asked for analysis → use `analyze.md` (it auto-annotates as last step)
- Pure concept Q&A → use `qna.md`
- Non-trading images → not this skill

## The two paths

### Route inheritance (mandatory before either path)

Use the route contract summarized in `SKILL.md`. Read
`workflows/analysis_profiles.md` only when the user explicitly names or
excludes a lens/School/source, requests augment/compare, or capability support
is uncertain. Annotation never invents a new analytical lens:

- **Path A** inherits the route's `mode`, `primary_lens`, `secondary_lenses`,
  `schools`, `sources`, plus `knowledge_sources`, from the existing analysis
  JSON. Set `intent=annotate` and recompute `capabilities` for annotation. A
  color/layout-only request does not change the inherited selectors.
- If a legacy analysis JSON has no `route`, recover it from the prior analysis
  only when exact. Otherwise switch to Path B; never default and relabel old
  levels as `ict_smc`.
- If the user explicitly changes school/source/profile, the old analytical
  levels cannot merely be relabelled. Switch to Path B and re-evaluate them
  under the new route.
- **Path B** resolves selectors exactly as `analyze.md` Step 0 does, then sets
  `intent=annotate` and checks annotation capabilities. With no explicit
  selection, use `intent=annotate`, `mode=strict`,
  `primary_lens=ict_smc`, `secondary_lenses=[]`, `schools=[ICT, SMC]`,
  `sources=[]`, with `capabilities` populated by the profile capability check.

Honor `strict` and `augment` exactly as defined in `analysis_profiles.md`.
Strict School grounding uses `school_knowledge_v2`; any exact source boundary
uses `source_evidence_v2`. If the analyzer cannot establish a requested
School's signals, fail closed and do not draw inferred entry/SL/target levels
for that lens. Phase 1 does not support compare-mode market annotation; fail
its capability gate before drawing or creating an artifact.

A fail-closed response still names the requested lens/schools/sources and
missing capability, and produces no misleading annotated artifact.

**Path A: User already has an analysis JSON** (from running `analyze.md` previously)
→ Use the glue script `kb_phase_b_to_c.py` directly — don't redo analysis.

**Path B: User wants annotation but no analysis has been done yet**
→ Run a slimmed version of `analyze.md` Steps 1-4 to decide what to annotate, then build the annotation JSON manually and call `kb_draw_annotation.py`.

---

## Path A: Reuse existing analysis JSON

```text
<PYTHON> scripts/kb_phase_b_to_c.py --input <ANALYSIS_JSON> --image <INPUT_IMAGE> --output <USER_OUTPUT_DIR>/annotated.png
```

Optional overrides:
- `--chart-bbox "x,y,w,h"` — override bbox from JSON
- `--y-range "top,bottom"` — override y_axis_range from JSON
- `--theme dark|light` — override theme

The tool reads `chart_bbox` / `y_axis_range` / `theme` from the JSON, maps `patterns` + `trade_setup` to annotations, and renders the image. You don't need to write the annotation JSON yourself in this path.

---

## Path B: Build annotation JSON manually

### Step 1: Examine the chart (same as analyze.md Step 1 + 1a + 1b)

Read the chart inventory; detect multi-panel; assess resolution. If low resolution, downgrade confidence and prefer fewer, broader annotations.

### Step 2: Calibrate the coordinate system (CRITICAL)

Same as `analyze.md` Step 1c. For EACH panel:

- `chart_bbox`: pixel position of the plotting area (exclude toolbars, price-label gutters, time scale)
- `y_axis_range`: top + bottom prices on the y-axis
- `theme`: dark / light

**Why**: every annotation's pixel position is derived from these. Wrong bbox → annotations drawn outside the chart.

### Step 3: Retrieve relevant concepts

```text
kb_retrieve.py "<keywords>" --layer school --schools ICT SMC --top-k 5
```

Replace `ICT SMC` with the inherited/resolved route's exact canonical School
tags and quote tags containing spaces. Use 2-5
candidate keywords from hypothesized patterns. In `augment`, keep primary and
secondary retrievals separate. Annotation `compare` must already have stopped
at the capability gate in Phase 1.
If `route.sources` is non-empty, use `--layer evidence --sources ...` and add
the route's `--schools ...` when present. Continue only when that exact
intersection is supported; post-filtering a broader top-K is not valid.

### Step 4: Decide what to annotate

Based on retrieved cards' identification rules + chart evidence, produce a list of annotations.

Two supported annotation types:

#### `horizontal_line` (price level)

Use for: Entry / Stop Loss / Targets / key Liquidity levels / Mean Threshold

```json
{
  "type": "horizontal_line",
  "price": 73000,
  "label": "Long Entry @ OTE",
  "color": "#00ff88",
  "style": "solid" | "dashed",
  "label_position": "right" | "left"
}
```

#### `rectangle` (price-range zone)

Use for: FVG / Order Block / Breaker / Mitigation / Premium-Discount zone / Killzone

```json
{
  "type": "rectangle",
  "price_top": 74500,
  "price_bottom": 73000,
  "x_pct_start": 0.6,
  "x_pct_end": 1.0,
  "label": "FVG",
  "fill_color": "#00ff8830",
  "border_color": "#00ff88"
}
```

### Step 5: Build the annotation JSON

Single panel:

```json
{
  "input_image": "<INPUT_IMAGE>",
  "output_image": "<USER_OUTPUT_DIR>/annotated.png",
  "route": {
    "intent": "annotate",
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
  "theme": "dark",
  "panels": [
    {
      "panel_id": "main",
      "chart_bbox": {"x": 50, "y": 30, "width": 800, "height": 400},
      "y_axis_range": {"top": 96000, "bottom": 70000},
      "annotations": [ ... ]
    }
  ]
}
```

Multi-panel: add more entries to `panels` array, each with its own `chart_bbox` / `y_axis_range` / `annotations`.

Save to `<TEMP_DIR>/annotation.json` with the host's file-writing tool or a JSON
serializer.

### Step 6: Call the drawing tool

```text
<PYTHON> scripts/kb_draw_annotation.py --json <TEMP_DIR>/annotation.json
```

Prints the output path on success.

### Step 7: Output to user

Tell the user:
1. **Original image path**
2. **Annotated image path**
3. **What was annotated** (summary list): "Entry at 73000 / SL at 71000 / T1 at 95200 / FVG zone 73000-74500"
4. **Confidence level** + rationale
5. If low confidence — what would improve it (better resolution, manual bbox confirmation, etc.)
6. **Lens** — inherited/resolved profile, schools, and mode
7. **Knowledge sources** — school/source labels and card IDs behind the drawn levels

---

## Color palette (semantic defaults)

| Role | Color | Use |
|---|---|---|
| Entry (long) | `#00ff88` solid | bullish entry |
| Entry (short) | `#ff4444` solid | bearish entry |
| Stop Loss | `#ff4444` dashed | risk |
| Target | `#4488ff` solid | T1/T2/T3 |
| FVG (bullish) | `#00ff88` border + `#00ff8830` fill | bullish FVG |
| FVG (bearish / IFVG) | `#ff8844` border + `#ff884430` fill | bearish or inverted |
| Order Block / Breaker | `#aa55ff` border + `#aa55ff30` fill | OB family |
| Liquidity Sweep level | `#ffaa00` dashed | swept liquidity |
| Discount zone | `#00ff88` border + `#00ff8820` fill | below EQ |
| Premium zone | `#ff4444` border + `#ff444420` fill | above EQ |

Deviate only if user requests specific colors. **Prefer consistency.**

## Constraints

1. **No fabricated levels** (shared rule) — every `price` traces to chart evidence or a retrieved rule
2. **No invented bbox** — conservative estimate if unsure; state low confidence
3. **Respect multi-panel boundaries** — annotations for `panel_left` must NOT extend into `panel_right`
4. **Theme matches background** — `dark` for most TradingView/Binance defaults
5. **Label limit** — ≤ 8 annotations per panel for readability; pick most actionable (entry/SL/T1/T2 + 2-3 most relevant zones)
6. **Language** (shared rule) — Chinese prose / English technical terms; JSON labels default to English unless user requests Chinese
7. **Resolution-aware** — low resolution → fewer, broader annotations + downgraded confidence
8. **Route-faithful** — strict uses exact School/evidence-layer scoping;
   augment labels secondary marks; Phase 1 compare annotation fails
   before artifact generation

## Examples

### Example 1 — Path A: follow-up after analysis

User (after running analysis on BTC 1D): "把上面分析的标到图上"

```text
<PYTHON> scripts/kb_phase_b_to_c.py --input <ANALYSIS_JSON> --image <INPUT_IMAGE> --output <USER_OUTPUT_DIR>/IMG_0557.annotated.png
```

Reply: output path + summary of what was annotated.

### Example 2 — Path B: standalone annotation on BTC 1D

User: [attaches BTC 1D chart] "把我应该入场的位置标在图上"

1. **Examine**: BTC 1D, price 79K, range 60K-105K, VWAP at 80,388
2. **Calibrate**: bbox: {x: 30, y: 200, w: 580, h: 500}, y_range: {top: 110000, bottom: 60000}, theme: dark
3. **Retrieve**: `kb_retrieve.py "discount zone OTE entry swing low" --layer school --schools ICT SMC`
4. **Decide**:
   - Long Entry near 75K (lower third of range, in discount)
   - SL below 60K (with caveat: stop too wide for daytrade)
   - T1: 82.5K (EQ), T2: 95K (prior swing high), T3: 105K
   - Discount zone rectangle 60K-82.5K
5. **Build JSON** → save to `<TEMP_DIR>/btc_annotation.json` and set its
   `output_image` to `<USER_OUTPUT_DIR>/IMG_0557.annotated.png`
6. **Call** `<PYTHON> scripts/kb_draw_annotation.py --json <TEMP_DIR>/btc_annotation.json`
7. **Reply**: "Annotated chart saved to `<USER_OUTPUT_DIR>/IMG_0557.annotated.png`. Marks: Long bias entry zone 75K, SL 60K (wide), T1 82.5K, T2 95K, T3 105K, Discount zone 60K-82.5K shaded. Confidence: low (HTF bias unclear; daily TF stops are very wide)."

### Example 3 — Multi-panel ETH 5m + 15m

User: [attaches dual-panel image] "画一下 short setup"

1. **Examine + 1a**: 2 panels, both ETHUSDT.P, left=5m, right=15m
2. **Calibrate each**:
   - panel_left: {x: 20, y: 30, w: 620, h: 400}, y_range: {top: 2280, bottom: 2200}
   - panel_right: {x: 680, y: 30, w: 620, h: 400}, y_range: {top: 2300, bottom: 2200}
3. **Retrieve**: `kb_retrieve.py "5m 15m HTF LTF alignment short entry breaker" --layer school --schools ICT SMC`
4. **Decide**: on both panels: 2245 breaker (rectangle), entry 2245 (line), SL 2270 (dashed), T1 2210 / T2 2200 (lines)
5. **Build JSON with 2 panels**
6. **Call draw tool** (handles multi-panel automatically)
7. **Reply**: confirm HTF+LTF alignment; output path

### Example 4 — User wants custom color

User: "把入场点标成紫色"

Use `#aa55ff` for entry instead of default green. Document the choice in output summary.
