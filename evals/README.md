# Retrieval evaluation

`retrieval_benchmark_v1.jsonl` is the tracked M1 retrieval benchmark. Each
line is one independently auditable query with:

- an immutable case id and benchmark version;
- a retrieval layer and hard School/source/type scope;
- exact relevant record and canonical ids;
- a snapshot of the target's current KB metadata and parent file;
- an explicit truth source for the query text.

Version 1 contains 180 cases: 160 positive queries (60 canonical, 60 School,
40 atomic evidence) and 20 fail-closed scope cases. It covers all 15
retrievable School labels, all 12 source collections, English, Chinese and
mixed-language queries, concepts and cases, and every content type emitted by
the evidence builder.

Alias truth comes only from the checked-in `knowledge_base/term_aliases.json`.
The evaluator does not invent, translate, or expand synonyms. Evidence-text
queries are excerpts of the referenced `source_evidence_v2` content. Natural
queries use the target card's exact term inside a fixed question template, so
they remain traceable rather than becoming unreviewable free-form labels.

This is a deterministic regression benchmark, not a substitute for a
human-judged relevance set. It catches ID, scope, provenance, ranking, and
latency regressions reproducibly; future versions can add independently
authored paraphrases and graded relevance judgments.

All positive queries are derived from their target KB records and are supplied
with a hard School/type scope (evidence cases also have a source scope).
Consequently, this version measures deterministic in-scope retrieval
regressions; it does **not** measure generalization to independently written,
unscoped real-world questions.

Before opening Chroma or loading an embedding model, the CLI rebuilds the v2
records in memory and rejects stale ids, changed metadata, invalid aliases,
positive targets outside their declared scope, and negative scopes that no
longer have the stated fail-closed property.

Validate the dataset without an index or model:

```bash
python3 scripts/evaluate_retrieval.py --validate-only
```

Run a full baseline:

```bash
python3 scripts/evaluate_retrieval.py \
  --search-mode auto \
  --summary-only \
  --output /tmp/openmobius-retrieval-eval.json
```

`baseline_v1.json` is the checked-in `auto` result for the release corpus and
schema-3 native index. Generate comparison runs under `/tmp`; replace the
tracked baseline only when the benchmark, corpus, index profile, or retrieval
behavior changes intentionally and the new result has been reviewed.

`auto`, `hybrid`, `semantic`, and `lexical` modes are supported by the current
Retriever API. Lexical mode does not load an embedding model. On an older API,
the adapter permits only modes it can represent honestly and reports a clear
adapter error for unsupported hybrid/lexical requests.

For vector modes, canonical queries always use the local Nomic query embedder
that matches the bundled canonical vectors. `--embedder` selects the query
provider for the independently embedded School/evidence collections. Before
loading a model, the evaluator checks each selected layer's manifest model,
pinned revision, and dimension and fails with a rebuild/provider hint on
mismatch. A single local
model instance is shared when canonical and v2 layers use the same provider.

The report includes record-level Recall@1/5, MRR, hard-scope purity, exact
source purity, fail-closed rejection rates by expected category,
duplicate-canonical ratio, query latency, per-layer/language/query-kind/type
breakdowns, dataset SHA-256, and
index manifest provenance, including each collection's embedding strategy,
model, revision, and dimension, plus the canonical/v2 embedding input
profiles. Fail-closed rates
are grouped by the benchmark's **expected scope category**; they assert that a
`RetrievalScopeError` stopped the query, not that free-form exception text was
classified as that category. Evidence recall is scored against the exact atomic
record id; with `native_document` embeddings, this directly evaluates whether
the requested source statement—not merely its canonical parent—was returned.
Latency is machine-, cache-, and storage-dependent; compare it only when the
runtime, hardware, embedding model, and warm/cold-cache procedure are held
constant.
