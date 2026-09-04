import argparse
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import install


ROOT = Path(__file__).resolve().parents[1]


class InstallerPlatformRegistryTests(unittest.TestCase):
    def test_brand_and_portable_skill_slug_are_separate(self) -> None:
        self.assertEqual(install.DISPLAY_NAME, "OpenMobius-skill")
        self.assertEqual(install.SKILL_SLUG, "openmobius-skill")
        self.assertEqual(install.SKILL_NAME, install.SKILL_SLUG)
        self.assertEqual(install.LEGACY_SKILL_NAME, install.DISPLAY_NAME)
        self.assertRegex(
            install.SKILL_SLUG,
            re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
        )

    def test_registry_is_the_single_six_platform_catalog(self) -> None:
        expected = (
            "claude-code",
            "codex",
            "openclaw",
            "hermes",
            "cursor",
            "workbuddy",
        )
        self.assertEqual(install.PLATFORM_NAMES, expected)
        self.assertEqual(tuple(install.PLATFORM_REGISTRY), expected)
        self.assertEqual(
            install.PLATFORM_DISPLAY_NAMES,
            {
                name: spec.display_name
                for name, spec in install.PLATFORM_REGISTRY.items()
            },
        )

    def test_default_targets_follow_each_platform_contract(self) -> None:
        home = Path("/test/home")
        targets = {
            name: spec.default_target(home=home, environ={})
            for name, spec in install.PLATFORM_REGISTRY.items()
        }
        self.assertEqual(
            targets,
            {
                "claude-code": home / ".claude/skills/openmobius-skill",
                "codex": home / ".agents/skills/openmobius-skill",
                "openclaw": home / ".openclaw/skills/openmobius-skill",
                "hermes": home
                / ".hermes/skills/market-data/openmobius-skill",
                "cursor": home / ".cursor/skills/openmobius-skill",
                "workbuddy": None,
            },
        )

    def test_backward_compatible_defaults_exclude_explicit_only_host(self) -> None:
        expected = (
            "claude-code",
            "codex",
            "openclaw",
            "hermes",
            "cursor",
        )
        self.assertEqual(install.DEFAULT_TARGET_PLATFORMS, expected)
        self.assertEqual(tuple(install.PLATFORM_DEFAULTS), expected)
        self.assertNotIn("workbuddy", install.PLATFORM_DEFAULTS)
        for target in install.PLATFORM_DEFAULTS.values():
            self.assertEqual(target.name, "openmobius-skill")

    def test_openclaw_and_hermes_environment_roots_are_respected(self) -> None:
        home = Path("/test/home")
        environ = {
            "OPENCLAW_STATE_DIR": "/state/openclaw",
            "HERMES_HOME": "/state/hermes",
        }
        openclaw = install.PLATFORM_REGISTRY["openclaw"]
        hermes = install.PLATFORM_REGISTRY["hermes"]
        self.assertEqual(
            openclaw.default_target(home=home, environ=environ),
            Path("/state/openclaw/skills/openmobius-skill"),
        )
        self.assertEqual(
            hermes.default_target(home=home, environ=environ),
            Path("/state/hermes/skills/market-data/openmobius-skill"),
        )
        self.assertEqual(
            openclaw.detection_root(home=home, environ=environ),
            Path("/state/openclaw"),
        )
        self.assertEqual(
            hermes.detection_root(home=home, environ=environ),
            Path("/state/hermes"),
        )
        self.assertEqual(
            openclaw.legacy_target(home=home, environ=environ),
            Path("/state/openclaw/skills/OpenMobius-skill"),
        )
        self.assertEqual(
            hermes.legacy_target(home=home, environ=environ),
            Path("/state/hermes/skills/market-data/OpenMobius-skill"),
        )

    def test_registry_limits_openclaw_and_hermes_to_linux_and_macos(self) -> None:
        expected = ("Linux", "Darwin")
        self.assertEqual(
            install.PLATFORM_REGISTRY["openclaw"].supported_host_systems,
            expected,
        )
        self.assertEqual(
            install.PLATFORM_REGISTRY["hermes"].supported_host_systems,
            expected,
        )
        for name in ("claude-code", "codex", "cursor", "workbuddy"):
            with self.subTest(platform=name):
                self.assertIsNone(
                    install.PLATFORM_REGISTRY[name].supported_host_systems
                )

    def test_empty_environment_roots_fall_back_to_home(self) -> None:
        home = Path("/test/home")
        environ = {"OPENCLAW_STATE_DIR": "  ", "HERMES_HOME": ""}
        self.assertEqual(
            install.PLATFORM_REGISTRY["openclaw"].default_target(
                home=home, environ=environ
            ),
            home / ".openclaw/skills/openmobius-skill",
        )
        self.assertEqual(
            install.PLATFORM_REGISTRY["hermes"].default_target(
                home=home, environ=environ
            ),
            home / ".hermes/skills/market-data/openmobius-skill",
        )

    def test_copy_payload_includes_agents_metadata(self) -> None:
        self.assertIn("agents", install.COPY_ENTRIES)

    def test_huggingface_cache_precedence_matches_hub_contract(self) -> None:
        home = Path("/test/home")
        self.assertEqual(
            install._huggingface_hub_cache(environ={}, home=home),
            home / ".cache/huggingface/hub",
        )
        self.assertEqual(
            install._huggingface_hub_cache(
                environ={"HF_HOME": "/custom/hf-home"},
                home=home,
            ),
            Path("/custom/hf-home/hub"),
        )
        self.assertEqual(
            install._huggingface_hub_cache(
                environ={
                    "HF_HOME": "/ignored/hf-home",
                    "HF_HUB_CACHE": "/custom/hub-cache",
                },
                home=home,
            ),
            Path("/custom/hub-cache"),
        )

    def test_every_generated_frontmatter_uses_the_slug(self) -> None:
        with (
            patch.object(install, "PLATFORMS_DIR", ROOT / "platforms"),
            patch.object(install, "SKILL_BODY_MD", ROOT / "SKILL.body.md"),
        ):
            for platform_name in install.PLATFORM_NAMES:
                with self.subTest(platform=platform_name):
                    document = install._compose_skill_md(platform_name)
                    self.assertTrue(
                        document.startswith("---\nname: openmobius-skill\n")
                    )


class InstallerPlatformDetectionTests(unittest.TestCase):
    def test_auto_detection_includes_cursor_and_never_guesses_workbuddy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for directory in (".claude", ".codex", ".cursor", ".workbuddy"):
                (home / directory).mkdir()

            with (
                patch.object(install.Path, "home", return_value=home),
                patch.dict(
                    os.environ,
                    {"OPENCLAW_STATE_DIR": "", "HERMES_HOME": ""},
                    clear=False,
                ),
            ):
                detected = install.detect_platforms()

        self.assertEqual(detected, ["claude-code", "codex", "cursor"])
        self.assertNotIn("workbuddy", detected)

    def test_auto_detection_respects_configured_state_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            openclaw_state = root / "openclaw-state"
            hermes_home = root / "hermes-home"
            home.mkdir()
            openclaw_state.mkdir()
            hermes_home.mkdir()

            with (
                patch.object(install.Path, "home", return_value=home),
                patch.dict(
                    os.environ,
                    {
                        "OPENCLAW_STATE_DIR": str(openclaw_state),
                        "HERMES_HOME": str(hermes_home),
                    },
                    clear=False,
                ),
            ):
                detected = install.detect_platforms()

        self.assertEqual(detected, ["openclaw", "hermes"])

    def test_codex_detection_uses_codex_host_root_not_agents_target_root(self) -> None:
        home = Path("/test/home")
        spec = install.PLATFORM_REGISTRY["codex"]
        self.assertEqual(spec.detection_root(home=home, environ={}), home / ".codex")
        self.assertEqual(
            spec.default_target(home=home, environ={}),
            home / ".agents/skills/openmobius-skill",
        )


class InstallerWorkBuddyTests(unittest.TestCase):
    def test_workbuddy_requires_an_explicit_target(self) -> None:
        self.assertIsNone(install._resolve_target("workbuddy", None))
        with self.assertRaises(install.PlatformTargetError) as caught:
            install._resolve_target_for_operation("workbuddy", None)
        message = str(caught.exception)
        self.assertIn("--target-dir", message)
        self.assertIn("developer staging tree", message)
        self.assertIn("Skills > Add Skill > Upload Skill", message)
        self.assertIn("separate publishing workflow", message)
        self.assertIn("will not guess a ~/.workbuddy path", message)

    def test_workbuddy_accepts_an_explicit_target(self) -> None:
        target = Path("/selected/by/user/workbuddy-skill")
        self.assertEqual(
            install._resolve_target_for_operation("workbuddy", target), target
        )

    def test_cli_fails_before_install_when_workbuddy_target_is_missing(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(install, "_run_single_install") as run_install,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = install.main(["--platform", "workbuddy"])

        self.assertEqual(result, 2)
        run_install.assert_not_called()
        self.assertIn("--target-dir", stderr.getvalue())
        self.assertIn("developer staging", stderr.getvalue())
        self.assertIn("Skills > Add Skill > Upload Skill", stderr.getvalue())

    def test_workbuddy_summary_reports_staging_not_installation(self) -> None:
        target = Path("/selected/by/user/workbuddy-stage")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            install.print_summary(
                {"Doctor": True},
                all_ok=True,
                platform_name="workbuddy",
                target_dir=target,
            )

        output = stdout.getvalue()
        self.assertIn("WorkBuddy staging validation complete", output)
        self.assertIn("Staged at:", output)
        self.assertIn(str(target), output)
        self.assertIn("did not import, install, or publish", output)
        self.assertIn("Skills > Add Skill > Upload Skill", output)
        self.assertNotIn("Installation complete", output)
        self.assertNotIn("use the skill in WorkBuddy", output)
        self.assertNotIn("Installed to:", output)

    def test_workbuddy_failed_summary_uses_staging_language(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            install.print_summary(
                {"Doctor": False},
                all_ok=False,
                platform_name="workbuddy",
                target_dir=Path("/selected/by/user/workbuddy-stage"),
            )

        output = stdout.getvalue()
        self.assertIn("WorkBuddy staging validation finished with issues", output)
        self.assertNotIn("Installation finished with issues", output)

    def test_workbuddy_update_reports_staging_not_platform_update(self) -> None:
        target = Path("/selected/by/user/workbuddy-stage")
        args = SimpleNamespace(platform="workbuddy")
        stdout = io.StringIO()
        with (
            patch.object(
                install,
                "_resolve_target_for_operation",
                return_value=target,
            ),
            patch.object(install, "_run_single_install", return_value=0),
            redirect_stdout(stdout),
        ):
            result = install.cmd_update(
                ["workbuddy"],
                target,
                no_pull=True,
                rebuild_index=False,
                args=args,
            )

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("WorkBuddy staging updater", output)
        self.assertIn("Updating WorkBuddy developer staging", output)
        self.assertIn("WorkBuddy staging update complete", output)
        self.assertNotIn("✓ Update complete", output)

    def test_workbuddy_uninstall_only_removes_developer_staging(self) -> None:
        target = Path("/selected/by/user/missing-workbuddy-stage")
        stdout = io.StringIO()
        with (
            patch.object(
                install,
                "_resolve_target_for_operation",
                return_value=target,
            ),
            patch.object(
                install,
                "knowledge_base_build_lock",
                side_effect=lambda *args, **kwargs: nullcontext(),
            ),
            redirect_stdout(stdout),
        ):
            result = install.cmd_uninstall(
                ["workbuddy"],
                target,
                full=False,
                purge=False,
                yes_i_know=False,
            )

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("WorkBuddy staging remover", output)
        self.assertIn("no developer staging tree", output)
        self.assertIn("WorkBuddy developer staging removed", output)
        self.assertIn("did not uninstall an imported or marketplace Skill", output)
        self.assertNotIn("✓ Uninstall complete", output)

    def test_cli_dispatches_workbuddy_when_target_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "chosen-target"
            with (
                patch.object(install, "_run_single_install", return_value=0) as run,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = install.main(
                    ["--platform", "workbuddy", "--target-dir", str(target)]
                )

        self.assertEqual(result, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0].platform, "workbuddy")
        self.assertEqual(run.call_args.args[0].target_dir, str(target))


class InstallerMutationSafetyTests(unittest.TestCase):
    def test_broad_targets_are_rejected_for_every_mutating_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with patch.object(install.Path, "home", return_value=home):
                for operation in ("install", "update", "uninstall"):
                    for target in (
                        Path(target)
                        for target in ("/", home, "/home", "/media", "/mnt", "/tmp")
                    ):
                        with self.subTest(operation=operation, target=target):
                            with self.assertRaises(install.PlatformTargetError):
                                install._resolve_target_for_operation(
                                    "codex",
                                    target,
                                    operation=operation,
                                )

    def test_source_checkout_cannot_be_updated_or_uninstalled(self) -> None:
        for operation in ("update", "uninstall"):
            with self.subTest(operation=operation):
                with self.assertRaises(install.PlatformTargetError):
                    install._resolve_target_for_operation(
                        "codex",
                        install.SOURCE_DIR,
                        operation=operation,
                    )
        self.assertEqual(
            install._resolve_target_for_operation(
                "codex",
                install.SOURCE_DIR,
                operation="install",
            ),
            install.SOURCE_DIR,
        )

    def test_managed_install_can_bootstrap_pulled_update_but_not_no_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "openmobius-skill"
            target.mkdir()
            install._write_install_state(
                target,
                platform_name="codex",
                owned_files=["install.py"],
            )
            with patch.object(install, "SOURCE_DIR", target):
                resolved = install._resolve_target_for_operation(
                    "codex",
                    target,
                    operation="update",
                    allow_managed_source_update=True,
                )
                self.assertEqual(resolved, target.resolve())
                with self.assertRaises(install.PlatformTargetError):
                    install._resolve_target_for_operation(
                        "codex",
                        target,
                        operation="update",
                    )

                with (
                    patch.object(install, "cmd_update", return_value=0) as update,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    result = install.main(
                        [
                            "--update",
                            "--platform",
                            "codex",
                            "--target-dir",
                            str(target),
                        ]
                    )
                self.assertEqual(result, 0)
                update.assert_called_once()

                with (
                    patch.object(install, "cmd_update") as no_pull_update,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    result = install.main(
                        [
                            "--update",
                            "--no-pull",
                            "--platform",
                            "codex",
                            "--target-dir",
                            str(target),
                        ]
                    )
                self.assertEqual(result, 2)
                no_pull_update.assert_not_called()

    def test_target_inside_source_checkout_is_rejected(self) -> None:
        target = install.SOURCE_DIR / "scripts" / "openmobius-descendant-probe"
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(install.PlatformTargetError, "inside the source"):
            install._resolve_target_for_operation(
                "codex",
                target,
                operation="install",
            )
        self.assertFalse(target.exists())

    def test_targets_cannot_overlap_external_lock_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_root = root / "private-lock-root"
            with patch.object(
                install,
                "knowledge_base_lock_root",
                return_value=lock_root,
            ):
                for target in (root, lock_root, lock_root / "child"):
                    with self.subTest(target=target):
                        with self.assertRaisesRegex(
                            install.PlatformTargetError,
                            "lock root",
                        ):
                            install._resolve_target_for_operation(
                                "codex",
                                target,
                                operation="install",
                            )
            self.assertFalse(lock_root.exists())

    def test_update_rejects_a_symlink_target_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real" / "openmobius-skill"
            real.mkdir(parents=True)
            link = root / "linked-openmobius-skill"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(install.PlatformTargetError, "symlink"):
                install._resolve_target_for_operation(
                    "codex",
                    link,
                    operation="update",
                )

    def test_uninstall_refuses_non_owned_directory_before_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "not-the-skill"
            target.mkdir()
            (target / "important.txt").write_text("keep", encoding="utf-8")
            with (
                patch.object(install, "_remove_path") as remove,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = install.cmd_uninstall(
                    platforms=["codex"],
                    target_dir=target,
                    full=False,
                    purge=False,
                    yes_i_know=False,
                )
        self.assertEqual(result, 1)
        remove.assert_not_called()

    def test_uninstall_refuses_busy_target_without_removing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "installed" / "openmobius-skill"
            (target / "knowledge_base").mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with (
                install.knowledge_base_build_lock(
                    install._installer_operation_resource(target),
                    mode="read",
                ),
                patch.object(install, "_remove_path") as remove,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = install.cmd_uninstall(
                    platforms=["codex"],
                    target_dir=target,
                    full=False,
                    purge=False,
                    yes_i_know=False,
                )

            self.assertEqual(result, 1)
            remove.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


class InstallerPayloadSyncTests(unittest.TestCase):
    DIRECTORIES = {
        "agents", "scripts", "workflows", "knowledge_base", "platforms",
        "evals", "docs",
    }

    def _make_source(self, root: Path, *, include_stale: bool) -> Path:
        source = root / "source"
        source.mkdir()
        for entry in install.COPY_ENTRIES:
            path = source / entry
            if entry in self.DIRECTORIES:
                path.mkdir()
                (path / "release.txt").write_text(entry, encoding="utf-8")
            else:
                path.write_text(entry, encoding="utf-8")
        scripts = source / "scripts"
        if include_stale:
            (scripts / "deleted_upstream.py").write_text("old", encoding="utf-8")
        else:
            (scripts / "new_upstream.py").write_text("new", encoding="utf-8")
        return source

    def test_update_mirrors_owned_files_and_preserves_runtime_and_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=True)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            self.assertTrue(
                (target / "knowledge_base" / install.INSTALL_GENERATION_MARKER).is_file()
            )
            (target / ".venv" / "bin").mkdir(parents=True)
            (target / ".venv" / "bin" / "python").write_text("venv", encoding="utf-8")
            (target / "knowledge_base" / "_index").mkdir()
            (target / "knowledge_base" / "_index" / "db").write_text("index", encoding="utf-8")
            (target / "knowledge_base" / "_embedding_cache").mkdir()
            (target / "knowledge_base" / "_embedding_cache" / "db").write_text("cache", encoding="utf-8")
            (target / "user-config.json").write_text("user", encoding="utf-8")
            (target / "scripts" / "user-extension.py").write_text("user", encoding="utf-8")

            (source / "scripts" / "deleted_upstream.py").unlink()
            (source / "scripts" / "new_upstream.py").write_text("new", encoding="utf-8")
            install.copy_source_to_target(source, target, platform_name="codex")

            self.assertFalse((target / "scripts" / "deleted_upstream.py").exists())
            self.assertTrue((target / "scripts" / "new_upstream.py").is_file())
            self.assertEqual((target / ".venv/bin/python").read_text(), "venv")
            self.assertEqual((target / "knowledge_base/_index/db").read_text(), "index")
            self.assertEqual(
                (target / "knowledge_base/_embedding_cache/db").read_text(),
                "cache",
            )
            self.assertEqual((target / "user-config.json").read_text(), "user")
            self.assertEqual(
                (target / "scripts/user-extension.py").read_text(),
                "user",
            )

    def test_staging_failure_leaves_live_target_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=True)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            with patch.object(
                install,
                "_copy_payload_into_staging",
                side_effect=OSError("injected staging failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    install.copy_source_to_target(
                        source,
                        target,
                        platform_name="codex",
                    )
            after = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_busy_source_fails_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"

            with install.knowledge_base_build_lock(
                source / "knowledge_base",
                mode="write",
            ):
                with self.assertRaises(install.BuildLockUnavailable):
                    install.copy_source_to_target(
                        source,
                        target,
                        platform_name="codex",
                    )

            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())

    def test_busy_target_fails_before_existing_install_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            marker_before = (target / install.INSTALL_STATE_FILE).read_bytes()

            with install.knowledge_base_build_lock(
                target / "knowledge_base",
                mode="read",
            ):
                with self.assertRaises(install.BuildLockUnavailable):
                    install.copy_source_to_target(
                        source,
                        target,
                        platform_name="codex",
                    )

            self.assertEqual(
                (target / install.INSTALL_STATE_FILE).read_bytes(),
                marker_before,
            )
            self.assertFalse(any(target.parent.glob(f".{target.name}.stage-*")))

    def test_source_symlink_is_rejected_before_live_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            marker_before = (target / install.INSTALL_STATE_FILE).read_bytes()
            (source / "scripts" / "outside-link").symlink_to(root / "outside")
            with self.assertRaisesRegex(ValueError, "symlink"):
                install.copy_source_to_target(
                    source,
                    target,
                    platform_name="codex",
                )
            self.assertEqual(
                (target / install.INSTALL_STATE_FILE).read_bytes(),
                marker_before,
            )

    def test_direct_copy_rejects_target_inside_source_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._make_source(Path(tmp), include_stale=False)
            target = source / "scripts" / "openmobius-skill"
            before = sorted(path.relative_to(source) for path in source.rglob("*"))
            with self.assertRaisesRegex(ValueError, "overlaps"):
                install.copy_source_to_target(
                    source,
                    target,
                    platform_name="codex",
                )
            after = sorted(path.relative_to(source) for path in source.rglob("*"))
            self.assertEqual(after, before)
            self.assertFalse(target.exists())

    def test_transaction_artifacts_are_excluded_from_release_payload(self) -> None:
        names = (
            "._index.build-deadbeef",
            "._index.backup-deadbeef",
            "._cards.build-deadbeef",
            "._cards.backup-deadbeef",
            ".embedding_seed_v2.build-deadbeef",
            ".embedding_seed_v2.backup-deadbeef",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            for name in names:
                artifact = source / "knowledge_base" / name
                artifact.mkdir()
                (artifact / "must-not-copy").write_text("temp", encoding="utf-8")
            owned = install._validate_release_source(source)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")

            for name in names:
                self.assertFalse(any(name in value for value in owned))
                self.assertFalse((target / "knowledge_base" / name).exists())
            self.assertFalse(install._is_copy_excluded("index.build-not-a-prefix"))

    def test_recovery_restores_complete_backup_after_interrupted_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            (target / "user-data").write_text("preserved", encoding="utf-8")
            transaction_id = "recovery-test"
            backup = target.parent / f".{target.name}.backup-{transaction_id}"
            stage = target.parent / f".{target.name}.stage-{transaction_id}"
            target.replace(backup)
            stage.mkdir()
            install._write_staging_state(
                stage,
                target,
                transaction_id,
                preserved_paths=(Path("user-data"),),
            )
            (backup / "user-data").replace(stage / "user-data")
            (stage / "partial").write_text("partial", encoding="utf-8")

            install._recover_copy_transaction(target)

            self.assertEqual((target / "user-data").read_text(), "preserved")
            self.assertFalse(backup.exists())
            self.assertFalse(stage.exists())

    def test_recovery_finishes_verified_commit_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            transaction_id = "committed-test"
            backup = target.parent / f".{target.name}.backup-{transaction_id}"
            target.replace(backup)
            shutil.copytree(backup, target)
            install._write_staging_state(target, target, transaction_id)
            (target / "committed").write_text("new", encoding="utf-8")

            install._recover_copy_transaction(target)

            self.assertEqual((target / "committed").read_text(), "new")
            self.assertFalse(backup.exists())
            self.assertFalse((target / install.STAGING_STATE_FILE).exists())

    def test_ambiguous_live_and_backup_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            transaction_id = "ambiguous-test"
            backup = target.parent / f".{target.name}.backup-{transaction_id}"
            shutil.copytree(target, backup)

            with self.assertRaisesRegex(RuntimeError, "manual review"):
                install._recover_copy_transaction(target)

            self.assertTrue(target.is_dir())
            self.assertTrue(backup.is_dir())

    def test_promotion_rename_failure_restores_live_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            (target / "live-sentinel").write_text("old", encoding="utf-8")
            stage = target.parent / f".{target.name}.stage-rename-failure"
            shutil.copytree(target, stage)
            install._write_staging_state(stage, target, "rename-failure")
            original_replace = Path.replace

            def injected_replace(path, destination):
                if path == stage and Path(destination) == target:
                    raise OSError("injected promote failure")
                return original_replace(path, destination)

            with patch.object(Path, "replace", new=injected_replace):
                with self.assertRaisesRegex(OSError, "injected promote"):
                    install._promote_staged_target(stage, target)

            self.assertEqual((target / "live-sentinel").read_text(), "old")
            self.assertFalse(any(target.parent.glob(f".{target.name}.backup-*")))

    def test_keyboard_interrupt_during_promotion_restores_preserved_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "openmobius-skill"
            target.mkdir()
            (target / "important-user-data").write_text("keep", encoding="utf-8")
            stage = root / ".openmobius-skill.stage-interrupt"
            stage.mkdir()
            (stage / "new-release").write_text("new", encoding="utf-8")
            install._write_staging_state(
                stage,
                target,
                "interrupt",
                preserved_paths=(Path("important-user-data"),),
            )
            original_replace = Path.replace

            def injected_replace(path, destination):
                if path == stage and Path(destination) == target:
                    raise KeyboardInterrupt("injected interrupt")
                return original_replace(path, destination)

            with patch.object(Path, "replace", new=injected_replace):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected"):
                    install._promote_staged_target(stage, target)

            self.assertEqual(
                (target / "important-user-data").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertFalse(any(root.glob(".openmobius-skill.backup-*")))

    def test_failed_rollback_keeps_staging_and_backup_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root, include_stale=False)
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            (target / "important-user-data").write_text("keep", encoding="utf-8")
            original_replace = Path.replace

            def fail_promotion(path, destination):
                path = Path(path)
                if (
                    path.name.startswith(f".{target.name}.stage-")
                    and Path(destination) == target
                ):
                    raise OSError("injected promotion failure")
                return original_replace(path, destination)

            with (
                patch.object(Path, "replace", new=fail_promotion),
                patch.object(
                    install,
                    "_rollback_preserved_moves",
                    side_effect=OSError("injected rollback failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery evidence"):
                    install.copy_source_to_target(
                        source,
                        target,
                        platform_name="codex",
                    )

            stages = list(target.parent.glob(f".{target.name}.stage-*"))
            backups = list(target.parent.glob(f".{target.name}.backup-*"))
            self.assertEqual(len(stages), 1)
            self.assertEqual(len(backups), 1)
            self.assertFalse(target.exists())
            self.assertTrue(
                any(
                    (path / "important-user-data").is_file()
                    for path in (stages[0], backups[0])
                )
            )


class InstallerGenerationMarkerTests(unittest.TestCase):
    def _write_minimal_verified_index(self, target: Path) -> None:
        import sqlite3

        index = target / "knowledge_base" / "_index"
        index.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(index / "chroma.sqlite3")
        database.execute("CREATE TABLE smoke_test (value INTEGER)")
        database.commit()
        database.close()
        (index / "index_manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 2,
                    "index_schema_version": 3,
                    "v2_input_fingerprint": "a" * 64,
                    "canonical_input_fingerprint": "b" * 64,
                    "collections": {
                        "school_knowledge_v2": {
                            "schema_version": 3,
                            "layer": "school",
                            "created": True,
                            "count": 1,
                        },
                        "source_evidence_v2": {
                            "schema_version": 3,
                            "layer": "evidence",
                            "created": True,
                            "count": 1,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_complete_generation_rejects_missing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "installed" / "openmobius-skill"
            (target / "knowledge_base").mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "marker is missing"):
                install._complete_install_generation(
                    target,
                    expected_generation_id="expected-generation",
                )

    def test_complete_generation_rejects_corrupt_database_and_missing_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = InstallerPayloadSyncTests()._make_source(
                root,
                include_stale=False,
            )
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            marker = target / "knowledge_base" / install.INSTALL_GENERATION_MARKER
            generation_id = install._read_install_generation_marker(target)[
                "generation_id"
            ]
            index = target / "knowledge_base" / "_index"
            index.mkdir()
            (index / "chroma.sqlite3").write_bytes(b"not-sqlite")
            manifest_path = index / "index_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_version": 2,
                        "index_schema_version": 3,
                        "v2_input_fingerprint": "a" * 64,
                        "canonical_input_fingerprint": "b" * 64,
                        "collections": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "populated school_knowledge_v2"):
                install._complete_install_generation(
                    target,
                    expected_generation_id=generation_id,
                )
            self.assertTrue(marker.is_file())

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["collections"] = {
                "school_knowledge_v2": {
                    "schema_version": 3,
                    "layer": "school",
                    "created": True,
                    "count": 1,
                },
                "source_evidence_v2": {
                    "schema_version": 3,
                    "layer": "evidence",
                    "created": True,
                    "count": 1,
                },
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid Chroma database"):
                install._complete_install_generation(
                    target,
                    expected_generation_id=generation_id,
                )
            self.assertTrue(marker.is_file())

    def test_complete_generation_requires_verified_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = InstallerPayloadSyncTests()._make_source(
                root,
                include_stale=False,
            )
            target = root / "installed" / "openmobius-skill"
            install.copy_source_to_target(source, target, platform_name="codex")
            marker = target / "knowledge_base" / install.INSTALL_GENERATION_MARKER
            generation_id = install._read_install_generation_marker(target)[
                "generation_id"
            ]

            with self.assertRaisesRegex(ValueError, "Chroma database"):
                install._complete_install_generation(
                    target,
                    expected_generation_id=generation_id,
                )
            self.assertTrue(marker.is_file())

            self._write_minimal_verified_index(target)
            with self.assertRaisesRegex(ValueError, "generation changed"):
                install._complete_install_generation(
                    target,
                    expected_generation_id="another-generation",
                )
            self.assertTrue(marker.is_file())
            self.assertTrue(
                install._complete_install_generation(
                    target,
                    expected_generation_id=generation_id,
                )
            )
            self.assertFalse(marker.exists())


class InstallerDoctorTests(unittest.TestCase):
    def test_doctor_receives_platform_and_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doctor = root / "scripts" / "kb_doctor.py"
            doctor.parent.mkdir()
            doctor.write_text("# test\n", encoding="utf-8")
            python = root / ".venv" / "bin" / "python"
            target = root / "installed" / "openmobius-skill"
            with (
                patch.object(install, "SKILL_DIR", root),
                patch.object(install, "VENV_PY", python),
                patch.object(
                    install.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ) as run,
                redirect_stdout(io.StringIO()),
            ):
                result = install.run_doctor("codex", target)

        self.assertTrue(result)
        run.assert_called_once_with(
            [
                str(python),
                str(doctor),
                "--platform",
                "codex",
                "--expected-dir",
                str(target.resolve()),
            ],
            check=False,
        )

    def test_nonzero_doctor_result_fails_install_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doctor = root / "scripts" / "kb_doctor.py"
            doctor.parent.mkdir()
            doctor.write_text("# test\n", encoding="utf-8")
            with (
                patch.object(install, "SKILL_DIR", root),
                patch.object(install, "VENV_PY", root / "python"),
                patch.object(
                    install.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=7),
                ),
                redirect_stdout(io.StringIO()),
            ):
                result = install.run_doctor()

        self.assertFalse(result)

    def test_missing_or_crashed_doctor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(install, "SKILL_DIR", root),
                redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(install.run_doctor())

            doctor = root / "scripts" / "kb_doctor.py"
            doctor.parent.mkdir()
            doctor.write_text("# test\n", encoding="utf-8")
            with (
                patch.object(install, "SKILL_DIR", root),
                patch.object(install, "VENV_PY", root / "python"),
                patch.object(
                    install.subprocess,
                    "run",
                    side_effect=OSError("cannot execute"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(install.run_doctor())


class InstallerLegacyTargetTests(unittest.TestCase):
    def test_legacy_uppercase_targets_are_parsed_without_mutation(self) -> None:
        home = Path("/test/home")
        expected = {
            "claude-code": home / ".claude/skills/OpenMobius-skill",
            "codex": home / ".codex/skills/OpenMobius-skill",
            "openclaw": home / ".openclaw/skills/OpenMobius-skill",
            "hermes": home
            / ".hermes/skills/market-data/OpenMobius-skill",
            "cursor": None,
            "workbuddy": None,
        }
        self.assertEqual(
            {
                name: spec.legacy_target(home=home, environ={})
                for name, spec in install.PLATFORM_REGISTRY.items()
            },
            expected,
        )

    def test_custom_openclaw_state_refuses_single_legacy_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state = root / "openclaw-state"
            home.mkdir()
            legacy = state / "skills/OpenMobius-skill"
            legacy.mkdir(parents=True)

            with (
                patch.object(install.Path, "home", return_value=home),
                patch.dict(
                    os.environ,
                    {"OPENCLAW_STATE_DIR": str(state)},
                    clear=False,
                ),
            ):
                with self.assertRaises(install.PlatformTargetError) as caught:
                    install._resolve_target_for_operation("openclaw", None)

            message = str(caught.exception)
            self.assertIn(str(legacy), message)
            self.assertIn(
                str(state / "skills/openmobius-skill"),
                message,
            )
            self.assertIn("a legacy install exists", message)

    def test_custom_hermes_home_reports_current_and_legacy_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state = root / "hermes-home"
            home.mkdir()
            parent = state / "skills/market-data"
            legacy = parent / "OpenMobius-skill"
            current = parent / "openmobius-skill"
            legacy.mkdir(parents=True)
            current.mkdir(parents=True)

            with (
                patch.object(install.Path, "home", return_value=home),
                patch.dict(
                    os.environ,
                    {"HERMES_HOME": str(state)},
                    clear=False,
                ),
            ):
                issue = install._legacy_target_issue("hermes")

            self.assertIsNotNone(issue)
            self.assertIn("both the current target", issue)
            self.assertIn(str(current), issue)
            self.assertIn(str(legacy), issue)

    def test_default_operation_refuses_legacy_install_and_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".codex/skills/OpenMobius-skill"
            legacy.mkdir(parents=True)
            marker = legacy / "keep-me"
            marker.write_text("legacy", encoding="utf-8")

            with patch.object(install.Path, "home", return_value=home):
                with self.assertRaises(install.PlatformTargetError) as caught:
                    install._resolve_target_for_operation("codex", None)

            message = str(caught.exception)
            self.assertIn(str(legacy), message)
            self.assertIn(
                str(home / ".agents/skills/openmobius-skill"), message
            )
            self.assertIn("Refusing to modify either path automatically", message)
            self.assertTrue(marker.is_file())

    def test_conflicting_current_and_legacy_targets_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".claude/skills/OpenMobius-skill"
            current = home / ".claude/skills/openmobius-skill"
            legacy.mkdir(parents=True)
            current.mkdir(parents=True)

            with patch.object(install.Path, "home", return_value=home):
                issue = install._legacy_target_issue("claude-code")

            self.assertIsNotNone(issue)
            self.assertIn("both the current target", issue)
            self.assertTrue(legacy.is_dir())
            self.assertTrue(current.is_dir())

    def test_explicit_legacy_target_is_an_intentional_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".codex/skills/OpenMobius-skill"
            legacy.mkdir(parents=True)

            with patch.object(install.Path, "home", return_value=home):
                resolved = install._resolve_target_for_operation("codex", legacy)

            self.assertEqual(resolved, legacy)
            self.assertTrue(legacy.is_dir())

    def test_uninstall_refuses_to_delete_legacy_default_implicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".codex/skills/OpenMobius-skill"
            legacy.mkdir(parents=True)

            with (
                patch.object(install.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = install.cmd_uninstall(
                    platforms=["codex"],
                    target_dir=None,
                    full=False,
                    purge=False,
                    yes_i_know=False,
                )

            self.assertEqual(result, 1)
            self.assertTrue(legacy.is_dir())

    def test_update_refuses_legacy_default_before_running_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".codex/skills/OpenMobius-skill"
            legacy.mkdir(parents=True)
            args = argparse.Namespace(platform="codex", target_dir=None)

            with (
                patch.object(install.Path, "home", return_value=home),
                patch.object(install, "_run_single_install") as run_install,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = install.cmd_update(
                    platforms=["codex"],
                    target_dir=None,
                    no_pull=True,
                    rebuild_index=False,
                    args=args,
                )

            self.assertEqual(result, 1)
            run_install.assert_not_called()
            self.assertTrue(legacy.is_dir())


class InstallerCliRegistryTests(unittest.TestCase):
    def test_cli_platform_choices_and_help_derive_from_registry(self) -> None:
        parser = install._build_parser()
        action = next(item for item in parser._actions if item.dest == "platform")
        self.assertEqual(
            tuple(action.choices),
            (*install.PLATFORM_NAMES, "auto", "all"),
        )
        help_text = parser.format_help()
        self.assertRegex(
            help_text,
            rf"all\s+{len(install.DEFAULT_TARGET_PLATFORMS)}\s+platforms",
        )
        help_text_flat = " ".join(help_text.split())
        self.assertIn("WorkBuddy is excluded", help_text_flat)
        self.assertIn(
            "requires --target-dir only for developer staging",
            help_text_flat,
        )
        self.assertIn("~/.claude/skills/openmobius-skill", help_text)
        self.assertNotIn("SKILL.md symlink", help_text)
        self.assertIn("api.mobiusquant.ai", help_text)
        self.assertIn("github.com", help_text)

    def test_all_dispatches_only_platforms_with_documented_local_targets(self) -> None:
        with (
            patch.object(install, "_preflight_targets", return_value=True),
            patch.object(install, "cmd_install_all", return_value=0) as install_all,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = install.main(["--platform", "all"])

        self.assertEqual(result, 0)
        install_all.assert_called_once()
        self.assertEqual(
            install_all.call_args.args[0],
            list(install.DEFAULT_TARGET_PLATFORMS),
        )
        self.assertNotIn("workbuddy", install_all.call_args.args[0])

    def test_all_still_rejects_one_shared_explicit_target(self) -> None:
        with redirect_stderr(io.StringIO()) as stderr:
            result = install.main(
                ["--platform", "all", "--target-dir", "/one/shared/path"]
            )
        self.assertEqual(result, 2)
        self.assertIn("incompatible", stderr.getvalue())


class InstallerHostCompatibilityTests(unittest.TestCase):
    def test_preflight_rejects_openclaw_and_hermes_on_windows(self) -> None:
        for platform_name, display_name in (
            ("openclaw", "OpenClaw"),
            ("hermes", "Hermes"),
        ):
            with self.subTest(platform=platform_name):
                stderr = io.StringIO()
                with (
                    patch.object(
                        install.platform,
                        "system",
                        return_value="Windows",
                    ),
                    redirect_stderr(stderr),
                ):
                    result = install._preflight_targets(
                        [platform_name],
                        Path("C:/selected/openmobius-skill"),
                    )

                self.assertFalse(result)
                message = stderr.getvalue()
                self.assertIn(f"[{platform_name}]", message)
                self.assertIn(display_name, message)
                self.assertIn("Linux, macOS", message)
                self.assertIn("current host is Windows", message)

    def test_preflight_allows_supported_and_unrestricted_hosts(self) -> None:
        target = Path("/selected/openmobius-skill")
        for platform_name, host_system in (
            ("openclaw", "Linux"),
            ("hermes", "Darwin"),
            ("claude-code", "Windows"),
        ):
            with (
                self.subTest(platform=platform_name, host=host_system),
                patch.object(
                    install.platform,
                    "system",
                    return_value=host_system,
                ),
                redirect_stderr(io.StringIO()),
            ):
                self.assertTrue(
                    install._preflight_targets([platform_name], target)
                )


class InstallerProgressTests(unittest.TestCase):
    def test_step_counter_has_no_fragile_total(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(install, "_step_num", 0),
            redirect_stdout(stdout),
        ):
            install.step("First")
            install.step("Second")

        output = stdout.getvalue()
        self.assertIn("[1] First", output)
        self.assertIn("[2] Second", output)
        self.assertNotRegex(output, r"\[\d+/\d+\]")


if __name__ == "__main__":
    unittest.main()
