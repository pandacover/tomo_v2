import unittest
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tomo_core.attachment_capability import AttachmentCapability, AttachmentCapabilityError, hash_file_id, issue_attachment_capability, load_or_create_attachment_key, verify_attachment_capability


class AttachmentCapabilityTests(unittest.TestCase):
    def test_capability_binds_exact_hashed_file_owner_and_generation(self):
        token = issue_attachment_capability(b"k" * 32, AttachmentCapability("owner", "generation", (hash_file_id("photo-id"),), 100, 400))
        claim = verify_attachment_capability(b"k" * 32, token, "photo-id", owner_id="owner", generation_id="generation", now=200)
        self.assertEqual(claim.owner_id, "owner")
        self.assertNotIn("photo-id", token)

    def test_capability_rejects_tampered_and_foreign_file_claims(self):
        token = issue_attachment_capability(b"k" * 32, AttachmentCapability("owner", "generation", (hash_file_id("photo-id"),), 100, 400))
        with self.assertRaises(AttachmentCapabilityError):
            verify_attachment_capability(b"k" * 32, token, "other", owner_id="owner", generation_id="generation", now=200)

    def test_key_creation_is_concurrent_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            with ThreadPoolExecutor(max_workers=8) as pool:
                keys = list(pool.map(lambda _: load_or_create_attachment_key(directory), range(8)))
            self.assertEqual(keys, [keys[0]] * 8)
            if os.name != "nt":
                self.assertEqual((Path(directory) / "attachment-capability.key").stat().st_mode & 0o077, 0)

    def test_rejects_malformed_or_unsafe_claims(self):
        key = b"k" * 32
        for token in ("", "v1..", "v1.%%%.$", "v2.abc.def"):
            with self.subTest(token=token), self.assertRaisesRegex(AttachmentCapabilityError, "invalid_capability"):
                verify_attachment_capability(key, token, "photo", owner_id="owner", generation_id="generation", now=200)
        for factory in (
            lambda: AttachmentCapability("owner", "generation", (hash_file_id("a"),) * 9, 100, 200),
            lambda: AttachmentCapability("owner", "generation", (hash_file_id("a"), hash_file_id("a")), 100, 200),
            lambda: AttachmentCapability("owner", "generation", ("0" * 64,), True, 200),
        ):
            with self.subTest(factory=factory), self.assertRaises(AttachmentCapabilityError):
                factory()

    def test_rejects_future_expired_long_lived_and_mismatched_claims(self):
        key = b"k" * 32
        def token(issued, expires):
            return issue_attachment_capability(key, AttachmentCapability("owner", "generation", (hash_file_id("photo"),), issued, expires))
        for value, code in ((token(100, 199), "expired_capability"), (token(231, 300), "invalid_capability")):
            with self.subTest(code=code), self.assertRaisesRegex(AttachmentCapabilityError, code):
                verify_attachment_capability(key, value, "photo", owner_id="owner", generation_id="generation", now=200)
        with self.assertRaisesRegex(AttachmentCapabilityError, "invalid_capability"):
            issue_attachment_capability(key, AttachmentCapability("owner", "generation", (hash_file_id("photo"),), 100, 401))
        value = token(100, 300)
        for owner, generation, file_id in (("other", "generation", "photo"), ("owner", "other", "photo"), ("owner", "generation", "other")):
            with self.subTest(owner=owner, generation=generation, file_id=file_id), self.assertRaisesRegex(AttachmentCapabilityError, "forbidden_capability"):
                verify_attachment_capability(key, value, file_id, owner_id=owner, generation_id=generation, now=200)
        self.assertNotIn("photo", value)
