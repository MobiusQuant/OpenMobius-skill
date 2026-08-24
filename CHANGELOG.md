# Changelog

All notable changes to **OpenMobius-skill** are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

中文版本：[CHANGELOG.zh.md](./CHANGELOG.zh.md)

---

## [Unreleased]

### Fixed

- Corrected uninstall CLI and documentation semantics: standard uninstall
  removes the entire self-contained platform target, while `--full` is now
  explicitly documented as a deprecated compatibility no-op.

---

## [0.3.0] — 2026-07-05

### 📚 Knowledge base — full rebuild & expansion

1. **KB expanded: 665 concepts + 1,246 cases → 726 concepts + 1,282
   cases**, now merged from 12 curated source collections (300+ teaching
   videos & live lessons) through an audited cross-collection pipeline.

2. **New school: ChanLun (缠论).** 58 concept cards + 52 case cards
   covering 笔 / 中枢 / 买卖点 applied to US equities — the skill now
   answers 缠论 questions with dedicated cards instead of generic
   price-action knowledge.

3. **New source collections**: live-lesson series (gold / crude
   intraday trading, 153 cases), SMC Strong/Weak supplement, and
   additional SMC supplement playlists.

4. **Audited concept merge.** Every concept card now comes from the
   cross-collection fusion pass with sampled model audit; per-source
   definitions are preserved on each card (`definition_per_source`) so
   school-specific nuances aren't averaged away.

5. **Content-level case deduplication.** Duplicate case extractions
   across collections (same video, same time range) are removed via
   time-range overlap matching with an asset-consistency guard —
   ~280 bilingual duplicates dropped, zero distinct cases lost
   (verified against v0.2.0: 665/665 concepts and 890/890 unique case
   contents carried over).

6. **Terminology continuity.** All v0.2.0 canonical terms and aliases
   are preserved in `term_aliases.json` — older spellings (MSS, CSD,
   IFVG variants, …) still retrieve the new cards.

### 📝 Docs

- README (EN/中文), SKILL description, and per-platform frontmatter
  updated to the new KB stats; added a Contributors section.

---

## [0.2.0] — 2026-05-23

### ✨ New features

1. **SMC structural indicator is the default market-analysis source.**
   When you ask about a chart or asset, the skill now auto-fetches a
   complete structural signal set (BOS/CHoCH events, Order Blocks,
   Fair Value Gaps, Equal H/L liquidity, Premium/Discount zones,
   Strong/Weak pivot labels) instead of reaching for generic
   technical indicators.

2. **Fully auto-generated chart overlay.** Structural elements
   (Order Blocks, Fair Value Gaps, BOS/CHoCH events, key levels) are
   rendered onto the chart automatically — no more hand-coded JSON.
   Faster output, no drawing mistakes.

3. **Volume sub-panel.** A volume histogram now appears below the main
   chart, colored green/red by candle direction.

4. **TradingView-style charts.** Light theme by default; bear Order
   Blocks in soft pink, bull Order Blocks in soft blue; BOS/CHoCH text
   labels in matching colors — aligned with mainstream charting
   conventions.

5. **Mandatory data-freshness disclosure.** Every market reply now
   includes the data timestamp, fetch time, and bar age. When data is
   stale, a ⚠️ warning is added automatically.

6. **Standardized data-source response.** When you ask "where is this
   data from / is it real-time?", the skill replies with a canonical
   template (Mobius Quant API) — no more fabricated upstream vendors.

7. **Knowledge base expanded.** From 380 concepts + 584 cases to
   **665 concepts + 1,246 cases**. Core SMC concepts including CHoCH,
   Strong/Weak Pivots, and Protected High/Low are now backed by
   dedicated cards.

### 🎨 Experience improvements

1. **No more answering from memory.** When you ask "how is BTC?" —
   even without saying "now" — the skill is required to fetch fresh
   data, eliminating stale prices and hallucinated numbers.

2. **No more indicator-name priming.** References to specific
   technical indicators have been removed from descriptions and
   examples, so the model doesn't reach for them reflexively.

3. **Default candle count raised: 200 → 300.** Extra warmup for the
   SMC indicator's long-period calculations yields more stable
   structural reads.

4. **Cleaner chart labels.** The right axis keeps only the key levels
   (Strong High / Weak Low / entry / SL / target); Order Blocks and
   Fair Value Gaps render as rectangles without crowding labels.

5. **`--trade-setup` simplifies user-level annotations.** Authoring a
   trade plot now means writing a tiny JSON file with entry/SL/target
   lines — the structural overlay is merged automatically.

### 🐛 Bug fixes

1. **Platform description over the length limit.** The claude-code
   yaml description exceeded 1,024 characters, causing Codex and
   similar platforms to reject or truncate the skill. Trimmed to a
   compliant length.

2. **Chart markers overwriting each other.** Multiple marker groups
   (BOS, CHoCH, EQH, etc.) only displayed the last group rendered;
   fixed to accumulate all markers.

3. **Occasional chart render crash.** A null-timestamp edge case
   crashed the entire chart; defenses added.

4. **K-lines squeezed into a corner.** When historical structural
   events fell outside the visible K-line range, the time axis
   auto-stretched and compressed candles. Time-clipping fixed.

5. **Volume bars couldn't be colored by direction.** Previously a
   single color; now per-bar red/green based on close vs open.

6. **Chart elements truncated by a too-low cap.** The default item
   limit was too restrictive and dropped some Order Blocks / Fair
   Value Gaps; raised to a sensible value.

7. **Missing SMC zone data.** A required server-side parameter wasn't
   being sent, so Premium/Discount/Equilibrium zones came back empty.
   Now auto-included.

---

## [0.1.0] — 2026-05-13

- Initial release: 380 concepts + 584 cases distilled from 130 ICT/SMC
  teaching videos; four interaction modes (concept Q&A, chart-image
  analysis, chart annotation, K-line analysis); chart generation via
  Playwright + lightweight-charts.
