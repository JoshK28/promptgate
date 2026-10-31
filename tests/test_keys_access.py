"""Task 1 contracts; deferred imports keep every test discoverable."""

import unittest


class KeysAccessTests(unittest.TestCase):
    def test_keys_are_owner_prefixed(self):
        from core import keys

        partition_keys = (
            keys.user_pk("alice"),
            keys.case_pk("alice", "s_123", 7),
            keys.result_pk("alice", "r_123"),
        )
        for partition_key in partition_keys:
            with self.subTest(partition_key=partition_key):
                self.assertTrue(partition_key.startswith("U#alice"))

    def test_versions_zero_padded(self):
        from core import keys

        self.assertEqual(keys.config_sk("c1", 7), "CFG#c1#V#0007")

    def test_resolve_owner_own_resource(self):
        from core.access import resolve_owner

        self.assertEqual(
            resolve_owner("alice", "c_123", write=True), "alice"
        )

    def test_public_read_allowed(self):
        from core.access import resolve_owner

        self.assertEqual(
            resolve_owner("alice", "pub_capitals", write=False), "public"
        )

    def test_public_write_denied(self):
        from core.access import AccessDenied, resolve_owner

        with self.assertRaises(AccessDenied):
            resolve_owner("alice", "pub_capitals", write=True)

    def test_ids_with_separators_rejected(self):
        from core.access import AccessDenied, resolve_owner

        with self.assertRaises(AccessDenied):
            resolve_owner("alice", "x#U#bob", write=False)
