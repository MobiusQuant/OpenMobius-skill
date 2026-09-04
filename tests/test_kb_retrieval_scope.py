import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_index  # noqa: E402
import kb_retrieve  # noqa: E402
from _lib.retriever import (  # noqa: E402
    RetrievedCard,
    Retriever,
    build_where_filter,
)


class _FakeVector:
    def tolist(self):
        return [0.25, 0.75]


class _FakeEmbedder:
    def embed_query(self, query):
        self.query = query
        return _FakeVector()


class _FakeCollection:
    def __init__(self):
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ids": [["card-1"]],
            "metadatas": [[{
                "type": "concept",
                "term": "Fair Value Gap",
                "school": "ICT",
                "file_path": "concepts/card-1.json",
            }]],
            "documents": [["document"]],
            "distances": [[0.1]],
        }


class FilterBuilderTests(unittest.TestCase):
    def test_no_filter_preserves_full_library_scope(self):
        self.assertIsNone(build_where_filter())

    def test_multiple_schools_use_in(self):
        self.assertEqual(
            build_where_filter(filter_schools=["ICT", "SMC"]),
            {"school": {"$in": ["ICT", "SMC"]}},
        )

    def test_school_and_type_use_explicit_and(self):
        self.assertEqual(
            build_where_filter(
                filter_schools=["ICT", "SMC"],
                filter_type="case",
            ),
            {
                "$and": [
                    {"school": {"$in": ["ICT", "SMC"]}},
                    {"type": "case"},
                ]
            },
        )

    def test_legacy_and_new_school_options_are_combined_once(self):
        self.assertEqual(
            build_where_filter(
                filter_school="ICT",
                filter_schools=["SMC", "ICT"],
            ),
            {"school": {"$in": ["ICT", "SMC"]}},
        )

    def test_retriever_without_filters_passes_where_none(self):
        retriever = Retriever.__new__(Retriever)
        retriever.embedder = _FakeEmbedder()
        retriever.collection = _FakeCollection()

        cards = retriever.search("query")

        self.assertEqual(len(cards), 1)
        self.assertIsNone(retriever.collection.calls[0]["where"])

    def test_retriever_passes_combined_filter_to_collection(self):
        retriever = Retriever.__new__(Retriever)
        retriever.embedder = _FakeEmbedder()
        retriever.collection = _FakeCollection()

        retriever.search(
            "query",
            filter_type="concept",
            filter_schools=["ICT", "SMC"],
        )

        self.assertEqual(
            retriever.collection.calls[0]["where"],
            {
                "$and": [
                    {"school": {"$in": ["ICT", "SMC"]}},
                    {"type": "concept"},
                ]
            },
        )


class CliScopeTests(unittest.TestCase):
    def test_legacy_school_with_spaces_is_preserved(self):
        parser = kb_retrieve.build_parser()
        args = parser.parse_args(["query", "--school", "Price Action"])
        self.assertEqual(
            kb_retrieve.resolve_school_scope(args, parser),
            ["Price Action"],
        )

    def test_plural_schools_accepts_multiple_values(self):
        parser = kb_retrieve.build_parser()
        args = parser.parse_args(["query", "--schools", "ICT", "SMC"])
        self.assertEqual(
            kb_retrieve.resolve_school_scope(args, parser),
            ["ICT", "SMC"],
        )

    def test_all_schools_is_mutually_exclusive_with_filters(self):
        parser = kb_retrieve.build_parser()
        args = parser.parse_args([
            "query", "--all-schools", "--schools", "ICT", "SMC",
        ])
        with (
            self.assertRaises(SystemExit),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            kb_retrieve.resolve_school_scope(args, parser)

    def test_explicit_empty_school_fails_closed(self):
        parser = kb_retrieve.build_parser()
        args = parser.parse_args(["query", "--school", "   "])
        with (
            self.assertRaises(SystemExit),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            kb_retrieve.resolve_school_scope(args, parser)

    def test_json_remains_a_list_and_adds_effective_scope(self):
        captured = {}
        card = RetrievedCard(
            card_id="card-1",
            card_type="case",
            term="BTC reversal",
            school="ICT",
            file_path="cases/card-1.json",
            document="document",
            distance=0.125,
            metadata={},
        )

        class FakeRetriever:
            def __init__(self, kb_dir, embedder):
                captured["kb_dir"] = kb_dir

            def search(self, **kwargs):
                captured["search"] = kwargs
                return [card]

            def get_full_card(self, retrieved):
                return {"title": retrieved.term}

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "BTC reversal",
                    "--kb", tmp,
                    "--schools", "ICT", "SMC",
                    "--type", "case",
                    "--format", "json",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertIsInstance(payload, list)
        self.assertEqual(
            payload[0]["scope"],
            {
                "schools": ["ICT", "SMC"],
                "all_schools": False,
                "type": "case",
            },
        )
        self.assertEqual(captured["search"]["filter_schools"], ["ICT", "SMC"])
        self.assertEqual(captured["search"]["filter_type"], "case")

    def test_compact_preserves_the_original_single_line_shape(self):
        card = RetrievedCard(
            card_id="card-1",
            card_type="concept",
            term="Spring",
            school="Wyckoff",
            file_path="concepts/card-1.json",
            document="document",
            distance=0.2,
            metadata={},
        )

        class FakeRetriever:
            def __init__(self, kb_dir, embedder):
                pass

            def search(self, **kwargs):
                return [card]

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "spring",
                    "--kb", tmp,
                    "--schools", "Wyckoff",
                    "--format", "compact",
                ])

        lines = stdout.getvalue().splitlines()
        self.assertEqual(result, 0)
        self.assertEqual(len(lines), 1)
        self.assertNotIn("scope=", lines[0])
        self.assertTrue(lines[0].endswith("→ concepts/card-1.json"))

    def test_empty_compact_preserves_original_sentinel(self):
        class FakeRetriever:
            def __init__(self, kb_dir, embedder):
                pass

            def search(self, **kwargs):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.object(kb_retrieve, "get_embedder", return_value=object()),
                patch.object(kb_retrieve, "Retriever", FakeRetriever),
                redirect_stdout(stdout),
            ):
                result = kb_retrieve.main([
                    "missing",
                    "--kb", tmp,
                    "--schools", "Wyckoff",
                    "--format", "compact",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "(no results)\n")


class IndexMetadataTests(unittest.TestCase):
    def test_existing_fields_are_preserved_and_provenance_is_additive(self):
        item = {
            "id": "fair_value_gap",
            "type": "concept",
            "file_path": "concepts/fair_value_gap.json",
            "card": {
                "global_card_id": "fair_value_gap",
                "canonical_term": "Fair Value Gap",
                "school": "ICT",
                "source_cards": [
                    {"project": "Education - ICT", "source_school": "ICT"},
                    {"project": "Teach-Wuyuan", "source_school": "SMC"},
                    {"project": "Education - ICT", "source_school": "ICT"},
                ],
            },
        }

        metadata = build_index.build_card_metadata(item)

        self.assertEqual(metadata["type"], "concept")
        self.assertEqual(metadata["card_id"], "fair_value_gap")
        self.assertEqual(metadata["term"], "Fair Value Gap")
        self.assertEqual(metadata["school"], "ICT")
        self.assertEqual(metadata["file_path"], "concepts/fair_value_gap.json")
        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(metadata["layer"], "canonical_concept")
        self.assertEqual(metadata["source_collection_count"], 2)
        self.assertEqual(metadata["source_card_count"], 3)
        self.assertEqual(
            json.loads(metadata["source_names"]),
            ["Education - ICT", "Teach-Wuyuan"],
        )
        self.assertEqual(
            json.loads(metadata["source_schools"]),
            ["ICT", "SMC"],
        )
        self.assertTrue(all(
            isinstance(value, (str, int, float, bool))
            for value in metadata.values()
        ))

    def test_older_or_sparse_card_schema_still_builds_metadata(self):
        item = {
            "id": "legacy-card",
            "type": "concept",
            "file_path": "concepts/legacy-card.json",
            "card": {"definition": "legacy"},
        }

        metadata = build_index.build_card_metadata(item)

        self.assertEqual(metadata["term"], "legacy-card")
        self.assertEqual(metadata["school"], "")
        self.assertEqual(metadata["canonical_id"], "legacy-card")
        self.assertEqual(metadata["source_collection_count"], 0)
        self.assertEqual(metadata["source_card_count"], 0)
        self.assertEqual(json.loads(metadata["source_names"]), [])


if __name__ == "__main__":
    unittest.main()
