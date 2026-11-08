"""Exercise interpreter selection in Bash without installing or changing Python."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name == 'nt' else shutil.which('bash')


@unittest.skipUnless(BASH and Path(BASH).exists(), 'Bash unavailable')
class PythonBootstrapTests(unittest.TestCase):
    def test_unsupported_legacy_interpreter_does_not_block_a_newer_one(self):
        result = subprocess.run([BASH, '-c', '''
source deploy/workspace_python.sh
old_python() { return 1; }
new_python() { return 0; }
workspace_find_python old_python new_python
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'new_python')

    def test_no_compatible_interpreter_returns_failure_without_claiming_success(self):
        result = subprocess.run([BASH, '-c', '''
source deploy/workspace_python.sh
old_python() { return 1; }
workspace_find_python old_python nonexistent_zeus_python
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
