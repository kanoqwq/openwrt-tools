"""4Kn GPT and storage topology regressions; byte arrays only, no devices."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
import uuid
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import gpt,storage


def fixture(block):
    sectors=32*1024*1024
    factor=block//512;logical=sectors//factor
    table=bytearray(128*128)
    table[:16]=uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4').bytes_le
    table[16:32]=uuid.UUID('34c02f03-7425-4a01-b1c9-1eff292e1929').bytes_le
    struct.pack_into('<QQQ',table,32,2048//factor,(2099200//factor)-1,0x1000000000000000)
    table[56:72]='userdata'.encode('utf-16-le')
    pages=(len(table)+block-1)//block
    guid=uuid.UUID('3b670445-cf94-488d-a0f4-20fd807b37f3').bytes_le
    def header(at,alternate,table_lba):
        raw=bytearray(block)
        struct.pack_into('<8sIIIIQQQQ16sQIII',raw,0,b'EFI PART',0x10000,92,0,0,
            at,alternate,2+pages,logical-pages-2,guid,table_lba,128,128,zlib.crc32(table))
        struct.pack_into('<I',raw,16,zlib.crc32(raw[:92]))
        return raw
    first=bytearray((2+pages)*block);first[block:2*block]=header(1,logical-1,2)
    first[2*block:2*block+len(table)]=table
    last=bytearray((pages+1)*block);last[:len(table)]=table
    last[-block:]=header(logical-1,1,logical-pages-1)
    return bytes(first),bytes(last),sectors


def snapshot(block, sectors=64*2097152):
    factor=block//512;total=sectors//factor;count=128;rows=(count*128+block-1)//block
    table=bytearray(count*128)
    for number,name,start,end in ((1,'misc',2048,4095),(2,'userdata',4096,16*2097152-1)):
        e=bytearray(128);e[:16]=uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4').bytes_le
        e[16:32]=uuid.UUID(int=number).bytes_le
        struct.pack_into('<QQ',e,32,start//factor,(end+1)//factor-1)
        text=name.encode('utf-16-le');e[56:56+len(text)]=text;table[(number-1)*128:number*128]=e
    def header(at,other,entries):
        h=bytearray(block);h[:8]=b'EFI PART';struct.pack_into('<II',h,8,0x10000,92)
        struct.pack_into('<QQQQ',h,24,at,other,2+rows,total-2-rows)
        h[56:72]=uuid.UUID(int=999).bytes_le
        struct.pack_into('<QIII',h,72,entries,count,128,zlib.crc32(table))
        struct.pack_into('<I',h,16,zlib.crc32(h[:92]));return h
    first=bytes(block)+header(1,total-1,2)+table
    last=table+header(total-1,1,total-1-rows)
    return first,last,sectors


class StorageUFS(unittest.TestCase):
    def test_512_and_4096_have_same_public_geometry(self):
        results=[gpt.gpt_space(*fixture(n)) for n in (512,4096)]
        self.assertEqual(results[0]['parts'],results[1]['parts'])
        self.assertEqual(results[0]['gaps'],results[1]['gaps'])
        self.assertEqual(results[1]['parts'][0]['start'],2048)

    def test_both_sector_sizes_add_root_preserves_existing_partition_entry(self):
        for block in (512,4096):
            with self.subTest(block=block):
                first,last,total=fixture(block)
                result=gpt.gpt_add_root(first,last,total,4194304,4194304,root_name='e5root')
                info=gpt.gpt_space(*result,total)
                self.assertEqual(info['parts'][1]['name'],'e5root')
                self.assertEqual(info['parts'][1]['start'],4194304)
                self.assertEqual(result[0][2*block:2*block+128],first[2*block:2*block+128])

    def test_unaligned_request_does_not_modify_either_snapshot(self):
        for block in (512,4096):
            first,last,total=fixture(block);before=(hashlib.sha256(first).digest(),hashlib.sha256(last).digest())
            with self.assertRaises(ValueError):gpt.gpt_add_root(first,last,total,4194305,4194304)
            self.assertEqual(before,(hashlib.sha256(first).digest(),hashlib.sha256(last).digest()))

    def test_corrupt_backup_and_ambiguous_magic_are_rejected(self):
        first,last,total=fixture(4096)
        corrupt=bytearray(last);corrupt[5]^=1
        with self.assertRaises(ValueError):gpt.gpt_space(first,bytes(corrupt),total)
        corrupt=bytearray(first);corrupt[512:520]=b'EFI PART'
        with self.assertRaises(ValueError):gpt.gpt_space(bytes(corrupt),last,total)

    def test_mmc_and_ufs_partition_names(self):
        for node,disk in (('mmcblk0p76','mmcblk0'),('sda76','sda'),('sdb76','sdb'),('sdaa76','sdaa')):
            self.assertEqual(storage.disk_node(node),disk)
            self.assertEqual(storage.part_node(disk,76),node)
        for invalid in ('sda','/dev/sda76','sda0','../../sda76'):
            with self.assertRaises(ValueError):storage.disk_node(invalid)

    def test_ufs_headers_read_in_logical_blocks_but_inventory_uses_512(self):
        info={'NODE':'sdb76','SECTOR_SIZE':4096,'DISK_SIZE':32*1024*1024}
        commands=storage.gpt_header_commands(info)
        self.assertIn('if=/dev/block/sdb bs=4096 skip=1 count=1',commands[0])
        self.assertIn('skip=4194303 count=1',commands[1])

    def test_inventory_rejects_ambiguous_identity_wrong_disk_and_alignment(self):
        text='NODE=sda76\nDISK=sda\nSTORAGE_KIND=ufs\nIDENTITY_KIND=ufs-descriptor-v1\nCID='+'a'*32+'\nSECTOR_SIZE=4096\nSTART=2048\nSIZE=2097152\nDISK_SIZE=33554432\nFREE=1024\n'
        self.assertEqual(storage.parse_inventory(text)['START'],2048)
        for bad in (text.replace('DISK=sda','DISK=sdb'),text.replace('START=2048','START=2049'),text.replace('STORAGE_KIND=ufs','STORAGE_KIND=emmc'),text.replace('a'*32,'not-unique')):
            with self.assertRaises(ValueError):storage.parse_inventory(bad)

    def test_restore_original_4kn_and_512_gpt_after_shrink_or_free_install(self):
        from backend import uninstall
        for block in (512,4096):
            with self.subTest(block=block):
                first,last,total=snapshot(block)
                current=gpt.gpt_add_root(first,last,total,20*2097152,8*2097152,'e5root')
                restored,_,_,wipe=uninstall.device_layout(current,total,'e5root')
                self.assertFalse(wipe)
                self.assertEqual(restored,[first,last])
                # Simulate the actual carve result by shrinking the userdata
                # entry, then adding root into exactly its former tail.
                updated=bytearray(uninstall.table(first));factor=block//512
                target=8*2097152
                struct.pack_into('<Q',updated,128+40,(4096+target)//factor-1)
                shrunk=uninstall.with_table((first,last),updated,total)
                carved=gpt.gpt_add_root(*shrunk,total,4096+target,16*2097152-4096-target,'e5root')
                restored,_,_,wipe=uninstall.restored_layout((first,last),carved,total,'e5root')
                self.assertTrue(wipe)
                self.assertEqual(restored,[first,last])


if __name__=='__main__':unittest.main()
