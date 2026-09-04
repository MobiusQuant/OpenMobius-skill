import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import kb_doctor


class SkillManifestDoctorTests(unittest.TestCase):
    def _make_skill(
        self,
        parent: Path,
        dirname: str = "openmobius-skill",
        *,
        name: str = "openmobius-skill",
        description: str = "Portable multi-school market analysis skill.",
    ) -> Path:
        skill_dir = parent / dirname
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n# Test skill\n",
            encoding="utf-8",
        )
        return skill_dir

    def _check(self, skill_dir: Path, **kwargs) -> tuple[bool, str]:
        output = io.StringIO()
        with (
            patch.object(kb_doctor, "SKILL_DIR", skill_dir),
            redirect_stdout(output),
        ):
            result = kb_doctor.check_skill_install(**kwargs)
        return result, output.getvalue()

    def test_valid_manifest_and_matching_directory_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._make_skill(Path(tmp))

            result, output = self._check(skill_dir, platform="hermes")

        self.assertTrue(result)
        self.assertIn("Skill manifest (hermes)", output)
        self.assertIn("manifest name: openmobius-skill", output)

    def test_check_does_not_consult_home_or_claude_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._make_skill(Path(tmp))
            with patch.object(
                kb_doctor.Path,
                "home",
                side_effect=AssertionError("Path.home must not be consulted"),
            ):
                result, output = self._check(skill_dir, platform="openclaw")

        self.assertTrue(result)
        self.assertNotIn(".claude", output)

    def test_missing_required_description_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "openmobius-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: openmobius-skill\n---\n# Test skill\n",
                encoding="utf-8",
            )

            result, output = self._check(skill_dir)

        self.assertFalse(result)
        self.assertIn("missing required frontmatter field(s): description", output)

    def test_invalid_uppercase_slug_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._make_skill(
                Path(tmp),
                dirname="OpenMobius-skill",
                name="OpenMobius-skill",
            )

            result, output = self._check(skill_dir)

        self.assertFalse(result)
        self.assertIn("Invalid skill slug", output)

    def test_branded_source_checkout_case_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._make_skill(Path(tmp), dirname="OpenMobius-skill")
            (skill_dir / ".git").mkdir()

            result, output = self._check(skill_dir)

        self.assertTrue(result)
        self.assertIn("source checkout uses branded directory casing", output)

    def test_installed_directory_must_match_manifest_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._make_skill(Path(tmp), dirname="wrong-name")

            result, output = self._check(skill_dir)

        self.assertFalse(result)
        self.assertIn("does not match manifest name", output)

    def test_expected_directory_is_checked_without_platform_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = self._make_skill(root)

            result, output = self._check(
                skill_dir,
                platform="codex",
                expected_dir=root / "different-directory",
            )

        self.assertFalse(result)
        self.assertIn("Skill directory mismatch", output)

    def test_description_over_portable_limit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self._make_skill(
                Path(tmp),
                description="x" * (kb_doctor.SKILL_DESCRIPTION_MAX_CHARS + 1),
            )

            result, output = self._check(skill_dir)

        self.assertFalse(result)
        self.assertIn("description is too long", output)


class DoctorCliTests(unittest.TestCase):
    def test_main_forwards_platform_and_expected_directory(self) -> None:
        expected = Path("/tmp/openmobius-skill-test")
        other_checks = (
            "check_env_python",
            "check_python_packages",
            "check_embedding_model",
            "check_kb_index",
            "check_playwright_chromium",
            "check_cjk_fonts",
            "check_mobius_api",
        )
        output = io.StringIO()
        with ExitStack() as stack:
            for check_name in other_checks:
                stack.enter_context(
                    patch.object(kb_doctor, check_name, return_value=True),
                )
            skill_check = stack.enter_context(
                patch.object(kb_doctor, "check_skill_install", return_value=True),
            )
            stack.enter_context(redirect_stdout(output))

            result = kb_doctor.main(
                ["--platform", "workbuddy", "--expected-dir", str(expected)],
            )

        self.assertEqual(result, 0)
        skill_check.assert_called_once_with("workbuddy", expected)


class EmbeddingCacheDoctorTests(unittest.TestCase):
    def test_cache_resolution_matches_installer_precedence(self) -> None:
        home = Path("/test/home")
        self.assertEqual(
            kb_doctor.huggingface_hub_cache(environ={}, home=home),
            home / ".cache/huggingface/hub",
        )
        self.assertEqual(
            kb_doctor.huggingface_hub_cache(
                environ={"HF_HOME": "/custom/hf-home"},
                home=home,
            ),
            Path("/custom/hf-home/hub"),
        )
        self.assertEqual(
            kb_doctor.huggingface_hub_cache(
                environ={
                    "HF_HOME": "/ignored/hf-home",
                    "HF_HUB_CACHE": "/custom/hub-cache",
                },
                home=home,
            ),
            Path("/custom/hub-cache"),
        )

    def test_configured_hf_home_with_pinned_snapshot_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hf_home = Path(tmp) / "hf-home"
            snapshot = (
                hf_home
                / "hub/models--nomic-ai--nomic-embed-text-v1.5/snapshots"
                / kb_doctor.NOMIC_MODEL_REVISION
            )
            snapshot.mkdir(parents=True)
            (snapshot / "model.safetensors").write_bytes(b"weights")
            output = io.StringIO()
            with (
                patch.dict(
                    kb_doctor.os.environ,
                    {"HF_HOME": str(hf_home), "HF_HUB_CACHE": ""},
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                result = kb_doctor.check_embedding_model()

        self.assertTrue(result)
        self.assertIn(kb_doctor.NOMIC_MODEL_REVISION, output.getvalue())


if __name__ == "__main__":
    unittest.main()
