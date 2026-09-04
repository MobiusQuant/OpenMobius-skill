# knowledge_base/

Structured knowledge cards used by the skill's retrieval-augmented
answers. Two card types:

```
knowledge_base/
├── concepts/          # 726 trading-concept cards (JSON)
├── cases/             # 1282 case-study cards (JSON)
├── index.json         # card catalog (id + canonical term / title)
├── term_aliases.json  # canonical-term → aliases map (incl. legacy terms)
├── schools.json       # canonical School registry, aliases, capabilities
├── embedding_seed_v2/ # verified native School/evidence vectors (16 shards)
└── schemas/           # School projection + source-evidence v2 schemas
```

## Contents

Each card is a JSON document with a schema-driven structure. Concept
cards carry: identification rules, trading implications, common
mistakes, related concepts, and per-source definitions
(`definition_per_source`). Case cards carry: market context, key
observation, analysis steps, lessons, and source video/time-range
provenance.

Across concept and case cards, coverage spans 14 top-level School/category
labels: ICT, SMC, Price Action, Indicator Based, ChanLun (缠论), Risk
Management, General, Order Flow, Volume Analysis, Elliott Wave, Wyckoff,
The Strat, On-chain, and Market Structure. They are distilled from 300+
teaching videos and live lessons across 12 curated source collections.

`schools.json` registers 15 retrievable labels: all 14 top-level labels plus
the evidence-only `Scalping` category found in source-card provenance. It also
distinguishes analytical lenses from general knowledge categories and records
which lenses have a native market analyzer.

## Provenance & quality

Cards are produced by a cross-collection merge pipeline:
per-collection extraction → LLM term normalization → cross-collection
concept fusion (with sampled model audit) → content-level case
deduplication (same-video time-range overlap, with an asset-consistency
guard). Terminology from earlier releases is preserved in
`term_aliases.json`, so older term spellings remain retrievable.

The cards are **original structured summaries** authored by this project
from analysis of publicly available multi-School trading education. They are
not verbatim copies of source material;
each card paraphrases and re-structures trading concepts into a schema
useful for retrieval-augmented generation.

The build derives two conservative v2 views without rewriting the canonical
cards:

- `school_knowledge_v2` contains 2,144 School projections. Same-School
  synthesized content is retained; on cross-School cards, only explicitly
  mapped per-source definitions are projected.
- `source_evidence_v2` contains 18,645 atomic statements. Each record has one
  exact `school`, one exact source collection, a content type, parent card,
  JSON pointer, and source-material identifiers when present.

No fuzzy classifier is used for provenance. Of 1,226 per-source definitions,
1,164 (94.9429%) map exactly. All 12,579 case content items map exactly.
Ambiguous project/School mappings (10), unmatched source keys (52), and
cross-School fused definitions/rules (120/2,374) are skipped and reported
rather than guessed. The 58 ChanLun concept cards are available in the School
layer, but intentionally have no concept-level source evidence because their
legacy source cards lack an exact collection/School pair; ChanLun cases retain
their explicit source evidence.

## Purpose

For research and educational use as a grounding source for AI trading
assistants — to reduce hallucination by retrieving structured,
schema-validated knowledge at query time instead of letting the LLM
guess.

## Build or upgrade

The vector index lives in `_index/` (gitignored — built by the
installer; not shipped in the source repository).

Use the command matching the index state:

```bash
cd <skill-dir>

# First build (no existing _index):
.venv/bin/python scripts/build_index.py

# Safe check/upgrade of an existing index:
.venv/bin/python scripts/build_index.py --upgrade

# Explicit staged full rebuild:
.venv/bin/python scripts/build_index.py --force
```

The index contains `knowledge_base` (legacy canonical compatibility),
`school_knowledge_v2`, and `source_evidence_v2`, plus a versioned
`index_manifest.json`. Canonical cards keep their bundled vectors. Every v2
record embeds its own scoped document and stores the result in the gitignored
`_embedding_cache/`; subsequent builds compute only content/model cache misses.
Release checkouts also ship a verified float32 seed in `embedding_seed_v2/`,
split into 16 SHA-256-prefix NPZ shards. Builds resolve each exact document
hash from the persistent SQLite cache first, then the read-only release seed,
and load the model only for remaining misses. Seed identity is isolated by
model and input profile; a stale corpus fingerprint still permits safe reuse
of unchanged exact document hashes.
The release local-document profile uses `max_seq_length=512`; that versioned
profile is part of both the cache key and index manifest, so older 8192-token
cache entries cannot be reused accidentally.
A release-matched first build normally resolves all bundled v2 documents from
the seed without loading the model for indexing. New or locally changed
documents load the model only for their cache/seed misses.
`--v2-embedding-strategy inherit` is retained only as an explicit
emergency/testing fallback.

Maintainers can publish a new seed only after the current native SQLite cache
is complete. The exporter refuses partial caches and atomically promotes a
fully re-verified 16-shard staging directory:

```bash
.venv/bin/python scripts/export_v2_embedding_seed.py
```

v2 retrieval defaults to hard-filtered hybrid search (BM25 + semantic RRF).
Use `--search-mode lexical` for model-free diagnostics, or
`--search-mode semantic` for the vector-only baseline. Exact canonical terms
and aliases remain deterministically first in every mode.

Audit the deterministic projection, or optionally export JSONL artifacts to a
temporary directory:

```bash
.venv/bin/python scripts/build_knowledge_v2.py --json
.venv/bin/python scripts/build_knowledge_v2.py --output /tmp/openmobius-v2
```

Run the tracked retrieval benchmark after rebuilding the index:

```bash
.venv/bin/python scripts/evaluate_retrieval.py
```

## Attribution

If you believe a card contains material that should be removed,
attributed differently, or corrected, please open an issue on the
project repository.

See `../ATTRIBUTION.md` for the project's full third-party attribution.
