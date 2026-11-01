"""Free-space inspection and resumable growth. No real device is accessed."""
import hashlib
import io
import json
import random
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_uninstall as fx
from backend import expand_space as grow, gpt
from backend.service import Service
from backend.state import Context, RecoveryRequired, Cancelled
from backend.uninstall import table
from backend.uninstall_growth import PreservationUnavailable, inspect_android_growth


def native_copies(first, sectors, block):
    """Translate the same entries to native 512/4096-LBA GPT fixtures."""
    data=bytearray(table(first));factor=block//512;logical=sectors//factor
    for at in range(0,len(data),128):
        if data[at:at+16]!=bytes(16):
            start,end=struct.unpack_from('<QQ',data,at+32)
            assert start%factor==0 and (end+1)%factor==0
            struct.pack_into('<QQ',data,at+32,start//factor,(end+1)//factor-1)
    count=len(data)//block
    def header(current,alternate,at):
        raw=bytearray(block)
        struct.pack_into('<8sIIIIQQQQ16sQIII',raw,0,b'EFI PART',65536,92,0,0,
            current,alternate,2+count,logical-count-2,b'G'*16,at,128,128,zlib.crc32(data))
        struct.pack_into('<I',raw,16,zlib.crc32(raw[:92]));return bytes(raw)
    return bytes(block)+header(1,logical-1,2)+data,bytes(data)+header(logical-1,1,logical-count-1)


class Geometry(unittest.TestCase):
    def test_randomized_native_sector_growth_preserves_every_other_entry(self):
        rng=random.Random(20261031)
        original,_=fx.layouts()
        for case in range(256):
            block=rng.choice((512,4096));total=rng.choice((fx.SECTORS,2*(1<<40)//512))
            raw=bytearray(table(original[0]));size=rng.randrange(2,45)*(1<<30)
            struct.pack_into('<Q',raw,3*128+40,fx.GIB//512+size//512-1)
            before=native_copies(fx.copies(raw)[0],total,block)
            info=gpt.gpt_space(*before,total);info.update(first=before[0],last=before[1])
            summary=grow.describe(info,{'storage':{'NODE':'sda4','DISK_SIZE':total}})
            extra=rng.randrange(1,summary['recoverable_bytes']//4096+1)*4096
            after,part=grow.grow_layout(before,total,size+extra)
            a,b=bytearray(table(before[0])),bytearray(table(after[0]));at=(part['n']-1)*128+40
            a[at:at+8]=b[at:at+8]
            self.assertEqual(a,b,case)
            self.assertEqual((part['end']-part['start']+1)*512,size+extra)
    def test_emmc_ufs_native_lba_and_large_disks_only_change_userdata_end(self):
        original,_=fx.layouts()
        for block in (512,4096):
            for total in (fx.SECTORS,4*(1<<40)//512):
                with self.subTest(block=block,total=total):
                    before=native_copies(original[0],total,block)
                    info=gpt.gpt_space(*before,total);info.update(first=before[0],last=before[1])
                    device={'storage':{'DISK_SIZE':total,'NODE':'sda4'}}
                    summary=grow.describe(info,device)
                    self.assertGreater(summary['recoverable_bytes'],0)
                    target=summary['userdata_bytes']+summary['recoverable_bytes']
                    after,new=grow.grow_layout(before,total,target)
                    a,b=bytearray(table(before[0])),bytearray(table(after[0]))
                    offset=(new['n']-1)*128+40
                    a[offset:offset+8]=b[offset:offset+8]
                    self.assertEqual(a,b)
                    self.assertEqual(new['start'],fx.GIB//512)
                    self.assertEqual(summary['disk_bytes'],total*512)
                    self.assertEqual(target%4096,0)
                    with self.assertRaises(RecoveryRequired):grow.grow_layout(before,total,target+4096)

    def test_does_not_reclaim_other_partitions_or_known_unlisted_filesystems(self):
        original,with_root=fx.layouts()
        for first,last in (original,with_root):
            info=gpt.gpt_space(first,last,fx.SECTORS);info.update(first=first,last=last)
            device={'storage':{'DISK_SIZE':fx.SECTORS,'NODE':'mmcblk0p4'}}
            summary=grow.describe(info,device)
            if (first,last)==tuple(with_root):
                self.assertEqual(summary['recoverable_bytes'],0)
                self.assertGreater(summary['unallocated_bytes'],0)
                with self.assertRaises(RecoveryRequired):grow.grow_layout((first,last),fx.SECTORS,32*fx.GIB)
            else:
                data=next(p for p in info['parts'] if p['name']=='userdata')
                info['gap_signatures']=[{'offset':data['end']+2048,'kind':'ext4'}]
                blocked=grow.describe(info,device)
                self.assertEqual(blocked['recoverable_bytes'],0)
                self.assertTrue(any(p['reason']=='filesystem' for p in blocked['gaps']))

    def test_checksums_and_noninteger_or_unaligned_sizes_are_rejected(self):
        before,_=fx.layouts();bad=bytearray(before[1]);bad[0]^=1
        for current,size in ((before,33*fx.GIB+1),(before,float(33*fx.GIB)),((before[0],bytes(bad)),33*fx.GIB)):
            with self.subTest(size=size),self.assertRaises((ValueError,RecoveryRequired)):
                grow.grow_layout(current,fx.SECTORS,size)

    def test_hidden_signature_at_unaligned_gap_start_is_not_reclaimed(self):
        original,_=fx.layouts();data=bytearray(table(original[0]))
        end=struct.unpack_from('<Q',data,3*128+40)[0]
        struct.pack_into('<Q',data,3*128+40,end-8)
        before=fx.copies(data);info=gpt.gpt_space(*before,fx.SECTORS)
        info.update(first=before[0],last=before[1]);head=end-7
        def read(cmd):
            raw=bytearray(2048)
            if 'skip='+str(head)+' ' in cmd:raw[1080:1082]=b'\x53\xef'
            return raw
        adapter=SimpleNamespace(kind='e5',adb=SimpleNamespace(read=Mock(side_effect=read)))
        _,summary=grow.inspect_space(adapter,{'storage':{'DISK_SIZE':fx.SECTORS,'NODE':'sda4'}},info)
        self.assertEqual(summary['recoverable_bytes'],0)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.fx=fx.Journal();self.fx.setUp()
        self.device=self.fx.device
        self.device.update(userdata_bytes=32*fx.GIB,encrypted=False,encryption_state='unencrypted',encryption_type='',boot_id='oldboot')
        self.device['storage'].update(SIZE=32*fx.GIB//512,SOURCE='/dev/block/mmcblk0p4')
        self.adb=self.fx.adb;self.adapter=self.fx.adapter
        self.before=fx.layouts()[0]
        info=gpt.gpt_space(*self.before,fx.SECTORS);info.update(first=self.before[0],last=self.before[1])
        self.adapter.read_gpt=Mock(return_value=info)
        self.adapter.root=Path(self.fx.temp.name)
        self.adapter.probe=Mock(return_value=self.device)
        self.bcb=bytes(2048)+fx.android_bc()+bytes(2016)
        self.adb.read=Mock(side_effect=lambda cmd:bytes(2048) if 'bs=512 skip=' in cmd else self.bcb)
        self.adb.shell=Mock(side_effect=lambda cmd,**_:self.volume['source'] if cmd.startswith('readlink -f ') else str(32*fx.GIB))
        self.volume={'source':'/dev/block/mmcblk0p4','mapped_bytes':32*fx.GIB,'filesystem_bytes':32*fx.GIB,
                     'uuid':'55'*16,'filesystem':'ext4','mapping_overhead':0}
        self.tools={'openwrt-uninstall/tools/lib/fixture':b'elf'}
        self.adapter.password='';self.adb.call=Mock()
        self.vol_patch=patch.object(grow,'android_volume',side_effect=lambda *_a,**_k:dict(self.volume));self.vol_patch.start()
        self.tools_patch=patch.object(grow,'growth_tools',return_value=self.tools);self.tools_patch.start()
    def tearDown(self):self.vol_patch.stop();self.tools_patch.stop();self.fx.tearDown()
    def plan(self,authorize=None):
        plan,meta,files=grow.prepare(self.adapter,self.device,authorize)
        r=self.fx.store.create(self.fx.package,self.device,{'method':'expand'},plan)
        ctx=Context(self.fx.store,r,lambda *_:None);grow.persist(ctx,meta,files)
        return grow.Expander(None,ctx,self.adapter)
    def test_plan_is_readonly_and_does_not_require_original_installation_record(self):
        worker=self.plan()
        self.assertFalse(worker.r['plan']['wipe'])
        self.assertEqual(worker.meta['action'],'grow-online-ext4')
        self.assertGreater(worker.r['plan']['reclaimed_bytes'],0)
        self.assertFalse(any('of=' in c.args[0] for c in self.adb.shell.call_args_list))
        self.adb.call.assert_not_called()
        worker.ready();self.adapter.probe.assert_called_with(self.device['serial'],'expand',gpt_scan=False)

    def test_foreign_recovery_request_stops_before_plan(self):
        self.adb.read.side_effect=None
        self.adb.read.return_value=b'boot-recovery'.ljust(2048,b'\0')+fx.android_bc()+bytes(2016)
        with self.assertRaises(RecoveryRequired):self.plan()
        self.adb.call.assert_not_called()

    def test_mount_alias_is_resolved_before_geometry_checks(self):
        self.device['storage']['SOURCE']='/dev/block/by-name/userdata'
        worker=self.plan()
        self.assertFalse(worker.r['plan']['wipe'])
        self.assertTrue(any(c.args[0]=='readlink -f /dev/block/by-name/userdata' for c in self.adb.shell.call_args_list))

    def test_explicit_reset_is_evidence_bound_and_not_automatic(self):
        self.volume['filesystem']='unsupported'
        with self.assertRaises(PreservationUnavailable) as caught:self.plan()
        fingerprint=caught.exception.fingerprint
        worker=self.plan(lambda actual:actual==fingerprint)
        self.assertTrue(worker.r['plan']['wipe']);self.assertEqual(worker.meta['action'],'reset')
        self.adb.call.assert_not_called()
        self.device['encryption_type']='changed'
        with self.assertRaises(PreservationUnavailable):self.plan(lambda actual:actual==fingerprint)

    def test_partial_gpt_resume_and_foreign_change_detection(self):
        worker=self.plan();a,b=worker.files['after-first'],worker.files['after-last']
        for current,expected in ((self.before,'before'),((a,b),'after'),((self.before[0],b),'partial'),((a,self.before[1]),'partial')):
            with patch.object(worker,'current',return_value=current):self.assertEqual(worker.layout_state(),expected)
        with patch.object(worker,'current',return_value=(b'bad',b)),self.assertRaises(RecoveryRequired):worker.layout_state()
        worker.files['after-first']=self.before[0]
        path=worker.folder/'after-first';path.write_bytes(self.before[0]);worker.meta['checksums']['after-first']=hashlib.sha256(self.before[0]).hexdigest()
        with self.assertRaises(RecoveryRequired):grow.Expander(None,worker.ctx,self.adapter)

    def test_lost_gpt_receipt_is_reconciled_without_another_write(self):
        worker=self.plan()
        with patch.object(worker,'layout_state',return_value='after'):worker.commit_gpt()
        self.assertTrue(worker.r['expand_committed']);self.assertEqual(worker.r['effects']['expand-gpt']['state'],'done')
        self.adb.call.assert_not_called()

    def test_active_disconnected_worker_prevents_duplicate_write(self):
        worker=self.plan();self.adb.shell.side_effect=None;self.adb.shell.return_value='BUSY'
        with self.assertRaisesRegex(RecoveryRequired,'still active'):worker.commit_gpt()
        self.adb.call.assert_not_called()

    def test_emitted_device_library_and_checksum_list_use_unix_bytes(self):
        worker=self.plan();worker.layout_state=Mock(side_effect=['before','after']);worker.backup=Mock()
        self.adb.shell=Mock(side_effect=lambda cmd,**_:'EXPAND-GPT-VERIFIED\n' if cmd.startswith('sh ') else '')
        with patch('backend.storage.library',return_value='helper() {\n echo verified\n}\n'):
            worker.commit_gpt()
        for name in ('storage.sh','SHA256SUMS','commit.sh'):
            with self.subTest(name=name):
                data=(worker.folder/name).read_bytes()
                self.assertIn(b'\n',data);self.assertNotIn(b'\r',data)

    def test_wrong_data_partition_never_becomes_a_preserving_or_reset_plan(self):
        self.volume['source']='/dev/block/mmcblk0p6'
        with self.assertRaisesRegex(RecoveryRequired,'userdata'):
            self.plan(lambda _:True)
        self.adb.call.assert_not_called()

    def test_resize_still_running_after_connection_loss_cannot_complete_or_restart(self):
        worker=self.plan();target=worker.r['plan']['android_bytes']
        self.volume.update(mapped_bytes=target,filesystem_bytes=target)
        worker.ready=Mock(return_value={**self.device,'boot_id':'new','userdata_bytes':target})
        worker.layout_state=Mock(return_value='after');worker.r['effects']['expand-gpt']={'state':'done'}
        self.adb.shell=Mock(side_effect=lambda cmd,**_:'BUSY' if 'ext4.sh' in cmd else '')
        with self.assertRaisesRegex(RecoveryRequired,'still active'):worker.run()
        self.assertNotEqual(worker.r['status'],'completed');self.adb.call.assert_not_called()

    def test_final_checks_require_uuid_full_mapping_and_filesystem_capacity(self):
        worker=self.plan();expected=worker.r['plan']['android_bytes']
        device={**self.device,'userdata_bytes':expected}
        volume={**self.volume,'mapped_bytes':expected,'filesystem_bytes':expected}
        for changed in ({'uuid':'66'*16},{'mapped_bytes':expected-8*fx.GIB},{'filesystem_bytes':32*fx.GIB}):
            with self.subTest(changed=changed),self.assertRaises(RecoveryRequired):worker.finish(device,{**volume,**changed})
        self.adb.read.side_effect=None;self.adb.read.return_value=bytes(2048)
        with patch.object(worker,'layout_state',return_value='after'):worker.finish(device,volume)
        self.assertEqual(worker.r['status'],'completed');self.assertTrue(worker.r['result']['data_preserved'])
        self.assertFalse(any('of=/dev' in c.args[0] for c in self.adb.shell.call_args_list))

    def test_runtime_fallback_requires_yes_and_new_final_plan_without_writing(self):
        worker=self.plan()
        with self.assertRaises(grow.FormatRequired):worker.offer_reset('stock cannot grow')
        self.assertEqual(worker.r['status'],'waiting_user')
        service=SimpleNamespace(store=self.fx.store,emit=lambda *_:None,adapter=lambda *_:self.adapter,public=lambda r:r)
        offer=worker.r['expand_reset_offer'];params={'nonce':offer['nonce'],'confirmation':'yes'}
        with patch.object(grow.Expander,'layout_state',return_value='after'):
            for wrong in ({'nonce':'wrong'},{'confirmation':'YES'}):
                with self.assertRaises(ValueError):grow.confirm_reset(service,worker.r,{**params,**wrong})
            result=grow.confirm_reset(service,worker.r,params)
        self.assertEqual(result['status'],'ready');self.assertTrue(result['plan']['wipe'])
        self.adb.call.assert_not_called()
        grow.Expander(None,worker.ctx,self.adapter)

    def test_reset_request_does_not_touch_bc_ab_and_is_not_reissued_after_return(self):
        self.volume['filesystem']='unsupported';worker=self.plan(lambda _:True)
        folder=worker.ctx.directory/'backup';folder.mkdir();(folder/'misc.bin').write_bytes(self.bcb)
        self.adb.read.side_effect=None;self.adb.read.return_value=self.bcb[:2048]
        with patch.object(worker,'reboot'):
            worker.reset(self.device)
        content=(worker.folder/'reset-bcb').read_bytes()
        self.assertEqual(len(content),2048);self.assertEqual(content[:32].split(b'\0')[0],b'boot-recovery')
        self.assertIn(b'--wipe_data',content);self.assertTrue(worker.r['expand_reset_armed'])
        script=self.adb.shell.call_args.args[0]
        self.assertIn('bs=2048 count=1 conv=notrunc,fsync',script)
        self.assertNotIn('boot_b',script);self.assertNotIn('userdata',script)
        worker.ready=Mock(return_value={**self.device,'boot_id':'new','userdata_bytes':worker.r['plan']['android_bytes']})
        worker.layout_state=Mock(return_value='after');worker.r['effects']['expand-gpt']={'state':'done'}
        worker.r.update(expand_android_growth_attempted=True,expand_android_growth_restored=True)
        with patch.object(worker,'reset') as reset,patch.object(worker,'finish'),patch.object(grow.android_growth_boot,'restore') as restore:
            self.volume.update(mapped_bytes=worker.r['plan']['android_bytes'],filesystem_bytes=worker.r['plan']['android_bytes'])
            worker.run();reset.assert_not_called();restore.assert_not_called()

    def test_uncertain_boot_write_never_offers_a_reset(self):
        worker=self.plan();worker.r['unsafe']=True
        with self.assertRaises(RecoveryRequired):worker.offer_reset('write pending')
        self.assertNotIn('expand_reset_offer',worker.r)

    def test_missing_backup_after_reconnect_is_reported_not_hidden_as_a_disconnect(self):
        worker=self.plan();worker.ready=Mock(return_value={**self.device,'boot_id':'next'})
        worker.run=Mock(side_effect=FileNotFoundError('backup missing'))
        with self.assertRaises(FileNotFoundError):worker.wait()

    def test_device_identity_changes_stop_before_any_mutation(self):
        worker=self.plan()
        for change in ({'cid':'a'*32},{'slot':'_b'},{'storage':{**self.device['storage'],'NODE':'mmcblk0p7'}},
                       {'storage':{**self.device['storage'],'DISK_SIZE':fx.SECTORS-8}},
                       {'storage':{**self.device['storage'],'START':self.device['storage']['START']+8}}):
            with self.subTest(change=change):
                self.adapter.probe.return_value={**self.device,**change}
                with self.assertRaises(RecoveryRequired):worker.ready()
        self.adb.call.assert_not_called();self.assertFalse(worker.r['effects'])

    def test_backup_corruption_or_pause_prevents_gpt_write(self):
        worker=self.plan();worker.layout_state=Mock(return_value='before');self.adb.cleanup_backup=Mock()
        self.adb.backup_partition=Mock(side_effect=lambda name,path,ctx:Path(path).write_bytes(self.bcb))
        worker.backup()
        (worker.ctx.directory/'backup/gpt-first.bin').write_bytes(b'corrupt')
        with self.assertRaisesRegex(RecoveryRequired,'backup failed'):worker.commit_gpt()
        self.adb.call.assert_not_called()
        other=self.plan();other.layout_state=Mock(return_value='before');other.ctx.cancel=True
        with self.assertRaises(Cancelled):other.commit_gpt()
        self.adb.call.assert_not_called()

    def test_resize_failure_never_formats_and_pauses_for_a_separate_decision(self):
        worker=self.plan();target=worker.r['plan']['android_bytes']
        self.volume.update(mapped_bytes=target)
        worker.ready=Mock(return_value={**self.device,'boot_id':'new','userdata_bytes':target})
        worker.layout_state=Mock(return_value='after');worker.r['effects']['expand-gpt']={'state':'done'}
        worker.grow_ext4=Mock(return_value='kernel resize refused')
        with patch.object(worker,'reset') as reset,self.assertRaises(grow.FormatRequired):worker.run()
        reset.assert_not_called();self.assertEqual(worker.r['status'],'waiting_user')
        self.assertFalse(worker.r['plan']['wipe']);self.assertFalse(worker.meta['reset_authorized'])

    def test_ext4_lost_receipt_or_changed_tools_never_authorizes_a_reset(self):
        worker=self.plan()
        def shell(cmd,**_):
            if cmd.startswith('for p in /proc/'):return ''
            if cmd.startswith('sha256sum '):return hashlib.sha256(b'elf').hexdigest()+' file'
            if cmd.startswith('sh '):return 'resize output without receipt'
            return ''
        self.adb.shell=Mock(side_effect=shell)
        with self.assertRaisesRegex(RecoveryRequired,'receipt'):worker.grow_ext4(self.device,self.volume)
        self.assertFalse(worker.r['plan']['wipe']);self.assertNotIn('expand_reset_offer',worker.r)
        self.tools_patch.stop()
        with patch.object(grow,'growth_tools',return_value={'wrong':b'changed'}),self.assertRaisesRegex(RecoveryRequired,'differ'):
            worker.grow_ext4(self.device,self.volume)

    @unittest.skipUnless(sys.platform.startswith('linux'),'Real process-argument fixtures')
    def test_orphan_resize_is_detected_without_blocking_unrelated_resizers(self):
        worker=self.plan();remote=worker._remote()
        folder=worker.ctx.directory/'proc';(folder/'123').mkdir(parents=True)
        def shell(cmd,**_):
            cmd=cmd.replace('/proc/[0-9]*/cmdline',str(folder/'[0-9]*/cmdline'))
            result=subprocess.run(['bash','-c',cmd],text=True,capture_output=True,check=True)
            return result.stdout
        self.adb.shell=Mock(side_effect=shell)
        for args,busy in ((['sh',remote+'/ext4.sh'],True),
                          ([remote+'/tools/lib/ld-musl-aarch64.so.1','--library-path',remote+'/tools/lib',remote+'/tools/usr/sbin/resize2fs','/dev/dm-7'],True),
                          (['resize2fs','/another/transaction','/dev/dm-7'],False)):
            with self.subTest(args=args):
                (folder/'123/cmdline').write_bytes(b'\0'.join(a.encode() for a in args)+b'\0')
                if busy:
                    with self.assertRaisesRegex(RecoveryRequired,'still active'):worker.ensure_idle('ext4.sh')
                else:worker.ensure_idle('ext4.sh')

    def test_new_operation_requires_yes_token_and_plan_confirmation_in_both_families(self):
        self.volume['filesystem']='unsupported'
        for family in ('e5','mu300'):
            with self.subTest(family=family):
                service=Service('.',self.fx.store,io.StringIO());service.package={**self.fx.package,'adapter':family}
                service.adapters=lambda *_:self.adapter
                self.device['cid']=hashlib.md5(family.encode()).hexdigest()
                # Opposite-slot guard reads only its header. Planning never writes it.
                self.adb.read.side_effect=lambda cmd: bytes(4096) if 'by-name/boot_' in cmd else bytes(2048) if 'bs=512 skip=' in cmd else self.bcb
                def request(**params):
                    return service.dispatch({'protocol':1,'command':'plan','params':{
                        'serial':self.device['serial'],'choices':{'method':'expand'},**params}})
                prompt=request()
                self.assertEqual(prompt['operation'],'expand')
                again=request(reset_token=prompt['reset_token'])
                self.assertEqual(again['confirmation_required'],'format_android')
                plan=request(reset_token=again['reset_token'],reset_confirmation='yes')
                self.assertEqual(plan['status'],'ready');self.assertTrue(plan['plan']['wipe'])
                with self.assertRaisesRegex(ValueError,'Confirm the displayed plan'),patch('backend.service.inspect_package',return_value=service.package):
                    service.dispatch({'protocol':1,'command':'start','params':{'transaction':plan['id']}})
                self.adb.call.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'),'Real POSIX GPT write fault fixtures')
    def test_real_commit_worker_completes_interrupted_backup_first_write(self):
        # A sparse ordinary file represents a disk; only its tiny GPT regions
        # are written. No loop devices, Android or real partitions are used.
        for state in ('before','partial','foreign'):
            with self.subTest(state=state):
                self.adb.read=Mock(side_effect=lambda cmd:bytes(2048) if 'bs=512 skip=' in cmd else self.bcb)
                self.adb.shell=Mock(side_effect=lambda cmd,**_:self.volume['source'] if cmd.startswith('readlink -f ') else str(32*fx.GIB))
                worker=self.plan();remote=worker.ctx.directory/'remote';remote.mkdir()
                disk=worker.ctx.directory/'disk';size=worker.ctx.directory/'sectors';size.write_text(str(fx.SECTORS))
                with disk.open('wb') as f:
                    f.truncate(fx.SECTORS*512);f.seek(0);f.write(worker.files['before-first'])
                    f.seek(worker.meta['tail']*512)
                    f.write(worker.files['after-last' if state=='partial' else 'before-last'])
                    if state=='foreign':f.seek(0);f.write(b'foreign')
                def read(cmd):
                    offset=worker.meta['tail']*512 if 'skip=' in cmd else 0
                    length=(worker.meta['total']-worker.meta['tail'])*512 if offset else worker.meta['front']*512
                    with disk.open('rb') as f:f.seek(offset);return f.read(length)
                def call(op,local,target,**_):
                    self.assertEqual(op,'push');Path(target).write_bytes(Path(local).read_bytes())
                def shell(cmd,**_):
                    if cmd.startswith('for p in /proc/'):return ''
                    if cmd.startswith('sh '):
                        path=Path(cmd[3:]);text=path.read_text().replace('/dev/block/mmcblk0',str(disk)).replace('/sys/class/block/mmcblk0/size',str(size))
                        path.write_text(text)
                        result=subprocess.run(['bash',str(path)],capture_output=True,text=True)
                        if result.returncode:raise RuntimeError(result.stderr)
                        return result.stdout
                    return ''
                self.adb.read=Mock(side_effect=read);self.adb.shell=Mock(side_effect=shell);self.adb.call=Mock(side_effect=call)
                worker._remote=lambda:str(remote);worker.backup=Mock()
                library='openwrt_storage_identity() { echo '+self.device['cid']+'; }\ngetprop() { echo '+self.device['slot']+'; }\n'
                with patch('backend.storage.library',return_value=library):
                    if state=='foreign':
                        with self.assertRaises(RecoveryRequired):worker.commit_gpt()
                        worker.backup.assert_not_called()
                    else:
                        worker.commit_gpt();self.assertEqual(worker.layout_state(),'after')
                        self.assertTrue(worker.r['expand_committed'])
                        script=(remote/'commit.sh').read_text()
                        self.assertLess(script.index('dd if=after-last'),script.index('dd if=after-first'))
                        self.assertNotIn('BLKRRPART',script.replace('never request BLKRRPART',''))
                        self.assertNotIn('boot_b',script);self.assertNotIn('mkfs',script)


class StockEvidence(unittest.TestCase):
    def test_raw_and_dm_sources_must_belong_to_the_current_userdata(self):
        adb=SimpleNamespace(shell=Mock(return_value='sda75'))
        device={'storage':{'NODE':'sda75'}}
        grow.verify_data_source(adb,device,'/dev/block/sda75');adb.shell.assert_not_called()
        grow.verify_data_source(adb,device,'/dev/block/dm-8')
        for slaves in ('sda76','sda75 sda76','dm-7',''):
            adb.shell.return_value=slaves
            with self.subTest(slaves=slaves),self.assertRaises(RecoveryRequired):
                grow.verify_data_source(adb,device,'/dev/block/dm-8')
    def test_file_encrypted_raw_f2fs_and_metadata_encrypted_mapper_are_separate(self):
        binary=bytearray(64);binary[:6]=b'\x7fELF\x02\x01';struct.pack_into('<H',binary,18,183)
        manager=bytes(binary)+b'/system/bin/resize.f2fs\0'
        for mapping in ('raw','dm'):
            device={'encrypted':True,'encryption_state':'encrypted','encryption_type':'file','userdata_bytes':32*fx.GIB,'storage':{'NODE':'sda75'}}
            volume={'filesystem':'f2fs','mapping_overhead':0,'mapped_bytes':32*fx.GIB,'uuid':'55'*16,
                    'source':'/dev/block/sda75' if mapping=='raw' else '/dev/block/dm-7'}
            adb=SimpleNamespace(shell=Mock(side_effect=lambda cmd,**_: 'sda75' if '/slaves/' in cmd else '/system/lib64/libfs_mgr.so'),
                                read=Mock(side_effect=lambda cmd:manager if 'libfs_mgr' in cmd else bytes(binary)))
            proof=inspect_android_growth(SimpleNamespace(adb=adb),device,volume,mapping=mapping)
            self.assertEqual(proof['android_growth'].get('mapping_kind','dm'),mapping)
            device['encryption_type']='block'
            with self.assertRaises(ValueError):inspect_android_growth(SimpleNamespace(adb=adb),device,volume,mapping=mapping)


if __name__=='__main__':unittest.main()
