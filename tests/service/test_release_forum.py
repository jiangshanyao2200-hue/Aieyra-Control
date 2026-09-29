"""Signed announcements remain authenticated, exact, and idempotent."""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from test_cloud import cloud
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import release_forum


class ReleaseForumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        self.manifest = {
            "schema": 1,
            "product": "aieyra-control",
            "version": "1.2.3",
            "sequence": 123,
            "notes": "Verified fixture release with a public change description.",
        }
        self.raw = json.dumps(self.manifest).encode()
        self.envelope = {
            "manifest": self.manifest,
            "payload": base64.b64encode(self.raw).decode(),
            "signature": base64.b64encode(self.key.sign(self.raw)).decode(),
        }

    def test_signature_and_displayed_manifest_must_match(self):
        self.assertEqual(
            release_forum.release_payload(self.envelope, self.public)["version"], "1.2.3"
        )
        with self.assertRaises(InvalidSignature):
            release_forum.release_payload(
                {**self.envelope, "payload": base64.b64encode(b"{}").decode()}, self.public
            )
        with self.assertRaisesRegex(ValueError, "invalid_signed_release"):
            release_forum.release_payload(
                {**self.envelope, "manifest": {**self.manifest, "version": "9.9.9"}}, self.public
            )

    def test_exact_release_once_and_event_readable_without_login(self):
        app = cloud.Cloud(self.temp.name)
        before = Path(self.temp.name, "signed-copy.json")
        before.write_bytes(json.dumps(self.envelope).encode())
        data = release_forum.release_payload(self.envelope, self.public)
        first = release_forum.publish(app, data)
        replay = release_forum.publish(app, data)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["topic"], replay["topic"])
        public = app.matrix_read("/v1/matrix/topics", {"board": ["releases"]})["items"]
        self.assertEqual(len(public), 1)
        self.assertTrue(public[0]["official"])
        self.assertEqual(public[0]["sourceId"], "1.2.3")
        self.assertEqual(public[0]["author"]["name"], "Aieyra Control")
        with app.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM matrix_events").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM matrix_audit").fetchone()[0], 1)
        self.assertEqual(before.read_bytes(), json.dumps(self.envelope).encode())
        with self.assertRaisesRegex(ValueError, "release_sequence_conflict"):
            release_forum.publish(app, {**data, "digest": "f" * 64})


if __name__ == "__main__":
    unittest.main()
