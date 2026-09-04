"""向量检索：从 ChromaDB 持久化索引按 query 找 top-K cards。"""

from __future__ import annotations

import json
import logging
import math
import re
import shlex
import sqlite3
import threading
import unicodedata
from array import array
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .build_lock import (
    INSTALL_GENERATION_MARKER,
    current_thread_has_borrowable_read_session,
    knowledge_base_build_lock,
    register_borrowed_read_cleanup,
)
from .compact_v2 import (
    COMPACT_V2_FILENAME,
    load_compact_v2_records,
)


log = logging.getLogger(__name__)


_CHROMA_CLIENT_REFS: dict[int, dict[str, Any]] = {}
_CHROMA_CLIENT_REFS_GUARD = threading.Lock()


def _register_chroma_client(client: Any) -> Optional[int]:
    """Track only legacy Chroma clients that lack their own close method."""
    if callable(getattr(client, "close", None)):
        # Current Chroma clients already reference-count their shared System;
        # every Client must receive its own close() call.
        return None
    system = getattr(client, "_system", None)
    key = id(system) if system is not None else id(client)
    with _CHROMA_CLIENT_REFS_GUARD:
        entry = _CHROMA_CLIENT_REFS.get(key)
        if entry is None:
            _CHROMA_CLIENT_REFS[key] = {"count": 1, "client": client}
        else:
            entry["count"] += 1
    return key


def _release_chroma_client(client: Any, key: Optional[int]) -> None:
    """Close one client, with a shared-System fallback for old Chroma."""
    close = getattr(client, "close", None)
    if callable(close):
        close()
        return
    if key is None:
        return
    should_close = False
    close_client = client
    with _CHROMA_CLIENT_REFS_GUARD:
        entry = _CHROMA_CLIENT_REFS.get(key)
        if entry is None:
            return
        entry["count"] -= 1
        if entry["count"] == 0:
            close_client = entry["client"]
            del _CHROMA_CLIENT_REFS[key]
            should_close = True
    if not should_close:
        return
    system = getattr(close_client, "_system", None)
    stop = getattr(system, "stop", None)
    if callable(stop):
        stop()


LAYER_COLLECTIONS = {
    "canonical": "knowledge_base",
    "school": "school_knowledge_v2",
    "evidence": "source_evidence_v2",
}

SEARCH_MODES = ("auto", "hybrid", "semantic", "lexical")
INDEX_MANIFEST_VERSION = 2
NOMIC_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
NOMIC_MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
REGEN_INDEX_MARKER_FILE = ".openmobius-regenerate-index.json"


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def assert_readable_generation(kb_dir: Path) -> None:
    """Refuse a live/mixed KB until the builder's recovery has completed."""
    kb_dir = Path(kb_dir)
    index_dir = kb_dir / "_index"
    artifacts = [
        *sorted(kb_dir.glob("._cards.backup-*")),
        *sorted(kb_dir.glob("._index.backup-*")),
    ]
    marker = index_dir / REGEN_INDEX_MARKER_FILE
    if _path_present(marker):
        artifacts.append(marker)
    install_marker = kb_dir / INSTALL_GENERATION_MARKER
    if _path_present(install_marker):
        artifacts.append(install_marker)
    artifacts = list(dict.fromkeys(artifacts))
    if artifacts:
        raise RuntimeError(
            "知识库存在未完成的 install/update 或 regenerate 事务，拒绝"
            "读取可能混合代际的 cards/index。请先恢复/重跑相应安装或 "
            "scripts/build_index.py 操作。"
            "事务证据: " + ", ".join(str(path) for path in artifacts)
        )


def resolve_search_mode(mode: str, layer: str) -> str:
    """Resolve the public search mode without changing legacy canonical behavior."""
    normalized = str(mode or "auto").strip().lower()
    if normalized not in SEARCH_MODES:
        raise ValueError(
            f"未知 search mode {mode!r}；可用值: {', '.join(SEARCH_MODES)}"
        )
    if normalized == "auto":
        return "semantic" if normalize_layer(layer) == "canonical" else "hybrid"
    return normalized


_DOTTED_ACRONYM_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z]\.)+[a-z]\.?(?![a-z0-9])"
)
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+(?:['_-][a-z0-9]+)*")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def tokenize_for_lexical_search(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text for dependency-free BM25.

    Latin text uses normalized words. CJK runs emit characters and bigrams so
    both short terms (``缠论``) and longer natural-language queries can match.
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    dotted_acronyms: list[str] = []

    def collapse_dotted_acronym(match: re.Match) -> str:
        collapsed = match.group(0).replace(".", "")
        dotted_acronyms.append(collapsed)
        # Spaces prevent the ordinary word matcher from also emitting each
        # acronym letter as an unrelated token.
        return " " * len(match.group(0))

    latin_text = _DOTTED_ACRONYM_RE.sub(collapse_dotted_acronym, normalized)
    tokens = dotted_acronyms
    for token in _LATIN_TOKEN_RE.findall(latin_text):
        tokens.append(token)
        # Keep the compound for precision, but also emit its components so
        # ``order-block`` and ``order block`` have useful lexical overlap.
        if "-" in token or "_" in token or "'" in token:
            tokens.extend(part for part in re.split(r"[-_']+", token) if part)
    for run in _CJK_RUN_RE.findall(normalized):
        tokens.extend(run)
        if len(run) > 1:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _canonicalize_where_value(value: Any, parent_key: str = "") -> Any:
    """Canonicalize commutative Chroma filter expressions for cache keys."""
    if isinstance(value, dict):
        return {
            key: _canonicalize_where_value(value[key], key)
            for key in sorted(value)
        }
    if isinstance(value, list):
        items = [_canonicalize_where_value(item, parent_key) for item in value]
        if parent_key in {"$and", "$or", "$in", "$nin"}:
            by_json = {}
            for item in items:
                serialized = json.dumps(
                    item,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                by_json[serialized] = item
            return [by_json[key] for key in sorted(by_json)]
        return items
    return value


def lexical_scope_cache_key(where: Optional[dict]) -> str:
    """Return a stable key for semantically equivalent metadata scopes."""
    return json.dumps(
        _canonicalize_where_value(where),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def bm25_rank(
    query: str,
    documents: Sequence[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[int, float]]:
    """Return ``(document index, score)`` pairs ranked by BM25.

    Zero-overlap documents are intentionally omitted: hybrid mode can still
    surface them through semantic retrieval, while lexical-only mode remains
    honest about having no lexical match.
    """
    query_terms = list(dict.fromkeys(tokenize_for_lexical_search(query)))
    if not query_terms or not documents:
        return []

    tokenized = [tokenize_for_lexical_search(document) for document in documents]
    frequencies = [Counter(tokens) for tokens in tokenized]
    lengths = [len(tokens) for tokens in tokenized]
    return _bm25_rank_prepared(
        query_terms,
        frequencies,
        lengths,
        k1=k1,
        b=b,
    )


def _bm25_rank_prepared(
    query_terms: Sequence[str],
    frequencies: Sequence[Counter],
    lengths: Sequence[int],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[int, float]]:
    """BM25 core over a tokenized corpus, reused by Retriever instances."""
    doc_count = len(frequencies)
    avg_length = sum(lengths) / max(doc_count, 1)
    if avg_length <= 0:
        return []

    document_frequency = {
        term: sum(1 for counts in frequencies if counts.get(term, 0) > 0)
        for term in query_terms
    }
    ranked: list[tuple[int, float]] = []
    for index, counts in enumerate(frequencies):
        length = lengths[index]
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = math.log(1.0 + (doc_count - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (
                1.0 - b + b * length / avg_length
            )
            score += inverse_frequency * frequency * (k1 + 1.0) / denominator
        if score > 0:
            ranked.append((index, score))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


class RetrievalScopeError(ValueError):
    """The requested hard-filter scope is invalid or matches no records."""


def normalize_layer(layer: str) -> str:
    """Validate and normalize a public retrieval-layer name."""
    normalized = str(layer or "").strip().lower()
    if normalized not in LAYER_COLLECTIONS:
        choices = ", ".join(LAYER_COLLECTIONS)
        raise RetrievalScopeError(
            f"未知知识层 {layer!r}；可用值: {choices}"
        )
    return normalized


def normalize_filter_values(values: Optional[Sequence[str]]) -> list[str]:
    """Return non-empty, de-duplicated filter values in input order."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def build_where_filter(
    *,
    filter_school: Optional[str] = None,
    filter_schools: Optional[Sequence[str]] = None,
    filter_sources: Optional[Sequence[str]] = None,
    exclude_schools: Optional[Sequence[str]] = None,
    filter_type: Optional[str] = None,
) -> Optional[dict]:
    """Build a Chroma-compatible metadata filter.

    Chroma requires a single expression at the top level. Multiple fields
    therefore have to be joined explicitly with ``$and``; multiple schools
    are an OR scope represented by ``$in`` on the scalar ``school`` field.

    ``filter_school`` is retained for callers using the original API. When
    both forms are supplied their values are combined and de-duplicated.
    """
    schools = normalize_filter_values(
        ([filter_school] if filter_school else [])
        + normalize_filter_values(filter_schools)
    )

    excluded = normalize_filter_values(exclude_schools)
    overlap = sorted(set(schools).intersection(excluded))
    if overlap:
        raise RetrievalScopeError(
            "同一 School 不能同时包含和排除: " + ", ".join(overlap)
        )

    sources = normalize_filter_values(filter_sources)
    clauses: list[dict] = []
    if len(schools) == 1:
        clauses.append({"school": schools[0]})
    elif schools:
        clauses.append({"school": {"$in": schools}})
    if len(excluded) == 1:
        clauses.append({"school": {"$ne": excluded[0]}})
    elif excluded:
        clauses.append({"school": {"$nin": excluded}})
    if len(sources) == 1:
        clauses.append({"source": sources[0]})
    elif sources:
        clauses.append({"source": {"$in": sources}})
    if filter_type:
        clauses.append({"type": filter_type})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def combine_where_filters(*filters: Optional[dict]) -> Optional[dict]:
    """Combine already-built Chroma filters without emitting invalid syntax."""
    clauses = [item for item in filters if item]
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def normalize_exact_text(value: str) -> str:
    """Normalize canonical terms and aliases for deterministic exact lookup."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.casefold().split())


@dataclass
class RetrievedCard:
    """一条检索结果。"""
    card_id: str            # 卡片唯一 id（文件名 stem）
    card_type: str          # "concept" / "case"
    term: str               # 概念名 / 案例标题
    school: str             # 流派
    file_path: str          # 相对于 knowledge_base/ 的路径
    document: str           # 检索时存的拼接文本
    distance: Optional[float]  # 向量距离（越小越相关；纯 lexical 为 None）
    metadata: dict          # 完整 metadata
    match_kind: str = "semantic"  # exact / hybrid / semantic / lexical
    fusion_score: float = 0.0      # RRF score for hybrid results
    semantic_rank: Optional[int] = None
    lexical_rank: Optional[int] = None
    lexical_score: float = 0.0     # BM25 score; meaningful for lexical matches


@dataclass(slots=True)
class _LexicalScopeIndex:
    """Compact BM25 corpus for one already-filtered Chroma scope.

    Token strings live only as posting-list keys. Posting document ids, term
    frequencies, and document lengths use packed unsigned-int arrays instead
    of retaining one Python ``Counter`` (and its token strings) per record.
    """

    record_ids: list[str]
    document_lengths: array
    postings: dict[str, array]
    total_document_length: int

    @property
    def record_count(self) -> int:
        return len(self.record_ids)


class ReadOnlySearchModeError(RuntimeError):
    """Raised when a read-only index cannot honor an explicit vector mode."""


@dataclass(slots=True)
class _ReadOnlyRecord:
    record_id: str
    metadata: dict
    document: str


def _is_read_only_open_error(error: BaseException) -> bool:
    """Return whether a Chroma startup failure is a filesystem write denial."""
    messages: list[str] = []
    current: Optional[BaseException] = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current).casefold())
        current = current.__cause__ or current.__context__
    text = "\n".join(messages)
    return any(
        marker in text
        for marker in (
            "attempt to write a readonly database",
            "attempt to write a read-only database",
            "read-only file system",
            "readonly filesystem",
            "permission denied",
            "access is denied",
        )
    )


def _matches_read_only_where(metadata: dict, where: Optional[dict]) -> bool:
    """Evaluate the Chroma metadata-filter subset emitted by this project."""
    if not where:
        return True
    if not isinstance(where, dict):
        raise ValueError("read-only metadata filter must be an object")

    for key, condition in where.items():
        if key == "$and":
            if not isinstance(condition, list) or not all(
                _matches_read_only_where(metadata, clause)
                for clause in condition
            ):
                return False
            continue
        if key == "$or":
            if not isinstance(condition, list) or not any(
                _matches_read_only_where(metadata, clause)
                for clause in condition
            ):
                return False
            continue
        if key.startswith("$"):
            raise ValueError(f"unsupported read-only filter operator: {key}")

        exists = key in metadata
        actual = metadata.get(key)
        if not isinstance(condition, dict):
            if not exists or actual != condition:
                return False
            continue
        for operator, expected in condition.items():
            if operator == "$eq":
                matched = exists and actual == expected
            elif operator == "$ne":
                matched = exists and actual != expected
            elif operator == "$in":
                matched = exists and actual in expected
            elif operator == "$nin":
                matched = exists and actual not in expected
            elif operator == "$gt":
                matched = exists and actual > expected
            elif operator == "$gte":
                matched = exists and actual >= expected
            elif operator == "$lt":
                matched = exists and actual < expected
            elif operator == "$lte":
                matched = exists and actual <= expected
            else:
                raise ValueError(
                    f"unsupported read-only filter operator: {operator}"
                )
            if not matched:
                return False
    return True


def _get_read_only_records(
    records: Sequence[_ReadOnlyRecord],
    *,
    ids: Optional[Sequence[str]] = None,
    where: Optional[dict] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    include: Optional[Sequence[str]] = None,
) -> dict:
    """Return a Chroma-shaped page from immutable in-memory records."""
    include = list(include or ["metadatas", "documents"])
    if isinstance(ids, str):
        allowed_ids = {ids}
    else:
        allowed_ids = None if ids is None else {str(item) for item in ids}
    selected = [
        record
        for record in records
        if (allowed_ids is None or record.record_id in allowed_ids)
        and _matches_read_only_where(record.metadata, where)
    ]
    start = max(0, int(offset or 0))
    if limit is None:
        selected = selected[start:]
    else:
        selected = selected[start : start + max(0, int(limit))]

    result: dict[str, Any] = {
        "ids": [record.record_id for record in selected]
    }
    if "metadatas" in include:
        result["metadatas"] = [record.metadata for record in selected]
    if "documents" in include:
        result["documents"] = [record.document for record in selected]
    return result


class _SourceLexicalCollection:
    """Build a lexical-only collection from bundled deterministic JSON."""

    def __init__(self, kb_dir: Path, layer: str) -> None:
        self.name = LAYER_COLLECTIONS[layer]
        kb_dir = Path(kb_dir)
        compact_v2 = kb_dir / COMPACT_V2_FILENAME
        if compact_v2.exists() or compact_v2.is_symlink():
            if layer not in {"school", "evidence"}:
                raise RuntimeError(
                    "this compact host package contains attributable School and "
                    "exact-source evidence layers, but canonical fused-card "
                    "retrieval is unavailable"
                )
            raw_records = load_compact_v2_records(compact_v2, layer)
            records = [
                _ReadOnlyRecord(
                    record_id=str(record["id"]),
                    metadata=dict(record["metadata"]),
                    document=str(record["document"]),
                )
                for record in raw_records
            ]
        elif layer == "canonical":
            # Reuse the canonical text/metadata contract used by build_index;
            # importing the module does not import or start Chroma.
            from build_index import (  # noqa: PLC0415
                build_card_metadata,
                collect_cards,
            )

            records = [
                _ReadOnlyRecord(
                    record_id=str(item["id"]),
                    metadata=build_card_metadata(item),
                    document=str(item["text"]),
                )
                for item in collect_cards(kb_dir)
            ]
        else:
            from .knowledge_v2 import build_v2_records  # noqa: PLC0415

            result = build_v2_records(kb_dir)
            raw_records = (
                result.school_records
                if layer == "school"
                else result.evidence_records
            )
            records = [
                _ReadOnlyRecord(
                    record_id=str(record["id"]),
                    metadata=dict(record["metadata"]),
                    document=str(record["document"]),
                )
                for record in raw_records
            ]
        if not records:
            raise RuntimeError(
                f"bundled knowledge corpus produced no records for layer={layer}"
            )
        self._source_records = records

    def count(self) -> int:
        return len(self._source_records)

    def get(
        self,
        ids: Optional[Sequence[str]] = None,
        where: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[Sequence[str]] = None,
        **_unused: Any,
    ) -> dict:
        return _get_read_only_records(
            self._source_records,
            ids=ids,
            where=where,
            limit=limit,
            offset=offset,
            include=include,
        )

    def query(self, *_args: Any, **_kwargs: Any) -> dict:
        raise ReadOnlySearchModeError(
            "source JSON fallback does not provide vector queries"
        )


class _ReadOnlySQLiteCollection:
    """Read Chroma's metadata segment without starting its writable client.

    Chroma's local ``PersistentClient`` runs write-oriented startup work even
    for queries. Installed skills may instead be mounted read-only. This
    narrow adapter exposes only ``get`` and ``count`` -- the operations needed
    by exact matching, hard-scope validation, and BM25 retrieval -- through an
    immutable SQLite connection. It deliberately has no vector ``query``
    implementation, so callers cannot silently mistake lexical fallback for
    semantic retrieval.
    """

    DEFAULT_TENANT = "default_tenant"
    DEFAULT_DATABASE = "default_database"

    def __init__(
        self,
        index_dir: Path,
        collection_name: str,
        *,
        tenant: str = DEFAULT_TENANT,
        database: str = DEFAULT_DATABASE,
    ) -> None:
        self.name = collection_name
        database_path = (Path(index_dir) / "chroma.sqlite3").resolve()
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Chroma metadata database does not exist: {database_path}"
            )
        uri = database_path.as_uri() + "?mode=ro&immutable=1"
        self._connection = sqlite3.connect(uri, uri=True)
        # Large metadata scans may otherwise spill ORDER BY state into a
        # temporary file. Fully read-only agent sandboxes reject that hidden
        # write and SQLite surfaces only a misleading SQLITE_IOERR.
        self._connection.execute("PRAGMA temp_store = MEMORY")
        self._connection.execute("PRAGMA query_only = ON")
        rows = self._connection.execute(
            """
            SELECT segments.id
              FROM segments
              JOIN collections ON collections.id = segments.collection
              JOIN databases ON databases.id = collections.database_id
             WHERE collections.name = ?
               AND databases.name = ?
               AND databases.tenant_id = ?
               AND (segments.scope = 'METADATA'
                    OR segments.type LIKE '%metadata%')
             ORDER BY CASE WHEN segments.scope = 'METADATA' THEN 0 ELSE 1 END
             LIMIT 2
            """,
            (collection_name, database, tenant),
        ).fetchall()
        if len(rows) != 1:
            self._connection.close()
            if not rows:
                raise RuntimeError(
                    "read-only index has no metadata segment for "
                    f"{tenant}/{database}/{collection_name!r}"
                )
            raise RuntimeError(
                "read-only index has multiple metadata segments for "
                f"{tenant}/{database}/{collection_name!r}"
            )
        self._segment_id = str(rows[0][0])
        self._records_cache: Optional[list[_ReadOnlyRecord]] = None

    def close(self) -> None:
        """Release the immutable SQLite handle before index promotion."""
        connection = getattr(self, "_connection", None)
        if connection is not None:
            self._connection = None
            connection.close()

    @staticmethod
    def _metadata_value(row: tuple) -> Any:
        string_value, int_value, float_value, bool_value = row
        if string_value is not None:
            return string_value
        if int_value is not None:
            return int_value
        if float_value is not None:
            return float_value
        if bool_value is not None:
            return bool(bool_value)
        return None

    def _records(self) -> list[_ReadOnlyRecord]:
        if self._records_cache is not None:
            return self._records_cache

        rows = self._connection.execute(
            """
            SELECT embeddings.id,
                   embeddings.embedding_id,
                   embedding_metadata.key,
                   embedding_metadata.string_value,
                   embedding_metadata.int_value,
                   embedding_metadata.float_value,
                   embedding_metadata.bool_value
              FROM embeddings
              LEFT JOIN embedding_metadata
                ON embedding_metadata.id = embeddings.id
             WHERE embeddings.segment_id = ?
             ORDER BY embeddings.id, embedding_metadata.key
            """,
            (self._segment_id,),
        )
        records: list[_ReadOnlyRecord] = []
        current_row_id: Optional[int] = None
        current_record: Optional[_ReadOnlyRecord] = None
        for (
            row_id,
            embedding_id,
            key,
            string_value,
            int_value,
            float_value,
            bool_value,
        ) in rows:
            if row_id != current_row_id:
                current_record = _ReadOnlyRecord(
                    record_id=str(embedding_id), metadata={}, document=""
                )
                records.append(current_record)
                current_row_id = row_id
            if current_record is None or key is None:
                continue
            value = self._metadata_value(
                (string_value, int_value, float_value, bool_value)
            )
            if key == "chroma:document":
                current_record.document = str(value or "")
            elif not str(key).startswith("chroma:"):
                current_record.metadata[str(key)] = value
        self._records_cache = records
        return records

    def count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM embeddings WHERE segment_id = ?",
            (self._segment_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def get(
        self,
        ids: Optional[Sequence[str]] = None,
        where: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[Sequence[str]] = None,
        **_unused: Any,
    ) -> dict:
        return _get_read_only_records(
            self._records(),
            ids=ids,
            where=where,
            limit=limit,
            offset=offset,
            include=include,
        )

    def query(self, *_args: Any, **_kwargs: Any) -> dict:
        raise ReadOnlySearchModeError(
            "read-only SQLite fallback does not provide vector queries"
        )


class Retriever:
    """ChromaDB 检索器。"""

    COLLECTION_NAME = "knowledge_base"
    COLLECTION_NAMES = LAYER_COLLECTIONS
    LEXICAL_CACHE_MAX_SCOPES = 4
    LEXICAL_CACHE_RECORD_BUDGET = 25_000
    LEXICAL_INDEX_PAGE_SIZE = 1_000

    def __init__(self, kb_dir: Path, embedder, *, layer: str = "canonical") -> None:
        """
        Args:
            kb_dir: 知识库目录（如 materials/<name>/knowledge_base/）
            embedder: rag.embedder 的实例（LocalNomicEmbedder 或 OpenAIEmbedder）
        """
        self.kb_dir = Path(kb_dir)
        self.embedder = embedder
        self.layer = normalize_layer(layer)
        self.collection_name = self.COLLECTION_NAMES[self.layer]
        self._metadata_cache: Optional[list[tuple[str, dict]]] = None
        self._alias_cache: Optional[dict[str, list[tuple[str, str]]]] = None
        self._school_registry_cache: Optional[list[dict]] = None
        self._lexical_cache: OrderedDict[str, _LexicalScopeIndex] = OrderedDict()
        self._lexical_cache_records = 0
        self.read_only_fallback = False
        self.read_only_fallback_reason: Optional[str] = None
        self.source_lexical_fallback = False
        self._enforce_embedding_identity = True
        self._generation_lock_context = None
        self._generation_lock_held = False
        self._generation_lock_borrowed = False
        self._chroma_client_ref_key: Optional[int] = None
        self._closed = False
        self._acquire_generation_lock()
        try:
            self._initialize_index()
        except BaseException:
            self.close()
            raise

    def _acquire_generation_lock(self) -> None:
        if current_thread_has_borrowable_read_session(self.kb_dir):
            # The CLI owns a longer lease spanning construction, search,
            # canonical JSON hydration and output serialization. Borrow it
            # instead of depending on object finalization to drop lock depth.
            assert_readable_generation(self.kb_dir)
            self._generation_lock_borrowed = True
            register_borrowed_read_cleanup(self.kb_dir, self.close)
            return
        context = knowledge_base_build_lock(self.kb_dir, mode="read")
        context.__enter__()
        self._generation_lock_context = context
        self._generation_lock_held = True
        try:
            assert_readable_generation(self.kb_dir)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close index handles before releasing this Retriever's lease."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self._close_backend()
        except Exception as exc:  # noqa: BLE001 - always release OS lease
            log.warning("failed to close retrieval backend cleanly: %s", exc)
        finally:
            if getattr(self, "_generation_lock_held", False):
                context = self._generation_lock_context
                self._generation_lock_held = False
                self._generation_lock_context = None
                context.__exit__(None, None, None)
            self._generation_lock_borrowed = False

    def _close_backend(self) -> None:
        client = getattr(self, "client", None)
        reference_key = getattr(self, "_chroma_client_ref_key", None)
        collection = getattr(self, "collection", None)
        self.client = None
        self.collection = None
        self._chroma_client_ref_key = None
        if client is not None:
            _release_chroma_client(client, reference_key)
            return
        close = getattr(collection, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "Retriever":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise from finalization
            pass

    def _initialize_index(self) -> None:
        index_dir = self.kb_dir / "_index"
        if not index_dir.is_dir():
            raise FileNotFoundError(
                f"向量索引不存在: {index_dir}\n"
                f"请先跑：.venv/bin/python scripts/build_index.py"
            )
        database_path = index_dir / "chroma.sqlite3"
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Chroma metadata database does not exist: {database_path}\n"
                f"请先跑：.venv/bin/python scripts/build_index.py"
            )
        import chromadb  # noqa: PLC0415

        try:
            self.client = chromadb.PersistentClient(path=str(index_dir))
            self._chroma_client_ref_key = _register_chroma_client(self.client)
            self.collection = self.client.get_collection(self.collection_name)
        except Exception as e:
            if _is_read_only_open_error(e):
                try:
                    self._close_backend()
                    self.collection = _ReadOnlySQLiteCollection(
                        index_dir, self.collection_name
                    )
                except Exception as fallback_error:
                    raise RuntimeError(
                        f"Chroma 无法打开索引，SQLite 只读回退也失败："
                        f"{fallback_error}\n索引目录: {index_dir}"
                    ) from e
                self.client = None
                self.read_only_fallback = True
                self.read_only_fallback_reason = str(e)
                log.warning(
                    "Chroma 索引不可写；已通过 SQLite 只读模式打开 %s。"
                    "向量查询不可用，auto 将明确降级为 lexical。",
                    self.collection_name,
                )
                return
            if self.layer == "canonical":
                hint = "请先跑：.venv/bin/python scripts/build_index.py"
            else:
                hint = (
                    "该知识层需要 v2 索引；请跑："
                    ".venv/bin/python scripts/build_index.py "
                    f"--kb {shlex.quote(str(self.kb_dir))} --upgrade\n"
                    f"所需 collection: '{self.collection_name}'"
                )
            raise RuntimeError(
                f"无法加载 collection '{self.collection_name}' "
                f"(layer={self.layer})：{e}\n"
                f"索引目录: {index_dir}\n{hint}"
            )

    @classmethod
    def from_source(cls, kb_dir: Path, *, layer: str) -> "Retriever":
        """Create an explicit lexical-only retriever without Chroma/index.

        The CLI calls this only when ``--search-mode lexical`` was explicitly
        selected and ``_index`` is completely absent. Keeping this as a
        separate constructor prevents an unavailable or damaged vector index
        from being silently masked by source JSON.
        """
        self = cls.__new__(cls)
        self.kb_dir = Path(kb_dir)
        self.embedder = None
        self.layer = normalize_layer(layer)
        self.collection_name = self.COLLECTION_NAMES[self.layer]
        self._metadata_cache = None
        self._alias_cache = None
        self._school_registry_cache = None
        self._lexical_cache = OrderedDict()
        self._lexical_cache_records = 0
        self.client = None
        self.read_only_fallback = False
        self.read_only_fallback_reason = None
        self.source_lexical_fallback = True
        self._enforce_embedding_identity = False
        self._generation_lock_context = None
        self._generation_lock_held = False
        self._generation_lock_borrowed = False
        self._chroma_client_ref_key = None
        self._closed = False
        self._acquire_generation_lock()
        try:
            self.collection = _SourceLexicalCollection(self.kb_dir, self.layer)
        except BaseException:
            self.close()
            raise
        log.warning(
            "向量索引不存在；已从 bundled knowledge corpus/确定性投影创建 "
            "lexical-only 检索。无向量语义排序。"
        )
        return self

    def available_search_mode(self, requested_mode: str) -> str:
        """Resolve search mode against capabilities of the opened index."""
        mode = resolve_search_mode(
            requested_mode, getattr(self, "layer", "canonical")
        )
        if getattr(self, "source_lexical_fallback", False):
            if mode == "lexical":
                return mode
            raise ReadOnlySearchModeError(
                "source JSON fallback 仅支持显式 --search-mode lexical；"
                "无向量语义排序。请构建可用的 _index 后再使用 "
                f"--search-mode {mode}。"
            )
        if not getattr(self, "read_only_fallback", False) or mode == "lexical":
            return mode
        if str(requested_mode or "auto").strip().lower() == "auto":
            return "lexical"
        raise ReadOnlySearchModeError(
            f"只读索引无法执行显式 --search-mode {mode}：Chroma 的本地 "
            "PersistentClient 启动时需要写数据库。未自动改变显式检索语义；"
            "请使用可写安装目录，或明确指定 --search-mode lexical "
            "（BM25 + exact alias，无向量语义排序）。"
        )

    def _metadata_records(self) -> list[tuple[str, dict]]:
        """Load collection metadata once for selector validation/inventory."""
        if self._metadata_cache is not None:
            return self._metadata_cache
        result = self.collection.get(include=["metadatas"])
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        self._metadata_cache = [
            (str(record_id), metadata or {})
            for record_id, metadata in zip(ids, metadatas)
        ]
        return self._metadata_cache

    def metadata_value_counts(self, field: str) -> dict[str, int]:
        """Return non-empty scalar metadata values and their record counts."""
        counts: dict[str, int] = {}
        for _, metadata in self._metadata_records():
            value = metadata.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))

    def list_schools(self) -> dict[str, int]:
        """List Schools actually present in the selected collection."""
        return self.metadata_value_counts("school")

    def list_sources(self) -> dict[str, int]:
        """List exact-filterable sources in the evidence collection."""
        if self.layer != "evidence":
            raise RetrievalScopeError(
                "source 是 evidence 层的单值字段；"
                "canonical/school 层不能保证来源级硬过滤"
            )
        return self.metadata_value_counts("source")

    def _school_registry(self) -> list[dict]:
        """Load the optional School registry used for selector aliases."""
        cached = getattr(self, "_school_registry_cache", None)
        if cached is not None:
            return cached
        kb_dir = getattr(self, "kb_dir", None)
        if kb_dir is None:
            self._school_registry_cache = []
            return []
        try:
            payload = json.loads(
                (kb_dir / "schools.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            self._school_registry_cache = []
            return []
        entries = [
            entry for entry in (payload.get("schools") or [])
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"].strip()
        ]
        self._school_registry_cache = entries
        return entries

    def _school_alias_map(self) -> dict[str, str]:
        """Return case-insensitive registry id/name/alias → canonical name."""
        alias_map: dict[str, str] = {}
        for entry in self._school_registry():
            canonical = entry["name"].strip()
            values = [entry.get("id"), canonical, *(entry.get("aliases") or [])]
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    continue
                key = normalize_exact_text(value)
                existing = alias_map.get(key)
                if existing is not None and existing != canonical:
                    raise RetrievalScopeError(
                        f"School registry alias 冲突: {value!r} -> "
                        f"{existing!r}/{canonical!r}"
                    )
                alias_map[key] = canonical
        return alias_map

    def school_inventory(self) -> list[dict]:
        """Return available Schools enriched with registry capabilities."""
        counts = self.list_schools()
        registry = {
            entry["name"].strip(): entry
            for entry in self._school_registry()
        }
        inventory: list[dict] = []
        for name, count in counts.items():
            entry = registry.get(name, {})
            item = {
                "name": name,
                "count": count,
                "available_in_layer": True,
                "aliases": entry.get("aliases") or [],
            }
            for field in (
                "id",
                "kind",
                "availability",
                "knowledge_qna",
                "native_market_analyzer",
            ):
                if field in entry:
                    item[field] = entry[field]
            inventory.append(item)
        return inventory

    @staticmethod
    def _resolve_known_values(
        requested: Sequence[str],
        available: dict[str, int],
        selector_name: str,
    ) -> list[str]:
        """Resolve case-insensitively, while rejecting unknown selectors."""
        by_folded: dict[str, list[str]] = {}
        for value in available:
            by_folded.setdefault(value.casefold(), []).append(value)

        resolved: list[str] = []
        unknown: list[str] = []
        for value in normalize_filter_values(requested):
            if value in available:
                resolved.append(value)
                continue
            candidates = by_folded.get(value.casefold(), [])
            if len(candidates) == 1:
                resolved.append(candidates[0])
            else:
                unknown.append(value)

        if unknown:
            available_text = ", ".join(available) or "(none)"
            raise RetrievalScopeError(
                f"未知 {selector_name}: {', '.join(unknown)}；"
                f"当前 layer 可用值: {available_text}"
            )
        return normalize_filter_values(resolved)

    def _resolve_school_values(
        self,
        requested: Sequence[str],
        selector_name: str,
    ) -> list[str]:
        """Canonicalize registry aliases, then require layer availability."""
        alias_map = self._school_alias_map()
        canonicalized = [
            alias_map.get(normalize_exact_text(value), value)
            for value in normalize_filter_values(requested)
        ]
        return self._resolve_collection_values(
            canonicalized, "school", selector_name
        )

    def _metadata_value_exists(self, field: str, value: str) -> bool:
        """Check an exact scalar value without transferring all metadata."""
        result = self.collection.get(
            where={field: value},
            limit=1,
            include=["metadatas"],
        )
        return bool(result.get("ids") or [])

    def _resolve_collection_values(
        self,
        requested: Sequence[str],
        field: str,
        selector_name: str,
    ) -> list[str]:
        """Prefer cheap exact checks; load inventory only for case fallback."""
        resolved: list[str] = []
        for value in normalize_filter_values(requested):
            if self._metadata_value_exists(field, value):
                resolved.append(value)
                continue
            # This path both supports case-insensitive raw selectors and emits
            # the complete available set in a useful fail-closed error.
            fallback = self._resolve_known_values(
                [value], self.metadata_value_counts(field), selector_name
            )
            resolved.extend(fallback)
        return normalize_filter_values(resolved)

    def resolve_scope(
        self,
        *,
        filter_schools: Optional[Sequence[str]] = None,
        filter_sources: Optional[Sequence[str]] = None,
        exclude_schools: Optional[Sequence[str]] = None,
        filter_type: Optional[str] = None,
    ) -> dict:
        """Validate hard selectors and return their canonical collection values.

        This method deliberately validates against metadata in the selected
        collection. A selector that exists in another layer, or a valid set of
        selectors whose intersection is empty, fails closed instead of silently
        widening the query.
        """
        if filter_schools is not None and not normalize_filter_values(filter_schools):
            raise RetrievalScopeError("School selector 不能为空")
        if filter_sources is not None and not normalize_filter_values(filter_sources):
            raise RetrievalScopeError("source selector 不能为空")
        if exclude_schools is not None and not normalize_filter_values(exclude_schools):
            raise RetrievalScopeError("exclude-school selector 不能为空")
        if filter_sources is not None and self.layer != "evidence":
            raise RetrievalScopeError(
                "--sources 只支持 layer=evidence。canonical/school 层的 "
                "source_names 是聚合字段，不能用它伪装精确来源过滤"
            )

        schools = self._resolve_school_values(
            filter_schools or [], "School selector"
        )
        excluded = self._resolve_school_values(
            exclude_schools or [], "excluded School selector"
        )
        sources: list[str] = []
        if filter_sources is not None:
            sources = self._resolve_collection_values(
                filter_sources, "source", "source selector"
            )

        where = build_where_filter(
            filter_schools=schools,
            filter_sources=sources,
            exclude_schools=excluded,
            filter_type=filter_type,
        )
        if where:
            matches = self.collection.get(
                where=where,
                limit=1,
                include=["metadatas"],
            )
            if not (matches.get("ids") or []):
                raise RetrievalScopeError(
                    "所请求的 School/source/type 硬过滤交集为空；"
                    "查询已停止，不会扩大到其他知识范围"
                )

        return {
            "schools": schools,
            "sources": sources,
            "excluded_schools": excluded,
            "type": filter_type,
            "where": where,
        }

    def _load_alias_lookup(self) -> dict[str, list[tuple[str, str]]]:
        """Load canonical-term/alias → (canonical id, canonical term)."""
        cached = getattr(self, "_alias_cache", None)
        if cached is not None:
            return cached
        lookup: dict[str, list[tuple[str, str]]] = {}
        kb_dir = getattr(self, "kb_dir", None)
        if kb_dir is None:
            self._alias_cache = lookup
            return lookup
        alias_path = kb_dir / "term_aliases.json"
        try:
            payload = json.loads(alias_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._alias_cache = lookup
            return lookup

        for mapping in payload.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            canonical_id = str(mapping.get("card_id") or "").strip()
            canonical_term = str(mapping.get("canonical") or "").strip()
            if not canonical_id or not canonical_term:
                continue
            values = [canonical_term, *(mapping.get("aliases") or [])]
            for value in values:
                if not isinstance(value, str) or not normalize_exact_text(value):
                    continue
                key = normalize_exact_text(value)
                target = (canonical_id, canonical_term)
                if target not in lookup.setdefault(key, []):
                    lookup[key].append(target)
        self._alias_cache = lookup
        return lookup

    def _exact_filter(self, query: str) -> Optional[dict]:
        """Build an exact canonical/alias filter usable across v1 and v2."""
        targets = self._load_alias_lookup().get(normalize_exact_text(query), [])
        if not targets:
            return None
        canonical_ids = normalize_filter_values([target[0] for target in targets])
        canonical_terms = normalize_filter_values([target[1] for target in targets])
        clauses: list[dict] = []
        if canonical_ids:
            clauses.extend([
                {"canonical_id": {"$in": canonical_ids}},
                {"card_id": {"$in": canonical_ids}},
            ])
        if canonical_terms:
            clauses.append({"term": {"$in": canonical_terms}})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$or": clauses}

    @staticmethod
    def _cards_from_query_result(results: dict, match_kind: str) -> list[RetrievedCard]:
        """Convert a single-query Chroma result into RetrievedCard objects."""
        ids = (results.get("ids") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        documents = (results.get("documents") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        cards: list[RetrievedCard] = []
        for i, record_id in enumerate(ids):
            meta = metadatas[i] or {}
            cards.append(RetrievedCard(
                card_id=record_id,
                card_type=meta.get("type", "?"),
                term=meta.get("term", "?"),
                school=meta.get("school", ""),
                file_path=meta.get("file_path", ""),
                document=documents[i] if i < len(documents) else "",
                distance=distances[i] if i < len(distances) else None,
                metadata=meta,
                match_kind=match_kind,
            ))
        return cards

    @staticmethod
    def _cards_from_get_result(results: dict) -> list[RetrievedCard]:
        """Convert a Chroma ``get`` result used by lexical retrieval."""
        ids = results.get("ids") or []
        metadatas = results.get("metadatas") or []
        documents = results.get("documents") or []
        cards: list[RetrievedCard] = []
        for index, record_id in enumerate(ids):
            meta = metadatas[index] if index < len(metadatas) else {}
            meta = meta or {}
            cards.append(RetrievedCard(
                card_id=str(record_id),
                card_type=meta.get("type", "?"),
                term=meta.get("term", "?"),
                school=meta.get("school", ""),
                file_path=meta.get("file_path", ""),
                document=documents[index] if index < len(documents) else "",
                distance=None,
                metadata=meta,
                match_kind="lexical",
            ))
        return cards

    def _semantic_candidates(
        self,
        query_embedding: list[float],
        where: Optional[dict],
        candidate_k: int,
    ) -> list[RetrievedCard]:
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=candidate_k,
            where=where,
            include=["metadatas", "documents", "distances"],
        )
        cards = self._cards_from_query_result(results, "semantic")
        for rank, card in enumerate(cards, 1):
            card.semantic_rank = rank
        return cards

    def _lexical_candidates(
        self,
        query: str,
        where: Optional[dict],
        candidate_k: int,
    ) -> list[RetrievedCard]:
        """Rank records admitted by Chroma, materializing only top hits."""
        lexical_index = self._lexical_scope_index(where)
        ranking = self._rank_lexical_scope(query, lexical_index)
        selected_ranking = ranking[:candidate_k]
        if not selected_ranking:
            return []

        selected_ids = [
            lexical_index.record_ids[index] for index, _ in selected_ranking
        ]
        get_kwargs = {
            "ids": selected_ids,
            "include": ["metadatas", "documents"],
        }
        if where is not None:
            # The ids already came from a filtered posting index. Keeping the
            # same filter on hydration makes the isolation boundary explicit
            # and protects against a concurrently replaced collection.
            get_kwargs["where"] = where
        hydrated = self._cards_from_get_result(
            self.collection.get(**get_kwargs)
        )
        hydrated_by_id = {card.card_id: card for card in hydrated}
        ranked: list[RetrievedCard] = []
        for rank, (index, score) in enumerate(selected_ranking, 1):
            card = hydrated_by_id.get(lexical_index.record_ids[index])
            if card is None:
                continue
            card.match_kind = "lexical"
            card.fusion_score = 0.0
            card.lexical_score = score
            card.semantic_rank = None
            card.lexical_rank = rank
            ranked.append(card)
        return ranked

    def _build_lexical_scope_index(
        self, where: Optional[dict]
    ) -> _LexicalScopeIndex:
        """Page through a filtered scope and build packed postings."""
        record_ids: list[str] = []
        document_lengths = array("I")
        postings: dict[str, array] = {}
        total_document_length = 0
        seen_ids: set[str] = set()
        offset = 0
        page_size = max(1, int(getattr(self, "LEXICAL_INDEX_PAGE_SIZE", 1_000)))
        while True:
            get_kwargs = {
                "limit": page_size,
                "offset": offset,
                "include": ["documents"],
            }
            if where is not None:
                get_kwargs["where"] = where
            results = self.collection.get(**get_kwargs)
            raw_ids = results.get("ids") or []
            raw_documents = results.get("documents") or []
            added = 0
            for index, raw_id in enumerate(raw_ids):
                record_id = str(raw_id)
                if record_id in seen_ids:
                    continue
                seen_ids.add(record_id)
                document_id = len(record_ids)
                record_ids.append(record_id)
                document = (
                    str(raw_documents[index] or "")
                    if index < len(raw_documents)
                    else ""
                )
                frequencies = Counter(tokenize_for_lexical_search(document))
                document_length = sum(frequencies.values())
                document_lengths.append(document_length)
                total_document_length += document_length
                for token, frequency in frequencies.items():
                    posting = postings.get(token)
                    if posting is None:
                        posting = array("I")
                        postings[token] = posting
                    posting.extend((document_id, frequency))
                added += 1
            if len(raw_ids) < page_size or added == 0:
                break
            offset += len(raw_ids)
        return _LexicalScopeIndex(
            record_ids=record_ids,
            document_lengths=document_lengths,
            postings=postings,
            total_document_length=total_document_length,
        )

    def _lexical_scope_index(self, where: Optional[dict]) -> _LexicalScopeIndex:
        """Return an LRU-cached packed index for an already-filtered scope."""
        cache = getattr(self, "_lexical_cache", None)
        if not isinstance(cache, OrderedDict):
            cache = OrderedDict(cache or {})
            self._lexical_cache = cache
        if not hasattr(self, "_lexical_cache_records"):
            self._lexical_cache_records = sum(
                item.record_count
                for item in cache.values()
                if isinstance(item, _LexicalScopeIndex)
            )

        cache_key = lexical_scope_cache_key(where)
        cached = cache.get(cache_key)
        if isinstance(cached, _LexicalScopeIndex):
            cache.move_to_end(cache_key)
            return cached

        lexical_index = self._build_lexical_scope_index(where)

        max_scopes = max(
            0, int(getattr(self, "LEXICAL_CACHE_MAX_SCOPES", 0))
        )
        record_budget = max(
            0, int(getattr(self, "LEXICAL_CACHE_RECORD_BUDGET", 0))
        )
        if (
            max_scopes > 0
            and record_budget > 0
            and lexical_index.record_count <= record_budget
        ):
            while cache and (
                len(cache) >= max_scopes
                or self._lexical_cache_records + lexical_index.record_count
                > record_budget
            ):
                _, evicted = cache.popitem(last=False)
                if isinstance(evicted, _LexicalScopeIndex):
                    self._lexical_cache_records -= evicted.record_count
            cache[cache_key] = lexical_index
            self._lexical_cache_records += lexical_index.record_count
        return lexical_index

    @staticmethod
    def _rank_lexical_scope(
        query: str,
        lexical_index: _LexicalScopeIndex,
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> list[tuple[int, float]]:
        """Rank a compact posting index with BM25."""
        query_terms = list(dict.fromkeys(tokenize_for_lexical_search(query)))
        document_count = lexical_index.record_count
        if (
            not query_terms
            or not document_count
            or lexical_index.total_document_length <= 0
        ):
            return []
        average_length = lexical_index.total_document_length / document_count
        scores: dict[int, float] = {}
        for term in query_terms:
            posting = lexical_index.postings.get(term)
            if posting is None:
                continue
            document_frequency = len(posting) // 2
            inverse_frequency = math.log(
                1.0
                + (document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            for posting_index in range(0, len(posting), 2):
                document_id = posting[posting_index]
                frequency = posting[posting_index + 1]
                document_length = lexical_index.document_lengths[document_id]
                denominator = frequency + k1 * (
                    1.0 - b + b * document_length / average_length
                )
                scores[document_id] = scores.get(document_id, 0.0) + (
                    inverse_frequency * frequency * (k1 + 1.0) / denominator
                )
        ranked = list(scores.items())
        ranked.sort(key=lambda item: (
            -item[1], lexical_index.record_ids[item[0]]
        ))
        return ranked

    @staticmethod
    def _rrf_candidates(
        semantic_cards: Sequence[RetrievedCard],
        lexical_cards: Sequence[RetrievedCard],
        *,
        rank_constant: int = 60,
    ) -> list[RetrievedCard]:
        """Fuse semantic and BM25 ranks with reciprocal-rank fusion."""
        by_id: dict[str, RetrievedCard] = {}
        scores: dict[str, float] = {}
        for rank, source_card in enumerate(semantic_cards, 1):
            card = replace(source_card, semantic_rank=rank)
            by_id[card.card_id] = card
            scores[card.card_id] = scores.get(card.card_id, 0.0) + (
                1.0 / (rank_constant + rank)
            )
        for rank, lexical_card in enumerate(lexical_cards, 1):
            existing = by_id.get(lexical_card.card_id)
            if existing is None:
                card = replace(lexical_card, lexical_rank=rank)
            else:
                card = replace(
                    existing,
                    lexical_rank=rank,
                    lexical_score=lexical_card.lexical_score,
                )
            by_id[card.card_id] = card
            scores[card.card_id] = scores.get(card.card_id, 0.0) + (
                1.0 / (rank_constant + rank)
            )

        fused: list[RetrievedCard] = []
        for card in by_id.values():
            if card.semantic_rank is not None and card.lexical_rank is not None:
                match_kind = "hybrid"
            elif card.semantic_rank is not None:
                match_kind = "semantic"
            else:
                match_kind = "lexical"
            fused.append(replace(
                card,
                match_kind=match_kind,
                fusion_score=scores[card.card_id],
            ))
        fused.sort(key=lambda card: (
            -card.fusion_score,
            card.semantic_rank or 10**9,
            card.lexical_rank or 10**9,
            card.card_id,
        ))
        return fused

    @staticmethod
    def _limit_and_diversify(
        cards: Sequence[RetrievedCard],
        top_k: int,
        max_per_canonical: Optional[int],
    ) -> list[RetrievedCard]:
        """Limit repeated projections/evidence from one canonical parent."""
        selected: list[RetrievedCard] = []
        counts: dict[str, int] = {}
        for card in cards:
            canonical_id = str(
                card.metadata.get("canonical_id")
                or card.metadata.get("card_id")
                or card.card_id
            )
            if (
                max_per_canonical is not None
                and counts.get(canonical_id, 0) >= max_per_canonical
            ):
                continue
            counts[canonical_id] = counts.get(canonical_id, 0) + 1
            selected.append(card)
            if len(selected) == top_k:
                break
        return selected

    def _validate_query_embedding_identity(self) -> None:
        """Require the loaded query embedder to match the indexed vectors."""
        manifest_path = self.kb_dir / "_index" / "index_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"semantic/hybrid 检索需要可验证的 index manifest: "
                f"{manifest_path}: {exc}"
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("manifest_version") != INDEX_MANIFEST_VERSION
        ):
            raise RuntimeError(
                "index embedding identity 缺失或已过期；请使用当前版本 "
                "scripts/build_index.py --force 重建"
            )

        models = manifest.get("embedding_models")
        revisions = manifest.get("embedding_revisions")
        dimensions = manifest.get("embedding_dimensions")
        if not all(
            isinstance(value, dict)
            for value in (models, revisions, dimensions)
        ):
            raise RuntimeError(
                "index manifest 缺少 model/revision/dimension identity；请重建索引"
            )
        collection_name = self.collection_name
        if collection_name not in revisions:
            raise RuntimeError(
                f"index manifest 未声明 {collection_name} 的 model revision"
            )

        model_name = getattr(self.embedder, "model_name", None)
        model_revision = getattr(self.embedder, "model_revision", None)
        dimension = getattr(self.embedder, "dim", None)
        if model_name == NOMIC_MODEL_ID and model_revision != NOMIC_MODEL_REVISION:
            raise RuntimeError(
                "query Nomic embedder 没有加载项目固定的 model revision"
            )
        actual = (
            models.get(collection_name),
            revisions.get(collection_name),
            dimensions.get(collection_name),
        )
        expected = (model_name, model_revision, dimension)
        if actual != expected:
            raise RuntimeError(
                f"query/index embedding identity mismatch for {collection_name}: "
                f"index={actual!r}, query={expected!r}; 请重建索引或选择匹配的 embedder"
            )

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_school: Optional[str] = None,
        filter_type: Optional[str] = None,
        *,
        filter_schools: Optional[Sequence[str]] = None,
        filter_sources: Optional[Sequence[str]] = None,
        exclude_schools: Optional[Sequence[str]] = None,
        strict_scope: bool = False,
        exact_match: bool = True,
        search_mode: str = "auto",
        max_per_canonical: Optional[int] = None,
    ) -> list[RetrievedCard]:
        """检索 top-K 相关 cards。

        Args:
            query: 自然语言查询
            top_k: 返回数量
            filter_school: 限定单个流派（如 "ICT"），保留用于向后兼容
            filter_type: 限定类型（"concept" 或 "case"）
            filter_schools: 限定一个或多个流派（OR 语义）
            filter_sources: 限定一个或多个来源（OR；仅 evidence 层）
            exclude_schools: 排除一个或多个流派
            strict_scope: 校验 selector 且在空交集时停止
            exact_match: 将 canonical term/alias 的精确匹配置顶
            search_mode: auto/hybrid/semantic/lexical；auto 在 v2 使用 hybrid
            max_per_canonical: 同一 canonical parent 最多返回几条；v2 默认 2
        """
        if filter_sources is not None and not normalize_filter_values(filter_sources):
            raise RetrievalScopeError("source selector 不能为空")
        if filter_sources is not None and self.layer != "evidence":
            raise RetrievalScopeError(
                "source 硬过滤只支持 evidence 层；"
                "canonical/school 层不会执行不精确的来源过滤"
            )
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        layer = getattr(self, "layer", "canonical")
        mode = self.available_search_mode(search_mode)
        diversity_was_unspecified = max_per_canonical is None
        if max_per_canonical is not None and max_per_canonical < 0:
            raise ValueError("max_per_canonical 不能小于 0")
        if max_per_canonical == 0:
            max_per_canonical = None
        elif diversity_was_unspecified and layer != "canonical":
            max_per_canonical = 2

        if strict_scope:
            school_selector_provided = (
                filter_school is not None or filter_schools is not None
            )
            requested_schools = normalize_filter_values(
                ([filter_school] if filter_school is not None else [])
                + normalize_filter_values(filter_schools)
            )
            resolved = self.resolve_scope(
                filter_schools=requested_schools or (
                    [] if school_selector_provided else None
                ),
                filter_sources=filter_sources,
                exclude_schools=exclude_schools,
                filter_type=filter_type,
            )
            filter_school = None
            filter_schools = resolved["schools"] or None
            filter_sources = resolved["sources"] or None
            exclude_schools = resolved["excluded_schools"] or None

        where = build_where_filter(
            filter_school=filter_school,
            filter_schools=filter_schools,
            filter_sources=filter_sources,
            exclude_schools=exclude_schools,
            filter_type=filter_type,
        )

        needs_overfetch = mode == "hybrid" or max_per_canonical is not None
        candidate_k = max(top_k * 4, 20) if needs_overfetch else top_k
        q_embedding: Optional[list[float]] = None
        if mode in ("semantic", "hybrid"):
            if self.embedder is None:
                raise RuntimeError(
                    f"search_mode={mode} 需要 embedder；lexical 模式可离线运行"
                )
            if getattr(self, "_enforce_embedding_identity", False):
                self._validate_query_embedding_identity()
            q_vec = self.embedder.embed_query(query)
            q_embedding = q_vec.tolist() if hasattr(q_vec, "tolist") else list(q_vec)

        exact_cards: list[RetrievedCard] = []
        exact_filter = self._exact_filter(query) if exact_match else None
        exact_loaded = exact_filter is None
        previous_candidate_ids: Optional[set[str]] = None

        while True:
            semantic_cards: list[RetrievedCard] = []
            if q_embedding is not None:
                semantic_cards = self._semantic_candidates(
                    q_embedding, where, candidate_k
                )

            lexical_cards: list[RetrievedCard] = []
            if mode in ("lexical", "hybrid"):
                lexical_cards = self._lexical_candidates(
                    query, where, candidate_k
                )

            # Preserve the established main-query-before-exact-query call
            # order for API wrappers, while loading the complete lexical exact
            # set once so stable sorting/diversity happen before final top-K.
            if not exact_loaded:
                exact_where = combine_where_filters(where, exact_filter)
                if q_embedding is not None:
                    exact_results = self.collection.query(
                        query_embeddings=[q_embedding],
                        n_results=candidate_k,
                        where=exact_where,
                        include=["metadatas", "documents", "distances"],
                    )
                    exact_cards = self._cards_from_query_result(
                        exact_results, "exact"
                    )
                    exact_cards.sort(key=lambda card: (
                        card.distance is None,
                        card.distance if card.distance is not None else math.inf,
                        card.card_id,
                    ))
                else:
                    get_kwargs = {
                        "where": exact_where,
                        "include": ["metadatas", "documents"],
                    }
                    exact_cards = self._cards_from_get_result(
                        self.collection.get(**get_kwargs)
                    )
                    for card in exact_cards:
                        card.match_kind = "exact"
                    exact_cards.sort(key=lambda card: card.card_id)
                exact_loaded = True

            if mode == "hybrid":
                candidates = self._rrf_candidates(
                    semantic_cards, lexical_cards
                )
            elif mode == "lexical":
                candidates = lexical_cards
            else:
                candidates = semantic_cards

            merged: list[RetrievedCard] = []
            seen: set[str] = set()
            for card in [*exact_cards, *candidates]:
                if card.card_id in seen:
                    continue
                seen.add(card.card_id)
                merged.append(card)
            selected = self._limit_and_diversify(
                merged, top_k, max_per_canonical
            )
            if len(selected) >= top_k:
                return selected

            candidate_ids = {
                card.card_id for card in [*semantic_cards, *lexical_cards]
            }
            if (
                previous_candidate_ids is not None
                and candidate_ids.issubset(previous_candidate_ids)
            ):
                return selected
            previous_candidate_ids = candidate_ids

            semantic_has_more = (
                mode in ("semantic", "hybrid")
                and len(semantic_cards) >= candidate_k
            )
            lexical_has_more = (
                mode in ("lexical", "hybrid")
                and len(lexical_cards) >= candidate_k
            )
            if not semantic_has_more and not lexical_has_more:
                return selected

            next_candidate_k = candidate_k * 2
            try:
                collection_count = int(self.collection.count())
            except Exception:  # noqa: BLE001 - optional optimization only
                collection_count = 0
            if collection_count > 0:
                next_candidate_k = min(next_candidate_k, collection_count)
            if next_candidate_k <= candidate_k:
                return selected
            candidate_k = next_candidate_k

    def get_full_card(self, card: RetrievedCard) -> Optional[dict]:
        """读取 canonical 全卡；v2 层严禁回读跨 School 父卡。"""
        if getattr(self, "layer", "canonical") != "canonical":
            return None
        if not card.file_path:
            return None
        try:
            kb_root = self.kb_dir.resolve()
            full_path = (kb_root / card.file_path).resolve()
            full_path.relative_to(kb_root)
        except (OSError, RuntimeError, ValueError):
            log.error(
                "拒绝读取知识库目录外的卡片: %s",
                card.file_path,
            )
            return None
        if not full_path.exists():
            log.warning("卡片文件不存在: %s", full_path)
            return None
        try:
            return json.loads(full_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.error("读取 %s 失败: %s", full_path, e)
            return None
