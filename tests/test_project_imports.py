"""Project modules must be available to file-based launches in the project venv."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProjectImportTests(unittest.TestCase):
    def test_all_entry_files_import_outside_repository_without_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        entries = [
            "api/app.py",
            "scripts/serve_api.py",
            "scripts/sandbox_cleanup.py",
            "tests/verify_agent.py",
            "tests/verify_development_tools.py",
            "tests/verify_daytona.py",
            "scripts/devmate_with_mw.py",
            "scripts/verify_web_team.py",
            "tests/verify_env.py",
        ]
        env = os.environ | {
            "PYTHONPATH": "",
            "PYTHONIOENCODING": "utf-8",
            "SYC_API_KEY": "import-check-placeholder",
        }
        with tempfile.TemporaryDirectory() as cwd:
            for entry in entries:
                with self.subTest(entry=entry):
                    # Match file launching: sys.path starts with the script's folder.
                    # Do not run __main__: this regression checks imports, not APIs.
                    code = (
                        "import runpy, sys; from pathlib import Path; "
                        "sys.path[0] = str(Path(sys.argv[1]).parent); "
                        "runpy.run_path(sys.argv[1], run_name='import_check')"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", code, str(root / entry)],
                        cwd=cwd, env=env, capture_output=True,
                        text=True, encoding="utf-8", timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
