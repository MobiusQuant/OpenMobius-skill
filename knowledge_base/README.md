# knowledge_base/

Structured knowledge cards used by the skill's retrieval-augmented
answers. Two card types:

```
knowledge_base/
├── concepts/          # 726 trading-concept cards (JSON)
├── cases/             # 1282 case-study cards (JSON)
├── index.json         # card catalog (id + canonical term / title)
└── term_aliases.json  # canonical-term → aliases map (incl. legacy terms)
```

## Contents

Each card is a JSON document with a schema-driven structure. Concept
cards carry: identification rules, trading implications, common
mistakes, related concepts, and per-source definitions
(`definition_per_source`). Case cards carry: market context, key
observation, analysis steps, lessons, and source video/time-range
provenance.

Coverage spans 13 schools — ICT, SMC, Price Action, Indicator-Based,
ChanLun (缠论), Risk Management, Order Flow, Volume Analysis, Wyckoff
and more — distilled from 300+ teaching videos and live lessons across
12 curated source collections.

## Provenance & quality

Cards are produced by a cross-collection merge pipeline:
per-collection extraction → LLM term normalization → cross-collection
concept fusion (with sampled model audit) → content-level case
deduplication (same-video time-range overlap, with an asset-consistency
guard). Terminology from earlier releases is preserved in
`term_aliases.json`, so older term spellings remain retrievable.

The cards are **original structured summaries** authored by this project
from analysis of publicly available educational content (online ICT/SMC
trading tutorials). They are not verbatim copies of source material;
each card paraphrases and re-structures trading concepts into a schema
useful for retrieval-augmented generation.

## Purpose

For research and educational use as a grounding source for AI trading
assistants — to reduce hallucination by retrieving structured,
schema-validated knowledge at query time instead of letting the LLM
guess.

## Build

The vector index lives in `_index/` (gitignored — built by the
installer; not shipped in the source repository).

To rebuild manually:

```bash
cd <skill-dir>
.venv/bin/python scripts/build_index.py
```

## Attribution

If you believe a card contains material that should be removed,
attributed differently, or corrected, please open an issue on the
project repository.

See `../ATTRIBUTION.md` for the project's full third-party attribution.
