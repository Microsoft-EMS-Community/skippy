# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# contributors.json maps GitHub usernames to Discord user IDs for the deploy
# notice's pings. The deployer silently skips bad entries, so catch them here.

import json
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "contributors.json"
GITHUB_LOGIN = r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$"


class ContributorsTests(unittest.TestCase):
    def setUp(self):
        # Pairs rather than a dict, so a duplicated username isn't silently merged.
        self.pairs = json.loads(PATH.read_text(encoding="utf-8"), object_pairs_hook=list)
        self.entries = [(k, v) for k, v in self.pairs if not k.startswith("_")]

    def test_entries_are_valid(self):
        for login, discord_id in self.entries:
            with self.subTest(login=login):
                self.assertRegex(login, GITHUB_LOGIN, "use your GitHub username")
                self.assertIsInstance(discord_id, str, "quote the Discord ID as a string")
                self.assertRegex(discord_id, r"^\d{17,20}$",
                                 "use your numeric Discord User ID, not your username")

    def test_no_duplicate_usernames(self):
        logins = [login.lower() for login, _ in self.entries]
        self.assertEqual(len(logins), len(set(logins)))


if __name__ == "__main__":
    unittest.main()
