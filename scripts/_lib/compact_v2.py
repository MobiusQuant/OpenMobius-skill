"""Deterministic, size-efficient v2 corpus for constrained skill hosts.

The compact representation stores each attributable statement once and
reconstructs the exact School-projection and source-evidence retrieval records
at runtime. It intentionally contains no vectors or canonical fused cards.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .knowledge_v2 import (
    EMBEDDING_STRATEGY,
    EVIDENCE_COLLECTION,
    SCHOOL_COLLECTION,
    SCHEMA_VERSION,
    BuildResult,
    _evidence_search_text,
    _projection_search_text,
    _stable_id,
)


COMPACT_V2_FILENAME = "knowledge_v2.compact.json"
COMPACT_V2_FORMAT = "openmobius-compact-v2"
COMPACT_V2_FORMAT_VERSION = 1
COMPACT_V2_MAX_UNCOMPRESSED_BYTES = 8_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CARD_TYPES = ("concept", "case")
_BODY_KEYS = {"cards", "content_types", "projections", "schools", "sources"}


def _json_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"compact v2 {label} must be a non-empty string")
    return value


def _sorted_unique_strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"compact v2 {label} must be a string list")
    if value != sorted(set(value)):
        raise ValueError(f"compact v2 {label} must be sorted and unique")
    return value


def encode_compact_v2(result: BuildResult) -> bytes:
    """Encode exact School/evidence records without duplicating their content."""
    school_records = sorted(result.school_records, key=lambda record: str(record["id"]))
    evidence_records = sorted(
        result.evidence_records,
        key=lambda record: str(record["id"]),
    )
    evidence_by_id = {str(record["id"]): record for record in evidence_records}
    if len(evidence_by_id) != len(evidence_records):
        raise ValueError("cannot compact duplicate source-evidence ids")
    if not school_records or not evidence_records:
        raise ValueError("compact v2 requires populated School and evidence layers")

    schools = sorted(
        {str(record["payload"]["school"]) for record in school_records}
    )
    sources = sorted(
        {
            str(source)
            for record in school_records
            for source in record["payload"]["source_names"]
        }
        | {str(record["payload"]["source"]) for record in evidence_records}
    )
    content_types = sorted(
        {
            str(content_type)
            for record in school_records
            for content_type in record["payload"]["content_by_type"]
        }
    )
    school_index = {value: index for index, value in enumerate(schools)}
    source_index = {value: index for index, value in enumerate(sources)}
    content_type_index = {
        value: index for index, value in enumerate(content_types)
    }

    cards: dict[tuple[str, str], list[Any]] = {}
    for record in school_records:
        payload = record["payload"]
        card_type = str(payload["type"])
        if card_type not in _CARD_TYPES:
            raise ValueError(f"cannot compact unknown card type: {card_type}")
        key = (card_type, str(payload["canonical_id"]))
        row = [
            str(payload["canonical_id"]),
            str(payload["canonical_term"]),
            _CARD_TYPES.index(card_type),
            list(payload["aliases"]),
            str(payload["file_path"]),
        ]
        previous = cards.setdefault(key, row)
        if previous != row:
            raise ValueError(f"inconsistent compact card identity: {key}")
    card_keys = sorted(cards)
    card_rows = [cards[key] for key in card_keys]
    card_index = {key: index for index, key in enumerate(card_keys)}

    projections: list[list[Any]] = []
    used_evidence: set[str] = set()
    projection_only_count = 0
    for record in school_records:
        payload = record["payload"]
        card_key = (str(payload["type"]), str(payload["canonical_id"]))
        items: list[list[Any]] = []
        for content_type, values in payload["content_by_type"].items():
            for item in values:
                evidence_id = item.get("evidence_id")
                source_position = -1
                if evidence_id:
                    evidence_id = str(evidence_id)
                    if evidence_id in used_evidence:
                        raise ValueError(
                            f"duplicate compact evidence reference: {evidence_id}"
                        )
                    evidence_record = evidence_by_id.get(evidence_id)
                    if evidence_record is None:
                        raise ValueError(
                            f"missing compact evidence record: {evidence_id}"
                        )
                    evidence = evidence_record["payload"]
                    expected = {
                        "canonical_id": payload["canonical_id"],
                        "canonical_term": payload["canonical_term"],
                        "type": payload["type"],
                        "school": payload["school"],
                        "content_type": content_type,
                        "content": item["content"],
                        "file_path": payload["file_path"],
                        "ref": item["ref"],
                    }
                    if any(evidence.get(key) != value for key, value in expected.items()):
                        raise ValueError(
                            f"evidence/projection mismatch while compacting {evidence_id}"
                        )
                    source = str(evidence["source"])
                    if source not in payload["source_names"]:
                        raise ValueError(
                            f"evidence source missing from projection: {evidence_id}"
                        )
                    source_position = source_index[source]
                    used_evidence.add(evidence_id)
                else:
                    projection_only_count += 1
                items.append(
                    [
                        content_type_index[str(content_type)],
                        source_position,
                        str(item["content"]),
                        str(item["ref"]),
                    ]
                )
        projections.append(
            [
                card_index[card_key],
                school_index[str(payload["school"])],
                [source_index[str(source)] for source in payload["source_names"]],
                items,
            ]
        )
    if used_evidence != set(evidence_by_id):
        missing = sorted(set(evidence_by_id) - used_evidence)
        raise ValueError(
            "source evidence is not a bijection with School projection items: "
            + ", ".join(missing[:3])
        )

    body = _json_line(
        {
            "cards": card_rows,
            "content_types": content_types,
            "projections": projections,
            "schools": schools,
            "sources": sources,
        }
    )
    header = {
        "card_count": len(card_rows),
        "collections": {
            SCHOOL_COLLECTION: len(school_records),
            EVIDENCE_COLLECTION: len(evidence_records),
        },
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "format": COMPACT_V2_FORMAT,
        "format_version": COMPACT_V2_FORMAT_VERSION,
        "input_fingerprint": result.input_fingerprint,
        "projection_only_count": projection_only_count,
        "registry_version": result.registry_version,
    }
    encoded = _json_line(header) + body
    if len(encoded) > COMPACT_V2_MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            "compact v2 corpus exceeds its uncompressed safety limit: "
            f"{len(encoded)} bytes"
        )
    return encoded


def _validated_index(value: object, length: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
        raise ValueError(f"compact v2 {label} index is out of range: {value!r}")
    return value


def _decode_compact_v2(payload: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    if not payload or len(payload) > COMPACT_V2_MAX_UNCOMPRESSED_BYTES:
        raise ValueError("compact v2 corpus has an invalid uncompressed size")
    header_line, separator, body = payload.partition(b"\n")
    if not separator or not body or not body.endswith(b"\n") or b"\n" in body[:-1]:
        raise ValueError("compact v2 corpus framing is invalid")
    try:
        header = json.loads(header_line)
        body_object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"compact v2 corpus contains invalid JSON: {exc}") from exc
    if (
        not isinstance(header, dict)
        or header.get("format") != COMPACT_V2_FORMAT
        or header.get("format_version") != COMPACT_V2_FORMAT_VERSION
        or not _SHA256_RE.fullmatch(str(header.get("input_fingerprint", "")))
        or not _SHA256_RE.fullmatch(str(header.get("content_sha256", "")))
        or isinstance(header.get("registry_version"), bool)
        or not isinstance(header.get("registry_version"), int)
        or header["registry_version"] <= 0
        or isinstance(header.get("card_count"), bool)
        or not isinstance(header.get("card_count"), int)
        or header["card_count"] <= 0
        or isinstance(header.get("projection_only_count"), bool)
        or not isinstance(header.get("projection_only_count"), int)
        or header["projection_only_count"] < 0
        or not isinstance(header.get("collections"), dict)
    ):
        raise ValueError("compact v2 corpus header is unsupported")
    for collection in (SCHOOL_COLLECTION, EVIDENCE_COLLECTION):
        count = header["collections"].get(collection)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"compact v2 collection count is invalid: {collection}")
    if hashlib.sha256(body).hexdigest() != header["content_sha256"]:
        raise ValueError("compact v2 corpus content hash mismatch")
    if not isinstance(body_object, dict) or set(body_object) != _BODY_KEYS:
        raise ValueError("compact v2 corpus body has unsupported fields")
    return header, body_object


def _validate_relative_card_path(value: object) -> str:
    path = _nonempty_string(value, "card path")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or "\\" in path
        or ":" in path
        or any(ord(char) < 32 for char in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or pure.suffix != ".json"
    ):
        raise ValueError(f"compact v2 card path is unsafe: {path}")
    return path


def reconstruct_compact_v2_records(
    payload: bytes,
) -> dict[str, list[dict[str, Any]]]:
    """Validate a compact payload and reconstruct exact retrieval records."""
    header, body = _decode_compact_v2(payload)
    schools = _sorted_unique_strings(body["schools"], "schools")
    sources = _sorted_unique_strings(body["sources"], "sources")
    content_types = _sorted_unique_strings(body["content_types"], "content types")
    cards = body["cards"]
    projections = body["projections"]
    if not isinstance(cards, list) or len(cards) != header["card_count"]:
        raise ValueError("compact v2 card count mismatch")
    if not isinstance(projections, list):
        raise ValueError("compact v2 projections must be a list")

    decoded_cards: list[dict[str, Any]] = []
    card_keys: list[tuple[str, str]] = []
    for row in cards:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("compact v2 card row is invalid")
        canonical_id = _nonempty_string(row[0], "canonical id")
        canonical_term = _nonempty_string(row[1], "canonical term")
        type_index = _validated_index(row[2], len(_CARD_TYPES), "card type")
        aliases = row[3]
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ) or len(set(aliases)) != len(aliases):
            raise ValueError("compact v2 card aliases are invalid")
        card_type = _CARD_TYPES[type_index]
        key = (card_type, canonical_id)
        card_keys.append(key)
        decoded_cards.append(
            {
                "canonical_id": canonical_id,
                "canonical_term": canonical_term,
                "type": card_type,
                "aliases": aliases,
                "file_path": _validate_relative_card_path(row[4]),
            }
        )
    if card_keys != sorted(set(card_keys)):
        raise ValueError("compact v2 cards must be sorted and unique")

    school_records: list[dict[str, Any]] = []
    evidence_records: list[dict[str, Any]] = []
    projection_keys: set[tuple[str, str, str]] = set()
    evidence_ids: set[str] = set()
    actual_projection_only = 0
    for row in projections:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("compact v2 projection row is invalid")
        card = decoded_cards[_validated_index(row[0], len(decoded_cards), "card")]
        school = schools[_validated_index(row[1], len(schools), "school")]
        raw_source_names = row[2]
        raw_items = row[3]
        if (
            not isinstance(raw_source_names, list)
            or not isinstance(raw_items, list)
            or not raw_items
        ):
            raise ValueError("compact v2 projection contents are invalid")
        source_positions = [
            _validated_index(value, len(sources), "projection source")
            for value in raw_source_names
        ]
        if source_positions != sorted(set(source_positions)):
            raise ValueError("compact v2 projection sources must be sorted and unique")
        source_names = [sources[index] for index in source_positions]
        projection_key = (card["type"], card["canonical_id"], school)
        if projection_key in projection_keys:
            raise ValueError(f"duplicate compact v2 projection: {projection_key}")
        projection_keys.add(projection_key)

        content_by_type: dict[str, list[dict[str, Any]]] = {}
        previous_content_position = -1
        for item in raw_items:
            if not isinstance(item, list) or len(item) != 4:
                raise ValueError("compact v2 content row is invalid")
            content_position = _validated_index(
                item[0], len(content_types), "content type"
            )
            if content_position < previous_content_position:
                raise ValueError("compact v2 content types are not grouped")
            previous_content_position = content_position
            content_type = content_types[content_position]
            source_position = item[1]
            content = _nonempty_string(item[2], "content")
            ref = _nonempty_string(item[3], "content ref")
            projected_item: dict[str, Any] = {
                "attribution_level": "school",
                "content": content,
                "ref": ref,
            }
            if source_position != -1:
                source_position = _validated_index(
                    source_position, len(sources), "evidence source"
                )
                source = sources[source_position]
                if source not in source_names:
                    raise ValueError("compact v2 evidence source is outside projection")
                evidence_id = _stable_id(
                    "ev",
                    card["canonical_id"],
                    school,
                    source,
                    content_type,
                    card["file_path"],
                    ref,
                    content,
                )
                if evidence_id in evidence_ids:
                    raise ValueError(f"duplicate compact v2 evidence id: {evidence_id}")
                evidence_ids.add(evidence_id)
                projected_item.update(
                    {
                        "attribution_level": "source",
                        "evidence_id": evidence_id,
                        "source": source,
                    }
                )
                evidence_payload = {
                    "canonical_term": card["canonical_term"],
                    "school": school,
                    "source": source,
                    "content_type": content_type,
                    "content": content,
                }
                evidence_records.append(
                    {
                        "id": evidence_id,
                        "document": _evidence_search_text(
                            evidence_payload,
                            card["aliases"],
                        ),
                        "metadata": {
                            "canonical_id": card["canonical_id"],
                            "content_type": content_type,
                            "embedding_strategy": EMBEDDING_STRATEGY,
                            "evidence_id": evidence_id,
                            "file_path": card["file_path"],
                            "layer": "evidence",
                            "record_id": evidence_id,
                            "ref": ref,
                            "schema_version": SCHEMA_VERSION,
                            "school": school,
                            "source": source,
                            "term": card["canonical_term"],
                            "type": card["type"],
                        },
                    }
                )
            else:
                actual_projection_only += 1
            content_by_type.setdefault(content_type, []).append(projected_item)

        record_id = _stable_id(
            "sp", card["type"], card["canonical_id"], school
        )
        projection_payload = {
            "aliases": card["aliases"],
            "canonical_term": card["canonical_term"],
            "content_by_type": content_by_type,
            "school": school,
            "source_names": source_names,
        }
        school_records.append(
            {
                "id": record_id,
                "document": _projection_search_text(projection_payload),
                "metadata": {
                    "canonical_id": card["canonical_id"],
                    "embedding_strategy": EMBEDDING_STRATEGY,
                    "file_path": card["file_path"],
                    "layer": "school",
                    "record_id": record_id,
                    "schema_version": SCHEMA_VERSION,
                    "school": school,
                    "source_collection_count": len(source_names),
                    "source_names": json.dumps(
                        source_names,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "term": card["canonical_term"],
                    "type": card["type"],
                },
            }
        )

    school_records.sort(key=lambda record: record["id"])
    evidence_records.sort(key=lambda record: record["id"])
    if {key[:2] for key in projection_keys} != set(card_keys):
        raise ValueError("compact v2 contains an unreferenced card")
    expected = header["collections"]
    if (
        len(school_records) != expected[SCHOOL_COLLECTION]
        or len(evidence_records) != expected[EVIDENCE_COLLECTION]
        or actual_projection_only != header["projection_only_count"]
    ):
        raise ValueError("compact v2 reconstructed counts do not match the header")
    return {
        "school": school_records,
        "evidence": evidence_records,
    }


def load_compact_v2_records(path: Path, layer: str) -> list[dict[str, Any]]:
    """Read one exact retrieval layer from a verified compact v2 corpus."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"compact v2 corpus is not a regular file: {path}")
    if path.stat().st_size > COMPACT_V2_MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"compact v2 corpus exceeds the safety limit: {path}")
    records = reconstruct_compact_v2_records(path.read_bytes())
    if layer not in records:
        raise ValueError(
            "compact v2 contains only attributable School and exact-source "
            f"evidence layers; unsupported layer={layer}"
        )
    return records[layer]
