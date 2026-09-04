# Workflow: Analysis Profile Orchestration

Read this document when the user explicitly names a methodology, school, or
source; asks to compare or combine them; excludes one; or when profile support
is uncertain. Plain default `ict_smc` requests use the route summarized in
`SKILL.md` without loading this reference. This document refines the selected
intent workflow and does not replace it.

## Route contract

Resolve this object before retrieval, indicator calls, analysis, or drawing:

```text
route = {
  intent,               # qna | analyze | annotate | klines
  mode,                 # strict | augment | compare
  primary_lens,         # selected analysis profile
  secondary_lenses,
  schools,
  sources,
  capabilities          # object; canonical schema below
}
```

The dimensions are independent:

- `intent` is exactly `qna`, `analyze`, `annotate`, or `klines`, selecting the
  matching workflow. Never emit `kline_analysis`, `kline`, or another alias.
- `primary_lens`/`secondary_lenses` select analytical methodologies.
- `schools` constrain school membership in retrieval.
- `sources` constrain a corpus, teacher, or collection. `Teach-Wuyuan`, for
  example, is a source spanning several schools; it is not itself a lens.
- `capabilities` records whether the route can perform the requested intent
  with exact filtering and any required native analyzer. It must always be the
  following object shape, never a list or string:

```json
{
  "exact_primary_school_filter": true,
  "native_market_analyzer": "supported",
  "source_filter": "not_requested",
  "intent_supported": true,
  "reason": null
}
```

Allowed values for `native_market_analyzer` are `supported`, `unsupported`, or
`not_required`; allowed values for `source_filter` are `supported`,
`unsupported`, or `not_requested`. `exact_primary_school_filter` and
`intent_supported` are booleans. `reason` is `null` when supported, otherwise a
short capability-gap string. The compatibility field name
`exact_primary_school_filter` now means that an exact School hard filter is
enforced on the selected School/evidence layer, not that a fused canonical
card's top-level label is sufficient. For Q&A, the native analyzer is
`not_required`.
For a requested source, use `source_filter=supported` only after the
`source_evidence_v2` collection accepts the exact source (and optional School)
intersection. If a legacy index has no evidence collection, use
`source_filter=unsupported`; a required strict/primary branch then has
`intent_supported=false`. If only an `augment` secondary is unavailable, the
supported primary may continue with `intent_supported=true`; omit that
secondary and explain it in `reason`. For a Phase 1 market `compare`, use
`intent_supported=false` and state that market compare is not implemented.

When work moves to another intent (for example Analyze → Annotate), preserve
the selected mode/lenses/schools/sources, set `intent` to the new workflow, and
recheck capabilities for that intent. Do not copy an obsolete `intent` or its
capability result unchanged.

With no explicit selector, resolve exactly:

```text
intent=<qna|analyze|annotate|klines selected by the scenario router>
mode=strict
primary_lens=ict_smc
secondary_lenses=[]
schools=[ICT, SMC]
sources=[]
capabilities=<canonical object above, evaluated for the selected intent>
```

For the knowledge-retrieval stage of this default route, use the School
projection layer with the explicit OR filter
`--layer school --schools ICT SMC`. Do not substitute canonical or unfiltered
retrieval. In Q&A `compare`, query each branch separately even though the CLI
can accept multiple schools. Market `compare` stops at the capability gate in
Phase 1.

## Routing priority

1. Honor explicit exclusions first (`不要 ICT`, `ChanLun only`).
2. Infer composition:
   - `compare`: “compare / vs / 对比 / 分别”, or multiple lenses with no
     requested primary. Phase 1 supports this mode for Q&A only; a market,
     chart, or annotation request using it fails the capability gate.
   - `augment`: “以 A 为主、B 为辅 / 参考 / 辅以 / augment”.
   - `strict`: “只按 / only / 不要混合”, or one explicit selector with no
     composition wording.
3. Resolve lens/school and source separately. “Wuyuan 的 SMC” means their
   intersection: `schools=[SMC]`, `sources=[Teach-Wuyuan]`.
4. A selector in the current request overrides a previously established
   conversational preference. Use `ict_smc` only when neither exists.
5. If an actionable `augment` request names multiple lenses but no primary,
   ask which lens controls the trade plan. For an explanatory Q&A request, use
   `compare` instead.

For source-only Q&A, set `primary_lens=source_scoped` and constrain retrieval
to that source. For source-only market analysis, ask for a supported lens: the
source name cannot choose a structural analyzer, and the default must not be
inserted after an explicit source selector. In Q&A `compare`, `primary_lens`
records the first requested branch for route shape only; it has no analytical
priority over `secondary_lenses`.

An explicitly selected boundary is closed: never widen it to another lens,
school, source, or unfiltered retrieval merely because results are sparse.

## Canonical School tags and aliases

Normalize user wording with `knowledge_base/schools.json`, the machine-readable
registry. The CLI also resolves registered aliases, then verifies that the
canonical School actually exists in the selected layer. A tag containing
spaces must be one shell argument, for example
`--schools "Order Flow"` or `--schools "Price Action" Wyckoff`.

| Exact `school` tag | Common user wording to normalize | Kind |
|---|---|---|
| `ICT` | ICT, Inner Circle Trader | native `ict_smc` lens |
| `SMC` | SMC, Smart Money Concepts | native `ict_smc` lens |
| `缠论` | ChanLun, Chan Lun, 缠论 | knowledge lens; Q&A only |
| `Price Action` | Price Action, PA | knowledge lens candidate; Q&A only |
| `Indicator Based` | Indicator Based, indicator strategies | category, not an automatic lens |
| `Risk Management` | Risk Management, risk | category, not an automatic lens |
| `General` | General, general trading | category, not an automatic lens |
| `Order Flow` | Order Flow | knowledge lens candidate; Q&A only |
| `Volume Analysis` | Volume Analysis, VSA, Volume Spread Analysis | knowledge lens candidate; Q&A only |
| `Elliott Wave` | Elliott Wave, Elliott, 波浪理论 | knowledge lens candidate; Q&A only |
| `Wyckoff` | Wyckoff, 威科夫 | knowledge lens candidate; Q&A only |
| `The Strat` | The Strat | knowledge lens candidate; Q&A only |
| `On-chain` | On-chain, Onchain, On Chain, 链上 | category, not an automatic lens |
| `Market Structure` | Market Structure | category, not an automatic lens |
| `Scalping` | Scalping | evidence-derived category; Q&A only |

`SMC/ICT` or equivalent wording resolves to `schools=[ICT, SMC]`. For an
explicit category that is not a methodology, Q&A uses
`primary_lens=school_scoped`; the category alone cannot select a market
analyzer. `Scalping` is present only through attributable source evidence, not
as a top-level canonical-card label. `Teach-Wuyuan` remains a source selector,
not a School tag.

## Retrieval layers

Use the narrowest layer that represents the route:

| Layer | Collection | Use |
|---|---|---|
| `school` | `school_knowledge_v2` | Default and School-scoped Q&A/grounding; contains only content safely attributable to each School |
| `evidence` | `source_evidence_v2` | Any exact source request; `--sources` is a hard metadata filter and may be combined with School/type filters |
| `canonical` | `knowledge_base` | Backward-compatible fused-card exploration only; never use it to claim strict School/source isolation |

School-only retrieval uses `--layer school --schools ...`. Source-only
retrieval uses `--layer evidence --sources ...`; a source-plus-School request
uses both filters in one query. `--exclude-schools`, `--list-schools`, and
`--explain-scope` are available for routing and diagnostics. Unknown selectors,
missing v2 collections, and empty hard-filter intersections fail closed before
embedding or semantic retrieval.

## Mode semantics

### `strict`

Use `layer=school` for a School boundary or `layer=evidence` for any source
boundary, then use only the selected lens and supported analyzers for
structural claims, bias, trade levels, and overlays. School projections retain
same-School synthesized fields and explicitly mapped per-source definitions;
cross-School fused rules that cannot be attributed are omitted. Evidence
records contain one exact source collection and School. If the selection has
no evidence or lacks a required capability, report that and stop the
unsupported work.
Generic risk management may still be stated as general safety context, but it
must not drive directional analysis or masquerade as part of the selected
methodology.

### `augment`

The primary lens alone owns the directional bias, entry, stop, targets, and
primary chart. Secondary lenses/sources may confirm, challenge, or add risk
context, with every contribution labeled. They must not silently replace or
blend away the primary methodology. A knowledge-only secondary may contribute
retrieved context but not structural signals it cannot compute. Market
`augment` requires a native analyzer for the primary lens; an ICT/SMC secondary
cannot rescue an unsupported primary.

### `compare`

Phase 1 supports `compare` only for Q&A. Run the same `layer=school` knowledge
query and `top-k` independently for each requested School, keep records and
citations separate, then summarize agreements and disagreements. Do not blend
conflicting definitions into one rule. For market analysis, chart analysis, or
annotation, fail the capability gate before network calls or artifact
generation; do not attempt a partial or SMC-backed comparison.

## Current capabilities

| Selector | Kind | Q&A | Market analysis / chart | Required behavior |
|---|---|---|---|---|
| `ict_smc` (`ICT`, `SMC`, or both) | lens/profile | supported | supported in strict mode or as the primary/labelled secondary side of augment; Phase 1 market compare is unsupported | Keep SMC evidence inside this lens |
| `chanlun` / `缠论` | lens/profile | supported with `--layer school --schools 缠论` | no native ChanLun analyzer or overlay yet | Never substitute SMC output |
| `Teach-Wuyuan` / `Wuyuan` | source | supported through `--layer evidence --sources Teach-Wuyuan` when the v2 collection/intersection exists | inherits a separately selected lens and still requires exact source evidence | Do not infer a lens from the source name |
| Other knowledge-base schools/categories | knowledge scope | supported when the School layer returns attributable evidence | unsupported unless a native analysis profile is declared | Treat retrieval coverage and analyzer support separately |

Perform this capability check before network calls or artifact generation. An
uploaded chart does not grant a profile an analyzer it does not have. An
annotation follow-up inherits the prior route's selectors, updates
`intent=annotate`, and rechecks capabilities; never re-resolve its selectors to
default `ict_smc` merely because annotation is a new intent.

The v2 retriever exposes separate School and evidence collections. Exact source
filtering is supported only on `layer=evidence`; `canonical`/`school`
`source_names` are aggregated provenance and must never be post-filtered as if
they were exact. An installation with only the legacy canonical collection
remains usable for compatibility, but cannot satisfy a strict School/source
route and must report that capability gap.

## Failure and partial-support behavior

| Condition | Required response |
|---|---|
| Unknown selector | Say it is unrecognized and ask for the intended school/source; do not default |
| Empty strict result or empty lens/source intersection | State that the selected scope has insufficient evidence; do not broaden it |
| Native analyzer missing | Do not produce authoritative structure, trade levels, or overlays for that profile |
| Exact source filtering unavailable (for example a legacy-only index) | Mark strict source mode unsupported; do not claim source isolation |
| `compare` with a market/chart/annotation intent | Phase 1 capability gap: stop before network/artifact work; do not return a partial comparison |
| Unsupported branch in Q&A `compare` | Return supported knowledge branches only when still useful and label the missing branch explicitly |
| Unsupported secondary in `augment` | Continue the supported primary; omit or label the secondary as knowledge-only |
| Branches disagree | Preserve the disagreement and each branch's invalidation; do not merge them |

Market-data freshness and API failure rules remain those of the selected intent
workflow. Report data failure separately from profile or retrieval failure.

## Examples

- `BTC 1h 怎么样` → strict default `ict_smc`, `schools=[ICT, SMC]`.
- `只按缠论解释中枢` → strict ChanLun Q&A with
  `--layer school --schools 缠论`.
- `用缠论分析 BTC 1h` → strict ChanLun market analysis is currently
  unsupported; explain the missing native analyzer and do not call SMC as a
  substitute.
- `只按 Wuyuan 解释 Breakout` → strict source Q&A with
  `--layer evidence --sources Teach-Wuyuan`; an absent collection or empty
  intersection fails closed.
- `用 Wuyuan 的 SMC 解释 Order Block` → strict intersection of the SMC lens
  and `Teach-Wuyuan` source.
- `对比 ICT 和缠论怎么看 BTC 4h` → Phase 1 market compare is
  unsupported; stop before fetching OHLCV and explain the capability gap.
- `以 SMC 为主，参考 Wuyuan 看 BTC 1h` → augment with SMC controlling bias
  and trade levels; retrieve the secondary context with
  `--layer evidence --schools SMC --sources Teach-Wuyuan`.
