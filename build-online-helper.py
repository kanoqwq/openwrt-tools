"""Build the single static ARM64 Android online-F2FS helper in the Linux workspace.

Run through tools/build-release.py run; no kernel/rootfs rebuild or downloads.
Native Windows/macOS packaging consumes the checked, source-bound executable.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def main():
    source = HERE/'device/f2fs-online.c'
    with tempfile.TemporaryDirectory(prefix='f2fs-online-') as temp:
        test = Path(temp)/'test'
        subprocess.run(['cc','-O2','-Wall','-Wextra','-Werror',str(HERE/'device/test-f2fs-online.c'),'-o',str(test)],check=True)
        subprocess.run([str(test)],check=True)
        output = HERE/'device/f2fs-online-arm64'
        pending = output.with_suffix('.pending')
        subprocess.run(['aarch64-linux-gnu-gcc','-static','-O2','-Wall','-Wextra','-Werror',
                        str(source),'-o',str(pending)],check=True)
        subprocess.run(['aarch64-linux-gnu-strip',str(pending)],check=True)
        pending.replace(output)
        (HERE/'device/build.json').write_text(json.dumps({
            'source':hashlib.sha256(source.read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
            'binary':hashlib.sha256(output.read_bytes()).hexdigest(),
            'architecture':'aarch64-static-linux','protocol':1},indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
