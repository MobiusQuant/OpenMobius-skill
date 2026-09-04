# Knowledge model v2

Version 2 adds two derived views without changing the legacy canonical cards:

- `school_knowledge_v2` contains one projection per `(type, canonical_id,
  school)` and is the layer for exact School filtering.
- `source_evidence_v2` contains atomic statements with one exact `school` and
  one exact `source`. It is the only layer on which source filtering is valid.

The source files remain `concepts/*.json` and `cases/*.json`. Derived records
carry that parent path in `file_path`, but each School/evidence record embeds
its own scoped `search_text`. Release builds resolve those native vectors from
the verified `embedding_seed_v2/` first and compute only exact content-hash
misses; parent-vector inheritance is an explicit emergency/testing fallback,
not the published index policy.

## Conservative attribution policy

The builder does not classify text or infer provenance:

1. A `definition_per_source` item becomes exact evidence only when its key
   exactly matches source-card metadata. Supported exact labels are the
   project name and project combined with a declared School, card ID, or
   source canonical term. A project name shared by several Schools is
   ambiguous and is skipped unless the key disambiguates it.
2. A concept card's top-level definition/rules enter a School projection only
   when all explicit source Schools agree with the card School. The legacy
   ChanLun shape, whose source cards only contain video metadata, may enter the
   School layer based on its explicit card School but never becomes source
   evidence.
3. A top-level concept statement becomes source evidence only when all source
   metadata is complete and names exactly one source collection.
4. Case statements require both an explicit School and `project_origin`.
5. Fused rules, missing provenance, ambiguous project/School mappings, and
   unknown labels fail closed and are counted in the build manifest.

Here, `exact` means the statement can be tied to one source *collection* and
one School using fields already present in the canonical card. It does not
reconstruct a missing timestamp or prove which one of several source cards
supports a synthesized rule. `source_material_ids` is therefore a candidate
set when a project-level definition or same-project synthesis references more
than one card. Segment-level provenance still requires the original material.

`schools.json` is JSON rather than YAML so the registry adds no runtime
dependency. It contains the 14 Schools/categories present as top-level card
labels plus `Scalping`, a legacy source-only label retained as
`availability=evidence_only` instead of being silently discarded.

## Build and inspect

Run an in-memory audit without changing the knowledge base:

```bash
python3 scripts/build_knowledge_v2.py
python3 scripts/build_knowledge_v2.py --json
```

Optional deterministic JSONL artifacts can be written outside the repository:

```bash
python3 scripts/build_knowledge_v2.py --output /tmp/openmobius-v2
```

The normal vector-index builder imports `build_v2_records(kb_dir)` directly;
JSONL export is not required. Published payload contracts are defined by
`school_projection_v2.schema.json` and `source_evidence_v2.schema.json`.
