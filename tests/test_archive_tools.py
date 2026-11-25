"""Archive reproducible inputs without losing payloads or macOS execute bits."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_tools import write_member


class ArchiveTimestamps(unittest.TestCase):
    def test_mac_repair_launchers_keep_execute_bits_on_windows_archives(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root/'launcher'
            source.write_bytes(b'#!/bin/sh\n'); source.chmod(0o644)
            archive = root/'package.zip'
            names = ('OpenWrt-Installer-macOS.command', 'OpenWrt-Diff-Studio-macOS.command',
                     'OpenWrt-Updater-macOS.command')
            with zipfile.ZipFile(archive, 'w') as stream:
                for name in names:
                    write_member(stream, source, name)
            with zipfile.ZipFile(archive) as stream:
                for name in names:
                    self.assertEqual(stat.S_IMODE(stream.getinfo(name).external_attr >> 16), 0o755)

    def test_reproducible_epoch_and_future_times_fit_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'input';source.write_bytes(b'verified package bytes')
            source.chmod(0o644)
            for timestamp, year in ((0,1980),(7258118400,2107)):
                os.utime(source,(timestamp,timestamp))
                archive=root/'package.zip'
                with zipfile.ZipFile(archive,'w') as stream:write_member(stream,source,'tools/input')
                with zipfile.ZipFile(archive) as stream:
                    self.assertEqual(stream.read('tools/input'),source.read_bytes())
                    self.assertEqual(stream.getinfo('tools/input').date_time[0],year)
                    self.assertEqual(stat.S_IMODE(stream.getinfo('tools/input').external_attr>>16),0o644)
                    self.assertIsNone(stream.testzip())

    def test_epoch_input_keeps_mac_launcher_executable(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'launcher';source.write_bytes(b'\xcf\xfa\xed\xfe executable fixture')
            source.chmod(0o644);os.utime(source,(0,0))
            archive=root/'package.zip';name='OpenWrt-Installer.app/Contents/MacOS/openwrt-installer'
            with zipfile.ZipFile(archive,'w') as stream:write_member(stream,source,name)
            with zipfile.ZipFile(archive) as stream:
                self.assertEqual(stream.read(name),source.read_bytes())
                self.assertEqual(stat.S_IMODE(stream.getinfo(name).external_attr>>16),0o755)


if __name__=='__main__':unittest.main()
