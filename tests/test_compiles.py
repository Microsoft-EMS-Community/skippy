# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Most of the bot needs a live Discord connection to exercise, so this at
# least catches syntax errors in every module before a deploy restarts it.

import ast
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

    def test_every_feature_module_is_loaded_at_startup(self):
        # A module with setup(bot) adds commands or listeners; bot.py has to list it, or it never runs.
        features = sorted(path.stem for path in ROOT.glob("*.py")
                          if any(isinstance(node, ast.AsyncFunctionDef) and node.name == "setup"
                                 for node in ast.parse(path.read_text(encoding="utf-8")).body))
        bot = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
        listed = next(ast.literal_eval(node.value) for node in bot.body
                      if isinstance(node, ast.Assign) and node.targets[0].id == "EXTENSIONS")
        self.assertEqual(features, sorted(listed))


if __name__ == "__main__":
    unittest.main()
