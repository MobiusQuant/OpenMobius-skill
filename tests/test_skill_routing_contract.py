import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install  # noqa: E402


class SkillRoutingContractTests(unittest.TestCase):
    def test_claude_frontmatter_and_shared_body_reproduce_source_skill(self):
        expected = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(install._compose_skill_md("claude-code"), expected)

    def test_runtime_examples_are_host_neutral(self):
        body = (ROOT / "SKILL.body.md").read_text(encoding="utf-8")
        workflows = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "workflows").glob("*.md"))
        }
        combined = body + "\n" + "\n".join(workflows.values())

        for placeholder in (
            "<SKILL_ROOT>",
            "<PYTHON>",
            "<TEMP_DIR>",
            "<USER_OUTPUT_DIR>",
            "<INPUT_IMAGE>",
        ):
            self.assertIn(placeholder, body)

        for forbidden in (
            "/home/Codes/QuantKnowledge",
            "/tmp/",
            ".venv/bin/python",
            r".venv\Scripts\python.exe",
            "cat >",
            "<<'JSON'",
        ):
            self.assertNotIn(forbidden, combined)

        self.assertNotRegex(combined, r"(?m)^\s*echo\b.*\|")
        self.assertNotRegex(combined, r"(?m)^.*\\\s*$")
        for name, workflow in workflows.items():
            with self.subTest(workflow=name):
                self.assertNotIn("```bash", workflow)

        self.assertIn(
            "must** be expanded to `<PYTHON> scripts/kb_retrieve.py`",
            body,
        )

    def test_documented_school_catalog_matches_knowledge_base(self):
        profile = (ROOT / "workflows" / "analysis_profiles.md").read_text(
            encoding="utf-8"
        )
        catalog = profile.split("## Canonical School tags and aliases", 1)[1]
        catalog = catalog.split("## Retrieval layers", 1)[0]
        documented = set(re.findall(r"^\| `([^`]+)` \|", catalog, re.MULTILINE))

        registry = json.loads(
            (ROOT / "knowledge_base" / "schools.json").read_text(encoding="utf-8")
        )
        registered = {entry["name"] for entry in registry["schools"]}

        actual = set()
        for kind in ("concepts", "cases"):
            for path in (ROOT / "knowledge_base" / kind).glob("*.json"):
                card = json.loads(path.read_text(encoding="utf-8"))
                if card.get("school"):
                    actual.add(card["school"])

        self.assertEqual(documented, registered)
        self.assertTrue(actual.issubset(registered))
        self.assertEqual(registered - actual, {"Scalping"})

    def test_route_schema_and_phase_one_market_compare_are_explicit(self):
        profile = (ROOT / "workflows" / "analysis_profiles.md").read_text(
            encoding="utf-8"
        )
        for field in (
            '"exact_primary_school_filter"',
            '"native_market_analyzer"',
            '"source_filter"',
            '"intent_supported"',
            '"reason"',
        ):
            self.assertIn(field, profile)
        self.assertNotIn('"exact_school_filter"', profile)
        self.assertIn("Phase 1 supports `compare` only for Q&A", profile)

    def test_shared_instructions_advertise_multi_school_scope(self):
        # Some hosts impose much tighter discovery-description limits than the
        # portable 1,024-character ceiling (OpenClaw documents <160 and Hermes
        # quality checks use <=60). Keep the full routing contract in the
        # progressively loaded body instead of duplicating it in every summary.
        text = "\n".join(
            [
                (ROOT / "SKILL.body.md").read_text(encoding="utf-8"),
                (ROOT / "workflows" / "analysis_profiles.md").read_text(
                    encoding="utf-8"
                ),
            ]
        )
        for term in ("Wyckoff", "Order Flow", "Phase 1"):
            self.assertIn(term, text)

    def test_platform_descriptions_advertise_capability_discovery(self):
        for path in sorted((ROOT / "platforms").glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            description = next(
                line.removeprefix("description:").strip()
                for line in text.splitlines()
                if line.startswith("description:")
            )
            with self.subTest(platform=path.name):
                self.assertRegex(description, r"(?i)(?:capabilit|\u80fd\u529b)")
                self.assertIn("School", description)

    def test_capability_discovery_precedes_normal_qna_retrieval(self):
        workflow = (ROOT / "workflows" / "qna.md").read_text(encoding="utf-8")
        inventory_flag = workflow.find("--list-schools")
        normal_retrieval = workflow.find('kb_retrieve.py "<query>"')

        self.assertGreaterEqual(inventory_flag, 0)
        self.assertGreater(normal_retrieval, inventory_flag)

        block_start = workflow.rfind("```", 0, inventory_flag)
        block_end = workflow.find("```", inventory_flag)
        self.assertGreaterEqual(block_start, 0)
        self.assertGreater(block_end, inventory_flag)
        inventory_command = workflow[block_start:block_end]
        for required_part in (
            "kb_retrieve.py",
            "--list-schools",
            "--layer school",
            "--format json",
        ):
            self.assertIn(required_part, inventory_command)
        self.assertIn(
            "expanded to `<PYTHON> scripts/kb_retrieve.py`",
            workflow[:inventory_flag],
        )

    def test_capability_discovery_is_bounded_to_one_local_inventory(self):
        body = (ROOT / "SKILL.body.md").read_text(encoding="utf-8")
        workflow = (ROOT / "workflows" / "qna.md").read_text(
            encoding="utf-8"
        )
        bounded = body + "\n" + workflow
        for required in (
            "exactly one inventory",
            "Do **not** invoke the skill recursively",
            "subagent",
            "run `git`",
        ):
            self.assertIn(required, bounded)
        self.assertRegex(bounded, r"synthesize\s+the response immediately")

    def test_capability_discovery_is_qna_control_plane_not_a_fifth_intent(self):
        body = (ROOT / "SKILL.body.md").read_text(encoding="utf-8")
        orchestration = body.split("## Analysis profile orchestration", 1)[1]
        orchestration = orchestration.split(
            "## Market-analysis output format is mandatory", 1
        )[0]

        self.assertRegex(
            orchestration,
            r"(?is)(?:capability discovery|capability inventory|control-plane)",
        )
        self.assertIn("intent=qna", orchestration)
        self.assertIn("workflows/qna.md", orchestration)

        match = re.search(
            r"`intent` must be exactly one of (?P<values>.*?)\.",
            orchestration,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            set(re.findall(r"`([a-z_]+)`", match.group("values"))),
            {"qna", "analyze", "annotate", "klines"},
        )

    def test_readmes_include_a_capability_discovery_example(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh.md").read_text(encoding="utf-8")

        self.assertRegex(
            english,
            r"(?i)[\"'](?:what|which)[^\"']{0,80}(?:analysis models|analysis lenses|schools)[^\"']*[?][\"']",
        )
        self.assertRegex(
            chinese,
            r"[\"'](?:\u5f53\u524d)?[^\"']{0,30}(?:\u5206\u6790\u6a21\u578b|\u5206\u6790\u89c6\u89d2|School)[^\"']{0,30}(?:\u53ef\u4ee5\u4f7f\u7528|\u53ef\u7528|\u652f\u6301)[^\"']*[\uff1f?][\"']",
        )


if __name__ == "__main__":
    unittest.main()
