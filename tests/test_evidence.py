import hashlib
import tempfile
import unittest
from pathlib import Path

from fincl.errors import EvidenceError
from fincl.evidence import PlaintextEvidenceStore


class EvidenceTests(unittest.TestCase):
    def test_capture_is_byte_identical_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.csv"
            original = b"a,b\r\n1,\xff\r\n"
            source.write_bytes(original)
            before = source.stat()
            store = PlaintextEvidenceStore(root / "evidence")

            first = store.capture(source)
            object_path = root / "evidence" / first.evidence_key
            object_mtime = object_path.stat().st_mtime_ns
            second = store.capture(source)

            self.assertEqual(first.sha256, hashlib.sha256(original).hexdigest())
            self.assertEqual(object_path.read_bytes(), original)
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(object_path.stat().st_mtime_ns, object_mtime)
            self.assertEqual(source.read_bytes(), original)
            after = source.stat()
            self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_verified_open_rejects_corrupted_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.csv"
            source.write_bytes(b"original")
            store = PlaintextEvidenceStore(root / "evidence")
            capture = store.capture(source)
            (root / "evidence" / capture.evidence_key).write_bytes(b"corrupt")
            with self.assertRaises(EvidenceError):
                store.open_verified(capture.sha256, capture.evidence_key)


if __name__ == "__main__":
    unittest.main()
