"""Deterministic School/evidence projections for the OpenMobius knowledge base.

The legacy concept cards are canonical, cross-project cards.  Some of them
fuse material from more than one School, so their top-level synthesized rules
cannot safely be assigned to a selected School.  This module deliberately
uses a conservative policy:

* ``definition_per_source`` entries become source evidence only when their
  key matches source-card metadata exactly and resolves to one project and
  one School.
* top-level concept fields enter a School projection only when every explicit
  source School agrees with the card School, or when the card uses the
  dedicated card-School-only legacy shape (currently ChanLun).
* top-level concept fields become source evidence only when source metadata
  is complete and resolves to one exact project collection.
* case fields require both an explicit School and ``project_origin``.

No fuzzy matching, text classification, or model inference is performed.
Skipped content is counted by reason so a later rebuild from raw source
materials can close the gaps without treating guesses as provenance.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 2
EMBEDDING_STRATEGY = "native_document"
SCHOOL_COLLECTION = "school_knowledge_v2"
EVIDENCE_COLLECTION = "source_evidence_v2"

_CONCEPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("definition", "definition"),
    ("identification_rules", "identification_rule"),
    ("common_mistakes", "common_mistake"),
    ("trading_implication", "trading_implication"),
)
_CASE_FIELDS: tuple[tuple[str, str], ...] = (
    ("market_context", "market_context"),
    ("key_observation", "key_observation"),
    ("analysis_steps", "analysis_step"),
    ("outcome", "outcome"),
    ("lessons", "lesson"),
)


def _clean_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unique_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = _clean_string(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _iter_field_content(card: Mapping[str, Any], field_name: str) -> Iterator[tuple[str, str]]:
    """Yield ``(content, JSON pointer)`` for a scalar or list field."""
    value = card.get(field_name)
    if isinstance(value, str):
        content = value.strip()
        if content:
            yield content, f"/{field_name}"
        return
    if not isinstance(value, list):
        return
    for index, item in enumerate(value):
        content = _clean_string(item)
        if content:
            yield content, f"/{field_name}/{index}"


@dataclass(frozen=True)
class SchoolDefinition:
    id: str
    name: str
    aliases: tuple[str, ...]
    kind: str
    availability: str
    knowledge_qna: bool
    native_market_analyzer: str | None


@dataclass(frozen=True)
class SchoolRegistry:
    registry_version: int
    schools: tuple[SchoolDefinition, ...]
    default_profile: Mapping[str, Any]
    _alias_map: Mapping[str, str] = field(repr=False)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(school.name for school in self.schools)

    def resolve(self, value: str) -> str | None:
        """Resolve a canonical name or declared alias; unknowns fail closed."""
        normalized = " ".join(value.strip().split()).casefold()
        return self._alias_map.get(normalized)


def load_school_registry(kb_dir: Path) -> SchoolRegistry:
    """Load and structurally validate ``knowledge_base/schools.json``.

    JSON is used instead of YAML so installation gains no new runtime
    dependency.
    """
    path = Path(kb_dir) / "schools.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"School registry must be a regular file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"School registry root must be an object: {path}")
    registry_version = data.get("registry_version")
    if (
        isinstance(registry_version, bool)
        or not isinstance(registry_version, int)
        or registry_version != 1
    ):
        raise ValueError(f"unsupported School registry version in {path}")
    raw_schools = data.get("schools")
    if not isinstance(raw_schools, list) or not raw_schools:
        raise ValueError(f"School registry has no schools: {path}")

    definitions: list[SchoolDefinition] = []
    alias_map: dict[str, str] = {}
    ids: set[str] = set()
    names: set[str] = set()
    for raw in raw_schools:
        if not isinstance(raw, dict):
            raise ValueError("each School registry entry must be an object")
        school_id = _clean_string(raw.get("id"))
        name = _clean_string(raw.get("name"))
        raw_aliases = raw.get("aliases")
        if not isinstance(raw_aliases, list) or any(
            not isinstance(alias, str) or not alias.strip()
            for alias in raw_aliases
        ):
            raise ValueError(f"School aliases must be a string list: {name or school_id}")
        aliases = tuple(_unique_strings(raw_aliases))
        kind = _clean_string(raw.get("kind"))
        availability = _clean_string(raw.get("availability"))
        if not school_id or not name:
            raise ValueError("School registry entries require non-empty id/name")
        if school_id in ids or name in names:
            raise ValueError(f"duplicate School id/name: {school_id}/{name}")
        if kind not in {"analysis_lens", "knowledge_category"}:
            raise ValueError(f"invalid School kind for {name}: {kind}")
        if availability not in {"top_level", "evidence_only"}:
            raise ValueError(f"invalid School availability for {name}: {availability}")
        knowledge_qna = raw.get("knowledge_qna")
        if not isinstance(knowledge_qna, bool):
            raise ValueError(f"School knowledge_qna must be boolean: {name}")
        raw_analyzer = raw.get("native_market_analyzer")
        if raw_analyzer is not None and (
            not isinstance(raw_analyzer, str) or not raw_analyzer.strip()
        ):
            raise ValueError(
                f"School native_market_analyzer must be null or a string: {name}"
            )

        definition = SchoolDefinition(
            id=school_id,
            name=name,
            aliases=aliases,
            kind=kind,
            availability=availability,
            knowledge_qna=knowledge_qna,
            native_market_analyzer=(
                _clean_string(raw_analyzer) or None
            ),
        )
        definitions.append(definition)
        ids.add(school_id)
        names.add(name)
        for alias in (name, *aliases):
            normalized = " ".join(alias.strip().split()).casefold()
            previous = alias_map.get(normalized)
            if previous and previous != name:
                raise ValueError(
                    f"School alias {alias!r} maps to both {previous!r} and {name!r}"
                )
            alias_map[normalized] = name

    default_profile = data.get("default_profile")
    if not isinstance(default_profile, dict):
        raise ValueError("School registry default_profile must be an object")
    default_schools = default_profile.get("schools")
    if not isinstance(default_schools, list) or any(
        not isinstance(school, str) or not school.strip()
        for school in default_schools
    ):
        raise ValueError("School registry default_profile.schools must be a string list")
    unknown_defaults = set(default_schools) - names
    if unknown_defaults:
        raise ValueError(f"default profile references unknown Schools: {unknown_defaults}")
    return SchoolRegistry(
        registry_version=1,
        schools=tuple(definitions),
        default_profile=default_profile,
        _alias_map=alias_map,
    )


@dataclass(frozen=True)
class _SourceCard:
    project: str
    school: str
    card_id: str
    canonical_term: str


@dataclass(frozen=True)
class _DefinitionMatch:
    source: str
    school: str
    material_ids: tuple[str, ...]
    strategy: str


def _source_cards(card: Mapping[str, Any]) -> list[_SourceCard]:
    result: list[_SourceCard] = []
    for raw in card.get("source_cards") or []:
        if not isinstance(raw, dict):
            continue
        result.append(
            _SourceCard(
                project=_clean_string(raw.get("project")),
                school=_clean_string(raw.get("source_school")),
                card_id=(
                    _clean_string(raw.get("card_id"))
                    or _clean_string(raw.get("original_card_id"))
                    or _clean_string(raw.get("video_id"))
                ),
                canonical_term=_clean_string(raw.get("source_canonical_term")),
            )
        )
    return result


def _finish_definition_match(
    candidates: Sequence[_SourceCard], strategy: str
) -> tuple[_DefinitionMatch | None, str | None]:
    if not candidates:
        return None, "unmapped_definition_key"
    if any(not item.project or not item.school for item in candidates):
        return None, "missing_source_provenance"
    projects = {item.project for item in candidates}
    schools = {item.school for item in candidates}
    if len(projects) != 1 or len(schools) != 1:
        return None, "ambiguous_project_school"
    return (
        _DefinitionMatch(
            source=next(iter(projects)),
            school=next(iter(schools)),
            material_ids=tuple(sorted({item.card_id for item in candidates if item.card_id})),
            strategy=strategy,
        ),
        None,
    )


def _match_definition_source(
    key: str,
    source_cards: Sequence[_SourceCard],
    registry: SchoolRegistry,
) -> tuple[_DefinitionMatch | None, str | None]:
    """Map a definition label using exact metadata-derived labels only."""
    exact_project = [item for item in source_cards if key == item.project]
    if exact_project:
        return _finish_definition_match(exact_project, "project")

    labelled: list[tuple[_SourceCard, str]] = []
    for item in source_cards:
        if not item.project:
            continue
        labels: list[tuple[str, str]] = []
        if item.school and item.school in registry.names:
            labels.extend(
                (
                    (f"{item.project} ({item.school})", "project_and_school"),
                    (f"{item.project} [{item.school}]", "project_and_school"),
                )
            )
        if item.card_id:
            labels.extend(
                (
                    (f"{item.project} ({item.card_id})", "project_and_card_id"),
                    (f"{item.project}/{item.card_id}", "project_and_card_id"),
                    (f"{item.project} [{item.card_id}]", "project_and_card_id"),
                )
            )
        if item.card_id and item.canonical_term:
            labels.append(
                (
                    f"{item.project} ({item.card_id}·{item.canonical_term})",
                    "project_card_id_and_canonical_term",
                )
            )
        if item.canonical_term:
            labels.extend(
                (
                    (
                        f"{item.project} ({item.canonical_term})",
                        "project_and_canonical_term",
                    ),
                    (
                        f"{item.project} [{item.canonical_term}]",
                        "project_and_canonical_term",
                    ),
                )
            )
        for label, strategy in labels:
            if key == label:
                labelled.append((item, strategy))

    if not labelled:
        if source_cards and any(not item.project or not item.school for item in source_cards):
            return None, "missing_source_provenance"
        return None, "unmapped_definition_key"
    strategies = {strategy for _, strategy in labelled}
    strategy = sorted(strategies)[0]
    return _finish_definition_match([item for item, _ in labelled], strategy)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    packed = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(packed.encode('utf-8')).hexdigest()[:24]}"


def _evidence_search_text(payload: Mapping[str, Any], aliases: Sequence[str]) -> str:
    parts = [
        f"Term: {payload['canonical_term']}",
        f"School: {payload['school']}",
        f"Source: {payload['source']}",
        f"Content type: {payload['content_type']}",
    ]
    if aliases:
        parts.insert(1, f"Aliases: {', '.join(aliases)}")
    parts.append(str(payload["content"]))
    return "\n".join(parts)


def _make_evidence_payload(
    *,
    canonical_id: str,
    canonical_term: str,
    card_type: str,
    school: str,
    source: str,
    content_type: str,
    content: str,
    file_path: str,
    ref: str,
    source_material_ids: Sequence[str],
    attribution_kind: str,
    match_key: str | None,
    match_strategy: str,
    aliases: Sequence[str],
    review_status: Any,
    extraction_confidence: Any,
) -> dict[str, Any]:
    evidence_id = _stable_id(
        "ev",
        canonical_id,
        school,
        source,
        content_type,
        file_path,
        ref,
        content,
    )
    attribution: dict[str, Any] = {
        "kind": attribution_kind,
        "confidence": "exact",
        "match_strategy": match_strategy,
    }
    if match_key:
        attribution["match_key"] = match_key
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "layer": "source_evidence",
        "id": evidence_id,
        "evidence_id": evidence_id,
        "canonical_id": canonical_id,
        "canonical_term": canonical_term,
        "type": card_type,
        "school": school,
        "source": source,
        "content_type": content_type,
        "content": content,
        "content_hash": _content_hash(content),
        "file_path": file_path,
        "ref": ref,
        "source_material_ids": sorted(set(source_material_ids)),
        "attribution": attribution,
        "review_status": review_status,
        "extraction_confidence": extraction_confidence,
        "embedding_strategy": EMBEDDING_STRATEGY,
    }
    payload["search_text"] = _evidence_search_text(payload, aliases)
    return payload


def _wrap_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "layer": "evidence",
        "evidence_id": payload["evidence_id"],
        "record_id": payload["id"],
        "canonical_id": payload["canonical_id"],
        "type": payload["type"],
        "term": payload["canonical_term"],
        "school": payload["school"],
        "source": payload["source"],
        "content_type": payload["content_type"],
        "file_path": payload["file_path"],
        "ref": payload["ref"],
        "embedding_strategy": EMBEDDING_STRATEGY,
    }
    return {
        "id": payload["id"],
        "document": payload["search_text"],
        "metadata": metadata,
        "payload": dict(payload),
    }


@dataclass
class _ProjectionAccumulator:
    canonical_id: str
    canonical_term: str
    card_type: str
    school: str
    aliases: list[str]
    file_path: str
    review_status: Any
    strategy: str = "source_definitions_only"
    school_attribution: str = "explicit_source_mapping"
    content_by_type: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    evidence_ids: set[str] = field(default_factory=set)
    source_names: set[str] = field(default_factory=set)
    skipped_content_count: int = 0

    def add_content(
        self,
        *,
        content_type: str,
        content: str,
        ref: str,
        attribution_level: str,
        evidence_id: str | None = None,
        source: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "content": content,
            "ref": ref,
            "attribution_level": attribution_level,
        }
        if evidence_id:
            item["evidence_id"] = evidence_id
            self.evidence_ids.add(evidence_id)
        if source:
            item["source"] = source
            self.source_names.add(source)
        self.content_by_type[content_type].append(item)


def _projection_search_text(payload: Mapping[str, Any]) -> str:
    parts = [f"Term: {payload['canonical_term']}"]
    aliases = payload.get("aliases") or []
    if aliases:
        parts.append(f"Aliases: {', '.join(aliases)}")
    parts.append(f"School: {payload['school']}")
    sources = payload.get("source_names") or []
    if sources:
        parts.append(f"Sources: {', '.join(sources)}")
    labels = {
        "definition": "Definition",
        "identification_rule": "Identification rule",
        "common_mistake": "Common mistake",
        "trading_implication": "Trading implication",
        "market_context": "Market context",
        "key_observation": "Key observation",
        "analysis_step": "Analysis step",
        "outcome": "Outcome",
        "lesson": "Lesson",
    }
    for content_type, items in payload.get("content_by_type", {}).items():
        label = labels.get(content_type, content_type)
        for item in items:
            parts.append(f"{label}: {item['content']}")
    return "\n".join(parts)


def _finish_projection(accumulator: _ProjectionAccumulator) -> dict[str, Any]:
    record_id = _stable_id(
        "sp", accumulator.card_type, accumulator.canonical_id, accumulator.school
    )
    content_by_type = {
        key: value
        for key, value in sorted(accumulator.content_by_type.items())
        if value
    }
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "layer": "school_projection",
        "id": record_id,
        "record_id": record_id,
        "canonical_id": accumulator.canonical_id,
        "canonical_term": accumulator.canonical_term,
        "type": accumulator.card_type,
        "school": accumulator.school,
        "aliases": accumulator.aliases,
        "content_by_type": content_by_type,
        "evidence_ids": sorted(accumulator.evidence_ids),
        "source_names": sorted(accumulator.source_names),
        "source_collection_count": len(accumulator.source_names),
        "file_path": accumulator.file_path,
        "derivation": {
            "strategy": accumulator.strategy,
            "school_attribution": accumulator.school_attribution,
            "skipped_content_count": accumulator.skipped_content_count,
        },
        "review_status": accumulator.review_status,
        "embedding_strategy": EMBEDDING_STRATEGY,
    }
    payload["search_text"] = _projection_search_text(payload)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "layer": "school",
        "record_id": record_id,
        "canonical_id": accumulator.canonical_id,
        "type": accumulator.card_type,
        "term": accumulator.canonical_term,
        "school": accumulator.school,
        "file_path": accumulator.file_path,
        "source_names": json.dumps(
            payload["source_names"], ensure_ascii=False, separators=(",", ":")
        ),
        "source_collection_count": payload["source_collection_count"],
        "embedding_strategy": EMBEDDING_STRATEGY,
    }
    return {
        "id": record_id,
        "document": payload["search_text"],
        "metadata": metadata,
        "payload": payload,
    }


@dataclass(frozen=True)
class BuildResult:
    school_records: tuple[dict[str, Any], ...]
    evidence_records: tuple[dict[str, Any], ...]
    stats: Mapping[str, Any]
    input_fingerprint: str
    registry_version: int

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "registry_version": self.registry_version,
            "embedding_strategy": EMBEDDING_STRATEGY,
            "input_fingerprint": self.input_fingerprint,
            "collections": {
                SCHOOL_COLLECTION: len(self.school_records),
                EVIDENCE_COLLECTION: len(self.evidence_records),
            },
            "stats": self.stats,
        }


def _get_projection(
    projections: dict[tuple[str, str, str], _ProjectionAccumulator],
    *,
    canonical_id: str,
    canonical_term: str,
    card_type: str,
    school: str,
    aliases: Sequence[str],
    file_path: str,
    review_status: Any,
) -> _ProjectionAccumulator:
    key = (card_type, canonical_id, school)
    if key not in projections:
        projections[key] = _ProjectionAccumulator(
            canonical_id=canonical_id,
            canonical_term=canonical_term,
            card_type=card_type,
            school=school,
            aliases=list(aliases),
            file_path=file_path,
            review_status=review_status,
        )
    return projections[key]


def _add_evidence_to_projection(
    accumulator: _ProjectionAccumulator,
    payload: Mapping[str, Any],
) -> None:
    accumulator.add_content(
        content_type=str(payload["content_type"]),
        content=str(payload["content"]),
        ref=str(payload["ref"]),
        attribution_level="source",
        evidence_id=str(payload["evidence_id"]),
        source=str(payload["source"]),
    )


def _card_identity(card: Mapping[str, Any], fallback: str, card_type: str) -> tuple[str, str]:
    canonical_id = (
        _clean_string(card.get("global_card_id"))
        or _clean_string(card.get("card_id"))
        or fallback
    )
    if card_type == "concept":
        term = (
            _clean_string(card.get("canonical_term"))
            or _clean_string(card.get("global_canonical"))
            or canonical_id
        )
    else:
        term = _clean_string(card.get("title")) or canonical_id
    return canonical_id, term


def _top_level_concept_scope(
    card: Mapping[str, Any], source_cards: Sequence[_SourceCard]
) -> tuple[bool, str, list[str], bool]:
    """Return projection safety, attribution label, sources, evidence safety."""
    primary_school = _clean_string(card.get("school"))
    if not primary_school or not source_cards:
        return False, "", [], False
    explicit_schools = {item.school for item in source_cards if item.school}
    missing_school = any(not item.school for item in source_cards)
    projects = _unique_strings(
        [item.project for item in source_cards] + [card.get("project_origin")]
    )
    missing_project = any(not item.project for item in source_cards)

    if not missing_school and explicit_schools == {primary_school}:
        evidence_safe = not missing_project and len(projects) == 1
        return True, "card_and_sources_agree", projects, evidence_safe

    # The dedicated ChanLun import schema records the School on the card while
    # each source card only carries video metadata.  This is enough for a
    # School projection, but intentionally not source evidence.  Do not make
    # project_origin a requirement: older cards omit it as well.
    if (
        primary_school == "缠论"
        and not explicit_schools
        and all(not item.project for item in source_cards)
    ):
        return True, "card_school_only", projects, False
    return False, "", projects, False


def _source_material_ids(source_cards: Sequence[_SourceCard]) -> list[str]:
    return sorted({item.card_id for item in source_cards if item.card_id})


def _build_concept(
    *,
    card: Mapping[str, Any],
    stem: str,
    file_path: str,
    registry: SchoolRegistry,
    projections: dict[tuple[str, str, str], _ProjectionAccumulator],
    evidence_records: list[dict[str, Any]],
    inputs: Counter[str],
    emitted: Counter[str],
    skipped: Counter[str],
) -> None:
    inputs["concepts"] += 1
    canonical_id, canonical_term = _card_identity(card, stem, "concept")
    aliases = _unique_strings(card.get("aliases") or [])
    source_cards = _source_cards(card)
    inputs["concept_source_cards"] += len(source_cards)
    review_status = card.get("review_status")

    definition_map = card.get("definition_per_source") or {}
    if not isinstance(definition_map, dict):
        definition_map = {}
    for raw_key, raw_content in sorted(definition_map.items()):
        key = _clean_string(raw_key)
        content = _clean_string(raw_content)
        if not key or not content:
            skipped["empty_definition_per_source"] += 1
            continue
        inputs["definition_per_source"] += 1
        match, reason = _match_definition_source(key, source_cards, registry)
        if match is None:
            skipped[reason or "unmapped_definition_key"] += 1
            continue
        if match.school not in registry.names:
            skipped["unknown_school"] += 1
            continue
        ref = f"/definition_per_source/{_pointer_token(key)}"
        payload = _make_evidence_payload(
            canonical_id=canonical_id,
            canonical_term=canonical_term,
            card_type="concept",
            school=match.school,
            source=match.source,
            content_type="definition",
            content=content,
            file_path=file_path,
            ref=ref,
            source_material_ids=match.material_ids,
            attribution_kind="definition_per_source",
            match_key=key,
            match_strategy=match.strategy,
            aliases=aliases,
            review_status=review_status,
            extraction_confidence=None,
        )
        evidence_records.append(_wrap_evidence(payload))
        emitted["definition_source_evidence"] += 1
        emitted[f"definition_match_{match.strategy}"] += 1
        accumulator = _get_projection(
            projections,
            canonical_id=canonical_id,
            canonical_term=canonical_term,
            card_type="concept",
            school=match.school,
            aliases=aliases,
            file_path=file_path,
            review_status=review_status,
        )
        _add_evidence_to_projection(accumulator, payload)

    projection_safe, attribution, source_names, evidence_safe = _top_level_concept_scope(
        card, source_cards
    )
    primary_school = _clean_string(card.get("school"))
    top_pieces: list[tuple[str, str, str]] = []
    for field_name, content_type in _CONCEPT_FIELDS:
        for content, ref in _iter_field_content(card, field_name):
            top_pieces.append((content_type, content, ref))
            inputs["concept_top_level_content"] += 1

    if not projection_safe or primary_school not in registry.names:
        if primary_school and primary_school not in registry.names:
            skipped["unknown_school"] += len(top_pieces)
        else:
            for content_type, _, _ in top_pieces:
                if content_type == "definition":
                    skipped["fused_definition_not_attributable"] += 1
                else:
                    skipped["fused_rule_not_attributable"] += 1
        for accumulator in projections.values():
            if accumulator.card_type == "concept" and accumulator.canonical_id == canonical_id:
                accumulator.skipped_content_count += len(top_pieces)
        return

    accumulator = _get_projection(
        projections,
        canonical_id=canonical_id,
        canonical_term=canonical_term,
        card_type="concept",
        school=primary_school,
        aliases=aliases,
        file_path=file_path,
        review_status=review_status,
    )
    accumulator.strategy = "single_school_card"
    accumulator.school_attribution = attribution
    accumulator.source_names.update(source_names)
    source = source_names[0] if evidence_safe else None
    if not evidence_safe:
        reason = (
            "missing_source_provenance"
            if any(not item.project or not item.school for item in source_cards)
            else "non_atomic_source_provenance"
        )
        skipped[reason] += len(top_pieces)

    for content_type, content, ref in top_pieces:
        if source:
            payload = _make_evidence_payload(
                canonical_id=canonical_id,
                canonical_term=canonical_term,
                card_type="concept",
                school=primary_school,
                source=source,
                content_type=content_type,
                content=content,
                file_path=file_path,
                ref=ref,
                source_material_ids=_source_material_ids(source_cards),
                attribution_kind="single_school_single_source",
                match_key=None,
                match_strategy="project",
                aliases=aliases,
                review_status=review_status,
                extraction_confidence=None,
            )
            evidence_records.append(_wrap_evidence(payload))
            emitted["concept_top_level_source_evidence"] += 1
            _add_evidence_to_projection(accumulator, payload)
        else:
            accumulator.add_content(
                content_type=content_type,
                content=content,
                ref=ref,
                attribution_level="school",
            )
            emitted["concept_school_only_content"] += 1


def _case_material_ids(card: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for source in card.get("sources") or []:
        if not isinstance(source, dict):
            continue
        values.append(source.get("video_id"))
        values.extend(source.get("segment_ids") or [])
    return _unique_strings(values)


def _build_case(
    *,
    card: Mapping[str, Any],
    stem: str,
    file_path: str,
    registry: SchoolRegistry,
    projections: dict[tuple[str, str, str], _ProjectionAccumulator],
    evidence_records: list[dict[str, Any]],
    inputs: Counter[str],
    emitted: Counter[str],
    skipped: Counter[str],
) -> None:
    inputs["cases"] += 1
    canonical_id, canonical_term = _card_identity(card, stem, "case")
    school = _clean_string(card.get("school"))
    source = _clean_string(card.get("project_origin"))
    pieces: list[tuple[str, str, str]] = []
    for field_name, content_type in _CASE_FIELDS:
        for content, ref in _iter_field_content(card, field_name):
            pieces.append((content_type, content, ref))
            inputs["case_content"] += 1
    if not school or school not in registry.names:
        skipped["unknown_school"] += len(pieces)
        return
    if not source:
        skipped["missing_source_provenance"] += len(pieces)
        return

    accumulator = _get_projection(
        projections,
        canonical_id=canonical_id,
        canonical_term=canonical_term,
        card_type="case",
        school=school,
        aliases=[],
        file_path=file_path,
        review_status=card.get("review_status"),
    )
    accumulator.strategy = "case_card"
    accumulator.school_attribution = "direct_case"
    accumulator.source_names.add(source)
    material_ids = _case_material_ids(card)
    for content_type, content, ref in pieces:
        payload = _make_evidence_payload(
            canonical_id=canonical_id,
            canonical_term=canonical_term,
            card_type="case",
            school=school,
            source=source,
            content_type=content_type,
            content=content,
            file_path=file_path,
            ref=ref,
            source_material_ids=material_ids,
            attribution_kind="direct_case",
            match_key=source,
            match_strategy="case_project_origin",
            aliases=[],
            review_status=card.get("review_status"),
            extraction_confidence=card.get("extraction_confidence"),
        )
        evidence_records.append(_wrap_evidence(payload))
        emitted["case_source_evidence"] += 1
        _add_evidence_to_projection(accumulator, payload)


def _round_ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def build_v2_records(kb_dir: Path) -> BuildResult:
    """Build deterministic index-ready School and source-evidence records."""
    requested_kb_dir = Path(kb_dir)
    if requested_kb_dir.is_symlink() or not requested_kb_dir.is_dir():
        raise ValueError(
            f"knowledge base must be a regular directory, not a symlink: "
            f"{requested_kb_dir}"
        )
    kb_dir = requested_kb_dir.resolve()
    registry = load_school_registry(kb_dir)
    projections: dict[tuple[str, str, str], _ProjectionAccumulator] = {}
    evidence_records: list[dict[str, Any]] = []
    inputs: Counter[str] = Counter()
    emitted: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    card_identities: set[tuple[str, str]] = set()
    fingerprint = hashlib.sha256()
    registry_path = kb_dir / "schools.json"
    fingerprint.update(
        b"schools.json\0" + registry_path.read_bytes() + b"\0"
    )

    for card_type, directory in (("concept", "concepts"), ("case", "cases")):
        card_dir = kb_dir / directory
        if card_dir.is_symlink():
            raise ValueError(
                f"knowledge card directory must not be a symlink: {card_dir}"
            )
        if card_dir.exists() and not card_dir.is_dir():
            raise ValueError(f"knowledge card path is not a directory: {card_dir}")
        for path in sorted(card_dir.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise ValueError(
                    f"knowledge card must be a regular file, not a symlink: {path}"
                )
            file_mode = path.stat().st_mode
            if not stat.S_ISREG(file_mode) or not path.resolve().is_relative_to(kb_dir):
                raise ValueError(f"knowledge card escapes the knowledge base: {path}")
            relative = path.relative_to(kb_dir).as_posix()
            raw = path.read_bytes()
            fingerprint.update(relative.encode("utf-8") + b"\0" + raw + b"\0")
            try:
                card = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid {card_type} card {relative}: {exc}"
                ) from exc
            if not isinstance(card, dict):
                raise ValueError(
                    f"invalid {card_type} card {relative}: "
                    "top-level JSON value must be an object"
                )
            canonical_id, _canonical_term = _card_identity(
                card,
                path.stem,
                card_type,
            )
            identity = (card_type, canonical_id)
            if identity in card_identities:
                raise ValueError(
                    "duplicate canonical knowledge-card identity: "
                    f"type={card_type}, canonical_id={canonical_id}, file={relative}"
                )
            card_identities.add(identity)
            if card_type == "concept":
                _build_concept(
                    card=card,
                    stem=path.stem,
                    file_path=relative,
                    registry=registry,
                    projections=projections,
                    evidence_records=evidence_records,
                    inputs=inputs,
                    emitted=emitted,
                    skipped=skipped,
                )
            else:
                _build_case(
                    card=card,
                    stem=path.stem,
                    file_path=relative,
                    registry=registry,
                    projections=projections,
                    evidence_records=evidence_records,
                    inputs=inputs,
                    emitted=emitted,
                    skipped=skipped,
                )

    school_records = [_finish_projection(item) for item in projections.values()]
    school_records.sort(key=lambda record: record["id"])
    evidence_records.sort(key=lambda record: record["id"])
    emitted["school_projection_records"] = len(school_records)
    emitted["source_evidence_records"] = len(evidence_records)
    projected_schools = {
        record["metadata"]["school"] for record in school_records
    }
    registered_top_level = {
        school.name
        for school in registry.schools
        if school.availability == "top_level"
    }
    inputs["top_level_schools_observed"] = len(
        projected_schools.intersection(registered_top_level)
    )
    inputs["projected_school_labels"] = len(projected_schools)

    stats: dict[str, Any] = {
        "input": dict(sorted(inputs.items())),
        "emitted": dict(sorted(emitted.items())),
        "skipped": dict(sorted(skipped.items())),
        "coverage": {
            "definition_per_source_exact_evidence": _round_ratio(
                emitted["definition_source_evidence"], inputs["definition_per_source"]
            ),
            "concept_top_level_school_projection": _round_ratio(
                emitted["concept_top_level_source_evidence"]
                + emitted["concept_school_only_content"],
                inputs["concept_top_level_content"],
            ),
            "case_content_exact_evidence": _round_ratio(
                emitted["case_source_evidence"], inputs["case_content"]
            ),
        },
    }
    return BuildResult(
        school_records=tuple(school_records),
        evidence_records=tuple(evidence_records),
        stats=stats,
        input_fingerprint=fingerprint.hexdigest(),
        registry_version=registry.registry_version,
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_v2_artifacts(result: BuildResult, output_dir: Path) -> dict[str, Path]:
    """Write optional, deterministic JSONL artifacts (not required by index build)."""
    output_dir = Path(output_dir)
    school_path = output_dir / "school_knowledge_v2.jsonl"
    evidence_path = output_dir / "source_evidence_v2.jsonl"
    manifest_path = output_dir / "manifest.json"

    def encode(records: Sequence[Mapping[str, Any]]) -> str:
        return "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for record in records
        )

    _atomic_write_text(school_path, encode(result.school_records))
    _atomic_write_text(evidence_path, encode(result.evidence_records))
    _atomic_write_text(
        manifest_path,
        json.dumps(result.manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return {
        "school": school_path,
        "evidence": evidence_path,
        "manifest": manifest_path,
    }


def iter_artifact_records(path: Path) -> Iterator[dict[str, Any]]:
    """Read records previously written by :func:`write_v2_artifacts`."""
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"record at {path}:{line_number} is not an object")
            yield value
