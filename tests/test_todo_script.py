"""Regression coverage for running the example script by file path."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class TodoScriptTests(unittest.TestCase):
    def test_runs_directly_from_its_file_path(self) -> None:
        self._run_script_directly("todo_test.py")

    def test_streaming_script_runs_directly_from_its_file_path(self) -> None:
        self._run_script_directly("todo_astream_test.py")

    def _run_script_directly(self, script_name: str) -> None:
        repository_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            scripts_dir = project_root / "scripts"
            agent_dir = project_root / "agent"
            scripts_dir.mkdir()
            agent_dir.mkdir()
            shutil.copy(repository_root / "scripts" / script_name, scripts_dir)
            (scripts_dir / "_common.py").write_text(
                "def print_all(result):\n    return None\n",
                encoding="utf-8",
            )
            (agent_dir / "__init__.py").touch()
            (agent_dir / "main.py").write_text(
                "class FakeAgent:\n"
                "    async def ainvoke(self, payload, **kwargs):\n"
                "        return {}\n\n"
                "    async def astream(self, payload, stream_mode, **kwargs):\n"
                "        yield {}\n\n"
                "def build_agent():\n"
                "    return FakeAgent()\n",
                encoding="utf-8",
            )

            environment = os.environ | {"PYTHONPATH": ""}
            result = subprocess.run(
                [sys.executable, str(scripts_dir / script_name)],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
