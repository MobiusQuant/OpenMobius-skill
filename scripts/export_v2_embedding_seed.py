#!/usr/bin/env python3
"""Export a verified release seed from the complete native v2 SQLite cache.

The command never loads an embedding model. Every current unique v2 document
must already exist under the current model/input-profile cache key or export
is refused without touching the prior seed directory.
"""

from __future__ import annotations

import argparse
from array import array
import json
import logging
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


THIS_DIR = Path(__file__).resolve().parent
SKILL_DIR = THIS_DIR.parent
sys.path.insert(0, str(THIS_DIR))

import build_index  # noqa: E402
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
    load_embedding_seed,
    write_embedding_seed_shard,
)


log = logging.getLogger("export_v2_embedding_seed")


def is_seed_directory(path: Path) -> bool:
    """Return whether an existing directory carries our seed marker."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        manifest = json.loads(
            (path / SEED_MANIFEST_FILE).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("format") == SEED_FORMAT
        and type(manifest.get("format_version")) is int
        and manifest.get("format_version") > 0
    )


def recover_interrupted_seed(output_dir: Path) -> bool:
    """Restore the only valid backup when promotion stopped between renames."""
    backup_pattern = f".{output_dir.name}.backup-*"
    backups = sorted(output_dir.parent.glob(backup_pattern))
    if not backups:
        return False
    if len(backups) != 1:
        raise RuntimeError(
            "found multiple interrupted seed backups; refusing recovery: "
            + ", ".join(str(path) for path in backups)
        )
    backup = backups[0]
    if output_dir.exists():
        raise RuntimeError(
            f"both seed output and recovery backup exist: {output_dir}, {backup}"
        )
    if not is_seed_directory(backup):
        raise RuntimeError(f"interrupted seed backup has no valid marker: {backup}")
    backup.replace(output_dir)
    log.warning("Restored interrupted embedding seed backup: %s", output_dir)
    return True


def collect_current_v2_documents(kb_dir: Path, provider: str) -> tuple[dict, dict[str, str], str]:
    """Return the exact indexed profile, unique documents, and fingerprint."""
    raw_school, raw_evidence, _stats = build_index.load_optional_v2_records(kb_dir)
    school_records = build_index.normalize_v2_records(raw_school, layer="school")
    evidence_records = build_index.normalize_v2_records(raw_evidence, layer="evidence")
    del raw_school, raw_evidence
    if not school_records and not evidence_records:
        raise RuntimeError("no deterministic v2 records are available")

    spec = build_index.v2_embedding_spec(provider)
    profile = spec["input_profile"]
    build_index.apply_v2_embedding_strategy(
        school_records, build_index.V2_NATIVE_STRATEGY, profile
    )
    build_index.apply_v2_embedding_strategy(
        evidence_records, build_index.V2_NATIVE_STRATEGY, profile
    )
    fingerprint = build_index.fingerprint_v2_records(
        school_records, evidence_records
    )

    documents_by_hash: dict[str, str] = {}
    for record in school_records + evidence_records:
        document = record["document"]
        content_hash = document_content_hash(document)
        previous = documents_by_hash.setdefault(content_hash, document)
        if previous != document:
            raise RuntimeError("SHA-256 collision in current v2 documents")
    return spec, documents_by_hash, fingerprint


def promote_seed_directory(staging_dir: Path, output_dir: Path) -> None:
    """Atomically promote a complete seed, restoring the prior one on error."""
    backup_pattern = f".{output_dir.name}.backup-*"
    leftovers = sorted(output_dir.parent.glob(backup_pattern))
    if leftovers:
        raise RuntimeError(
            "found interrupted seed backup(s); refusing automatic replacement: "
            + ", ".join(str(path) for path in leftovers)
        )

    backup_dir = None
    try:
        if output_dir.exists():
            if not is_seed_directory(output_dir):
                raise RuntimeError(
                    f"refusing to replace a directory without a seed marker: {output_dir}"
                )
            backup_dir = output_dir.with_name(
                f".{output_dir.name}.backup-{uuid.uuid4().hex}"
            )
            output_dir.replace(backup_dir)
        staging_dir.replace(output_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not output_dir.exists():
            backup_dir.replace(output_dir)
        raise

    if backup_dir is not None:
        try:
            shutil.rmtree(backup_dir)
        except OSError as exc:
            log.warning(
                "new seed is active, but old backup cleanup failed: %s", exc
            )


def export_seed(kb_dir: Path, output_dir: Path, provider: str) -> dict:
    """Build and atomically publish the seed, returning its manifest."""
    kb_dir = Path(kb_dir)
    output_dir = Path(output_dir)
    resolved_kb = kb_dir.resolve()
    resolved_output = output_dir.resolve()
    if not output_dir.name or resolved_kb == resolved_output:
        raise ValueError("seed output must be a dedicated child directory")
    try:
        kb_is_inside_output = resolved_kb.is_relative_to(resolved_output)
    except AttributeError:  # Python 3.8/3.9 compatibility for direct script use
        kb_is_inside_output = resolved_output in resolved_kb.parents
    if kb_is_inside_output:
        raise ValueError("seed output must not contain the knowledge base")
    default_output = (resolved_kb / build_index.V2_EMBEDDING_SEED_DIR).resolve()
    try:
        output_is_inside_kb = resolved_output.is_relative_to(resolved_kb)
    except AttributeError:  # Python 3.8/3.9 compatibility for direct script use
        output_is_inside_kb = resolved_kb in resolved_output.parents
    if output_is_inside_kb and resolved_output != default_output:
        raise ValueError(
            "inside knowledge_base, seed output must be embedding_seed_v2"
        )
    if output_dir.parent.exists():
        recover_interrupted_seed(output_dir)
    if output_dir.exists() and not is_seed_directory(output_dir):
        raise ValueError(
            f"refusing to replace a directory without a seed marker: {output_dir}"
        )

    spec, documents_by_hash, fingerprint = collect_current_v2_documents(
        kb_dir, provider
    )
    content_hashes = sorted(documents_by_hash)
    cache_path = kb_dir / build_index.V2_EMBEDDING_CACHE
    if not cache_path.is_file():
        raise RuntimeError(f"native v2 embedding cache is missing: {cache_path}")

    with EmbeddingCache(cache_path) as cache:
        cached = cache.get_many(
            spec["cache_model_key"],
            content_hashes,
            expected_dimension=spec["dimension"],
        )
    missing = [content_hash for content_hash in content_hashes if content_hash not in cached]
    if missing:
        examples = ", ".join(missing[:5])
        raise RuntimeError(
            f"current-profile cache is incomplete: {len(missing)} / "
            f"{len(content_hashes)} unique documents missing ({examples})"
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.build-", dir=str(output_dir.parent)
        )
    )
    try:
        shards = {}
        for prefix in SEED_PREFIXES:
            shard_hashes = [
                content_hash
                for content_hash in content_hashes
                if content_hash.startswith(prefix)
            ]
            shards[prefix] = write_embedding_seed_shard(
                staging_dir / f"{prefix}.npz",
                shard_hashes,
                [cached[content_hash] for content_hash in shard_hashes],
                expected_dimension=spec["dimension"],
            )

        manifest = {
            "format": SEED_FORMAT,
            "format_version": SEED_FORMAT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model_key": spec["cache_model_key"],
            "input_profile": spec["input_profile"],
            "model": spec["model"],
            "model_revision": spec["revision"],
            "dimension": spec["dimension"],
            "dtype": SEED_DTYPE,
            "content_hash_algorithm": SEED_HASH_ALGORITHM,
            "shard_scheme": SEED_SHARD_SCHEME,
            "v2_input_fingerprint": fingerprint,
            "unique_document_count": len(content_hashes),
            "shards": shards,
        }
        (staging_dir / SEED_MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        verified, verification = load_embedding_seed(
            staging_dir,
            content_hashes,
            expected_model_key=spec["cache_model_key"],
            expected_input_profile=spec["input_profile"],
            expected_model=spec["model"],
            expected_dimension=spec["dimension"],
            current_v2_fingerprint=fingerprint,
            verify_all_shards=True,
        )
        if (
            len(verified) != len(content_hashes)
            or verification["invalid_shards"]
            or set(verification["validated_shards"]) != set(SEED_PREFIXES)
        ):
            raise RuntimeError(
                "staged embedding seed failed full verification: "
                f"{verification}"
            )
        mismatched = [
            content_hash
            for content_hash in content_hashes
            if array("f", verified[content_hash]).tobytes()
            != array("f", cached[content_hash]).tobytes()
        ]
        if mismatched:
            raise RuntimeError(
                "staged embedding seed changed cached float32 vectors: "
                + ", ".join(mismatched[:5])
            )

        promote_seed_directory(staging_dir, output_dir)
        staging_dir = None
        return manifest
    finally:
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the complete cached native v2 embeddings as release shards."
    )
    parser.add_argument(
        "--kb",
        default=None,
        help=f"knowledge_base directory (default {SKILL_DIR / 'knowledge_base'})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="output directory (default <kb>/embedding_seed_v2)",
    )
    parser.add_argument(
        "--embedder",
        choices=["local", "openai"],
        default="local",
        help="current native cache provider (default local)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    kb_dir = Path(args.kb) if args.kb else SKILL_DIR / "knowledge_base"
    output_dir = (
        Path(args.output)
        if args.output
        else kb_dir / build_index.V2_EMBEDDING_SEED_DIR
    )
    if not kb_dir.is_dir():
        log.error("knowledge_base/ not found: %s", kb_dir)
        return 1

    try:
        manifest = export_seed(kb_dir, output_dir, args.embedder)
    except Exception as exc:  # noqa: BLE001 - CLI reports a safe failed export
        log.error("v2 embedding seed export failed: %s", exc)
        return 1
    log.info(
        "Exported %d exact float32 embeddings to %s",
        manifest["unique_document_count"],
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
