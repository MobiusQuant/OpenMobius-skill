import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kb_retrieve  # noqa: E402
from _lib.retriever import (  # noqa: E402
    RetrievalScopeError,
    RetrievedCard,
    Retriever,
    build_where_filter,
)


def _matches(metadata, where):
    if where is None:
        return True
    if "$and" in where:
        return all(_matches(metadata, clause) for clause in where["$and"])
    if "$or" in where:
        return any(_matches(metadata, clause) for clause in where["$or"])
    field, expected = next(iter(where.items()))
    actual = metadata.get(field)
    if not isinstance(expected, dict):
        return actual == expected
    if "$in" in expected:
        return actual in expected["$in"]
    if "$ne" in expected:
        return actual != expected["$ne"]
    if "$nin" in expected:
        return actual not in expected["$nin"]
    raise AssertionError(f"unsupported test filter: {where}")


class _InventoryCollection:
    def __init__(self, records):
        self.records = records

    def get(self, where=None, limit=None, include=None):
        selected = [
            (record_id, metadata)
            for record_id, metadata in self.records
            if _matches(metadata, where)
        ]
        if limit is not None:
            selected = selected[:limit]
        return {
            "ids": [record_id for record_id, _ in selected],
            "metadatas": [metadata for _, metadata in selected],
        }


class V2FilterTests(unittest.TestCase):
    def test_school_source_exclusion_and_type_are_one_hard_filter(self):
        self.assertEqual(
            build_where_filter(
                filter_schools=["ICT", "SMC"],
                filter_sources=["Course A", "Course B"],
                exclude_schools=["General"],
                filter_type="concept",
            ),
            {
                "$and": [
                    {"school": {"$in": ["ICT", "SMC"]}},
                    {"school": {"$ne": "General"}},
                    {"source": {"$in": ["Course A", "Course B"]}},
                    {"type": "concept"},
                ]
            },
        )

    def test_include_exclude_overlap_fails_closed(self):
        with self.assertRaisesRegex(RetrievalScopeError, "同时包含和排除"):
            build_where_filter(
                filter_schools=["ICT", "SMC"],
                exclude_schools=["SMC"],
            )

    def test_source_filter_is_refused_on_canonical_layer(self):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "canonical"
        with self.assertRaisesRegex(RetrievalScopeError, "evidence"):
            retriever.search("query", filter_sources=["Teach-Wuyuan"])

    def test_empty_source_filter_fails_even_without_strict_scope(self):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "evidence"
        with self.assertRaisesRegex(RetrievalScopeError, "source selector 不能为空"):
            retriever.search("query", filter_sources=[])

    def test_strict_legacy_empty_school_filter_fails_closed(self):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "canonical"
        with self.assertRaisesRegex(RetrievalScopeError, "School selector 不能为空"):
            retriever.search("query", filter_school="   ", strict_scope=True)

    def test_strict_plural_empty_school_filter_fails_closed(self):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "canonical"
        with self.assertRaisesRegex(RetrievalScopeError, "School selector 不能为空"):
            retriever.search("query", filter_schools=[], strict_scope=True)

    def test_strict_empty_excluded_school_filter_fails_closed(self):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "canonical"
        with self.assertRaisesRegex(
            RetrievalScopeError, "exclude-school selector 不能为空"
        ):
            retriever.search("query", exclude_schools=[], strict_scope=True)

    def test_non_strict_legacy_blank_school_keeps_backward_compatibility(self):
        class CaptureCollection:
            def __init__(self):
                self.where = "unset"

            def query(self, **kwargs):
                self.where = kwargs["where"]
                return {
                    "ids": [[]],
                    "metadatas": [[]],
                    "documents": [[]],
                    "distances": [[]],
                }

        retriever = Retriever.__new__(Retriever)
        retriever.layer = "canonical"
        retriever.embedder = _Embedder()
        retriever.collection = CaptureCollection()
        retriever._alias_cache = {}

        retriever.search("query", filter_school="   ")

        self.assertIsNone(retriever.collection.where)

    def test_v2_get_full_card_never_reads_parent_canonical_file(self):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = "school"
        card = RetrievedCard(
            card_id="fvg::ict",
            card_type="concept",
            term="Fair Value Gap",
            school="ICT",
            file_path="concepts/fair_value_gap.json",
            document="scoped",
            distance=0.1,
            metadata={},
        )
        self.assertIsNone(retriever.get_full_card(card))

    def test_canonical_full_card_rejects_parent_path_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"secret": true}', encoding="utf-8")
            link = kb_dir / "escape.json"
            link.symlink_to(outside)

            retriever = Retriever.__new__(Retriever)
            retriever.layer = "canonical"
            retriever.kb_dir = kb_dir

            def card(file_path):
                return RetrievedCard(
                    card_id="x",
                    card_type="concept",
                    term="x",
                    school="ICT",
                    file_path=file_path,
                    document="",
                    distance=0.1,
                    metadata={},
                )

            with self.assertLogs("_lib.retriever", level="ERROR"):
                self.assertIsNone(retriever.get_full_card(card("../outside.json")))
            with self.assertLogs("_lib.retriever", level="ERROR"):
                self.assertIsNone(retriever.get_full_card(card("escape.json")))

    def test_canonical_full_card_still_reads_an_in_root_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            card_path = kb_dir / "concept.json"
            card_path.write_text('{"canonical_term": "FVG"}', encoding="utf-8")
            retriever = Retriever.__new__(Retriever)
            retriever.layer = "canonical"
            retriever.kb_dir = kb_dir
            card = RetrievedCard(
                card_id="fvg",
                card_type="concept",
                term="FVG",
                school="ICT",
                file_path="concept.json",
                document="",
                distance=0.1,
                metadata={},
            )

            payload = retriever.get_full_card(card)

        self.assertEqual(payload, {"canonical_term": "FVG"})

    def test_missing_v2_collection_hint_uses_upgrade_and_quotes_kb_path(self):
        class FakeClient:
            def get_collection(self, name):
                raise RuntimeError("missing")

        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "kb with spaces"
            index_dir = kb_dir / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_bytes(b"fixture")
            chromadb = SimpleNamespace(
                PersistentClient=lambda path: FakeClient()
            )
            with (
                patch.dict(sys.modules, {"chromadb": chromadb}),
                self.assertRaises(RuntimeError) as raised,
            ):
                Retriever(kb_dir, None, layer="school")

        message = str(raised.exception)
        self.assertIn("scripts/build_index.py", message)
        self.assertIn("--upgrade", message)
        self.assertIn("'" + str(kb_dir) + "'", message)


class ScopeValidationTests(unittest.TestCase):
    def make_retriever(self, layer="evidence"):
        retriever = Retriever.__new__(Retriever)
        retriever.layer = layer
        retriever.collection = _InventoryCollection([
            (
                "ict-a",
                {
                    "school": "ICT",
                    "source": "Course A",
                    "type": "concept",
                },
            ),
            (
                "smc-b",
                {
                    "school": "SMC",
                    "source": "Course B",
                    "type": "case",
                },
            ),
        ])
        retriever._metadata_cache = None
        return retriever

    def test_selector_case_is_resolved_to_collection_value(self):
        scope = self.make_retriever().resolve_scope(
            filter_schools=["ict"],
            filter_sources=["course a"],
            filter_type="concept",
        )
        self.assertEqual(scope["schools"], ["ICT"])
        self.assertEqual(scope["sources"], ["Course A"])

    def test_registry_alias_is_canonicalized_then_checked_in_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "schools.json").write_text(json.dumps({
                "schools": [{
                    "id": "chanlun",
                    "name": "缠论",
                    "aliases": ["ChanLun", "Chan Lun"],
                }]
            }), encoding="utf-8")
            retriever = Retriever.__new__(Retriever)
            retriever.layer = "school"
            retriever.kb_dir = Path(tmp)
            retriever.collection = _InventoryCollection([
                ("chan", {"school": "缠论", "type": "concept"}),
            ])
            retriever._metadata_cache = None
            retriever._school_registry_cache = None

            scope = retriever.resolve_scope(filter_schools=["Chan Lun"])

        self.assertEqual(scope["schools"], ["缠论"])

    def test_registry_alias_does_not_bypass_layer_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "schools.json").write_text(json.dumps({
                "schools": [{
                    "id": "chanlun",
                    "name": "缠论",
                    "aliases": ["ChanLun"],
                }]
            }), encoding="utf-8")
            retriever = self.make_retriever(layer="school")
            retriever.kb_dir = Path(tmp)
            retriever._school_registry_cache = None

            with self.assertRaisesRegex(RetrievalScopeError, "当前 layer 可用值"):
                retriever.resolve_scope(filter_schools=["ChanLun"])

    def test_unknown_school_fails_closed(self):
        with self.assertRaisesRegex(RetrievalScopeError, "未知 School"):
            self.make_retriever().resolve_scope(filter_schools=["Unknown"])

    def test_empty_explicit_source_fails_closed(self):
        with self.assertRaisesRegex(RetrievalScopeError, "source selector 不能为空"):
            self.make_retriever().resolve_scope(filter_sources=[])

    def test_valid_selectors_with_empty_intersection_fail_closed(self):
        with self.assertRaisesRegex(RetrievalScopeError, "硬过滤交集为空"):
            self.make_retriever().resolve_scope(
                filter_schools=["ICT"],
                filter_sources=["Course B"],
            )

    def test_source_selector_is_refused_on_school_layer(self):
        with self.assertRaisesRegex(RetrievalScopeError, "layer=evidence"):
            self.make_retriever(layer="school").resolve_scope(
                filter_sources=["Course A"]
            )


class _Vector:
    def tolist(self):
        return [0.0, 1.0]


class _Embedder:
    def embed_query(self, query):
        return _Vector()


class _ExactCollection:
    def __init__(self):
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return {
                "ids": [["semantic-card"]],
                "metadatas": [[{
                    "type": "concept",
                    "term": "FVG Trading Rules",
                    "school": "ICT",
                }]],
                "documents": [["semantic"]],
                "distances": [[0.1]],
            }
        return {
            "ids": [["fair_value_gap"]],
            "metadatas": [[{
                "type": "concept",
                "term": "Fair Value Gap",
                "school": "ICT",
                "card_id": "fair_value_gap",
            }]],
            "documents": [["exact"]],
            "distances": [[0.5]],
        }


class ExactPriorityTests(unittest.TestCase):
    def test_alias_exact_match_precedes_closer_semantic_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            alias_path = Path(tmp) / "term_aliases.json"
            alias_path.write_text(json.dumps({
                "mappings": [{
                    "canonical": "Fair Value Gap",
                    "card_id": "fair_value_gap",
                    "aliases": ["FVG"],
                }]
            }), encoding="utf-8")
            retriever = Retriever.__new__(Retriever)
            retriever.layer = "canonical"
            retriever.kb_dir = Path(tmp)
            retriever.embedder = _Embedder()
            retriever.collection = _ExactCollection()
            retriever._alias_cache = None

            cards = retriever.search("fvg", top_k=2)

        self.assertEqual([card.card_id for card in cards], [
            "fair_value_gap", "semantic-card",
        ])
        self.assertEqual(cards[0].match_kind, "exact")
        exact_where = retriever.collection.calls[1]["where"]
        self.assertIn("$or", exact_where)


class CliV2Tests(unittest.TestCase):
    @staticmethod
    def failing_query_retriever(error):
        class FakeRetriever:
            def __init__(self, kb_dir, embedder):
                self.embedder = embedder

            def resolve_scope(self, **kwargs):
                return {
                    "schools": [],
                    "sources": [],
                    "excluded_schools": [],
                    "type": None,
                    "where": None,
                }

            def search(self, **kwargs):
                raise error

        return FakeRetriever

    def test_embedder_failure_is_reported_without_default_traceback(self):
        class FakeRetriever:
            def __init__(self, kb_dir, embedder):
                self.embedder = embedder

            def resolve_scope(self, **kwargs):
                return {
                    "schools": [],
                    "sources": [],
                    "excluded_schools": [],
                    "type": None,
                    "where": None,
                }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                patch.object(
                    kb_retrieve,
                    "get_embedder",
                    side_effect=RuntimeError("missing credentials"),
                ),
                patch.object(kb_retrieve.log, "error") as log_error,
                patch.object(kb_retrieve.log, "exception") as log_exception,
            ):
                result = kb_retrieve.main(["query", "--kb", tmp])

        self.assertEqual(result, 1)
        log_error.assert_called_once()
        self.assertIn("missing credentials", str(log_error.call_args))
        log_exception.assert_not_called()

    def test_search_failure_is_reported_without_default_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    kb_retrieve,
                    "Retriever",
                    self.failing_query_retriever(RuntimeError("query failed")),
                ),
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                patch.object(kb_retrieve.log, "error") as log_error,
                patch.object(kb_retrieve.log, "exception") as log_exception,
            ):
                result = kb_retrieve.main(["query", "--kb", tmp])

        self.assertEqual(result, 1)
        log_error.assert_called_once()
        self.assertIn("query failed", str(log_error.call_args))
        log_exception.assert_not_called()

    def test_verbose_search_failure_keeps_diagnostic_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    kb_retrieve,
                    "Retriever",
                    self.failing_query_retriever(RuntimeError("query failed")),
                ),
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                patch.object(kb_retrieve.log, "error") as log_error,
                patch.object(kb_retrieve.log, "exception") as log_exception,
            ):
                result = kb_retrieve.main(["query", "--kb", tmp, "--verbose"])

        self.assertEqual(result, 1)
        log_exception.assert_called_once()
        log_error.assert_not_called()

    def test_explain_scope_does_not_load_embedding_model(self):
        class FakeRetriever:
            def __init__(self, kb_dir, embedder):
                self.embedder = embedder

            def resolve_scope(self, **kwargs):
                return {
                    "schools": ["ICT"],
                    "sources": [],
                    "excluded_schools": [],
                    "type": "concept",
                    "where": {"school": "ICT"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                patch.object(kb_retrieve, "get_embedder") as get_embedder,
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "--kb", tmp,
                    "--schools", "ICT",
                    "--type", "concept",
                    "--explain-scope",
                    "--format", "json",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["hard_filter"], {"school": "ICT"})
        get_embedder.assert_not_called()

    def test_list_schools_works_without_query(self):
        class FakeRetriever:
            def __init__(self, kb_dir, embedder, *, layer):
                self.layer = layer

            def list_schools(self):
                return {"ICT": 3, "SMC": 2}

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                patch.object(kb_retrieve, "get_embedder") as get_embedder,
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "--kb", tmp,
                    "--layer", "school",
                    "--list-schools",
                    "--format", "json",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["schools"][0]["name"], "ICT")
        self.assertEqual(payload["schools"][0]["count"], 3)
        get_embedder.assert_not_called()

    def test_list_schools_uses_manifest_without_opening_chroma(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            (kb_dir / "schools.json").write_text(
                json.dumps({
                    "registry_version": 1,
                    "default_profile": {
                        "id": "ict_smc",
                        "schools": ["ICT", "SMC"],
                    },
                    "schools": [
                        {
                            "id": "ict",
                            "name": "ICT",
                            "aliases": ["Inner Circle Trader"],
                            "kind": "analysis_lens",
                            "availability": "top_level",
                            "knowledge_qna": True,
                            "native_market_analyzer": "smc",
                        },
                        {
                            "id": "smc",
                            "name": "SMC",
                            "aliases": ["Smart Money Concepts"],
                            "kind": "analysis_lens",
                            "availability": "top_level",
                            "knowledge_qna": True,
                            "native_market_analyzer": "smc",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            (index_dir / "index_manifest.json").write_text(
                json.dumps({
                    "manifest_version": 1,
                    "collections": {
                        "school_knowledge_v2": {
                            "count": 5,
                            "created": True,
                            "layer": "school",
                            "metadata_value_counts": {
                                "school": {"ICT": 3, "SMC": 2},
                            },
                        },
                    },
                }),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with (
                patch.object(
                    kb_retrieve,
                    "Retriever",
                    side_effect=AssertionError("Chroma must not be opened"),
                ) as retriever,
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "--kb", tmp,
                    "--layer", "school",
                    "--list-schools",
                    "--format", "json",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(
            [(item["name"], item["count"]) for item in payload["schools"]],
            [("ICT", 3), ("SMC", 2)],
        )
        self.assertEqual(payload["schools"][0]["id"], "ict")
        self.assertEqual(payload["schools"][0]["native_market_analyzer"], "smc")
        retriever.assert_not_called()

    def test_list_schools_derives_legacy_manifest_counts_without_chroma(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            (kb_dir / "schools.json").write_text(
                json.dumps({
                    "registry_version": 1,
                    "default_profile": {"id": "ict", "schools": ["ICT"]},
                    "schools": [{
                        "id": "ict",
                        "name": "ICT",
                        "aliases": [],
                        "kind": "analysis_lens",
                        "availability": "top_level",
                        "knowledge_qna": True,
                        "native_market_analyzer": "smc",
                    }],
                }),
                encoding="utf-8",
            )
            (index_dir / "index_manifest.json").write_text(
                json.dumps({
                    "manifest_version": 1,
                    "collections": {
                        "school_knowledge_v2": {
                            "count": 2,
                            "created": True,
                            "layer": "school",
                        },
                    },
                }),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with (
                patch.object(
                    kb_retrieve,
                    "_projected_school_counts",
                    return_value={"ICT": 2},
                ) as projected_counts,
                patch.object(
                    kb_retrieve,
                    "Retriever",
                    side_effect=AssertionError("Chroma must not be opened"),
                ) as retriever,
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "--kb", tmp,
                    "--layer", "school",
                    "--list-schools",
                    "--format", "json",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["schools"][0]["count"], 2)
        projected_counts.assert_called_once_with(kb_dir, "school")
        retriever.assert_not_called()

    def test_list_schools_fails_closed_on_manifest_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            (index_dir / "index_manifest.json").write_text(
                json.dumps({
                    "manifest_version": 1,
                    "collections": {
                        "school_knowledge_v2": {
                            "count": 3,
                            "created": True,
                            "layer": "school",
                            "metadata_value_counts": {"school": {"ICT": 2}},
                        },
                    },
                }),
                encoding="utf-8",
            )

            with (
                patch.object(kb_retrieve.log, "error") as log_error,
                patch.object(kb_retrieve, "Retriever") as retriever,
            ):
                result = kb_retrieve.main([
                    "--kb", tmp,
                    "--layer", "school",
                    "--list-schools",
                ])

        self.assertEqual(result, 2)
        self.assertIn("计数不一致", str(log_error.call_args))
        retriever.assert_not_called()

    def test_source_selector_on_canonical_layer_exits_before_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(kb_retrieve, "get_embedder") as get_embedder,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                kb_retrieve.main([
                    "query", "--kb", tmp, "--sources", "Course A",
                ])

        self.assertEqual(raised.exception.code, 2)
        get_embedder.assert_not_called()

    def test_v2_json_falls_back_to_document_and_metadata(self):
        card = RetrievedCard(
            card_id="e-1",
            card_type="definition",
            term="Fair Value Gap",
            school="SMC",
            file_path="",
            document="evidence text",
            distance=0.2,
            metadata={"source": "Course A"},
            match_kind="exact",
        )

        class FakeRetriever:
            def __init__(self, kb_dir, embedder, *, layer):
                self.embedder = embedder

            def resolve_scope(self, **kwargs):
                return {
                    "schools": ["SMC"],
                    "sources": ["Course A"],
                    "excluded_schools": [],
                    "type": None,
                    "where": {"source": "Course A"},
                }

            def search(self, **kwargs):
                return [card]

            def get_full_card(self, card):
                raise AssertionError("v2 must not load a fused canonical card")

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "FVG", "--kb", tmp,
                    "--layer", "evidence",
                    "--schools", "SMC",
                    "--sources", "Course A",
                    "--format", "json",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertNotIn("card", payload[0])
        self.assertEqual(payload[0]["record"]["document"], "evidence text")
        self.assertEqual(
            payload[0]["record"]["metadata"], {"source": "Course A"}
        )

    def test_v2_markdown_renders_scoped_document_without_full_card(self):
        card = RetrievedCard(
            card_id="s-1",
            card_type="concept",
            term="中枢",
            school="缠论",
            file_path="concepts/fused-parent.json",
            document="仅属于缠论投影的内容",
            distance=0.2,
            metadata={"layer": "school"},
        )

        class FakeRetriever:
            def __init__(self, kb_dir, embedder, *, layer):
                pass

            def resolve_scope(self, **kwargs):
                return {
                    "schools": ["缠论"],
                    "sources": [],
                    "excluded_schools": [],
                    "type": None,
                    "where": {"school": "缠论"},
                }

            def search(self, **kwargs):
                return [card]

            def get_full_card(self, card):
                raise AssertionError("v2 must not load a fused canonical card")

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "中枢", "--kb", tmp,
                    "--layer", "school",
                    "--schools", "缠论",
                ])

        self.assertEqual(result, 0)
        self.assertIn("仅属于缠论投影的内容", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
