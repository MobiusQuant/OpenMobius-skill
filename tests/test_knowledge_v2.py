import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _lib.knowledge_v2 import (  # noqa: E402
    EVIDENCE_COLLECTION,
    SCHOOL_COLLECTION,
    build_v2_records,
    iter_artifact_records,
    load_school_registry,
    write_v2_artifacts,
)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _fixture_kb(root: Path, concepts=None, cases=None) -> Path:
    kb = root / "knowledge_base"
    kb.mkdir()
    (kb / "schools.json").write_text(
        (ROOT / "knowledge_base" / "schools.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for name, card in (concepts or {}).items():
        _write_json(kb / "concepts" / f"{name}.json", card)
    for name, card in (cases or {}).items():
        _write_json(kb / "cases" / f"{name}.json", card)
    return kb


class SchoolRegistryTests(unittest.TestCase):
    def test_registry_distinguishes_top_level_and_evidence_only_schools(self):
        registry = load_school_registry(ROOT / "knowledge_base")

        self.assertEqual(len(registry.schools), 15)
        self.assertEqual(
            sum(school.availability == "top_level" for school in registry.schools),
            14,
        )
        scalping = next(school for school in registry.schools if school.name == "Scalping")
        self.assertEqual(scalping.availability, "evidence_only")
        self.assertEqual(registry.resolve("  chan lun  "), "缠论")
        self.assertEqual(registry.resolve("smart MONEY concepts"), "SMC")
        self.assertIsNone(registry.resolve("not-a-school"))

    def test_registry_rejects_malformed_typed_fields(self):
        base = json.loads(
            (ROOT / "knowledge_base" / "schools.json").read_text(encoding="utf-8")
        )
        mutations = (
            (lambda data: data.update({"registry_version": True}), "version"),
            (
                lambda data: data["schools"][0].update({"aliases": "ABC"}),
                "aliases",
            ),
            (
                lambda data: data["schools"][0].update({"knowledge_qna": "false"}),
                "knowledge_qna",
            ),
            (
                lambda data: data.update({"default_profile": []}),
                "default_profile",
            ),
            (
                lambda data: data["default_profile"].update({"schools": "ICT"}),
                "default_profile.schools",
            ),
        )
        for mutate, message in mutations:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                kb = Path(temp)
                malformed = copy.deepcopy(base)
                mutate(malformed)
                _write_json(kb / "schools.json", malformed)
                with self.assertRaisesRegex(ValueError, message):
                    load_school_registry(kb)

    def test_registry_rejects_non_object_root(self):
        with tempfile.TemporaryDirectory() as temp:
            kb = Path(temp)
            _write_json(kb / "schools.json", [])
            with self.assertRaisesRegex(ValueError, "root must be an object"):
                load_school_registry(kb)


class SourceIntegrityTests(unittest.TestCase):
    def test_malformed_json_fails_instead_of_emitting_partial_v2_records(self):
        with tempfile.TemporaryDirectory() as temp:
            kb = _fixture_kb(
                Path(temp),
                concepts={
                    "valid": {
                        "global_card_id": "valid",
                        "canonical_term": "Valid",
                        "school": "ICT",
                    }
                },
            )
            broken = kb / "concepts" / "broken.json"
            broken.write_text('{"canonical_term":', encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, r"invalid concept card concepts/broken\.json"
            ):
                build_v2_records(kb)

    def test_non_object_json_fails_with_the_source_path(self):
        with tempfile.TemporaryDirectory() as temp:
            kb = _fixture_kb(Path(temp), cases={"array": []})

            with self.assertRaisesRegex(
                ValueError,
                r"invalid case card cases/array\.json: top-level JSON value",
            ):
                build_v2_records(kb)

    def test_duplicate_canonical_identity_fails_instead_of_merging_files(self):
        card = {
            "global_card_id": "duplicate",
            "canonical_term": "Duplicate",
            "school": "缠论",
            "definition": "Definition",
            "source_cards": [{"video_id": "v1"}],
        }
        with tempfile.TemporaryDirectory() as temp:
            kb = _fixture_kb(
                Path(temp),
                concepts={"first": card, "second": copy.deepcopy(card)},
            )

            with self.assertRaisesRegex(
                ValueError,
                "duplicate canonical knowledge-card identity",
            ):
                build_v2_records(kb)


class ConservativeConceptBuildTests(unittest.TestCase):
    def test_cross_school_card_only_emits_exactly_mapped_definition(self):
        card = {
            "global_card_id": "shared_concept",
            "canonical_term": "Shared Concept",
            "type": "merged_concept",
            "school": "ICT",
            "aliases": ["SC"],
            "definition": "Fused top-level definition",
            "definition_per_source": {
                "Shared Project": "Ambiguous project-only definition",
                "Shared Project (SMC)": "Explicit SMC definition",
            },
            "identification_rules": ["Fused rule"],
            "common_mistakes": [],
            "trading_implication": "Fused implication",
            "source_cards": [
                {
                    "project": "Shared Project",
                    "card_id": "ict_card",
                    "source_school": "ICT",
                },
                {
                    "project": "Shared Project",
                    "card_id": "smc_card",
                    "source_school": "SMC",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            result = build_v2_records(
                _fixture_kb(Path(temp), concepts={"shared_concept": card})
            )

        self.assertEqual(len(result.evidence_records), 1)
        evidence = result.evidence_records[0]
        self.assertEqual(evidence["metadata"]["school"], "SMC")
        self.assertEqual(evidence["metadata"]["source"], "Shared Project")
        self.assertEqual(evidence["metadata"]["type"], "concept")
        self.assertEqual(
            evidence["payload"]["attribution"]["match_strategy"],
            "project_and_school",
        )
        self.assertEqual(len(result.school_records), 1)
        self.assertEqual(result.school_records[0]["metadata"]["school"], "SMC")
        self.assertEqual(result.stats["skipped"]["ambiguous_project_school"], 1)
        self.assertEqual(
            result.stats["skipped"]["fused_rule_not_attributable"], 2
        )
        self.assertEqual(
            result.stats["skipped"]["fused_definition_not_attributable"], 1
        )

    def test_single_school_multi_source_rules_are_projected_but_not_source_evidence(self):
        card = {
            "global_card_id": "one_school",
            "canonical_term": "One School",
            "school": "ICT",
            "aliases": [],
            "definition": "School-safe synthesis",
            "definition_per_source": {
                "Source A": "Definition A",
                "Source B": "Definition B",
            },
            "identification_rules": ["School-safe rule"],
            "common_mistakes": [],
            "trading_implication": "",
            "source_cards": [
                {"project": "Source A", "card_id": "a", "source_school": "ICT"},
                {"project": "Source B", "card_id": "b", "source_school": "ICT"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            result = build_v2_records(
                _fixture_kb(Path(temp), concepts={"one_school": card})
            )

        self.assertEqual(len(result.evidence_records), 2)
        projection = result.school_records[0]["payload"]
        self.assertEqual(projection["school"], "ICT")
        self.assertEqual(projection["source_names"], ["Source A", "Source B"])
        school_only = [
            item
            for items in projection["content_by_type"].values()
            for item in items
            if item["attribution_level"] == "school"
        ]
        self.assertEqual({item["content"] for item in school_only}, {
            "School-safe synthesis",
            "School-safe rule",
        })
        self.assertTrue(all("source" not in item for item in school_only))
        self.assertEqual(result.stats["skipped"]["non_atomic_source_provenance"], 2)

    def test_card_school_only_legacy_shape_projects_chanlun_without_inventing_source(self):
        card = {
            "global_card_id": "chanlun__center",
            "canonical_term": "中枢",
            "school": "缠论",
            "aliases": [],
            "definition": "中枢定义",
            "identification_rules": ["中枢规则"],
            "common_mistakes": [],
            "trading_implication": "",
            "source_cards": [
                {"video_id": "video-a", "segment_ids": ["seg-1"]},
                {"video_id": "video-b", "segment_ids": ["seg-2"]},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            result = build_v2_records(
                _fixture_kb(Path(temp), concepts={"chanlun__center": card})
            )

        self.assertEqual(len(result.evidence_records), 0)
        self.assertEqual(len(result.school_records), 1)
        projection = result.school_records[0]["payload"]
        self.assertEqual(projection["school"], "缠论")
        self.assertEqual(projection["source_names"], [])
        self.assertEqual(
            projection["derivation"]["school_attribution"], "card_school_only"
        )
        self.assertTrue(
            all(
                item["attribution_level"] == "school"
                for items in projection["content_by_type"].values()
                for item in items
            )
        )
        self.assertEqual(result.stats["skipped"]["missing_source_provenance"], 2)

    def test_card_school_only_legacy_shape_cannot_claim_another_school(self):
        card = {
            "global_card_id": "unproven_ict",
            "canonical_term": "Unproven ICT concept",
            "school": "ICT",
            "aliases": [],
            "definition": "Unproven definition",
            "identification_rules": ["Unproven rule"],
            "common_mistakes": [],
            "trading_implication": "",
            "source_cards": [{"video_id": "video-a"}],
        }
        with tempfile.TemporaryDirectory() as temp:
            result = build_v2_records(
                _fixture_kb(Path(temp), concepts={"unproven_ict": card})
            )

        self.assertEqual(result.school_records, ())
        self.assertEqual(result.evidence_records, ())
        self.assertEqual(
            result.stats["skipped"]["fused_definition_not_attributable"],
            1,
        )
        self.assertEqual(
            result.stats["skipped"]["fused_rule_not_attributable"],
            1,
        )


class CaseAndArtifactTests(unittest.TestCase):
    def test_case_requires_and_preserves_exact_project_origin(self):
        case = {
            "global_card_id": "case-1",
            "title": "A case",
            "school": "Wyckoff",
            "project_origin": "Wyckoff Course",
            "market_context": "Range",
            "key_observation": "Spring",
            "analysis_steps": ["Confirm recovery"],
            "outcome": "Markup",
            "lessons": "Wait for confirmation",
            "sources": [{"video_id": "v1", "segment_ids": ["s1"]}],
            "review_status": "reviewed",
            "extraction_confidence": "high",
        }
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            kb = _fixture_kb(temp_path, cases={"case-1": case})
            first = build_v2_records(kb)
            second = build_v2_records(kb)
            first_paths = write_v2_artifacts(first, temp_path / "out-a")
            second_paths = write_v2_artifacts(second, temp_path / "out-b")

            self.assertEqual(first.manifest(), second.manifest())
            self.assertEqual(len(first.evidence_records), 5)
            self.assertTrue(
                all(
                    record["metadata"]["source"] == "Wyckoff Course"
                    for record in first.evidence_records
                )
            )
            self.assertEqual(
                first_paths["school"].read_bytes(),
                second_paths["school"].read_bytes(),
            )
            self.assertEqual(
                first_paths["evidence"].read_bytes(),
                second_paths["evidence"].read_bytes(),
            )
            self.assertEqual(
                len(list(iter_artifact_records(first_paths["evidence"]))), 5
            )
            self.assertEqual(first.manifest()["collections"][SCHOOL_COLLECTION], 1)
            self.assertEqual(first.manifest()["collections"][EVIDENCE_COLLECTION], 5)


class FullCorpusContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = build_v2_records(ROOT / "knowledge_base")

    def test_real_chanlun_concepts_are_projected_without_source_evidence(self):
        projections = [
            record
            for record in self.result.school_records
            if record["metadata"]["type"] == "concept"
            and record["metadata"]["school"] == "缠论"
        ]
        evidence = [
            record
            for record in self.result.evidence_records
            if record["metadata"]["type"] == "concept"
            and record["metadata"]["school"] == "缠论"
        ]
        self.assertEqual(len(projections), 58)
        self.assertTrue(
            any(record["metadata"]["term"] == "中枢" for record in projections)
        )
        self.assertEqual(evidence, [])

    def test_real_tent_airplane_composite_keys_map_without_guessing(self):
        card = json.loads(
            (
                ROOT
                / "knowledge_base"
                / "concepts"
                / "tent_airplane_pattern.json"
            ).read_text(encoding="utf-8")
        )
        evidence = [
            record
            for record in self.result.evidence_records
            if record["metadata"]["type"] == "concept"
            and record["metadata"]["canonical_id"] == "tent_airplane_pattern"
        ]
        projections = [
            record
            for record in self.result.school_records
            if record["metadata"]["type"] == "concept"
            and record["metadata"]["canonical_id"] == "tent_airplane_pattern"
        ]

        self.assertEqual(len(evidence), 3)
        self.assertEqual(
            sorted(record["metadata"]["school"] for record in evidence),
            ["General", "Price Action", "Price Action"],
        )
        self.assertEqual(
            {record["metadata"]["source"] for record in evidence},
            {"Teach-Wuyuan"},
        )
        self.assertEqual(
            {
                record["payload"]["attribution"]["match_strategy"]
                for record in evidence
            },
            {"project_card_id_and_canonical_term"},
        )
        self.assertEqual(
            sorted(record["metadata"]["school"] for record in projections),
            ["General", "Price Action"],
        )
        expected_content = set(card["definition_per_source"].values())
        self.assertEqual(
            {record["payload"]["content"] for record in evidence},
            expected_content,
        )
        self.assertEqual(
            {
                item["content"]
                for record in projections
                for items in record["payload"]["content_by_type"].values()
                for item in items
            },
            expected_content,
        )

    def test_published_full_corpus_counts_and_definition_coverage(self):
        self.assertEqual(len(self.result.school_records), 2_144)
        self.assertEqual(len(self.result.evidence_records), 18_645)
        self.assertEqual(self.result.stats["input"]["definition_per_source"], 1_226)
        self.assertEqual(
            self.result.stats["emitted"]["definition_source_evidence"], 1_164
        )
        self.assertEqual(self.result.stats["skipped"]["unmapped_definition_key"], 52)
        self.assertEqual(
            self.result.stats["coverage"]["definition_per_source_exact_evidence"],
            0.949429,
        )
        self.assertEqual(
            self.result.stats["emitted"][
                "definition_match_project_card_id_and_canonical_term"
            ],
            3,
        )

    def test_index_metadata_contract_is_scalar_and_parent_linked(self):
        for record in (
            self.result.school_records[0],
            self.result.evidence_records[0],
        ):
            metadata = record["metadata"]
            self.assertIn(metadata["type"], {"concept", "case"})
            self.assertIn(metadata["layer"], {"school", "evidence"})
            self.assertTrue(metadata["file_path"].startswith(("concepts/", "cases/")))
            self.assertTrue(
                all(
                    isinstance(value, (str, int, float, bool))
                    for value in metadata.values()
                )
            )
            self.assertEqual(metadata["embedding_strategy"], "native_document")
            self.assertEqual(record["payload"]["embedding_strategy"], "native_document")

    def test_representative_payloads_validate_against_published_schemas(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        school_schema = json.loads(
            (ROOT / "knowledge_base" / "schemas" / "school_projection_v2.schema.json")
            .read_text(encoding="utf-8")
        )
        evidence_schema = json.loads(
            (ROOT / "knowledge_base" / "schemas" / "source_evidence_v2.schema.json")
            .read_text(encoding="utf-8")
        )
        school_validator = jsonschema.Draft202012Validator(school_schema)
        evidence_validator = jsonschema.Draft202012Validator(evidence_schema)
        samples = [
            self.result.school_records[0]["payload"],
            next(
                record["payload"]
                for record in self.result.school_records
                if record["metadata"]["school"] == "缠论"
                and record["metadata"]["type"] == "concept"
            ),
        ]
        for payload in samples:
            self.assertEqual(list(school_validator.iter_errors(payload)), [])
        for payload in (
            self.result.evidence_records[0]["payload"],
            self.result.evidence_records[-1]["payload"],
            next(
                record["payload"]
                for record in self.result.evidence_records
                if record["metadata"]["canonical_id"] == "tent_airplane_pattern"
            ),
        ):
            self.assertEqual(list(evidence_validator.iter_errors(payload)), [])

    def test_school_schema_rejects_empty_content_and_incomplete_source_attribution(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        school_schema = json.loads(
            (ROOT / "knowledge_base" / "schemas" / "school_projection_v2.schema.json")
            .read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(school_schema)
        source_projection = next(
            record["payload"]
            for record in self.result.school_records
            if record["metadata"]["canonical_id"] == "tent_airplane_pattern"
            and record["metadata"]["school"] == "General"
        )

        empty_content = copy.deepcopy(source_projection)
        empty_content["content_by_type"] = {}
        self.assertTrue(list(validator.iter_errors(empty_content)))

        incomplete_source = copy.deepcopy(source_projection)
        item = incomplete_source["content_by_type"]["definition"][0]
        self.assertEqual(item["attribution_level"], "source")
        item.pop("source")
        item.pop("evidence_id")
        self.assertTrue(list(validator.iter_errors(incomplete_source)))


if __name__ == "__main__":
    unittest.main()
