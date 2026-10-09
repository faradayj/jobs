"""Protect existing local candidate configuration during repository setup."""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from init_local import initialize


class LocalSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        examples = self.root / "examples"
        examples.mkdir()
        for name in ("library.example.json", ".env.example"):
            (examples / name).write_bytes((ROOT / "examples" / name).read_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_setup_creates_both_configuration_files(self):
        created = initialize(self.root)
        self.assertEqual({path.name for path in created}, {"library.json", ".env"})
        self.assertEqual((self.root / "data" / "library.json").read_bytes(),
                         (self.root / "examples" / "library.example.json").read_bytes())

    def test_repeat_setup_preserves_user_profile_and_credentials(self):
        initialize(self.root)
        data = self.root / "data"
        (data / "library.json").write_bytes(b'custom candidate profile')
        (data / ".env").write_bytes(b'private credentials')
        self.assertEqual(initialize(self.root), [])
        self.assertEqual((data / "library.json").read_bytes(), b'custom candidate profile')
        self.assertEqual((data / ".env").read_bytes(), b'private credentials')

    def test_partial_setup_preserves_existing_profile_and_history(self):
        data = self.root / "data"
        data.mkdir()
        (data / "library.json").write_bytes(b'custom candidate profile')
        (data / "applied_history.json").write_bytes(b'important application history')
        self.assertEqual([path.name for path in initialize(self.root)], [".env"])
        self.assertEqual((data / "library.json").read_bytes(), b'custom candidate profile')
        self.assertEqual((data / "applied_history.json").read_bytes(), b'important application history')


if __name__ == "__main__":
    unittest.main()
