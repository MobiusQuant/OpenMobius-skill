# Workflow: Concept Q&A

For text-based, multi-school trading-knowledge questions and installed-skill
capability discovery. Ordinary Q&A applies when **no chart is attached**.

Use the host-neutral path placeholders from `SKILL.md`. Every
`kb_retrieve.py` command below is launcher-neutral shorthand that must be
expanded to `<PYTHON> scripts/kb_retrieve.py` before execution; do not assume
the script is on `PATH`.

## When this workflow applies

- User asks "what is X" / "how to identify Y" / "how to enter Z"
- Topics on ICT / SMC / Wyckoff / VSA / Order Flow / Price Action
- User mentions specific terms (FVG / OB / Killzone / CISD / OTE / Liquidity / Inducement / Breaker / IFVG, etc.)
- User asks about trading strategy, market structure, risk management
- User asks what analysis lenses, Schools, composition modes, or intent
  capabilities this installed skill currently supports

## When NOT to use

- User attached a chart and requests analysis → switch to `analyze.md`;
  capability-only questions remain in the special case below
- General greetings / unrelated topics → no skill needed

The capability-discovery exception below takes priority over the chart and
asset/timeframe rules: a question about whether a method *can* analyze a chart
or market is not itself a request to perform that analysis.

## Control-plane special case: capability discovery

Treat a request as capability discovery when its subject is the installed
skill's available analysis models/profiles, lenses, Schools, trading schools,
composition modes, or supported intents. This includes targeted questions such
as "缠论能不能直接分析 BTC 1h?" or "Can Wyckoff analyze the
current chart?", even though they contain an asset, timeframe, or chart
reference. A conceptual question such as "ICT 是什么?" remains ordinary
knowledge Q&A unless it asks what this skill supports.

In this branch, interpret “analysis model” as a trading methodology/profile
when the user is asking about this skill. If they mean the underlying LLM or
embedding model, explain that distinction instead of presenting School
selectors as AI models.

Capability discovery is a control-plane branch of `intent=qna`, not a fifth
intent. Run it before inheriting an analysis route or applying the default
`strict` ICT/SMC route. Read `workflows/analysis_profiles.md`, then inspect the
installed School index with exactly:

```text
kb_retrieve.py --layer school --list-schools --format json
```

Keep this branch mechanically bounded: use this workflow, the declared
capability table in `workflows/analysis_profiles.md`, and exactly one inventory
command above. The inventory plus that contract is authoritative for this
answer. Do **not** invoke the skill recursively, delegate to a subagent or
background task, run `git`, search/scan the repository, enumerate source
collections, inspect individual knowledge cards/index manifests/analyzer code,
or perform extra cross-checks. When the inventory command succeeds, synthesize
the response immediately.

This command is inventory inspection, not semantic card retrieval. Use its
`schools[]` fields (`name`, `count`, `available_in_layer`, `kind`,
`knowledge_qna`, and `native_market_analyzer`) together with the declared
profiles and mode constraints in `analysis_profiles.md`. Describe a School as
currently indexed only when `available_in_layer=true`; report registered but
missing entries separately rather than presenting them as operational. The
`count` is the number of records in this layer, not a measure of analyzer
quality, and this inventory call does not itself exercise a native analyzer.

For a broad inventory question, group the answer as follows:

1. **Native market-analysis profiles** — report declared profiles, not one
   invented profile per School. At present, `ict_smc` covers the `ICT` and
   `SMC` School selectors and has the native SMC analyzer.
2. **Q&A-only analysis lenses** — entries with `kind=analysis_lens`,
   `knowledge_qna=true`, and no native market analyzer. State plainly that
   retrieval coverage does not make them current-market/chart analyzers.
3. **Knowledge categories** — entries with `kind=knowledge_category`. Label
   them as retrieval scopes, not analysis models.
4. **Composition modes** — summarize `strict`, `augment`, and `compare` from
   `analysis_profiles.md`, including that Phase 1 `compare` is Q&A-only.

For a targeted support question, answer the named selector directly with its
kind and supported intents; a full catalog is optional. Keep the dimensions
distinct in either form:

- A `source` such as `Teach-Wuyuan` is a corpus filter, not a School, lens, or
  native analyzer. It must be paired with a separately supported lens for
  market work.
- `intent` means `qna`, `analyze`, `annotate`, or `klines`; it is not an
  analysis model or composition mode.
- Counts and `available_in_layer` describe the installed School knowledge
  index. Analyzer support is the separate declared capability in the registry
  and `analysis_profiles.md`.
- Registry `availability=evidence_only` means the label is not present as a
  top-level canonical-card label and was derived from attributable source
  evidence. It does not by itself mean that no derived School projection is
  indexed; report the inventory result separately.

If the command cannot run or the School index cannot be inspected, read
`knowledge_base/schools.json` plus `workflows/analysis_profiles.md` as a
declarative fallback. Explicitly say that these are registered capabilities
and that **operational availability was not verified**; do not imply that the
indexed records or analyzers were exercised successfully.

Then stop. Do **not** run a normal query/top-K retrieval, construct or inherit
the default School route, fetch market data, call an API, inspect/generate a
chart, create another artifact, or continue validating an already successful
inventory. Do not append the knowledge-card lens/source provenance footer to a
capability response.

## Analysis route (resolve or inherit first)

Use the route contract summarized in `SKILL.md`. Read
`workflows/analysis_profiles.md` only when the user explicitly names or
excludes a lens/School/source, requests augment/compare, or capability support
is uncertain. If a caller or an earlier workflow supplied a route, inherit its `mode`,
`primary_lens`, `secondary_lenses`, `schools`, and `sources`; set
`intent=qna` and recompute `capabilities` for Q&A. An explicit lens, school,
source, or mode in the current request overrides the corresponding inherited
selector. If no route or selector exists, use the exact default route:
`intent=qna`, `mode=strict`, `primary_lens=ict_smc`,
`secondary_lenses=[]`, `schools=[ICT, SMC]`, `sources=[]`, with
`capabilities` populated by the profile capability check.

Apply the route mode without silently blending schools:

- **strict** — use `layer=school` for a School boundary and `layer=evidence`
  for any exact source boundary. School projections omit cross-School fused
  rules that cannot be attributed; evidence records contain one exact School
  and source collection. If the requested route cannot be represented by the
  installed index, stop and state the capability gap; do not substitute
  another lens or the fused canonical layer.
- **augment** — `primary_lens` alone controls the answer's primary
  methodology. Secondary lenses/sources may only provide clearly labelled
  confirmation, challenges, or risk context and must not override it.
- **compare** — run the same query separately for every selected school,
  preserve attribution, and report agreements and conflicts side by side.
  Never synthesize conflicting rules into a single rule.

A fail-closed response still names the requested lens/schools/sources and the
failed entry in `route.capabilities`; it does not present substitute evidence.

## Special case: data-source / freshness questions

If the user's question is about the data source / pipeline / freshness
/ upstream vendors — e.g. "数据从哪来 / 数据源 / data source / where is
this data from / 你用什么数据 / 实时吗 / 怎么取的数据" — **do NOT call
`kb_retrieve.py`**. Instead, respond using the canonical data-source
disclosure from `SKILL.body.md` § "Data source disclosure".

If the conversation has already produced an API response in this turn
(klines / indicators / chart), substitute its `freshness` block and
`exchange`/`market`/`symbol` fields into the template.

If no API call has been made yet, answer:

> 本对话尚未发起行情数据请求；如果你接下来问某个资产的行情，数据将通过
> **Mobius Quant API** (`api.mobiusquant.ai`) 获取。具体上游来源（Mobius
> 内部接入哪些交易所 / 数据供应商）skill 无法核实，详情见
> [mobiusquant.ai](https://www.mobiusquant.ai/)。

Then stop. Do NOT add SMC analysis or chart in response to a data-source
question alone.

## Steps

### Step 1: Retrieve relevant cards

Extract the core concepts from the user's question (prefer English technical terms), then run:

```text
kb_retrieve.py "<query>" --layer school --schools ICT SMC --top-k 5
```

Replace `ICT SMC` with the route's resolved schools. The default must still be
written explicitly; never rely on an implicit all-school search. Use the exact
tag and alias table in `analysis_profiles.md`; quote tags containing spaces.
If `route.sources` is non-empty, switch to `--layer evidence --sources ...`
and add `--schools ...` when the route also constrains School. Proceed only
when `route.capabilities` confirms that the v2 collection and hard-filter
intersection exist. Never approximate a source boundary by filtering a
broader top-K result.

Variants:

```text
# Case-only retrieval
kb_retrieve.py "BTC reversal liquidity sweep" --layer school --schools ICT SMC --type case --top-k 5

# Strict single-school route
kb_retrieve.py "smart money concepts market structure" --layer school --schools ICT --top-k 5

# Exact source + School intersection
kb_retrieve.py "Order Block" --layer evidence --schools SMC --sources Teach-Wuyuan --type concept --top-k 5

# Compare route: equal query and top-k, isolated result sets
kb_retrieve.py "market structure break" --layer school --schools ICT --top-k 5
kb_retrieve.py "market structure break" --layer school --schools SMC --top-k 5
```

### Step 2: Synthesize the answer

The School/evidence layers return scoped documents plus scalar provenance
metadata (`record_id`, `canonical_id`, `school`, and, for evidence, `source`,
`content_type`, and `ref`). They deliberately do not load the fused parent
canonical card. Use those record IDs and references for citations.

**Strict requirements**:

1. **Anchor every claim to the knowledge base** — cite specific rule numbers
2. **No vague generalities** — give concrete identification steps, entry points, stop placements
3. **If retrieval is insufficient** — say "knowledge base does not explicitly cover X, but concept Y may be relevant"
4. **Link related concepts** — when discussing FVG, mention PD Array / OTE / CISD relations
5. **Match user's language** — Chinese question → Chinese answer; English → English (technical terms stay English per shared rules)
6. **Honor route semantics** — strict stays within the selected School/evidence
   records; augment labels additions; compare keeps each School's records and
   citations separate
7. **Expose provenance** — name the active lens/mode and list the school/source
   labels and card IDs actually used

## Query optimization tips

- **Use English technical terms** for best retrieval (knowledge base is English):
  - "如何识别市场反转" → retrieve `"market structure shift trend reversal"`
  - "止损放哪" → retrieve `"stop loss placement swing point"`
- Join multiple concepts with spaces to let vector search match related clusters
- For case queries, use concrete features: `"4H FVG liquidity sweep entry"`

## Examples

### Example 1 — Concept question

User: "什么是 Fair Value Gap，怎么交易"

Action:
```text
kb_retrieve.py "Fair Value Gap how to trade entry" --layer school --schools ICT SMC --top-k 5
```

Response (in Chinese, technical terms in English):
- Precise FVG definition (three-candle non-overlap pattern)
- 3 identification rules (specific bullish/bearish criteria)
- Entry strategy (wait for CISD confirmation + entry at OTE 0.62-0.79 + stop below swept low)
- Common mistakes (5 concrete pitfalls, citing the knowledge base)

### Example 2 — School overview

User: "ICT 是什么流派，它的核心方法论是什么"

Action:
```text
kb_retrieve.py "ICT methodology smart money concepts" --layer school --schools ICT --top-k 8
```

Response:
- Positioning of ICT (Inner Circle Trader)
- 4-5 core tools (OB / FVG / Liquidity Sweep / Killzone)
- Typical workflow: HTF bias → PD Array → CISD → entry → stops/targets
- Common misapplications

### Example 3 — Case query

User: "找一个 BTC 在 FVG 反转的真实案例"

Action:
```text
kb_retrieve.py "BTC bitcoin Fair Value Gap reversal entry" --layer school --schools ICT SMC --type case --top-k 3
```

Response: Extract 1-2 most relevant cases' `analysis_steps` + `lessons`.

## Output format

Standard prose answer (no JSON, no special structure). The 5-section format with auto-annotation is for chart analysis only — Q&A is free-form structured prose with clear sections like:

```markdown
## 定义 / Definition
<from card>

## 识别规则 / Identification Rules
1. <rule 1>
2. <rule 2>
...

## 交易意义 / Trading Implication
<from card>

## 常见错误 / Common Mistakes
- <mistake 1>
- <mistake 2>

## 相关概念 / Related
<linked terms>
```

Adapt section headers to fit the question type (e.g. for a strategy question, lead with "Strategy" rather than "Definition").

End every knowledge answer with these provenance lines (they do not apply to
the data-source-only or capability-discovery special cases and do not change the
free-form Q&A section structure):

```text
🧭 分析视角 / Lens: <route.primary_lens + secondary_lenses> (<route.mode>)
📚 知识来源 / Knowledge sources: <school/source labels + card IDs actually used>
```
