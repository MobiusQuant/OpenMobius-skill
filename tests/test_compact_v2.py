import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _lib.compact_v2 import (  # noqa: E402
    COMPACT_V2_FILENAME,
    encode_compact_v2,
    load_compact_v2_records,
    reconstruct_compact_v2_records,
)
from _lib.knowledge_v2 import build_v2_records  # noqa: E402


def _record_contract(records):
    return sorted(
        (
            {
                "id": record["id"],
                "document": record["document"],
                "metadata": record["metadata"],
            }
            for record in records
        ),
        key=lambda record: record["id"],
    )


def _json_line(value) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


class CompactV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = build_v2_records(ROOT / "knowledge_base")
        cls.encoded = encode_compact_v2(cls.result)

    def test_exactly_reconstructs_complete_school_and_evidence_contracts(self):
        reconstructed = reconstruct_compact_v2_records(self.encoded)

        self.assertEqual(len(reconstructed["school"]), 2144)
        self.assertEqual(len(reconstructed["evidence"]), 18645)
        self.assertEqual(
            reconstructed["school"],
            _record_contract(self.result.school_records),
        )
        self.assertEqual(
            reconstructed["evidence"],
            _record_contract(self.result.evidence_records),
        )

    def test_file_loader_exposes_both_attributable_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / COMPACT_V2_FILENAME
            path.write_bytes(self.encoded)

            self.assertEqual(len(load_compact_v2_records(path, "school")), 2144)
            self.assertEqual(len(load_compact_v2_records(path, "evidence")), 18645)
            with self.assertRaisesRegex(ValueError, "unsupported layer=canonical"):
                load_compact_v2_records(path, "canonical")

    def test_body_tampering_fails_content_hash_validation(self):
        header, body = self.encoded.split(b"\n", 1)
        self.assertIn(b"ICT", body)
        tampered = header + b"\n" + body.replace(b"ICT", b"ict", 1)

        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            reconstruct_compact_v2_records(tampered)

    def test_boolean_header_counts_are_rejected(self):
        header_line, body = self.encoded.split(b"\n", 1)
        header = json.loads(header_line)
        header["card_count"] = True

        with self.assertRaisesRegex(ValueError, "header is unsupported"):
            reconstruct_compact_v2_records(_json_line(header) + body)

    def test_unsafe_card_path_fails_even_with_a_matching_hash(self):
        header_line, body_line = self.encoded.split(b"\n", 1)
        header = json.loads(header_line)
        body = json.loads(body_line)
        body["cards"][0][4] = "../outside.json"
        encoded_body = _json_line(body)
        header["content_sha256"] = hashlib.sha256(encoded_body).hexdigest()

        with self.assertRaisesRegex(ValueError, "card path is unsafe"):
            reconstruct_compact_v2_records(_json_line(header) + encoded_body)


if __name__ == "__main__":
    unittest.main()
