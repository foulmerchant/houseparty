import sys
import unittest

import test_engine  # noqa: F401  (loads the modules)
from hp.auth import Authenticator, Lockout, User, hash_code, valid_code, verify_code  # noqa: E402
from hp.engine import Mode  # noqa: E402


class Codes(unittest.TestCase):
    def test_hash_roundtrip_and_salt(self):
        h1, h2 = hash_code("123456"), hash_code("123456")
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_code("123456", h1))
        self.assertFalse(verify_code("123457", h1))
        self.assertNotIn("123456", h1)

    def test_garbage_hash_is_false(self):
        self.assertFalse(verify_code("123456", "nonsense"))
        self.assertFalse(verify_code("123456", "md5$1$1$1$AA==$AA=="))

    def test_code_rules(self):
        self.assertTrue(valid_code("123456"))
        for bad in ("12345", "1234567890123", "12a456", "١٢٣٤٥٦", ""):
            self.assertFalse(valid_code(bad), bad)

    def test_identify(self):
        sam = User("1", "Sam", hash_code("111111"))
        duress = User("2", "Sam", hash_code("999999"), duress=True)
        off = User("3", "Old", hash_code("222222"), enabled=False)
        auth = Authenticator([sam, duress, off])
        self.assertEqual(auth.identify("111111"), sam)
        self.assertTrue(auth.identify("999999").duress)
        self.assertIsNone(auth.identify("222222"))
        self.assertIsNone(auth.identify("abc"))
        self.assertIsNone(auth.identify(None))

    def test_user_roundtrip(self):
        u = User("1", "Sam", "h", can_arm=frozenset({Mode.AWAY}), duress=True)
        self.assertEqual(User.from_dict(u.to_dict()), u)


class LockoutTest(unittest.TestCase):
    def test_schedule_and_isolation(self):
        lo = Lockout()
        self.assertEqual([lo.failure("tablet", 0) for _ in range(3)], [0.0, 0.0, 30.0])
        self.assertEqual(lo.remaining("tablet", 10), 20)
        self.assertEqual(lo.remaining("phone", 10), 0)  # other HA users unaffected
        self.assertEqual(lo.failure("tablet", 40), 60.0)
        lo.success("tablet")
        self.assertEqual(lo.remaining("tablet", 41), 0)

    def test_cap(self):
        lo = Lockout()
        for _ in range(20):
            last = lo.failure("t", 0)
        self.assertEqual(last, 900.0)


if __name__ == "__main__":
    unittest.main()
