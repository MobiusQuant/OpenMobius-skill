import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_index  # noqa: E402


def _records(*documents):
    return [
        {"id": f"record-{position}", "document": document, "metadata": {}}
        for position, document in enumerate(documents)
    ]


class _CountingEmbedder:
    dim = 2

    def __init__(self):
        self.calls = []

    def embed_documents(self, documents):
        self.calls.append(list(documents))
        return [
            [float(len(document)), float(sum(document.encode("utf-8")) % 97)]
            for document in documents
        ]


class NativeEmbeddingCacheTests(unittest.TestCase):
    def test_first_run_batches_and_warm_cache_never_loads_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache" / "v2.sqlite3"
            records = _records("a", "bb", "ccc", "dddd", "eeeee")
            embedder = _CountingEmbedder()

            first, stats = build_index.native_embeddings_for_records(
                records,
                cache_path=cache_path,
                model_key="mock:model-a",
                expected_dimension=2,
                embedder_factory=lambda: embedder,
                batch_size=2,
            )

            self.assertEqual([len(batch) for batch in embedder.calls], [2, 2, 1])
            self.assertEqual(stats["cache_hit_records"], 0)
            self.assertEqual(stats["computed_unique_documents"], 5)
            self.assertTrue(cache_path.is_file())

            def fail_if_loaded():
                raise AssertionError("warm cache must not load the embedder")

            second, warm_stats = build_index.native_embeddings_for_records(
                records,
                cache_path=cache_path,
                model_key="mock:model-a",
                expected_dimension=2,
                embedder_factory=fail_if_loaded,
                batch_size=2,
            )

            self.assertEqual(len(second), len(first))
            for actual, expected in zip(second, first):
                self.assertEqual(len(actual), len(expected))
                for actual_value, expected_value in zip(actual, expected):
                    self.assertAlmostEqual(actual_value, expected_value, places=5)
            self.assertEqual(warm_stats["cache_hit_records"], 5)
            self.assertEqual(warm_stats["computed_unique_documents"], 0)

    def test_cache_is_incremental_content_addressed_and_model_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "v2.sqlite3"
            first_embedder = _CountingEmbedder()
            build_index.native_embeddings_for_records(
                _records("same", "old"),
                cache_path=cache_path,
                model_key="mock:model-a",
                expected_dimension=2,
                embedder_factory=lambda: first_embedder,
            )

            incremental = _CountingEmbedder()
            _, stats = build_index.native_embeddings_for_records(
                _records("same", "old", "new"),
                cache_path=cache_path,
                model_key="mock:model-a",
                expected_dimension=2,
                embedder_factory=lambda: incremental,
            )
            self.assertEqual(incremental.calls, [["new"]])
            self.assertEqual(stats["cache_hit_records"], 2)

            other_model = _CountingEmbedder()
            _, isolated_stats = build_index.native_embeddings_for_records(
                _records("same"),
                cache_path=cache_path,
                model_key="mock:model-b",
                expected_dimension=2,
                embedder_factory=lambda: other_model,
            )
            self.assertEqual(other_model.calls, [["same"]])
            self.assertEqual(isolated_stats["cache_hit_records"], 0)

    def test_identical_documents_are_embedded_once_but_keep_record_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            embedder = _CountingEmbedder()
            vectors, stats = build_index.native_embeddings_for_records(
                _records("duplicate", "other", "duplicate"),
                cache_path=Path(tmp) / "v2.sqlite3",
                model_key="mock:model-a",
                expected_dimension=2,
                embedder_factory=lambda: embedder,
            )

            self.assertEqual(embedder.calls, [["other", "duplicate"]])
            self.assertEqual(stats["unique_document_count"], 2)
            self.assertEqual(vectors[0], vectors[2])
            self.assertNotEqual(vectors[0], vectors[1])

    def test_native_max_length_is_scoped_to_each_document_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen_lengths = []
            seen_progress_flags = []

            class ProfileEmbedder(_CountingEmbedder):
                def __init__(self):
                    super().__init__()
                    self.model = types.SimpleNamespace(max_seq_length=8192)

                def embed_documents(self, documents, show_progress_bar=True):
                    seen_lengths.append(self.model.max_seq_length)
                    seen_progress_flags.append(show_progress_bar)
                    return super().embed_documents(documents)

            embedder = ProfileEmbedder()
            build_index.native_embeddings_for_records(
                _records("short", "a little longer", "longest document here"),
                cache_path=Path(tmp) / "v2.sqlite3",
                model_key="profile:model",
                expected_dimension=2,
                embedder_factory=lambda: embedder,
                batch_size=1,
                max_seq_length=build_index.V2_NATIVE_MAX_SEQ_LENGTH,
            )

            self.assertEqual(seen_lengths, [512, 512, 512])
            self.assertEqual(seen_progress_flags, [False, False, False])
            self.assertEqual(embedder.model.max_seq_length, 8192)

    def test_bumped_input_profile_cannot_hit_the_old_8192_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "v2.sqlite3"
            old_embedder = _CountingEmbedder()
            old_key = f"search-document-v1:local:{build_index.EXPECTED_MODEL}"
            build_index.native_embeddings_for_records(
                _records("previously cached"),
                cache_path=cache_path,
                model_key=old_key,
                expected_dimension=2,
                embedder_factory=lambda: old_embedder,
            )

            spec = build_index.v2_embedding_spec("local")
            new_embedder = _CountingEmbedder()
            _, stats = build_index.native_embeddings_for_records(
                _records("previously cached"),
                cache_path=cache_path,
                model_key=spec["cache_model_key"],
                expected_dimension=2,
                embedder_factory=lambda: new_embedder,
            )

            self.assertNotEqual(old_key, spec["cache_model_key"])
            self.assertIn(build_index.V2_EMBEDDING_INPUT_VERSION, spec["cache_model_key"])
            self.assertIn("max_seq_length=512", spec["cache_model_key"])
            self.assertEqual(stats["cache_hit_records"], 0)
            self.assertEqual(new_embedder.calls, [["previously cached"]])


class NativeIndexPolicyTests(unittest.TestCase):
    def test_effective_strategy_overrides_builder_metadata_in_both_modes(self):
        records = [
            {
                "id": "one",
                "document": "doc",
                "metadata": {"embedding_strategy": "builder-default"},
            }
        ]
        build_index.apply_v2_embedding_strategy(
            records, build_index.V2_NATIVE_STRATEGY
        )
        self.assertEqual(
            records[0]["metadata"]["embedding_strategy"],
            build_index.V2_NATIVE_STRATEGY,
        )
        self.assertEqual(
            records[0]["metadata"]["embedding_max_seq_length"], 512
        )
        self.assertEqual(
            records[0]["metadata"]["embedding_input_version"],
            build_index.V2_EMBEDDING_INPUT_VERSION,
        )
        build_index.apply_v2_embedding_strategy(
            records, build_index.V2_INHERITED_STRATEGY
        )
        self.assertEqual(
            records[0]["metadata"]["embedding_strategy"],
            build_index.V2_INHERITED_STRATEGY,
        )
        self.assertNotIn("embedding_max_seq_length", records[0]["metadata"])
        self.assertEqual(
            records[0]["metadata"]["embedding_input_version"],
            build_index.V2_INHERITED_INPUT_VERSION,
        )

    def test_chroma_batch_boundary_normalizes_non_list_sequences(self):
        captured = []

        class Collection:
            def add(self, *, embeddings, **_kwargs):
                captured.extend(embeddings)

        class Client:
            def create_collection(self, **_kwargs):
                return Collection()

        build_index.add_collection_records(
            Client(),
            name="test_collection",
            records=[{"id": "one", "document": "doc", "metadata": {}}],
            embeddings=[(0.25, 0.75)],
            layer="school",
        )

        self.assertEqual(captured, [[0.25, 0.75]])
        self.assertIsInstance(captured[0], list)

    def test_inherited_manifest_is_not_current_for_native_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp)
            manifest = {
                "manifest_version": build_index.INDEX_MANIFEST_VERSION,
                "index_schema_version": build_index.INDEX_SCHEMA_VERSION,
                "v2_input_fingerprint": "a" * 64,
                "canonical_input_fingerprint": "b" * 64,
                "v2_embedding_input_profile": build_index.v2_input_profile(
                    build_index.V2_INHERITED_STRATEGY, "parent_card"
                ),
                "embedding_strategy": {
                    build_index.SCHOOL_COLLECTION: build_index.V2_INHERITED_STRATEGY,
                    build_index.EVIDENCE_COLLECTION: build_index.V2_INHERITED_STRATEGY,
                },
                "embedding_models": {
                    build_index.SCHOOL_COLLECTION: build_index.EXPECTED_MODEL,
                    build_index.EVIDENCE_COLLECTION: build_index.EXPECTED_MODEL,
                },
                "embedding_dimensions": {
                    build_index.SCHOOL_COLLECTION: build_index.EXPECTED_DIM,
                    build_index.EVIDENCE_COLLECTION: build_index.EXPECTED_DIM,
                },
                "collections": {},
            }
            (index_dir / build_index.INDEX_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            self.assertFalse(
                build_index.index_has_current_v2(
                    index_dir,
                    fingerprint="a" * 64,
                    canonical_fingerprint="b" * 64,
                    legacy_count=1,
                    school_count=1,
                    evidence_count=1,
                    v2_embedding_strategy=build_index.V2_NATIVE_STRATEGY,
                    v2_embedding_model=build_index.EXPECTED_MODEL,
                    v2_embedding_dimension=build_index.EXPECTED_DIM,
                )
            )

    def test_default_build_indexes_native_documents_and_records_manifest_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            item = {
                "id": "parent",
                "type": "concept",
                "file_path": "concepts/parent.json",
                "text": "canonical parent text",
                "card": {
                    "global_card_id": "parent",
                    "_embedding_model": build_index.EXPECTED_MODEL,
                    "_embedding": [0.0] * build_index.EXPECTED_DIM,
                },
            }
            school_record = {
                "id": "school-record",
                "document": "school-specific document",
                "metadata": {
                    "canonical_id": "parent",
                    "type": "concept",
                    "term": "Parent",
                    "school": "ICT",
                    "file_path": "concepts/parent.json",
                    "source_names": ["Mentorship"],
                    "source_collection_count": 1,
                },
            }
            evidence_record = {
                "id": "evidence-record",
                "document": "source-specific evidence",
                "metadata": {
                    "canonical_id": "parent",
                    "type": "concept",
                    "term": "Parent",
                    "school": "ICT",
                    "source": "Mentorship",
                    "content_type": "definition",
                    "file_path": "concepts/parent.json",
                    "ref": "definition_per_source.Mentorship",
                },
            }

            captured = {}

            class Collection:
                def __init__(self, name, metadata):
                    self.name = name
                    self.metadata = metadata
                    self.embeddings = []
                    self.metadatas = []

                def add(self, *, embeddings, **_kwargs):
                    self.embeddings.extend(embeddings)
                    self.metadatas.extend(_kwargs["metadatas"])

                def count(self):
                    return len(self.embeddings)

            class Client:
                def __init__(self, path):
                    path = Path(path)
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "chroma.sqlite3").write_text("fake", encoding="utf-8")
                    self._system = types.SimpleNamespace(stop=lambda: None)

                def create_collection(self, *, name, metadata):
                    collection = Collection(name, metadata)
                    captured[name] = collection
                    return collection

                def get_collection(self, name):
                    return captured[name]

            fake_chromadb = types.SimpleNamespace(PersistentClient=Client)
            embedder = _CountingEmbedder()
            fake_embedder_module = types.SimpleNamespace(
                get_embedder=lambda _provider: embedder
            )
            with (
                patch.object(
                    sys,
                    "argv",
                    ["build_index.py", "--kb", str(kb_dir), "--limit", "1"],
                ),
                patch.object(build_index, "collect_cards", return_value=[item]),
                patch.object(
                    build_index,
                    "load_optional_v2_records",
                    return_value=(
                        [school_record],
                        [evidence_record],
                        {"available": True},
                    ),
                ),
                patch.object(
                    build_index,
                    "v2_embedding_spec",
                    return_value={
                        "provider": "local",
                        "model": "mock-native-model",
                        "dimension": 2,
                        "cache_model_key": "mock:native:model",
                        "input_profile": build_index.v2_input_profile(
                            build_index.V2_NATIVE_STRATEGY, "local"
                        ),
                    },
                ),
                patch.dict(
                    sys.modules,
                    {"chromadb": fake_chromadb, "_lib.embedder": fake_embedder_module},
                ),
            ):
                self.assertEqual(build_index.main(), 0)

            manifest = json.loads(
                (kb_dir / "_index" / build_index.INDEX_MANIFEST_FILE).read_text(
                    encoding="utf-8"
                )
            )
            for name in build_index.V2_COLLECTIONS:
                self.assertEqual(
                    manifest["embedding_strategy"][name],
                    build_index.V2_NATIVE_STRATEGY,
                )
                self.assertEqual(
                    manifest["embedding_models"][name], "mock-native-model"
                )
                self.assertEqual(manifest["embedding_dimensions"][name], 2)
            self.assertEqual(manifest["index_schema_version"], 3)
            self.assertEqual(
                manifest["v2_embedding_input_profile"]["max_seq_length"], 512
            )
            cache_stats = manifest["v2_embedding_cache"]
            self.assertEqual(cache_stats["persistent_cache_hit_records"], 0)
            self.assertEqual(cache_stats["seed_hit_records"], 0)
            self.assertEqual(cache_stats["computed_records"], 2)
            self.assertEqual(cache_stats["seed"]["status"], "missing")

            school_vector = captured[build_index.SCHOOL_COLLECTION].embeddings[0]
            evidence_vector = captured[build_index.EVIDENCE_COLLECTION].embeddings[0]
            parent_vector = captured[build_index.LEGACY_COLLECTION].embeddings[0]
            expected_compute_order = sorted(
                [school_record["document"], evidence_record["document"]],
                key=lambda document: (
                    len(document),
                    build_index.document_content_hash(document),
                ),
            )
            self.assertEqual(embedder.calls, [expected_compute_order])
            self.assertIsInstance(school_vector, list)
            self.assertIsInstance(evidence_vector, list)
            school_metadata = captured[build_index.SCHOOL_COLLECTION].metadatas[0]
            self.assertEqual(school_metadata["embedding_max_seq_length"], 512)
            self.assertEqual(
                school_metadata["embedding_input_version"],
                build_index.V2_EMBEDDING_INPUT_VERSION,
            )
            self.assertNotEqual(school_vector, parent_vector)
            self.assertNotEqual(evidence_vector, parent_vector)
            self.assertNotEqual(school_vector, evidence_vector)

    def test_native_embedding_failure_preserves_live_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            live_db = index_dir / "chroma.sqlite3"
            live_db.write_text("working-live-index", encoding="utf-8")

            item = {
                "id": "parent",
                "type": "concept",
                "file_path": "concepts/parent.json",
                "text": "parent text",
                "card": {
                    "global_card_id": "parent",
                    "_embedding_model": build_index.EXPECTED_MODEL,
                    "_embedding": [0.0] * build_index.EXPECTED_DIM,
                },
            }
            school_record = {
                "id": "school-record",
                "document": "independent school text",
                "metadata": {
                    "canonical_id": "parent",
                    "type": "concept",
                    "term": "Parent",
                    "school": "ICT",
                    "file_path": "concepts/parent.json",
                    "source_names": [],
                    "source_collection_count": 0,
                },
            }

            class FailingEmbedder:
                dim = build_index.EXPECTED_DIM

                def embed_documents(self, _documents):
                    raise RuntimeError("synthetic embedding failure")

            fake_embedder_module = types.SimpleNamespace(
                get_embedder=lambda _provider: FailingEmbedder()
            )
            with (
                patch.object(
                    sys,
                    "argv",
                    ["build_index.py", "--kb", str(kb_dir), "--upgrade"],
                ),
                patch.object(build_index, "collect_cards", return_value=[item]),
                patch.object(
                    build_index,
                    "load_optional_v2_records",
                    return_value=([school_record], [], {"available": True}),
                ),
                patch.object(build_index, "index_has_current_v2", return_value=False),
                patch.dict(sys.modules, {"_lib.embedder": fake_embedder_module}),
                self.assertLogs(build_index.log, level="ERROR"),
            ):
                self.assertEqual(build_index.main(), 1)

            self.assertEqual(live_db.read_text(encoding="utf-8"), "working-live-index")
            self.assertFalse(any(kb_dir.glob("._index.build-*")))

    def test_upgrade_without_v2_still_checks_current_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            (index_dir / "chroma.sqlite3").write_bytes(b"existing")
            item = {
                "id": "parent",
                "type": "concept",
                "file_path": "concepts/parent.json",
                "text": "parent text",
                "card": {
                    "global_card_id": "parent",
                    "canonical_term": "Parent",
                    "_embedding_model": build_index.EXPECTED_MODEL,
                    "_embedding": [0.0] * build_index.EXPECTED_DIM,
                },
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    ["build_index.py", "--kb", str(kb_dir), "--upgrade"],
                ),
                patch.object(build_index, "collect_cards", return_value=[item]),
                patch.object(
                    build_index,
                    "load_optional_v2_records",
                    return_value=([], [], {"available": False}),
                ),
                patch.object(
                    build_index,
                    "index_has_current_v2",
                    return_value=True,
                ) as current,
            ):
                self.assertEqual(build_index.main(), 0)

            current.assert_called_once()
            kwargs = current.call_args.kwargs
            self.assertEqual(kwargs["legacy_count"], 1)
            self.assertEqual(kwargs["school_count"], 0)
            self.assertEqual(kwargs["evidence_count"], 0)
            self.assertEqual(
                kwargs["fingerprint"],
                build_index.fingerprint_v2_records([], []),
            )
            self.assertEqual(
                kwargs["canonical_fingerprint"],
                build_index.fingerprint_canonical_items([item]),
            )

    def test_stale_upgrade_without_v2_never_preserves_old_index_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            live_db = index_dir / "chroma.sqlite3"
            live_db.write_bytes(b"old-index-generation")
            item = {
                "id": "parent",
                "type": "concept",
                "file_path": "concepts/parent.json",
                "text": "new parent text",
                "card": {
                    "global_card_id": "parent",
                    "canonical_term": "Parent",
                    "_embedding_model": "stale-model",
                    "_embedding": [],
                },
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    ["build_index.py", "--kb", str(kb_dir), "--upgrade"],
                ),
                patch.object(build_index, "collect_cards", return_value=[item]),
                patch.object(
                    build_index,
                    "load_optional_v2_records",
                    return_value=([], [], {"available": False}),
                ),
                patch.object(
                    build_index,
                    "index_has_current_v2",
                    return_value=False,
                ) as current,
                self.assertLogs(build_index.log, level="ERROR"),
            ):
                self.assertEqual(build_index.main(), 1)

            current.assert_called_once()
            self.assertEqual(live_db.read_bytes(), b"old-index-generation")
            self.assertFalse(any(kb_dir.glob("._index.build-*")))


if __name__ == "__main__":
    unittest.main()
