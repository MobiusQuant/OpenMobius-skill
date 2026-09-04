import io
import json
import signal
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_index  # noqa: E402
import install  # noqa: E402
from scripts import kb_doctor  # noqa: E402


def _fake_chromadb(counts, metadata_overrides=None):
    layers = {
        "knowledge_base": "legacy",
        "school_knowledge_v2": "school",
        "source_evidence_v2": "evidence",
    }
    metadata_overrides = metadata_overrides or {}

    class Collection:
        def __init__(self, name):
            self.name = name
            self.metadata = {
                "kb_schema_version": build_index.INDEX_SCHEMA_VERSION,
                "layer": layers[name],
                **metadata_overrides.get(name, {}),
            }

        def count(self):
            return counts[self.name]

    class Client:
        def list_collections(self):
            return [Collection(name) for name in counts]

        def get_collection(self, name):
            return Collection(name)

    return types.SimpleNamespace(PersistentClient=lambda path: Client())


def _valid_manifest(legacy_count=2, school_count=1, evidence_count=3):
    counts = {
        "knowledge_base": (legacy_count, "legacy"),
        "school_knowledge_v2": (school_count, "school"),
        "source_evidence_v2": (evidence_count, "evidence"),
    }
    return {
        "manifest_version": build_index.INDEX_MANIFEST_VERSION,
        "index_schema_version": build_index.INDEX_SCHEMA_VERSION,
        "v2_input_fingerprint": "a" * 64,
        "canonical_input_fingerprint": "b" * 64,
        "embedding_model": build_index.EXPECTED_MODEL,
        "embedding_model_revision": build_index.EXPECTED_MODEL_REVISION,
        "embedding_dimension": build_index.EXPECTED_DIM,
        "canonical_embedding_input_profile": (
            build_index.CANONICAL_EMBEDDING_INPUT_PROFILE
        ),
        "v2_embedding_input_profile": build_index.v2_input_profile(
            build_index.V2_NATIVE_STRATEGY, "local"
        ),
        "embedding_strategy": {
            "knowledge_base": "bundled_card_embeddings",
            "school_knowledge_v2": build_index.V2_NATIVE_STRATEGY,
            "source_evidence_v2": build_index.V2_NATIVE_STRATEGY,
        },
        "embedding_models": {
            "knowledge_base": build_index.EXPECTED_MODEL,
            "school_knowledge_v2": build_index.EXPECTED_MODEL,
            "source_evidence_v2": build_index.EXPECTED_MODEL,
        },
        "embedding_revisions": {
            "knowledge_base": build_index.EXPECTED_MODEL_REVISION,
            "school_knowledge_v2": build_index.EXPECTED_MODEL_REVISION,
            "source_evidence_v2": build_index.EXPECTED_MODEL_REVISION,
        },
        "embedding_dimensions": {
            "knowledge_base": build_index.EXPECTED_DIM,
            "school_knowledge_v2": build_index.EXPECTED_DIM,
            "source_evidence_v2": build_index.EXPECTED_DIM,
        },
        "collections": {
            name: {
                "count": count,
                "schema_version": build_index.INDEX_SCHEMA_VERSION,
                "layer": layer,
                "created": name == "knowledge_base" or count > 0,
            }
            for name, (count, layer) in counts.items()
        },
    }


class V2RecordContractTests(unittest.TestCase):
    def test_metadata_value_counts_are_sorted_and_ignore_non_strings(self):
        records = [
            {"metadata": {"school": "SMC"}},
            {"metadata": {"school": "ICT"}},
            {"metadata": {"school": "SMC"}},
            {"metadata": {"school": "  "}},
            {"metadata": {"school": None}},
        ]

        self.assertEqual(
            build_index.metadata_value_counts(records, "school"),
            {"ICT": 1, "SMC": 2},
        )

    def test_missing_v2_registry_preserves_legacy_build_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            schools, evidence, stats = build_index.load_optional_v2_records(Path(tmp))

        self.assertEqual(schools, [])
        self.assertEqual(evidence, [])
        self.assertFalse(stats["available"])
        self.assertEqual(stats["reason"], "v2_data_missing")

    def test_school_record_is_scalar_and_contract_fields_are_forced(self):
        records = build_index.normalize_v2_records(
            [
                {
                    "id": "fvg::ict",
                    "document": "ICT fair value gap",
                    "metadata": {
                        "canonical_id": "fair_value_gap",
                        "type": "concept",
                        "term": "Fair Value Gap",
                        "school": "ICT",
                        "file_path": "concepts/fair_value_gap.json",
                        "source_names": ["ICT Mentorship"],
                        "source_collection_count": 1,
                    },
                }
            ],
            layer="school",
        )

        self.assertEqual(records[0]["metadata"]["record_id"], "fvg::ict")
        self.assertEqual(records[0]["metadata"]["layer"], "school")
        self.assertEqual(records[0]["metadata"]["schema_version"], 2)
        self.assertEqual(
            json.loads(records[0]["metadata"]["source_names"]),
            ["ICT Mentorship"],
        )

    def test_evidence_requires_exact_source(self):
        with self.assertRaisesRegex(ValueError, "source"):
            build_index.normalize_v2_records(
                [
                    {
                        "id": "evidence-1",
                        "document": "A rule",
                        "metadata": {
                            "canonical_id": "fair_value_gap",
                            "type": "concept",
                            "term": "Fair Value Gap",
                            "school": "ICT",
                            "file_path": "concepts/fair_value_gap.json",
                            "ref": "definition_per_source.ICT Mentorship",
                            "content_type": "definition",
                        },
                    }
                ],
                layer="evidence",
            )

    def test_duplicate_ids_fail_before_chroma_write(self):
        record = {
            "id": "same",
            "document": "text",
            "metadata": {
                "canonical_id": "cid",
                "type": "concept",
                "term": "Term",
                "school": "ICT",
                "file_path": "concepts/cid.json",
                "source_names": [],
                "source_collection_count": 0,
            },
        }
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_index.normalize_v2_records([record, record], layer="school")

    def test_v2_embeddings_are_inherited_from_canonical_card(self):
        vector = [0.1, 0.2]
        mapping = build_index.canonical_embedding_map(
            [
                {
                    "id": "fair_value_gap",
                    "file_path": "concepts/fair_value_gap.json",
                    "card": {
                        "global_card_id": "fair_value_gap",
                        "card_id": "fair_value_gap",
                    },
                }
            ],
            [vector],
        )
        records = [
            {
                "id": "fvg::ict",
                "document": "text",
                "metadata": {"canonical_id": "fair_value_gap"},
            }
        ]
        self.assertEqual(
            build_index.embeddings_for_v2_records(records, mapping),
            [vector],
        )

    def test_fingerprint_is_independent_of_record_order(self):
        first = {"id": "a", "document": "a", "metadata": {"school": "ICT"}}
        second = {"id": "b", "document": "b", "metadata": {"school": "SMC"}}
        self.assertEqual(
            build_index.fingerprint_v2_records([first, second], []),
            build_index.fingerprint_v2_records([second, first], []),
        )

    def test_canonical_fingerprint_covers_text_and_parent_embedding(self):
        item = {
            "id": "cid",
            "type": "concept",
            "file_path": "concepts/cid.json",
            "text": "original text",
            "card": {
                "_embedding_model": "model",
                "_embedding": [0.1, 0.2],
            },
        }
        original = build_index.fingerprint_canonical_items([item])
        changed_text = {
            **item,
            "text": "changed text",
        }
        changed_embedding = {
            **item,
            "card": {
                **item["card"],
                "_embedding": [0.1, 0.3],
            },
        }
        changed_metadata = {
            **item,
            "card": {
                **item["card"],
                "school": "SMC",
            },
        }

        self.assertNotEqual(
            original,
            build_index.fingerprint_canonical_items([changed_text]),
        )
        self.assertNotEqual(
            original,
            build_index.fingerprint_canonical_items([changed_embedding]),
        )
        self.assertNotEqual(
            original,
            build_index.fingerprint_canonical_items([changed_metadata]),
        )


class CardCollectionIntegrityTests(unittest.TestCase):
    @staticmethod
    def _write_card(kb_dir: Path, directory: str, name: str, payload) -> Path:
        path = kb_dir / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_malformed_non_object_and_empty_cards_fail_closed(self):
        fixtures = (
            ("concepts", "broken.json", '{"canonical_term":', "invalid concept card"),
            ("concepts", "array.json", "[]", "top-level JSON value must be an object"),
            ("cases", "empty.json", "{}", "card produces empty retrieval text"),
        )
        for directory, name, contents, expected in fixtures:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                kb_dir = Path(tmp) / "knowledge_base"
                path = kb_dir / directory / name
                path.parent.mkdir(parents=True)
                path.write_text(contents, encoding="utf-8")

                with self.assertRaisesRegex(
                    build_index.KnowledgeCardLoadError, expected
                ) as caught:
                    build_index.collect_cards(kb_dir)

                self.assertIn(f"{directory}/{name}", str(caught.exception))

    def test_cli_validation_failure_preserves_existing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            self._write_card(
                kb_dir,
                "concepts",
                "good.json",
                {
                    "canonical_term": "Good",
                    "definition": "Valid source card",
                },
            )
            broken = kb_dir / "concepts" / "truncated.json"
            broken.write_text('{"canonical_term":', encoding="utf-8")
            index_dir = kb_dir / "_index"
            index_dir.mkdir()
            live_database = index_dir / "chroma.sqlite3"
            live_database.write_bytes(b"existing-live-index")

            with (
                patch.object(
                    sys,
                    "argv",
                    ["build_index.py", "--kb", str(kb_dir), "--force"],
                ),
                self.assertLogs(build_index.log, level="ERROR") as logs,
            ):
                result = build_index.main()

            self.assertEqual(result, 1)
            self.assertEqual(live_database.read_bytes(), b"existing-live-index")
            self.assertFalse(any(kb_dir.glob("._index.build-*")))
            self.assertIn(
                "existing index was not changed",
                "\n".join(logs.output),
            )


class RegeneratedCardAtomicityTests(unittest.TestCase):
    @staticmethod
    def _card_item(path: str, value: str) -> dict:
        return {
            "id": Path(path).stem,
            "type": "concept",
            "file_path": path,
            "text": value,
            "card": {
                "canonical_term": value,
                "definition": value,
                "_embedding_model": "replacement-model",
                "_embedding": [0.25, 0.5],
            },
        }

    def test_index_promotion_failure_rolls_back_every_regenerated_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            concepts = kb_dir / "concepts"
            concepts.mkdir(parents=True)
            originals = {
                "concepts/a.json": b'{"original":"a"}\n',
                "concepts/b.json": b'{"original":"b"}\n',
            }
            for relative, contents in originals.items():
                (kb_dir / relative).write_bytes(contents)
            items = [
                self._card_item("concepts/a.json", "new-a"),
                self._card_item("concepts/b.json", "new-b"),
            ]
            staged_cards = build_index.stage_regenerated_cards(kb_dir, items)
            live_index = kb_dir / "_index"
            staged_index = kb_dir / "._index.build-test"
            live_index.mkdir()
            staged_index.mkdir()
            (live_index / "chroma.sqlite3").write_bytes(b"old-index")
            (staged_index / "chroma.sqlite3").write_bytes(b"new-index")

            with (
                patch.object(
                    build_index,
                    "_promote_regeneration_index",
                    side_effect=OSError("injected index promotion failure"),
                ),
                self.assertRaisesRegex(OSError, "injected index promotion failure"),
            ):
                build_index.commit_regenerated_build(
                    staged_index,
                    live_index,
                    staged_cards_dir=staged_cards,
                    kb_dir=kb_dir,
                    card_paths=list(originals),
                )

            for relative, contents in originals.items():
                self.assertEqual((kb_dir / relative).read_bytes(), contents)
            self.assertEqual(
                (live_index / "chroma.sqlite3").read_bytes(), b"old-index"
            )
            self.assertFalse(any(kb_dir.glob("._cards.backup-*")))

    def test_index_rename_failure_restores_live_index_and_regenerated_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            concepts = kb_dir / "concepts"
            concepts.mkdir(parents=True)
            card_path = concepts / "a.json"
            original_card = b'{"original":"a"}\n'
            card_path.write_bytes(original_card)
            items = [self._card_item("concepts/a.json", "new-a")]
            staged_cards = build_index.stage_regenerated_cards(kb_dir, items)
            live_index = kb_dir / "_index"
            staged_index = kb_dir / "._index.build-test"
            live_index.mkdir()
            staged_index.mkdir()
            live_database = live_index / "chroma.sqlite3"
            live_database.write_bytes(b"old-index")
            (staged_index / "chroma.sqlite3").write_bytes(b"new-index")
            real_replace = Path.replace

            def fail_staged_index_rename(source, target):
                if Path(source) == staged_index and Path(target) == live_index:
                    raise OSError("injected staged index rename failure")
                return real_replace(source, target)

            with (
                patch.object(Path, "replace", fail_staged_index_rename),
                self.assertRaisesRegex(OSError, "staged index rename failure"),
            ):
                build_index.commit_regenerated_build(
                    staged_index,
                    live_index,
                    staged_cards_dir=staged_cards,
                    kb_dir=kb_dir,
                    card_paths=["concepts/a.json"],
                )

            self.assertEqual(card_path.read_bytes(), original_card)
            self.assertEqual(live_database.read_bytes(), b"old-index")
            self.assertFalse(staged_index.exists())
            self.assertFalse(any(kb_dir.glob("._cards.backup-*")))
            self.assertFalse(any(kb_dir.glob("._index.backup-*")))

    def test_success_promotes_matching_cards_and_index_and_removes_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            concepts = kb_dir / "concepts"
            concepts.mkdir(parents=True)
            card_path = concepts / "a.json"
            card_path.write_bytes(b'{"original":"a"}\n')
            items = [self._card_item("concepts/a.json", "new-a")]
            staged_cards = build_index.stage_regenerated_cards(kb_dir, items)
            expected_card = (staged_cards / "concepts" / "a.json").read_bytes()
            live_index = kb_dir / "_index"
            staged_index = kb_dir / "._index.build-test"
            live_index.mkdir()
            staged_index.mkdir()
            (live_index / "chroma.sqlite3").write_bytes(b"old-index")
            (staged_index / "chroma.sqlite3").write_bytes(b"new-index")

            build_index.commit_regenerated_build(
                staged_index,
                live_index,
                staged_cards_dir=staged_cards,
                kb_dir=kb_dir,
                card_paths=["concepts/a.json"],
            )

            self.assertEqual(card_path.read_bytes(), expected_card)
            self.assertEqual(
                (live_index / "chroma.sqlite3").read_bytes(), b"new-index"
            )
            self.assertFalse(
                (live_index / build_index.REGEN_INDEX_MARKER_FILE).exists()
            )
            self.assertFalse(any(kb_dir.glob("._cards.backup-*")))
            self.assertFalse(any(kb_dir.glob("._index.backup-*")))

    def test_mid_card_promotion_failure_restores_prior_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            concepts = kb_dir / "concepts"
            concepts.mkdir(parents=True)
            originals = {
                "concepts/a.json": b"original-a",
                "concepts/b.json": b"original-b",
            }
            for relative, contents in originals.items():
                (kb_dir / relative).write_bytes(contents)
            items = [
                self._card_item("concepts/a.json", "new-a"),
                self._card_item("concepts/b.json", "new-b"),
            ]
            staged_cards = build_index.stage_regenerated_cards(kb_dir, items)
            failing_source = staged_cards / "concepts" / "b.json"
            real_replace = Path.replace

            def replace_with_one_failure(source, target):
                if Path(source) == failing_source:
                    raise OSError("injected card promotion failure")
                return real_replace(source, target)

            with (
                patch.object(Path, "replace", replace_with_one_failure),
                self.assertRaisesRegex(OSError, "injected card promotion failure"),
            ):
                build_index.begin_card_promotion(
                    staged_cards,
                    kb_dir,
                    list(originals),
                )

            for relative, contents in originals.items():
                self.assertEqual((kb_dir / relative).read_bytes(), contents)
            self.assertFalse(any(kb_dir.glob("._cards.backup-*")))

    def test_chroma_staging_failure_leaves_card_and_live_index_bytes_unchanged(self):
        class FakeEmbedder:
            dim = build_index.EXPECTED_DIM
            model_name = "replacement-model"

            def embed_documents(self, texts):
                return [[0.25] * self.dim for _text in texts]

        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            concepts = kb_dir / "concepts"
            concepts.mkdir(parents=True)
            card_path = concepts / "one.json"
            card_path.write_text(
                json.dumps(
                    {
                        "canonical_term": "One",
                        "definition": "Original",
                        "_embedding_model": build_index.EXPECTED_MODEL,
                        "_embedding": [0.1] * build_index.EXPECTED_DIM,
                    }
                ),
                encoding="utf-8",
            )
            original_card = card_path.read_bytes()
            live_index = kb_dir / "_index"
            live_index.mkdir()
            live_database = live_index / "chroma.sqlite3"
            live_database.write_bytes(b"old-index")
            failing_chromadb = types.SimpleNamespace(
                PersistentClient=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("injected Chroma staging failure")
                )
            )

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "build_index.py",
                        "--kb",
                        str(kb_dir),
                        "--regenerate",
                        "--force",
                    ],
                ),
                patch("_lib.embedder.get_embedder", return_value=FakeEmbedder()),
                patch.dict(sys.modules, {"chromadb": failing_chromadb}),
                self.assertLogs(build_index.log, level="ERROR"),
            ):
                result = build_index.main()

            self.assertEqual(result, 1)
            self.assertEqual(card_path.read_bytes(), original_card)
            self.assertEqual(live_database.read_bytes(), b"old-index")
            self.assertFalse(any(kb_dir.glob("._cards.*-*")))
            self.assertFalse(any(kb_dir.glob("._index.build-*")))


class _SimulatedProcessCrash(BaseException):
    """Bypass in-process Exception handlers like SIGKILL would."""


class RegenerationCrashRecoveryTests(unittest.TestCase):
    @staticmethod
    def _card_item(path: str, value: str) -> dict:
        return RegeneratedCardAtomicityTests._card_item(path, value)

    def _workspace(self, root: Path, *, had_live_index: bool = True) -> dict:
        kb_dir = root / "knowledge_base"
        concepts = kb_dir / "concepts"
        concepts.mkdir(parents=True)
        originals = {
            "concepts/a.json": b'{"original":"a"}\n',
            "concepts/b.json": b'{"original":"b"}\n',
        }
        for relative, value in originals.items():
            (kb_dir / relative).write_bytes(value)
        staged_cards = build_index.stage_regenerated_cards(
            kb_dir,
            [
                self._card_item("concepts/a.json", "new-a"),
                self._card_item("concepts/b.json", "new-b"),
            ],
        )
        regenerated = {
            relative: (staged_cards / relative).read_bytes()
            for relative in originals
        }
        live_index = kb_dir / "_index"
        if had_live_index:
            live_index.mkdir()
            (live_index / "chroma.sqlite3").write_bytes(b"old-index")
            segment = live_index / "old-segment"
            segment.mkdir()
            (segment / "data.bin").write_bytes(b"old-segment-data")
        staged_index = kb_dir / "._index.build-crash-test"
        staged_index.mkdir()
        (staged_index / "chroma.sqlite3").write_bytes(b"new-index")
        segment = staged_index / "new-segment"
        segment.mkdir()
        (segment / "data.bin").write_bytes(b"new-segment-data")
        return {
            "kb": kb_dir,
            "originals": originals,
            "regenerated": regenerated,
            "staged_cards": staged_cards,
            "live_index": live_index,
            "staged_index": staged_index,
            "had_live_index": had_live_index,
        }

    @staticmethod
    def _commit(workspace: dict) -> None:
        build_index.commit_regenerated_build(
            workspace["staged_index"],
            workspace["live_index"],
            staged_cards_dir=workspace["staged_cards"],
            kb_dir=workspace["kb"],
            card_paths=list(workspace["originals"]),
        )

    @staticmethod
    def _make_unpublished_transaction(workspace: dict):
        transaction = build_index._prepare_regeneration_transaction(
            workspace["staged_index"],
            workspace["live_index"],
            staged_cards_dir=workspace["staged_cards"],
            kb_dir=workspace["kb"],
            card_paths=list(workspace["originals"]),
        )
        preparing = workspace["kb"] / (
            build_index.REGEN_PREPARE_PREFIX
            + transaction.journal["transaction_id"]
        )
        transaction.root.replace(preparing)
        transaction.root = preparing
        transaction.card_backup_dir = preparing / "cards"
        return transaction

    def _assert_old(self, workspace: dict) -> None:
        for relative, value in workspace["originals"].items():
            self.assertEqual((workspace["kb"] / relative).read_bytes(), value)
        if workspace["had_live_index"]:
            self.assertEqual(
                (workspace["live_index"] / "chroma.sqlite3").read_bytes(),
                b"old-index",
            )
            self.assertEqual(
                (workspace["live_index"] / "old-segment" / "data.bin").read_bytes(),
                b"old-segment-data",
            )
        else:
            self.assertFalse(workspace["live_index"].exists())

    def _assert_new(self, workspace: dict) -> None:
        for relative, value in workspace["regenerated"].items():
            self.assertEqual((workspace["kb"] / relative).read_bytes(), value)
        self.assertEqual(
            (workspace["live_index"] / "chroma.sqlite3").read_bytes(),
            b"new-index",
        )
        self.assertEqual(
            (workspace["live_index"] / "new-segment" / "data.bin").read_bytes(),
            b"new-segment-data",
        )

    def _assert_clean(self, workspace: dict) -> None:
        kb_dir = workspace["kb"]
        self.assertFalse((kb_dir / build_index.REGEN_TRANSACTION_DIR).exists())
        self.assertFalse(any(kb_dir.glob(f"{build_index.REGEN_PREPARE_PREFIX}*")))
        self.assertFalse(any(kb_dir.glob("._cards.backup-*")))
        self.assertFalse(any(kb_dir.glob("._cards.build-cleanup-*")))
        self.assertFalse(any(kb_dir.glob("._index.backup-*")))
        self.assertFalse(
            (workspace["live_index"] / build_index.REGEN_INDEX_MARKER_FILE).exists()
        )

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
        snapshot: dict[str, tuple[str, bytes | str | None]] = {}
        for candidate in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                snapshot[relative] = ("link", str(candidate.readlink()))
            elif candidate.is_dir():
                snapshot[relative] = ("dir", None)
            else:
                snapshot[relative] = ("file", candidate.read_bytes())
        return snapshot

    def _recover_twice(self, workspace: dict) -> None:
        with self.assertLogs(build_index.log, level="WARNING"):
            self.assertEqual(
                build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                ),
                (True, True),
            )
        self.assertEqual(
            build_index.recover_interrupted_regeneration(
                workspace["kb"], workspace["live_index"]
            ),
            (True, False),
        )

    def test_crash_in_card_loop_rolls_back_all_cards_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            failing_source = workspace["staged_cards"] / "concepts" / "b.json"
            real_replace = Path.replace

            def crash_before_second_card(source, target):
                if Path(source) == failing_source:
                    raise _SimulatedProcessCrash("card loop interrupted")
                return real_replace(source, target)

            with patch.object(Path, "replace", crash_before_second_card), self.assertRaises(
                _SimulatedProcessCrash
            ):
                self._commit(workspace)

            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_crash_after_old_index_backup_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            real_replace = Path.replace

            def crash_before_new_index(source, target):
                if (
                    Path(source) == workspace["staged_index"]
                    and Path(target) == workspace["live_index"]
                ):
                    raise _SimulatedProcessCrash("index promotion interrupted")
                return real_replace(source, target)

            with patch.object(Path, "replace", crash_before_new_index), self.assertRaises(
                _SimulatedProcessCrash
            ):
                self._commit(workspace)

            self.assertFalse(workspace["live_index"].exists())
            self.assertTrue(any(workspace["kb"].glob("._index.backup-regenerate-*")))
            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_crash_with_new_index_live_but_uncommitted_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            real_write = build_index._write_regeneration_journal

            def crash_before_commit_marker(transaction, phase):
                if phase == "committed":
                    raise _SimulatedProcessCrash("commit marker interrupted")
                return real_write(transaction, phase)

            with patch.object(
                build_index,
                "_write_regeneration_journal",
                crash_before_commit_marker,
            ), self.assertRaises(_SimulatedProcessCrash):
                self._commit(workspace)

            self._assert_new(workspace)
            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is POSIX-only")
    def test_real_sigkill_process_is_recovered_on_next_process_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            payload = json.dumps(
                {
                    "kb": str(workspace["kb"]),
                    "live": str(workspace["live_index"]),
                    "staged_index": str(workspace["staged_index"]),
                    "staged_cards": str(workspace["staged_cards"]),
                    "card_paths": list(workspace["originals"]),
                }
            )
            child = """
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "scripts"))
import build_index
payload = json.loads(sys.argv[1])
real_write = build_index._write_regeneration_journal
def kill_before_committed(transaction, phase):
    if phase == "committed":
        os.kill(os.getpid(), signal.SIGKILL)
    return real_write(transaction, phase)
with patch.object(build_index, "_write_regeneration_journal", kill_before_committed):
    build_index.commit_regenerated_build(
        Path(payload["staged_index"]),
        Path(payload["live"]),
        staged_cards_dir=Path(payload["staged_cards"]),
        kb_dir=Path(payload["kb"]),
        card_paths=payload["card_paths"],
    )
"""
            process = subprocess.run(
                [sys.executable, "-c", child, payload],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, -signal.SIGKILL)

            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is POSIX-only")
    def test_sigkill_before_active_publish_cleans_verified_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            payload = json.dumps(
                {
                    "kb": str(workspace["kb"]),
                    "live": str(workspace["live_index"]),
                    "staged_index": str(workspace["staged_index"]),
                    "staged_cards": str(workspace["staged_cards"]),
                    "card_paths": list(workspace["originals"]),
                }
            )
            child = """
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "scripts"))
import build_index
payload = json.loads(sys.argv[1])
active = Path(payload["kb"]) / build_index.REGEN_TRANSACTION_DIR
real_replace = Path.replace
def kill_before_publish(source, target):
    if Path(target) == active:
        os.kill(os.getpid(), signal.SIGKILL)
    return real_replace(source, target)
with patch.object(Path, "replace", kill_before_publish):
    build_index.commit_regenerated_build(
        Path(payload["staged_index"]),
        Path(payload["live"]),
        staged_cards_dir=Path(payload["staged_cards"]),
        kb_dir=Path(payload["kb"]),
        card_paths=payload["card_paths"],
    )
"""
            process = subprocess.run(
                [sys.executable, "-c", child, payload],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertFalse(
                (workspace["kb"] / build_index.REGEN_TRANSACTION_DIR).exists()
            )
            candidates = list(
                workspace["kb"].glob(f"{build_index.REGEN_PREPARE_PREFIX}*")
            )
            self.assertEqual(len(candidates), 1)
            self.assertTrue(
                (candidates[0] / build_index.REGEN_JOURNAL_FILE).is_file()
            )
            self.assertTrue(
                (
                    workspace["staged_index"]
                    / build_index.REGEN_INDEX_MARKER_FILE
                ).is_file()
            )
            self._assert_old(workspace)

            with self.assertLogs(build_index.log, level="WARNING"):
                self.assertEqual(
                    build_index.recover_interrupted_regeneration(
                        workspace["kb"], workspace["live_index"]
                    ),
                    (True, True),
                )
            self._assert_old(workspace)
            self._assert_clean(workspace)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is POSIX-only")
    def test_sigkill_before_first_journal_never_publishes_empty_active_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            payload = json.dumps(
                {
                    "kb": str(workspace["kb"]),
                    "live": str(workspace["live_index"]),
                    "staged_index": str(workspace["staged_index"]),
                    "staged_cards": str(workspace["staged_cards"]),
                    "card_paths": list(workspace["originals"]),
                }
            )
            child = """
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "scripts"))
import build_index
payload = json.loads(sys.argv[1])
def kill_before_journal(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
with patch.object(build_index, "_write_regeneration_journal", kill_before_journal):
    build_index.commit_regenerated_build(
        Path(payload["staged_index"]),
        Path(payload["live"]),
        staged_cards_dir=Path(payload["staged_cards"]),
        kb_dir=Path(payload["kb"]),
        card_paths=payload["card_paths"],
    )
"""
            process = subprocess.run(
                [sys.executable, "-c", child, payload],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertFalse(
                (workspace["kb"] / build_index.REGEN_TRANSACTION_DIR).exists()
            )
            self.assertFalse(
                (
                    workspace["staged_index"]
                    / build_index.REGEN_INDEX_MARKER_FILE
                ).exists()
            )
            self._assert_old(workspace)
            with self.assertLogs(build_index.log, level="WARNING"):
                self.assertEqual(
                    build_index.recover_interrupted_regeneration(
                        workspace["kb"], workspace["live_index"]
                    ),
                    (True, False),
                )

            # Unverifiable empty staging is reported, but cannot permanently
            # block a retry or be mistaken for an active transaction.
            self._commit(workspace)
            self._assert_new(workspace)
            self.assertFalse(
                (workspace["kb"] / build_index.REGEN_TRANSACTION_DIR).exists()
            )
            self.assertFalse(
                (
                    workspace["live_index"]
                    / build_index.REGEN_INDEX_MARKER_FILE
                ).exists()
            )

    def test_publish_rename_then_exception_preserves_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            active = workspace["kb"] / build_index.REGEN_TRANSACTION_DIR
            real_replace = Path.replace

            def crash_after_publish(source, target):
                result = real_replace(source, target)
                if Path(target) == active:
                    raise _SimulatedProcessCrash("publish completed before exception")
                return result

            with patch.object(Path, "replace", crash_after_publish), self.assertRaises(
                _SimulatedProcessCrash
            ):
                self._commit(workspace)

            self.assertTrue((active / build_index.REGEN_JOURNAL_FILE).is_file())
            self.assertTrue(
                (
                    workspace["staged_index"]
                    / build_index.REGEN_INDEX_MARKER_FILE
                ).is_file()
            )
            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_unpublished_cleanup_is_restartable_at_every_durable_cut(self):
        for crash_point in (
            "cleanup-journal",
            "staged-cards-move",
            "staged-index-move",
            "retire-move",
        ):
            with self.subTest(crash_point=crash_point), tempfile.TemporaryDirectory() as tmp:
                workspace = self._workspace(Path(tmp))
                self._make_unpublished_transaction(workspace)

                if crash_point == "cleanup-journal":
                    real_write = build_index._write_regeneration_journal

                    def crash_after_cleanup_journal(transaction, phase):
                        result = real_write(transaction, phase)
                        if phase == "unpublished_cleanup":
                            raise _SimulatedProcessCrash("cleanup journal durable")
                        return result

                    context = patch.object(
                        build_index,
                        "_write_regeneration_journal",
                        crash_after_cleanup_journal,
                    )
                elif crash_point in {"staged-cards-move", "staged-index-move"}:
                    real_move = build_index._move_directory
                    target_label = (
                        "unpublished staged cards"
                        if crash_point == "staged-cards-move"
                        else "unpublished staged index"
                    )

                    def crash_after_cleanup_move(source, destination, *, label):
                        result = real_move(source, destination, label=label)
                        if label == target_label:
                            raise _SimulatedProcessCrash(f"{label} durable")
                        return result

                    context = patch.object(
                        build_index, "_move_directory", crash_after_cleanup_move
                    )
                else:
                    def crash_after_retire_move(candidate, kb_dir):
                        raise _SimulatedProcessCrash(
                            f"retired before cleanup: {candidate}"
                        )

                    context = patch.object(
                        build_index,
                        "_remove_retired_regeneration_transaction",
                        crash_after_retire_move,
                    )

                with context, self.assertRaises(_SimulatedProcessCrash):
                    build_index.recover_interrupted_regeneration(
                        workspace["kb"], workspace["live_index"]
                    )

                if crash_point == "retire-move":
                    recovered = build_index.recover_interrupted_regeneration(
                        workspace["kb"], workspace["live_index"]
                    )
                    self.assertEqual(recovered, (True, False))
                else:
                    with self.assertLogs(build_index.log, level="WARNING"):
                        self.assertEqual(
                            build_index.recover_interrupted_regeneration(
                                workspace["kb"], workspace["live_index"]
                            ),
                            (True, True),
                        )
                self.assertEqual(
                    build_index.recover_interrupted_regeneration(
                        workspace["kb"], workspace["live_index"]
                    ),
                    (True, False),
                )
                self._assert_old(workspace)
                self._assert_clean(workspace)

    def test_second_process_cannot_recover_a_live_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            with build_index.knowledge_base_build_lock(workspace["kb"]):
                transaction = build_index._prepare_regeneration_transaction(
                    workspace["staged_index"],
                    workspace["live_index"],
                    staged_cards_dir=workspace["staged_cards"],
                    kb_dir=workspace["kb"],
                    card_paths=list(workspace["originals"]),
                )
                build_index.begin_card_promotion(
                    workspace["staged_cards"],
                    workspace["kb"],
                    list(workspace["originals"]),
                    backup_dir=transaction.card_backup_dir,
                    rollback_on_error=False,
                )
                build_index._write_regeneration_journal(
                    transaction, "cards_promoted"
                )
                before = self._snapshot(workspace["kb"])
                child = """
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import build_index
print(json.dumps(build_index.recover_interrupted_regeneration(
    Path(sys.argv[1]), Path(sys.argv[2])
)))
"""
                process = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        child,
                        str(workspace["kb"]),
                        str(workspace["live_index"]),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(json.loads(process.stdout), [False, False])
                self.assertIn("another build", process.stderr)
                self.assertEqual(self._snapshot(workspace["kb"]), before)

            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_committed_crash_before_cleanup_keeps_new_pair_and_removes_marker(self):
        for had_live_index in (True, False):
            with self.subTest(had_live_index=had_live_index), tempfile.TemporaryDirectory() as tmp:
                workspace = self._workspace(
                    Path(tmp), had_live_index=had_live_index
                )
                with patch.object(
                    build_index,
                    "_finalize_committed_regeneration",
                    side_effect=_SimulatedProcessCrash("cleanup interrupted"),
                ), self.assertRaises(_SimulatedProcessCrash):
                    self._commit(workspace)

                self._recover_twice(workspace)
                self._assert_new(workspace)
                self._assert_clean(workspace)

    def test_committed_cleanup_is_restartable_at_each_move_and_before_retire(self):
        for crash_point in ("obsolete index backup", "obsolete staged cards", "retire"):
            with self.subTest(crash_point=crash_point), tempfile.TemporaryDirectory() as tmp:
                workspace = self._workspace(Path(tmp))
                if crash_point == "retire":
                    context = patch.object(
                        build_index,
                        "_retire_regeneration_transaction",
                        side_effect=_SimulatedProcessCrash("retire interrupted"),
                    )
                else:
                    real_move = build_index._move_directory

                    def crash_after_move(source, destination, *, label):
                        real_move(source, destination, label=label)
                        if label == crash_point:
                            raise _SimulatedProcessCrash(f"{label} interrupted")

                    context = patch.object(
                        build_index, "_move_directory", crash_after_move
                    )
                with context, self.assertRaises(_SimulatedProcessCrash):
                    self._commit(workspace)

                self._recover_twice(workspace)
                self._assert_new(workspace)
                self._assert_clean(workspace)

    def test_verified_retired_cleanup_failure_does_not_block_live_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            with patch.object(
                build_index,
                "_remove_retired_regeneration_transaction",
                side_effect=OSError("cleanup temporarily unavailable"),
            ), self.assertLogs(build_index.log, level="WARNING"):
                self._commit(workspace)

            self._assert_new(workspace)
            self.assertFalse(
                (workspace["live_index"] / build_index.REGEN_INDEX_MARKER_FILE).exists()
            )
            self.assertTrue(any(workspace["kb"].glob("._cards.build-cleanup-*")))
            self.assertEqual(
                build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                ),
                (True, False),
            )
            self._assert_new(workspace)
            self._assert_clean(workspace)

    def test_crash_without_old_index_rolls_back_to_absent_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp), had_live_index=False)
            real_write = build_index._write_regeneration_journal

            def crash_before_commit_marker(transaction, phase):
                if phase == "committed":
                    raise _SimulatedProcessCrash("commit interrupted")
                return real_write(transaction, phase)

            with patch.object(
                build_index,
                "_write_regeneration_journal",
                crash_before_commit_marker,
            ), self.assertRaises(_SimulatedProcessCrash):
                self._commit(workspace)

            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_prepared_crash_without_old_index_rolls_back_to_absent_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp), had_live_index=False)
            with patch.object(
                build_index,
                "begin_card_promotion",
                side_effect=_SimulatedProcessCrash("before card promotion"),
            ), self.assertRaises(_SimulatedProcessCrash):
                self._commit(workspace)

            self._recover_twice(workspace)
            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_unknown_live_card_fails_closed_without_mutating_any_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            failing_source = workspace["staged_cards"] / "concepts" / "b.json"
            real_replace = Path.replace

            def crash_in_card_loop(source, target):
                if Path(source) == failing_source:
                    raise _SimulatedProcessCrash("card interrupted")
                return real_replace(source, target)

            with patch.object(Path, "replace", crash_in_card_loop), self.assertRaises(
                _SimulatedProcessCrash
            ):
                self._commit(workspace)
            (workspace["kb"] / "concepts" / "a.json").write_bytes(b"unknown-bytes")
            before = self._snapshot(workspace["kb"])

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                )

            self.assertEqual(result, (False, False))
            self.assertEqual(self._snapshot(workspace["kb"]), before)

    def test_duplicate_live_and_discarded_index_fails_before_card_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            real_write = build_index._write_regeneration_journal

            def crash_before_commit_marker(transaction, phase):
                if phase == "committed":
                    raise _SimulatedProcessCrash("commit interrupted")
                return real_write(transaction, phase)

            with patch.object(
                build_index,
                "_write_regeneration_journal",
                crash_before_commit_marker,
            ), self.assertRaises(_SimulatedProcessCrash):
                self._commit(workspace)
            transaction = build_index._load_regeneration_transaction(workspace["kb"])
            build_index._write_regeneration_journal(transaction, "rolling_back")
            shutil.copytree(
                workspace["live_index"],
                transaction.root / "discarded-live-index",
            )
            before = self._snapshot(workspace["kb"])

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                )

            self.assertEqual(result, (False, False))
            self.assertEqual(self._snapshot(workspace["kb"]), before)

    def test_unknown_discarded_staged_card_fails_closed_without_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            transaction = build_index._prepare_regeneration_transaction(
                workspace["staged_index"],
                workspace["live_index"],
                staged_cards_dir=workspace["staged_cards"],
                kb_dir=workspace["kb"],
                card_paths=list(workspace["originals"]),
            )
            build_index._write_regeneration_journal(transaction, "rolling_back")
            discarded_stage = transaction.root / "discarded-staged-cards"
            transaction.staged_cards_dir.replace(discarded_stage)
            (discarded_stage / "concepts" / "a.json").write_bytes(b"unknown")
            before = self._snapshot(workspace["kb"])

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                )

            self.assertEqual(result, (False, False))
            self.assertEqual(self._snapshot(workspace["kb"]), before)

    def test_invalid_journal_phase_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            transaction = build_index._prepare_regeneration_transaction(
                workspace["staged_index"],
                workspace["live_index"],
                staged_cards_dir=workspace["staged_cards"],
                kb_dir=workspace["kb"],
                card_paths=list(workspace["originals"]),
            )
            journal_path = transaction.root / build_index.REGEN_JOURNAL_FILE
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["phase"] = "unknown"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            before = self._snapshot(workspace["kb"])

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                )

            self.assertEqual(result, (False, False))
            self.assertEqual(self._snapshot(workspace["kb"]), before)

    def test_extra_index_backup_fails_closed_before_any_rollback_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            build_index._prepare_regeneration_transaction(
                workspace["staged_index"],
                workspace["live_index"],
                staged_cards_dir=workspace["staged_cards"],
                kb_dir=workspace["kb"],
                card_paths=list(workspace["originals"]),
            )
            extra = workspace["kb"] / "._index.backup-unrelated"
            extra.mkdir()
            (extra / "chroma.sqlite3").write_bytes(b"unrelated")
            before = self._snapshot(workspace["kb"])

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_regeneration(
                    workspace["kb"], workspace["live_index"]
                )

            self.assertEqual(result, (False, False))
            self.assertEqual(self._snapshot(workspace["kb"]), before)

    def test_main_recovers_regeneration_before_legacy_index_backup_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            real_replace = Path.replace

            def crash_before_new_index(source, target):
                if (
                    Path(source) == workspace["staged_index"]
                    and Path(target) == workspace["live_index"]
                ):
                    raise _SimulatedProcessCrash("index promotion interrupted")
                return real_replace(source, target)

            with patch.object(Path, "replace", crash_before_new_index), self.assertRaises(
                _SimulatedProcessCrash
            ):
                self._commit(workspace)

            with patch.object(
                sys,
                "argv",
                ["build_index.py", "--kb", str(workspace["kb"])],
            ), self.assertLogs(build_index.log, level="WARNING"):
                self.assertEqual(build_index.main(), 0)

            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_main_never_reports_active_index_after_no_index_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp), had_live_index=False)
            real_write = build_index._write_regeneration_journal

            def crash_before_commit_marker(transaction, phase):
                if phase == "committed":
                    raise _SimulatedProcessCrash("commit interrupted")
                return real_write(transaction, phase)

            with patch.object(
                build_index,
                "_write_regeneration_journal",
                crash_before_commit_marker,
            ), self.assertRaises(_SimulatedProcessCrash):
                self._commit(workspace)

            with (
                patch.object(
                    sys,
                    "argv",
                    ["build_index.py", "--kb", str(workspace["kb"])],
                ),
                patch.object(
                    build_index,
                    "collect_cards",
                    side_effect=build_index.KnowledgeCardLoadError(
                        "continued after recovery"
                    ),
                ),
                self.assertLogs(build_index.log, level="ERROR"),
            ):
                self.assertEqual(build_index.main(), 1)

            self._assert_old(workspace)
            self._assert_clean(workspace)

    def test_index_only_recovery_refuses_regeneration_backup_without_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_index = root / "_index"
            backup = root / (
                build_index.REGEN_INDEX_BACKUP_PREFIX + "a" * 32
            )
            backup.mkdir()
            (backup / "chroma.sqlite3").write_bytes(b"old-index")

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_index(live_index)

            self.assertEqual(result, (False, False))
            self.assertFalse(live_index.exists())
            self.assertTrue(backup.exists())

    def test_cleanup_shaped_user_directory_without_journal_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            cleanup = kb_dir / ("._cards.build-cleanup-" + "a" * 32)
            cleanup.mkdir()
            user_data = cleanup / "user-data"
            user_data.write_bytes(b"must-not-delete")
            before = self._snapshot(kb_dir)

            with self.assertLogs(build_index.log, level="ERROR"):
                result = build_index.recover_interrupted_regeneration(
                    kb_dir, kb_dir / "_index"
                )

            self.assertEqual(result, (False, False))
            self.assertEqual(self._snapshot(kb_dir), before)

    def test_staging_directory_symlink_is_rejected_before_journal_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            linked_stage = workspace["kb"] / "._index.build-linked"
            try:
                linked_stage.symlink_to(
                    workspace["staged_index"], target_is_directory=True
                )
            except OSError:
                self.skipTest("directory symlinks are unavailable")

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                build_index._prepare_regeneration_transaction(
                    linked_stage,
                    workspace["live_index"],
                    staged_cards_dir=workspace["staged_cards"],
                    kb_dir=workspace["kb"],
                    card_paths=list(workspace["originals"]),
                )

            self.assertFalse(
                (workspace["kb"] / build_index.REGEN_TRANSACTION_DIR).exists()
            )


class AtomicIndexTests(unittest.TestCase):
    def test_upgrade_refuses_limit_to_avoid_live_index_truncation(self):
        with (
            patch.object(sys, "argv", ["build_index.py", "--upgrade", "--limit", "1"]),
            self.assertLogs(build_index.log, level="ERROR"),
        ):
            self.assertEqual(build_index.main(), 2)

    def test_staged_index_replaces_old_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "_index"
            staged = root / "staged"
            live.mkdir()
            staged.mkdir()
            (live / "old").write_text("old", encoding="utf-8")
            (staged / "new").write_text("new", encoding="utf-8")

            build_index.commit_staged_index(staged, live)

            self.assertTrue((live / "new").is_file())
            self.assertFalse((live / "old").exists())
            self.assertFalse(any(root.glob("._index.backup-*")))
    def test_failed_promotion_restores_old_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "_index"
            missing_staged = root / "missing"
            live.mkdir()
            (live / "old").write_text("old", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                build_index.commit_staged_index(missing_staged, live)

            self.assertEqual((live / "old").read_text(encoding="utf-8"), "old")
            self.assertFalse(any(root.glob("._index.backup-*")))

    def test_manifest_currentness_checks_fingerprint_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp)
            manifest = _valid_manifest(
                legacy_count=4,
                school_count=2,
                evidence_count=3,
            )
            (index_dir / build_index.INDEX_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (index_dir / "chroma.sqlite3").write_text("fake", encoding="utf-8")
            fake = _fake_chromadb(
                {
                    "knowledge_base": 4,
                    "school_knowledge_v2": 2,
                    "source_evidence_v2": 3,
                }
            )
            with patch.dict(sys.modules, {"chromadb": fake}):
                self.assertTrue(
                    build_index.index_has_current_v2(
                        index_dir,
                        fingerprint="a" * 64,
                        canonical_fingerprint="b" * 64,
                        legacy_count=4,
                        school_count=2,
                        evidence_count=3,
                    )
                )
                self.assertFalse(
                    build_index.index_has_current_v2(
                        index_dir,
                        fingerprint="changed",
                        canonical_fingerprint="b" * 64,
                        legacy_count=4,
                        school_count=2,
                        evidence_count=3,
                    )
                )

    def test_manifest_currentness_rejects_schema_or_physical_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp)
            manifest = _valid_manifest()
            manifest["collections"]["school_knowledge_v2"]["schema_version"] = 999
            (index_dir / build_index.INDEX_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (index_dir / "chroma.sqlite3").write_text("fake", encoding="utf-8")
            fake = _fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 1,
                    "source_evidence_v2": 3,
                }
            )
            with patch.dict(sys.modules, {"chromadb": fake}):
                self.assertFalse(
                    build_index.index_has_current_v2(
                        index_dir,
                        fingerprint="a" * 64,
                        canonical_fingerprint="b" * 64,
                        legacy_count=2,
                        school_count=1,
                        evidence_count=3,
                    )
                )

            manifest = _valid_manifest()
            (index_dir / build_index.INDEX_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            wrong_count = _fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 9,
                    "source_evidence_v2": 3,
                }
            )
            with patch.dict(sys.modules, {"chromadb": wrong_count}):
                self.assertFalse(
                    build_index.index_has_current_v2(
                        index_dir,
                        fingerprint="a" * 64,
                        canonical_fingerprint="b" * 64,
                        legacy_count=2,
                        school_count=1,
                        evidence_count=3,
                    )
                )

    def test_manifest_currentness_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp)
            manifest = _valid_manifest()
            (index_dir / build_index.INDEX_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            fake = _fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 1,
                    "source_evidence_v2": 3,
                }
            )
            with patch.dict(sys.modules, {"chromadb": fake}):
                self.assertFalse(
                    build_index.index_has_current_v2(
                        index_dir,
                        fingerprint="a" * 64,
                        canonical_fingerprint="b" * 64,
                        legacy_count=2,
                        school_count=1,
                        evidence_count=3,
                    )
                )
            self.assertFalse((index_dir / "chroma.sqlite3").exists())

    def test_manifest_currentness_rejects_the_old_8192_input_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp)
            manifest = _valid_manifest()
            manifest["v2_embedding_input_profile"] = {
                **manifest["v2_embedding_input_profile"],
                "version": "search-document-v1",
                "max_seq_length": 8192,
            }
            (index_dir / build_index.INDEX_MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (index_dir / "chroma.sqlite3").write_text("fake", encoding="utf-8")
            fake = _fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 1,
                    "source_evidence_v2": 3,
                }
            )
            with patch.dict(sys.modules, {"chromadb": fake}):
                self.assertFalse(
                    build_index.index_has_current_v2(
                        index_dir,
                        fingerprint="a" * 64,
                        canonical_fingerprint="b" * 64,
                        legacy_count=2,
                        school_count=1,
                        evidence_count=3,
                    )
                )

    def test_unique_backup_is_restored_when_live_index_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "_index"
            backup = root / "._index.backup-one"
            backup.mkdir()
            (backup / "chroma.sqlite3").write_text("old", encoding="utf-8")

            with self.assertLogs(build_index.log, level="WARNING"):
                safe, restored = build_index.recover_interrupted_index(live)

            self.assertTrue(safe)
            self.assertTrue(restored)
            self.assertEqual(
                (live / "chroma.sqlite3").read_text(encoding="utf-8"),
                "old",
            )
            self.assertFalse(backup.exists())

    def test_multiple_backups_fail_closed_without_moving_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "_index"
            backups = [
                root / "._index.backup-one",
                root / "._index.backup-two",
            ]
            for backup in backups:
                backup.mkdir()
                (backup / "chroma.sqlite3").write_text("old", encoding="utf-8")

            with self.assertLogs(build_index.log, level="ERROR"):
                safe, restored = build_index.recover_interrupted_index(live)

            self.assertFalse(safe)
            self.assertFalse(restored)
            self.assertFalse(live.exists())
            self.assertTrue(all(backup.exists() for backup in backups))


class IndexCliGuardTests(unittest.TestCase):
    def test_force_and_upgrade_are_mutually_exclusive(self):
        with (
            patch.object(sys, "argv", ["build_index.py", "--force", "--upgrade"]),
            self.assertLogs(build_index.log, level="ERROR"),
        ):
            self.assertEqual(build_index.main(), 2)

    def test_force_and_limit_are_mutually_exclusive(self):
        with (
            patch.object(
                sys,
                "argv",
                ["build_index.py", "--force", "--limit", "1"],
            ),
            self.assertLogs(build_index.log, level="ERROR"),
        ):
            self.assertEqual(build_index.main(), 2)

    def test_limit_must_be_positive(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), patch.object(
                sys,
                "argv",
                ["build_index.py", "--limit", value],
            ), self.assertLogs(build_index.log, level="ERROR"):
                self.assertEqual(build_index.main(), 2)


class InstallerUpgradeTests(unittest.TestCase):
    def test_existing_index_uses_upgrade_instead_of_blind_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_file = Path(tmp) / "_index" / "chroma.sqlite3"
            index_file.parent.mkdir()
            index_file.write_text("legacy", encoding="utf-8")
            with (
                patch.object(install, "INDEX_FILE", index_file),
                patch.object(install, "VENV_PY", Path("python")),
                patch.object(install, "SKILL_DIR", Path("skill")),
                patch.object(install, "run_cmd") as run_cmd,
                patch.object(install, "step"),
                patch.object(install, "info"),
                patch.object(install, "ok"),
            ):
                self.assertTrue(install.build_index(resume=True))

            command = run_cmd.call_args.args[0]
            self.assertIn("--upgrade", command)
            self.assertNotIn("--force", command)

    def test_forced_installer_rebuild_is_delegated_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_file = Path(tmp) / "_index" / "chroma.sqlite3"
            index_file.parent.mkdir()
            index_file.write_text("legacy", encoding="utf-8")
            with (
                patch.object(install, "INDEX_FILE", index_file),
                patch.object(install, "VENV_PY", Path("python")),
                patch.object(install, "SKILL_DIR", Path("skill")),
                patch.object(install, "run_cmd") as run_cmd,
                patch.object(install, "step"),
                patch.object(install, "info"),
                patch.object(install, "ok"),
            ):
                self.assertTrue(install.build_index(resume=True, force=True))

            self.assertIn("--force", run_cmd.call_args.args[0])
            self.assertEqual(index_file.read_text(encoding="utf-8"), "legacy")

    def test_failed_rebuild_does_not_delete_existing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_file = Path(tmp) / "_index" / "chroma.sqlite3"
            index_file.parent.mkdir()
            index_file.write_text("legacy", encoding="utf-8")
            with (
                patch.object(install, "INDEX_FILE", index_file),
                patch.object(install, "VENV_PY", Path("python")),
                patch.object(install, "SKILL_DIR", Path("skill")),
                patch.object(
                    install,
                    "run_cmd",
                    side_effect=subprocess.CalledProcessError(1, ["build_index"]),
                ),
                patch.object(install, "step"),
                patch.object(install, "info"),
                patch.object(install, "fail"),
            ):
                self.assertFalse(install.build_index(resume=True, force=True))

            self.assertEqual(index_file.read_text(encoding="utf-8"), "legacy")


class DoctorManifestTests(unittest.TestCase):
    @staticmethod
    def _fake_chromadb(counts):
        return _fake_chromadb(counts)

    def test_doctor_verifies_declared_collection_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            index_dir = skill_dir / "knowledge_base" / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")
            manifest = _valid_manifest()
            (index_dir / "index_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            fake = self._fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 1,
                    "source_evidence_v2": 3,
                }
            )
            with (
                patch.object(kb_doctor, "SKILL_DIR", skill_dir),
                patch.dict(sys.modules, {"chromadb": fake}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(kb_doctor.check_kb_index())

    def test_doctor_rejects_manifest_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            index_dir = skill_dir / "knowledge_base" / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")
            manifest = _valid_manifest(
                legacy_count=2,
                school_count=0,
                evidence_count=0,
            )
            (index_dir / "index_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            fake = self._fake_chromadb({"knowledge_base": 1})
            with (
                patch.object(kb_doctor, "SKILL_DIR", skill_dir),
                patch.dict(sys.modules, {"chromadb": fake}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(kb_doctor.check_kb_index())

    def test_doctor_distinguishes_missing_and_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            index_dir = skill_dir / "knowledge_base" / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")
            fake = self._fake_chromadb({"knowledge_base": 1})

            with (
                patch.object(kb_doctor, "SKILL_DIR", skill_dir),
                patch.dict(sys.modules, {"chromadb": fake}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(kb_doctor.check_kb_index())

            (index_dir / "index_manifest.json").write_text(
                "{broken",
                encoding="utf-8",
            )
            with (
                patch.object(kb_doctor, "SKILL_DIR", skill_dir),
                patch.dict(sys.modules, {"chromadb": fake}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(kb_doctor.check_kb_index())

    def test_doctor_rejects_wrong_manifest_schema_or_created_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            index_dir = skill_dir / "knowledge_base" / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")
            fake = self._fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 1,
                    "source_evidence_v2": 3,
                }
            )
            manifest = _valid_manifest()
            manifest["collections"]["school_knowledge_v2"]["created"] = False
            (index_dir / "index_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with (
                patch.object(kb_doctor, "SKILL_DIR", skill_dir),
                patch.dict(sys.modules, {"chromadb": fake}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(kb_doctor.check_kb_index())

    def test_doctor_rejects_invalid_or_inconsistent_embedding_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            index_dir = skill_dir / "knowledge_base" / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")
            fake = self._fake_chromadb(
                {
                    "knowledge_base": 2,
                    "school_knowledge_v2": 1,
                    "source_evidence_v2": 3,
                }
            )

            invalid_strategy = _valid_manifest()
            invalid_strategy["embedding_strategy"]["school_knowledge_v2"] = (
                "unknown"
            )
            inconsistent_model = _valid_manifest()
            inconsistent_model["embedding_models"]["source_evidence_v2"] = (
                "different-model"
            )
            stale_input_profile = _valid_manifest()
            stale_input_profile["v2_embedding_input_profile"] = {
                **stale_input_profile["v2_embedding_input_profile"],
                "version": "search-document-v1",
                "max_seq_length": 8192,
            }

            for manifest in (
                invalid_strategy,
                inconsistent_model,
                stale_input_profile,
            ):
                with self.subTest(manifest=manifest):
                    (index_dir / "index_manifest.json").write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                    with (
                        patch.object(kb_doctor, "SKILL_DIR", skill_dir),
                        patch.dict(sys.modules, {"chromadb": fake}),
                        redirect_stdout(io.StringIO()),
                    ):
                        self.assertFalse(kb_doctor.check_kb_index())


if __name__ == "__main__":
    unittest.main()
