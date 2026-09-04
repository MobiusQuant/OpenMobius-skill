import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_index  # noqa: E402
import install  # noqa: E402
from _lib import embedder as embedder_module  # noqa: E402


class PinnedEmbeddingIdentityTests(unittest.TestCase):
    def test_default_nomic_loader_pins_revision_and_disables_remote_code(self):
        calls = []

        class FakeModel:
            def get_embedding_dimension(self):
                return 768

        def sentence_transformer(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeModel()

        fake_package = SimpleNamespace(SentenceTransformer=sentence_transformer)
        with patch.dict(sys.modules, {"sentence_transformers": fake_package}):
            loaded = embedder_module.LocalNomicEmbedder()

        self.assertEqual(loaded.model_revision, build_index.EXPECTED_MODEL_REVISION)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, (build_index.EXPECTED_MODEL,))
        self.assertEqual(kwargs["revision"], build_index.EXPECTED_MODEL_REVISION)
        self.assertIs(kwargs["trust_remote_code"], False)
        self.assertNotIn("code_revision", repr(kwargs))

    def test_installer_builder_and_runtime_use_same_revision(self):
        self.assertEqual(
            install.NOMIC_MODEL_REVISION,
            build_index.EXPECTED_MODEL_REVISION,
        )
        self.assertEqual(
            embedder_module.LocalNomicEmbedder.DEFAULT_REVISION,
            build_index.EXPECTED_MODEL_REVISION,
        )
        spec = build_index.v2_embedding_spec("local")
        self.assertEqual(spec["revision"], build_index.EXPECTED_MODEL_REVISION)
        self.assertEqual(
            spec["input_profile"]["model_revision"],
            build_index.EXPECTED_MODEL_REVISION,
        )
        self.assertIn(build_index.EXPECTED_MODEL_REVISION, spec["cache_model_key"])

    def test_runtime_constraints_support_builtin_nomic_implementation(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("sentence-transformers>=5.3,<6", requirements)
        self.assertIn("transformers>=5.5,<6", requirements)


if __name__ == "__main__":
    unittest.main()
