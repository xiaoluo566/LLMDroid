import importlib.util
import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot"


@unittest.skipUnless(importlib.util.find_spec("setuptools"), "需要 setuptools")
class PackagingTests(unittest.TestCase):
    def test_setup_includes_maestro_package(self):
        original = Path.cwd()
        try:
            os.chdir(ROOT)
            with patch("setuptools.setup") as setup:
                runpy.run_path(str(ROOT / "setup.py"), run_name="__main__")
            self.assertIn("droidbot.maestro", setup.call_args.kwargs["packages"])
        finally:
            os.chdir(original)
