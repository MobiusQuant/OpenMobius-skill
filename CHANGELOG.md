# Changelog

All notable changes to **OpenMobius-skill** are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

中文版本：[CHANGELOG.zh.md](./CHANGELOG.zh.md)

---

## [Unreleased]

### Added

- Added current Agent Skills compatibility contracts for Claude Code, Codex,
  OpenClaw, Hermes, Cursor, and WorkBuddy. Cursor now has user/project skill
  support, Codex includes optional `agents/openai.yaml` interface metadata,
  and WorkBuddy has a deterministic ZIP builder for local desktop import and
  separate Open Platform publication submission.
- Added three retrieval layers: backward-compatible fused `canonical`,
  attributable `school`, and atomic exact-source `evidence`. `kb_retrieve.py`
  now supports hard School/source/type/exclusion intersections, School aliases,
  inventory/scope diagnostics, and deterministic exact term/alias boosting.
- Added multi-school analysis-profile orchestration. The default route is strict
  ICT/SMC, explicit School selectors never silently fall back, and Phase 1
  compare mode is available for knowledge Q&A while unsupported native market
  analyzers fail closed before fetching data or generating artifacts.
- Added a machine-readable 15-label School registry, published JSON Schemas,
  and a deterministic evidence builder. The current corpus produces 2,144
  School projections and 18,645 exact-source evidence records; ambiguous fused
  content is skipped with auditable reason counts.
- Added safe v2 index upgrades with a versioned manifest, input fingerprint,
  staging verification/rollback, and doctor checks. School/evidence records now
  receive independent document embeddings backed by a content-addressed,
  model-isolated incremental cache; parent-vector inheritance remains an
  explicit emergency/testing option only.
- Added a verified float32 release seed for native v2 embeddings, split into 16
  content-addressed shards. Builds use persistent cache, then exact seed hits,
  then compute only misses; a guarded exporter refuses incomplete caches and
  atomically publishes only a fully re-verified seed.
- Added hybrid v2 retrieval: hard-filtered BM25 candidates and semantic
  candidates are fused with reciprocal-rank fusion, exact canonical terms and
  aliases stay first, and per-canonical diversity prevents repeated atomic
  evidence from crowding out other relevant concepts. Lexical-only retrieval
  works without loading an embedding model.
- Added a versioned retrieval-evaluation dataset and CLI reporting Recall@K,
  MRR, scope/source purity, fail-closed behavior, duplicate-parent rate, and
  latency, plus a checked-in `auto` release baseline. This makes retrieval
  changes measurable before release.
- Added a checksummed WorkBuddy compact corpus that losslessly reconstructs all
  2,144 School projections and 18,645 exact-source evidence records with only
  the Python standard library, preserving hard School/source lexical retrieval.
  Generated package prose derives both counts from the same verified build
  result instead of relying on stale literals.

### Changed

- Standardized the skill slug and installed directory name as lowercase
  `openmobius-skill` while retaining **OpenMobius-skill** as the product and
  repository name. Codex now installs to `~/.agents/skills`, OpenClaw respects
  `OPENCLAW_STATE_DIR`, Hermes respects `HERMES_HOME`, and Cursor installs to
  `~/.cursor/skills` for local user use.
- On Linux/macOS, `--platform all` means the five hosts with documented local
  discovery paths: Claude Code, Codex, OpenClaw, Hermes, and Cursor. OpenClaw
  and Hermes are not advertised by this release on Windows; Windows users
  select Claude Code, Codex, or Cursor explicitly.
- WorkBuddy setup now distinguishes local ZIP import, installation of a
  published marketplace copy, and Open Platform publication. Its public docs
  do not define a fixed third-party-writable directory for automatic discovery;
  explicit `--target-dir` mode is therefore developer staging/validation only
  and no longer reports installation success. The deterministic builder
  enforces WorkBuddy's documented 3 MB maximum using a conservative
  3,000,000-byte ceiling; its compact package omits canonical/vector artifacts
  and fails those unsupported routes closed.
- Pinned the Nomic embedding model to a verified immutable revision and weight
  digest, disabled model-repository remote code, and raised the supported
  Sentence Transformers / Transformers dependency ranges to their current
  major versions.

### Fixed

- Made School inventory discovery work on read-only skill mounts by reading
  verified manifest counts or deterministically deriving legacy v2 counts
  without opening Chroma for writes.
- Made POSIX generation readers open pre-initialized external lock files with
  read-only descriptors, so Codex read-only sandboxes can acquire shared
  leases. Lock-infrastructure errors are now reported separately from genuine
  lock contention. WorkBuddy's immutable compact package now also binds every
  runtime knowledge input by size and SHA-256, and only that verified package
  may fall back when a sandbox forbids first-run lock-file initialization.
- Closed an index-upgrade path that could report success without checking a
  legacy-only index against changed canonical cards. Standalone install/update
  now also keeps its fail-closed generation marker until both populated v2
  collections and a valid SQLite database have been verified; a missing marker
  is rejected instead of being treated as an already-completed generation.
- Made `kb_doctor.py` platform-neutral: it validates the active copy's
  frontmatter, lowercase slug, and optional expected directory instead of
  assuming the Claude Code home layout.
- Corrected uninstall CLI and documentation semantics: standard uninstall
  removes the entire self-contained platform target, while `--full` is now
  explicitly documented as a deprecated compatibility no-op.
- Made card projection and Chroma promotion one durable transaction with an
  integrity-checked journal, deterministic crash recovery, and fail-fast
  cross-process generation leases. Readers now hold one generation lease from
  scope resolution through result serialization, so they cannot mix old index
  data with newly promoted or recovering cards.
- Made standalone install/update a synchronized atomic mirror: guarded target
  validation rejects broad, overlapping, symlinked, or unrelated directories;
  upstream deletions are reflected while runtime/user-owned data is preserved;
  interrupted switches recover from a verified journal. Install/update,
  uninstall, indexing, retrieval, and WorkBuddy export now coordinate on the
  same external knowledge-base lock.
- Made malformed, unreadable, non-object, and empty v2 knowledge cards fail
  closed instead of being silently skipped. Read-only retrieval now uses the
  existing Chroma tenant/database without creating state, keeps SQLite query
  temporary storage in memory for immutable sandboxes, and retains an explicit
  lexical fallback for packages that intentionally omit an index.
- Prevented WorkBuddy compact export from following symlinked registries,
  aliases, card directories, card files, composition inputs, or output targets;
  unfinished knowledge generations and oversized builds also leave any existing
  output untouched.
- Restricted the legacy card-School-only projection exception to its actual
  ChanLun import shape, preventing incomplete ICT/SMC provenance from entering
  a strict School scope.
- Duplicate canonical identities across different card files now fail closed
  instead of silently merging content under the first file's provenance.
- Installation and uninstall examples now clone into unique `mktemp`/GUID
  directories and only remove the captured path, avoiding fixed temporary
  directory names that could collide with pre-existing data.

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
