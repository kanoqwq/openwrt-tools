"""Compile and execute the actual native path resolver without Tauri/WebView."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class DistributionPathTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('rustc'), 'Rust compiler required for native resolver tests')
    def test_native_resolver(self):
        source = Path(__file__).resolve().parents[1]/'ui/src-tauri/src/distribution.rs'
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder)/'distribution-tests.exe'
            compiled = subprocess.run(['rustc', '--edition=2021', '--test', str(source), '-o', str(binary)],
                                      capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(compiled.returncode, 0, compiled.stdout+compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('0 failed', result.stdout)


if __name__ == '__main__':
    unittest.main()
