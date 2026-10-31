"""Exercise real frozen SSH DLLs in deep Windows extraction directories.

Uses junctions to the existing backend, never duplicates its dependency cache.
No device connection is made. Run with --backend tools/backend/openwrt-backend.exe.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def child():
    import _cffi_backend
    from backend.ssh_runtime import load_ssh
    assert getattr(sys, 'frozen', False), 'Expected the frozen backend'
    assert len(_cffi_backend.__file__) > 260, 'Fixture did not cross the DLL path limit'
    assert load_ssh().__name__ == 'paramiko'
    print(json.dumps({'native_ssh': True, 'dll_path_length': len(_cffi_backend.__file__),
                      'hardware_access': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This regression test requires Windows')
    backend = args.backend.resolve()
    if not backend.is_file():
        parser.error('Frozen backend is missing')
    args.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='long-path-', dir=args.work) as tmp:
        root = Path(tmp).resolve()
        for label in ('ASCII', '下载目录 包含空格', '路径 共享安装器'):
            parent = root / label
            while len(str(parent)) < 205:
                parent /= 'nested-release-folder'
            parent.mkdir(parents=True)
            link = parent / 'backend'
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(backend.parent)],
                           check=True, capture_output=True)
            try:
                # Mirrors Rust canonicalize's verbatim Windows executable path.
                executable = '\\\\?\\' + str(link / backend.name)
                result = subprocess.run([executable, '--run-tool', str(Path(__file__).resolve()), '--child'],
                                        capture_output=True, timeout=60)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode('utf-8', 'replace'))
                print(label + ': ' + result.stdout.decode('utf-8').strip())
            finally:
                # Remove only this fixture junction; never its shared target.
                if (link.is_junction() and link.parent.resolve().is_relative_to(root)
                        and link.resolve() == backend.parent):
                    link.rmdir()


if __name__ == '__main__':
    if '--child' in sys.argv:
        child()
    else:
        main()
