import os
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from tomo_core.cron_capability import CronCapability, CronCapabilityError, issue_capability, load_or_create_key, verify_capability


class CronCapabilityTests(unittest.TestCase):
    def test_signed_capability_binds_verified_telegram_context(self):
        key = b"k" * 32
        token = issue_capability(
            key,
            CronCapability("owner", "actor", "telegram:1", "telegram:actor:actor", 100, 200),
        )

        claim = verify_capability(key, token, now=150)

        self.assertEqual(
            (claim.owner_id, claim.actor_id, claim.destination, claim.session_id),
            ("owner", "actor", "telegram:1", "telegram:actor:actor"),
        )

    def test_signed_capability_binds_claims_operation_and_expiry(self):
        key = b"k" * 32
        token = issue_capability(key, CronCapability("owner", "actor", "telegram:1", "telegram:actor:actor", 100, 200, frozenset({"list"})))
        claim = verify_capability(key, token, now=150, operation="list")
        self.assertEqual((claim.owner_id, claim.destination), ("owner", "telegram:1"))
        for now, operation in ((200, "list"), (150, "create")):
            with self.subTest(now=now, operation=operation), self.assertRaises(CronCapabilityError):
                verify_capability(key, token, now=now, operation=operation)
        with self.assertRaises(CronCapabilityError):
            verify_capability(key, token[:-1] + "A", now=150)

    def test_key_is_durable_and_private_where_permissions_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = load_or_create_key(tmp)
            self.assertEqual(load_or_create_key(tmp), first)
            path = os.path.join(tmp, "cron-capability.key")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o077, 0)

    def test_key_creation_fails_closed_when_private_permissions_cannot_be_enforced(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tomo_core.cron_capability._posix_permissions_supported", return_value=True), patch("tomo_core.cron_capability.os.chmod", side_effect=OSError("denied")):
            with self.assertRaisesRegex(CronCapabilityError, "capability_key_permissions"):
                load_or_create_key(tmp)

    def test_concurrent_key_load_never_reads_a_partially_created_destination(self):
        real_open = os.open
        creator_opened = threading.Event()
        allow_creator_write = threading.Event()
        creator_result = []

        def pause_first_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            if not creator_opened.is_set():
                creator_opened.set()
                self.assertTrue(allow_creator_write.wait(1))
            return descriptor

        with tempfile.TemporaryDirectory() as tmp, patch("tomo_core.cron_capability.os.open", side_effect=pause_first_open):
            creator = threading.Thread(target=lambda: creator_result.append(load_or_create_key(tmp)))
            creator.start()
            self.assertTrue(creator_opened.wait(1))
            reader_result = []
            reader_error = []

            def load_reader():
                try:
                    reader_result.append(load_or_create_key(tmp))
                except Exception as error:
                    reader_error.append(error)

            reader = threading.Thread(target=load_reader)
            reader.start()
            reader.join(1)
            allow_creator_write.set()
            creator.join(1)

        self.assertFalse(reader.is_alive())
        self.assertFalse(creator.is_alive())
        self.assertEqual(reader_error, [])
        self.assertEqual(reader_result, creator_result)
        self.assertEqual(len(reader_result[0]), 32)

    def test_capability_rejects_weak_keys_future_issue_and_excess_lifetime(self):
        with self.assertRaises(CronCapabilityError):
            issue_capability(b"short", CronCapability("owner", "actor", "telegram:1", "telegram:actor:actor", 100, 200))
        future = issue_capability(b"k" * 32, CronCapability("owner", "actor", "telegram:1", "telegram:actor:actor", 200, 300))
        with self.assertRaises(CronCapabilityError):
            verify_capability(b"k" * 32, future, now=100)
        with self.assertRaises(CronCapabilityError):
            issue_capability(b"k" * 32, CronCapability("owner", "actor", "telegram:1", "telegram:actor:actor", 100, 3701))
        with self.assertRaises(CronCapabilityError):
            CronCapability("owner", "actor", "https://not-telegram.example", "telegram:actor:actor", 100, 200)

    def test_non_ascii_token_is_a_safe_capability_failure(self):
        with self.assertRaisesRegex(CronCapabilityError, "invalid_capability"):
            verify_capability(b"k" * 32, "v1.\u2603.signature", now=150)
