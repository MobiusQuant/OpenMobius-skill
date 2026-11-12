import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_hosted_package as hosted  # noqa: E402


class HostedPackageBuilderTests(unittest.TestCase):
    def _make_minimal_source(self, root: Path) -> Path:
        source = root / "source"
        (source / "platforms").mkdir(parents=True)
        (source / "scripts" / "_lib").mkdir(parents=True)
        (source / "workflows").mkdir()
        (source / "knowledge_base" / "concepts").mkdir(parents=True)
        (source / "knowledge_base" / "schemas").mkdir()
        (source / "knowledge_base" / "_index").mkdir()
        for pname, label in (("grok-bot", "Grok Bot"), ("muse", "Muse")):
            (source / "platforms" / f"{pname}.yaml").write_text(
                f"name: openmobius-skill\ndescription: Trading workflows for {label} with School capability discovery.\n",
                encoding="utf-8",
            )
        (source / "SKILL.body.md").write_text(
            "# OpenMobius\n"
            "<PYTHON> scripts/kb_retrieve.py --layer school --list-schools\n"
            "| `scripts/build_index.py` | Build canonical + independently embedded v2 collections; unchanged v2 documents reuse the local content cache |\n",
            encoding="utf-8",
        )
        (source / "workflows" / "qna.md").write_text(
            "# QnA\nkb_retrieve.py \"Order Block\" --layer school --schools ICT\n",
            encoding="utf-8",
        )
        (source / "scripts" / "kb_retrieve.py").write_text(
            "print('ok')\n", encoding="utf-8"
        )
        (source / "scripts" / "build_index.py").write_text(
            "raise SystemExit('must not ship')\n", encoding="utf-8"
        )
        (source / "scripts" / ".env.local").write_text(
            "SECRET=1\n", encoding="utf-8"
        )
        (source / "knowledge_base" / "concepts" / "ob.json").write_text(
            json.dumps(
                {
                    "global_card_id": "order_block",
                    "canonical_term": "Order Block",
                    "school": "ICT",
                    "aliases": ["OB"],
                    "definition": "Institutional footprint candle.",
                    "source_cards": [
                        {
                            "project": "Test Source",
                            "source_school": "ICT",
                            "card_id": "src-1",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "schools.json").write_text(
            json.dumps(
                {
                    "registry_version": 1,
                    "default_profile": {"schools": ["ICT"]},
                    "schools": [
                        {
                            "id": "ict",
                            "name": "ICT",
                            "aliases": [],
                            "kind": "analysis_lens",
                            "availability": "top_level",
                            "knowledge_qna": True,
                            "native_market_analyzer": "smc",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "term_aliases.json").write_text(
            json.dumps(
                {
                    "mappings": [
                        {
                            "card_id": "order_block",
                            "canonical": "Order Block",
                            "aliases": ["OB"],
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "README.md").write_text(
            "# KB\n", encoding="utf-8"
        )
        (source / "knowledge_base" / "schemas" / "README.md").write_text(
            "# Schemas\n", encoding="utf-8"
        )
        (source / "knowledge_base" / "_index" / "chroma.sqlite3").write_bytes(
            b"do-not-bundle"
        )
        (source / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
        (source / "ATTRIBUTION.md").write_text("# Attr\n", encoding="utf-8")
        (source / "PRIVACY.md").write_text("# Privacy\n", encoding="utf-8")
        return source

    def test_builds_deterministic_grok_bot_and_muse_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_minimal_source(root)
            for pname, brand in (("grok-bot", "Grok Bot"), ("muse", "Muse")):
                with self.subTest(platform=pname):
                    out_a = root / f"{pname}-a.zip"
                    out_b = root / f"{pname}-b.zip"
                    summary_a = hosted.build_package(
                        source, out_a, platform_name=pname
                    )
                    summary_b = hosted.build_package(
                        source, out_b, platform_name=pname
                    )
                    self.assertEqual(summary_a["platform"], pname)
                    self.assertLess(summary_a["bytes"], hosted.HOSTED_MAX_ARCHIVE_BYTES)
                    self.assertEqual(out_a.read_bytes(), out_b.read_bytes())

                    with zipfile.ZipFile(out_a) as archive:
                        names = set(archive.namelist())
                        self.assertIn("openmobius-skill/SKILL.md", names)
                        self.assertIn(
                            "openmobius-skill/knowledge_base/knowledge_v2.compact.json",
                            names,
                        )
                        self.assertIn(
                            "openmobius-skill/knowledge_base/.openmobius-readonly-bundle.json",
                            names,
                        )
                        self.assertNotIn(
                            "openmobius-skill/scripts/build_index.py", names
                        )
                        self.assertFalse(any(".env" in n for n in names))
                        self.assertFalse(any("_index" in n for n in names))
                        skill_md = archive.read("openmobius-skill/SKILL.md").decode(
                            "utf-8"
                        )
                        self.assertIn(f"{brand} hosted compact-package mode:", skill_md)
                        self.assertIn("--search-mode lexical", skill_md)
                        self.assertNotIn("scripts/build_index.py", skill_md)

    def test_real_repository_hosted_archive_runs_stdlib_lexical_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "openmobius-skill-grok-bot.zip"
            summary = hosted.build_package(
                ROOT, archive_path, platform_name="grok-bot"
            )
            self.assertLess(summary["bytes"], hosted.HOSTED_MAX_ARCHIVE_BYTES)

            extracted = root / "extracted"
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extracted)

            retrieve_script = (
                extracted / "openmobius-skill" / "scripts" / "kb_retrieve.py"
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(retrieve_script),
                    "--search-mode",
                    "lexical",
                    "Order Block",
                    "--layer",
                    "school",
                    "--schools",
                    "ICT",
                    "SMC",
                    "--top-k",
                    "2",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(proc.stdout)
            self.assertIsInstance(payload, list)
            self.assertGreaterEqual(len(payload), 2)
            self.assertEqual(payload[0]["term"], "Order Block")
            self.assertIn(payload[0]["school"], {"ICT", "SMC"})

            canonical_proc = subprocess.run(
                [
                    sys.executable,
                    str(retrieve_script),
                    "--search-mode",
                    "lexical",
                    "Order Block",
                    "--layer",
                    "canonical",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(canonical_proc.returncode, 0)
            self.assertIn(
                "canonical fused-card retrieval is unavailable",
                canonical_proc.stderr,
            )


if __name__ == "__main__":
    unittest.main()
