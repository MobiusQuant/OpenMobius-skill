import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import install


class UninstallTests(unittest.TestCase):
    def _make_target(self, parent: Path, name: str) -> Path:
        target = parent / name
        (target / ".venv").mkdir(parents=True)
        index = target / "knowledge_base" / "_index"
        index.mkdir(parents=True)
        (index / "chroma.sqlite3").write_text("test", encoding="utf-8")
        (target / "SKILL.md").write_text(
            "---\nname: openmobius-skill\ndescription: test\n---\n",
            encoding="utf-8",
        )
        return target

    def test_standard_uninstall_removes_entire_self_contained_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self._make_target(Path(tmp), "standard")

            with redirect_stdout(io.StringIO()):
                result = install.cmd_uninstall(
                    platforms=["codex"],
                    target_dir=target,
                    full=False,
                    purge=False,
                    yes_i_know=False,
                )

            self.assertEqual(result, 0)
            self.assertFalse(target.exists())

    def test_deprecated_full_flag_has_same_uninstall_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self._make_target(Path(tmp), "full")

            with redirect_stdout(io.StringIO()), patch.object(install, "warn") as warn:
                result = install.cmd_uninstall(
                    platforms=["codex"],
                    target_dir=target,
                    full=True,
                    purge=False,
                    yes_i_know=False,
                )

            self.assertEqual(result, 0)
            self.assertFalse(target.exists())
            warn.assert_called_once()
            self.assertIn("deprecated", warn.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
