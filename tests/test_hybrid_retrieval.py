import io
import json
import sys
import tempfile
import unittest
from array import array
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _lib.retriever import (  # noqa: E402
    RetrievedCard,
    Retriever,
    bm25_rank,
    lexical_scope_cache_key,
    resolve_search_mode,
    tokenize_for_lexical_search,
)
import kb_retrieve  # noqa: E402


class _Vector:
    def tolist(self):
        return [0.0, 1.0]


class _Embedder:
    def embed_query(self, query):
        return _Vector()


class _HybridCollection:
    def __init__(self):
        self.query_calls = []
        self.get_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {
            "ids": [["semantic-a", "shared-b"]],
            "metadatas": [[
                {"type": "concept", "term": "A", "school": "ICT", "canonical_id": "a"},
                {"type": "concept", "term": "B", "school": "ICT", "canonical_id": "b"},
            ]],
            "documents": [["unrelated semantic result", "fair value gap setup"]],
            "distances": [[0.1, 0.2]],
        }

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return {
            "ids": ["shared-b", "lexical-c"],
            "metadatas": [
                {"type": "concept", "term": "B", "school": "ICT", "canonical_id": "b"},
                {"type": "concept", "term": "C", "school": "ICT", "canonical_id": "c"},
            ],
            "documents": ["fair value gap fair value gap", "fair value concept"],
        }


def _metadata_matches(metadata, where):
    if where is None:
        return True
    if "$and" in where:
        return all(_metadata_matches(metadata, item) for item in where["$and"])
    if "$or" in where:
        return any(_metadata_matches(metadata, item) for item in where["$or"])
    field, expected = next(iter(where.items()))
    actual = metadata.get(field)
    if not isinstance(expected, dict):
        return actual == expected
    if "$in" in expected:
        return actual in expected["$in"]
    if "$nin" in expected:
        return actual not in expected["$nin"]
    if "$ne" in expected:
        return actual != expected["$ne"]
    raise AssertionError(f"unsupported fake where: {where}")


class _CorpusCollection:
    """Small Chroma-shaped collection with reversed id hydration."""

    def __init__(self, records):
        self.records = records
        self.get_calls = []
        self.query_calls = []

    def _filtered(self, where):
        return [
            record for record in self.records
            if _metadata_matches(record["metadata"], where)
        ]

    @staticmethod
    def _get_payload(records):
        return {
            "ids": [record["id"] for record in records],
            "metadatas": [record["metadata"] for record in records],
            "documents": [record["document"] for record in records],
        }

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        records = self._filtered(kwargs.get("where"))
        if kwargs.get("ids") is not None:
            requested = set(kwargs["ids"])
            # Chroma does not promise to preserve requested-id order.
            records = list(reversed([
                record for record in records if record["id"] in requested
            ]))
        else:
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit")
            records = records[offset : None if limit is None else offset + limit]
        return self._get_payload(records)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        records = self._filtered(kwargs.get("where"))[: kwargs["n_results"]]
        payload = self._get_payload(records)
        return {
            "ids": [payload["ids"]],
            "metadatas": [payload["metadatas"]],
            "documents": [payload["documents"]],
            "distances": [[
                record.get("distance", index / 100.0)
                for index, record in enumerate(records)
            ]],
        }

    def count(self):
        return len(self.records)


def _record(record_id, canonical_id, *, school="ICT", document="target"):
    return {
        "id": record_id,
        "metadata": {
            "type": "concept",
            "term": record_id,
            "school": school,
            "canonical_id": canonical_id,
        },
        "document": document,
    }


class LexicalUtilitiesTests(unittest.TestCase):
    def test_mixed_language_tokenizer_emits_words_and_cjk_bigrams(self):
        tokens = tokenize_for_lexical_search("ICT 的缠论结构 FVG")
        self.assertIn("ict", tokens)
        self.assertIn("fvg", tokens)
        self.assertIn("缠论", tokens)

    def test_bm25_omits_zero_overlap_and_ranks_repetition(self):
        ranked = bm25_rank(
            "fair value gap",
            ["other topic", "fair value", "fair value gap fair value gap"],
        )
        self.assertEqual([index for index, _ in ranked], [2, 1])

    def test_compounds_and_dotted_acronyms_share_lexical_tokens(self):
        compound = tokenize_for_lexical_search("order-block order_block")
        self.assertIn("order-block", compound)
        self.assertIn("order", compound)
        self.assertIn("block", compound)
        self.assertEqual(tokenize_for_lexical_search("F.V.G."), ["fvg"])
        self.assertNotIn("153", tokenize_for_lexical_search("version 1.5.3"))
        self.assertTrue(bm25_rank("order-block", ["order block setup"]))
        self.assertTrue(bm25_rank("F.V.G.", ["FVG imbalance"]))

    def test_equivalent_where_expressions_have_one_cache_key(self):
        first = {
            "$and": [
                {"school": {"$in": ["SMC", "ICT"]}},
                {"type": "concept"},
            ]
        }
        second = {
            "$and": [
                {"type": "concept"},
                {"school": {"$in": ["ICT", "SMC"]}},
            ]
        }
        self.assertEqual(
            lexical_scope_cache_key(first), lexical_scope_cache_key(second)
        )

    def test_auto_mode_preserves_canonical_and_enables_v2_hybrid(self):
        self.assertEqual(resolve_search_mode("auto", "canonical"), "semantic")
        self.assertEqual(resolve_search_mode("auto", "school"), "hybrid")
        self.assertEqual(resolve_search_mode("auto", "evidence"), "hybrid")

    def test_lexical_score_is_appended_after_existing_positional_fields(self):
        card = RetrievedCard(
            "id", "concept", "term", "ICT", "path", "document", 0.1, {},
            "hybrid", 0.02, 3, 4,
        )
        self.assertEqual(card.semantic_rank, 3)
        self.assertEqual(card.lexical_rank, 4)
        self.assertEqual(card.lexical_score, 0.0)


class HybridSearchTests(unittest.TestCase):
    def test_runtime_embedding_revision_must_match_index_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            manifest = {
                "manifest_version": 2,
                "embedding_models": {
                    "school_knowledge_v2": "nomic-ai/nomic-embed-text-v1.5",
                },
                "embedding_revisions": {
                    "school_knowledge_v2": "wrong-revision",
                },
                "embedding_dimensions": {"school_knowledge_v2": 768},
            }
            (index_dir / "index_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            retriever = Retriever.__new__(Retriever)
            retriever.kb_dir = kb_dir
            retriever.collection_name = "school_knowledge_v2"
            retriever.embedder = type(
                "PinnedEmbedder",
                (),
                {
                    "model_name": "nomic-ai/nomic-embed-text-v1.5",
                    "model_revision": (
                        "e9b6763023c676ca8431644204f50c2b100d9aab"
                    ),
                    "dim": 768,
                },
            )()

            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                retriever._validate_query_embedding_identity()

            manifest["embedding_revisions"]["school_knowledge_v2"] = (
                retriever.embedder.model_revision
            )
            (index_dir / "index_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            retriever._validate_query_embedding_identity()

    def test_hybrid_uses_same_hard_filter_for_vector_and_lexical_candidates(self):
        collection = _HybridCollection()
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "school"
        retriever.embedder = _Embedder()
        retriever.collection = collection
        retriever._alias_cache = {}

        cards = retriever.search(
            "fair value gap",
            top_k=3,
            filter_schools=["ICT"],
            search_mode="hybrid",
            exact_match=False,
        )

        self.assertEqual(cards[0].card_id, "shared-b")
        self.assertEqual(cards[0].match_kind, "hybrid")
        self.assertEqual(cards[0].semantic_rank, 2)
        self.assertEqual(cards[0].lexical_rank, 1)
        by_id = {card.card_id: card for card in cards}
        self.assertEqual(by_id["semantic-a"].match_kind, "semantic")
        self.assertIsNotNone(by_id["semantic-a"].distance)
        self.assertEqual(by_id["lexical-c"].match_kind, "lexical")
        self.assertIsNone(by_id["lexical-c"].distance)
        self.assertGreater(by_id["lexical-c"].lexical_score, 0)
        self.assertGreater(by_id["shared-b"].lexical_score, 0)
        self.assertGreater(by_id["shared-b"].fusion_score, 0)
        self.assertEqual(collection.query_calls[0]["where"], {"school": "ICT"})
        self.assertTrue(collection.get_calls)
        self.assertTrue(all(
            call["where"] == {"school": "ICT"}
            for call in collection.get_calls
        ))

    def test_all_modes_refill_after_diversity_removes_initial_candidates(self):
        records = [
            _record(f"a-{index:02d}", "duplicate") for index in range(20)
        ] + [
            _record(f"z-{index:02d}", f"unique-{index}") for index in range(5)
        ]

        for mode in ("semantic", "lexical", "hybrid"):
            with self.subTest(mode=mode):
                collection = _CorpusCollection(records)
                retriever = Retriever.__new__(Retriever)
                retriever.layer = "school"
                retriever.embedder = None if mode == "lexical" else _Embedder()
                retriever.collection = collection
                retriever._alias_cache = {}

                cards = retriever.search(
                    "target", top_k=5, search_mode=mode, exact_match=False
                )

                self.assertEqual(len(cards), 5)
                self.assertEqual(
                    sum(
                        card.metadata["canonical_id"] == "duplicate"
                        for card in cards
                    ),
                    2,
                )
                if mode in ("semantic", "hybrid"):
                    self.assertEqual(
                        [call["n_results"] for call in collection.query_calls],
                        [20, 25],
                    )
                if mode in ("lexical", "hybrid"):
                    hydrated_sizes = [
                        len(call["ids"])
                        for call in collection.get_calls
                        if call.get("ids") is not None
                    ]
                    self.assertEqual(hydrated_sizes, [20, 25])

    def test_compact_posting_cache_is_bounded_and_lru(self):
        records = [
            _record("ict-1", "ict-1", school="ICT"),
            _record("ict-2", "ict-2", school="ICT"),
            _record("smc-1", "smc-1", school="SMC"),
            _record("smc-2", "smc-2", school="SMC"),
        ]
        collection = _CorpusCollection(records)
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "school"
        retriever.embedder = None
        retriever.collection = collection
        retriever._alias_cache = {}
        retriever.LEXICAL_CACHE_RECORD_BUDGET = 3
        retriever.LEXICAL_CACHE_MAX_SCOPES = 2

        for school in ("ICT", "SMC", "ICT"):
            retriever.search(
                "target", top_k=1, filter_schools=[school],
                search_mode="lexical", exact_match=False,
            )

        build_calls = [
            call for call in collection.get_calls
            if call.get("ids") is None
        ]
        self.assertEqual(len(build_calls), 3)
        self.assertLessEqual(retriever._lexical_cache_records, 3)
        self.assertEqual(len(retriever._lexical_cache), 1)
        cached = next(iter(retriever._lexical_cache.values()))
        self.assertFalse(hasattr(cached, "documents"))
        self.assertFalse(hasattr(cached, "metadatas"))
        self.assertIsInstance(cached.document_lengths, array)
        self.assertTrue(all(
            isinstance(posting, array) for posting in cached.postings.values()
        ))

    def test_posting_bm25_matches_public_reference_scores(self):
        documents = ["other topic", "fair value", "fair value gap fair value gap"]
        collection = _CorpusCollection([
            _record(str(index), str(index), document=document)
            for index, document in enumerate(documents)
        ])
        retriever = Retriever.__new__(Retriever)
        retriever.collection = collection
        lexical_index = retriever._lexical_scope_index(None)

        expected = bm25_rank("fair value gap", documents)
        actual = retriever._rank_lexical_scope("fair value gap", lexical_index)

        self.assertEqual(
            [index for index, _ in actual], [index for index, _ in expected]
        )
        for (_, actual_score), (_, expected_score) in zip(actual, expected):
            self.assertAlmostEqual(actual_score, expected_score)

    def test_cache_hit_updates_lru_eviction_order(self):
        collection = _CorpusCollection([
            _record("ict", "ict", school="ICT"),
            _record("smc", "smc", school="SMC"),
            _record("wyckoff", "wyckoff", school="Wyckoff"),
        ])
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "school"
        retriever.embedder = None
        retriever.collection = collection
        retriever._alias_cache = {}
        retriever.LEXICAL_CACHE_RECORD_BUDGET = 2
        retriever.LEXICAL_CACHE_MAX_SCOPES = 2

        for school in ("ICT", "SMC", "ICT", "Wyckoff", "SMC"):
            retriever.search(
                "target", top_k=1, filter_schools=[school],
                search_mode="lexical", exact_match=False,
                max_per_canonical=0,
            )

        build_calls = [
            call for call in collection.get_calls
            if call.get("ids") is None
        ]
        # ICT was touched before inserting Wyckoff, so SMC—not ICT—was evicted;
        # the final SMC request therefore rebuilds its posting index.
        self.assertEqual(len(build_calls), 4)
        self.assertEqual(retriever._lexical_cache_records, 2)

    def test_equivalent_scope_reuses_cache_and_pages_keep_hard_filter(self):
        records = [
            _record("ict-1", "ict-1", school="ICT"),
            _record("smc-1", "smc-1", school="SMC"),
            _record("ict-2", "ict-2", school="ICT"),
            _record("smc-2", "smc-2", school="SMC"),
        ]
        collection = _CorpusCollection(records)
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "school"
        retriever.embedder = None
        retriever.collection = collection
        retriever._alias_cache = {}
        retriever.LEXICAL_INDEX_PAGE_SIZE = 2

        for schools in (["SMC", "ICT"], ["ICT", "SMC"]):
            cards = retriever.search(
                "target", top_k=2, filter_schools=schools,
                search_mode="lexical", exact_match=False,
                max_per_canonical=0,
            )
            self.assertEqual([card.card_id for card in cards], ["ict-1", "ict-2"])

        build_calls = [
            call for call in collection.get_calls
            if call.get("ids") is None
        ]
        # Two full pages and one empty terminator, all only on the first query.
        self.assertEqual(len(build_calls), 3)
        self.assertTrue(all(call.get("where") for call in build_calls))
        self.assertEqual(
            {tuple(call["where"]["school"]["$in"]) for call in build_calls},
            {("SMC", "ICT")},
        )

    def test_lexical_exact_match_needs_no_embedder_and_stays_first(self):
        class Collection:
            def __init__(self):
                self.calls = 0

            def get(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "ids": ["other"],
                        "metadatas": [{
                            "type": "concept", "term": "Other", "school": "ICT",
                            "canonical_id": "other",
                        }],
                        "documents": ["fvg continuation"],
                    }
                return {
                    "ids": ["fair_value_gap::ict"],
                    "metadatas": [{
                        "type": "concept", "term": "Fair Value Gap", "school": "ICT",
                        "canonical_id": "fair_value_gap",
                    }],
                    "documents": ["imbalance definition"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "term_aliases.json").write_text(json.dumps({
                "mappings": [{
                    "canonical": "Fair Value Gap",
                    "card_id": "fair_value_gap",
                    "aliases": ["FVG"],
                }]
            }), encoding="utf-8")
            retriever = Retriever.__new__(Retriever)
            retriever.layer = "school"
            retriever.kb_dir = Path(tmp)
            retriever.embedder = None
            retriever.collection = Collection()
            retriever._alias_cache = None

            cards = retriever.search(
                "FVG", top_k=2, search_mode="lexical"
            )

        self.assertEqual(cards[0].card_id, "fair_value_gap::ict")
        self.assertEqual(cards[0].match_kind, "exact")

    def test_lexical_exact_gets_all_then_sorts_before_final_limit(self):
        class Collection:
            def __init__(self):
                self.get_calls = []

            @staticmethod
            def payload(ids, term="Other", canonical_id="other"):
                return {
                    "ids": ids,
                    "metadatas": [{
                        "type": "concept", "term": term, "school": "ICT",
                        "canonical_id": canonical_id,
                    } for _ in ids],
                    "documents": ["fvg continuation" for _ in ids],
                }

            def get(self, **kwargs):
                self.get_calls.append(kwargs)
                if kwargs.get("ids") is not None:
                    return self.payload(list(reversed(kwargs["ids"])))
                if kwargs.get("where") is not None:
                    return self.payload(
                        ["exact-e", "exact-d", "exact-c", "exact-b", "exact-a"],
                        term="Fair Value Gap",
                        canonical_id="fair_value_gap",
                    )
                return self.payload(["other"])

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "term_aliases.json").write_text(json.dumps({
                "mappings": [{
                    "canonical": "Fair Value Gap",
                    "card_id": "fair_value_gap",
                    "aliases": ["FVG"],
                }]
            }), encoding="utf-8")
            collection = Collection()
            retriever = Retriever.__new__(Retriever)
            retriever.layer = "school"
            retriever.kb_dir = Path(tmp)
            retriever.embedder = None
            retriever.collection = collection
            retriever._alias_cache = None

            cards = retriever.search(
                "FVG", top_k=3, search_mode="lexical",
                max_per_canonical=0,
            )

        self.assertEqual(
            [card.card_id for card in cards],
            ["exact-a", "exact-b", "exact-c"],
        )
        exact_calls = [
            call for call in collection.get_calls
            if call.get("where") is not None and call.get("ids") is None
        ]
        self.assertEqual(len(exact_calls), 1)
        self.assertNotIn("limit", exact_calls[0])

    def test_v2_default_caps_duplicate_canonical_parents(self):
        cards = [
            RetrievedCard(
                card_id=f"e-{index}", card_type="definition", term="FVG",
                school="ICT", file_path="", document="", distance=0.1,
                metadata={"canonical_id": "fvg"},
            )
            for index in range(4)
        ]
        cards.append(RetrievedCard(
            card_id="e-other", card_type="definition", term="OB",
            school="ICT", file_path="", document="", distance=0.2,
            metadata={"canonical_id": "ob"},
        ))

        selected = Retriever._limit_and_diversify(cards, 5, 2)

        self.assertEqual([card.card_id for card in selected], [
            "e-0", "e-1", "e-other",
        ])


class HybridCliTests(unittest.TestCase):
    def test_lexical_cli_does_not_load_embedding_model(self):
        captured = {}

        class FakeRetriever:
            def __init__(self, kb_dir, embedder, *, layer):
                self.embedder = embedder

            def resolve_scope(self, **kwargs):
                return {
                    "schools": ["ICT"],
                    "sources": [],
                    "excluded_schools": [],
                    "type": None,
                    "where": {"school": "ICT"},
                }

            def search(self, **kwargs):
                captured.update(kwargs)
                return []

        with tempfile.TemporaryDirectory() as tmp, (
            patch.object(kb_retrieve, "Retriever", FakeRetriever)
        ), patch.object(kb_retrieve, "get_embedder") as get_embedder, (
            redirect_stdout(io.StringIO())
        ):
            result = kb_retrieve.main([
                "FVG", "--kb", tmp, "--layer", "school",
                "--schools", "ICT", "--search-mode", "lexical",
            ])

        self.assertEqual(result, 0)
        get_embedder.assert_not_called()
        self.assertEqual(captured["search_mode"], "lexical")

    def test_explain_scope_reports_resolved_auto_mode_and_diversity(self):
        class FakeRetriever:
            def __init__(self, kb_dir, embedder, *, layer):
                pass

            def resolve_scope(self, **kwargs):
                return {
                    "schools": ["ICT"],
                    "sources": [],
                    "excluded_schools": [],
                    "type": None,
                    "where": {"school": "ICT"},
                }

        with tempfile.TemporaryDirectory() as tmp, (
            patch.object(kb_retrieve, "Retriever", FakeRetriever)
        ), redirect_stdout(io.StringIO()) as stdout:
            result = kb_retrieve.main([
                "--kb", tmp, "--layer", "school", "--schools", "ICT",
                "--explain-scope", "--format", "json",
            ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["search_mode"], "hybrid")
        self.assertEqual(payload["max_per_canonical"], 2)

    def test_lexical_scores_and_missing_distance_have_unambiguous_output(self):
        card = RetrievedCard(
            card_id="lexical", card_type="concept", term="FVG",
            school="ICT", file_path="concepts/fvg.json", document="FVG",
            distance=None, metadata={"canonical_id": "fvg"},
            match_kind="lexical", lexical_score=3.25,
        )

        class FakeRetriever:
            def __init__(self, kb_dir, embedder, *, layer):
                pass

            def resolve_scope(self, **kwargs):
                return {
                    "schools": [], "sources": [], "excluded_schools": [],
                    "type": None, "where": None,
                }

            def search(self, **kwargs):
                return [card]

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            kb_retrieve, "Retriever", FakeRetriever
        ):
            with redirect_stdout(io.StringIO()) as stdout:
                result = kb_retrieve.main([
                    "FVG", "--kb", tmp, "--layer", "school",
                    "--all-schools", "--search-mode", "lexical",
                    "--format", "json",
                ])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertIsNone(payload[0]["distance"])
            self.assertEqual(payload[0]["retrieval"]["fusion_score"], 0.0)
            self.assertEqual(payload[0]["retrieval"]["lexical_score"], 3.25)

            with redirect_stdout(io.StringIO()) as stdout:
                result = kb_retrieve.main([
                    "FVG", "--kb", tmp, "--layer", "school",
                    "--all-schools", "--search-mode", "lexical",
                    "--format", "compact",
                ])
            self.assertEqual(result, 0)
            self.assertIn("bm25=3.25000", stdout.getvalue())
            self.assertIn("distance=n/a", stdout.getvalue())
            self.assertNotIn("rrf=", stdout.getvalue())

            with redirect_stdout(io.StringIO()) as stdout:
                result = kb_retrieve.main([
                    "FVG", "--kb", tmp, "--layer", "school",
                    "--all-schools", "--search-mode", "lexical",
                    "--format", "markdown",
                ])
            self.assertEqual(result, 0)
            self.assertIn("distance=n/a", stdout.getvalue())
            self.assertIn("bm25=3.25000", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
