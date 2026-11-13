"""OTA envelope import, review identity and bounded extraction regressions."""
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from test_ota import package
from backend.ota_engine import Patch
from uploads import copy_payload, ota_payload


class Uploads(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.direct = package(self.root/'patch.ota')

    def envelope(self, name='release/update.ota'):
        output = self.root/'bundle.zip'
        with zipfile.ZipFile(output,'w') as z:
            z.writestr(name,self.direct.read_bytes())
            z.writestr('../ignored-host-program.exe',b'not extracted')
        return output

    def test_direct_and_wrapped_have_identical_review_and_payload(self):
        with Patch(self.direct) as patch:
            wanted = patch.id
        for source in (self.direct,self.envelope()):
            with self.subTest(source=source.name):
                with ota_payload(source) as selected, Patch(selected) as patch:
                    self.assertEqual(patch.id,wanted)
                self.assertTrue(source.exists())
                self.assertFalse(list(self.root.glob('.import-ota-*')))
                target=self.root/(source.name+'.staged')
                copy_payload(source,target)
                self.assertEqual(target.read_bytes(),self.direct.read_bytes())
        self.assertFalse((self.root.parent/'ignored-host-program.exe').exists())

    def test_multiple_or_missing_payloads_are_not_guessed(self):
        for entries in ({'a.ota':b'a','b.ota':b'b'},{'files/rootfs.gz':b'full release'}):
            p=self.root/'ambiguous.zip'
            with zipfile.ZipFile(p,'w') as z:
                for name,data in entries.items():z.writestr(name,data)
            with self.assertRaisesRegex(ValueError,'multiple OTA|no OTA'):
                with ota_payload(p):pass

    def test_unsafe_selected_paths_and_symlinks_rejected(self):
        for name in ('../update.ota','/update.ota','C:/update.ota','a\\update.ota'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'Unsafe'):
                with ota_payload(self.envelope(name)):pass
        p=self.root/'link.zip';info=zipfile.ZipInfo('update.ota');info.create_system=3
        info.external_attr=(stat.S_IFLNK|0o777)<<16
        with zipfile.ZipFile(p,'w') as z:z.writestr(info,b'/etc/shadow')
        with self.assertRaisesRegex(ValueError,'Unsafe'):
            with ota_payload(p):pass

    def test_duplicate_zip_entries_rejected(self):
        p=self.root/'duplicate.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('update.ota',b'a');z.writestr('update.ota',b'b')
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            with ota_payload(p):pass

    def test_existing_destination_never_deleted(self):
        target=self.root/'existing.ota';target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):copy_payload(self.direct,target)
        self.assertEqual(target.read_bytes(),b'keep')

    def test_insufficient_space_retains_upload_and_cleans_temporary(self):
        source=self.envelope()
        with patch('uploads.shutil.disk_usage',return_value=type('Space',(),{'free':1})()):
            with self.assertRaisesRegex(ValueError,'space'):
                with ota_payload(source):pass
        self.assertTrue(source.exists());self.assertFalse(list(self.root.glob('.import-ota-*')))

    def test_crc_failure_does_not_leave_partial_payload(self):
        source=self.envelope();data=bytearray(source.read_bytes())
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            node=z.getinfo('release/update.ota');at=node.header_offset+30+len(node.filename.encode())+len(node.extra)
        data[at]^=1;source.write_bytes(data)
        with self.assertRaises(zipfile.BadZipFile):
            with ota_payload(source):pass
        self.assertFalse(list(self.root.glob('.import-ota-*')))


if __name__=='__main__':unittest.main()
