#!/usr/bin/env python3
"""Build canonical, School-projection, and source-evidence vector indexes.

This script has two modes:

  Default mode — incremental install path (used by every user):
    Reads pre-computed embeddings from each JSON card's `_embedding` field
    for the canonical collection. School/evidence documents use an incremental
    local cache and verified release seed; only remaining misses load the
    selected embedding model.

  --regenerate — KB maintainer path:
    Loads the embedding model, computes embeddings for every card, writes
    them back into the JSON cards' `_embedding` / `_embedding_model` fields,
    then writes ChromaDB. Takes 30 s – 10 min depending on CPU.

Usage:
    python scripts/build_index.py                       # fast load from JSON
    python scripts/build_index.py --force               # rebuild ChromaDB only
    python scripts/build_index.py --upgrade             # safely add/update v2 layers
    python scripts/build_index.py --regenerate          # recompute only if no index exists
    python scripts/build_index.py --regenerate --force  # full rebuild
    python scripts/build_index.py --limit 10            # only first N (testing)
    python scripts/build_index.py --v2-embedding-strategy inherit  # emergency
    python scripts/build_index.py --regenerate --embedder openai  # different model
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


THIS_DIR = Path(__file__).resolve().parent        # scripts/
SKILL_DIR = THIS_DIR.parent                       # skill root
sys.path.insert(0, str(THIS_DIR))                 # for _lib import

from _lib.embedding_cache import (
    EmbeddingCache,
    document_content_hash,
    load_embedding_seed,
)
from _lib.build_lock import BuildLockUnavailable, knowledge_base_build_lock


log = logging.getLogger("build_kb_index")


# The canonical embedding model used to populate `_embedding` fields in the
# bundled knowledge_base/ JSON cards. If --regenerate uses a different model
# (e.g. openai), this constant doesn't change — the per-card
# `_embedding_model` field reflects the actual model that produced its vector.
EXPECTED_MODEL = "nomic-ai/nomic-embed-text-v1.5"
EXPECTED_MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
EXPECTED_DIM   = 768
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDING_DIM = 1536

# The payload/record contract remains v2.  Index schema v3 denotes that v2
# collections use their own document embeddings instead of parent vectors.
RECORD_SCHEMA_VERSION = 2
INDEX_SCHEMA_VERSION = 3
INDEX_MANIFEST_VERSION = 2

LEGACY_COLLECTION = "knowledge_base"
SCHOOL_COLLECTION = "school_knowledge_v2"
EVIDENCE_COLLECTION = "source_evidence_v2"
V2_COLLECTIONS = (SCHOOL_COLLECTION, EVIDENCE_COLLECTION)
INDEX_MANIFEST_FILE = "index_manifest.json"
V2_EMBEDDING_CACHE = "_embedding_cache/v2_embeddings.sqlite3"
V2_EMBEDDING_SEED_DIR = "embedding_seed_v2"
V2_NATIVE_STRATEGY = "native_document"
V2_INHERITED_STRATEGY = "inherited_parent_card"
V2_EMBEDDING_INPUT_VERSION = "search-document-v2-maxseq512"
V2_INHERITED_INPUT_VERSION = "inherited-parent-card-v1"
V2_NATIVE_MAX_SEQ_LENGTH = 512
V2_EMBEDDING_BATCH_SIZE = 128
REGEN_TRANSACTION_DIR = "._cards.backup-regenerate-active"
REGEN_PREPARE_PREFIX = "._cards.build-transaction-"
REGEN_JOURNAL_FILE = "journal.json"
REGEN_JOURNAL_VERSION = 1
REGEN_INDEX_MARKER_FILE = ".openmobius-regenerate-index.json"
REGEN_INDEX_BACKUP_PREFIX = "._index.backup-regenerate-"
REGEN_PHASES = {
    "prepared",
    "cards_promoted",
    "rolling_back",
    "committed",
    "unpublished_cleanup",
}
CANONICAL_EMBEDDING_INPUT_PROFILE = {
    "version": "search-document-v1-maxseq8192",
    "provider": "local",
    "task": "search_document",
    "max_seq_length": 8192,
    "model_revision": EXPECTED_MODEL_REVISION,
}


class KnowledgeCardLoadError(ValueError):
    """A canonical source card is unreadable or cannot produce index text."""


@dataclass
class _CardPromotion:
    """Backups retained while regenerated cards and their index are committed."""

    backup_dir: Path
    entries: list[tuple[Path, Path]]


@dataclass
class _RegenerationTransaction:
    """Validated paths and durable state for one regeneration transaction."""

    root: Path
    kb_dir: Path
    index_dir: Path
    staged_cards_dir: Path
    staging_index_dir: Path
    card_backup_dir: Path
    index_backup_dir: Path
    journal: dict


def _json_safe(value):
    """Return a deterministic JSON-compatible copy for manifests/hashes."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def load_optional_v2_records(kb_dir: Path) -> tuple[list[dict], list[dict], dict]:
    """Load deterministic v2 records when the optional builder is present.

    The legacy index is intentionally independent of the v2 data model. An
    older checkout, or an installation where v2 generation is not available
    yet, therefore continues to build and use ``knowledge_base`` normally.
    Errors *inside* an available builder are not swallowed: a bad v2 build
    must fail before the live index is replaced.
    """
    registry_path = Path(kb_dir) / "schools.json"
    if not registry_path.is_file():
        log.info("v2 School registry is absent; building legacy collection only")
        return [], [], {"available": False, "reason": "v2_data_missing"}

    try:
        module = importlib.import_module("_lib.knowledge_v2")
    except ModuleNotFoundError as exc:
        if exc.name not in {"_lib.knowledge_v2", "knowledge_v2"}:
            raise
        log.info("v2 knowledge builder is not installed; building legacy collection only")
        return [], [], {"available": False, "reason": "builder_missing"}

    builder = getattr(module, "build_v2_records", None)
    if builder is None:
        log.info("v2 knowledge builder has no build_v2_records(); legacy mode remains active")
        return [], [], {"available": False, "reason": "builder_unavailable"}

    result = builder(kb_dir)
    school_records = list(getattr(result, "school_records", []) or [])
    evidence_records = list(getattr(result, "evidence_records", []) or [])
    stats = _json_safe(getattr(result, "stats", {}) or {})
    stats.update({
        "available": bool(school_records or evidence_records),
        "school_record_count": len(school_records),
        "evidence_record_count": len(evidence_records),
    })
    return school_records, evidence_records, stats


def normalize_v2_records(records: list[dict], *, layer: str) -> list[dict]:
    """Validate and normalize the in-memory v2 record contract for Chroma."""
    if layer not in {"school", "evidence"}:
        raise ValueError(f"unsupported v2 layer: {layer}")

    normalized: list[dict] = []
    seen: set[str] = set()
    for position, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise ValueError(f"{layer} record #{position} must be an object")
        record_id = str(raw.get("id") or "").strip()
        document = raw.get("document")
        metadata = raw.get("metadata")
        if not record_id:
            raise ValueError(f"{layer} record #{position} has no id")
        if record_id in seen:
            raise ValueError(f"duplicate {layer} record id: {record_id}")
        if not isinstance(document, str) or not document.strip():
            raise ValueError(f"{layer} record {record_id!r} has no document")
        if not isinstance(metadata, dict):
            raise ValueError(f"{layer} record {record_id!r} has no metadata object")

        metadata = dict(metadata)
        metadata["schema_version"] = RECORD_SCHEMA_VERSION
        metadata["layer"] = layer
        metadata["record_id"] = record_id
        if layer == "evidence":
            metadata.setdefault("evidence_id", record_id)

        required = ["canonical_id", "type", "term", "school", "file_path"]
        if layer == "evidence":
            required.extend(["source", "content_type", "ref"])
        missing = [name for name in required if not str(metadata.get(name) or "").strip()]
        if layer == "school":
            missing.extend(
                name
                for name in ("source_names", "source_collection_count")
                if name not in metadata
            )
        if missing:
            raise ValueError(
                f"{layer} record {record_id!r} missing metadata: {', '.join(missing)}"
            )
        if layer == "evidence" and not isinstance(metadata["source"], str):
            raise ValueError(
                f"evidence record {record_id!r} requires one exact scalar source"
            )

        # Chroma 0.5+ accepts scalar metadata only. Keep JSON arrays encoded
        # rather than making the supported dependency range narrower.
        for key, value in list(metadata.items()):
            if isinstance(value, (list, dict, tuple)):
                metadata[key] = json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            elif value is None:
                del metadata[key]
            elif not isinstance(value, (str, int, float, bool)):
                metadata[key] = str(value)

        normalized.append({
            "id": record_id,
            "document": document.strip(),
            "metadata": metadata,
        })
        seen.add(record_id)
    return normalized


def metadata_value_counts(records: list[dict], field: str) -> dict[str, int]:
    """Return sorted non-empty string metadata counts for an index manifest."""
    counts: dict[str, int] = {}
    for record in records:
        value = (record.get("metadata") or {}).get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))


def v2_input_profile(strategy: str, provider: str) -> dict:
    """Return the versioned document-input contract for one v2 policy."""
    if strategy not in {V2_NATIVE_STRATEGY, V2_INHERITED_STRATEGY}:
        raise ValueError(f"unsupported v2 embedding strategy: {strategy}")
    if strategy == V2_INHERITED_STRATEGY:
        return {
            "version": V2_INHERITED_INPUT_VERSION,
            "strategy": strategy,
            "provider": "parent_card",
            "task": "inherited_parent_vector",
            "max_seq_length": None,
        }
    if provider not in {"local", "openai"}:
        raise ValueError(f"unsupported embedding provider: {provider}")
    profile = {
        "version": V2_EMBEDDING_INPUT_VERSION,
        "strategy": strategy,
        "provider": provider,
        "task": "search_document" if provider == "local" else "document",
        # SentenceTransformer exposes this knob; the remote OpenAI provider
        # owns its tokenization/truncation policy.
        "max_seq_length": V2_NATIVE_MAX_SEQ_LENGTH if provider == "local" else None,
    }
    if provider == "local":
        profile["model_revision"] = EXPECTED_MODEL_REVISION
    return profile


def apply_v2_embedding_strategy(
    records: list[dict], strategy: str, input_profile: Optional[dict] = None
) -> None:
    """Make every indexed record report the effective vector/input policy."""
    profile = input_profile or v2_input_profile(strategy, "local")
    if profile.get("strategy") != strategy:
        raise ValueError("v2 input profile does not match embedding strategy")
    encoded_profile = json.dumps(
        profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    for record in records:
        record["metadata"]["embedding_strategy"] = strategy
        record["metadata"]["embedding_input_profile"] = encoded_profile
        record["metadata"]["embedding_input_version"] = profile["version"]
        max_seq_length = profile.get("max_seq_length")
        if isinstance(max_seq_length, int):
            record["metadata"]["embedding_max_seq_length"] = max_seq_length
        else:
            record["metadata"].pop("embedding_max_seq_length", None)


def fingerprint_v2_records(school_records: list[dict], evidence_records: list[dict]) -> str:
    """Hash the effective v2 records so upgrades can cheaply detect drift."""
    payload = {
        SCHOOL_COLLECTION: sorted(school_records, key=lambda record: record["id"]),
        EVIDENCE_COLLECTION: sorted(evidence_records, key=lambda record: record["id"]),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_canonical_items(items: list[dict]) -> str:
    """Hash every indexed canonical document and its inherited parent vector."""
    digest = hashlib.sha256()
    for item in sorted(
        items,
        key=lambda value: (
            str(value.get("type") or ""),
            str(value.get("id") or ""),
            str(value.get("file_path") or ""),
        ),
    ):
        card = item.get("card") or {}
        payload = {
            "id": item.get("id"),
            "type": item.get("type"),
            "file_path": item.get("file_path"),
            "document": item.get("text"),
            "metadata": build_card_metadata(item),
            "embedding_model": card.get("_embedding_model"),
            "embedding_input_profile": CANONICAL_EMBEDDING_INPUT_PROFILE,
            "embedding": card.get("_embedding"),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def v2_embedding_spec(provider: str) -> dict:
    """Resolve the native v2 model without importing/loading the embedder."""
    if provider == "local":
        model = EXPECTED_MODEL
        revision = EXPECTED_MODEL_REVISION
        dimension = EXPECTED_DIM
    elif provider == "openai":
        model = OPENAI_EMBEDDING_MODEL
        revision = None
        dimension = OPENAI_EMBEDDING_DIM
    else:
        raise ValueError(f"unsupported embedding provider: {provider}")
    input_profile = v2_input_profile(V2_NATIVE_STRATEGY, provider)
    max_seq_label = input_profile["max_seq_length"] or "provider-default"
    return {
        "provider": provider,
        "model": model,
        "revision": revision,
        "dimension": dimension,
        "input_profile": input_profile,
        "cache_model_key": (
            f"{V2_EMBEDDING_INPUT_VERSION}:{provider}:{model}:"
            f"revision={revision or 'provider-managed'}:"
            f"max_seq_length={max_seq_label}"
        ),
    }


def vector_as_floats(vector) -> list[float]:
    """Normalize numpy-backed or plain-list vectors for JSON/Chroma."""
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(value) for value in vector]


def embed_documents_without_progress(embedder, documents: list[str]):
    """Call modern embedders quietly while retaining simple mock support."""
    method = embedder.embed_documents
    try:
        parameters = inspect.signature(method).parameters.values()
        supports_progress = any(
            parameter.name == "show_progress_bar"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_progress = False
    if supports_progress:
        return method(documents, show_progress_bar=False)
    return method(documents)


def native_embeddings_for_records(
    records: list[dict],
    *,
    cache_path: Path,
    model_key: str,
    expected_dimension: int,
    embedder_factory: Callable[[], object],
    batch_size: int = V2_EMBEDDING_BATCH_SIZE,
    max_seq_length: Optional[int] = None,
    seed_dir: Optional[Path] = None,
    seed_model: Optional[str] = None,
    seed_input_profile: Optional[dict] = None,
    v2_fingerprint: Optional[str] = None,
) -> tuple[list, dict]:
    """Resolve vectors from SQLite, then the release seed, then the model.

    The factory is intentionally lazy: a fully warm cache never imports or
    instantiates the embedding model.  Identical documents share one cached
    vector even if multiple deterministic v2 records reference that text.
    Read-only seed hits are not copied into SQLite, avoiding a duplicate
    on-disk copy of the shipped float32 assets.
    """
    if batch_size <= 0:
        raise ValueError("v2 embedding batch_size must be positive")
    if expected_dimension <= 0:
        raise ValueError("v2 expected embedding dimension must be positive")

    content_hashes: list[str] = []
    documents_by_hash: dict[str, str] = {}
    for record in records:
        document = record.get("document")
        content_hash = document_content_hash(document)
        previous = documents_by_hash.setdefault(content_hash, document)
        if previous != document:
            raise RuntimeError("SHA-256 collision in v2 embedding documents")
        content_hashes.append(content_hash)

    with EmbeddingCache(cache_path) as cache:
        vectors_by_hash = cache.get_many(
            model_key,
            content_hashes,
            expected_dimension=expected_dimension,
        )
        persistent_hashes = set(vectors_by_hash)
        seed_stats = {
            "status": "not_needed" if seed_dir is not None else "disabled",
            "requested_unique_documents": 0,
            "seed_hit_unique_documents": 0,
            "corpus_stale": False,
            "manifest_v2_input_fingerprint": None,
            "invalid_shards": [],
            "validated_shards": [],
        }
        persistent_misses = [
            content_hash
            for content_hash in documents_by_hash
            if content_hash not in persistent_hashes
        ]
        if seed_dir is not None and persistent_misses:
            if seed_model is None or seed_input_profile is None or v2_fingerprint is None:
                raise ValueError(
                    "seed model, input profile, and v2 fingerprint are required"
                )
            seed_vectors, seed_stats = load_embedding_seed(
                seed_dir,
                persistent_misses,
                expected_model_key=model_key,
                expected_input_profile=seed_input_profile,
                expected_model=seed_model,
                expected_dimension=expected_dimension,
                current_v2_fingerprint=v2_fingerprint,
            )
            vectors_by_hash.update(seed_vectors)
        seed_hashes = set(vectors_by_hash) - persistent_hashes

        # Global deterministic length bucketing materially reduces padding
        # versus builder order. Output order remains the original record order
        # because vectors are mapped back through ``content_hashes`` below.
        missing_hashes = sorted(
            (
                content_hash
                for content_hash in documents_by_hash
                if content_hash not in vectors_by_hash
            ),
            key=lambda key: (len(documents_by_hash[key]), key),
        )
        computed_hashes = set(missing_hashes)

        if missing_hashes:
            embedder = embedder_factory()
            actual_dimension = getattr(embedder, "dim", expected_dimension)
            if actual_dimension != expected_dimension:
                raise ValueError(
                    "v2 embedder dimension mismatch: "
                    f"{actual_dimension} != {expected_dimension}"
                )

            total_batches = (len(missing_hashes) + batch_size - 1) // batch_size
            for batch_number, offset in enumerate(
                range(0, len(missing_hashes), batch_size), start=1
            ):
                batch_hashes = missing_hashes[offset : offset + batch_size]
                batch_documents = [documents_by_hash[key] for key in batch_hashes]
                model = getattr(embedder, "model", None)
                can_set_max_length = (
                    max_seq_length is not None
                    and model is not None
                    and hasattr(model, "max_seq_length")
                )
                original_max_length = None
                if can_set_max_length:
                    original_max_length = model.max_seq_length
                    model.max_seq_length = max_seq_length
                try:
                    raw_vectors = list(
                        embed_documents_without_progress(embedder, batch_documents)
                    )
                finally:
                    # Do not leak the v2 document profile into canonical
                    # regeneration or any later use of this embedder instance.
                    if can_set_max_length:
                        model.max_seq_length = original_max_length
                if len(raw_vectors) != len(batch_hashes):
                    raise ValueError(
                        "v2 embedder returned "
                        f"{len(raw_vectors)} vectors for {len(batch_hashes)} documents"
                    )
                completed: dict[str, array] = {}
                for content_hash, raw_vector in zip(batch_hashes, raw_vectors):
                    vector = vector_as_floats(raw_vector)
                    if len(vector) != expected_dimension:
                        raise ValueError(
                            "v2 embedding dimension mismatch: "
                            f"{len(vector)} != {expected_dimension}"
                        )
                    # Keep the full-corpus working set compact (~4 bytes per
                    # component instead of one Python object per float).
                    completed[content_hash] = array("f", vector)
                # Commit only a complete, validated model batch.  Earlier
                # batches remain useful if a later batch fails.
                cache.put_many(
                    model_key,
                    completed,
                    expected_dimension=expected_dimension,
                )
                vectors_by_hash.update(completed)
                if (
                    batch_number == 1
                    or batch_number % 20 == 0
                    or batch_number == total_batches
                ):
                    log.info(
                        "[v2] Embedded cache-miss batch %d/%d (%d/%d unique docs)",
                        batch_number,
                        total_batches,
                        min(batch_number * batch_size, len(missing_hashes)),
                        len(missing_hashes),
                    )

    stats = {
        "record_count": len(records),
        "unique_document_count": len(documents_by_hash),
        "persistent_cache_hit_records": sum(
            1 for content_hash in content_hashes if content_hash in persistent_hashes
        ),
        "persistent_cache_hit_unique_documents": len(persistent_hashes),
        "seed_hit_records": sum(
            1 for content_hash in content_hashes if content_hash in seed_hashes
        ),
        "seed_hit_unique_documents": len(seed_hashes),
        "computed_records": sum(
            1 for content_hash in content_hashes if content_hash in computed_hashes
        ),
        "cache_hit_records": sum(
            1 for content_hash in content_hashes if content_hash in persistent_hashes
        ),
        "cache_miss_records": sum(
            1 for content_hash in content_hashes if content_hash not in persistent_hashes
        ),
        "computed_unique_documents": len(computed_hashes),
        "seed": seed_stats,
    }
    return [vectors_by_hash[content_hash] for content_hash in content_hashes], stats


def read_index_manifest(index_dir: Path) -> Optional[dict]:
    """Read an index manifest, returning ``None`` for legacy/invalid files."""
    path = index_dir / INDEX_MANIFEST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def index_has_current_v2(
    index_dir: Path,
    *,
    fingerprint: str,
    canonical_fingerprint: str,
    legacy_count: int,
    school_count: int,
    evidence_count: int,
    v2_embedding_strategy: str = V2_NATIVE_STRATEGY,
    v2_embedding_model: str = EXPECTED_MODEL,
    v2_embedding_dimension: int = EXPECTED_DIM,
    v2_embedding_input_profile: Optional[dict] = None,
) -> bool:
    """Verify manifest and physical Chroma collections match all current input."""
    manifest = read_index_manifest(index_dir)
    if not manifest or manifest.get("manifest_version") != INDEX_MANIFEST_VERSION:
        return False
    if manifest.get("index_schema_version") != INDEX_SCHEMA_VERSION:
        return False
    if manifest.get("v2_input_fingerprint") != fingerprint:
        return False
    if manifest.get("canonical_input_fingerprint") != canonical_fingerprint:
        return False
    expected_input_profile = v2_embedding_input_profile or v2_input_profile(
        v2_embedding_strategy,
        "local" if v2_embedding_strategy == V2_NATIVE_STRATEGY else "parent_card",
    )
    if manifest.get("v2_embedding_input_profile") != expected_input_profile:
        return False
    strategies = manifest.get("embedding_strategy") or {}
    models = manifest.get("embedding_models") or {}
    revisions = manifest.get("embedding_revisions") or {}
    dimensions = manifest.get("embedding_dimensions") or {}
    if (
        manifest.get("embedding_model") == EXPECTED_MODEL
        and manifest.get("embedding_model_revision") != EXPECTED_MODEL_REVISION
    ):
        return False
    if (
        manifest.get("embedding_model") == EXPECTED_MODEL
        and manifest.get("canonical_embedding_input_profile")
        != CANONICAL_EMBEDDING_INPUT_PROFILE
    ):
        return False
    if (
        LEGACY_COLLECTION not in revisions
        or revisions.get(LEGACY_COLLECTION)
        != manifest.get("embedding_model_revision")
    ):
        return False
    for name in V2_COLLECTIONS:
        if strategies.get(name) != v2_embedding_strategy:
            return False
        if models.get(name) != v2_embedding_model:
            return False
        if name not in revisions:
            return False
        expected_revision = (
            EXPECTED_MODEL_REVISION
            if v2_embedding_model == EXPECTED_MODEL
            else None
        )
        if revisions.get(name) != expected_revision:
            return False
        if dimensions.get(name) != v2_embedding_dimension:
            return False
    if not (index_dir / "chroma.sqlite3").is_file():
        return False
    collections = manifest.get("collections") or {}
    expected = {
        LEGACY_COLLECTION: (legacy_count, "legacy", True),
        SCHOOL_COLLECTION: (school_count, "school", bool(school_count)),
        EVIDENCE_COLLECTION: (evidence_count, "evidence", bool(evidence_count)),
    }
    for name, (count, layer, created) in expected.items():
        details = collections.get(name)
        if not isinstance(details, dict):
            return False
        if details.get("count") != count:
            return False
        if details.get("schema_version") != INDEX_SCHEMA_VERSION:
            return False
        if details.get("layer") != layer or details.get("created") is not created:
            return False

    client = None
    try:
        import chromadb  # noqa: PLC0415

        client = chromadb.PersistentClient(path=str(index_dir))
        actual_names = {
            entry if isinstance(entry, str) else getattr(entry, "name", None)
            for entry in client.list_collections()
        }
        actual_names.discard(None)
        for name, (count, layer, created) in expected.items():
            if created:
                if name not in actual_names:
                    return False
                collection = client.get_collection(name)
                if collection.count() != count:
                    return False
                metadata = collection.metadata or {}
                if metadata.get("kb_schema_version") != INDEX_SCHEMA_VERSION:
                    return False
                if metadata.get("layer") != layer:
                    return False
            elif name in actual_names:
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Existing index failed currentness verification: %s", exc)
        return False
    finally:
        if client is not None:
            try:
                close_chroma_client(client)
            except Exception:  # noqa: BLE001
                pass


def canonical_embedding_map(
    items: list[dict], embeddings: list[list[float]]
) -> dict[str, list[float]]:
    """Map canonical ids and paths to bundled vectors for v2 reuse."""
    result: dict[str, list[float]] = {}
    for item, embedding in zip(items, embeddings):
        card = item["card"]
        keys = (
            item.get("id"),
            item.get("file_path"),
            card.get("global_card_id"),
            card.get("card_id"),
        )
        for key in keys:
            if isinstance(key, str) and key:
                result.setdefault(key, embedding)
    return result


def embeddings_for_v2_records(
    records: list[dict], embedding_map: dict[str, list[float]]
) -> list[list[float]]:
    """Resolve every v2 record to its canonical card's bundled embedding."""
    resolved: list[list[float]] = []
    for record in records:
        metadata = record["metadata"]
        candidates = (
            metadata.get("canonical_id"),
            metadata.get("canonical_file"),
            metadata.get("file_path"),
        )
        embedding = next(
            (embedding_map[key] for key in candidates if key in embedding_map),
            None,
        )
        if embedding is None:
            raise ValueError(
                f"v2 record {record['id']!r} cannot resolve a canonical embedding "
                f"(canonical_id={metadata.get('canonical_id')!r})"
            )
        resolved.append(embedding)
    return resolved


def add_collection_records(
    client,
    *,
    name: str,
    records: list[dict],
    embeddings: list[list[float]],
    layer: str,
) -> None:
    """Create one Chroma collection and add records in bounded batches."""
    collection = client.create_collection(
        name=name,
        metadata={
            "hnsw:space": "cosine",
            "kb_schema_version": INDEX_SCHEMA_VERSION,
            "layer": layer,
        },
    )
    batch_size = 1000
    for offset in range(0, len(records), batch_size):
        batch = records[offset : offset + batch_size]
        collection.add(
            ids=[record["id"] for record in batch],
            embeddings=[
                (
                    vector
                    if isinstance(vector, list)
                    else vector.tolist()
                    if hasattr(vector, "tolist")
                    else list(vector)
                )
                for vector in embeddings[offset : offset + batch_size]
            ],
            documents=[record["document"] for record in batch],
            metadatas=[record["metadata"] for record in batch],
        )


def close_chroma_client(client) -> None:
    """Release SQLite handles before a cross-platform directory promotion."""
    system = getattr(client, "_system", None)
    stop = getattr(system, "stop", None)
    if callable(stop):
        stop()


def commit_staged_index(staging_dir: Path, index_dir: Path) -> None:
    """Atomically promote a complete staged index, restoring on failure."""
    staging_dir = Path(staging_dir)
    index_dir = Path(index_dir)
    if staging_dir.is_symlink() or (
        staging_dir.exists() and not staging_dir.is_dir()
    ):
        raise ValueError(f"staged index must be a real directory: {staging_dir}")
    if index_dir.is_symlink() or (index_dir.exists() and not index_dir.is_dir()):
        raise ValueError(f"live index must be a real directory: {index_dir}")
    backup_dir: Optional[Path] = None
    if index_dir.exists():
        backup_dir = index_dir.with_name(
            f".{index_dir.name}.backup-{uuid.uuid4().hex}"
        )
        index_dir.replace(backup_dir)
    try:
        staging_dir.replace(index_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not index_dir.exists():
            backup_dir.replace(index_dir)
        raise
    if backup_dir is not None:
        try:
            shutil.rmtree(backup_dir)
        except OSError as exc:
            log.warning("new index is active, but old backup cleanup failed: %s", exc)


def _recover_interrupted_index_unlocked(index_dir: Path) -> tuple[bool, bool]:
    """Restore one interrupted-promotion backup, failing closed on ambiguity.

    Returns ``(safe_to_continue, restored)``. A backup is restored only when
    the live index is absent and the backup contains a Chroma database. If a
    live index and a backup coexist, or multiple backups exist, choosing one
    automatically could discard the only good copy, so the build stops.
    """
    backup_pattern = f".{index_dir.name}.backup-*"
    backups = sorted(index_dir.parent.glob(backup_pattern))
    if not backups:
        return True, False
    if len(backups) != 1:
        log.error(
            "Found %d interrupted index backups; refusing automatic recovery: %s",
            len(backups),
            ", ".join(str(path) for path in backups),
        )
        return False, False

    backup = backups[0]
    if backup.name.startswith(REGEN_INDEX_BACKUP_PREFIX):
        log.error(
            "Regeneration-owned index backup requires its card journal; "
            "refusing index-only recovery: %s",
            backup,
        )
        return False, False
    if _path_present(index_dir):
        log.error(
            "Both live index and recovery backup exist; refusing to choose: %s, %s",
            index_dir,
            backup,
        )
        return False, False
    database = backup / "chroma.sqlite3"
    if (
        backup.is_symlink()
        or not backup.is_dir()
        or database.is_symlink()
        or not database.is_file()
    ):
        log.error("Recovery backup has no chroma.sqlite3; refusing restore: %s", backup)
        return False, False
    try:
        backup.replace(index_dir)
    except OSError as exc:
        log.error("Failed to restore interrupted index backup %s: %s", backup, exc)
        return False, False
    log.warning("Restored interrupted index backup: %s -> %s", backup, index_dir)
    return True, True


def recover_interrupted_index(index_dir: Path) -> tuple[bool, bool]:
    """Run legacy index recovery only while this KB's build lock is held."""
    try:
        with knowledge_base_build_lock(Path(index_dir).parent):
            return _recover_interrupted_index_unlocked(Path(index_dir))
    except BuildLockUnavailable as exc:
        log.error("Index recovery refused because another build is active: %s", exc)
        return False, False


def _unique_strings(values) -> list[str]:
    """Return non-empty strings once, preserving their first-seen order."""
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value in seen:
            continue
        unique.append(value)
        seen.add(value)
    return unique


def extract_card_sources(card: dict) -> list[str]:
    """Extract source collection names without requiring a newer card schema."""
    values = [card.get("project_origin")]
    for source_card in card.get("source_cards") or []:
        if isinstance(source_card, dict):
            values.append(source_card.get("project"))
    return _unique_strings(values)


def extract_card_schools(card: dict) -> list[str]:
    """Extract the primary and source-level school labels for diagnostics."""
    values = [card.get("school")]
    for source_card in card.get("source_cards") or []:
        if isinstance(source_card, dict):
            values.append(source_card.get("source_school"))
    return _unique_strings(values)


def build_card_metadata(item: dict) -> dict:
    """Build scalar Chroma metadata for old and new knowledge cards.

    Lists are JSON-encoded strings because the supported dependency range
    starts at Chroma 0.5, whose metadata values are scalar. Retrieval scope
    continues to use the established scalar ``school`` field; the richer
    fields are additive provenance for diagnostics and a future evidence
    index.
    """
    card = item["card"]
    school = card.get("school") or ""
    sources = extract_card_sources(card)
    source_cards = [
        source_card
        for source_card in (card.get("source_cards") or [])
        if isinstance(source_card, dict)
    ]
    source_schools = extract_card_schools(card)
    metadata = {
        # Original fields: do not rename or change their semantics.
        "type": item["type"],
        "card_id": item["id"],
        "term": (
            card.get("canonical_term")
            or card.get("title")
            or item["id"]
        ),
        "school": school,
        "file_path": item["file_path"],
        # Additive v2 provenance. Arrays remain scalar JSON for Chroma 0.5+.
        "schema_version": RECORD_SCHEMA_VERSION,
        "layer": "canonical_concept" if item["type"] == "concept" else "case",
        "canonical_id": (
            card.get("global_card_id")
            or card.get("card_id")
            or item["id"]
        ),
        "primary_school": school,
        "source_collection_count": len(sources),
        "source_card_count": len(source_cards),
        "source_names": json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
        "source_schools": json.dumps(
            source_schools, ensure_ascii=False, separators=(",", ":")
        ),
    }

    if len(sources) == 1:
        metadata["source"] = sources[0]

    for field in (
        "project_origin",
        "asset",
        "timeframe",
        "review_status",
        "extraction_confidence",
    ):
        value = card.get(field)
        if isinstance(value, (str, int, float, bool)) and value != "":
            metadata[field] = value
    return metadata


def build_concept_text(card: dict) -> str:
    """Compose a concept card into embedding-friendly plain text."""
    parts: list[str] = []
    term = card.get("canonical_term") or card.get("term") or ""
    if term:
        parts.append(f"Term: {term}")
    aliases = card.get("aliases") or []
    if aliases:
        parts.append(f"Aliases: {', '.join(aliases)}")
    school = card.get("school") or ""
    if school:
        parts.append(f"School: {school}")
    definition = card.get("definition") or ""
    if definition:
        parts.append(f"Definition: {definition}")
    rules = card.get("identification_rules") or []
    if rules:
        parts.append("Identification rules:\n" + "\n".join(f"- {r}" for r in rules))
    impl = card.get("trading_implication") or ""
    if impl:
        parts.append(f"Trading implication: {impl}")
    mistakes = card.get("common_mistakes") or []
    if mistakes:
        parts.append("Common mistakes:\n" + "\n".join(f"- {m}" for m in mistakes))
    related = card.get("related_concepts") or []
    if related:
        rel_strs = [
            r.get("term") if isinstance(r, dict) else r
            for r in related
            if r
        ]
        if rel_strs:
            parts.append(f"Related concepts: {', '.join(filter(None, rel_strs))}")
    return "\n\n".join(parts)


def build_case_text(card: dict) -> str:
    """Compose a case card into embedding-friendly plain text."""
    parts: list[str] = []
    title = card.get("title") or ""
    if title:
        parts.append(f"Title: {title}")
    school = card.get("school") or ""
    if school:
        parts.append(f"School: {school}")
    asset = card.get("asset")
    tf = card.get("timeframe")
    if asset or tf:
        parts.append(f"Market: {asset or '?'} @ {tf or '?'}")
    ctx = card.get("market_context") or ""
    if ctx:
        parts.append(f"Market context: {ctx}")
    obs = card.get("key_observation") or ""
    if obs:
        parts.append(f"Key observation: {obs}")
    steps = card.get("analysis_steps") or []
    if steps:
        parts.append("Analysis steps:\n" + "\n".join(f"- {s}" for s in steps))
    lessons = card.get("lessons") or ""
    if lessons:
        parts.append(f"Lessons: {lessons}")
    related = card.get("related_concepts") or card.get("illustrates_concepts") or []
    if related:
        rel_strs = [
            r.get("term") if isinstance(r, dict) else r
            for r in related
            if r
        ]
        if rel_strs:
            parts.append(f"Related concepts: {', '.join(filter(None, rel_strs))}")
    return "\n\n".join(parts)


def collect_cards(kb_dir: Path, limit: Optional[int] = None) -> list[dict]:
    """Return all concept+case cards, failing closed on an invalid source.

    Silently omitting a malformed or empty card would let a later staged build
    publish an internally consistent but incomplete index.  Every discovered
    JSON card is therefore part of the production input contract.
    """
    items: list[dict] = []

    for card_type, directory, build_text in (
        ("concept", "concepts", build_concept_text),
        ("case", "cases", build_case_text),
    ):
        cards_dir = kb_dir / directory
        if not cards_dir.is_dir():
            continue
        for f in sorted(cards_dir.glob("*.json")):
            relative_path = f"{directory}/{f.name}"
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise KnowledgeCardLoadError(
                    f"invalid {card_type} card {relative_path}: {exc}"
                ) from exc
            if not isinstance(d, dict):
                raise KnowledgeCardLoadError(
                    f"invalid {card_type} card {relative_path}: "
                    "top-level JSON value must be an object"
                )
            try:
                text = build_text(d)
            except Exception as exc:  # noqa: BLE001 - add source path at boundary
                raise KnowledgeCardLoadError(
                    f"invalid {card_type} card {relative_path}: "
                    f"cannot build retrieval text: {exc}"
                ) from exc
            if not text:
                raise KnowledgeCardLoadError(
                    f"invalid {card_type} card {relative_path}: "
                    "card produces empty retrieval text"
                )
            items.append({
                "id": f.stem,
                "type": card_type,
                "file_path": relative_path,
                "card": d,
                "text": text,
            })

    if limit:
        items = items[:limit]
    return items


def write_card_json(file_path: Path, card_data: dict) -> None:
    """Atomic write a card JSON back to disk (UTF-8, indent=2, preserves CJK)."""
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(card_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(file_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """Durably flush a directory on POSIX; Windows keeps rename atomicity."""
    if os.name == "nt":
        # Python cannot portably open directory handles for FlushFileBuffers.
        # Files are still fsynced and same-volume replacements stay atomic,
        # but sudden-power-loss durability is necessarily best-effort there.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    """Flush one regular file before its containing directory is renamed."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"cannot flush non-regular file: {path}")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_tree(path: Path) -> None:
    """Flush a staged tree bottom-up before it becomes recovery evidence."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"cannot flush non-directory tree: {path}")
    directories = [path]
    for candidate in path.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"staged tree contains a symbolic link: {candidate}")
        if candidate.is_dir():
            directories.append(candidate)
        elif candidate.is_file():
            _fsync_file(candidate)
        else:
            raise ValueError(f"staged tree contains a non-regular file: {candidate}")
    for directory in sorted(
        directories, key=lambda value: len(value.parts), reverse=True
    ):
        _fsync_directory(directory)


def _atomic_write_json_durable(path: Path, value: dict) -> None:
    """Atomically replace JSON and flush it before a live-data rename."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _safe_regenerated_card_path(raw_path: str) -> Path:
    relative = Path(str(raw_path or ""))
    if (
        len(relative.parts) != 2
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[0] not in {"concepts", "cases"}
        or relative.suffix != ".json"
    ):
        raise ValueError(f"unsafe regenerated card path: {relative}")
    return relative


def _regular_file_sha256(path: Path, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    return _sha256_file(path)


def _index_identity(path: Path) -> dict:
    """Hash every durable index file except this transaction's marker."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"index must be a real directory: {path}")
    database = path / "chroma.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise ValueError(f"index has no regular chroma.sqlite3: {path}")
    digest = hashlib.sha256()
    file_count = 0
    for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        if candidate.is_symlink():
            raise ValueError(f"index contains a symbolic link: {candidate}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError(f"index contains a non-regular file: {candidate}")
        relative = candidate.relative_to(path).as_posix()
        if relative == REGEN_INDEX_MARKER_FILE:
            continue
        encoded_path = relative.encode("utf-8")
        file_hash = _sha256_file(candidate).encode("ascii")
        digest.update(len(encoded_path).to_bytes(8, byteorder="big"))
        digest.update(encoded_path)
        digest.update(file_hash)
        file_count += 1
    return {
        "kind": "index-tree-v1",
        "sha256": digest.hexdigest(),
        "file_count": file_count,
    }


def _valid_index_identity(value) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == "index-tree-v1"
        and isinstance(value.get("sha256"), str)
        and len(value["sha256"]) == 64
        and all(character in "0123456789abcdef" for character in value["sha256"])
        and isinstance(value.get("file_count"), int)
        and value["file_count"] >= 1
    )


def _index_matches_identity(path: Path, expected: dict) -> bool:
    try:
        return _index_identity(path) == expected
    except (OSError, ValueError):
        return False


def _write_regeneration_index_marker(
    staging_index_dir: Path,
    transaction_id: str,
    new_index_identity: dict,
) -> None:
    _atomic_write_json_durable(
        Path(staging_index_dir) / REGEN_INDEX_MARKER_FILE,
        {
            "schema_version": REGEN_JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "index_identity": new_index_identity,
        },
    )


def _read_regeneration_index_marker(
    index_dir: Path,
    transaction_id: str,
    expected_identity: dict,
) -> Optional[dict]:
    marker = Path(index_dir) / REGEN_INDEX_MARKER_FILE
    if not (marker.exists() or marker.is_symlink()):
        return None
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"invalid regeneration index marker: {marker}")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid regeneration index marker: {marker}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != REGEN_JOURNAL_VERSION
        or value.get("transaction_id") != transaction_id
        or value.get("index_identity") != expected_identity
    ):
        raise ValueError(f"regeneration index marker does not match journal: {marker}")
    return value


def _write_regeneration_journal(
    transaction: _RegenerationTransaction,
    phase: str,
) -> None:
    if phase not in REGEN_PHASES:
        raise ValueError(f"unknown regeneration transaction phase: {phase}")
    updated = dict(transaction.journal)
    updated["phase"] = phase
    _atomic_write_json_durable(transaction.root / REGEN_JOURNAL_FILE, updated)
    transaction.journal = updated


def _prepare_regeneration_transaction(
    staging_index_dir: Path,
    index_dir: Path,
    *,
    staged_cards_dir: Path,
    kb_dir: Path,
    card_paths: list[str],
) -> _RegenerationTransaction:
    """Persist everything required to roll back before touching live data."""
    raw_index_dir = Path(index_dir)
    raw_staging_index_dir = Path(staging_index_dir)
    raw_staged_cards_dir = Path(staged_cards_dir)
    for raw_path, label in (
        (raw_index_dir, "live index"),
        (raw_staging_index_dir, "staged index"),
        (raw_staged_cards_dir, "staged cards"),
    ):
        if raw_path.is_symlink():
            raise ValueError(f"regeneration {label} cannot be a symbolic link: {raw_path}")
    kb_dir = Path(kb_dir).resolve()
    index_dir = raw_index_dir.resolve()
    staging_index_dir = raw_staging_index_dir.resolve()
    staged_cards_dir = raw_staged_cards_dir.resolve()
    transaction_root = kb_dir / REGEN_TRANSACTION_DIR
    if transaction_root.exists() or transaction_root.is_symlink():
        raise RuntimeError(
            f"an unfinished regeneration transaction already exists: {transaction_root}"
        )
    extra_card_backups = sorted(kb_dir.glob("._cards.backup-*"))
    if extra_card_backups:
        raise RuntimeError(
            "orphan regenerated-card backup(s) require manual review: "
            + ", ".join(str(path) for path in extra_card_backups)
        )
    if index_dir.parent.resolve() != kb_dir or index_dir.name != "_index":
        raise ValueError(f"regeneration index target must be <kb>/_index: {index_dir}")
    for staged, prefix, label in (
        (staged_cards_dir, "._cards.build-", "card staging directory"),
        (staging_index_dir, "._index.build-", "index staging directory"),
    ):
        if (
            staged.is_symlink()
            or not staged.is_dir()
            or staged.parent.resolve() != kb_dir
            or not staged.name.startswith(prefix)
        ):
            raise ValueError(f"unsafe {label}: {staged}")
    for marker in (
        index_dir / REGEN_INDEX_MARKER_FILE,
        staging_index_dir / REGEN_INDEX_MARKER_FILE,
    ):
        if _path_present(marker):
            raise ValueError(f"unexpected pre-existing regeneration marker: {marker}")

    # A committed journal is meaningful after sudden power loss only if every
    # staged byte and directory entry it identifies reached durable storage.
    _fsync_tree(staged_cards_dir)
    _fsync_tree(staging_index_dir)

    transaction_id = uuid.uuid4().hex
    card_entries: list[dict] = []
    seen: set[Path] = set()
    for raw_path in card_paths:
        relative = _safe_regenerated_card_path(raw_path)
        if relative in seen:
            raise ValueError(f"duplicate regenerated card path: {relative}")
        seen.add(relative)
        target = kb_dir / relative
        staged = staged_cards_dir / relative
        try:
            target.parent.resolve().relative_to(kb_dir)
            staged.parent.resolve().relative_to(staged_cards_dir.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"regenerated card path escapes its managed root: {relative}"
            ) from exc
        card_entries.append(
            {
                "path": relative.as_posix(),
                "old_sha256": _regular_file_sha256(
                    target, label="live regenerated card"
                ),
                "new_sha256": _regular_file_sha256(
                    staged, label="staged regenerated card"
                ),
            }
        )
    if not card_entries:
        raise ValueError("regeneration transaction has no cards")
    _validate_card_tree(
        staged_cards_dir,
        {_safe_regenerated_card_path(entry["path"]) for entry in card_entries},
        label="staged regenerated cards",
        required=True,
    )

    had_live_index = index_dir.exists() or index_dir.is_symlink()
    old_index_identity = _index_identity(index_dir) if had_live_index else None
    new_index_identity = _index_identity(staging_index_dir)
    index_backup_dir = kb_dir / f"{REGEN_INDEX_BACKUP_PREFIX}{transaction_id}"
    if index_backup_dir.exists() or index_backup_dir.is_symlink():
        raise RuntimeError(f"regeneration index backup already exists: {index_backup_dir}")
    extra_index_backups = sorted(kb_dir.glob(f".{index_dir.name}.backup-*"))
    if extra_index_backups:
        raise RuntimeError(
            "orphan regeneration index backup(s) require manual review: "
            + ", ".join(str(path) for path in extra_index_backups)
        )

    journal = {
        "schema_version": REGEN_JOURNAL_VERSION,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "kb_dir": str(kb_dir),
        "index_dir": index_dir.name,
        "staged_cards_dir": staged_cards_dir.name,
        "staging_index_dir": staging_index_dir.name,
        "index_backup_dir": index_backup_dir.name,
        "had_live_index": had_live_index,
        "old_index_identity": old_index_identity,
        "new_index_identity": new_index_identity,
        "cards": card_entries,
    }
    preparing_root = kb_dir / f"{REGEN_PREPARE_PREFIX}{transaction_id}"
    if _path_present(preparing_root):
        raise RuntimeError(
            f"regeneration transaction staging already exists: {preparing_root}"
        )
    created_preparing_root = False
    published = False
    marker_path = staging_index_dir / REGEN_INDEX_MARKER_FILE
    try:
        preparing_root.mkdir()
        created_preparing_root = True
        transaction = _RegenerationTransaction(
            root=preparing_root,
            kb_dir=kb_dir,
            index_dir=index_dir,
            staged_cards_dir=staged_cards_dir,
            staging_index_dir=staging_index_dir,
            card_backup_dir=preparing_root / "cards",
            index_backup_dir=index_backup_dir,
            journal=journal,
        )
        _write_regeneration_journal(transaction, "prepared")
        _write_regeneration_index_marker(
            staging_index_dir, transaction_id, new_index_identity
        )
        # Publish the complete durable journal with one directory rename. A
        # sudden stop before this point can leave only ignored build staging;
        # it can never expose an active root without its recovery journal.
        preparing_root.replace(transaction_root)
        published = True
        transaction.root = transaction_root
        transaction.card_backup_dir = transaction_root / "cards"
        _fsync_directory(kb_dir)
        return transaction
    except BaseException:
        # A rename wrapper or asynchronous exception can report failure after
        # the directory move actually completed. Trust the durable namespace,
        # not the in-memory flag, or we could strip the only marker needed to
        # recover the now-published journal.
        published = _path_present(transaction_root)
        if created_preparing_root and not published:
            shutil.rmtree(preparing_root, ignore_errors=True)
        if not published and (marker_path.exists() or marker_path.is_symlink()):
            marker_path.unlink()
        raise


def _load_regeneration_transaction(
    kb_dir: Path,
    *,
    transaction_root: Optional[Path] = None,
) -> _RegenerationTransaction:
    kb_dir = Path(kb_dir).resolve()
    root = (
        Path(transaction_root)
        if transaction_root is not None
        else kb_dir / REGEN_TRANSACTION_DIR
    )
    if root.parent.resolve() != kb_dir:
        raise ValueError(f"regeneration transaction root escapes knowledge base: {root}")
    journal_path = root / REGEN_JOURNAL_FILE
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"regeneration transaction root is invalid: {root}")
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError(f"regeneration transaction journal is missing: {journal_path}")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid regeneration journal {journal_path}: {exc}") from exc
    if not isinstance(journal, dict):
        raise ValueError(f"regeneration journal root must be an object: {journal_path}")
    transaction_id = journal.get("transaction_id")
    if (
        journal.get("schema_version") != REGEN_JOURNAL_VERSION
        or not isinstance(transaction_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
        or journal.get("phase") not in REGEN_PHASES
        or journal.get("kb_dir") != str(kb_dir)
        or journal.get("index_dir") != "_index"
        or type(journal.get("had_live_index")) is not bool
        or not _valid_index_identity(journal.get("new_index_identity"))
    ):
        raise ValueError(f"regeneration journal header is invalid: {journal_path}")
    if journal["had_live_index"]:
        if not _valid_index_identity(journal.get("old_index_identity")):
            raise ValueError(f"regeneration journal old-index identity is invalid")
    elif journal.get("old_index_identity") is not None:
        raise ValueError(f"regeneration journal unexpectedly identifies an old index")

    staged_cards_name = journal.get("staged_cards_dir")
    staging_index_name = journal.get("staging_index_dir")
    expected_backup_name = f"{REGEN_INDEX_BACKUP_PREFIX}{transaction_id}"
    if (
        not isinstance(staged_cards_name, str)
        or not staged_cards_name.startswith("._cards.build-")
        or Path(staged_cards_name).name != staged_cards_name
        or not isinstance(staging_index_name, str)
        or not staging_index_name.startswith("._index.build-")
        or Path(staging_index_name).name != staging_index_name
        or journal.get("index_backup_dir") != expected_backup_name
    ):
        raise ValueError(f"regeneration journal artifact paths are invalid")

    cards = journal.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ValueError("regeneration journal has no card entries")
    seen: set[Path] = set()
    for entry in cards:
        if not isinstance(entry, dict):
            raise ValueError("regeneration journal card entry is not an object")
        relative = _safe_regenerated_card_path(entry.get("path"))
        if relative in seen:
            raise ValueError(f"duplicate regeneration journal card: {relative}")
        seen.add(relative)
        for field in ("old_sha256", "new_sha256"):
            value = entry.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(
                    f"regeneration journal has invalid {field}: {relative}"
                )
        try:
            (kb_dir / relative).parent.resolve().relative_to(kb_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"regeneration journal card escapes knowledge base: {relative}"
            ) from exc

    return _RegenerationTransaction(
        root=root,
        kb_dir=kb_dir,
        index_dir=kb_dir / "_index",
        staged_cards_dir=kb_dir / staged_cards_name,
        staging_index_dir=kb_dir / staging_index_name,
        card_backup_dir=root / "cards",
        index_backup_dir=kb_dir / expected_backup_name,
        journal=journal,
    )


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _optional_regular_file_sha256(path: Path, *, label: str) -> Optional[str]:
    if not _path_present(path):
        return None
    return _regular_file_sha256(path, label=label)


def _card_relatives(transaction: _RegenerationTransaction) -> list[Path]:
    return [
        _safe_regenerated_card_path(entry["path"])
        for entry in transaction.journal["cards"]
    ]


def _validate_card_tree(
    root: Path,
    allowed_files: set[Path],
    *,
    label: str,
    required: bool = False,
) -> None:
    """Reject links and unjournaled files in a transaction-owned card tree."""
    if not _path_present(root):
        if required:
            raise ValueError(f"{label} is missing: {root}")
        return
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{label} must be a real directory: {root}")

    allowed_dirs: set[Path] = {Path(".")}
    for relative in allowed_files:
        for depth in range(1, len(relative.parts)):
            allowed_dirs.add(Path(*relative.parts[:depth]))

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_relative = current_path.relative_to(root)
        for name in directory_names:
            candidate = current_path / name
            relative = current_relative / name
            if candidate.is_symlink() or relative not in allowed_dirs:
                raise ValueError(f"unexpected directory in {label}: {candidate}")
        for name in file_names:
            candidate = current_path / name
            relative = current_relative / name
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or relative not in allowed_files
            ):
                raise ValueError(f"unexpected file in {label}: {candidate}")


def _validate_transaction_root_layout(
    transaction: _RegenerationTransaction,
) -> None:
    phase = transaction.journal["phase"]
    common = {REGEN_JOURNAL_FILE, "cards"}
    rolling = {
        "discarded-cards",
        "discarded-live-index",
        "discarded-staged-cards",
        "discarded-staging-index",
    }
    committed = {"obsolete-index", "obsolete-staged-cards"}
    unpublished = {"obsolete-staged-cards", "obsolete-staging-index"}
    allowed = common | (rolling if phase == "rolling_back" else set())
    allowed |= committed if phase == "committed" else set()
    allowed |= unpublished if phase == "unpublished_cleanup" else set()
    journal_tmp_prefix = f".{REGEN_JOURNAL_FILE}.tmp-"
    for candidate in transaction.root.iterdir():
        if candidate.name in allowed:
            continue
        if candidate.name.startswith(journal_tmp_prefix):
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(
                    f"invalid temporary regeneration journal: {candidate}"
                )
            continue
        raise ValueError(f"unexpected regeneration transaction artifact: {candidate}")


def _validate_index_backup_set(transaction: _RegenerationTransaction) -> None:
    backups = sorted(transaction.kb_dir.glob(f".{transaction.index_dir.name}.backup-*"))
    unexpected = [path for path in backups if path != transaction.index_backup_dir]
    if unexpected:
        raise ValueError(
            "unexpected index backup(s) during regeneration recovery: "
            + ", ".join(str(path) for path in unexpected)
        )


def _index_role(
    path: Path,
    transaction: _RegenerationTransaction,
    *,
    allow_markerless_new: bool = False,
) -> str:
    """Classify an index as this transaction's old/new tree or missing."""
    if not _path_present(path):
        return "missing"
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"index transaction artifact is unsafe: {path}")

    identity = _index_identity(path)
    marker_path = path / REGEN_INDEX_MARKER_FILE
    if _path_present(marker_path):
        _read_regeneration_index_marker(
            path,
            transaction.journal["transaction_id"],
            transaction.journal["new_index_identity"],
        )
        if identity != transaction.journal["new_index_identity"]:
            raise ValueError(f"marked regenerated index has changed: {path}")
        return "new"
    if allow_markerless_new and identity == transaction.journal["new_index_identity"]:
        return "new"
    old_identity = transaction.journal.get("old_index_identity")
    if old_identity is not None and identity == old_identity:
        return "old"
    raise ValueError(f"index does not match regeneration journal: {path}")


def _preflight_regeneration_rollback(
    transaction: _RegenerationTransaction,
) -> None:
    """Validate the complete rollback plan before the first live rename."""
    phase = transaction.journal["phase"]
    if phase not in {"prepared", "cards_promoted", "rolling_back"}:
        raise ValueError(f"cannot roll back regeneration phase: {phase}")
    _validate_transaction_root_layout(transaction)
    _validate_index_backup_set(transaction)

    relatives = _card_relatives(transaction)
    allowed = set(relatives)
    _validate_card_tree(
        transaction.staged_cards_dir,
        allowed,
        label="staged regenerated cards",
        required=phase != "rolling_back",
    )
    _validate_card_tree(
        transaction.card_backup_dir,
        allowed,
        label="regenerated card backups",
    )
    discarded_cards = transaction.root / "discarded-cards"
    discarded_staged_cards = transaction.root / "discarded-staged-cards"
    _validate_card_tree(
        discarded_cards,
        allowed,
        label="discarded regenerated cards",
    )
    _validate_card_tree(
        discarded_staged_cards,
        allowed,
        label="discarded staged cards",
    )
    if phase != "rolling_back" and (
        _path_present(discarded_cards)
        or _path_present(discarded_staged_cards)
    ):
        raise ValueError("rollback artifacts exist before rollback was authorized")
    if _path_present(transaction.staged_cards_dir) and _path_present(
        discarded_staged_cards
    ):
        raise ValueError("both live and discarded card staging directories exist")

    for entry, relative in zip(transaction.journal["cards"], relatives):
        target = transaction.kb_dir / relative
        staged = transaction.staged_cards_dir / relative
        backup = transaction.card_backup_dir / relative
        discarded = discarded_cards / relative
        discarded_staged = discarded_staged_cards / relative
        target_hash = _optional_regular_file_sha256(
            target, label="live regenerated card"
        )
        staged_hash = _optional_regular_file_sha256(
            staged, label="staged regenerated card"
        )
        backup_hash = _optional_regular_file_sha256(
            backup, label="regenerated card backup"
        )
        discarded_hash = _optional_regular_file_sha256(
            discarded, label="discarded regenerated card"
        )
        discarded_staged_hash = _optional_regular_file_sha256(
            discarded_staged, label="discarded staged regenerated card"
        )
        old_hash = entry["old_sha256"]
        new_hash = entry["new_sha256"]

        if backup_hash is not None:
            if backup_hash != old_hash:
                raise ValueError(f"regenerated card backup has changed: {relative}")
            if target_hash not in {None, new_hash}:
                raise ValueError(f"live regenerated card is ambiguous: {relative}")
        elif target_hash != old_hash:
            raise ValueError(f"original regenerated card cannot be recovered: {relative}")
        if staged_hash not in {None, new_hash}:
            raise ValueError(f"staged regenerated card has changed: {relative}")
        if discarded_hash not in {None, new_hash}:
            raise ValueError(f"discarded regenerated card has changed: {relative}")
        if discarded_staged_hash not in {None, new_hash}:
            raise ValueError(
                f"discarded staged regenerated card has changed: {relative}"
            )
        if backup_hash is not None and target_hash is not None and discarded_hash is not None:
            raise ValueError(
                f"live and discarded regenerated card both exist: {relative}"
            )

        if phase != "rolling_back":
            if discarded_hash is not None:
                raise ValueError(f"unexpected discarded regenerated card: {relative}")
            if backup_hash is None:
                if staged_hash != new_hash:
                    raise ValueError(
                        f"staged regenerated card disappeared before promotion: {relative}"
                    )
            elif target_hash is None:
                if staged_hash != new_hash:
                    raise ValueError(
                        f"new regenerated card has no recoverable copy: {relative}"
                    )
            elif staged_hash is not None:
                raise ValueError(
                    f"regenerated card exists in both staging and live: {relative}"
                )

    discarded_live_index = transaction.root / "discarded-live-index"
    discarded_staging_index = transaction.root / "discarded-staging-index"
    if phase != "rolling_back" and (
        _path_present(discarded_live_index)
        or _path_present(discarded_staging_index)
    ):
        raise ValueError("index rollback artifacts exist before rollback was authorized")
    if _path_present(transaction.staging_index_dir) and _path_present(
        discarded_staging_index
    ):
        raise ValueError("both live and discarded index staging directories exist")

    live_role = _index_role(transaction.index_dir, transaction)
    stage_role = _index_role(transaction.staging_index_dir, transaction)
    backup_role = _index_role(transaction.index_backup_dir, transaction)
    discarded_live_role = _index_role(discarded_live_index, transaction)
    discarded_stage_role = _index_role(discarded_staging_index, transaction)
    if backup_role not in {"missing", "old"}:
        raise ValueError("regeneration index backup is not the original index")
    if discarded_live_role not in {"missing", "new"}:
        raise ValueError("discarded live index is not the regenerated index")
    if discarded_stage_role not in {"missing", "new"}:
        raise ValueError("discarded staging index is not the regenerated index")
    if live_role == "new" and discarded_live_role == "new":
        raise ValueError("live and discarded regenerated indexes both exist")

    had_live_index = transaction.journal["had_live_index"]
    if had_live_index:
        if backup_role == "old":
            if live_role not in {"missing", "new"}:
                raise ValueError("live index is ambiguous while its backup exists")
        elif live_role != "old":
            raise ValueError("original live index cannot be recovered")
    else:
        if backup_role != "missing" or live_role not in {"missing", "new"}:
            raise ValueError("unexpected old index state for a new installation")

    if phase != "rolling_back":
        if discarded_live_role != "missing" or discarded_stage_role != "missing":
            raise ValueError("unexpected discarded index before rollback")
        if live_role == "new":
            if stage_role != "missing":
                raise ValueError("regenerated index exists in both live and staging")
        elif stage_role != "new":
            raise ValueError("staged regenerated index cannot be recovered")
    elif stage_role not in {"missing", "new"}:
        raise ValueError("staged index is ambiguous during rollback")


def _move_directory(source: Path, destination: Path, *, label: str) -> None:
    """Atomically move one exact managed directory, supporting restart."""
    source_present = _path_present(source)
    destination_present = _path_present(destination)
    if source_present and destination_present:
        raise ValueError(f"both source and destination exist for {label}")
    if destination_present:
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError(f"unsafe recovered {label}: {destination}")
        return
    if not source_present:
        return
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"unsafe {label}: {source}")
    source.replace(destination)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


def _verify_regeneration_rollback_complete(
    transaction: _RegenerationTransaction,
) -> None:
    for entry in transaction.journal["cards"]:
        relative = _safe_regenerated_card_path(entry["path"])
        if _regular_file_sha256(
            transaction.kb_dir / relative, label="restored regenerated card"
        ) != entry["old_sha256"]:
            raise RuntimeError(f"regenerated card rollback was incomplete: {relative}")
        if _path_present(transaction.card_backup_dir / relative):
            raise RuntimeError(f"regenerated card backup was not consumed: {relative}")
    if transaction.journal["had_live_index"]:
        if _index_role(transaction.index_dir, transaction) != "old":
            raise RuntimeError("original index rollback was incomplete")
    elif _path_present(transaction.index_dir):
        raise RuntimeError("new index remained live after rollback")
    if _path_present(transaction.index_backup_dir):
        raise RuntimeError("index backup was not consumed during rollback")


def _retire_regeneration_transaction(
    transaction: _RegenerationTransaction,
) -> None:
    """Atomically make recovery unnecessary, then best-effort delete trash."""
    transaction_id = transaction.journal["transaction_id"]
    retired = transaction.kb_dir / f"._cards.build-cleanup-{transaction_id}"
    if _path_present(retired):
        raise RuntimeError(f"regeneration cleanup destination already exists: {retired}")
    transaction.root.replace(retired)
    _fsync_directory(transaction.kb_dir)
    try:
        _remove_retired_regeneration_transaction(retired, transaction.kb_dir)
    except OSError as exc:
        log.warning("regeneration is consistent; deferred cleanup remains: %s", exc)


def _rollback_regeneration_transaction(
    transaction: _RegenerationTransaction,
) -> None:
    if transaction.journal["phase"] != "rolling_back":
        _preflight_regeneration_rollback(transaction)
        _write_regeneration_journal(transaction, "rolling_back")
    _preflight_regeneration_rollback(transaction)

    discarded_cards = transaction.root / "discarded-cards"
    for entry in transaction.journal["cards"]:
        relative = _safe_regenerated_card_path(entry["path"])
        target = transaction.kb_dir / relative
        backup = transaction.card_backup_dir / relative
        discarded = discarded_cards / relative
        if _path_present(backup):
            if _path_present(target):
                discarded.parent.mkdir(parents=True, exist_ok=True)
                _fsync_directory(transaction.root)
                _fsync_directory(discarded_cards)
                target.replace(discarded)
                _fsync_directory(target.parent)
                _fsync_directory(discarded.parent)
            backup.replace(target)
            _fsync_directory(backup.parent)
            _fsync_directory(target.parent)

    discarded_live_index = transaction.root / "discarded-live-index"
    if transaction.journal["had_live_index"]:
        if _path_present(transaction.index_backup_dir):
            if _path_present(transaction.index_dir):
                transaction.index_dir.replace(discarded_live_index)
                _fsync_directory(transaction.kb_dir)
                _fsync_directory(transaction.root)
            transaction.index_backup_dir.replace(transaction.index_dir)
            _fsync_directory(transaction.kb_dir)
    elif _path_present(transaction.index_dir):
        transaction.index_dir.replace(discarded_live_index)
        _fsync_directory(transaction.kb_dir)
        _fsync_directory(transaction.root)

    _verify_regeneration_rollback_complete(transaction)
    _move_directory(
        transaction.staged_cards_dir,
        transaction.root / "discarded-staged-cards",
        label="staged regenerated cards",
    )
    _move_directory(
        transaction.staging_index_dir,
        transaction.root / "discarded-staging-index",
        label="staged regenerated index",
    )
    _retire_regeneration_transaction(transaction)


def _preflight_committed_regeneration(
    transaction: _RegenerationTransaction,
) -> None:
    if transaction.journal["phase"] != "committed":
        raise ValueError("regeneration transaction is not committed")
    _validate_transaction_root_layout(transaction)
    _validate_index_backup_set(transaction)
    relatives = _card_relatives(transaction)
    allowed = set(relatives)
    _validate_card_tree(
        transaction.card_backup_dir,
        allowed,
        label="committed regenerated card backups",
    )
    for entry, relative in zip(transaction.journal["cards"], relatives):
        if _regular_file_sha256(
            transaction.kb_dir / relative, label="committed regenerated card"
        ) != entry["new_sha256"]:
            raise ValueError(f"committed regenerated card has changed: {relative}")
        backup = transaction.card_backup_dir / relative
        if _path_present(backup) and _regular_file_sha256(
            backup, label="committed regenerated card backup"
        ) != entry["old_sha256"]:
            raise ValueError(f"committed regenerated card backup has changed: {relative}")

    if _index_role(
        transaction.index_dir,
        transaction,
        allow_markerless_new=True,
    ) != "new":
        raise ValueError("committed regenerated index is not live")
    if _path_present(transaction.staging_index_dir):
        raise ValueError("committed regenerated index unexpectedly remains staged")

    obsolete_index = transaction.root / "obsolete-index"
    if _path_present(transaction.index_backup_dir) and _path_present(obsolete_index):
        raise ValueError("old index exists in two committed-cleanup locations")
    if _path_present(transaction.index_backup_dir):
        if not transaction.journal["had_live_index"]:
            raise ValueError("unexpected committed old-index backup")
        if _index_role(transaction.index_backup_dir, transaction) != "old":
            raise ValueError("committed old-index backup has changed")
    if _path_present(obsolete_index):
        if obsolete_index.is_symlink() or not obsolete_index.is_dir():
            raise ValueError(f"unsafe obsolete index: {obsolete_index}")
        if _index_role(obsolete_index, transaction) != "old":
            raise ValueError("obsolete committed index has changed")

    obsolete_staged_cards = transaction.root / "obsolete-staged-cards"
    if _path_present(transaction.staged_cards_dir) and _path_present(
        obsolete_staged_cards
    ):
        raise ValueError("staged-card cleanup exists in two locations")
    for staged_root, label in (
        (transaction.staged_cards_dir, "committed staged cards"),
        (obsolete_staged_cards, "obsolete staged cards"),
    ):
        _validate_card_tree(staged_root, allowed, label=label)
        for relative in relatives:
            if _path_present(staged_root / relative):
                raise ValueError(f"committed card unexpectedly remains staged: {relative}")


def _finalize_committed_regeneration(
    transaction: _RegenerationTransaction,
) -> None:
    _preflight_committed_regeneration(transaction)
    _move_directory(
        transaction.index_backup_dir,
        transaction.root / "obsolete-index",
        label="obsolete index backup",
    )
    _move_directory(
        transaction.staged_cards_dir,
        transaction.root / "obsolete-staged-cards",
        label="obsolete staged cards",
    )
    marker = transaction.index_dir / REGEN_INDEX_MARKER_FILE
    if _path_present(marker):
        if marker.is_symlink() or not marker.is_file():
            raise ValueError(f"unsafe live regeneration marker: {marker}")
        marker.unlink()
        _fsync_directory(transaction.index_dir)
    _retire_regeneration_transaction(transaction)


def _preflight_unpublished_regeneration(
    transaction: _RegenerationTransaction,
) -> None:
    """Prove a pre-publication journal never changed any live generation."""
    phase = transaction.journal["phase"]
    if phase not in {"prepared", "unpublished_cleanup"}:
        raise ValueError(
            f"unpublished regeneration has invalid phase: {phase}"
        )
    _validate_transaction_root_layout(transaction)
    _validate_index_backup_set(transaction)
    if _path_present(transaction.card_backup_dir):
        raise ValueError("unpublished regeneration unexpectedly has card backups")
    if _path_present(transaction.index_backup_dir):
        raise ValueError("unpublished regeneration unexpectedly has an index backup")

    relatives = _card_relatives(transaction)
    allowed = set(relatives)
    moved_cards = transaction.root / "obsolete-staged-cards"
    for root, label in (
        (transaction.staged_cards_dir, "unpublished staged cards"),
        (moved_cards, "retired unpublished staged cards"),
    ):
        _validate_card_tree(root, allowed, label=label)
    staged_cards_present = _path_present(transaction.staged_cards_dir)
    moved_cards_present = _path_present(moved_cards)
    if staged_cards_present == moved_cards_present:
        raise ValueError(
            "unpublished staged cards must exist in exactly one managed location"
        )
    if phase == "prepared" and moved_cards_present:
        raise ValueError("prepared regeneration already contains cleanup artifacts")

    staged_card_root = moved_cards if moved_cards_present else transaction.staged_cards_dir
    for entry, relative in zip(transaction.journal["cards"], relatives):
        live_hash = _regular_file_sha256(
            transaction.kb_dir / relative,
            label="live card before unpublished cleanup",
        )
        if live_hash != entry["old_sha256"]:
            raise ValueError(
                f"unpublished regeneration cannot prove old live card: {relative}"
            )
        staged_hash = _regular_file_sha256(
            staged_card_root / relative,
            label="unpublished staged regenerated card",
        )
        if staged_hash != entry["new_sha256"]:
            raise ValueError(
                f"unpublished staged regenerated card has changed: {relative}"
            )

    live_role = _index_role(transaction.index_dir, transaction)
    expected_live_role = "old" if transaction.journal["had_live_index"] else "missing"
    if live_role != expected_live_role:
        raise ValueError(
            "unpublished regeneration cannot prove the original live index"
        )
    moved_index = transaction.root / "obsolete-staging-index"
    external_index_present = _path_present(transaction.staging_index_dir)
    moved_index_present = _path_present(moved_index)
    if external_index_present == moved_index_present:
        raise ValueError(
            "unpublished staged index must exist in exactly one managed location"
        )
    if phase == "prepared" and moved_index_present:
        raise ValueError("prepared regeneration already contains index cleanup artifacts")
    staged_index = moved_index if moved_index_present else transaction.staging_index_dir
    if _index_role(staged_index, transaction) != "new":
        raise ValueError("unpublished staged index has changed")


def _cleanup_unpublished_regeneration(
    transaction: _RegenerationTransaction,
) -> None:
    """Restartably retire staging owned by a complete unpublished journal."""
    _preflight_unpublished_regeneration(transaction)
    if transaction.journal["phase"] == "prepared":
        _write_regeneration_journal(transaction, "unpublished_cleanup")
    _preflight_unpublished_regeneration(transaction)
    _move_directory(
        transaction.staged_cards_dir,
        transaction.root / "obsolete-staged-cards",
        label="unpublished staged cards",
    )
    _move_directory(
        transaction.staging_index_dir,
        transaction.root / "obsolete-staging-index",
        label="unpublished staged index",
    )
    _preflight_unpublished_regeneration(transaction)
    _retire_regeneration_transaction(transaction)


def _validated_unpublished_regeneration_transactions(
    kb_dir: Path,
    index_dir: Path,
) -> list[_RegenerationTransaction]:
    """Find cleanup-safe pre-publication journals; report incomplete debris."""
    pattern = re.compile(
        rf"^{re.escape(REGEN_PREPARE_PREFIX)}([0-9a-f]{{32}})$"
    )
    transactions: list[_RegenerationTransaction] = []
    referenced_paths: set[Path] = set()
    for candidate in sorted(kb_dir.glob(f"{REGEN_PREPARE_PREFIX}*")):
        match = pattern.fullmatch(candidate.name)
        journal_path = candidate / REGEN_JOURNAL_FILE
        if (
            not match
            or candidate.is_symlink()
            or not candidate.is_dir()
            or journal_path.is_symlink()
            or not journal_path.is_file()
        ):
            # Before the journal's atomic directory publication, abrupt death
            # may leave an empty/partial build directory. It cannot have
            # touched live cards/index, but it also lacks sufficient ownership
            # evidence for automatic deletion.
            log.warning(
                "Ignoring unverifiable unpublished build staging; live "
                "generation is unaffected, manual cleanup may reclaim it: %s",
                candidate,
            )
            continue
        try:
            transaction = _load_regeneration_transaction(
                kb_dir, transaction_root=candidate
            )
            if transaction.journal["transaction_id"] != match.group(1):
                raise ValueError("unpublished transaction id does not match its path")
            if transaction.index_dir != index_dir:
                raise ValueError("unpublished transaction index target is invalid")
            _preflight_unpublished_regeneration(transaction)
            owned_paths = {
                transaction.staged_cards_dir,
                transaction.staging_index_dir,
                transaction.index_backup_dir,
            }
            if referenced_paths.intersection(owned_paths):
                raise ValueError("unpublished transactions share managed artifacts")
            referenced_paths.update(owned_paths)
        except (OSError, ValueError) as exc:
            log.warning(
                "Ignoring unverifiable unpublished build staging; live "
                "generation is unaffected, manual review required: %s: %s",
                candidate,
                exc,
            )
            continue
        transactions.append(transaction)
    return transactions


def _read_retired_regeneration_journal(candidate: Path, kb_dir: Path) -> dict:
    """Verify that a cleanup tombstone was created by this builder."""
    journal_path = candidate / REGEN_JOURNAL_FILE
    if journal_path.is_symlink() or not journal_path.is_file():
        # Cleanup deletes its ownership record last. An empty directory means
        # a prior attempt reached the final rmdir boundary and is safe to end.
        if not any(candidate.iterdir()):
            return {}
        raise ValueError(f"deferred cleanup has no ownership journal: {candidate}")
    transaction = _load_regeneration_transaction(
        kb_dir,
        transaction_root=candidate,
    )
    journal = transaction.journal
    transaction_id = journal["transaction_id"]
    if (
        journal["phase"] not in {
            "rolling_back",
            "committed",
            "unpublished_cleanup",
        }
        or candidate.name != f"._cards.build-cleanup-{transaction_id}"
    ):
        raise ValueError(f"deferred cleanup ownership is invalid: {candidate}")
    _validate_transaction_root_layout(transaction)
    allowed = set(_card_relatives(transaction))
    for root, label in (
        (transaction.card_backup_dir, "retired card backups"),
        (candidate / "discarded-cards", "retired discarded cards"),
        (candidate / "discarded-staged-cards", "retired staged cards"),
        (candidate / "obsolete-staged-cards", "retired obsolete staged cards"),
    ):
        _validate_card_tree(root, allowed, label=label)
    for index_tree, expected_phase in (
        (candidate / "obsolete-index", "committed"),
        (candidate / "obsolete-staging-index", "unpublished_cleanup"),
    ):
        if not _path_present(index_tree):
            continue
        if journal["phase"] != expected_phase:
            raise ValueError(f"unexpected retired index tree: {index_tree}")
        if index_tree.is_symlink() or not index_tree.is_dir():
            raise ValueError(f"unsafe retired index tree: {index_tree}")
        # Recursive deletion can be interrupted between arbitrary children.
        # Once the journaled root was atomically retired, a subset of regular
        # files/directories is valid cleanup progress; links remain forbidden.
        for descendant in index_tree.rglob("*"):
            if descendant.is_symlink() or not (
                descendant.is_file() or descendant.is_dir()
            ):
                raise ValueError(
                    f"unsafe retired index artifact: {descendant}"
                )
    return journal


def _remove_retired_regeneration_transaction(candidate: Path, kb_dir: Path) -> None:
    """Delete a verified tombstone while retaining its journal until last.

    Retirement happens only after the live old/new pair was fully verified;
    all remaining children are therefore transaction garbage. The journal and
    constrained layout prove ownership, while deleting it last keeps a partial
    recursive cleanup restartable.
    """
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"unsafe deferred regeneration cleanup: {candidate}")
    journal = _read_retired_regeneration_journal(candidate, kb_dir)
    for child in list(candidate.iterdir()):
        if child.name == REGEN_JOURNAL_FILE:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    _fsync_directory(candidate)
    journal_path = candidate / REGEN_JOURNAL_FILE
    if journal and _path_present(journal_path):
        journal_path.unlink()
        _fsync_directory(candidate)
    candidate.rmdir()
    _fsync_directory(kb_dir)


def _retired_regeneration_transactions(kb_dir: Path) -> Optional[list[Path]]:
    """Return only globally verified cleanup tombstones, or fail closed."""
    pattern = re.compile(r"^\._cards\.build-cleanup-[0-9a-f]{32}$")
    candidates = sorted(kb_dir.glob("._cards.build-cleanup-*"))
    for candidate in candidates:
        if not pattern.fullmatch(candidate.name) or candidate.is_symlink() or not candidate.is_dir():
            log.error("Unsafe deferred regeneration cleanup artifact: %s", candidate)
            return None
        try:
            _read_retired_regeneration_journal(candidate, kb_dir)
        except (OSError, ValueError) as exc:
            log.error("Could not verify deferred cleanup %s: %s", candidate, exc)
            return None
    return candidates


def _cleanup_retired_regeneration_transactions(
    kb_dir: Path,
    candidates: list[Path],
) -> None:
    for candidate in candidates:
        try:
            _remove_retired_regeneration_transaction(candidate, kb_dir)
        except OSError as exc:
            # The live knowledge/index state no longer depends on this retired
            # directory. Once ownership is proven, deletion failure is only a
            # warning and must not block a healthy live index/build.
            log.warning("Could not remove deferred cleanup %s: %s", candidate, exc)


def _recover_interrupted_regeneration_unlocked(
    kb_dir: Path,
    index_dir: Path,
) -> tuple[bool, bool]:
    """Recover one journaled --regenerate transaction before legacy recovery."""
    kb_dir = Path(kb_dir).resolve()
    index_dir = Path(index_dir).resolve()
    if index_dir.parent.resolve() != kb_dir or index_dir.name != "_index":
        log.error("Unsafe regeneration recovery index target: %s", index_dir)
        return False, False
    transaction_root = kb_dir / REGEN_TRANSACTION_DIR
    active = _path_present(transaction_root)
    card_backups = sorted(kb_dir.glob("._cards.backup-*"))
    unexpected_card_backups = [
        path for path in card_backups if path != transaction_root
    ]
    regeneration_index_backups = sorted(
        kb_dir.glob(f"{REGEN_INDEX_BACKUP_PREFIX}*")
    )
    retired = _retired_regeneration_transactions(kb_dir)
    if retired is None:
        return False, False
    unpublished = (
        _validated_unpublished_regeneration_transactions(kb_dir, index_dir)
        if not active
        else []
    )

    if not active:
        marker = index_dir / REGEN_INDEX_MARKER_FILE
        if unexpected_card_backups or regeneration_index_backups or _path_present(marker):
            log.error(
                "Found regeneration artifacts without their active journal; "
                "refusing automatic recovery: %s",
                ", ".join(
                    str(path)
                    for path in [
                        *unexpected_card_backups,
                        *regeneration_index_backups,
                        *([marker] if _path_present(marker) else []),
                    ]
                ),
            )
            return False, False
        # A generic legacy index backup must be recovered first. Leave any
        # independent unpublished staging for the next invocation so no
        # cleanup mutation precedes that recovery's ambiguity checks.
        generic_index_backups = sorted(kb_dir.glob("._index.backup-*"))
        if not generic_index_backups:
            try:
                for transaction in unpublished:
                    _cleanup_unpublished_regeneration(transaction)
            except Exception as exc:  # noqa: BLE001 - recovery boundary
                log.error(
                    "Unpublished regeneration cleanup failed closed: %s", exc
                )
                return False, False
            if unpublished:
                log.warning(
                    "Recovered %d complete unpublished regeneration staging "
                    "transaction(s)",
                    len(unpublished),
                )
        _cleanup_retired_regeneration_transactions(kb_dir, retired)
        return True, bool(unpublished and not generic_index_backups)
    if unexpected_card_backups:
        log.error(
            "Found extra card backups during regeneration recovery: %s",
            ", ".join(str(path) for path in unexpected_card_backups),
        )
        return False, False

    try:
        transaction = _load_regeneration_transaction(kb_dir)
        if transaction.index_dir != index_dir:
            raise ValueError("regeneration journal index target does not match request")
        if transaction.journal["phase"] == "committed":
            _finalize_committed_regeneration(transaction)
            action = "finalized committed"
        else:
            _rollback_regeneration_transaction(transaction)
            action = "rolled back uncommitted"
    except Exception as exc:  # noqa: BLE001 - fail closed at recovery boundary
        log.error("Regeneration transaction recovery failed closed: %s", exc)
        return False, False
    _cleanup_retired_regeneration_transactions(kb_dir, retired)
    log.warning("Recovered interrupted regeneration transaction (%s)", action)
    return True, True


def recover_interrupted_regeneration(
    kb_dir: Path,
    index_dir: Path,
) -> tuple[bool, bool]:
    """Recover regeneration only while no other process can mutate the KB."""
    try:
        with knowledge_base_build_lock(kb_dir):
            return _recover_interrupted_regeneration_unlocked(kb_dir, index_dir)
    except BuildLockUnavailable as exc:
        log.error(
            "Regeneration recovery refused because another build is active: %s",
            exc,
        )
        return False, False


def stage_regenerated_cards(kb_dir: Path, items: list[dict]) -> Path:
    """Serialize regenerated cards without changing any live source file."""
    kb_dir = Path(kb_dir)
    staging_dir = Path(
        tempfile.mkdtemp(prefix="._cards.build-", dir=str(kb_dir))
    )
    try:
        seen: set[Path] = set()
        for item in items:
            relative = Path(str(item.get("file_path") or ""))
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or relative in seen
            ):
                raise ValueError(
                    f"unsafe or duplicate regenerated card path: {relative}"
                )
            seen.add(relative)
            staged_path = staging_dir / relative
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            write_card_json(staged_path, item["card"])
        return staging_dir
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def rollback_card_promotion(promotion: _CardPromotion) -> None:
    """Restore every original card retained by a pending promotion."""
    failures: list[str] = []
    for target, backup in reversed(promotion.entries):
        try:
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    raise IsADirectoryError(target)
                target.unlink()
            backup.replace(target)
            _fsync_directory(backup.parent)
            _fsync_directory(target.parent)
        except OSError as exc:
            failures.append(f"{target}: {exc}")
    if failures:
        raise RuntimeError(
            "failed to roll back regenerated card(s): " + "; ".join(failures)
        )
    shutil.rmtree(promotion.backup_dir, ignore_errors=True)


def begin_card_promotion(
    staged_cards_dir: Path,
    kb_dir: Path,
    relative_paths: list[str],
    *,
    backup_dir: Optional[Path] = None,
    rollback_on_error: bool = True,
) -> _CardPromotion:
    """Replace all regenerated cards while retaining rollback copies."""
    staged_cards_dir = Path(staged_cards_dir)
    kb_dir = Path(kb_dir)
    kb_root = kb_dir.resolve()
    prepared: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for raw_relative in relative_paths:
        relative = Path(raw_relative)
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or relative in seen
        ):
            raise ValueError(f"unsafe or duplicate regenerated card path: {relative}")
        seen.add(relative)
        target = kb_dir / relative
        staged = staged_cards_dir / relative
        try:
            target.parent.resolve().relative_to(kb_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"regenerated card target escapes knowledge base: {relative}"
            ) from exc
        if target.is_symlink() or not target.is_file():
            raise ValueError(
                f"regenerated card target must be a regular file: {relative}"
            )
        if staged.is_symlink() or not staged.is_file():
            raise ValueError(
                f"staged regenerated card is missing or unsafe: {relative}"
            )
        prepared.append((target, staged))

    if backup_dir is None:
        backup_dir = Path(
            tempfile.mkdtemp(prefix="._cards.backup-", dir=str(kb_dir))
        )
    else:
        backup_dir = Path(backup_dir)
        if backup_dir.exists() or backup_dir.is_symlink():
            raise FileExistsError(
                f"regenerated card backup directory already exists: {backup_dir}"
            )
        if backup_dir.parent.resolve() != kb_root:
            # Transaction-owned backups live one level deeper than kb_dir.
            try:
                backup_dir.parent.resolve().relative_to(kb_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(
                    f"regenerated card backup escapes knowledge base: {backup_dir}"
                ) from exc
        backup_dir.mkdir(parents=True)
    _fsync_directory(backup_dir.parent)
    promotion = _CardPromotion(backup_dir=backup_dir, entries=[])
    try:
        for target, staged in prepared:
            relative = target.relative_to(kb_dir)
            backup = backup_dir / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            _fsync_directory(backup_dir)
            target.replace(backup)
            _fsync_directory(target.parent)
            _fsync_directory(backup.parent)
            promotion.entries.append((target, backup))
            staged.replace(target)
            _fsync_directory(staged.parent)
            _fsync_directory(target.parent)
        return promotion
    except Exception:
        if rollback_on_error:
            rollback_card_promotion(promotion)
        raise


def finish_card_promotion(promotion: _CardPromotion) -> None:
    """Discard rollback copies after the matching index is active."""
    try:
        shutil.rmtree(promotion.backup_dir)
    except OSError as exc:
        log.warning(
            "regenerated cards and index are active, but card backup cleanup "
            "failed: %s",
            exc,
        )


def _promote_regeneration_index(transaction: _RegenerationTransaction) -> None:
    """Promote the journal's exact staged index without deleting its old copy."""
    if transaction.journal["had_live_index"]:
        transaction.index_dir.replace(transaction.index_backup_dir)
        _fsync_directory(transaction.kb_dir)
    transaction.staging_index_dir.replace(transaction.index_dir)
    _fsync_directory(transaction.kb_dir)


def _commit_regenerated_build_unlocked(
    staging_index_dir: Path,
    index_dir: Path,
    *,
    staged_cards_dir: Optional[Path] = None,
    kb_dir: Optional[Path] = None,
    card_paths: Optional[list[str]] = None,
) -> None:
    """Commit a verified index and optional regenerated cards as one rollback unit."""
    if staged_cards_dir is None:
        commit_staged_index(staging_index_dir, index_dir)
        return
    if kb_dir is None or card_paths is None:
        raise ValueError("kb_dir and card_paths are required for regenerated cards")

    transaction = _prepare_regeneration_transaction(
        staging_index_dir,
        index_dir,
        staged_cards_dir=staged_cards_dir,
        kb_dir=kb_dir,
        card_paths=card_paths,
    )
    try:
        _preflight_regeneration_rollback(transaction)
        begin_card_promotion(
            staged_cards_dir,
            kb_dir,
            card_paths,
            backup_dir=transaction.card_backup_dir,
            rollback_on_error=False,
        )
        _write_regeneration_journal(transaction, "cards_promoted")
        _promote_regeneration_index(transaction)
        _write_regeneration_journal(transaction, "committed")
        _finalize_committed_regeneration(transaction)
    except Exception as original_error:
        # Reload the durable phase instead of trusting in-memory state. An
        # fsync error may be raised after the atomic journal replacement.
        try:
            if _path_present(transaction.root):
                durable = _load_regeneration_transaction(transaction.kb_dir)
                if durable.journal["phase"] == "committed":
                    _finalize_committed_regeneration(durable)
                else:
                    _rollback_regeneration_transaction(durable)
        except Exception as recovery_error:
            raise RuntimeError(
                "regeneration failed and automatic recovery could not prove a "
                f"safe state: {recovery_error}"
            ) from original_error
        raise


def commit_regenerated_build(
    staging_index_dir: Path,
    index_dir: Path,
    *,
    staged_cards_dir: Optional[Path] = None,
    kb_dir: Optional[Path] = None,
    card_paths: Optional[list[str]] = None,
) -> None:
    """Serialize any public staged commit with recovery and other builds."""
    lock_root = Path(kb_dir) if kb_dir is not None else Path(index_dir).parent
    with knowledge_base_build_lock(lock_root):
        _commit_regenerated_build_unlocked(
            staging_index_dir,
            index_dir,
            staged_cards_dir=staged_cards_dir,
            kb_dir=kb_dir,
            card_paths=card_paths,
        )


def load_embeddings_from_cards(items: list[dict]) -> tuple[list[list[float]], list[str]]:
    """Read pre-computed embeddings from each card's `_embedding` field.

    Returns (embeddings, missing_or_stale_ids).
    """
    embeddings: list[list[float]] = []
    bad: list[str] = []
    for it in items:
        emb = it["card"].get("_embedding")
        model = it["card"].get("_embedding_model")
        if (
            emb is None
            or model != EXPECTED_MODEL
            or not isinstance(emb, list)
            or len(emb) != EXPECTED_DIM
        ):
            bad.append(it["id"])
            embeddings.append([])  # placeholder, won't be used
            continue
        embeddings.append(emb)
    return embeddings, bad


def main(*, _lock_already_held: bool = False) -> int:
    p = argparse.ArgumentParser(
        description="Build vector index for the skill's knowledge_base/."
    )
    p.add_argument(
        "--kb", default=None,
        help=f"knowledge_base directory (default {SKILL_DIR / 'knowledge_base'})",
    )
    p.add_argument(
        "--embedder", default="local",
        choices=["local", "openai"],
        help="embedding provider for native v2 misses and --regenerate",
    )
    p.add_argument(
        "--v2-embedding-strategy",
        choices=["native", "inherit"],
        default="native",
        help="native embeds each v2 document independently (default); inherit "
             "reuses parent-card vectors for emergency/testing only",
    )
    p.add_argument(
        "--force", action="store_true",
        help="atomically rebuild ChromaDB even if it already exists",
    )
    p.add_argument(
        "--upgrade", action="store_true",
        help="upgrade an existing legacy index when deterministic v2 records "
             "are available; otherwise leave the working index untouched",
    )
    p.add_argument(
        "--regenerate", action="store_true",
        help="recompute embeddings and write them back into each JSON card "
             "(KB maintainer mode; needs the embedding model). "
             "Default mode reads canonical embeddings from JSON; native v2 "
             "cache misses may still load the selected model.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="only process first N cards (testing)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.upgrade and args.limit is not None:
        log.error("--upgrade cannot be combined with --limit; refusing to truncate a live index")
        return 2
    if args.upgrade and args.regenerate:
        log.error("--upgrade cannot be combined with --regenerate; use --force --regenerate")
        return 2
    if args.upgrade and args.force:
        log.error("--upgrade cannot be combined with --force; choose one index policy")
        return 2
    if args.limit is not None and args.limit <= 0:
        log.error("--limit must be a positive integer")
        return 2
    if args.force and args.limit is not None:
        log.error("--force cannot be combined with --limit; refusing to truncate a live index")
        return 2

    kb_dir = Path(args.kb) if args.kb else SKILL_DIR / "knowledge_base"
    if not kb_dir.is_dir():
        log.error("knowledge_base/ not found: %s", kb_dir)
        return 1
    if not _lock_already_held:
        try:
            with knowledge_base_build_lock(kb_dir):
                # Re-enter once so the existing early returns remain inside
                # one lock spanning recovery, source reads, staging and commit.
                return main(_lock_already_held=True)
        except BuildLockUnavailable as exc:
            log.error("Build refused because another process is active: %s", exc)
            return 1

    index_dir = kb_dir / "_index"
    regeneration_ok, regeneration_recovered = recover_interrupted_regeneration(
        kb_dir, index_dir
    )
    if not regeneration_ok:
        return 1
    recovery_ok, index_recovery_restored = recover_interrupted_index(index_dir)
    if not recovery_ok:
        return 1
    recovery_restored = regeneration_recovered or index_recovery_restored
    if (
        recovery_restored
        and not (index_dir / "chroma.sqlite3").is_symlink()
        and (index_dir / "chroma.sqlite3").is_file()
        and not args.force
        and not args.upgrade
        and not args.regenerate
        and args.limit is None
    ):
        log.info("Recovered index is active; no rebuild requested")
        return 0

    # Existence check for the ChromaDB output
    if index_dir.exists() and not args.force and not args.upgrade:
        log.error(
            "Index already exists: %s\nAdd --force to rebuild or --upgrade "
            "to add available v2 collections.", index_dir,
        )
        return 1

    # Collect cards
    log.info("Scanning knowledge base: %s", kb_dir)
    try:
        items = collect_cards(kb_dir, limit=args.limit)
    except KnowledgeCardLoadError as exc:
        log.error(
            "Knowledge card validation failed; existing index was not changed: %s",
            exc,
        )
        return 1
    n_concept = sum(1 for it in items if it["type"] == "concept")
    n_case = sum(1 for it in items if it["type"] == "case")
    log.info("Found %d concept + %d case = %d cards", n_concept, n_case, len(items))
    if args.limit:
        log.info("(--limit %d applied)", args.limit)
    if not items:
        log.error("No cards to index.")
        return 1

    # v2 records are an optional, deterministic projection of the canonical
    # cards. Their builder is deliberately decoupled from legacy indexing.
    try:
        raw_school_records, raw_evidence_records, v2_stats = (
            load_optional_v2_records(kb_dir)
        )
        school_records = normalize_v2_records(raw_school_records, layer="school")
        evidence_records = normalize_v2_records(raw_evidence_records, layer="evidence")
        # The builder records also carry rich payloads used by its artifact
        # CLI. Chroma needs only document+metadata, so release those payloads
        # before allocating collection batches.
        del raw_school_records, raw_evidence_records
    except Exception as exc:  # noqa: BLE001
        log.error("v2 record generation failed; existing index was not changed: %s", exc)
        return 1

    if args.limit:
        # Keep --limit useful for isolated integration tests. Production
        # builds never take this branch.
        included_ids = {
            key
            for item in items
            for key in (
                item.get("id"),
                item.get("file_path"),
                item["card"].get("global_card_id"),
                item["card"].get("card_id"),
            )
            if isinstance(key, str) and key
        }
        school_records = [
            record for record in school_records
            if record["metadata"].get("canonical_id") in included_ids
            or record["metadata"].get("canonical_file") in included_ids
            or record["metadata"].get("file_path") in included_ids
        ]
        evidence_records = [
            record for record in evidence_records
            if record["metadata"].get("canonical_id") in included_ids
            or record["metadata"].get("canonical_file") in included_ids
            or record["metadata"].get("file_path") in included_ids
        ]

    v2_available = bool(school_records or evidence_records)
    if args.v2_embedding_strategy == "native":
        v2_spec = v2_embedding_spec(args.embedder)
        v2_strategy = V2_NATIVE_STRATEGY
        v2_embedding_model = v2_spec["model"]
        v2_embedding_dim = v2_spec["dimension"]
        v2_embedding_input_profile = v2_spec["input_profile"]
    else:
        v2_spec = None
        v2_strategy = V2_INHERITED_STRATEGY
        # --upgrade cannot be combined with --regenerate, so this is the
        # exact expected identity when checking an existing inherited index.
        v2_embedding_model = EXPECTED_MODEL
        v2_embedding_dim = EXPECTED_DIM
        v2_embedding_input_profile = v2_input_profile(v2_strategy, "parent_card")
    # The data builder describes content; this index owns the actual vector
    # policy and must persist the effective choice on every Chroma record.
    apply_v2_embedding_strategy(
        school_records, v2_strategy, v2_embedding_input_profile
    )
    apply_v2_embedding_strategy(
        evidence_records, v2_strategy, v2_embedding_input_profile
    )

    v2_fingerprint = fingerprint_v2_records(school_records, evidence_records)
    canonical_fingerprint = fingerprint_canonical_items(items)
    log.info(
        "v2 projection: %d school + %d evidence records; strategy=%s%s",
        len(school_records),
        len(evidence_records),
        v2_strategy,
        "" if v2_available else " (not available; legacy-only is supported)",
    )

    if args.upgrade and index_dir.exists():
        if index_has_current_v2(
            index_dir,
            fingerprint=v2_fingerprint,
            canonical_fingerprint=canonical_fingerprint,
            legacy_count=len(items),
            school_count=len(school_records),
            evidence_count=len(evidence_records),
            v2_embedding_strategy=v2_strategy,
            v2_embedding_model=v2_embedding_model,
            v2_embedding_dimension=v2_embedding_dim,
            v2_embedding_input_profile=v2_embedding_input_profile,
        ):
            log.info("Index already contains the current v2 collections; no rebuild needed")
            return 0
        if not v2_available:
            log.warning(
                "No deterministic v2 records are available; rebuilding the "
                "legacy collection because the existing index did not match "
                "the current canonical cards"
            )
        log.info("Upgrading existing index in a recoverable staging directory")

    # ── Two paths to obtain embeddings ──────────────────────────────────────

    embedder = None
    if args.regenerate:
        # Compute fresh embeddings via the model, write back to JSON
        from _lib.embedder import get_embedder  # noqa: PLC0415

        log.info("[regenerate] Loading embedder (%s)...", args.embedder)
        embedder = get_embedder(args.embedder)
        log.info("[regenerate] embedding dim = %d", embedder.dim)

        texts = [it["text"] for it in items]
        log.info("[regenerate] Embedding %d cards (this can take 30 s – 10 min)...", len(texts))
        vecs = list(embedder.embed_documents(texts))
        if len(vecs) != len(items):
            log.error(
                "Embedder returned %d vectors for %d canonical cards",
                len(vecs),
                len(items),
            )
            return 1

        # Update the in-memory cards now. Their serialized files are staged only
        # after every embedding has resolved, and promoted together with the
        # verified Chroma index below.
        model_label = getattr(embedder, "model_name", "unknown")
        for it, vec in zip(items, vecs):
            it["card"]["_embedding"]       = vector_as_floats(vec)
            it["card"]["_embedding_model"] = model_label

        embeddings_for_chroma = [vector_as_floats(vector) for vector in vecs]
        embedding_dim = embedder.dim
        embedding_model = model_label
        canonical_fingerprint = fingerprint_canonical_items(items)
    else:
        # Load embeddings directly from each JSON card
        log.info("[load] Reading embeddings from JSON cards...")
        embeddings_for_chroma, bad = load_embeddings_from_cards(items)
        if bad:
            log.error("")
            log.error("%d / %d cards have missing / stale / wrong-dim embeddings:",
                      len(bad), len(items))
            for cid in bad[:5]:
                log.error("  - %s", cid)
            if len(bad) > 5:
                log.error("  ... and %d more", len(bad) - 5)
            log.error("")
            log.error("To regenerate embeddings (needs the nomic model, takes 30 s – 10 min):")
            log.error("  python scripts/build_index.py --regenerate --force")
            return 1
        log.info(
            "[load] All %d cards have valid embeddings (%s, %dd)",
            len(items), EXPECTED_MODEL, EXPECTED_DIM,
        )
        embedding_dim = EXPECTED_DIM
        embedding_model = EXPECTED_MODEL

    # ── Resolve independent v2 embeddings ───────────────────────────────────

    v2_cache_stats = {
        "record_count": len(school_records) + len(evidence_records),
        "strategy": v2_strategy,
    }
    try:
        if v2_strategy == V2_INHERITED_STRATEGY:
            embedding_map = canonical_embedding_map(items, embeddings_for_chroma)
            school_embeddings = embeddings_for_v2_records(
                school_records, embedding_map
            )
            evidence_embeddings = embeddings_for_v2_records(
                evidence_records, embedding_map
            )
            v2_embedding_model = embedding_model
            v2_embedding_dim = embedding_dim
        elif v2_available:
            assert v2_spec is not None

            def load_native_embedder():
                nonlocal embedder
                if embedder is None:
                    from _lib.embedder import get_embedder  # noqa: PLC0415

                    log.info(
                        "[v2] Loading embedder (%s) for cache misses...",
                        args.embedder,
                    )
                    embedder = get_embedder(args.embedder)
                return embedder

            combined_records = school_records + evidence_records
            combined_embeddings, v2_cache_stats = native_embeddings_for_records(
                combined_records,
                cache_path=kb_dir / V2_EMBEDDING_CACHE,
                model_key=v2_spec["cache_model_key"],
                expected_dimension=v2_embedding_dim,
                embedder_factory=load_native_embedder,
                max_seq_length=v2_embedding_input_profile.get("max_seq_length"),
                seed_dir=kb_dir / V2_EMBEDDING_SEED_DIR,
                seed_model=v2_embedding_model,
                seed_input_profile=v2_embedding_input_profile,
                v2_fingerprint=v2_fingerprint,
            )
            v2_cache_stats.update({
                "content_hash_algorithm": "sha256",
                "model_key": v2_spec["cache_model_key"],
                "batch_size": V2_EMBEDDING_BATCH_SIZE,
                "strategy": v2_strategy,
            })
            school_embeddings = combined_embeddings[: len(school_records)]
            evidence_embeddings = combined_embeddings[len(school_records) :]
            log.info(
                "[v2] Native embeddings ready: %d persistent-cache + %d seed "
                "record hits / %d; %d unique documents computed",
                v2_cache_stats["persistent_cache_hit_records"],
                v2_cache_stats["seed_hit_records"],
                v2_cache_stats["record_count"],
                v2_cache_stats["computed_unique_documents"],
            )
        else:
            school_embeddings = []
            evidence_embeddings = []
    except Exception as exc:  # noqa: BLE001
        log.error("v2 embedding resolution failed; existing index was not changed: %s", exc)
        return 1

    # ── Write to ChromaDB ───────────────────────────────────────────────────

    staging_dir: Optional[Path] = None
    staged_cards_dir: Optional[Path] = None
    try:
        if args.regenerate:
            staged_cards_dir = stage_regenerated_cards(kb_dir, items)
            log.info(
                "[regenerate] Staged %d updated JSON cards: %s",
                len(items),
                staged_cards_dir,
            )
        staging_dir = Path(
            tempfile.mkdtemp(prefix="._index.build-", dir=str(kb_dir))
        )
        log.info("Writing ChromaDB collections in staging: %s", staging_dir)
        import chromadb  # noqa: PLC0415

        client = chromadb.PersistentClient(path=str(staging_dir))
        legacy_records = [
            {
                "id": item["id"],
                "document": item["text"],
                "metadata": build_card_metadata(item),
            }
            for item in items
        ]
        add_collection_records(
            client,
            name=LEGACY_COLLECTION,
            records=legacy_records,
            embeddings=embeddings_for_chroma,
            layer="legacy",
        )
        if school_records:
            add_collection_records(
                client,
                name=SCHOOL_COLLECTION,
                records=school_records,
                embeddings=school_embeddings,
                layer="school",
            )
        if evidence_records:
            add_collection_records(
                client,
                name=EVIDENCE_COLLECTION,
                records=evidence_records,
                embeddings=evidence_embeddings,
                layer="evidence",
            )

        collections_manifest = {
            LEGACY_COLLECTION: {
                "count": len(legacy_records),
                "schema_version": INDEX_SCHEMA_VERSION,
                "layer": "legacy",
                "created": True,
                "metadata_value_counts": {
                    "school": metadata_value_counts(legacy_records, "school"),
                },
            },
            SCHOOL_COLLECTION: {
                "count": len(school_records),
                "schema_version": INDEX_SCHEMA_VERSION,
                "layer": "school",
                "created": bool(school_records),
                "metadata_value_counts": {
                    "school": metadata_value_counts(school_records, "school"),
                },
            },
            EVIDENCE_COLLECTION: {
                "count": len(evidence_records),
                "schema_version": INDEX_SCHEMA_VERSION,
                "layer": "evidence",
                "created": bool(evidence_records),
                "metadata_value_counts": {
                    "school": metadata_value_counts(evidence_records, "school"),
                },
            },
        }
        manifest = {
            "manifest_version": INDEX_MANIFEST_VERSION,
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "embedding_model": embedding_model,
            "embedding_model_revision": (
                EXPECTED_MODEL_REVISION
                if embedding_model == EXPECTED_MODEL
                else None
            ),
            "embedding_dimension": embedding_dim,
            "embedding_strategy": {
                LEGACY_COLLECTION: "bundled_card_embeddings",
                SCHOOL_COLLECTION: v2_strategy,
                EVIDENCE_COLLECTION: v2_strategy,
            },
            "embedding_models": {
                LEGACY_COLLECTION: embedding_model,
                SCHOOL_COLLECTION: v2_embedding_model,
                EVIDENCE_COLLECTION: v2_embedding_model,
            },
            "embedding_revisions": {
                LEGACY_COLLECTION: (
                    EXPECTED_MODEL_REVISION
                    if embedding_model == EXPECTED_MODEL
                    else None
                ),
                SCHOOL_COLLECTION: (
                    EXPECTED_MODEL_REVISION
                    if v2_embedding_model == EXPECTED_MODEL
                    else None
                ),
                EVIDENCE_COLLECTION: (
                    EXPECTED_MODEL_REVISION
                    if v2_embedding_model == EXPECTED_MODEL
                    else None
                ),
            },
            "embedding_dimensions": {
                LEGACY_COLLECTION: embedding_dim,
                SCHOOL_COLLECTION: v2_embedding_dim,
                EVIDENCE_COLLECTION: v2_embedding_dim,
            },
            "v2_embedding_cache": v2_cache_stats,
            "v2_embedding_input_profile": v2_embedding_input_profile,
            "canonical_embedding_input_profile": (
                CANONICAL_EMBEDDING_INPUT_PROFILE
                if embedding_model == EXPECTED_MODEL
                else None
            ),
            "v2_input_fingerprint": v2_fingerprint,
            "canonical_input_fingerprint": canonical_fingerprint,
            "v2_generation": v2_stats,
            "collections": collections_manifest,
        }
        write_card_json(staging_dir / INDEX_MANIFEST_FILE, manifest)

        # Verify the staged database before touching the live directory.
        for name, expected_count in (
            (LEGACY_COLLECTION, len(legacy_records)),
            (SCHOOL_COLLECTION, len(school_records)),
            (EVIDENCE_COLLECTION, len(evidence_records)),
        ):
            if not expected_count:
                continue
            actual_count = client.get_collection(name).count()
            if actual_count != expected_count:
                raise RuntimeError(
                    f"staged collection {name} has {actual_count} records; "
                    f"expected {expected_count}"
                )
        close_chroma_client(client)
        del client
        gc.collect()
        commit_regenerated_build(
            staging_dir,
            index_dir,
            staged_cards_dir=staged_cards_dir,
            kb_dir=kb_dir if staged_cards_dir is not None else None,
            card_paths=(
                [str(item["file_path"]) for item in items]
                if staged_cards_dir is not None
                else None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        if "client" in locals():
            try:
                close_chroma_client(client)
            except Exception:  # noqa: BLE001
                pass
        transaction_root = kb_dir / REGEN_TRANSACTION_DIR
        if _path_present(transaction_root):
            log.error(
                "Preserving staged regeneration evidence for the next "
                "fail-closed recovery: %s",
                transaction_root,
            )
        else:
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            if staged_cards_dir is not None and staged_cards_dir.exists():
                shutil.rmtree(staged_cards_dir, ignore_errors=True)
        log.error(
            "Index build did not complete cleanly; live state is unchanged "
            "or protected by durable transaction evidence: %s",
            exc,
        )
        return 1

    if staged_cards_dir is not None and staged_cards_dir.exists():
        shutil.rmtree(staged_cards_dir, ignore_errors=True)
    if args.regenerate:
        log.info("[regenerate] Updated %d JSON files", len(items))

    log.info(
        "✓ Index built: %d legacy + %d school + %d evidence vectors (%d-d) → %s",
        len(items), len(school_records), len(evidence_records), embedding_dim, index_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
