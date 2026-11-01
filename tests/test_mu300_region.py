"""Upstream GPT/raw-layout uninstall, with metadata and transports in memory."""
import hashlib
import copy
import struct
from pathlib import Path
from types import SimpleNamespace
import re
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_uninstall as fx
import test_uninstall_evidence as evidence
import test_storage_ufs as ufs
from backend import gpt, mu300_region as region
from backend.install_evidence import inspect_installation
from backend.state import Store, Context, RecoveryRequired
from backend.uninstall import prepare, persist, Uninstaller
from backend.uninstall_boot import overlay
from backend.adapter import Adapter


def boot():
    base=fx.boot('mu300')
    init=b'#!/bin/busybox sh\nis_mu300root() { :; }\nfind_root_offset() { :; }\n'
    ram=overlay({'init':init});head=bytearray(base[:4096]);struct.pack_into('<I',head,12,len(ram))
    body=(bytes(head)+base[4096:8192]+ram);body=body.ljust((len(body)+4095)&~4095,b'\0')
    footer=bytearray(base[-64:]);struct.pack_into('>QQQ',footer,12,len(body),len(body),128)
    return (body+b'V'*128).ljust(len(base)-64,b'\0')+footer


class Region(unittest.TestCase):
    def setUp(self):
        self.fx=fx.Journal();self.fx.setUp();self.addCleanup(self.fx.tearDown)
        self.fx.configure('mu300');self.device=self.fx.device
        self.store=Store(Path(self.fx.temp.name)/'fresh')
        table=bytearray(self.fx.before[0][1024:]);table[4*128:5*128]=bytes(128)
        self.current=fx.copies(table)
        info=gpt.gpt_space(*self.current,fx.SECTORS)
        self.info={**info,'first':self.current[0],'last':self.current[1]}
        data=next(p for p in info['parts'] if p['name']=='userdata')
        self.start=((data['end']+1)//4096+1)*4096;self.size=4*fx.GIB//512
        self.root=region.geometry(self.info,self.start,self.size)
        self.device['storage'].update(ROOT='mmcblk0',ROOT_LAYOUT=region.LAYOUT,ROOT_START=self.start,ROOT_SIZE=self.size)
        self.device.update(encrypted=False,encryption_state='unencrypted',encryption_type='')
        self.fs=bytearray(evidence.image({'.mu300/boot-os':b'openwrt\n',
                           'openwrt/etc/openwrt_release':b"DISTRIB_ID='OpenWrt'\n"}))
        self.fs[1144:1160]=b'mu300root'.ljust(16,b'\0')
        struct.pack_into('<I',self.fs,1028,self.size*512//4096)
        self.boot=boot();self.idle='IDLE'
        def shell(command,**kwargs):
            if 'echo IDLE' in command:return self.idle
            if command.startswith('blockdev '):return str(64 << 20)
            if command.startswith('readlink '):return '/dev/block/mmcblk0p4'
            if command.startswith('cat /sys/class/block/mmcblk0p4/size'):return str(28*fx.GIB//512)
            raise AssertionError(command)
        def read(command):
            match=re.fullmatch(r'dd if=(\S+) bs=(\d+)(?: skip=(\d+))? count=(\d+) 2>/dev/null',command)
            if not match:raise AssertionError(command)
            path,bs,skip,count=match[1],int(match[2]),int(match[3] or 0),int(match[4])
            offset=bs*skip;length=bs*count
            if path.endswith('boot_'+('a' if self.device['slot']=='_b' else 'b')):return self.boot[offset:offset+length]
            if path.endswith('mmcblk0p5'):return bytes(self.fs[offset:offset+length])
            if path.endswith('mmcblk0p4'):return (bytes(1024)+fx.superblock(28*fx.GIB))[offset:offset+length]
            if path.endswith('mmcblk0'):
                at=offset-self.start*512
                return bytes(self.fs[at:at+length]).ljust(length,b'\0') if at>=0 else bytes(length)
            raise AssertionError(command)
        self.fx.adb.shell=Mock(side_effect=shell);self.fx.adb.read=Mock(side_effect=read)
        self.fx.adapter.read_gpt=Mock(return_value=self.info)

    def plan(self):
        with patch('backend.uninstall.growth_tools',return_value={'tools/fixture':b'test'}),patch('backend.uninstall.base_boot'),patch('backend.uninstall.build'):
            plan,meta,files=prepare(self.fx.adapter,self.store,self.device)
        record=self.store.create(self.fx.package,self.device,{'method':'uninstall'},plan)
        context=Context(self.store,record,lambda *_:None);persist(context,meta,files)
        return context

    def test_raw_proof_and_removal_only_extend_userdata_to_the_reviewed_region_end(self):
        proof=inspect_installation(self.fx.adapter,self.device,self.root)
        self.assertEqual(proof['upstream_os'],'openwrt')
        self.assertEqual(proof['region_header_sha256'],hashlib.sha256(self.fs[:4096]).hexdigest())
        ctx=self.plan();Uninstaller(None,ctx,self.fx.adapter)
        self.assertEqual(ctx.record['uninstall']['version'],4)
        self.assertFalse(ctx.record['plan']['wipe'])
        self.assertEqual(ctx.record['plan']['android_bytes'],(self.start+self.size)*512-fx.GIB)
        after=gpt.gpt_space((ctx.directory/'uninstall/after-first').read_bytes(),(ctx.directory/'uninstall/after-last').read_bytes(),fx.SECTORS)
        for part in self.info['parts']:
            updated=next(p for p in after['parts'] if p['n']==part['n'])
            self.assertEqual(updated,dict(part,end=self.root['end']) if part['name']=='userdata' else part)
        for call in self.fx.adb.shell.call_args_list:self.assertNotIn('of=',call.args[0])

    def test_discovery_is_explicit_mu300_only_and_reads_known_gap_starts_only(self):
        del self.device['storage']['ROOT']
        self.assertEqual(region.discover(self.fx.adapter,self.device,self.info),self.root)
        self.fx.adapter.kind='e5';self.fx.adb.read.reset_mock()
        self.assertIsNone(region.discover(self.fx.adapter,self.device,self.info))
        self.fx.adb.read.assert_not_called()

    def test_actual_probe_never_enters_upstream_discovery_for_install_update_or_expand(self):
        adapter=Adapter.__new__(Adapter)
        adapter.kind='mu300';adapter.root=Path(self.fx.temp.name);adapter.ctx=None;adapter.bind=Mock()
        adapter.adb=SimpleNamespace(devices=Mock(return_value=[{'serial':'selected','state':'device'}]),
            properties=Mock(return_value={'ro.product.model':'F50','ro.product.device':'MU300',
                'ro.board.platform':'ums9620','ro.boot.slot_suffix':'_a','sys.boot_completed':'1',
                'ro.crypto.state':'unencrypted','ro.crypto.type':''}),root_status=Mock(return_value='ready'),
            shell=Mock(return_value='1'))
        adapter.read_gpt=Mock(return_value=self.info)
        inventory={k:v for k,v in self.device['storage'].items() if not k.startswith('ROOT')}
        inventory.update(CID=fx.IDENTITY,FREE=1024)
        for method in ('tf','part','reinstall','image','boot','expand','uninstall'):
            with self.subTest(method=method),patch('backend.adapter.mu300_android',return_value=('f50',None)), \
                    patch('backend.adapter.storage_io.parse_inventory',return_value=copy.deepcopy(inventory)), \
                    patch('backend.mu300_region.discover',return_value=self.root) as discover, \
                    patch('backend.expand_space.inspect_space',return_value=(self.info,{})):
                result=adapter.probe('selected',method)
                self.assertEqual(discover.call_count,1 if method=='uninstall' else 0)
                self.assertEqual(result['existing'],method=='uninstall')

    def test_upstream_gpt_partition_and_both_android_slots_use_the_same_proof(self):
        for slot in ('_a','_b'):
            self.device['slot']=slot
            self.assertEqual(inspect_installation(self.fx.adapter,self.device,self.root)['upstream_os'],'openwrt')
        self.device['slot']='_a'
        self.device['storage'].pop('ROOT_LAYOUT')
        self.device['storage'].update(ROOT='mmcblk0p5',ROOT_START=29*fx.GIB//512)
        self.fx.adapter.read_gpt.return_value={**gpt.gpt_space(*self.fx.before,fx.SECTORS),
                                              'first':self.fx.before[0],'last':self.fx.before[1]}
        self.root=next(p for p in self.fx.adapter.read_gpt.return_value['parts'] if p['name']=='mu300root')
        proof=inspect_installation(self.fx.adapter,self.device,self.root)
        self.assertEqual(proof['upstream_os'],'openwrt')
        ctx=self.plan();self.assertEqual(ctx.record['uninstall']['version'],3)
        Uninstaller(None,ctx,self.fx.adapter)

    def test_forged_larger_region_cannot_reclaim_beyond_the_filesystem(self):
        self.root['end']+=4096;self.device['storage']['ROOT_SIZE']+=4096
        with self.assertRaisesRegex(RecoveryRequired,'filesystem size'):
            inspect_installation(self.fx.adapter,self.device,self.root)

    def test_space_recovery_does_not_treat_an_upstream_system_as_empty(self):
        from backend.expand_space import inspect_space
        _,summary=inspect_space(self.fx.adapter,self.device,self.info)
        self.assertEqual(summary['recoverable_bytes'],0)
        self.assertTrue(any(row['reason']=='filesystem' for row in summary['gaps']))

    def test_raw_journal_recovery_is_bounded_and_rechecks_uuid_and_markers(self):
        struct.pack_into('<I',self.fs,1024+92,4)
        struct.pack_into('<I',self.fs,1024+96,0x46)
        struct.pack_into('<I',self.fs,1024+224,8)
        previous=self.fx.adb.shell.side_effect
        def shell(command,**kwargs):
            if 'journal_only' in command:
                self.assertIn('losetup -o '+str(self.start*512)+' -S '+str(self.size*512),command)
                self.assertIn('trap',command);self.assertIn('losetup -d',command)
                self.assertNotIn('mount ',command)
                struct.pack_into('<I',self.fs,1024+96,0x42)
                return ''
            return previous(command,**kwargs)
        self.fx.adb.shell.side_effect=shell
        ctx=self.plan();Uninstaller(None,ctx,self.fx.adapter)
        self.assertEqual(sum('journal_only' in c.args[0] for c in self.fx.adb.shell.call_args_list),1)

    def test_multiple_candidates_and_incomplete_reads_are_not_absent(self):
        del self.device['storage']['ROOT']
        raw=bytes(self.fs[:2048])
        self.fx.adb.read.return_value=raw;self.fx.adb.read.side_effect=None
        self.device['storage']['ROOT']='already-known'
        self.assertIsNone(region.discover(self.fx.adapter,self.device,self.info))
        del self.device['storage']['ROOT']
        # Move userdata below the legacy start, making both upstream starts
        # genuinely unallocated; two valid filesystems cannot be auto-selected.
        table=bytearray(self.current[0][1024:]);struct.pack_into('<Q',table,3*128+40,20*fx.GIB//512-1)
        first,last=fx.copies(table);info={**gpt.gpt_space(first,last,fx.SECTORS),'first':first,'last':last}
        with self.assertRaisesRegex(RecoveryRequired,'Multiple'):
            region.discover(self.fx.adapter,self.device,info)
        self.fx.adb.read.return_value=b''
        with self.assertRaisesRegex(RecoveryRequired,'Incomplete'):
            region.discover(self.fx.adapter,self.device,info)

    def test_bad_label_busy_loop_or_unknown_system_cannot_authorize_removal(self):
        for failure in ('label','busy','os','boot'):
            original=bytes(self.fs);idle=self.idle;boot_image=self.boot
            if failure=='label':self.fs[1144:1160]=b'mu300rootfs'.ljust(16,b'\0')
            if failure=='busy':self.idle='BUSY'
            if failure=='os':self.fs=bytearray(evidence.image({'.mu300/boot-os':b'foreign\n'}))
            if failure=='boot':self.boot=evidence.linux_boot('mu300')
            with self.subTest(failure=failure),self.assertRaises(RecoveryRequired):self.plan()
            self.fs=bytearray(original);self.idle=idle;self.boot=boot_image

    def test_imported_record_cannot_redirect_region_to_userdata_or_enlarge_it(self):
        ctx=self.plan()
        for field in ('root_start','root_size'):
            value=ctx.record['uninstall'][field];ctx.record['uninstall'][field]+=8
            with self.subTest(field=field),self.assertRaises(RecoveryRequired):Uninstaller(None,ctx,self.fx.adapter)
            ctx.record['uninstall'][field]=value
        ctx.record['package']['adapter']='e5'
        with self.assertRaises(RecoveryRequired):Uninstaller(None,ctx,self.fx.adapter)

    def test_512_and_4096_byte_gpt_layouts_and_other_entries_are_preserved(self):
        for block in (512,4096):
            first,last,total=ufs.snapshot(block)
            info={**gpt.gpt_space(first,last,total),'first':first,'last':last}
            end=max(p['end']+1 for p in info['parts']);start=(end//4096+1)*4096
            after,target,root,_=region.layout((first,last),total,start,self.size)
            check=gpt.gpt_space(*after,total)
            self.assertEqual(check['sector_size'],block)
            self.assertEqual(target['end'],root['end'])
            self.assertEqual(next(p for p in check['parts'] if p['name']=='misc'),info['parts'][0])
            with self.assertRaises(RecoveryRequired):region.layout((first,last),total,end-4096,self.size)


if __name__=='__main__':unittest.main()
