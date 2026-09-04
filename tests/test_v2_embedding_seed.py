import json
import sys
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_index  # noqa: E402
import export_v2_embedding_seed as exporter  # noqa: E402
from _lib.embedding_cache import (  # noqa: E402
    SEED_DTYPE,
    SEED_FORMAT,
    SEED_FORMAT_VERSION,
    SEED_HASH_ALGORITHM,
    SEED_MANIFEST_FILE,
    SEED_PREFIXES,
    SEED_SHARD_SCHEME,
    EmbeddingCache,
    document_content_hash,
    file_sha256,
    load_embedding_seed,
    write_embedding_seed_shard,
)


MODEL_KEY = "test-profile:local:test-model:revision=test-revision:max_seq_length=512"
MODEL = "test-model"
PROFILE = {
    "version": "test-profile",
    "strategy": build_index.V2_NATIVE_STRATEGY,
    "provider": "local",
    "task": "search_document",
    "max_seq_length": 512,
    "model_revision": "test-revision",
}
FINGERPRINT = "a" * 64


def _records(*documents):
    return [
        {"id": f"record-{position}", "document": document, "metadata": {}}
        for position, document in enumerate(documents)
    ]


def _document_for_prefix(prefix, *, exclude=()):
    excluded = set(exclude)
    for index in range(100000):
        document = f"prefix-{prefix}-{index}"
        if document not in excluded and document_content_hash(document).startswith(prefix):
            return document
    raise AssertionError(f"could not synthesize SHA-256 prefix {prefix}")


def _write_seed(seed_dir, vectors_by_document, *, fingerprint=FINGERPRINT):
    seed_dir.mkdir(parents=True, exist_ok=True)
    by_hash = {
        document_content_hash(document): vector
        for document, vector in vectors_by_document.items()
    }
    shards = {}
    for prefix in SEED_PREFIXES:
        hashes = sorted(
            content_hash for content_hash in by_hash if content_hash.startswith(prefix)
        )
        shards[prefix] = write_embedding_seed_shard(
            seed_dir / f"{prefix}.npz",
            hashes,
            [by_hash[content_hash] for content_hash in hashes],
            expected_dimension=2,
        )
    manifest = {
        "format": SEED_FORMAT,
        "format_version": SEED_FORMAT_VERSION,
        "model_key": MODEL_KEY,
        "input_profile": PROFILE,
        "model": MODEL,
        "model_revision": PROFILE.get("model_revision"),
        "dimension": 2,
        "dtype": SEED_DTYPE,
        "content_hash_algorithm": SEED_HASH_ALGORITHM,
        "shard_scheme": SEED_SHARD_SCHEME,
        "v2_input_fingerprint": fingerprint,
        "unique_document_count": len(by_hash),
        "shards": shards,
    }
    (seed_dir / SEED_MANIFEST_FILE).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return manifest


class _CountingEmbedder:
    dim = 2

    def __init__(self):
        self.calls = []

    def embed_documents(self, documents):
        self.calls.append(list(documents))
        return [[float(len(document)), 99.0] for document in documents]


class EmbeddingSeedLoadTests(unittest.TestCase):
    def test_seed_hit_is_lazy_and_not_backfilled_into_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = "seed-only document"
            expected = array("f", [1.25, -2.5])
            _write_seed(root / "seed", {document: expected})

            vectors, stats = build_index.native_embeddings_for_records(
                _records(document),
                cache_path=root / "cache.sqlite3",
                model_key=MODEL_KEY,
                expected_dimension=2,
                embedder_factory=lambda: self.fail("seed hit loaded the model"),
                seed_dir=root / "seed",
                seed_model=MODEL,
                seed_input_profile=PROFILE,
                v2_fingerprint=FINGERPRINT,
            )

            self.assertEqual(vectors[0].tobytes(), expected.tobytes())
            self.assertEqual(stats["persistent_cache_hit_records"], 0)
            self.assertEqual(stats["seed_hit_records"], 1)
            self.assertEqual(stats["computed_records"], 0)
            with EmbeddingCache(root / "cache.sqlite3") as cache:
                self.assertEqual(
                    cache.get_many(MODEL_KEY, [document_content_hash(document)]), {}
                )

    def test_sqlite_precedes_seed_and_stats_split_all_three_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persistent_doc = "persistent document"
            seed_doc = "seed document"
            computed_doc = "computed document"
            _write_seed(
                root / "seed",
                {
                    persistent_doc: [50.0, 51.0],
                    seed_doc: [20.0, 21.0],
                },
            )
            with EmbeddingCache(root / "cache.sqlite3") as cache:
                cache.put_many(
                    MODEL_KEY,
                    {document_content_hash(persistent_doc): [10.0, 11.0]},
                    expected_dimension=2,
                )

            embedder = _CountingEmbedder()
            vectors, stats = build_index.native_embeddings_for_records(
                _records(
                    persistent_doc,
                    seed_doc,
                    computed_doc,
                    seed_doc,
                    persistent_doc,
                ),
                cache_path=root / "cache.sqlite3",
                model_key=MODEL_KEY,
                expected_dimension=2,
                embedder_factory=lambda: embedder,
                seed_dir=root / "seed",
                seed_model=MODEL,
                seed_input_profile=PROFILE,
                v2_fingerprint=FINGERPRINT,
            )

            self.assertEqual(embedder.calls, [[computed_doc]])
            self.assertEqual(list(vectors[0]), [10.0, 11.0])
            self.assertEqual(list(vectors[1]), [20.0, 21.0])
            self.assertEqual(stats["persistent_cache_hit_records"], 2)
            self.assertEqual(stats["seed_hit_records"], 2)
            self.assertEqual(stats["computed_records"], 1)
            self.assertEqual(stats["persistent_cache_hit_unique_documents"], 1)
            self.assertEqual(stats["seed_hit_unique_documents"], 1)
            self.assertEqual(stats["computed_unique_documents"], 1)
            self.assertEqual(stats["cache_hit_records"], 2)
            self.assertEqual(stats["cache_miss_records"], 3)

    def test_stale_corpus_fingerprint_reuses_exact_document_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = "unchanged across corpus revisions"
            _write_seed(root / "seed", {document: [7.0, 8.0]})

            vectors, stats = build_index.native_embeddings_for_records(
                _records(document),
                cache_path=root / "cache.sqlite3",
                model_key=MODEL_KEY,
                expected_dimension=2,
                embedder_factory=lambda: self.fail("exact stale seed hit loaded model"),
                seed_dir=root / "seed",
                seed_model=MODEL,
                seed_input_profile=PROFILE,
                v2_fingerprint="b" * 64,
            )

            self.assertEqual(list(vectors[0]), [7.0, 8.0])
            self.assertTrue(stats["seed"]["corpus_stale"])
            self.assertEqual(stats["seed_hit_records"], 1)

    def test_one_corrupt_shard_becomes_a_miss_without_hiding_good_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good_doc = _document_for_prefix("1")
            bad_doc = _document_for_prefix("e", exclude=(good_doc,))
            manifest = _write_seed(
                root / "seed", {good_doc: [1.0, 2.0], bad_doc: [3.0, 4.0]}
            )
            bad_path = root / "seed" / "e.npz"
            bad_path.write_bytes(bad_path.read_bytes() + b"corrupt")
            self.assertNotEqual(file_sha256(bad_path), manifest["shards"]["e"]["sha256"])

            embedder = _CountingEmbedder()
            with self.assertLogs("_lib.embedding_cache", level="WARNING"):
                vectors, stats = build_index.native_embeddings_for_records(
                    _records(good_doc, bad_doc),
                    cache_path=root / "cache.sqlite3",
                    model_key=MODEL_KEY,
                    expected_dimension=2,
                    embedder_factory=lambda: embedder,
                    seed_dir=root / "seed",
                    seed_model=MODEL,
                    seed_input_profile=PROFILE,
                    v2_fingerprint=FINGERPRINT,
                )

            self.assertEqual(list(vectors[0]), [1.0, 2.0])
            self.assertEqual(embedder.calls, [[bad_doc]])
            self.assertEqual(stats["seed"]["invalid_shards"], ["e"])
            self.assertEqual(stats["seed_hit_unique_documents"], 1)
            self.assertEqual(stats["computed_unique_documents"], 1)

    def test_incompatible_profile_invalidates_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = "profile-bound document"
            _write_seed(root / "seed", {document: [1.0, 2.0]})
            incompatible = dict(PROFILE, max_seq_length=8192)

            with self.assertLogs("_lib.embedding_cache", level="WARNING"):
                found, stats = load_embedding_seed(
                    root / "seed",
                    [document_content_hash(document)],
                    expected_model_key=MODEL_KEY,
                    expected_input_profile=incompatible,
                    expected_model=MODEL,
                    expected_dimension=2,
                    current_v2_fingerprint=FINGERPRINT,
                )
            self.assertEqual(found, {})
            self.assertEqual(stats["status"], "invalid")

    def test_manifest_rejects_boolean_version_and_aggregate_count_mismatch(self):
        mutations = (
            ("boolean version", lambda manifest: manifest.update(format_version=True)),
            (
                "model revision",
                lambda manifest: manifest.update(model_revision="wrong-revision"),
            ),
            (
                "aggregate count",
                lambda manifest: manifest.update(unique_document_count=999999),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                document = "strict manifest document"
                manifest = _write_seed(root / "seed", {document: [1.0, 2.0]})
                mutate(manifest)
                (root / "seed" / SEED_MANIFEST_FILE).write_text(
                    json.dumps(manifest), encoding="utf-8"
                )

                with self.assertLogs("_lib.embedding_cache", level="WARNING"):
                    found, stats = load_embedding_seed(
                        root / "seed",
                        [document_content_hash(document)],
                        expected_model_key=MODEL_KEY,
                        expected_input_profile=PROFILE,
                        expected_model=MODEL,
                        expected_dimension=2,
                        current_v2_fingerprint=FINGERPRINT,
                    )
                self.assertEqual(found, {})
                self.assertEqual(stats["status"], "invalid")

    def test_nonfinite_npz_is_rejected_after_valid_asset_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = _document_for_prefix("7")
            manifest = _write_seed(root / "seed", {document: [1.0, 2.0]})
            content_hash = document_content_hash(document)
            shard_path = root / "seed" / "7.npz"
            np.savez_compressed(
                shard_path,
                content_hashes=np.asarray([content_hash], dtype="S64"),
                embeddings=np.asarray([[np.nan, 2.0]], dtype="<f4"),
            )
            manifest["shards"]["7"]["sha256"] = file_sha256(shard_path)
            (root / "seed" / SEED_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertLogs("_lib.embedding_cache", level="WARNING"):
                found, stats = load_embedding_seed(
                    root / "seed",
                    [content_hash],
                    expected_model_key=MODEL_KEY,
                    expected_input_profile=PROFILE,
                    expected_model=MODEL,
                    expected_dimension=2,
                    current_v2_fingerprint=FINGERPRINT,
                )
            self.assertEqual(found, {})
            self.assertEqual(stats["invalid_shards"], ["7"])


class EmbeddingSeedExportTests(unittest.TestCase):
    def _spec(self):
        return {
            "provider": "local",
            "model": MODEL,
            "revision": "test-revision",
            "dimension": 2,
            "input_profile": PROFILE,
            "cache_model_key": MODEL_KEY,
        }

    def test_incomplete_cache_refuses_without_replacing_existing_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            cache_path = kb_dir / build_index.V2_EMBEDDING_CACHE
            with EmbeddingCache(cache_path):
                pass
            output = kb_dir / build_index.V2_EMBEDDING_SEED_DIR
            output.mkdir()
            sentinel = output / "old-seed"
            sentinel.write_text("preserve", encoding="utf-8")
            (output / SEED_MANIFEST_FILE).write_text(
                json.dumps(
                    {"format": SEED_FORMAT, "format_version": SEED_FORMAT_VERSION}
                ),
                encoding="utf-8",
            )
            documents = {document_content_hash("missing"): "missing"}

            with patch.object(
                exporter,
                "collect_current_v2_documents",
                return_value=(self._spec(), documents, FINGERPRINT),
            ):
                with self.assertRaisesRegex(RuntimeError, "cache is incomplete"):
                    exporter.export_seed(kb_dir, output, "local")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(any(kb_dir.glob(".embedding_seed_v2.build-*")))

    def test_output_cannot_replace_another_knowledge_base_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            unsafe_output = kb_dir / "concepts"
            unsafe_output.mkdir(parents=True)
            sentinel = unsafe_output / "card.json"
            sentinel.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be embedding_seed_v2"):
                exporter.export_seed(kb_dir, unsafe_output, "local")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_complete_small_cache_exports_and_verifies_all_16_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            documents = {
                document_content_hash("first"): "first",
                document_content_hash("second"): "second",
            }
            cache_path = kb_dir / build_index.V2_EMBEDDING_CACHE
            with EmbeddingCache(cache_path) as cache:
                cache.put_many(
                    MODEL_KEY,
                    {
                        document_content_hash("first"): [1.0, 2.0],
                        document_content_hash("second"): [3.0, 4.0],
                    },
                    expected_dimension=2,
                )
            output = kb_dir / build_index.V2_EMBEDDING_SEED_DIR

            with patch.object(
                exporter,
                "collect_current_v2_documents",
                return_value=(self._spec(), documents, FINGERPRINT),
            ):
                manifest = exporter.export_seed(kb_dir, output, "local")

            self.assertEqual(manifest["unique_document_count"], 2)
            self.assertEqual(
                {path.name for path in output.glob("*.npz")},
                {f"{prefix}.npz" for prefix in SEED_PREFIXES},
            )
            found, stats = load_embedding_seed(
                output,
                documents,
                expected_model_key=MODEL_KEY,
                expected_input_profile=PROFILE,
                expected_model=MODEL,
                expected_dimension=2,
                current_v2_fingerprint=FINGERPRINT,
                verify_all_shards=True,
            )
            self.assertEqual(len(found), 2)
            self.assertEqual(set(stats["validated_shards"]), set(SEED_PREFIXES))
            self.assertEqual(stats["invalid_shards"], [])

    def test_promotion_failure_restores_old_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "embedding_seed_v2"
            staging = root / ".embedding_seed_v2.build-test"
            output.mkdir()
            staging.mkdir()
            (output / "sentinel").write_text("old", encoding="utf-8")
            (output / SEED_MANIFEST_FILE).write_text(
                json.dumps(
                    {"format": SEED_FORMAT, "format_version": SEED_FORMAT_VERSION}
                ),
                encoding="utf-8",
            )
            (staging / "sentinel").write_text("new", encoding="utf-8")
            real_replace = Path.replace

            def fail_staging_replace(path, target):
                if path == staging:
                    raise OSError("synthetic promotion failure")
                return real_replace(path, target)

            with patch.object(Path, "replace", new=fail_staging_replace):
                with self.assertRaisesRegex(OSError, "synthetic"):
                    exporter.promote_seed_directory(staging, output)

            self.assertEqual((output / "sentinel").read_text(encoding="utf-8"), "old")
            self.assertTrue(staging.is_dir())
            self.assertFalse(any(root.glob(".embedding_seed_v2.backup-*")))

    def test_unique_interrupted_backup_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "embedding_seed_v2"
            backup = root / ".embedding_seed_v2.backup-interrupted"
            backup.mkdir()
            (backup / SEED_MANIFEST_FILE).write_text(
                json.dumps(
                    {"format": SEED_FORMAT, "format_version": SEED_FORMAT_VERSION}
                ),
                encoding="utf-8",
            )
            (backup / "sentinel").write_text("recover", encoding="utf-8")

            with self.assertLogs(exporter.log, level="WARNING"):
                self.assertTrue(exporter.recover_interrupted_seed(output))

            self.assertEqual((output / "sentinel").read_text(encoding="utf-8"), "recover")
            self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
