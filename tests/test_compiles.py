# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Most of the bot needs a live Discord connection to exercise, so this at
# least catches syntax errors in every module before a deploy restarts it.

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CompileTests(unittest.TestCase):
    def test_every_module_compiles(self):
        modules = sorted(ROOT.glob("*.py"))
        self.assertTrue(modules)
        for path in modules:
            with self.subTest(module=path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
