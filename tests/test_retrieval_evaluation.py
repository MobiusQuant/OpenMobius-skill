import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import evaluate_retrieval as evaluation  # noqa: E402
from _lib.retriever import RetrievalScopeError  # noqa: E402


DATASET = ROOT / "evals" / "retrieval_benchmark_v1.jsonl"


def _case(
    case_id,
    query,
    record_id,
    canonical_id,
    *,
    schools=None,
    sources=None,
):
    return {
        "benchmark_version": 1,
        "id": case_id,
        "query": query,
        "language": "en",
        "query_kind": "natural_language",
        "truth_source": "canonical_card.term_template",
        "layer": "evidence",
        "scope": {
            "schools": schools or [],
            "sources": sources or [],
            "exclude_schools": [],
            "type": "concept",
        },
        "expected": {
            "outcome": "hit",
            "record_ids": [record_id],
            "canonical_ids": [canonical_id],
        },
    }


def _result(record_id, canonical_id, *, school="ICT", source="Course A"):
    return SimpleNamespace(
        card_id=record_id,
        card_type="concept",
        school=school,
        metadata={
            "record_id": record_id,
            "canonical_id": canonical_id,
            "type": "concept",
            "school": school,
            "source": source,
        },
    )


class TrackedDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.digest = evaluation.load_benchmark(DATASET)
        cls.distribution = evaluation.validate_benchmark_against_kb(
            cls.cases, ROOT / "knowledge_base"
        )

    def test_static_benchmark_is_current_and_has_stable_distribution(self):
        self.assertEqual(len(self.cases), 180)
        self.assertEqual(
            self.digest,
            "3a088f6399497d3e2c43c137d750b64a2f1c68265d84998b9f8c64a47dd730a6",
        )
        self.assertEqual(
            self.distribution["positive_layers"],
            {"canonical": 60, "evidence": 40, "school": 60},
        )
        self.assertEqual(
            self.distribution["outcomes"], {"fail_closed": 20, "hit": 160}
        )
        self.assertEqual(self.distribution["query_kinds"]["alias"], 40)
        self.assertEqual(len(self.distribution["schools"]), 15)
        self.assertEqual(len(self.distribution["sources"]), 12)
        self.assertEqual(len(self.distribution["content_types"]), 9)
        self.assertEqual(
            set(self.distribution["negative_reasons"]),
            set(evaluation.NEGATIVE_REASONS),
        )

    def test_alias_cases_use_checked_in_alias_truth_only(self):
        aliases = [case for case in self.cases if case["query_kind"] == "alias"]
        self.assertTrue(aliases)
        self.assertTrue(
            all(
                case["truth_source"] == "knowledge_base/term_aliases.json"
                for case in aliases
            )
        )
        changed = copy.deepcopy(aliases[0])
        changed["query"] = "invented evaluator synonym"
        with self.assertRaisesRegex(
            evaluation.BenchmarkValidationError, "not a current alias"
        ):
            evaluation.validate_benchmark_against_kb(
                [changed], ROOT / "knowledge_base", enforce_distribution=False
            )

    def test_loader_rejects_duplicate_case_ids(self):
        line = json.dumps(self.cases[0], ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "duplicate.jsonl"
            path.write_text(f"{line}\n{line}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                evaluation.BenchmarkValidationError, "duplicate benchmark id"
            ):
                evaluation.load_benchmark(path)

    def test_validate_only_cli_never_needs_an_index_or_model(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = evaluation.main([
                "--dataset", str(DATASET),
                "--kb", str(ROOT / "knowledge_base"),
                "--validate-only",
            ])
        report = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["dataset"], "evals/retrieval_benchmark_v1.jsonl")
        self.assertEqual(report["distribution"]["cases"], 180)


class SearchAdapterTests(unittest.TestCase):
    def test_native_search_mode_receives_resolved_scope(self):
        class NativeRetriever:
            def __init__(self):
                self.resolve_call = None
                self.search_call = None

            def resolve_scope(self, **kwargs):
                self.resolve_call = kwargs
                return {
                    "schools": ["ICT"],
                    "sources": ["Course A"],
                    "excluded_schools": [],
                    "type": "concept",
                    "where": {"school": "ICT"},
                }

            def search(
                self,
                query,
                top_k,
                *,
                filter_schools=None,
                filter_sources=None,
                exclude_schools=None,
                filter_type=None,
                strict_scope=False,
                exact_match=True,
                search_mode="auto",
                max_per_canonical=None,
            ):
                self.search_call = locals().copy()
                return [_result("ev-1", "concept-1")]

        retriever = NativeRetriever()
        case = _case(
            "adapter-native",
            "query",
            "ev-1",
            "concept-1",
            schools=["ict"],
            sources=["course a"],
        )
        results = evaluation.search_with_adapter(
            retriever,
            case,
            top_k=5,
            search_mode="hybrid",
            max_per_canonical=2,
            execution=(execution := {}),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(retriever.resolve_call["filter_schools"], ["ict"])
        self.assertEqual(retriever.search_call["filter_schools"], ["ICT"])
        self.assertEqual(retriever.search_call["filter_sources"], ["Course A"])
        self.assertEqual(retriever.search_call["search_mode"], "hybrid")
        self.assertEqual(retriever.search_call["max_per_canonical"], 2)
        self.assertTrue(retriever.search_call["exact_match"])
        self.assertFalse(retriever.search_call["strict_scope"])
        self.assertIs(execution["exact_match"], True)

    def test_legacy_adapter_is_explicit_about_supported_modes(self):
        class LegacyRetriever:
            def __init__(self):
                self.exact_match = None

            def resolve_scope(self, **kwargs):
                return {
                    "schools": [],
                    "sources": [],
                    "excluded_schools": [],
                    "type": None,
                    "where": None,
                }

            def search(
                self,
                query,
                top_k,
                *,
                exact_match=True,
                strict_scope=False,
            ):
                self.exact_match = exact_match
                return []

        case = _case("adapter-legacy", "query", "ev-1", "concept-1")
        case["scope"]["type"] = None
        retriever = LegacyRetriever()
        evaluation.search_with_adapter(
            retriever,
            case,
            top_k=5,
            search_mode="semantic",
            max_per_canonical=None,
            execution=(execution := {}),
        )
        self.assertFalse(retriever.exact_match)
        self.assertIs(execution["exact_match"], False)
        with self.assertRaisesRegex(
            evaluation.RetrieverAdapterError, "requires Retriever.search"
        ):
            evaluation.search_with_adapter(
                retriever,
                case,
                top_k=5,
                search_mode="hybrid",
                max_per_canonical=None,
            )

    def test_missing_resolve_scope_is_a_clear_adapter_error(self):
        class UnsafeRetriever:
            def search(self, **kwargs):
                return []

        case = _case("adapter-unsafe", "query", "ev-1", "concept-1")
        with self.assertRaisesRegex(
            evaluation.RetrieverAdapterError, "resolve_scope"
        ):
            evaluation.search_with_adapter(
                UnsafeRetriever(),
                case,
                top_k=5,
                search_mode="auto",
                max_per_canonical=None,
            )


class MetricTests(unittest.TestCase):
    def test_effective_diversity_limit_matches_retriever_defaults(self):
        self.assertIsNone(
            evaluation.effective_max_per_canonical(None, "canonical")
        )
        self.assertEqual(
            evaluation.effective_max_per_canonical(None, "school"), 2
        )
        self.assertEqual(
            evaluation.effective_max_per_canonical(None, "evidence"), 2
        )
        for layer in evaluation.LAYERS:
            self.assertIsNone(
                evaluation.effective_max_per_canonical(0, layer)
            )
            self.assertEqual(
                evaluation.effective_max_per_canonical(3, layer), 3
            )

    def test_metrics_cover_recall_purity_fail_closed_duplicates_and_latency(self):
        cases = [
            _case(
                "metric-one", "first", "ev-1", "canonical-1",
                schools=["ICT"], sources=["Course A"],
            ),
            _case(
                "metric-two", "second", "ev-3", "canonical-3",
                schools=["ICT"], sources=["Course A"],
            ),
            {
                "benchmark_version": 1,
                "id": "metric-negative",
                "query": "negative",
                "language": "en",
                "query_kind": "negative_scope",
                "truth_source": "retrieval_scope_contract",
                "layer": "evidence",
                "scope": {
                    "schools": ["Unknown"],
                    "sources": [],
                    "exclude_schools": [],
                    "type": "concept",
                },
                "expected": {
                    "outcome": "fail_closed",
                    "reason": "unknown_school",
                },
            },
        ]

        class FakeRetriever:
            def resolve_scope(self, **kwargs):
                if kwargs["filter_schools"] == ["Unknown"]:
                    raise RetrievalScopeError("unknown School")
                return {
                    "schools": kwargs["filter_schools"] or [],
                    "sources": kwargs["filter_sources"] or [],
                    "excluded_schools": kwargs["exclude_schools"] or [],
                    "type": kwargs["filter_type"],
                    "where": {},
                }

            def search(self, query, top_k, **kwargs):
                if query == "first":
                    return [
                        _result("ev-1", "canonical-1"),
                        _result("ev-1b", "canonical-1"),
                    ]
                return [
                    _result("irrelevant-a", "canonical-a"),
                    _result("irrelevant-b", "canonical-b"),
                    _result("ev-3", "canonical-3"),
                ]

        ticks = iter((0.00, 0.01, 0.01, 0.03, 0.03, 0.06))
        report = evaluation.evaluate_cases(
            cases,
            lambda layer: FakeRetriever(),
            top_k=5,
            search_mode="hybrid",
            clock=lambda: next(ticks),
        )
        metrics = report["metrics"]
        self.assertEqual(metrics["recall_at_1"], 0.5)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 0.666667)
        self.assertEqual(metrics["scope_purity"], 1.0)
        self.assertEqual(metrics["source_purity"], 1.0)
        self.assertEqual(metrics["fail_closed"]["rate"], 1.0)
        self.assertIn(
            "unknown_school",
            metrics["fail_closed"]["by_expected_scope_category"],
        )
        self.assertEqual(metrics["duplicate_canonical_ratio"], 0.2)
        self.assertEqual(metrics["latency_ms"]["mean"], 20.0)
        self.assertEqual(metrics["latency_ms"]["p95"], 30.0)

    def test_fail_closed_group_is_expected_category_not_error_classification(self):
        case = {
            "benchmark_version": 1,
            "id": "metric-negative-category",
            "query": "negative",
            "language": "en",
            "query_kind": "negative_scope",
            "truth_source": "retrieval_scope_contract",
            "layer": "canonical",
            "scope": {
                "schools": ["Unknown"],
                "sources": [],
                "exclude_schools": [],
                "type": "concept",
            },
            "expected": {
                "outcome": "fail_closed",
                "reason": "unknown_school",
            },
        }

        class RejectingRetriever:
            def resolve_scope(self, **kwargs):
                raise RetrievalScopeError("some scope rejection")

            def search(self, **kwargs):
                raise AssertionError("search must not run")

        report = evaluation.evaluate_cases(
            [case],
            lambda layer: RejectingRetriever(),
            top_k=5,
            search_mode="auto",
            clock=iter((0.0, 0.0)).__next__,
        )
        row = report["cases"][0]
        self.assertTrue(row["fail_closed_pass"])
        self.assertEqual(row["expected_scope_category"], "unknown_school")
        self.assertIn("some scope rejection", row["error"])

    def test_index_provenance_normalizes_per_collection_embedding_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            kb_dir = Path(temp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            manifest = {
                "manifest_version": 1,
                "index_schema_version": 2,
                "embedding_strategy": {"school_knowledge_v2": "native_document"},
                "embedding_models": {"school_knowledge_v2": "model-a"},
                "embedding_revisions": {"school_knowledge_v2": "revision-a"},
                "embedding_dimensions": {"school_knowledge_v2": 768},
                "v2_input_fingerprint": "abc",
                "canonical_input_fingerprint": "def",
                "v2_embedding_input_profile": {
                    "input_version": "search-document-v2-maxseq512",
                    "max_seq_length": 512,
                },
                "collections": {
                    "school_knowledge_v2": {
                        "count": 2,
                        "created": True,
                        "layer": "school",
                        "schema_version": 2,
                    }
                },
            }
            (index_dir / "index_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            provenance = evaluation.index_provenance(kb_dir)

        collection = provenance["collections"]["school_knowledge_v2"]
        self.assertEqual(collection["embedding_strategy"], "native_document")
        self.assertEqual(collection["embedding_model"], "model-a")
        self.assertEqual(collection["embedding_revision"], "revision-a")
        self.assertEqual(collection["embedding_dimension"], 768)
        self.assertEqual(
            provenance["v2_embedding_input_profile"]["max_seq_length"], 512
        )


class QueryEmbeddingCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _provenance(v2_model="nomic-ai/nomic-embed-text-v1.5", v2_dim=768):
        return {
            "manifest_present": True,
            "collections": {
                "knowledge_base": {
                    "embedding_model": "nomic-ai/nomic-embed-text-v1.5",
                    "embedding_revision": evaluation.QUERY_EMBEDDING_PROVIDERS[
                        "local"
                    ]["revision"],
                    "embedding_dimension": 768,
                },
                "school_knowledge_v2": {
                    "embedding_model": v2_model,
                    "embedding_revision": (
                        evaluation.QUERY_EMBEDDING_PROVIDERS["local"]["revision"]
                        if v2_model == "nomic-ai/nomic-embed-text-v1.5"
                        else None
                    ),
                    "embedding_dimension": v2_dim,
                },
                "source_evidence_v2": {
                    "embedding_model": v2_model,
                    "embedding_revision": (
                        evaluation.QUERY_EMBEDDING_PROVIDERS["local"]["revision"]
                        if v2_model == "nomic-ai/nomic-embed-text-v1.5"
                        else None
                    ),
                    "embedding_dimension": v2_dim,
                },
            },
        }

    def test_full_auto_uses_local_for_canonical_and_selected_provider_for_v2(self):
        providers = evaluation.required_query_embedding_providers(
            evaluation.LAYERS,
            search_mode="auto",
            v2_provider="openai",
        )
        self.assertEqual(
            providers,
            {"canonical": "local", "school": "openai", "evidence": "openai"},
        )
        identities = evaluation.validate_query_embedding_compatibility(
            self._provenance("text-embedding-3-small", 1536), providers
        )
        self.assertEqual(identities["canonical"]["dimension"], 768)
        self.assertEqual(identities["school"]["dimension"], 1536)

    def test_same_local_provider_can_be_shared_across_all_layers(self):
        providers = evaluation.required_query_embedding_providers(
            evaluation.LAYERS,
            search_mode="auto",
            v2_provider="local",
        )
        self.assertEqual(set(providers.values()), {"local"})
        evaluation.validate_query_embedding_compatibility(
            self._provenance(), providers
        )

    def test_revision_mismatch_fails_before_model_loading(self):
        providers = evaluation.required_query_embedding_providers(
            ("school",), search_mode="semantic", v2_provider="local"
        )
        provenance = self._provenance()
        provenance["collections"]["school_knowledge_v2"][
            "embedding_revision"
        ] = "wrong-revision"
        with self.assertRaisesRegex(
            evaluation.EvaluationSetupError,
            "query/index embedding mismatch.*revision",
        ):
            evaluation.validate_query_embedding_compatibility(
                provenance,
                providers,
            )

    def test_mismatch_fails_before_querying_with_actionable_message(self):
        providers = evaluation.required_query_embedding_providers(
            ("school",), search_mode="semantic", v2_provider="openai"
        )
        with self.assertRaisesRegex(
            evaluation.EvaluationSetupError,
            "query/index embedding mismatch.*--embedder openai",
        ):
            evaluation.validate_query_embedding_compatibility(
                self._provenance(), providers
            )

    def test_lexical_mode_needs_no_manifest_embedding_identity(self):
        providers = evaluation.required_query_embedding_providers(
            evaluation.LAYERS,
            search_mode="lexical",
            v2_provider="openai",
        )
        self.assertEqual(set(providers.values()), {None})
        identities = evaluation.validate_query_embedding_compatibility(
            {"manifest_present": False}, providers
        )
        self.assertTrue(all(value is None for value in identities.values()))


if __name__ == "__main__":
    unittest.main()
