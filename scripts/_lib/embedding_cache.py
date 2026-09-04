"""Persistent content-addressed cache for document embeddings.

The cache is deliberately independent from ChromaDB.  A failed index build
may therefore leave useful, complete embedding batches behind without ever
touching the live index.  Entries are isolated by an explicit model key and
the SHA-256 hash of the exact document text.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import sqlite3
import struct
import sys
from array import array
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence


CACHE_SCHEMA_VERSION = 1
_SQLITE_BATCH_SIZE = 500
SEED_FORMAT = "openmobius-v2-embedding-seed"
SEED_FORMAT_VERSION = 2
SEED_DTYPE = "float32"
SEED_MANIFEST_FILE = "manifest.json"
SEED_PREFIXES = tuple("0123456789abcdef")
SEED_HASH_ALGORITHM = "sha256"
SEED_SHARD_SCHEME = "sha256-first-hex-character"


log = logging.getLogger(__name__)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def file_sha256(path: Path) -> str:
    """Hash a file without loading a potentially large shard into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def document_content_hash(document: str) -> str:
    """Return the stable SHA-256 identity of one exact document string."""
    if not isinstance(document, str):
        raise TypeError("embedding cache documents must be strings")
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _normalize_vector(
    vector: Sequence[float], *, expected_dimension: Optional[int] = None
) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if isinstance(vector, (str, bytes, bytearray)):
        raise ValueError("embedding vector must be a one-dimensional sequence")
    try:
        result = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "embedding vector must be a one-dimensional numeric sequence"
        ) from exc
    if not result:
        raise ValueError("embedding vector must not be empty")
    if expected_dimension is not None and len(result) != expected_dimension:
        raise ValueError(
            f"embedding dimension mismatch: {len(result)} != {expected_dimension}"
        )
    if not all(math.isfinite(value) for value in result):
        raise ValueError("embedding vector contains a non-finite value")
    return result


def _encode_vector(vector: Sequence[float]) -> bytes:
    values = _normalize_vector(vector)
    return struct.pack(f"<{len(values)}f", *values)


def _decode_vector(blob: bytes, dimension: int) -> array:
    if dimension <= 0 or len(blob) != dimension * 4:
        raise ValueError("cached embedding payload has an invalid dimension")
    result = array("f")
    if result.itemsize != 4:
        raise RuntimeError("embedding cache requires 32-bit native floats")
    result.frombytes(blob)
    if sys.byteorder != "little":
        result.byteswap()
    if not all(math.isfinite(value) for value in result):
        raise ValueError("cached embedding payload contains a non-finite value")
    return result


class EmbeddingCache:
    """Small SQLite-backed cache keyed by ``(model_key, content_hash)``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "EmbeddingCache":
        self.open()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def open(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30)
        try:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version not in (0, CACHE_SCHEMA_VERSION):
                raise RuntimeError(
                    f"unsupported embedding cache schema: {current_version}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    model_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY (model_key, content_hash)
                )
                """
            )
            if current_version == 0:
                connection.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("embedding cache is not open")
        return self._connection

    def get_many(
        self,
        model_key: str,
        content_hashes: Iterable[str],
        *,
        expected_dimension: Optional[int] = None,
    ) -> dict[str, Sequence[float]]:
        """Return valid cached vectors, silently treating bad rows as misses."""
        hashes = list(dict.fromkeys(content_hashes))
        found: dict[str, Sequence[float]] = {}
        for offset in range(0, len(hashes), _SQLITE_BATCH_SIZE):
            batch = hashes[offset : offset + _SQLITE_BATCH_SIZE]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                "SELECT content_hash, dimension, vector FROM embeddings "
                f"WHERE model_key = ? AND content_hash IN ({placeholders})",
                [model_key, *batch],
            )
            for content_hash, dimension, blob in rows:
                try:
                    if expected_dimension is not None and dimension != expected_dimension:
                        continue
                    found[content_hash] = _decode_vector(blob, dimension)
                except (TypeError, ValueError, struct.error):
                    # A single partial/corrupt row must be recomputed; it must
                    # not make the rest of a valid incremental cache unusable.
                    continue
        return found

    def put_many(
        self,
        model_key: str,
        vectors: Mapping[str, Sequence[float]],
        *,
        expected_dimension: Optional[int] = None,
    ) -> None:
        """Atomically upsert a completed embedding batch."""
        rows = []
        for content_hash, vector in vectors.items():
            normalized = _normalize_vector(
                vector, expected_dimension=expected_dimension
            )
            rows.append(
                (model_key, content_hash, len(normalized), _encode_vector(normalized))
            )
        if not rows:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO embeddings(model_key, content_hash, dimension, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model_key, content_hash) DO UPDATE SET
                    dimension = excluded.dimension,
                    vector = excluded.vector
                """,
                rows,
            )


def write_embedding_seed_shard(
    path: Path,
    content_hashes: Sequence[str],
    vectors: Sequence[Sequence[float]],
    *,
    expected_dimension: int,
) -> dict:
    """Write one deterministic-prefix float32 NPZ shard and describe it.

    This helper is intentionally small and shared by the maintainer export
    command and its tests. The caller owns staging/promotion of the complete
    16-shard asset set.
    """
    if expected_dimension <= 0:
        raise ValueError("seed embedding dimension must be positive")
    path = Path(path)
    prefix = path.stem
    if path.name != f"{prefix}.npz" or prefix not in SEED_PREFIXES:
        raise ValueError("seed shard must be named with one hexadecimal prefix")
    if len(content_hashes) != len(vectors):
        raise ValueError("seed shard hashes/vectors have different lengths")

    ordered = sorted(zip(content_hashes, vectors), key=lambda item: item[0])
    ordered_hashes = [content_hash for content_hash, _vector in ordered]
    if len(set(ordered_hashes)) != len(ordered_hashes):
        raise ValueError(f"seed shard {prefix} contains duplicate hashes")
    for content_hash in ordered_hashes:
        if not _is_sha256(content_hash) or not content_hash.startswith(prefix):
            raise ValueError(f"invalid content hash in seed shard {prefix}")

    # NumPy is a Chroma/SentenceTransformer dependency, but remains a lazy
    # import so legacy-only cache users do not import it at startup.
    import numpy as np  # noqa: PLC0415

    normalized = [
        _normalize_vector(vector, expected_dimension=expected_dimension)
        for _content_hash, vector in ordered
    ]
    hash_array = np.asarray(ordered_hashes, dtype="S64")
    if normalized:
        embedding_array = np.asarray(normalized, dtype="<f4")
    else:
        embedding_array = np.empty((0, expected_dimension), dtype="<f4")
    if not bool(np.isfinite(embedding_array).all()):
        raise ValueError(f"seed shard {prefix} contains non-finite vectors")

    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        content_hashes=hash_array,
        embeddings=embedding_array,
    )
    return {
        "file": path.name,
        "count": len(ordered_hashes),
        "sha256": file_sha256(path),
    }


def _seed_stats(status: str, requested_count: int) -> dict:
    return {
        "status": status,
        "requested_unique_documents": requested_count,
        "seed_hit_unique_documents": 0,
        "corpus_stale": False,
        "manifest_v2_input_fingerprint": None,
        "invalid_shards": [],
        "validated_shards": [],
    }


def load_embedding_seed(
    seed_dir: Path,
    requested_hashes: Iterable[str],
    *,
    expected_model_key: str,
    expected_input_profile: Mapping,
    expected_model: str,
    expected_dimension: int,
    current_v2_fingerprint: str,
    verify_all_shards: bool = False,
) -> tuple[dict[str, Sequence[float]], dict]:
    """Load exact-hash vectors from a verified, read-only embedding seed.

    Global identity mismatches invalidate the whole seed. A malformed or
    corrupt NPZ invalidates only its hexadecimal shard so other prefixes can
    still avoid model work. A corpus fingerprint mismatch is informational:
    exact unchanged document hashes remain safe to reuse.
    """
    requested = list(dict.fromkeys(requested_hashes))
    for content_hash in requested:
        if not _is_sha256(content_hash):
            raise ValueError("requested seed content hash is not lowercase SHA-256")
    if expected_dimension <= 0:
        raise ValueError("seed expected dimension must be positive")
    if not _is_sha256(current_v2_fingerprint):
        raise ValueError("current v2 fingerprint is not lowercase SHA-256")

    seed_dir = Path(seed_dir)
    stats = _seed_stats("missing", len(requested))
    manifest_path = seed_dir / SEED_MANIFEST_FILE
    if not manifest_path.is_file():
        return {}, stats

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log.warning("Ignoring invalid v2 embedding seed manifest %s: %s", manifest_path, exc)
        stats["status"] = "invalid"
        return {}, stats

    expected_profile = json.loads(
        json.dumps(expected_input_profile, ensure_ascii=False, sort_keys=True)
    )
    shard_manifest = manifest.get("shards") if isinstance(manifest, dict) else None
    declared_counts = (
        [
            entry.get("count")
            for entry in shard_manifest.values()
            if isinstance(entry, dict)
        ]
        if isinstance(shard_manifest, dict)
        else []
    )
    all_counts_well_formed = (
        len(declared_counts) == len(SEED_PREFIXES)
        and all(type(count) is int and count >= 0 for count in declared_counts)
    )
    aggregate_count_ok = (
        not all_counts_well_formed
        or sum(declared_counts) == manifest.get("unique_document_count")
    )
    global_identity_ok = (
        isinstance(manifest, dict)
        and manifest.get("format") == SEED_FORMAT
        and type(manifest.get("format_version")) is int
        and manifest.get("format_version") == SEED_FORMAT_VERSION
        and manifest.get("model_key") == expected_model_key
        and manifest.get("input_profile") == expected_profile
        and manifest.get("model") == expected_model
        and "model_revision" in manifest
        and manifest.get("model_revision") == expected_profile.get("model_revision")
        and type(manifest.get("dimension")) is int
        and manifest.get("dimension") == expected_dimension
        and manifest.get("dtype") == SEED_DTYPE
        and manifest.get("content_hash_algorithm") == SEED_HASH_ALGORITHM
        and manifest.get("shard_scheme") == SEED_SHARD_SCHEME
        and _is_sha256(manifest.get("v2_input_fingerprint"))
        and type(manifest.get("unique_document_count")) is int
        and manifest.get("unique_document_count") >= 0
        and isinstance(manifest.get("shards"), dict)
        and set(manifest.get("shards", {})) == set(SEED_PREFIXES)
        and aggregate_count_ok
    )
    if not global_identity_ok:
        log.warning(
            "Ignoring v2 embedding seed with incompatible or invalid manifest: %s",
            manifest_path,
        )
        stats["status"] = "invalid"
        return {}, stats

    manifest_fingerprint = manifest["v2_input_fingerprint"]
    stats["manifest_v2_input_fingerprint"] = manifest_fingerprint
    stats["corpus_stale"] = manifest_fingerprint != current_v2_fingerprint
    if stats["corpus_stale"]:
        log.warning(
            "v2 embedding seed corpus fingerprint is stale; reusing only exact "
            "document-hash matches"
        )

    requested_by_prefix: dict[str, set[str]] = {}
    for content_hash in requested:
        requested_by_prefix.setdefault(content_hash[0], set()).add(content_hash)

    try:
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        log.warning("Ignoring v2 embedding seed because NumPy is unavailable: %s", exc)
        stats["status"] = "invalid"
        return {}, stats

    found: dict[str, Sequence[float]] = {}
    invalid_shards: list[str] = []
    prefixes_to_verify = SEED_PREFIXES if verify_all_shards else sorted(requested_by_prefix)
    validated_shards: list[str] = []
    for prefix in prefixes_to_verify:
        entry = manifest["shards"].get(prefix)
        shard_path = seed_dir / f"{prefix}.npz"
        try:
            if not isinstance(entry, dict):
                raise ValueError("manifest entry is missing or not an object")
            if entry.get("file") != shard_path.name:
                raise ValueError("manifest file name does not match shard prefix")
            count = entry.get("count")
            if type(count) is not int or count < 0:
                raise ValueError("manifest count is invalid")
            expected_asset_hash = entry.get("sha256")
            if not _is_sha256(expected_asset_hash):
                raise ValueError("manifest asset SHA-256 is invalid")
            if not shard_path.is_file():
                raise ValueError("asset is missing")
            # Shards are intentionally ~4 MB. Holding these exact bytes while
            # validating and loading closes the hash-then-reopen race without
            # materially affecting the full-corpus working set.
            asset_bytes = shard_path.read_bytes()
            if hashlib.sha256(asset_bytes).hexdigest() != expected_asset_hash:
                raise ValueError("asset SHA-256 does not match manifest")

            with np.load(io.BytesIO(asset_bytes), allow_pickle=False) as archive:
                if set(archive.files) != {"content_hashes", "embeddings"}:
                    raise ValueError("asset arrays do not match the seed format")
                hash_array = archive["content_hashes"]
                embedding_array = archive["embeddings"]

            if hash_array.dtype.kind != "S" or hash_array.dtype.itemsize != 64:
                raise ValueError("content_hashes must use fixed-width S64")
            if hash_array.shape != (count,):
                raise ValueError("content_hashes shape/count mismatch")
            if embedding_array.dtype != np.dtype("float32"):
                raise ValueError("embeddings dtype is not float32")
            if embedding_array.shape != (count, expected_dimension):
                raise ValueError("embeddings shape/count/dimension mismatch")
            if not bool(np.isfinite(embedding_array).all()):
                raise ValueError("embeddings contain non-finite values")

            decoded_hashes: list[str] = []
            for raw_hash in hash_array.tolist():
                try:
                    content_hash = raw_hash.decode("ascii")
                except (AttributeError, UnicodeDecodeError) as exc:
                    raise ValueError("content_hashes contain non-ASCII data") from exc
                if not _is_sha256(content_hash) or not content_hash.startswith(prefix):
                    raise ValueError("content_hashes contain an invalid shard member")
                decoded_hashes.append(content_hash)
            if decoded_hashes != sorted(decoded_hashes):
                raise ValueError("content_hashes are not deterministically sorted")
            if len(set(decoded_hashes)) != len(decoded_hashes):
                raise ValueError("content_hashes contain duplicates")

            positions = {content_hash: index for index, content_hash in enumerate(decoded_hashes)}
            for content_hash in requested_by_prefix.get(prefix, set()):
                position = positions.get(content_hash)
                if position is not None:
                    found[content_hash] = array(
                        "f", embedding_array[position].tolist()
                    )
            validated_shards.append(prefix)
        except Exception as exc:  # noqa: BLE001 - one bad asset is an isolated miss
            invalid_shards.append(prefix)
            log.warning(
                "Ignoring invalid v2 embedding seed shard %s: %s",
                shard_path,
                exc,
            )

    stats["invalid_shards"] = invalid_shards
    stats["validated_shards"] = validated_shards
    stats["seed_hit_unique_documents"] = len(found)
    stats["status"] = "ready_with_invalid_shards" if invalid_shards else "ready"
    return found, stats
