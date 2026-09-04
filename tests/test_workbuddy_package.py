import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path, PurePosixPath
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_workbuddy_package as package  # noqa: E402


class WorkBuddyPackageTests(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "source"
        (source / "platforms").mkdir(parents=True)
        (source / "scripts" / "__pycache__").mkdir(parents=True)
        (source / "scripts" / "cache").mkdir()
        (source / "workflows").mkdir()
        (source / "knowledge_base" / "_index").mkdir(parents=True)
        (source / "knowledge_base" / "_embedding_cache").mkdir()
        (source / "knowledge_base" / "embedding_seed_v2").mkdir()
        (source / "knowledge_base" / "._index.build-interrupted").mkdir()
        (source / "knowledge_base" / "._cards.build-interrupted").mkdir()
        (source / "knowledge_base" / ".embedding_seed_v2.build-interrupted").mkdir()
        (source / "knowledge_base" / ".embedding_seed_v2.backup-interrupted").mkdir()
        (source / "knowledge_base" / "concepts").mkdir()
        (source / "templates").mkdir()
        (source / "agents").mkdir()
        (source / "platforms" / "workbuddy.yaml").write_text(
            "name: openmobius-skill\n"
            "description: Trading analysis.\n"
            "description_zh: 交易分析。\n"
            "description_en: Trading analysis.\n"
            "version: 0.4.0\n"
            "author: MobiusQuant\n",
            encoding="utf-8",
        )
        (source / "SKILL.body.md").write_text(
            "# Test\n"
            "Read `workflows/qna.md` and `knowledge_base/schools.json`.\n"
            "Consult `SKILL.body.md` for field semantics.\n"
            ".venv/bin/python scripts/tool.py\n"
            ".venv/bin/python scripts/kb_retrieve.py --layer school "
            "--list-schools\n",
            encoding="utf-8",
        )
        (source / "scripts" / "tool.py").write_text(
            '# See SKILL.body.md for field semantics.\n'
            'from pathlib import Path\nSKILL_DIR = Path(".")\n'
            'KB = SKILL_DIR / "knowledge_base"\n'
            'HELP = ".venv/bin/pip install playwright; '
            '.venv/bin/playwright install chromium"\n',
            encoding="utf-8",
        )
        (source / "scripts" / "__pycache__" / "tool.pyc").write_bytes(b"cache")
        (source / "scripts" / "cache" / "result.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (source / "scripts" / ".env").write_text(
            "API_TOKEN=secret\n", encoding="utf-8"
        )
        (source / "scripts" / ".env.production").write_text(
            "API_TOKEN=secret\n", encoding="utf-8"
        )
        (source / "scripts" / "private.key").write_text(
            "secret\n", encoding="utf-8"
        )
        (source / "workflows" / "qna.md").write_text(
            "# Q&A\nRead `workflows/analyze.md`.\n"
            "Use the field map in `SKILL.body.md`.\n"
            ".venv/bin/python scripts/tool.py\n"
            "kb_retrieve.py \"FVG\" --layer school\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "concepts" / "one.json").write_text(
            json.dumps(
                {
                    "global_card_id": "fair_value_gap",
                    "canonical_term": "Fair Value Gap",
                    "school": "ICT",
                    "aliases": ["FVG"],
                    "definition": "A three-candle price imbalance.",
                    "source_cards": [
                        {
                            "project": "Test Source",
                            "source_school": "ICT",
                            "card_id": "source-card-1",
                        }
                    ],
                },
                ensure_ascii=False,
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
                            "aliases": ["Inner Circle Trader"],
                            "kind": "analysis_lens",
                            "availability": "top_level",
                            "knowledge_qna": True,
                            "native_market_analyzer": "smc",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "term_aliases.json").write_text(
            json.dumps(
                {
                    "mappings": [
                        {
                            "card_id": "fair_value_gap",
                            "canonical": "Fair Value Gap",
                            "aliases": ["FVG"],
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "README.md").write_text(
            "# knowledge_base/\n\n"
            "```\n"
            "knowledge_base/\n"
            "├── embedding_seed_v2/ # verified native School/evidence vectors (16 shards)\n"
            "└── schemas/           # School projection + source-evidence v2 schemas\n"
            "```\n\n"
            "## Build or upgrade\n\n"
            "```bash\n"
            ".venv/bin/python scripts/build_index.py\n"
            ".venv/bin/python scripts/export_v2_embedding_seed.py\n"
            ".venv/bin/python scripts/evaluate_retrieval.py\n"
            "```\n\n"
            "## Attribution\n\n"
            "See `../ATTRIBUTION.md` for the project's full third-party attribution.\n",
            encoding="utf-8",
        )
        (source / "knowledge_base" / "_index" / "chroma.sqlite3").write_bytes(
            b"local-index"
        )
        (
            source / "knowledge_base" / "_embedding_cache" / "embeddings.sqlite3"
        ).write_bytes(b"local-cache")
        (
            source / "knowledge_base" / "embedding_seed_v2" / "shard.bin"
        ).write_bytes(b"vector-seed")
        for transaction_dir in (
            "._index.build-interrupted",
            "._cards.build-interrupted",
            ".embedding_seed_v2.build-interrupted",
            ".embedding_seed_v2.backup-interrupted",
        ):
            (source / "knowledge_base" / transaction_dir / "private.json").write_text(
                '{"transaction": "must-not-ship"}\n', encoding="utf-8"
            )
        (source / "templates" / "report.md").write_text(
            "# Report\n", encoding="utf-8"
        )
        (source / "templates" / "credentials.json").write_text(
            '{"token": "secret"}\n', encoding="utf-8"
        )
        (source / "agents" / "agent.yaml").write_text(
            "name: no\n", encoding="utf-8"
        )
        (source / "requirements.txt").write_text("example==1\n", encoding="utf-8")
        (source / "install.py").write_text("print('install')\n", encoding="utf-8")
        (source / "LICENSE").write_text("Test license\n", encoding="utf-8")
        (source / "ATTRIBUTION.md").write_text(
            "# Attribution\n\n"
            "The repository does include `knowledge_base/embedding_seed_v2/`: derived\n"
            "float32 embedding vectors generated by that model from this project's scoped\n"
            "School/evidence documents. These numeric outputs are not model weights. Their\n"
            "manifest records the model, input profile, corpus fingerprint, and SHA-256 of\n"
            "each shard so the runtime can verify identity and integrity before reuse.\n",
            encoding="utf-8",
        )
        return source

    def test_compose_maps_workflows_to_official_references_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp))
            skill = package.compose_workbuddy_skill(source)

        self.assertIn("name: openmobius-skill", skill)
        self.assertIn("@references/workflows/qna.md", skill)
        self.assertIn("all 1 School projections", skill)
        self.assertIn("and 1 exact-source evidence", skill)
        self.assertNotIn("2,144", skill)
        self.assertNotIn("18,645", skill)
        self.assertNotIn("--layer canonical|school|evidence", skill)
        self.assertIn("`--layer school` or `--layer evidence`", skill)
        self.assertIn("@references/knowledge_base/schools.json", skill)
        self.assertNotIn("`workflows/qna.md`", skill)
        self.assertNotIn(".venv/bin/python", skill)
        self.assertIn("python3 scripts/tool.py", skill)
        self.assertIn(
            "python3 scripts/kb_retrieve.py --search-mode lexical",
            skill,
        )
        self.assertIn("the loaded root `SKILL.md`", skill)
        self.assertNotIn("SKILL.body.md", skill)
        self.assertIn("WorkBuddy package mode", skill)

    def test_archive_has_workbuddy_root_and_excludes_local_build_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            output = root / "openmobius-workbuddy.zip"

            summary = package.build_package(source, output)
            with zipfile.ZipFile(output) as archive:
                ordered_names = archive.namelist()
                names = set(ordered_names)
                skill = archive.read(
                    "skills/openmobius-skill/SKILL.md"
                ).decode("utf-8")
                workflow = archive.read(
                    "skills/openmobius-skill/references/workflows/qna.md"
                ).decode("utf-8")
                script = archive.read(
                    "skills/openmobius-skill/scripts/tool.py"
                ).decode("utf-8")
                attribution = archive.read(
                    "skills/openmobius-skill/references/legal/ATTRIBUTION.md"
                ).decode("utf-8")
                kb_readme = archive.read(
                    "skills/openmobius-skill/references/knowledge_base/README.md"
                ).decode("utf-8")

        self.assertEqual(summary["archive_root"], "skills/openmobius-skill")
        self.assertEqual(len(names), len(ordered_names))
        self.assertEqual(ordered_names, sorted(ordered_names))
        self.assertIn("skills/openmobius-skill/SKILL.md", names)
        self.assertIn(
            "skills/openmobius-skill/references/workflows/qna.md", names
        )
        self.assertIn("skills/openmobius-skill/scripts/tool.py", names)
        self.assertNotIn(
            "skills/openmobius-skill/references/knowledge_base/concepts/one.json",
            names,
        )
        self.assertIn(
            "skills/openmobius-skill/references/knowledge_base/"
            + package.COMPACT_V2_FILENAME,
            names,
        )
        self.assertIn(
            "skills/openmobius-skill/references/knowledge_base/"
            + package.READ_ONLY_BUNDLE_MARKER,
            names,
        )
        self.assertIn(
            "skills/openmobius-skill/references/knowledge_base/term_aliases.json",
            names,
        )
        self.assertLessEqual(summary["bytes"], package.WORKBUDDY_MAX_ARCHIVE_BYTES)
        self.assertIn("skills/openmobius-skill/templates/report.md", names)
        self.assertIn("skills/openmobius-skill/references/legal/LICENSE", names)
        allowed = {"SKILL.md", "references", "scripts", "templates"}
        self.assertTrue(
            all(name.split("/")[2] in allowed for name in ordered_names),
        )
        self.assertFalse(any("_index" in name for name in names))
        self.assertFalse(any("cache" in name.casefold() for name in names))
        self.assertFalse(any("__pycache__" in name for name in names))
        self.assertFalse(any(".env" in name.casefold() for name in names))
        self.assertFalse(any(name.casefold().endswith(".key") for name in names))
        self.assertFalse(any("credentials.json" in name.casefold() for name in names))
        self.assertFalse(any("/platforms/" in name for name in names))
        self.assertFalse(any("/agents/" in name for name in names))
        self.assertFalse(any(name.endswith("/install.py") for name in names))
        self.assertIn("@references/workflows/qna.md", skill)
        self.assertIn("@references/workflows/analyze.md", workflow)
        self.assertIn("python3 scripts/tool.py", workflow)
        self.assertIn(
            'kb_retrieve.py --search-mode lexical "FVG"',
            workflow,
        )
        self.assertIn("the loaded root `SKILL.md`", workflow)
        self.assertNotIn("SKILL.body.md", workflow)
        self.assertIn(
            'SKILL_DIR / "references" / "knowledge_base"',
            script,
        )
        self.assertIn("the loaded root SKILL.md", script)
        self.assertNotIn("SKILL.body.md", script)
        self.assertIn("This WorkBuddy package deliberately omits", attribution)
        self.assertNotIn(
            "The repository does include `@references/knowledge_base/embedding_seed_v2/`",
            attribution,
        )
        self.assertIn("## Runtime retrieval in this WorkBuddy package", kb_readme)
        self.assertIn(
            "python3 scripts/kb_retrieve.py --search-mode lexical",
            kb_readme,
        )
        self.assertNotIn("--search-mode lexical --search-mode lexical", kb_readme)
        self.assertIn("@references/legal/ATTRIBUTION.md", kb_readme)
        self.assertNotIn("## Build or upgrade", kb_readme)
        self.assertNotIn("scripts/build_index.py", kb_readme)
        self.assertNotIn("scripts/export_v2_embedding_seed.py", kb_readme)
        self.assertNotIn("scripts/evaluate_retrieval.py", kb_readme)
        self.assertNotIn("├── embedding_seed_v2/", kb_readme)

    def test_existing_output_requires_explicit_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            output = root / "existing.zip"
            output.write_bytes(b"keep")

            with self.assertRaises(FileExistsError):
                package.build_package(source, output)

            package.build_package(source, output, force=True)
            self.assertGreater(output.stat().st_size, 0)

    def test_size_limit_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            output = root / "existing.zip"
            output.write_bytes(b"keep")

            with mock.patch.object(package, "WORKBUDDY_MAX_ARCHIVE_BYTES", 100):
                with self.assertRaisesRegex(
                    ValueError,
                    "documented 3 MB size limit",
                ):
                    package.build_package(source, output, force=True)

            self.assertEqual(output.read_bytes(), b"keep")
            self.assertEqual(list(root.glob(f".{output.name}.*.tmp")), [])

    def test_busy_knowledge_base_leaves_existing_output_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            output = root / "existing.zip"
            output.write_bytes(b"keep")

            with package.knowledge_base_build_lock(
                source / "knowledge_base",
                mode="write",
            ):
                with self.assertRaises(package.BuildLockUnavailable):
                    package.build_package(source, output, force=True)

            self.assertEqual(output.read_bytes(), b"keep")
            self.assertEqual(list(root.glob(f".{output.name}.*.tmp")), [])

    def test_unfinished_generation_leaves_existing_output_unchanged(self):
        for artifact in (
            package.INSTALL_GENERATION_MARKER,
            "._cards.backup-interrupted",
            "._index.backup-interrupted",
        ):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self.make_source(root)
                transaction_artifact = source / "knowledge_base" / artifact
                if artifact == package.INSTALL_GENERATION_MARKER:
                    transaction_artifact.write_text("{}\n", encoding="utf-8")
                else:
                    transaction_artifact.mkdir()
                output = root / "existing.zip"
                output.write_bytes(b"keep")

                with self.assertRaisesRegex(RuntimeError, "混合代际"):
                    package.build_package(source, output, force=True)

                self.assertEqual(output.read_bytes(), b"keep")

    def test_unfinished_generation_cli_reports_boundary_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            (source / "knowledge_base" / "._cards.backup-crash").mkdir()
            output = root / "existing.zip"
            output.write_bytes(b"keep")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_workbuddy_package.py"),
                    "--source",
                    str(source),
                    "--output",
                    str(output),
                    "--force",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("ERROR:", completed.stderr)
            self.assertIn("混合代际", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(output.read_bytes(), b"keep")

    def test_direct_compose_rejects_an_unfinished_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp))
            (source / "knowledge_base" / package.INSTALL_GENERATION_MARKER).write_text(
                "{}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "混合代际"):
                package.compose_workbuddy_skill(source)

    def test_busy_output_prevents_concurrent_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            output = root / "contended.zip"
            failures = []

            def build() -> None:
                try:
                    package.build_package(source, output)
                except BaseException as exc:  # capture worker result
                    failures.append(exc)

            with package.knowledge_base_build_lock(output, mode="write"):
                worker = threading.Thread(target=build)
                worker.start()
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], package.BuildLockUnavailable)
            self.assertFalse(output.exists())

    def test_output_cannot_overwrite_a_packaged_source_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp))
            output = source / "scripts" / "payload.zip"

            with self.assertRaisesRegex(ValueError, "packaged source data"):
                package.build_package(source, output, force=True)

            with self.assertRaisesRegex(ValueError, r"\.zip suffix"):
                package.build_package(source, Path(tmp) / "payload.bin")

    def test_force_rejects_output_symlink_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            victim = root / "victim.zip"
            victim.write_bytes(b"keep")
            output = root / "payload.zip"
            try:
                output.symlink_to(victim)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "output must not be a symlink"):
                package.build_package(source, output, force=True)

            self.assertEqual(victim.read_bytes(), b"keep")
            self.assertTrue(output.is_symlink())

    def test_frontmatter_required_fields_slug_and_version_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            manifest = source / "platforms" / "workbuddy.yaml"

            original = manifest.read_text(encoding="utf-8")
            manifest.write_text(
                original.replace("author: MobiusQuant\n", ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "author"):
                package.compose_workbuddy_skill(source)

            manifest.write_text(
                original.replace("name: openmobius-skill", "name: OpenMobius"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "archive slug"):
                package.compose_workbuddy_skill(source)

            manifest.write_text(
                original.replace("version: 0.4.0", "version: latest"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "semantic versioning"):
                package.compose_workbuddy_skill(source)

            manifest.write_text(
                original.replace("author: MobiusQuant", "author: true"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "author"):
                package.compose_workbuddy_skill(source)

    def test_unsafe_or_unsupported_zip_paths_are_rejected(self):
        unsafe_paths = (
            PurePosixPath("../escape"),
            PurePosixPath("/skills/openmobius-skill/scripts/tool.py"),
            PurePosixPath(r"skills/openmobius-skill/scripts/a\..\escape.py"),
            PurePosixPath("skills/openmobius-skill/knowledge_base/data.json"),
        )
        for archive_path in unsafe_paths:
            with self.subTest(archive_path=archive_path), self.assertRaises(
                ValueError
            ):
                with zipfile.ZipFile(io.BytesIO(), "w") as archive:
                    package._write_bytes(archive, archive_path, b"unsafe")

    def test_duplicate_mapped_archive_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp))
            duplicate = source / "references" / "workflows" / "qna.md"
            duplicate.parent.mkdir(parents=True)
            duplicate.write_text("duplicate\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate WorkBuddy archive path"):
                package.package_entries(source)

    def test_build_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            first = root / "first.zip"
            second = root / "second.zip"

            package.build_package(source, first)
            package.build_package(source, second)

            first_digest = hashlib.sha256(first.read_bytes()).hexdigest()
            second_digest = hashlib.sha256(second.read_bytes()).hexdigest()
            self.assertEqual(first_digest, second_digest)
            with zipfile.ZipFile(first) as archive:
                self.assertTrue(
                    all(
                        info.date_time == (1980, 1, 1, 0, 0, 0)
                        for info in archive.infolist()
                    ),
                )

    def test_symlinked_payload_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            outside = root / "outside.py"
            outside.write_text("SECRET = True\n", encoding="utf-8")
            link = source / "scripts" / "linked.py"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            names = {
                entry.archive_path.as_posix()
                for entry in package.package_entries(source)
            }

        self.assertNotIn("skills/openmobius-skill/scripts/linked.py", names)

    def test_symlinked_knowledge_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            outside = root / "outside-card.json"
            outside.write_text(
                json.dumps(
                    {
                        "global_card_id": "outside",
                        "canonical_term": "Outside secret",
                        "school": "ICT",
                    }
                ),
                encoding="utf-8",
            )
            card = source / "knowledge_base" / "concepts" / "one.json"
            card.unlink()
            try:
                card.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            output = root / "payload.zip"
            with self.assertRaisesRegex(ValueError, "must be a regular file"):
                package.build_package(source, output)

            self.assertFalse(output.exists())

    def test_symlinked_composition_input_fails_without_leaking_content(self):
        for relative in (Path("SKILL.body.md"), Path("platforms/workbuddy.yaml")):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self.make_source(root)
                outside = root / (relative.name + ".outside")
                outside.write_text("OUTSIDE_SECRET\n", encoding="utf-8")
                target = source / relative
                target.unlink()
                try:
                    target.symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")

                output = root / "payload.zip"
                with self.assertRaisesRegex(ValueError, "must be a regular file"):
                    package.build_package(source, output)

                self.assertFalse(output.exists())

    def test_real_repository_inventory_matches_official_resource_layout(self):
        entries = package.package_entries(ROOT)
        names = [entry.archive_path.as_posix() for entry in entries]
        skill = package.compose_workbuddy_skill(ROOT)

        self.assertGreater(len(entries), 20)
        self.assertIn(
            "skills/openmobius-skill/references/knowledge_base/schools.json",
            names,
        )
        self.assertIn("skills/openmobius-skill/scripts/kb_retrieve.py", names)
        self.assertIn(
            "skills/openmobius-skill/references/knowledge_base/"
            + package.COMPACT_V2_FILENAME,
            names,
        )
        self.assertIn(
            "skills/openmobius-skill/references/knowledge_base/term_aliases.json",
            names,
        )
        self.assertFalse(any("/concepts/" in name for name in names))
        self.assertFalse(any("/cases/" in name for name in names))
        self.assertIn("@references/workflows/qna.md", skill)
        self.assertFalse(any("/_index/" in name for name in names))
        self.assertFalse(any("/_embedding_cache/" in name for name in names))
        self.assertFalse(any("/embedding_seed_v2/" in name for name in names))
        self.assertFalse(any("interrupted" in name for name in names))
        self.assertFalse(
            any(package.INSTALL_GENERATION_MARKER in name for name in names)
        )
        workflow_payloads = [
            package._entry_payload(entry).decode("utf-8")
            for entry in entries
            if entry.archive_path.suffix == ".md"
            and "references/workflows" in entry.archive_path.as_posix()
        ]
        self.assertGreater(len(workflow_payloads), 1)
        self.assertTrue(
            all("SKILL.body.md" not in payload for payload in workflow_payloads),
        )
        self.assertTrue(
            any(
                "the loaded root `SKILL.md`" in payload
                for payload in workflow_payloads
            ),
        )
        qna_entry = next(
            entry
            for entry in entries
            if entry.archive_path.as_posix().endswith(
                "/references/workflows/qna.md"
            )
        )
        qna_payload = package._entry_payload(qna_entry).decode("utf-8")
        retrieval_commands = [
            line
            for line in qna_payload.splitlines()
            if line.lstrip().startswith("kb_retrieve.py")
        ]
        self.assertTrue(retrieval_commands)
        self.assertTrue(
            all("--search-mode lexical" in line for line in retrieval_commands)
        )
        self.assertIn(
            "expanded to `<PYTHON> scripts/kb_retrieve.py`",
            qna_payload,
        )
        self.assertNotIn(
            "skills/openmobius-skill/scripts/build_workbuddy_package.py",
            names,
        )
        for maintainer_script in package.WORKBUDDY_OMITTED_SCRIPT_FILES:
            self.assertNotIn(
                "skills/openmobius-skill/scripts/" + maintainer_script,
                names,
            )
        text_payloads = [
            package._entry_payload(entry).decode("utf-8")
            for entry in entries
            if entry.archive_path.suffix in {".md", ".py"}
        ]
        self.assertTrue(
            all("SKILL.body.md" not in payload for payload in text_payloads),
        )
        self.assertTrue(
            all(
                name.split("/")[2] in {"references", "scripts", "templates"}
                for name in names
            ),
        )

        payloads = {
            entry.archive_path.as_posix(): package._entry_payload(entry).decode("utf-8")
            for entry in entries
            if entry.archive_path in {
                package.WORKBUDDY_ATTRIBUTION_PATH,
                package.WORKBUDDY_KB_README_PATH,
                package.WORKBUDDY_KB_SCHEMAS_README_PATH,
                package.WORKBUDDY_PRIVACY_PATH,
            }
        }
        attribution = payloads[package.WORKBUDDY_ATTRIBUTION_PATH.as_posix()]
        kb_readme = payloads[package.WORKBUDDY_KB_README_PATH.as_posix()]
        schema_readme = payloads[
            package.WORKBUDDY_KB_SCHEMAS_README_PATH.as_posix()
        ]
        privacy = payloads[package.WORKBUDDY_PRIVACY_PATH.as_posix()]
        self.assertIn("This WorkBuddy package deliberately omits", attribution)
        self.assertNotIn("The repository does include", attribution)
        self.assertNotIn("installed via pip", attribution)
        self.assertNotIn("during install", attribution)
        self.assertIn("## Runtime retrieval in this WorkBuddy package", kb_readme)
        self.assertNotIn("scripts/build_index.py", kb_readme)
        self.assertNotIn("scripts/export_v2_embedding_seed.py", kb_readme)
        self.assertNotIn("--search-mode lexical --search-mode lexical", kb_readme)
        self.assertIn("WorkBuddy package", schema_readme)
        self.assertNotIn("scripts/build_knowledge_v2.py", schema_readme)
        self.assertNotIn("embedding_seed_v2/` first", schema_readme)
        self.assertIn("does not contain credentials", privacy)
        privacy_flat = " ".join(privacy.split())
        self.assertIn("Importing", privacy_flat)
        self.assertIn("Open Platform for publication", privacy_flat)
        self.assertNotIn("pip install", privacy)

        annotation_entry = next(
            entry
            for entry in entries
            if entry.archive_path.as_posix().endswith("/scripts/kb_draw_annotation.py")
        )
        annotation = package._entry_payload(annotation_entry).decode("utf-8")
        self.assertNotIn("scripts/kb_doctor.py", annotation)

        profiles_entry = next(
            entry
            for entry in entries
            if entry.archive_path.as_posix().endswith(
                "/references/workflows/analysis_profiles.md"
            )
        )
        profiles = package._entry_payload(profiles_entry).decode("utf-8")
        self.assertIn("Unavailable in this WorkBuddy package", profiles)
        self.assertNotIn(
            "An installation with only the legacy canonical collection remains usable",
            profiles.replace("\n", " "),
        )

    def test_cli_help_reports_local_artifact_without_completion_claims(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_workbuddy_package.py"),
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0)
        help_text = " ".join(completed.stdout.split())
        self.assertIn("Build a local WorkBuddy", help_text)
        self.assertIn("Destination local .zip path", help_text)
        self.assertIn(
            "does not upload, import, install, submit, or publish it",
            help_text,
        )
        self.assertNotIn("Installation complete", help_text)

    def test_real_archive_runs_school_and_source_queries_with_system_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "openmobius-workbuddy.zip"
            summary = package.build_package(ROOT, output)
            self.assertLessEqual(summary["bytes"], package.WORKBUDDY_MAX_ARCHIVE_BYTES)

            with zipfile.ZipFile(output) as archive:
                archive.extractall(root / "unpacked")
            skill_root = root / "unpacked" / "skills" / "openmobius-skill"
            knowledge_root = skill_root / "references" / "knowledge_base"
            marker = json.loads(
                (knowledge_root / package.READ_ONLY_BUNDLE_MARKER).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["format"], "openmobius-readonly-bundle")
            self.assertEqual(
                set(marker["files"]),
                {
                    package.COMPACT_V2_FILENAME,
                    "schools.json",
                    "term_aliases.json",
                },
            )
            for name, identity in marker["files"].items():
                payload = (knowledge_root / name).read_bytes()
                self.assertEqual(identity["bytes"], len(payload))
                self.assertEqual(
                    identity["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )

            denied_lock_root = root / "read-only-lock-root"
            denied_lock_root.mkdir()
            read_only_launcher = r'''
import errno
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
from _lib import build_lock
import kb_retrieve

lock_root = Path(sys.argv[1])
real_open = os.open

def deny_first_lock_creation(path, flags, *args):
    candidate = Path(path)
    if candidate.parent == lock_root and flags & os.O_CREAT:
        raise OSError(errno.EROFS, "Read-only file system", str(candidate))
    return real_open(path, flags, *args)

build_lock._build_lock_directory = lambda: lock_root
build_lock.os.open = deny_first_lock_creation
raise SystemExit(kb_retrieve.main(sys.argv[2:]))
'''

            def run_read_only_query(*arguments: str, check: bool):
                return subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-S",
                        "-c",
                        read_only_launcher,
                        str(denied_lock_root),
                        *arguments,
                    ],
                    cwd=skill_root,
                    check=check,
                    capture_output=True,
                    text=True,
                )

            inventory = run_read_only_query(
                "--layer",
                "school",
                "--list-schools",
                "--search-mode",
                "lexical",
                "--format",
                "json",
                check=True,
            )
            inventory_payload = json.loads(inventory.stdout)
            self.assertEqual(
                sum(item["count"] for item in inventory_payload["schools"]),
                2144,
            )

            evidence = run_read_only_query(
                "Order Block",
                "--layer",
                "evidence",
                "--schools",
                "SMC",
                "--sources",
                "Teach-Wuyuan",
                "--search-mode",
                "lexical",
                "--format",
                "json",
                "--top-k",
                "3",
                check=True,
            )
            evidence_payload = json.loads(evidence.stdout)
            self.assertTrue(evidence_payload)
            self.assertTrue(
                all(item["school"] == "SMC" for item in evidence_payload)
            )
            self.assertTrue(
                all(
                    item["record"]["metadata"]["source"] == "Teach-Wuyuan"
                    for item in evidence_payload
                )
            )

            canonical = run_read_only_query(
                "FVG",
                "--layer",
                "canonical",
                "--search-mode",
                "lexical",
                check=False,
            )
            self.assertNotEqual(canonical.returncode, 0)
            self.assertIn("canonical fused-card retrieval is unavailable", canonical.stderr)


if __name__ == "__main__":
    unittest.main()
