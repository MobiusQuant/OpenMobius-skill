import builtins
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _lib.retriever import (  # noqa: E402
    ReadOnlySearchModeError,
    Retriever,
    _ReadOnlySQLiteCollection,
    _matches_read_only_where,
)
from _lib.build_lock import (  # noqa: E402
    BuildLockUnavailable,
    INSTALL_GENERATION_MARKER,
    READ_ONLY_BUNDLE_FILES,
    READ_ONLY_BUNDLE_MARKER,
    encode_read_only_bundle_marker,
    knowledge_base_build_lock,
    knowledge_base_lock_root,
)
from _lib import build_lock as build_lock_module  # noqa: E402
from scripts import kb_retrieve  # noqa: E402
from _lib import knowledge_v2  # noqa: E402


def _insert_metadata(
    connection: sqlite3.Connection,
    row_id: int,
    key: str,
    value,
) -> None:
    columns = [None, None, None, None]
    if isinstance(value, str):
        columns[0] = value
    elif type(value) is bool:
        columns[3] = int(value)
    elif type(value) is int:
        columns[1] = value
    elif type(value) is float:
        columns[2] = value
    else:
        raise TypeError(f"unsupported fixture metadata value: {value!r}")
    connection.execute(
        "INSERT INTO embedding_metadata "
        "(id, key, string_value, int_value, float_value, bool_value) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, key, *columns),
    )


def _create_small_chroma_metadata_index(kb_dir: Path) -> Path:
    index_dir = kb_dir / "_index"
    index_dir.mkdir(parents=True)
    database_path = index_dir / "chroma.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY
        );
        CREATE TABLE databases (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            UNIQUE (tenant_id, name)
        );
        CREATE TABLE collections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            database_id TEXT NOT NULL,
            UNIQUE (name, database_id)
        );
        CREATE TABLE segments (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            scope TEXT NOT NULL,
            collection TEXT NOT NULL
        );
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY,
            segment_id TEXT NOT NULL,
            embedding_id TEXT NOT NULL
        );
        CREATE TABLE embedding_metadata (
            id INTEGER NOT NULL,
            key TEXT NOT NULL,
            string_value TEXT,
            int_value INTEGER,
            float_value REAL,
            bool_value INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO tenants (id) VALUES (?)",
        ("default_tenant",),
    )
    connection.execute(
        "INSERT INTO databases (id, name, tenant_id) VALUES (?, ?, ?)",
        ("default-db", "default_database", "default_tenant"),
    )
    connection.execute(
        "INSERT INTO collections (id, name, database_id) VALUES (?, ?, ?)",
        ("collection-school", "school_knowledge_v2", "default-db"),
    )
    connection.execute(
        "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
        (
            "metadata-school",
            "urn:chroma:segment/metadata/sqlite",
            "METADATA",
            "collection-school",
        ),
    )
    records = [
        (
            "sp-fvg-ict",
            "Term: Fair Value Gap\nSchool: ICT\nDefinition: FVG imbalance",
            {
                "canonical_id": "fair_value_gap",
                "record_id": "sp-fvg-ict",
                "term": "Fair Value Gap",
                "school": "ICT",
                "type": "concept",
                "file_path": "concepts/fair_value_gap.json",
                "schema_version": 2,
            },
        ),
        (
            "sp-ob-smc",
            "Term: Order Block\nSchool: SMC\nDefinition: institutional block",
            {
                "canonical_id": "order_block",
                "record_id": "sp-ob-smc",
                "term": "Order Block",
                "school": "SMC",
                "type": "concept",
                "file_path": "concepts/order_block.json",
                "schema_version": 2,
            },
        ),
        (
            "sp-fvg-wyckoff",
            "Term: FVG example\nSchool: Wyckoff\nCase study",
            {
                "canonical_id": "fvg_case",
                "record_id": "sp-fvg-wyckoff",
                "term": "FVG example",
                "school": "Wyckoff",
                "type": "case",
                "file_path": "cases/fvg.json",
                "schema_version": 2,
            },
        ),
    ]
    for row_id, (record_id, document, metadata) in enumerate(records, 1):
        connection.execute(
            "INSERT INTO embeddings (id, segment_id, embedding_id) "
            "VALUES (?, ?, ?)",
            (row_id, "metadata-school", record_id),
        )
        _insert_metadata(connection, row_id, "chroma:document", document)
        for key, value in metadata.items():
            _insert_metadata(connection, row_id, key, value)
    connection.commit()
    connection.close()

    (kb_dir / "schools.json").write_text(
        json.dumps(
            {
                "schools": [
                    {"name": "ICT", "aliases": ["Inner Circle Trader"]},
                    {"name": "SMC", "aliases": []},
                    {"name": "Wyckoff", "aliases": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    (kb_dir / "term_aliases.json").write_text(
        json.dumps(
            {
                "mappings": [
                    {
                        "card_id": "fair_value_gap",
                        "canonical": "Fair Value Gap",
                        "aliases": ["FVG"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return database_path


@contextmanager
def _read_only_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        kb_dir = Path(tmp) / "knowledge_base"
        kb_dir.mkdir()
        database_path = _create_small_chroma_metadata_index(kb_dir)
        index_dir = database_path.parent
        os.chmod(database_path, 0o444)
        os.chmod(index_dir, 0o555)
        try:
            yield kb_dir, database_path
        finally:
            os.chmod(index_dir, 0o755)
            os.chmod(database_path, 0o644)


def _readonly_chromadb_module():
    def fail_to_open(*_args, **_kwargs):
        raise RuntimeError("attempt to write a readonly database")

    return types.SimpleNamespace(PersistentClient=fail_to_open)


def _write_source_registry(kb_dir: Path) -> None:
    (kb_dir / "schools.json").write_text(
        json.dumps(
            {
                "schools": [
                    {"name": "ICT", "aliases": ["Inner Circle Trader"]},
                    {"name": "SMC", "aliases": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    (kb_dir / "term_aliases.json").write_text(
        json.dumps(
            {
                "mappings": [
                    {
                        "card_id": "fair_value_gap",
                        "canonical": "Fair Value Gap",
                        "aliases": ["FVG"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _source_build_result():
    return types.SimpleNamespace(
        school_records=(
            {
                "id": "sp-source-fvg",
                "document": (
                    "Term: Fair Value Gap\nSchool: ICT\n"
                    "Definition: FVG imbalance"
                ),
                "metadata": {
                    "canonical_id": "fair_value_gap",
                    "record_id": "sp-source-fvg",
                    "term": "Fair Value Gap",
                    "school": "ICT",
                    "type": "concept",
                    "file_path": "concepts/fair_value_gap.json",
                },
            },
            {
                "id": "sp-source-ob",
                "document": "Term: Order Block\nSchool: SMC\nOrder block",
                "metadata": {
                    "canonical_id": "order_block",
                    "record_id": "sp-source-ob",
                    "term": "Order Block",
                    "school": "SMC",
                    "type": "concept",
                    "file_path": "concepts/order_block.json",
                },
            },
        ),
        evidence_records=(),
    )


class ReadOnlyWhereTests(unittest.TestCase):
    def test_filter_subset_preserves_and_or_include_exclude_semantics(self) -> None:
        metadata = {"school": "ICT", "type": "concept", "rank": 2}
        self.assertTrue(
            _matches_read_only_where(
                metadata,
                {
                    "$and": [
                        {"school": {"$in": ["ICT", "SMC"]}},
                        {"school": {"$nin": ["Wyckoff"]}},
                        {"type": "concept"},
                        {"rank": {"$gte": 2}},
                    ]
                },
            )
        )
        self.assertFalse(
            _matches_read_only_where(
                metadata,
                {"$or": [{"school": "SMC"}, {"type": "case"}]},
            )
        )

    def test_unsupported_filter_operator_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            _matches_read_only_where(
                {"school": "ICT"}, {"school": {"$contains": "IC"}}
            )


class ReadOnlyRetrieverTests(unittest.TestCase):
    def test_sqlite_fallback_selects_same_named_collection_in_default_database(
        self,
    ) -> None:
        import chromadb
        from chromadb.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "_index"
            settings = Settings(
                is_persistent=True,
                persist_directory=str(index_dir),
                anonymized_telemetry=False,
            )
            admin = chromadb.AdminClient(settings)
            admin.create_database("other_database")
            other_client = chromadb.PersistentClient(
                path=str(index_dir),
                settings=settings,
                database="other_database",
            )
            other_client.create_collection("school_knowledge_v2").add(
                ids=["other-db-record"],
                embeddings=[[1.0, 0.0]],
                documents=["unrelated database document"],
                metadatas=[{"school": "ICT", "type": "concept"}],
            )
            default_client = chromadb.PersistentClient(
                path=str(index_dir),
                settings=settings,
            )
            default_client.create_collection("school_knowledge_v2").add(
                ids=["default-db-record"],
                embeddings=[[0.0, 1.0]],
                documents=["expected default database document"],
                metadatas=[{"school": "ICT", "type": "concept"}],
            )

            self.assertEqual(
                default_client.get_collection("school_knowledge_v2").get()["ids"],
                ["default-db-record"],
            )
            read_only = _ReadOnlySQLiteCollection(
                index_dir, "school_knowledge_v2"
            )
            self.assertEqual(
                read_only._connection.execute("PRAGMA temp_store").fetchone(),
                (2,),
            )
            self.assertEqual(
                read_only._connection.execute("PRAGMA query_only").fetchone(),
                (1,),
            )
            result = read_only.get(
                where={"school": "ICT"}, include=["documents"]
            )

            self.assertEqual(result["ids"], ["default-db-record"])
            self.assertEqual(
                result["documents"], ["expected default database document"]
            )
            read_only._connection.close()
            default_client.close()
            other_client.close()

    def test_readonly_startup_uses_sqlite_without_modifying_database(self) -> None:
        with _read_only_fixture() as (kb_dir, database_path):
            before = hashlib.sha256(database_path.read_bytes()).hexdigest()
            with (
                patch.dict(
                    sys.modules,
                    {"chromadb": _readonly_chromadb_module()},
                ),
                self.assertLogs("_lib.retriever", level="WARNING"),
            ):
                retriever = Retriever(kb_dir, None, layer="school")
                scope = retriever.resolve_scope(filter_schools=["ICT"])
                cards = retriever.search(
                    "FVG",
                    top_k=3,
                    filter_schools=scope["schools"],
                    search_mode="auto",
                )
            after = hashlib.sha256(database_path.read_bytes()).hexdigest()

        self.assertTrue(retriever.read_only_fallback)
        self.assertEqual(retriever.available_search_mode("auto"), "lexical")
        self.assertEqual(before, after)
        self.assertEqual([card.card_id for card in cards], ["sp-fvg-ict"])
        self.assertEqual(cards[0].match_kind, "exact")
        self.assertIsNone(cards[0].distance)

    def test_explicit_vector_mode_is_not_silently_downgraded(self) -> None:
        with _read_only_fixture() as (kb_dir, _database_path), patch.dict(
            sys.modules, {"chromadb": _readonly_chromadb_module()}
        ):
            retriever = Retriever(kb_dir, None, layer="school")
            with self.assertRaises(ReadOnlySearchModeError) as caught:
                retriever.available_search_mode("hybrid")

        message = str(caught.exception)
        self.assertIn("未自动改变显式检索语义", message)
        self.assertIn("--search-mode lexical", message)

    def test_non_permission_chroma_failure_does_not_mask_corruption(self) -> None:
        chromadb = types.SimpleNamespace(
            PersistentClient=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("corrupt database header")
            )
        )
        with _read_only_fixture() as (kb_dir, _database_path), patch.dict(
            sys.modules, {"chromadb": chromadb}
        ):
            with self.assertRaises(RuntimeError) as caught:
                Retriever(kb_dir, None, layer="school")

        self.assertIn("corrupt database header", str(caught.exception))


class ReadOnlyCliTests(unittest.TestCase):
    def test_auto_query_downgrades_clearly_and_never_loads_embedder(self) -> None:
        with (
            _read_only_fixture() as (kb_dir, _database_path),
            patch.dict(
                sys.modules,
                {"chromadb": _readonly_chromadb_module()},
            ),
            patch.object(kb_retrieve, "get_embedder") as get_embedder,
            self.assertLogs(level="WARNING") as logs,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            result = kb_retrieve.main(
                [
                    "FVG",
                    "--kb",
                    str(kb_dir),
                    "--layer",
                    "school",
                    "--schools",
                    "ICT",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        get_embedder.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["term"], "Fair Value Gap")
        self.assertEqual(payload[0]["retrieval"]["search_mode"], "lexical")
        self.assertEqual(payload[0]["retrieval"]["match_kind"], "exact")
        warning_text = "\n".join(logs.output)
        self.assertIn("BM25", warning_text)
        self.assertIn("不包含向量语义排序", warning_text)

    def test_explicit_hybrid_query_fails_before_loading_embedder(self) -> None:
        with (
            _read_only_fixture() as (kb_dir, _database_path),
            patch.dict(
                sys.modules,
                {"chromadb": _readonly_chromadb_module()},
            ),
            patch.object(kb_retrieve, "get_embedder") as get_embedder,
            self.assertLogs(level="ERROR") as logs,
        ):
            result = kb_retrieve.main(
                [
                    "FVG",
                    "--kb",
                    str(kb_dir),
                    "--layer",
                    "school",
                    "--schools",
                    "ICT",
                    "--search-mode",
                    "hybrid",
                ]
            )

        self.assertEqual(result, 1)
        get_embedder.assert_not_called()
        self.assertIn("未自动改变显式检索语义", "\n".join(logs.output))


class SourceLexicalFallbackTests(unittest.TestCase):
    def test_corrupt_source_card_refuses_partial_canonical_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            concepts_dir = kb_dir / "concepts"
            concepts_dir.mkdir(parents=True)
            (concepts_dir / "valid.json").write_text(
                json.dumps(
                    {
                        "canonical_term": "Fair Value Gap",
                        "definition": "Valid definition",
                    }
                ),
                encoding="utf-8",
            )
            (concepts_dir / "broken.json").write_text(
                '{"canonical_term":', encoding="utf-8"
            )
            with (
                self.assertLogs("kb_retrieve", level="ERROR") as logs,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                result = kb_retrieve.main(
                    [
                        "Fair Value Gap",
                        "--kb",
                        str(kb_dir),
                        "--search-mode",
                        "lexical",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "bundled corpus lexical fallback 初始化失败",
            "\n".join(logs.output),
        )
        self.assertIn("concepts/broken.json", "\n".join(logs.output))

    def test_missing_index_explicit_lexical_uses_json_without_chromadb(self) -> None:
        real_import = builtins.__import__
        chromadb_imports: list[str] = []

        def guarded_import(name, *args, **kwargs):
            if name == "chromadb" or name.startswith("chromadb."):
                chromadb_imports.append(name)
                raise AssertionError("source fallback must not import chromadb")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            _write_source_registry(kb_dir)
            with (
                patch.object(
                    knowledge_v2,
                    "build_v2_records",
                    return_value=_source_build_result(),
                ),
                patch.object(builtins, "__import__", side_effect=guarded_import),
                patch.object(kb_retrieve, "get_embedder") as get_embedder,
                self.assertLogs(level="WARNING") as logs,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                result = kb_retrieve.main(
                    [
                        "FVG",
                        "--kb",
                        str(kb_dir),
                        "--layer",
                        "school",
                        "--schools",
                        "ICT",
                        "--search-mode",
                        "lexical",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(chromadb_imports, [])
        get_embedder.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual([item["card_id"] for item in payload], ["sp-source-fvg"])
        self.assertEqual(payload[0]["retrieval"]["search_mode"], "lexical")
        self.assertEqual(payload[0]["retrieval"]["match_kind"], "exact")
        warning_text = "\n".join(logs.output)
        self.assertIn("bundled knowledge corpus", warning_text)
        self.assertIn("无向量语义排序", warning_text)

    def test_missing_index_auto_does_not_use_source_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            _write_source_registry(kb_dir)
            with (
                patch.object(Retriever, "from_source") as from_source,
                patch.object(kb_retrieve, "get_embedder") as get_embedder,
                self.assertLogs("kb_retrieve", level="ERROR") as logs,
            ):
                result = kb_retrieve.main(
                    ["FVG", "--kb", str(kb_dir), "--layer", "school"]
                )

        self.assertEqual(result, 1)
        from_source.assert_not_called()
        get_embedder.assert_not_called()
        message = "\n".join(logs.output)
        self.assertIn("不会把 auto/semantic/hybrid 静默降级", message)
        self.assertIn("仅显式 --search-mode lexical", message)

    def test_empty_existing_index_fails_without_chroma_or_writes(
        self,
    ) -> None:
        real_import = builtins.__import__
        chromadb_imports: list[str] = []

        def guarded_import(name, *args, **kwargs):
            if name == "chromadb" or name.startswith("chromadb."):
                chromadb_imports.append(name)
                raise AssertionError("empty index must fail before importing chromadb")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            index_dir = kb_dir / "_index"
            index_dir.mkdir(parents=True)
            _write_source_registry(kb_dir)
            with (
                patch.object(builtins, "__import__", side_effect=guarded_import),
                patch.object(Retriever, "from_source") as from_source,
                patch.object(kb_retrieve, "get_embedder") as get_embedder,
                self.assertLogs("kb_retrieve", level="ERROR") as logs,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                result = kb_retrieve.main(
                    [
                        "FVG",
                        "--kb",
                        str(kb_dir),
                        "--layer",
                        "school",
                        "--schools",
                        "ICT",
                        "--search-mode",
                        "lexical",
                        "--format",
                        "json",
                    ]
                )

            self.assertEqual(list(index_dir.iterdir()), [])

        self.assertEqual(result, 1)
        self.assertEqual(chromadb_imports, [])
        from_source.assert_not_called()
        get_embedder.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("chroma.sqlite3", "\n".join(logs.output))

    def test_nonempty_broken_index_is_never_masked_by_source(self) -> None:
        chromadb = types.SimpleNamespace(
            PersistentClient=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("corrupt database header")
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            index_dir = kb_dir / "_index"
            index_dir.mkdir(parents=True)
            (index_dir / "chroma.sqlite3").write_bytes(b"not a sqlite database")
            _write_source_registry(kb_dir)
            with (
                patch.dict(sys.modules, {"chromadb": chromadb}),
                patch.object(Retriever, "from_source") as from_source,
                self.assertLogs("kb_retrieve", level="ERROR") as logs,
            ):
                result = kb_retrieve.main(
                    [
                        "FVG",
                        "--kb",
                        str(kb_dir),
                        "--layer",
                        "school",
                        "--search-mode",
                        "lexical",
                    ]
                )

        self.assertEqual(result, 1)
        from_source.assert_not_called()
        self.assertIn("corrupt database header", "\n".join(logs.output))

    def test_source_constructor_preserves_hard_scope_and_rejects_vector_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            _write_source_registry(kb_dir)
            with patch.object(
                knowledge_v2,
                "build_v2_records",
                return_value=_source_build_result(),
            ):
                retriever = Retriever.from_source(kb_dir, layer="school")
                scope = retriever.resolve_scope(
                    filter_schools=["Inner Circle Trader"],
                    exclude_schools=["SMC"],
                    filter_type="concept",
                )
                cards = retriever.search(
                    "FVG",
                    top_k=5,
                    filter_schools=scope["schools"],
                    exclude_schools=scope["excluded_schools"],
                    filter_type=scope["type"],
                    search_mode="lexical",
                )
                with self.assertRaises(ReadOnlySearchModeError):
                    retriever.available_search_mode("semantic")

        self.assertTrue(retriever.source_lexical_fallback)
        self.assertEqual(scope["schools"], ["ICT"])
        self.assertEqual([card.card_id for card in cards], ["sp-source-fvg"])


class GenerationLeaseTests(unittest.TestCase):
    @staticmethod
    def _write_read_only_bundle(kb_dir: Path) -> dict[str, bytes]:
        payloads = {
            "knowledge_v2.compact.json": b'{"header":true}\n{"body":true}\n',
            "schools.json": b'{"schools":[]}\n',
            "term_aliases.json": b'{"mappings":[]}\n',
        }
        if set(payloads) != set(READ_ONLY_BUNDLE_FILES):
            raise AssertionError("test fixture does not cover the runtime file set")
        for name, payload in payloads.items():
            (kb_dir / name).write_bytes(payload)
        (kb_dir / READ_ONLY_BUNDLE_MARKER).write_bytes(
            encode_read_only_bundle_marker(payloads)
        )
        return payloads

    @staticmethod
    def _file_snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    @staticmethod
    def _write_inventory_manifest(kb_dir: Path) -> None:
        index_dir = kb_dir / "_index"
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "index_manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 2,
                    "collections": {
                        "school_knowledge_v2": {
                            "count": 1,
                            "created": True,
                            "layer": "school",
                            "metadata_value_counts": {"school": {"ICT": 1}},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (kb_dir / "schools.json").write_text(
            json.dumps({"schools": [{"name": "ICT", "aliases": []}]}),
            encoding="utf-8",
        )

    def test_two_direct_retrievers_keep_lease_until_both_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            with patch.object(Retriever, "_initialize_index", return_value=None):
                first = Retriever(kb_dir, None)
                second = Retriever(kb_dir, None)
            first.close()

            outcomes: list[str] = []

            def try_writer() -> None:
                try:
                    with knowledge_base_build_lock(kb_dir, mode="write"):
                        outcomes.append("acquired")
                except BuildLockUnavailable:
                    outcomes.append("blocked")

            blocked_writer = threading.Thread(target=try_writer)
            blocked_writer.start()
            blocked_writer.join(timeout=5)
            self.assertFalse(blocked_writer.is_alive())
            self.assertEqual(outcomes, ["blocked"])

            second.close()
            available_writer = threading.Thread(target=try_writer)
            available_writer.start()
            available_writer.join(timeout=5)
            self.assertFalse(available_writer.is_alive())
            self.assertEqual(outcomes, ["blocked", "acquired"])

    def test_same_thread_cannot_upgrade_read_lease_to_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            with knowledge_base_build_lock(kb_dir, mode="read"):
                with self.assertRaisesRegex(
                    BuildLockUnavailable, "cannot change.*read to write"
                ):
                    with knowledge_base_build_lock(kb_dir, mode="write"):
                        self.fail("read-to-write upgrade must fail closed")

    @unittest.skipIf(os.name == "nt", "Windows intentionally serializes readers")
    def test_posix_read_leases_are_shared_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            child = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
from _lib.build_lock import knowledge_base_build_lock
with knowledge_base_build_lock(Path(sys.argv[1]), mode="read"):
    print("reader-acquired")
"""
            with knowledge_base_build_lock(kb_dir, mode="read"):
                process = subprocess.run(
                    [sys.executable, "-c", child, str(kb_dir)],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "reader-acquired")

    @unittest.skipIf(os.name == "nt", "POSIX read locks use read-only descriptors")
    def test_posix_reader_opens_existing_lock_without_write_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            lock_root = root / "locks"
            lock_root.mkdir()
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            lock_path.write_bytes(b"\0")
            real_open = os.open
            observed_access_modes: list[int] = []

            def read_only_open(path, flags, *args):
                if Path(path) == lock_path:
                    access_mode = flags & os.O_ACCMODE
                    observed_access_modes.append(access_mode)
                    if access_mode != os.O_RDONLY:
                        raise OSError(30, "Read-only file system", str(path))
                return real_open(path, flags, *args)

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(build_lock_module.os, "open", side_effect=read_only_open),
                knowledge_base_build_lock(kb_dir, mode="read"),
            ):
                pass

            self.assertEqual(observed_access_modes, [os.O_RDONLY])

    @unittest.skipIf(os.name == "nt", "POSIX-only first-reader initialization")
    def test_read_only_first_reader_reports_uninitialized_lock_precisely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            lock_root = root / "locks"
            lock_root.mkdir()
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            real_open = os.open

            def deny_creation(path, flags, *args):
                if Path(path) == lock_path and flags & os.O_CREAT:
                    raise OSError(30, "Read-only file system", str(path))
                return real_open(path, flags, *args)

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(build_lock_module.os, "open", side_effect=deny_creation),
                self.assertRaisesRegex(
                    BuildLockUnavailable,
                    "lock infrastructure is not initialized",
                ),
            ):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("an uninitialized read-only lock must fail closed")

    @unittest.skipIf(os.name == "nt", "POSIX-only creation-denial simulation")
    def test_static_bundle_reader_survives_uninitialized_read_only_lock_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            lock_root = root / "locks"
            lock_root.mkdir()
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            real_open = os.open

            def deny_creation(path, flags, *args):
                if Path(path) == lock_path and flags & os.O_CREAT:
                    raise OSError(30, "Read-only file system", str(path))
                return real_open(path, flags, *args)

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(build_lock_module.os, "open", side_effect=deny_creation),
                knowledge_base_build_lock(kb_dir, mode="read"),
            ):
                self.assertTrue(
                    build_lock_module.current_thread_holds_kb_lock(
                        kb_dir,
                        mode="read",
                    )
                )
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    pass

            self.assertFalse(lock_path.exists())
            self.assertFalse(
                build_lock_module.current_thread_holds_kb_lock(
                    kb_dir,
                    mode="read",
                )
            )

    @unittest.skipIf(os.name == "nt", "simulates the Windows branch on POSIX")
    def test_static_bundle_windows_first_reader_policy_denial_can_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            lock_root = root / "locks"
            lock_root.mkdir()
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            real_open = os.open

            def deny_creation(path, flags, *args):
                if Path(path) == lock_path and flags & os.O_CREAT:
                    raise PermissionError(13, "Permission denied", str(path))
                return real_open(path, flags, *args)

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(
                    build_lock_module,
                    "_uses_windows_locking",
                    return_value=True,
                ),
                patch.object(build_lock_module.os, "open", side_effect=deny_creation),
                knowledge_base_build_lock(kb_dir, mode="read"),
            ):
                self.assertTrue(
                    build_lock_module.current_thread_holds_kb_lock(
                        kb_dir,
                        mode="read",
                    )
                )

            self.assertFalse(lock_path.exists())

    def test_static_bundle_writer_and_corrupt_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)

            with self.assertRaisesRegex(BuildLockUnavailable, "immutable"):
                with knowledge_base_build_lock(kb_dir, mode="write"):
                    self.fail("a static bundle must never acquire a writer lease")

            (kb_dir / "schools.json").write_bytes(b'{"schools":["tampered"]}\n')
            with self.assertRaisesRegex(BuildLockUnavailable, "hash mismatch"):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("a changed scoped input must invalidate the bundle")

    @unittest.skipIf(os.name == "nt", "POSIX-only errno simulation")
    def test_static_bundle_does_not_mask_non_policy_lock_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            lock_root = root / "locks"
            lock_root.mkdir()
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            real_open = os.open

            def fail_creation(path, flags, *args):
                if Path(path) == lock_path and flags & os.O_CREAT:
                    raise OSError(28, "No space left on device", str(path))
                return real_open(path, flags, *args)

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(build_lock_module.os, "open", side_effect=fail_creation),
                self.assertRaisesRegex(
                    BuildLockUnavailable,
                    "cannot be initialized",
                ),
            ):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("ENOSPC must not be treated as a sandbox policy denial")

    @unittest.skipIf(os.name == "nt", "POSIX-only creation-denial simulation")
    def test_static_bundle_marker_disappearance_does_not_create_a_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            marker = kb_dir / READ_ONLY_BUNDLE_MARKER
            lock_root = root / "locks"
            lock_root.mkdir()
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            real_open = os.open

            def remove_marker_and_deny(path, flags, *args):
                if Path(path) == lock_path and flags & os.O_CREAT:
                    marker.unlink()
                    raise OSError(30, "Read-only file system", str(path))
                return real_open(path, flags, *args)

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(
                    build_lock_module.os,
                    "open",
                    side_effect=remove_marker_and_deny,
                ),
                self.assertRaisesRegex(
                    BuildLockUnavailable,
                    "lock infrastructure is not initialized",
                ),
            ):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("a vanished bundle marker must not authorize fallback")

            self.assertFalse(
                build_lock_module.current_thread_holds_kb_lock(
                    kb_dir,
                    mode="read",
                )
            )

    def test_static_bundle_marker_rejects_bool_version_and_duplicate_keys(self) -> None:
        malformed_markers = (
            b'{"files":{},"format":"openmobius-readonly-bundle",'
            b'"format_version":true}\n',
            b'{"files":{},"files":{},"format":"openmobius-readonly-bundle",'
            b'"format_version":1}\n',
            b'{"files":{},"format":"openmobius-readonly-bundle",'
            b'"format_version":1.0}\n',
        )
        for payload in malformed_markers:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                kb_dir = Path(tmp) / "knowledge_base"
                kb_dir.mkdir()
                self._write_read_only_bundle(kb_dir)
                (kb_dir / READ_ONLY_BUNDLE_MARKER).write_bytes(payload)
                with self.assertRaises(BuildLockUnavailable):
                    with knowledge_base_build_lock(kb_dir, mode="read"):
                        self.fail("a malformed immutable marker must fail closed")

    def test_static_bundle_marker_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            marker = kb_dir / READ_ONLY_BUNDLE_MARKER
            outside = root / "outside-marker.json"
            marker.replace(outside)
            try:
                marker.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with self.assertRaisesRegex(BuildLockUnavailable, "not a safe file"):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("a symlinked static marker must fail closed")

    def test_static_bundle_never_bypasses_real_lock_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            lock_root = root / "locks"
            lock_root.mkdir()

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                patch.object(
                    build_lock_module,
                    "_lock_file_descriptor",
                    side_effect=BlockingIOError(11, "Resource temporarily unavailable"),
                ),
                self.assertRaisesRegex(BuildLockUnavailable, "another operation"),
            ):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("real lock contention must not use the static fallback")

            self.assertFalse(
                build_lock_module.current_thread_holds_kb_lock(
                    kb_dir,
                    mode="read",
                )
            )

    @unittest.skipIf(os.name == "nt", "POSIX O_NOFOLLOW behavior")
    def test_static_bundle_never_bypasses_an_unsafe_external_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            lock_root = root / "locks"
            lock_root.mkdir()
            outside = root / "outside.lock"
            outside.write_bytes(b"\0")
            lock_path = lock_root / (
                build_lock_module._build_lock_key(kb_dir) + ".lock"
            )
            try:
                lock_path.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with (
                patch.object(
                    build_lock_module,
                    "_build_lock_directory",
                    return_value=lock_root,
                ),
                self.assertRaises(BuildLockUnavailable),
            ):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("an unsafe external lock must not use static fallback")

    @unittest.skipIf(not hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_static_bundle_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            self._write_read_only_bundle(kb_dir)
            marker = kb_dir / READ_ONLY_BUNDLE_MARKER
            marker.unlink()
            os.mkfifo(marker)

            with self.assertRaisesRegex(BuildLockUnavailable, "not a safe file"):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("a FIFO marker must fail before any blocking read")

    @unittest.skipIf(os.name == "nt", "hard-link semantics vary on Windows")
    def test_static_bundle_rejects_hard_linked_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            payloads = self._write_read_only_bundle(kb_dir)
            school_path = kb_dir / "schools.json"
            outside = root / "schools.json"
            outside.write_bytes(payloads["schools.json"])
            school_path.unlink()
            os.link(outside, school_path)

            with self.assertRaisesRegex(BuildLockUnavailable, "not a safe file"):
                with knowledge_base_build_lock(kb_dir, mode="read"):
                    self.fail("hard-linked bundle inputs must fail closed")

    def test_lock_rendezvous_is_independent_of_process_temp_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb_dir = root / "knowledge_base"
            kb_dir.mkdir()
            alternate_temp = root / "different-process-temp"
            alternate_temp.mkdir()
            child = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
from _lib.build_lock import BuildLockUnavailable, knowledge_base_build_lock
try:
    with knowledge_base_build_lock(Path(sys.argv[1]), mode="write"):
        print("incorrectly-acquired")
except BuildLockUnavailable:
    print("blocked")
"""
            environment = os.environ.copy()
            environment.update(
                {
                    "TMPDIR": str(alternate_temp),
                    "TMP": str(alternate_temp),
                    "TEMP": str(alternate_temp),
                }
            )
            with knowledge_base_build_lock(kb_dir, mode="write"):
                process = subprocess.run(
                    [sys.executable, "-c", child, str(kb_dir)],
                    cwd=ROOT,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "blocked")

    def test_stable_lock_root_resolver_has_no_filesystem_side_effect(self) -> None:
        with patch.object(
            Path,
            "mkdir",
            side_effect=AssertionError("resolver must not create directories"),
        ):
            root = knowledge_base_lock_root()
        self.assertTrue(root.is_absolute())

    def test_darwin_lock_key_normalizes_case_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            build_lock_module.sys, "platform", "darwin"
        ):
            root = Path(tmp)
            composed = root / "CAF\N{LATIN SMALL LETTER E WITH ACUTE}"
            decomposed = root / "cafe\N{COMBINING ACUTE ACCENT}"
            self.assertEqual(
                build_lock_module._build_lock_key(composed),
                build_lock_module._build_lock_key(decomposed),
            )

    def test_manifest_fast_path_rejects_every_generation_artifact(self) -> None:
        for artifact_kind in (
            "card-backup",
            "index-backup",
            "live-marker",
            "install-marker",
        ):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as tmp:
                kb_dir = Path(tmp)
                self._write_inventory_manifest(kb_dir)
                if artifact_kind == "card-backup":
                    (kb_dir / "._cards.backup-orphan").mkdir()
                elif artifact_kind == "index-backup":
                    (kb_dir / "._index.backup-orphan").mkdir()
                elif artifact_kind == "live-marker":
                    (kb_dir / "_index" / ".openmobius-regenerate-index.json").write_text(
                        "{}", encoding="utf-8"
                    )
                else:
                    (kb_dir / INSTALL_GENERATION_MARKER).write_text(
                        "{}", encoding="utf-8"
                    )
                with (
                    self.assertLogs("kb_retrieve", level="ERROR") as logs,
                    redirect_stdout(io.StringIO()) as stdout,
                ):
                    result = kb_retrieve.main(
                        [
                            "--kb",
                            str(kb_dir),
                            "--layer",
                            "school",
                            "--list-schools",
                            "--format",
                            "json",
                        ]
                    )

                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("拒绝读取可能混合代际", "\n".join(logs.output))

    def test_direct_retriever_rejects_installer_generation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            (kb_dir / INSTALL_GENERATION_MARKER).write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "install/update"):
                Retriever(kb_dir, None)

            # Constructor failure must not leak the read lease.
            with knowledge_base_build_lock(kb_dir, mode="write"):
                pass

    def test_repeated_cli_sessions_release_their_read_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp)
            self._write_inventory_manifest(kb_dir)
            for _ in range(2):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        kb_retrieve.main(
                            [
                                "--kb",
                                str(kb_dir),
                                "--layer",
                                "school",
                                "--list-schools",
                                "--format",
                                "json",
                            ]
                        ),
                        0,
                    )

            outcomes: list[str] = []

            def acquire_writer() -> None:
                try:
                    with knowledge_base_build_lock(kb_dir, mode="write"):
                        outcomes.append("acquired")
                except BuildLockUnavailable:
                    outcomes.append("blocked")

            writer = threading.Thread(target=acquire_writer)
            writer.start()
            writer.join(timeout=5)
            self.assertFalse(writer.is_alive())
            self.assertEqual(outcomes, ["acquired"])

    def test_source_retriever_blocks_real_builder_process_without_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            _write_source_registry(kb_dir)
            child = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import build_index
sys.argv = ["build_index.py", "--kb", sys.argv[1]]
raise SystemExit(build_index.main())
"""
            with patch.object(
                knowledge_v2,
                "build_v2_records",
                return_value=_source_build_result(),
            ):
                retriever = Retriever.from_source(kb_dir, layer="school")
                try:
                    before_cards = [
                        card.card_id
                        for card in retriever.search(
                            "FVG",
                            top_k=2,
                            filter_schools=["ICT"],
                            search_mode="lexical",
                        )
                    ]
                    before_files = self._file_snapshot(kb_dir)
                    process = subprocess.run(
                        [sys.executable, "-c", child, str(kb_dir)],
                        cwd=ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    after_cards = [
                        card.card_id
                        for card in retriever.search(
                            "FVG",
                            top_k=2,
                            filter_schools=["ICT"],
                            search_mode="lexical",
                        )
                    ]
                finally:
                    retriever.close()

            self.assertEqual(process.returncode, 1)
            self.assertIn("another process is active", process.stderr)
            self.assertEqual(before_cards, ["sp-source-fvg"])
            self.assertEqual(after_cards, before_cards)
            self.assertEqual(self._file_snapshot(kb_dir), before_files)

    def test_writer_with_active_artifact_blocks_reader_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            kb_dir.mkdir()
            (kb_dir / "._cards.backup-regenerate-active").mkdir()
            before = self._file_snapshot(kb_dir)
            child = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from scripts import kb_retrieve
raise SystemExit(kb_retrieve.main([
    "--kb", sys.argv[1], "--layer", "school", "--list-schools"
]))
"""
            with knowledge_base_build_lock(kb_dir, mode="write"):
                process = subprocess.run(
                    [sys.executable, "-c", child, str(kb_dir)],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stdout, "")
            self.assertIn("拒绝混合代际检索", process.stderr)
            self.assertEqual(self._file_snapshot(kb_dir), before)

    def test_last_chroma_retriever_close_stops_shared_backend(self) -> None:
        import chromadb

        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            index_dir = kb_dir / "_index"
            client = chromadb.PersistentClient(path=str(index_dir))
            client.create_collection("school_knowledge_v2").add(
                ids=["record"],
                embeddings=[[1.0, 0.0]],
                documents=["record"],
                metadatas=[{"school": "ICT", "type": "concept"}],
            )
            client.close()

            first = Retriever(kb_dir, None, layer="school")
            second = Retriever(kb_dir, None, layer="school")
            system = first.client._system
            self.assertIs(system, second.client._system)
            first.close()
            self.assertTrue(system._running)
            self.assertEqual(second.collection.count(), 1)
            second.close()

            self.assertFalse(system._running)
            self.assertIsNone(first.collection)
            self.assertIsNone(second.collection)
            with knowledge_base_build_lock(kb_dir, mode="write"):
                pass

    def test_sqlite_fallback_close_releases_connection_before_lease(self) -> None:
        with _read_only_fixture() as (kb_dir, _database_path), patch.dict(
            sys.modules, {"chromadb": _readonly_chromadb_module()}
        ), self.assertLogs("_lib.retriever", level="WARNING"):
            retriever = Retriever(kb_dir, None, layer="school")
            connection = retriever.collection._connection
            retriever.close()
            self.assertIsNone(retriever.collection)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            with knowledge_base_build_lock(kb_dir, mode="write"):
                pass

    def test_cli_closes_borrowed_backend_before_outer_lease_exits(self) -> None:
        import chromadb

        instances: list[Retriever] = []

        class TrackingRetriever(Retriever):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(self)

        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = Path(tmp) / "knowledge_base"
            index_dir = kb_dir / "_index"
            client = chromadb.PersistentClient(path=str(index_dir))
            client.create_collection("school_knowledge_v2").add(
                ids=["record"],
                embeddings=[[1.0, 0.0]],
                documents=["ICT record term"],
                metadatas=[
                    {
                        "canonical_id": "record",
                        "term": "Record",
                        "school": "ICT",
                        "type": "concept",
                        "file_path": "concepts/record.json",
                    }
                ],
            )
            client.close()
            (kb_dir / "schools.json").write_text(
                json.dumps({"schools": [{"name": "ICT", "aliases": []}]}),
                encoding="utf-8",
            )

            with (
                patch.object(kb_retrieve, "Retriever", TrackingRetriever),
                redirect_stdout(io.StringIO()),
            ):
                result = kb_retrieve.main(
                    [
                        "record",
                        "--kb",
                        str(kb_dir),
                        "--layer",
                        "school",
                        "--search-mode",
                        "lexical",
                        "--format",
                        "json",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(instances), 1)
            self.assertTrue(instances[0]._closed)
            self.assertIsNone(instances[0].client)
            self.assertIsNone(instances[0].collection)
            with knowledge_base_build_lock(kb_dir, mode="write"):
                pass


if __name__ == "__main__":
    unittest.main()
