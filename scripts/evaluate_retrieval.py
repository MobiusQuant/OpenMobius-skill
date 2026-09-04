#!/usr/bin/env python3
"""Offline, attribution-aware retrieval evaluation for the local knowledge base.

The benchmark is validated against the current canonical cards and the
deterministic v2 builder before any index or embedding model is loaded.  This
keeps stale record ids, impossible scopes, and mislabeled aliases from being
silently counted as retrieval misses.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


THIS_DIR = Path(__file__).resolve().parent
SKILL_DIR = THIS_DIR.parent
sys.path.insert(0, str(THIS_DIR))

from _lib.retriever import (  # noqa: E402
    LAYER_COLLECTIONS,
    RetrievalScopeError,
    resolve_search_mode,
)


DEFAULT_KB = SKILL_DIR / "knowledge_base"
DEFAULT_DATASET = SKILL_DIR / "evals" / "retrieval_benchmark_v1.jsonl"
BENCHMARK_VERSION = 1
LAYERS = tuple(LAYER_COLLECTIONS)
SEARCH_MODES = ("auto", "hybrid", "semantic", "lexical")
QUERY_EMBEDDING_PROVIDERS = {
    "local": {
        "model": "nomic-ai/nomic-embed-text-v1.5",
        "revision": "e9b6763023c676ca8431644204f50c2b100d9aab",
        "dimension": 768,
    },
    "openai": {
        "model": "text-embedding-3-small",
        "revision": None,
        "dimension": 1536,
    },
}
LANGUAGES = ("en", "zh", "mixed")
POSITIVE_QUERY_KINDS = (
    "canonical_term",
    "alias",
    "natural_language",
    "evidence_text",
)
NEGATIVE_REASONS = (
    "unknown_school",
    "unknown_source",
    "empty_intersection",
    "unsupported_source_layer",
    "overlapping_schools",
)
TRUTH_SOURCES = {
    "canonical_term": "canonical_card.term",
    "alias": "knowledge_base/term_aliases.json",
    "natural_language": "canonical_card.term_template",
    "evidence_text": "source_evidence_v2.content",
    "negative_scope": "retrieval_scope_contract",
}


class BenchmarkValidationError(ValueError):
    """The static benchmark does not agree with its contract or current KB."""


class RetrieverAdapterError(RuntimeError):
    """The installed Retriever API cannot honestly run the requested mode."""


class EvaluationSetupError(RuntimeError):
    """The requested query embedder cannot search the current index honestly."""


def _display_path(path: Path) -> str:
    """Prefer checkout-relative paths in reports intended for version control."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(SKILL_DIR.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _nonempty_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise BenchmarkValidationError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BenchmarkValidationError(f"{field} values must be non-empty strings")
        item = item.strip()
        if item in result:
            raise BenchmarkValidationError(f"{field} contains duplicate value {item!r}")
        result.append(item)
    return result


def _scope(case: Mapping[str, Any]) -> dict[str, Any]:
    raw = case.get("scope")
    if not isinstance(raw, dict):
        raise BenchmarkValidationError(f"{case.get('id', '?')}: scope must be an object")
    allowed = {"schools", "sources", "exclude_schools", "type"}
    extras = sorted(set(raw) - allowed)
    if extras:
        raise BenchmarkValidationError(
            f"{case.get('id', '?')}: unknown scope fields: {', '.join(extras)}"
        )
    card_type = raw.get("type")
    if card_type not in (None, "concept", "case"):
        raise BenchmarkValidationError(
            f"{case.get('id', '?')}: scope.type must be concept, case, or null"
        )
    return {
        "schools": _nonempty_strings(raw.get("schools", []), "scope.schools"),
        "sources": _nonempty_strings(raw.get("sources", []), "scope.sources"),
        "exclude_schools": _nonempty_strings(
            raw.get("exclude_schools", []), "scope.exclude_schools"
        ),
        "type": card_type,
    }


def _validate_case_shape(case: Any, line_number: int) -> None:
    if not isinstance(case, dict):
        raise BenchmarkValidationError(f"line {line_number}: JSON value must be an object")
    required = {
        "benchmark_version",
        "id",
        "query",
        "language",
        "query_kind",
        "truth_source",
        "layer",
        "scope",
        "expected",
    }
    missing = sorted(required - set(case))
    if missing:
        raise BenchmarkValidationError(
            f"line {line_number}: missing fields: {', '.join(missing)}"
        )
    if case["benchmark_version"] != BENCHMARK_VERSION:
        raise BenchmarkValidationError(
            f"line {line_number}: benchmark_version must be {BENCHMARK_VERSION}"
        )
    case_id = case["id"]
    if not isinstance(case_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", case_id):
        raise BenchmarkValidationError(f"line {line_number}: invalid id {case_id!r}")
    if not isinstance(case["query"], str) or not case["query"].strip():
        raise BenchmarkValidationError(f"{case_id}: query must be a non-empty string")
    if case["language"] not in LANGUAGES:
        raise BenchmarkValidationError(f"{case_id}: invalid language")
    if case["layer"] not in LAYERS:
        raise BenchmarkValidationError(f"{case_id}: invalid layer")
    expected_truth_source = TRUTH_SOURCES.get(str(case["query_kind"]))
    if case["truth_source"] != expected_truth_source:
        raise BenchmarkValidationError(
            f"{case_id}: truth_source must be {expected_truth_source!r}"
        )
    _scope(case)

    expected = case["expected"]
    if not isinstance(expected, dict):
        raise BenchmarkValidationError(f"{case_id}: expected must be an object")
    outcome = expected.get("outcome")
    if outcome == "hit":
        if case["query_kind"] not in POSITIVE_QUERY_KINDS:
            raise BenchmarkValidationError(f"{case_id}: invalid positive query_kind")
        record_ids = _nonempty_strings(expected.get("record_ids"), "expected.record_ids")
        canonical_ids = _nonempty_strings(
            expected.get("canonical_ids"), "expected.canonical_ids"
        )
        if not record_ids or not canonical_ids:
            raise BenchmarkValidationError(f"{case_id}: hit expectations cannot be empty")
        if not isinstance(case.get("target"), dict):
            raise BenchmarkValidationError(f"{case_id}: hit case requires target metadata")
    elif outcome == "fail_closed":
        if case["query_kind"] != "negative_scope":
            raise BenchmarkValidationError(
                f"{case_id}: fail_closed case must use query_kind=negative_scope"
            )
        if expected.get("reason") not in NEGATIVE_REASONS:
            raise BenchmarkValidationError(f"{case_id}: invalid fail-closed reason")
        if "target" in case:
            raise BenchmarkValidationError(f"{case_id}: negative case must not have target")
    else:
        raise BenchmarkValidationError(f"{case_id}: expected.outcome is invalid")


def load_benchmark(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Load JSONL strictly and return cases plus a byte-level SHA-256."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise BenchmarkValidationError(f"cannot read benchmark {path}: {exc}") from exc
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    for line_number, raw_line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            case = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise BenchmarkValidationError(
                f"line {line_number}: invalid JSON: {exc.msg}"
            ) from exc
        _validate_case_shape(case, line_number)
        if case["id"] in ids:
            raise BenchmarkValidationError(f"duplicate benchmark id: {case['id']}")
        ids.add(case["id"])
        cases.append(case)
    if not cases:
        raise BenchmarkValidationError("benchmark is empty")
    return cases, hashlib.sha256(raw).hexdigest()


def _load_aliases(kb_dir: Path) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = defaultdict(set)
    try:
        payload = json.loads((kb_dir / "term_aliases.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkValidationError(f"cannot read term_aliases.json: {exc}") from exc
    for mapping in payload.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        canonical_id = str(mapping.get("card_id") or "").strip()
        if not canonical_id:
            continue
        for alias in mapping.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                aliases[canonical_id].add(_normalize_text(alias))
    return aliases


def _load_known_school_labels(kb_dir: Path) -> set[str]:
    try:
        payload = json.loads((kb_dir / "schools.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkValidationError(f"cannot read schools.json: {exc}") from exc
    labels: set[str] = set()
    for entry in payload.get("schools") or []:
        if not isinstance(entry, dict):
            continue
        for value in (entry.get("id"), entry.get("name"), *(entry.get("aliases") or [])):
            if isinstance(value, str) and value.strip():
                labels.add(_normalize_text(value))
    return labels


def load_kb_inventory(kb_dir: Path) -> dict[str, Any]:
    """Build layer inventories from source cards, never from the live index."""
    kb_dir = Path(kb_dir)
    aliases = _load_aliases(kb_dir)
    records: dict[str, dict[str, dict[str, Any]]] = {
        layer: {} for layer in LAYERS
    }
    for card_type, dirname in (("concept", "concepts"), ("case", "cases")):
        for path in sorted((kb_dir / dirname).glob("*.json")):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BenchmarkValidationError(f"cannot read KB card {path}: {exc}") from exc
            canonical_id = str(
                card.get("global_card_id") or card.get("card_id") or path.stem
            ).strip()
            term = str(
                card.get("canonical_term") or card.get("title") or path.stem
            ).strip()
            records["canonical"][path.stem] = {
                "record_id": path.stem,
                "canonical_id": canonical_id,
                "type": card_type,
                "term": term,
                "school": str(card.get("school") or "").strip(),
                "source": "",
                "content_type": "",
                "content": "",
                "ref": "",
                "file_path": f"{dirname}/{path.name}",
            }

    try:
        from _lib.knowledge_v2 import build_v2_records  # noqa: PLC0415
    except ImportError as exc:
        raise BenchmarkValidationError(
            "v2 benchmark validation requires _lib.knowledge_v2.build_v2_records"
        ) from exc
    built = build_v2_records(kb_dir)
    for layer, layer_records in (
        ("school", built.school_records),
        ("evidence", built.evidence_records),
    ):
        for wrapper in layer_records:
            metadata = wrapper["metadata"]
            payload = wrapper["payload"]
            record_id = str(wrapper["id"])
            records[layer][record_id] = {
                "record_id": record_id,
                "canonical_id": str(metadata["canonical_id"]),
                "type": str(metadata["type"]),
                "term": str(metadata["term"]),
                "school": str(metadata["school"]),
                "source": str(metadata.get("source") or ""),
                "content_type": str(metadata.get("content_type") or ""),
                "content": str(payload.get("content") or ""),
                "ref": str(metadata.get("ref") or ""),
                "file_path": str(metadata["file_path"]),
            }
    alias_owners: dict[str, set[str]] = defaultdict(set)
    for canonical_id, values in aliases.items():
        for value in values:
            alias_owners[value].add(canonical_id)
    return {
        "records": records,
        "aliases": aliases,
        "alias_owners": alias_owners,
        "known_school_labels": _load_known_school_labels(kb_dir),
    }


def _record_matches_scope(record: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    schools = scope["schools"]
    sources = scope["sources"]
    excluded = scope["exclude_schools"]
    return (
        (not schools or record.get("school") in schools)
        and (not sources or record.get("source") in sources)
        and (not excluded or record.get("school") not in excluded)
        and (scope["type"] is None or record.get("type") == scope["type"])
    )


def _validate_target_snapshot(
    case_id: str,
    target: Mapping[str, Any],
    record: Mapping[str, Any],
    layer: str,
) -> None:
    required = [
        "record_id",
        "canonical_id",
        "type",
        "term",
        "school",
        "file_path",
    ]
    if layer == "evidence":
        required.extend(["source", "content_type", "ref"])
    for field in required:
        if field not in target:
            raise BenchmarkValidationError(f"{case_id}: target.{field} is required")
        if target[field] != record[field]:
            raise BenchmarkValidationError(
                f"{case_id}: target.{field}={target[field]!r} does not match "
                f"current KB value {record[field]!r}"
            )


def validate_benchmark_against_kb(
    cases: Sequence[Mapping[str, Any]],
    kb_dir: Path,
    *,
    enforce_distribution: bool = True,
) -> dict[str, Any]:
    """Reject stale ids, metadata snapshots, aliases, and impossible scopes."""
    inventory = load_kb_inventory(kb_dir)
    records = inventory["records"]
    aliases = inventory["aliases"]
    alias_owners = inventory["alias_owners"]
    known_school_labels = inventory["known_school_labels"]
    all_sources = {r["source"] for r in records["evidence"].values() if r["source"]}

    for case in cases:
        case_id = str(case["id"])
        layer = str(case["layer"])
        scope = _scope(case)
        expected = case["expected"]
        if expected["outcome"] == "hit":
            expected_records: list[Mapping[str, Any]] = []
            for record_id in expected["record_ids"]:
                record = records[layer].get(record_id)
                if record is None:
                    raise BenchmarkValidationError(
                        f"{case_id}: unknown {layer} record_id {record_id!r}"
                    )
                if not _record_matches_scope(record, scope):
                    raise BenchmarkValidationError(
                        f"{case_id}: expected record {record_id!r} is outside its scope"
                    )
                expected_records.append(record)
            actual_canonical_ids = {r["canonical_id"] for r in expected_records}
            if actual_canonical_ids != set(expected["canonical_ids"]):
                raise BenchmarkValidationError(
                    f"{case_id}: expected canonical_ids do not match record metadata"
                )
            target = case["target"]
            target_id = target.get("record_id")
            if target_id not in expected["record_ids"]:
                raise BenchmarkValidationError(
                    f"{case_id}: target.record_id must be relevant"
                )
            target_record = records[layer][target_id]
            _validate_target_snapshot(case_id, target, target_record, layer)

            normalized_query = _normalize_text(case["query"])
            kind = case["query_kind"]
            if kind == "canonical_term" and normalized_query != _normalize_text(
                target_record["term"]
            ):
                raise BenchmarkValidationError(
                    f"{case_id}: canonical_term query does not equal target term"
                )
            if kind == "alias" and normalized_query not in aliases.get(
                target_record["canonical_id"], set()
            ):
                raise BenchmarkValidationError(
                    f"{case_id}: query is not a current alias of its canonical id"
                )
            if kind == "alias" and alias_owners.get(normalized_query, set()) != set(
                expected["canonical_ids"]
            ):
                raise BenchmarkValidationError(
                    f"{case_id}: alias truth is ambiguous or relevance is incomplete"
                )
            if kind == "natural_language" and _normalize_text(
                target_record["term"]
            ) not in normalized_query:
                raise BenchmarkValidationError(
                    f"{case_id}: natural query must retain its traceable target term"
                )
            if kind == "evidence_text":
                if layer != "evidence" or normalized_query not in _normalize_text(
                    target_record["content"]
                ):
                    raise BenchmarkValidationError(
                        f"{case_id}: evidence_text query is not from target evidence"
                    )
        else:
            reason = expected["reason"]
            available_schools = {
                str(record["school"]) for record in records[layer].values()
            }
            if reason == "unknown_school":
                if not scope["schools"] or all(
                    _normalize_text(value) in known_school_labels
                    for value in scope["schools"]
                ):
                    raise BenchmarkValidationError(
                        f"{case_id}: unknown_school scope is not actually unknown"
                    )
            elif reason == "unknown_source":
                if layer != "evidence" or not scope["sources"] or all(
                    value in all_sources for value in scope["sources"]
                ):
                    raise BenchmarkValidationError(
                        f"{case_id}: unknown_source scope is not actually unknown"
                    )
            elif reason == "unsupported_source_layer":
                if layer == "evidence" or not scope["sources"]:
                    raise BenchmarkValidationError(
                        f"{case_id}: source selector is not on an unsupported layer"
                    )
            elif reason == "overlapping_schools":
                if not set(scope["schools"]).intersection(scope["exclude_schools"]):
                    raise BenchmarkValidationError(
                        f"{case_id}: include/exclude School scopes do not overlap"
                    )
            elif reason == "empty_intersection":
                if any(value not in available_schools for value in scope["schools"]):
                    raise BenchmarkValidationError(
                        f"{case_id}: empty intersection uses unknown School"
                    )
                if any(value not in all_sources for value in scope["sources"]):
                    raise BenchmarkValidationError(
                        f"{case_id}: empty intersection uses unknown source"
                    )
                if any(
                    _record_matches_scope(record, scope)
                    for record in records[layer].values()
                ):
                    raise BenchmarkValidationError(
                        f"{case_id}: declared empty scope has records"
                    )

    distribution = benchmark_distribution(cases)
    if enforce_distribution:
        count = distribution["cases"]
        if not 150 <= count <= 300:
            raise BenchmarkValidationError(
                f"tracked benchmark must contain 150-300 cases, got {count}"
            )
        for layer in LAYERS:
            if distribution["positive_layers"].get(layer, 0) < 20:
                raise BenchmarkValidationError(
                    f"benchmark needs at least 20 positive {layer} cases"
                )
        for language in LANGUAGES:
            if distribution["languages"].get(language, 0) < 10:
                raise BenchmarkValidationError(
                    f"benchmark needs at least 10 {language} queries"
                )
        if distribution["query_kinds"].get("alias", 0) < 20:
            raise BenchmarkValidationError("benchmark needs at least 20 alias queries")
        if set(distribution["negative_reasons"]) != set(NEGATIVE_REASONS):
            raise BenchmarkValidationError(
                "benchmark must cover every declared fail-closed reason"
            )
        if len(distribution["schools"]) < 10 or len(distribution["sources"]) < 8:
            raise BenchmarkValidationError(
                "benchmark School/source coverage is not sufficiently broad"
            )
    return distribution


def benchmark_distribution(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    outcomes: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    positive_layers: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    types: Counter[str] = Counter()
    content_types: Counter[str] = Counter()
    schools: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    negative: Counter[str] = Counter()
    for case in cases:
        outcome = case["expected"]["outcome"]
        scope = _scope(case)
        outcomes[outcome] += 1
        layers[str(case["layer"])] += 1
        languages[str(case["language"])] += 1
        kinds[str(case["query_kind"])] += 1
        if outcome == "hit":
            positive_layers[str(case["layer"])] += 1
            if scope["type"]:
                types[str(scope["type"])] += 1
            target = case.get("target") or {}
            if target.get("content_type"):
                content_types[str(target["content_type"])] += 1
            schools.update(scope["schools"])
            sources.update(scope["sources"])
        else:
            negative[str(case["expected"]["reason"])] += 1
    return {
        "cases": len(cases),
        "outcomes": dict(sorted(outcomes.items())),
        "layers": dict(sorted(layers.items())),
        "positive_layers": dict(sorted(positive_layers.items())),
        "languages": dict(sorted(languages.items())),
        "query_kinds": dict(sorted(kinds.items())),
        "types": dict(sorted(types.items())),
        "content_types": dict(sorted(content_types.items())),
        "schools": dict(sorted(schools.items())),
        "sources": dict(sorted(sources.items())),
        "negative_reasons": dict(sorted(negative.items())),
    }


def index_provenance(kb_dir: Path) -> dict[str, Any]:
    """Return the small, reproducibility-relevant part of the index manifest."""
    manifest_path = Path(kb_dir) / "_index" / "index_manifest.json"
    provenance: dict[str, Any] = {
        "manifest_path": _display_path(manifest_path),
        "manifest_present": manifest_path.is_file(),
    }
    if not manifest_path.is_file():
        return provenance
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        provenance["manifest_error"] = str(exc)
        return provenance
    strategies = manifest.get("embedding_strategy") or {}
    models = manifest.get("embedding_models") or {}
    revisions = manifest.get("embedding_revisions") or {}
    dimensions = manifest.get("embedding_dimensions") or {}
    collections: dict[str, Any] = {}
    for name, details in (manifest.get("collections") or {}).items():
        details = details if isinstance(details, dict) else {}
        collections[name] = {
            **{
                key: value
                for key, value in details.items()
                if key in {"count", "created", "layer", "schema_version"}
            },
            "embedding_strategy": (
                strategies.get(name) if isinstance(strategies, dict) else strategies
            ),
            "embedding_model": (
                models.get(name)
                if isinstance(models, dict) and models
                else manifest.get("embedding_model")
            ),
            "embedding_revision": (
                revisions.get(name)
                if isinstance(revisions, dict) and name in revisions
                else manifest.get("embedding_model_revision")
            ),
            "embedding_dimension": (
                dimensions.get(name)
                if isinstance(dimensions, dict) and dimensions
                else manifest.get("embedding_dimension")
            ),
        }
    provenance.update({
        "manifest_version": manifest.get("manifest_version"),
        "index_schema_version": manifest.get("index_schema_version"),
        "canonical_input_fingerprint": manifest.get("canonical_input_fingerprint"),
        "v2_input_fingerprint": manifest.get("v2_input_fingerprint"),
        "v2_embedding_input_profile": manifest.get("v2_embedding_input_profile"),
        "canonical_embedding_input_profile": manifest.get(
            "canonical_embedding_input_profile"
        ),
        "collections": collections,
    })
    return provenance


def required_query_embedding_providers(
    layers: Iterable[str],
    *,
    search_mode: str,
    v2_provider: str,
) -> dict[str, str | None]:
    """Map each evaluated layer to its query provider, if vectors are needed."""
    providers: dict[str, str | None] = {}
    for layer in sorted(set(layers), key=LAYERS.index):
        effective_mode = resolve_search_mode(search_mode, layer)
        if effective_mode == "lexical":
            providers[layer] = None
        else:
            # Canonical cards ship with Nomic vectors. The CLI provider selects
            # only the independently embedded v2 School/evidence collections.
            providers[layer] = "local" if layer == "canonical" else v2_provider
    return providers


def validate_query_embedding_compatibility(
    provenance: Mapping[str, Any],
    providers_by_layer: Mapping[str, str | None],
) -> dict[str, dict[str, Any] | None]:
    """Fail before model loading when query vectors cannot match the index."""
    identities: dict[str, dict[str, Any] | None] = {}
    vector_layers = [layer for layer, provider in providers_by_layer.items() if provider]
    if not vector_layers:
        return {layer: None for layer in providers_by_layer}
    if not provenance.get("manifest_present"):
        raise EvaluationSetupError(
            "index manifest is missing; rebuild with scripts/build_index.py --force "
            "before semantic/hybrid evaluation"
        )
    if provenance.get("manifest_error"):
        raise EvaluationSetupError(
            f"index manifest is unreadable: {provenance['manifest_error']}; "
            "rebuild it before semantic/hybrid evaluation"
        )
    collections = provenance.get("collections") or {}
    for layer, provider in providers_by_layer.items():
        if provider is None:
            identities[layer] = None
            continue
        expected = QUERY_EMBEDDING_PROVIDERS[provider]
        collection_name = LAYER_COLLECTIONS[layer]
        actual = collections.get(collection_name)
        if not isinstance(actual, Mapping):
            raise EvaluationSetupError(
                f"index manifest has no collection {collection_name!r} for "
                f"layer={layer}; rebuild with scripts/build_index.py --force"
            )
        actual_model = actual.get("embedding_model")
        actual_revision = actual.get("embedding_revision")
        actual_dimension = actual.get("embedding_dimension")
        if (
            actual_model != expected["model"]
            or actual_revision != expected["revision"]
            or actual_dimension != expected["dimension"]
        ):
            if layer == "canonical":
                remedy = (
                    "restore the bundled canonical Nomic embeddings and rebuild "
                    "without --regenerate"
                )
            else:
                remedy = (
                    f"rebuild v2 with --embedder {provider}, or select the provider "
                    "that matches the existing v2 index"
                )
            raise EvaluationSetupError(
                f"query/index embedding mismatch for layer={layer} "
                f"({collection_name}): index model={actual_model!r}, "
                f"revision={actual_revision!r}, dimension={actual_dimension!r}; "
                f"provider={provider!r} expects model={expected['model']!r}, "
                f"revision={expected['revision']!r}, "
                f"dimension={expected['dimension']}; "
                f"{remedy}"
            )
        identities[layer] = {
            "provider": provider,
            "model": expected["model"],
            "revision": expected["revision"],
            "dimension": expected["dimension"],
        }
    return identities


def _accepts_parameter(signature: inspect.Signature, name: str) -> bool:
    return name in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def search_with_adapter(
    retriever: Any,
    case: Mapping[str, Any],
    *,
    top_k: int,
    search_mode: str,
    max_per_canonical: int | None,
    execution: dict[str, Any] | None = None,
) -> list[Any]:
    """Call current/future Retriever APIs without silently changing semantics."""
    search = getattr(retriever, "search", None)
    if not callable(search):
        raise RetrieverAdapterError("Retriever has no callable search()")
    signature = inspect.signature(search)
    exact_match_applied: bool | None = None
    if _accepts_parameter(signature, "search_mode"):
        exact_match_applied = True if _accepts_parameter(signature, "exact_match") else None
    elif search_mode == "semantic" and _accepts_parameter(signature, "exact_match"):
        exact_match_applied = False
    elif search_mode == "auto":
        # A legacy API without an explicit exact-match switch cannot expose
        # that behavior reliably enough for provenance.
        exact_match_applied = None
    if execution is not None:
        execution["exact_match"] = exact_match_applied

    scope = _scope(case)
    resolve_scope = getattr(retriever, "resolve_scope", None)
    if not callable(resolve_scope):
        raise RetrieverAdapterError(
            "Retriever must expose resolve_scope() for fail-closed evaluation"
        )
    resolved = resolve_scope(
        filter_schools=scope["schools"] or None,
        filter_sources=scope["sources"] or None,
        exclude_schools=scope["exclude_schools"] or None,
        filter_type=scope["type"],
    )
    kwargs: dict[str, Any] = {
        "query": str(case["query"]),
        "top_k": top_k,
        "filter_schools": resolved.get("schools") or None,
        "filter_sources": resolved.get("sources") or None,
        "exclude_schools": resolved.get("excluded_schools") or None,
        "filter_type": resolved.get("type"),
    }
    for selector in (
        "filter_schools",
        "filter_sources",
        "exclude_schools",
        "filter_type",
    ):
        if not _accepts_parameter(signature, selector):
            if kwargs[selector] is not None:
                raise RetrieverAdapterError(
                    f"Retriever.search() cannot apply required selector {selector}"
                )
            kwargs.pop(selector)

    if _accepts_parameter(signature, "search_mode"):
        kwargs["search_mode"] = search_mode
        if _accepts_parameter(signature, "exact_match"):
            kwargs["exact_match"] = True
    elif search_mode == "semantic" and _accepts_parameter(signature, "exact_match"):
        kwargs["exact_match"] = False
    elif search_mode == "auto":
        # The pre-search_mode API's default semantic search plus exact alias
        # promotion is its only honest approximation of auto.
        pass
    else:
        raise RetrieverAdapterError(
            f"search_mode={search_mode!r} requires Retriever.search(search_mode=...); "
            "the installed Retriever exposes only the legacy API"
        )

    if max_per_canonical is not None:
        if not _accepts_parameter(signature, "max_per_canonical"):
            raise RetrieverAdapterError(
                "--max-per-canonical requires a Retriever API with that parameter"
            )
        kwargs["max_per_canonical"] = max_per_canonical

    if _accepts_parameter(signature, "strict_scope"):
        kwargs["strict_scope"] = False
    return list(search(**kwargs) or [])


def _result_view(result: Any) -> dict[str, str]:
    if isinstance(result, Mapping):
        metadata = result.get("metadata") or {}
        getter = result.get
    else:
        metadata = getattr(result, "metadata", {}) or {}
        getter = lambda name, default=None: getattr(result, name, default)
    if not isinstance(metadata, Mapping):
        metadata = {}
    record_id = str(
        getter("record_id")
        or getter("id")
        or getter("card_id")
        or metadata.get("record_id")
        or metadata.get("evidence_id")
        or ""
    ).strip()
    if not record_id:
        raise RetrieverAdapterError("Retriever result has no record/card id")
    return {
        "record_id": record_id,
        "canonical_id": str(
            metadata.get("canonical_id")
            or metadata.get("card_id")
            or getter("canonical_id")
            or record_id
        ),
        "school": str(metadata.get("school") or getter("school") or ""),
        "source": str(metadata.get("source") or getter("source") or ""),
        "type": str(metadata.get("type") or getter("card_type") or ""),
    }


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 6)


def effective_max_per_canonical(value: int | None, layer: str) -> int | None:
    """Mirror Retriever's public diversity defaults for reproducible reports."""
    if value == 0:
        return None
    if value is None:
        return None if layer == "canonical" else 2
    return value


def _mean(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return round(statistics.fmean(materialized), 6) if materialized else None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[position], 3)


def _positive_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "queries": len(rows),
        "recall_at_1": _mean(float(row["recall_at_1"]) for row in rows),
        "recall_at_5": _mean(float(row["recall_at_5"]) for row in rows),
        "mrr": _mean(float(row["reciprocal_rank"]) for row in rows),
    }


def evaluate_cases(
    cases: Sequence[Mapping[str, Any]],
    retriever_factory: Callable[[str], Any],
    *,
    top_k: int = 5,
    search_mode: str = "auto",
    max_per_canonical: int | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Evaluate retrieval and return aggregate metrics plus auditable rows."""
    if top_k < 5:
        raise ValueError("top_k must be at least 5 to compute Recall@5")
    if search_mode not in SEARCH_MODES:
        raise ValueError(f"unsupported search_mode: {search_mode}")
    retrievers: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    latency_values: list[float] = []
    scope_good = scope_total = 0
    source_good = source_total = 0
    duplicate_count = result_count = 0
    exact_match_by_layer: dict[str, bool | None] = {}

    for case in cases:
        started = clock()
        results: list[Any] = []
        error: Exception | None = None
        error_kind: str | None = None
        execution: dict[str, Any] = {}
        layer = str(case["layer"])
        try:
            if layer not in retrievers:
                retrievers[layer] = retriever_factory(layer)
            results = search_with_adapter(
                retrievers[layer],
                case,
                top_k=top_k,
                search_mode=search_mode,
                max_per_canonical=max_per_canonical,
                execution=execution,
            )
            views = [_result_view(result) for result in results]
        except RetrievalScopeError as exc:
            views = []
            error = exc
            error_kind = "scope_error"
        except RetrieverAdapterError as exc:
            views = []
            error = exc
            error_kind = "adapter_error"
        except Exception as exc:  # noqa: BLE001 - each query must remain auditable
            views = []
            error = exc
            error_kind = "runtime_error"
        if "exact_match" in execution:
            exact_match_by_layer[layer] = execution["exact_match"]
        elapsed_ms = max(0.0, (clock() - started) * 1000.0)
        latency_values.append(elapsed_ms)
        expected = case["expected"]
        row: dict[str, Any] = {
            "id": case["id"],
            "layer": case["layer"],
            "language": case["language"],
            "query_kind": case["query_kind"],
            "latency_ms": round(elapsed_ms, 3),
            "returned_record_ids": [view["record_id"] for view in views],
        }
        if error is not None:
            row["error_kind"] = error_kind
            row["error"] = str(error)

        if expected["outcome"] == "fail_closed":
            passed = error_kind == "scope_error"
            row.update({
                "status": "fail_closed_pass" if passed else "fail_closed_fail",
                "fail_closed_pass": passed,
                "expected_scope_category": expected["reason"],
            })
            rows.append(row)
            continue

        relevant = set(expected["record_ids"])
        ranks = [
            rank
            for rank, view in enumerate(views, 1)
            if view["record_id"] in relevant
        ]
        recall_1 = len(
            relevant.intersection(view["record_id"] for view in views[:1])
        ) / len(relevant)
        recall_5 = len(
            relevant.intersection(view["record_id"] for view in views[:5])
        ) / len(relevant)
        reciprocal_rank = 1.0 / min(ranks) if ranks else 0.0
        status = "hit" if ranks else "miss"
        if error_kind:
            status = error_kind
        scope = _scope(case)
        scoped = any((scope["schools"], scope["exclude_schools"], scope["type"]))
        if scoped:
            school_scope = {**scope, "sources": []}
            for view in views:
                scope_total += 1
                scope_good += int(_record_matches_scope(view, school_scope))
        if scope["sources"]:
            for view in views:
                source_total += 1
                source_good += int(view["source"] in scope["sources"])
        canonical_ids = [view["canonical_id"] for view in views]
        duplicate_count += len(canonical_ids) - len(set(canonical_ids))
        result_count += len(canonical_ids)
        row.update({
            "status": status,
            "relevant_ranks": ranks,
            "recall_at_1": round(recall_1, 6),
            "recall_at_5": round(recall_5, 6),
            "reciprocal_rank": round(reciprocal_rank, 6),
        })
        rows.append(row)

    positive_rows = [row for row in rows if "recall_at_1" in row]
    negative_rows = [row for row in rows if "fail_closed_pass" in row]
    breakdown: dict[str, dict[str, Any]] = {}
    case_by_id = {str(case["id"]): case for case in cases}
    for dimension in ("layer", "language", "query_kind"):
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in positive_rows:
            grouped[str(row[dimension])].append(row)
        breakdown[dimension] = {
            key: _positive_metrics(value) for key, value in sorted(grouped.items())
        }
    type_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in positive_rows:
        type_groups[str(_scope(case_by_id[str(row["id"])])["type"])].append(row)
    breakdown["type"] = {
        key: _positive_metrics(value) for key, value in sorted(type_groups.items())
    }

    fail_by_reason: dict[str, dict[str, Any]] = {}
    for reason in NEGATIVE_REASONS:
        selected = [
            row
            for row in negative_rows
            if row["expected_scope_category"] == reason
        ]
        if selected:
            passed = sum(bool(row["fail_closed_pass"]) for row in selected)
            fail_by_reason[reason] = {
                "passed": passed,
                "total": len(selected),
                "rate": _ratio(passed, len(selected)),
            }
    fail_passed = sum(bool(row["fail_closed_pass"]) for row in negative_rows)
    adapter_errors = sum(row.get("error_kind") == "adapter_error" for row in rows)
    runtime_errors = sum(row.get("error_kind") == "runtime_error" for row in rows)
    exact_values = set(exact_match_by_layer.values())
    aggregate_exact_match = (
        next(iter(exact_values)) if len(exact_values) == 1 else None
    )
    return {
        "metrics": {
            **_positive_metrics(positive_rows),
            "scope_purity": _ratio(scope_good, scope_total),
            "scope_results": scope_total,
            "source_purity": _ratio(source_good, source_total),
            "source_results": source_total,
            "fail_closed": {
                "passed": fail_passed,
                "total": len(negative_rows),
                "rate": _ratio(fail_passed, len(negative_rows)),
                "by_expected_scope_category": fail_by_reason,
            },
            "duplicate_canonical_ratio": _ratio(duplicate_count, result_count),
            "returned_results": result_count,
            "adapter_errors": adapter_errors,
            "runtime_errors": runtime_errors,
            "latency_ms": {
                "queries": len(latency_values),
                "mean": _mean(latency_values),
                "p50": _percentile(latency_values, 0.50),
                "p95": _percentile(latency_values, 0.95),
                "max": round(max(latency_values), 3) if latency_values else None,
            },
        },
        "execution": {
            "exact_match": aggregate_exact_match,
            "exact_match_by_layer": {
                layer: exact_match_by_layer[layer]
                for layer in LAYERS
                if layer in exact_match_by_layer
            },
        },
        "breakdown": breakdown,
        "cases": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and run the tracked OpenMobius retrieval benchmark"
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument(
        "--search-mode", choices=SEARCH_MODES, default="auto",
        help="Retriever mode; legacy APIs fail clearly when the mode cannot be represented",
    )
    parser.add_argument("--embedder", choices=("local", "openai"), default="local")
    parser.add_argument("-k", "--top-k", type=int, default=5)
    parser.add_argument(
        "--max-per-canonical",
        type=int,
        default=None,
        help="Pass a diversification limit to compatible Retriever versions; 0 disables it",
    )
    parser.add_argument("--limit", type=int, default=None, help="Evaluate the first N cases")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit per-case rows; suitable for a tracked baseline report",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser


def _emit_report(report: Mapping[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.top_k < 5:
        parser.error("--top-k must be at least 5 so Recall@5 is defined")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.max_per_canonical is not None and args.max_per_canonical < 0:
        parser.error("--max-per-canonical cannot be negative")
    try:
        cases, digest = load_benchmark(args.dataset)
        distribution = validate_benchmark_against_kb(cases, args.kb)
    except BenchmarkValidationError as exc:
        print(f"benchmark validation failed: {exc}", file=sys.stderr)
        return 2

    base_report: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "dataset": _display_path(args.dataset),
        "dataset_sha256": digest,
        "kb": _display_path(args.kb),
        "distribution": distribution,
        "index": index_provenance(args.kb),
    }
    if args.validate_only:
        base_report["status"] = "valid"
        _emit_report(base_report, args.output)
        return 0

    selected = cases[: args.limit] if args.limit is not None else cases
    selected_layers = {str(case["layer"]) for case in selected}
    providers_by_layer = required_query_embedding_providers(
        selected_layers,
        search_mode=args.search_mode,
        v2_provider=args.embedder,
    )
    try:
        query_embedding_identities = validate_query_embedding_compatibility(
            base_report["index"], providers_by_layer
        )
        providers_needed = {
            provider for provider in providers_by_layer.values() if provider is not None
        }
        embedder_cache: dict[str, Any] = {}
        if providers_needed:
            from _lib.embedder import get_embedder  # noqa: PLC0415

            for provider in sorted(providers_needed):
                embedder = get_embedder(provider)
                expected = QUERY_EMBEDDING_PROVIDERS[provider]
                actual_model = getattr(embedder, "model_name", None)
                actual_dimension = getattr(embedder, "dim", None)
                if (
                    actual_model != expected["model"]
                    or actual_dimension != expected["dimension"]
                ):
                    raise EvaluationSetupError(
                        f"loaded provider={provider!r} has model={actual_model!r}, "
                        f"dimension={actual_dimension!r}; expected "
                        f"model={expected['model']!r}, dimension={expected['dimension']}"
                    )
                embedder_cache[provider] = embedder
        from _lib.retriever import Retriever  # noqa: PLC0415
    except EvaluationSetupError as exc:
        print(f"evaluation setup failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - dependency boundary
        print(f"evaluation setup failed: {exc}", file=sys.stderr)
        return 1

    retriever_cache: dict[str, Any] = {}

    def factory(layer: str) -> Any:
        if layer not in retriever_cache:
            provider = providers_by_layer[layer]
            embedder = embedder_cache.get(provider) if provider is not None else None
            retriever_cache[layer] = Retriever(args.kb, embedder, layer=layer)
        return retriever_cache[layer]

    evaluated = evaluate_cases(
        selected,
        factory,
        top_k=args.top_k,
        search_mode=args.search_mode,
        max_per_canonical=args.max_per_canonical,
    )
    if args.summary_only:
        evaluated.pop("cases", None)
    execution = evaluated.pop("execution")
    base_report.update({
        "status": "evaluated",
        "config": {
            "search_mode": args.search_mode,
            "effective_search_modes": {
                layer: resolve_search_mode(args.search_mode, layer)
                for layer in LAYERS
            },
            "embedder": args.embedder if providers_needed else None,
            "query_embeddings": {
                layer: query_embedding_identities[layer]
                for layer in LAYERS
                if layer in query_embedding_identities
            },
            "top_k": args.top_k,
            "max_per_canonical": args.max_per_canonical,
            "effective_max_per_canonical": {
                layer: effective_max_per_canonical(
                    args.max_per_canonical, layer
                )
                for layer in LAYERS
            },
            "exact_match": execution["exact_match"],
            "exact_match_by_layer": execution["exact_match_by_layer"],
            "evaluated_cases": len(selected),
        },
        **evaluated,
    })
    _emit_report(base_report, args.output)
    if evaluated["metrics"]["adapter_errors"]:
        return 2
    if evaluated["metrics"]["runtime_errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
