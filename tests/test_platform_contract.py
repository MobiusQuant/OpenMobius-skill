import json
import re
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS_DIR = ROOT / "platforms"

EXPECTED_PLATFORMS = {
    "claude-code",
    "codex",
    "cursor",
    "hermes",
    "openclaw",
    "workbuddy",
}

PLATFORM_FIELDS = {
    "claude-code": {
        "required": {"name", "description"},
        "allowed": {"name", "description"},
    },
    "codex": {
        "required": {"name", "description"},
        "allowed": {"name", "description"},
    },
    "cursor": {
        "required": {"name", "description"},
        "allowed": {"name", "description"},
    },
    "openclaw": {
        "required": {
            "name",
            "description",
            "version",
            "homepage",
            "compatibility",
            "metadata",
        },
        "allowed": {
            "name",
            "description",
            "version",
            "homepage",
            "compatibility",
            "metadata",
        },
    },
    "hermes": {
        "required": {
            "name",
            "description",
            "version",
            "author",
            "license",
            "platforms",
            "metadata",
        },
        "allowed": {
            "name",
            "description",
            "version",
            "author",
            "license",
            "platforms",
            "metadata",
        },
    },
    "workbuddy": {
        "required": {
            "name",
            "description",
            "description_zh",
            "description_en",
            "version",
            "author",
        },
        "allowed": {
            "name",
            "description",
            "description_zh",
            "description_en",
            "version",
            "author",
        },
    },
}


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() in {"null", "~"}:
        return None
    return value


def _load_yaml_subset(path: Path) -> dict[str, Any]:
    """Parse the mapping/list/scalar YAML subset used by platform manifests.

    The project deliberately does not declare a YAML runtime dependency. This
    keeps the contract test executable with the Python standard library while
    still validating indentation, duplicate keys, nested mappings, and lists.
    """

    rows: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise AssertionError(f"{path}:{line_number}: tabs are not valid indentation")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        rows.append((indent, raw_line.strip()))

    if not rows:
        raise AssertionError(f"{path}: empty YAML document")

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        is_list = rows[index][1].startswith("- ")
        result: Any = [] if is_list else {}

        while index < len(rows):
            row_indent, content = rows[index]
            if row_indent < indent:
                break
            if row_indent > indent:
                raise AssertionError(
                    f"{path}: unexpected indentation before {content!r}"
                )

            if is_list:
                if not content.startswith("- "):
                    break
                item = content[2:].strip()
                if not item:
                    raise AssertionError(f"{path}: empty list item is unsupported")
                result.append(_parse_scalar(item))
                index += 1
                continue

            if content.startswith("- "):
                break
            key, separator, value = content.partition(":")
            if not separator or not key.strip():
                raise AssertionError(f"{path}: invalid mapping entry {content!r}")
            key = key.strip()
            if key in result:
                raise AssertionError(f"{path}: duplicate key {key!r}")
            index += 1
            if value.strip():
                result[key] = _parse_scalar(value)
                continue
            if index >= len(rows) or rows[index][0] <= indent:
                result[key] = {}
                continue
            result[key], index = parse_block(index, rows[index][0])

        return result, index

    document, end = parse_block(0, rows[0][0])
    if end != len(rows) or not isinstance(document, dict):
        raise AssertionError(f"{path}: expected one top-level mapping")
    return document


class PlatformContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platforms = {
            path.stem: _load_yaml_subset(path)
            for path in sorted(PLATFORMS_DIR.glob("*.yaml"))
        }

    def test_platform_file_set_is_complete(self):
        self.assertEqual(set(self.platforms), EXPECTED_PLATFORMS)

    def test_platform_fields_follow_their_contracts(self):
        for platform, document in self.platforms.items():
            with self.subTest(platform=platform):
                schema = PLATFORM_FIELDS[platform]
                actual = set(document)
                self.assertTrue(schema["required"].issubset(actual))
                self.assertTrue(actual.issubset(schema["allowed"]))

    def test_every_platform_uses_the_canonical_name(self):
        name_pattern = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        for platform, document in self.platforms.items():
            with self.subTest(platform=platform):
                name = document["name"]
                self.assertEqual(name, "openmobius-skill")
                self.assertLessEqual(len(name), 64)
                self.assertRegex(name, name_pattern)

    def test_descriptions_are_nonempty_and_within_platform_limits(self):
        for platform, document in self.platforms.items():
            with self.subTest(platform=platform):
                description = document["description"]
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                if platform == "hermes":
                    limit = 60
                elif platform == "openclaw":
                    # OpenClaw renders this in discovery and slash-command
                    # output and documents a strict sub-160-character target.
                    limit = 159
                else:
                    limit = 1024
                self.assertLessEqual(len(description), limit)
                if platform == "openclaw":
                    self.assertLess(len(description.encode("utf-8")), 160)

    def test_claude_codex_and_cursor_use_portable_minimal_frontmatter(self):
        for platform in ("claude-code", "codex", "cursor"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    set(self.platforms[platform]), {"name", "description"}
                )

    def test_openclaw_declares_host_eligibility(self):
        document = self.platforms["openclaw"]
        self.assertTrue(document["homepage"].startswith("https://"))
        self.assertIn("host execution", document["compatibility"].lower())

        openclaw = document["metadata"]["openclaw"]
        self.assertEqual(openclaw["os"], ["darwin", "linux"])
        self.assertEqual(
            set(openclaw["requires"]["anyBins"]), {"python", "python3"}
        )

    def test_hermes_requires_the_terminal_toolset(self):
        document = self.platforms["hermes"]
        self.assertEqual(document["platforms"], ["linux", "macos"])
        self.assertEqual(
            document["metadata"]["hermes"]["requires_toolsets"], ["terminal"]
        )

    def test_workbuddy_localized_required_fields_are_populated(self):
        document = self.platforms["workbuddy"]
        for field in (
            "description",
            "description_zh",
            "description_en",
            "version",
            "author",
        ):
            with self.subTest(field=field):
                self.assertIsInstance(document[field], str)
                self.assertTrue(document[field].strip())
        self.assertNotIn("allowed-tools", document)

    def test_agent_install_guide_detects_current_codex_root(self):
        guide = (ROOT / "README_AGENT.md").read_text(encoding="utf-8")
        detection = guide.split(
            "Otherwise, detect installed platforms:", 1
        )[1].split("Map the user's answer to a flag:", 1)[0]
        self.assertIn("~/.agents", detection)
        self.assertIn("~/.codex", detection)
        self.assertIn('echo "found: Codex"', detection)

    def test_openai_interface_metadata_and_policy(self):
        path = ROOT / "agents" / "openai.yaml"
        document = _load_yaml_subset(path)
        self.assertEqual(set(document), {"interface", "policy"})
        self.assertNotIn("dependencies", document)

        interface = document["interface"]
        self.assertEqual(
            set(interface),
            {"display_name", "short_description", "default_prompt"},
        )
        self.assertTrue(interface["display_name"].strip())
        self.assertGreaterEqual(len(interface["short_description"]), 25)
        self.assertLessEqual(len(interface["short_description"]), 64)
        self.assertIn("$openmobius-skill", interface["default_prompt"])
        self.assertTrue(document["policy"]["allow_implicit_invocation"])

        source = path.read_text(encoding="utf-8")
        for field in ("display_name", "short_description", "default_prompt"):
            self.assertRegex(source, rf'(?m)^  {field}: "[^"\n]+"$', msg=field)


if __name__ == "__main__":
    unittest.main()
