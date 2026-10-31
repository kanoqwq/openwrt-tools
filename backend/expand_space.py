"""Inspect GPT free space and grow Android without removing another partition.

Growing a GPT extent does not require unmounting Android: its running kernel
keeps the old (smaller) partition until reboot. No BLKRRPART is attempted on a
mounted userdata. Filesystem growth then runs through an explicit supported
Android route; stock recovery alone owns an explicitly authorized reset.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import time

from . import gpt, storage, android_growth_boot
from .state import Cancelled, Context, RecoveryRequired, atomic_json, sha
from .uninstall import android_volume, partition, table, with_table, uninstall_bcb
from .uninstall_growth import PreservationUnavailable, growth_tools, inspect_android_growth


def describe(space, device):
    """Include unaligned GPT holes too; installer allocation gaps round to MiB."""
    data = partition(space,'userdata')
    block = space['sector_size']; factor = block//512
    first, last = struct.unpack_from('<QQ',space['first'],block+40)
    first, last = first*factor,(last+1)*factor
    rows=[];at=first
    signatures=space.get('gap_signatures',[])
    for item in sorted(space['parts'],key=lambda p:p['start'])+[{'start':last,'end':last-1}]:
        if item['start']>at:
            end=item['start'];adjacent=at==data['end']+1
            occupied=any(at <= s['offset'] < end for s in signatures)
            limit=end//max(8,factor)*max(8,factor)
            recoverable=max(0,limit-at)*512 if adjacent and not occupied else 0
            reason='filesystem' if occupied else 'not_adjacent' if not adjacent else 'alignment' if not recoverable else ''
            rows.append({'start':at,'sectors':end-at,'start_bytes':at*512,'bytes':(end-at)*512,
                         'recoverable_bytes':recoverable,'reason':reason})
        at=item['end']+1
    available=sum(r['recoverable_bytes'] for r in rows)
    return {'disk_bytes':device['storage']['DISK_SIZE']*512,'userdata_bytes':(data['end']-data['start']+1)*512,
            'unallocated_bytes':sum(r['bytes'] for r in rows),'recoverable_bytes':available,
            'gaps':rows,'userdata_node':device['storage']['NODE'],'storage_kind':device['storage'].get('STORAGE_KIND','')}


def inspect_space(adapter,device,info=None):
    info=adapter.read_gpt(device['storage']) if info is None else info
    # The allocation scanner checks MiB-aligned starts. Also inspect each
    # actual raw gap start: expansion can consume the preceding partial MiB.
    info=dict(info);info['gap_signatures']=list(info.get('gap_signatures',[]))
    disk='/dev/block/'+storage.disk_node(device['storage']['NODE'])
    for row in describe(info,device)['gaps']:
        at=row['start']
        if at%2048==0 or row['sectors']<4:continue
        raw=adapter.adb.read(f'dd if={disk} bs=512 skip={at} count=4 2>/dev/null')
        if len(raw)!=2048:raise RecoveryRequired('Free-space signature read is incomplete / 空闲空间签名读取不完整')
        kind='ext4' if raw[1080:1082]==b'\x53\xef' else 'f2fs' if raw[1024:1028]==bytes.fromhex('1020f5f2') else None
        if kind:info['gap_signatures'].append({'offset':at,'kind':kind})
    return info,describe(info,device)


def grow_layout(current, total, target_bytes):
    """Only userdata.end and GPT checksums may change, in native GPT units."""
    space=gpt.gpt_space(*current,total)
    data=partition(space,'userdata')
    factor=space['sector_size']//512
    if type(target_bytes) is not int or target_bytes%4096:
        raise ValueError('Android target size must align to 4 KiB / Android 目标容量需按 4 KiB 对齐')
    sectors=target_bytes//512
    end=data['start']+sectors-1
    usable_end=(struct.unpack_from('<Q',current[0],space['sector_size']+48)[0]+1)*factor-1
    following=min((p['start'] for p in space['parts'] if p['start']>data['end']),default=usable_end+1)
    if sectors<=data['end']-data['start']+1 or end>=following or end>usable_end or (end+1)%factor:
        raise RecoveryRequired('Target is not adjacent unallocated space / 目标不在紧邻 userdata 的未分配空间内')
    updated=bytearray(table(current[0]))
    struct.pack_into('<Q',updated,(data['n']-1)*128+40,(end+1)//factor-1)
    after=with_table(current,updated,total)
    result=partition(gpt.gpt_space(*after,total),'userdata')
    if result['start']!=data['start'] or (result['end']-result['start']+1)*512!=target_bytes:
        raise RecoveryRequired('Expansion layout verification failed / 扩容布局校验失败')
    return after,result


def android_f2fs_proof(adapter, device, volume):
    raw='/dev/block/'+device['storage']['NODE']
    if volume.get('source') in (raw,raw.replace('/block/','/')):
        proof=inspect_android_growth(adapter,device,volume,mapping='raw')
    else:
        proof=inspect_android_growth(adapter,device,volume)
    slot=device['slot'].removeprefix('_')
    if slot not in ('a','b'):raise ValueError('Android boot slot unavailable / 无法确认 Android 启动槽位')
    path='/dev/block/by-name/boot_'+slot
    size=int(adapter.adb.shell('blockdev --getsize64 '+path,root=True).strip())
    header=adapter.adb.read('dd if='+path+' bs=4096 count=1 2>/dev/null')
    if (size!=64<<20 or len(header)!=4096 or header[:8]!=b'ANDROID!' or
            struct.unpack_from('<I',header,40)[0] not in (3,4) or
            not struct.unpack_from('<I',header,8)[0] or not struct.unpack_from('<I',header,12)[0]):
        raise ValueError('Current Android boot cannot use verified pre-mount maintenance / 当前 Android 启动格式不支持已核验的挂载前维护')
    android_growth_boot.capability(adapter.adb)
    android_growth_boot.identity_helper({'device':device},adapter)
    return proof


def prepare(adapter, device, authorize_reset=None):
    if device.get('readiness')!='ready':
        raise RecoveryRequired('Rooted Android must be ready / 请先完成 Android 初始化、调试和 root 授权')
    info,summary=inspect_space(adapter,device)
    available=summary['recoverable_bytes']
    if not available:
        raise RecoveryRequired('No reclaimable space directly after userdata / 没有紧邻 userdata 尾部、可直接回收的未分配空间；不会移动或删除其他分区')
    total=device['storage']['DISK_SIZE'];current=(info['first'],info['last'])
    data=partition(info,'userdata')
    if (data['start']!=device['storage']['START'] or
            (data['end']-data['start']+1)*512!=device['userdata_bytes']):
        raise RecoveryRequired('Kernel userdata boundary differs from GPT; reboot Android / 内核 userdata 边界与 GPT 不一致，请先重启 Android')
    target=device['userdata_bytes']+available
    after,new_data=grow_layout(current,total,target)
    for name in ('misc','boot_a','boot_b'):
        partition(info,name)
    # Do not overwrite someone else's pending recovery or Android OTA request.
    control=adapter.adb.read('dd if=/dev/block/by-name/misc bs=4096 count=1 2>/dev/null')
    if len(control)!=4096:raise RecoveryRequired('Incomplete misc read / misc 读取不完整')
    uninstall_bcb(control[:2048])
    from .boot_image import trial_control
    trial_control(control[2048:2080],device['slot'].removeprefix('_'))
    source=device['storage']['SOURCE']
    if not re.fullmatch(r'/dev/(?:block/)?(?:dm-\d+|mmcblk\d+p\d+|sd[a-z]+\d+)',source):
        raise RecoveryRequired('Android /data source is not a supported block mapping / Android /data 不是可确认的块设备映射')
    mapped=int(adapter.adb.shell('blockdev --getsize64 '+source,root=True).strip())
    if not 0<mapped<=device['userdata_bytes']:
        raise RecoveryRequired('Android /data mapping exceeds userdata / Android /data 映射超出 userdata')
    mapping_overhead=device['userdata_bytes']-mapped
    reason='';volume={};tools={};action=''
    try:
        volume=android_volume(adapter.adb,device,allow_small=True)
        if not re.fullmatch('[a-f0-9]{32}',volume.get('uuid','')) or volume['uuid']=='0'*32:
            raise ValueError('Android filesystem identity is unavailable / 无法确认 Android 文件系统身份')
        if volume['filesystem']=='f2fs':
            volume=android_f2fs_proof(adapter,device,volume)
            action='grow-android-f2fs'
        elif volume['filesystem']=='ext4':
            # resize2fs delegates mounted growth to ext4's own online ioctl.
            # It runs only after Android reboots with the enlarged mapping.
            if volume['mapped_bytes']!=device['userdata_bytes'] or not volume.get('uuid'):
                raise ValueError('Ext4 mapping does not cover userdata / ext4 映射未完整覆盖 userdata')
            tools=growth_tools(adapter,'ext4')
            action='grow-online-ext4'
        else:
            raise ValueError('Unsupported Android filesystem / Android 文件系统不受支持')
    except (ConnectionError,TimeoutError,subprocess.TimeoutExpired):
        raise
    except (ValueError,OSError,KeyError,RecoveryRequired) as error:
        reason=str(error)
        evidence={'cid':device['cid'],'slot':device['slot'],'layout':[hashlib.sha256(x).hexdigest() for x in current],
                  'volume':volume,'encryption':[device.get(k) for k in ('encrypted','encryption_state','encryption_type')],'reason':reason}
        token=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
        if not authorize_reset or not authorize_reset(token):
            raise PreservationUnavailable(reason,token) from error
        action='reset';tools={}
    plan={'method':'expand','target':device['storage']['NODE'],'wipe':action=='reset','two_stage':action=='reset',
          'offline':False,'data_action':action,'preservation_error':reason,'openwrt_bytes':0,'grow':False,
          'previous_android_bytes':device['userdata_bytes'],'android_bytes':target,'reclaimed_bytes':available,
          'space':summary}
    meta={'version':1,'action':action,'total':total,'front':info['front'],'tail':info['tail'],
          'volume':volume,'tool_hashes':{n:hashlib.sha256(v).hexdigest() for n,v in tools.items()},
          'mapping_overhead':mapping_overhead,'bcb_sha256':hashlib.sha256(control[:2048]).hexdigest(),
          'reset_authorized':action=='reset','userdata_start':new_data['start']}
    files=dict(zip(('before-first','before-last','after-first','after-last'),(*current,*after)))
    return plan,meta,files


def persist(context, metadata, files):
    folder=context.directory/'expand';folder.mkdir(parents=True,exist_ok=True)
    for name,content in files.items():
        (folder/name).write_bytes(content)
    metadata['checksums']={name:hashlib.sha256(content).hexdigest() for name,content in files.items()}
    context.record['expand']=metadata
    context.store.save(context.record)


class FormatRequired(Exception):
    """Expansion paused for a separately reviewed reset decision."""


class Expander:
    def __init__(self, service, context, adapter):
        self.service,self.ctx,self.adapter=service,context,adapter
        self.r=context.record;self.meta=self.r['expand'];self.folder=context.directory/'expand'
        self.files={}
        if (self.meta.get('version')!=1 or self.r['choices'].get('method')!='expand' or self.r['plan'].get('method')!='expand' or
                self.meta.get('action') not in ('reset','grow-android-f2fs','grow-online-ext4') or
                type(self.r['plan'].get('wipe')) is not bool or type(self.meta.get('reset_authorized')) is not bool):
            raise RecoveryRequired('Invalid expansion record / 扩容记录无效')
        for name in ('before-first','before-last','after-first','after-last'):
            path=self.folder/name
            if not path.is_file() or not 0<path.stat().st_size<=2<<20 or sha(path)!=self.meta['checksums'].get(name):
                raise RecoveryRequired('Expansion GPT backup is missing or damaged / 扩容 GPT 备份缺失或损坏')
            self.files[name]=path.read_bytes()
        calculated,data=grow_layout((self.files['before-first'],self.files['before-last']),self.meta['total'],self.r['plan']['android_bytes'])
        if tuple(calculated)!=(self.files['after-first'],self.files['after-last']) or data['start']!=self.meta['userdata_start']:
            raise RecoveryRequired('Expansion record changes more than userdata / 扩容记录包含 userdata 以外的分区更改')
        geometry=gpt.gpt_space(self.files['before-first'],self.files['before-last'],self.meta['total'])
        original=partition(geometry,'userdata')
        old_bytes=(original['end']-original['start']+1)*512
        if (self.meta['front']!=geometry['front'] or self.meta['tail']!=geometry['tail'] or
                self.r['plan']['previous_android_bytes']!=old_bytes or
                self.r['plan']['target']!=self.r['device']['storage']['NODE'] or
                self.r['plan']['reclaimed_bytes']!=self.r['plan']['android_bytes']-old_bytes or
                type(self.meta['mapping_overhead']) is not int or not 0<=self.meta['mapping_overhead']<old_bytes or
                not re.fullmatch('[a-f0-9]{64}',self.meta.get('bcb_sha256',''))):
            raise RecoveryRequired('Expansion record geometry differs from its GPT / 扩容记录容量或备份范围与 GPT 不符')
        if (self.meta['reset_authorized']!=self.r['plan']['wipe'] or self.meta['reset_authorized']!=(self.meta['action']=='reset') or
                self.meta['action']!=self.r['plan']['data_action']):
            raise RecoveryRequired('Expansion data choice differs from its reviewed plan / 扩容数据处理方式与确认计划不一致')

    def ready(self):
        device=self.adapter.probe(self.r['device']['serial'],'expand',gpt_scan=False)
        if device.get('readiness')=='ready':
            if (device['cid']!=self.r['device']['cid'] or device['slot']!=self.r['device']['slot'] or
                    device['storage']['NODE']!=self.r['device']['storage']['NODE'] or
                    device['storage']['DISK_SIZE']!=self.meta['total'] or
                    device['storage']['START']!=self.meta['userdata_start']):
                raise RecoveryRequired('Expansion device or userdata identity changed / 扩容设备、Android 槽位或 userdata 身份发生变化')
        return device

    def current(self):
        disk='/dev/block/'+storage.disk_node(self.r['device']['storage']['NODE'])
        adb=self.adapter.adb;m=self.meta
        return (adb.read(f'dd if={disk} bs=512 count={m["front"]} 2>/dev/null'),
                adb.read(f'dd if={disk} bs=512 skip={m["tail"]} count={m["total"]-m["tail"]} 2>/dev/null'))

    def layout_state(self):
        current=self.current()
        before=(self.files['before-first'],self.files['before-last']);after=(self.files['after-first'],self.files['after-last'])
        if current==before:return 'before'
        if current==after:return 'after'
        if current[0] in (before[0],after[0]) and current[1] in (before[1],after[1]):return 'partial'
        raise RecoveryRequired('GPT changed outside this expansion; no further writes / GPT 出现本次扩容以外的变化，已停止写入')

    def _remote(self):
        if not re.fullmatch('[a-f0-9-]{1,80}',self.r['id']):raise ValueError('Invalid expansion ID')
        return '/data/local/tmp/openwrt-expand-'+self.r['id']

    def backup(self):
        folder=self.ctx.directory/'backup';folder.mkdir(exist_ok=True)
        def save():
            adb=self.adapter.adb
            try:adb.backup_partition('misc',folder/'misc.bin',self.ctx)
            finally:adb.cleanup_backup(self.ctx)
            if hashlib.sha256((folder/'misc.bin').read_bytes()[:2048]).hexdigest()!=self.meta['bcb_sha256']:
                raise RecoveryRequired('Recovery request changed since review / 确认后恢复或升级请求发生变化，请重新检查')
            (folder/'gpt-first.bin').write_bytes(self.files['before-first'])
            (folder/'gpt-last.bin').write_bytes(self.files['before-last'])
            atomic_json(folder/'identity.json',self.r['device'])
            atomic_json(folder/'checksums.json',{p.name:sha(p) for p in folder.iterdir() if p.is_file() and p.name!='checksums.json'})
        self.ctx.effect('backup-expand',save,retry_safe=True)
        sums=json.loads((folder/'checksums.json').read_text())
        for name in ('misc.bin','gpt-first.bin','gpt-last.bin','identity.json'):
            if not (folder/name).is_file() or sha(folder/name)!=sums.get(name):
                raise RecoveryRequired('Expansion backup failed verification / 扩容备份校验失败')

    def commit_gpt(self):
        adb=self.adapter.adb;remote=self._remote();m=self.meta
        # A disconnected controller does not prove its device worker exited.
        # Do not overwrite staging or start another writer until it is gone.
        active=adb.shell('for p in /proc/[0-9]*/cmdline; do '
            '[ -r "$p" ] || continue; s=$(tr "\\000" "\\n" < "$p" 2>/dev/null | sed -n "2p"); '
            '[ "$s" != '+shlex.quote(remote+'/commit.sh')+' ] || echo BUSY; done; true',root=True).strip()
        if 'BUSY' in active.splitlines():
            raise RecoveryRequired('Expansion worker is still active; reconnect to check again / 扩容写入仍在执行，请稍后重新检查')
        state=self.layout_state()
        if state=='after':
            self.r['effects']['expand-gpt']={'state':'done','time':time.time(),'result':'GPT verified by readback'}
            self.r['expand_committed']=True;self.r['unsafe']=False;self.ctx.store.save(self.r);return
        self.ctx.stage('expand_prepare')
        self.backup()
        adb.shell('umask 077; mkdir -p '+remote)
        for name in self.files:
            adb.call('push',str(self.folder/name),remote+'/'+name,timeout=30)
        disk='/dev/block/'+storage.disk_node(self.r['device']['storage']['NODE'])
        checks='\n'.join(f'{self.meta["checksums"][name]}  {name}' for name in self.files)+'\n'
        (self.folder/'SHA256SUMS').write_text(checks,encoding='ascii')
        adb.call('push',str(self.folder/'SHA256SUMS'),remote+'/SHA256SUMS',timeout=30)
        from .storage import library
        (self.folder/'storage.sh').write_text(library(self.adapter.root,self.adapter.kind),encoding='utf-8')
        adb.call('push',str(self.folder/'storage.sh'),remote+'/storage.sh',timeout=30)
        script=f'''#!/system/bin/sh
set -eu
set -o pipefail
cd {remote}
sha256sum -c SHA256SUMS
. ./storage.sh
[ "$(getprop ro.boot.slot_suffix)" = "{self.r['device']['slot']}" ]
[ "$(openwrt_storage_identity {storage.disk_node(self.r['device']['storage']['NODE'])})" = "{self.r['device']['cid']}" ]
[ "$(cat /sys/class/block/{storage.disk_node(self.r['device']['storage']['NODE'])}/size)" = {m['total']} ]
snapshot() {{
 dd if={disk} of=check-first bs=512 count={m['front']} 2>/dev/null
 dd if={disk} of=check-last bs=512 skip={m['tail']} count={m['total']-m['tail']} 2>/dev/null
}}
snapshot
{{ cmp -s check-first before-first || cmp -s check-first after-first; }}
{{ cmp -s check-last before-last || cmp -s check-last after-last; }}
# userdata only grows. The mounted Android kernel keeps its old partition
# boundary until reboot; never request BLKRRPART or resize a mounted F2FS here.
dd if=after-last of={disk} bs=512 seek={m['tail']} conv=notrunc,fsync
dd if={disk} of=check-last bs=512 skip={m['tail']} count={m['total']-m['tail']} 2>/dev/null
cmp -s check-last after-last
dd if=after-first of={disk} bs=512 conv=notrunc,fsync
snapshot
cmp -s check-first after-first
cmp -s check-last after-last
echo EXPAND-GPT-VERIFIED
'''
        worker=self.folder/'commit.sh';worker.write_text(script,encoding='utf8',newline='\n')
        adb.call('push',str(worker),remote+'/commit.sh',timeout=30)
        def commit():
            output=adb.shell('sh '+remote+'/commit.sh',root=True,timeout=120)
            if 'EXPAND-GPT-VERIFIED' not in output.splitlines() or self.layout_state()!='after':
                raise RecoveryRequired('Expansion GPT write is not verified / 扩容 GPT 写入尚未校验完成')
            self.r['expand_committed']=True;self.ctx.store.save(self.r)
        # The worker accepts only the two exact before/after copies; an
        # interrupted GPT transaction is completed forward, not reconstructed.
        self.ctx.effect('expand-gpt',commit,retry_safe=True)

    def reboot(self,device):
        if not self.r.get('expand_reset_armed'):
            adb=self.adapter.adb
            bcb=adb.read('dd if=/dev/block/by-name/misc bs=2048 count=1 2>/dev/null')
            normal=uninstall_bcb(bcb)
            if normal!=bcb:
                remote=self._remote();adb.shell('umask 077; mkdir -p '+remote)
                path=self.folder/'normal-bcb';path.write_bytes(normal)
                adb.call('push',str(path),remote+'/normal-bcb',timeout=30)
                wanted=hashlib.sha256(normal).hexdigest();before=hashlib.sha256(bcb).hexdigest()
                script=('set -eu; set -o pipefail; '
                        '[ "$(dd if=/dev/block/by-name/misc bs=2048 count=1 2>/dev/null | sha256sum | cut -d" " -f1)" = '+before+' ]; '
                        '[ "$(sha256sum '+remote+'/normal-bcb | cut -d" " -f1)" = '+wanted+' ]; '
                        'dd if='+remote+'/normal-bcb of=/dev/block/by-name/misc bs=2048 count=1 conv=notrunc,fsync')
                self.ctx.effect('expand-clear-bootonce',lambda:adb.shell(script,root=True,timeout=30),retry_safe=True)
        self.r['expand_wait_boot_id']=device['boot_id'];self.ctx.store.save(self.r)
        self.ctx.stage('expand_offline','waiting_device',reboot_manually=False)
        try:self.adapter.adb.call('reboot',timeout=30)
        except (ConnectionError,OSError,TimeoutError,subprocess.TimeoutExpired):
            self.ctx.log('Expected ADB loss during expansion restart',scope='expand')

    def reset(self,device):
        if not self.meta['reset_authorized'] or not self.r['plan']['wipe']:
            raise RecoveryRequired('Android reset has not been explicitly confirmed / 尚未明确确认清空 Android 数据')
        adb=self.adapter.adb
        original=(self.ctx.directory/'backup/misc.bin').read_bytes()[:2048]
        normalized=uninstall_bcb(original)
        request=bytearray(2048);request[:13]=b'boot-recovery'
        message=b'recovery\n--wipe_data\n--reason=openwrt-userdata-expand\n'
        request[64:64+len(message)]=message
        current=adb.read('dd if=/dev/block/by-name/misc bs=2048 count=1 2>/dev/null')
        if current not in (original,normalized,bytes(request)):
            raise RecoveryRequired('Another recovery/update request is pending / 设备已有其他恢复或升级请求，不会覆盖')
        remote=self._remote();adb.shell('umask 077; mkdir -p '+remote)
        path=self.folder/'reset-bcb';path.write_bytes(request)
        adb.call('push',str(path),remote+'/reset-bcb',timeout=30)
        wanted=hashlib.sha256(request).hexdigest();before=hashlib.sha256(current).hexdigest()
        script=('set -eu; set -o pipefail; [ "$(getprop ro.boot.slot_suffix)" = '+self.r['device']['slot']+' ]; '
                '[ "$(sha256sum '+remote+'/reset-bcb | cut -d" " -f1)" = '+wanted+' ]; '
                '[ "$(dd if=/dev/block/by-name/misc bs=2048 count=1 2>/dev/null | sha256sum | cut -d" " -f1)" = '+before+' ]; '
                'dd if='+remote+'/reset-bcb of=/dev/block/by-name/misc bs=2048 count=1 conv=notrunc,fsync; '
                '[ "$(dd if=/dev/block/by-name/misc bs=2048 count=1 2>/dev/null | sha256sum | cut -d" " -f1)" = '+wanted+' ]')
        self.ctx.effect('expand-reset',lambda:adb.shell(script,root=True,timeout=30),retry_safe=True)
        self.r['expand_reset_armed']=True;self.ctx.store.save(self.r)
        self.reboot(device)

    def offer_reset(self,reason):
        # An uncertain Android boot write needs restoration, never a reset.
        if self.r.get('unsafe'):
            raise RecoveryRequired('Restore the interrupted Android maintenance boot before changing data handling / 请先恢复中断的 Android 维护启动，再选择数据处理方式')
        self.r['expand_reset_offer']={'state':'awaiting','nonce':__import__('secrets').token_hex(16),'reason':str(reason)}
        self.ctx.stage('expand_format','waiting_user')
        raise FormatRequired()

    def grow_ext4(self,device,volume):
        if volume['filesystem']!='ext4' or volume['uuid']!=self.meta['volume']['uuid']:
            raise RecoveryRequired('Android ext4 identity changed / Android ext4 文件系统身份发生变化')
        tools=growth_tools(self.adapter,'ext4')
        if {n:hashlib.sha256(v).hexdigest() for n,v in tools.items()}!=self.meta['tool_hashes']:
            raise RecoveryRequired('Ext4 tools differ from the reviewed package / ext4 工具与确认的安装包不一致')
        remote=self._remote()+'/tools';adb=self.adapter.adb
        for name,content in tools.items():
            relative=name.removeprefix('openwrt-uninstall/tools/')
            target=remote+'/'+relative
            adb.shell('umask 077; mkdir -p '+shlex.quote(str(Path(target).parent).replace('\\','/')))
            local=self.folder/('tool-'+hashlib.sha256(name.encode()).hexdigest()[:16]);local.write_bytes(content)
            adb.call('push',str(local),target,timeout=60)
            if adb.shell('sha256sum '+target,root=True).split()[0]!=hashlib.sha256(content).hexdigest():
                raise RecoveryRequired('Ext4 tool upload failed verification / ext4 工具上传校验失败')
        source=volume['source'];size=volume['mapped_bytes']
        if not re.fullmatch(r'/dev/(?:block/)?(?:dm-\d+|mmcblk\d+p\d+|sd[a-z]+\d+)',source):
            raise RecoveryRequired('Android ext4 source is invalid')
        command=('set -eu; [ "$(blockdev --getsize64 '+source+')" = '+str(size)+' ]; '
                 'chmod 755 '+remote+'/lib/ld-musl-aarch64.so.1 '+remote+'/usr/sbin/resize2fs; '+
                 remote+'/lib/ld-musl-aarch64.so.1 --library-path '+remote+'/lib:'+remote+'/usr/lib '+remote+'/usr/sbin/resize2fs '+source)
        output=self.ctx.effect('expand-ext4',lambda:adb.shell(
            'set +e; ('+command+'); rc=$?; echo __EXPAND_EXT4_STATUS=$rc; exit 0',root=True,timeout=180),retry_safe=True)
        status=re.findall(r'(?m)^__EXPAND_EXT4_STATUS=(\d+)\s*$',output)
        if len(status)!=1:raise RecoveryRequired('Ext4 resize receipt is missing / ext4 扩容缺少执行回执')
        self.ctx.log(output,scope='expand-ext4')
        return None if status[0]=='0' else output[-2500:]

    def finish(self,device,volume):
        expected=self.r['plan']['android_bytes']
        if (device['userdata_bytes']!=expected or volume['mapped_bytes']!=expected-self.meta['mapping_overhead'] or
                volume['filesystem_bytes']>volume['mapped_bytes'] or volume['mapped_bytes']-volume['filesystem_bytes']>4<<20):
            raise RecoveryRequired('Android filesystem did not reach its new allocation / Android 文件系统尚未达到新的分配容量')
        if not self.r['plan']['wipe'] and volume['uuid']!=self.meta['volume']['uuid']:
            raise RecoveryRequired('Preserved Android data identity changed / 保留的 Android 数据身份发生变化')
        reset_verified=bool(self.meta['volume'].get('uuid') and self.meta['volume']['uuid']!=volume['uuid']) if self.r['plan']['wipe'] else False
        if self.r['plan']['wipe'] and not reset_verified:
            self.ctx.log('Capacity verified; recovery was requested but data reset cannot be established from the old filesystem identity / 容量校验通过，已请求原厂清空，但无法依据原文件系统身份确认清空结果',level='warning',scope='expand')
        bcb=self.adapter.adb.read('dd if=/dev/block/by-name/misc bs=2048 count=1 2>/dev/null')
        uninstall_bcb(bcb)
        if bcb[:32].split(b'\0',1)[0] not in (b'',b'bootonce-bootloader'):
            raise RecoveryRequired('Stock recovery still has a pending request / 原厂 Recovery 请求仍未完成')
        if self.layout_state()!='after':raise RecoveryRequired('Expansion layout changed during final checks')
        try:
            android_growth_boot.cleanup(self,operation='expand')
            remote=self._remote()
            names=['before-first','before-last','after-first','after-last','SHA256SUMS','storage.sh','commit.sh','check-first','check-last','reset-bcb','normal-bcb']
            tool_names=[]
            for name in self.meta['tool_hashes']:
                relative=name.removeprefix('openwrt-uninstall/tools/')
                if (name==relative or not re.fullmatch(r'[A-Za-z0-9_./+-]+',relative) or '..' in relative.split('/')):
                    raise RecoveryRequired('Unsafe temporary-tool path / 临时工具路径无效')
                tool_names.append('tools/'+relative)
            directories={remote}
            for name in tool_names:
                parts=name.split('/')
                directories.update(remote+'/'+('/'.join(parts[:i])) for i in range(1,len(parts)))
            self.adapter.adb.shell('rm -f '+' '.join(shlex.quote(remote+'/'+n) for n in names+tool_names)+'; rmdir '+
                ' '.join(shlex.quote(d) for d in sorted(directories,key=len,reverse=True))+' 2>/dev/null || true',root=True)
            for path in self.folder.glob('tool-*'):
                if re.fullmatch(r'tool-[a-f0-9]{16}',path.name) and path.is_file():path.unlink()
        except (ConnectionError,OSError,TimeoutError) as error:
            self.ctx.log('Expansion verified; owned temporary-file cleanup can be retried / 扩容已校验，临时文件清理可重试: '+str(error),level='warning',scope='cleanup')
        # Only the fixed names owned by this operation are removed. Backups
        # remain in its exportable/deletable installation record.
        self.r['result']={'expanded':True,'filesystem_bytes':volume['filesystem_bytes'],'android_bytes':expected,
                          'reclaimed_bytes':self.r['plan']['reclaimed_bytes'],'backup_directory':str(self.ctx.directory/'backup'),
                          'url':'','temporary':False,'data_preserved':not self.r['plan']['wipe']}
        self.r['result']['reset_verified']=reset_verified
        self.ctx.stage('complete','completed')

    def run(self):
        device=self.ready()
        if device.get('readiness')!='ready':
            return self.wait()
        state=self.layout_state()
        if state!='after':
            if self.meta['action']!='reset':
                volume=android_volume(self.adapter.adb,device,allow_small=True)
                if any(volume.get(k)!=self.meta['volume'].get(k) for k in ('uuid','source','filesystem','mapped_bytes')):
                    raise RecoveryRequired('Android data identity changed since the plan / 确认后 Android 数据身份或映射发生变化')
            self.commit_gpt()
            if self.meta['action']=='reset':self.reset(device)
            else:self.reboot(device)
            return self.wait()
        if self.r['effects'].get('expand-gpt',{}).get('state')!='done':
            self.commit_gpt()
        if self.meta['action']=='reset' and not self.r.get('expand_reset_armed'):
            self.backup();self.reset(device);return self.wait()
        if device.get('boot_id')==self.r.get('expand_wait_boot_id',self.r['device'].get('boot_id')):
            self.reboot(device);return self.wait()
        if device['userdata_bytes']!=self.r['plan']['android_bytes']:
            raise RecoveryRequired('Android returned with a different userdata boundary / Android 重启后 userdata 边界仍与扩容计划不符')
        if self.r.get('expand_android_growth_attempted') and not self.r.get('expand_android_growth_restored'):
            if android_growth_boot.restore(self,device,operation='expand'):
                self.reboot(device);return self.wait()
            # Restoration was verified in an ordinary Android boot. A later
            # approved reset may legitimately require reinstalling Magisk;
            # never treat that new Android image as our still-pending hook.
            self.r['expand_android_growth_restored']=True;self.ctx.store.save(self.r)
        self.ctx.stage('verify_android')
        volume=android_volume(self.adapter.adb,device,allow_small=True)
        if not self.r['plan']['wipe'] and volume['uuid']!=self.meta['volume']['uuid']:
            raise RecoveryRequired('Android filesystem identity changed / Android 文件系统身份发生变化')
        if volume['mapped_bytes']-volume['filesystem_bytes']>4<<20:
            if self.meta['action']=='grow-android-f2fs':
                if self.r.get('expand_android_growth_attempted'):
                    self.offer_reset('Android pre-mount F2FS growth failed / Android 挂载前 F2FS 扩容未完成。'+android_growth_boot.report(self,operation='expand')[-2000:])
                try:
                    volume=android_f2fs_proof(self.adapter,device,volume)
                    android_growth_boot.stage(self,device,volume,operation='expand')
                except (ValueError,RecoveryRequired) as error:
                    if self.r.get('expand_android_growth_attempted'):
                        raise
                    self.offer_reset(str(error))
                return self.wait()
            if self.meta['action']=='grow-online-ext4':
                error=self.grow_ext4(device,volume)
                volume=android_volume(self.adapter.adb,device,allow_small=True)
                if volume['mapped_bytes']-volume['filesystem_bytes']>4<<20:
                    self.offer_reset(error or 'Online ext4 growth did not reach the target / 在线 ext4 扩容未达到目标容量')
        return self.finish(device,volume)

    def wait(self):
        while True:
            self.ctx.boundary()
            try:
                device=self.ready()
            except (ConnectionError,OSError,TimeoutError,subprocess.TimeoutExpired):
                device={'readiness':'android_boot'}
            # Only a disconnected probe is a waiting condition. Filesystem,
            # backup or boot-restoration errors in run() must reach the user.
            if device.get('readiness')=='ready' and device.get('boot_id')!=self.r.get('expand_wait_boot_id',self.r['device'].get('boot_id')):
                return self.run()
            self.ctx.stage(device.get('readiness','android_boot') if device.get('readiness')!='ready' else 'expand_offline',
                           'waiting_device' if device.get('readiness') in ('android_boot','ready') else 'waiting_user',reboot_manually=False)
            if self.service is None or self.service.stop.wait(3):raise Cancelled()


def confirm_reset(service, record, params):
    """Only prepare a new reviewed plan; this command never writes a device."""
    import secrets
    offer=record.get('expand_reset_offer',{})
    if (record['choices'].get('method')!='expand' or offer.get('state')!='awaiting' or
            params.get('confirmation')!='yes' or not secrets.compare_digest(str(params.get('nonce','')),str(offer.get('nonce','')))):
        raise ValueError('Type yes to prepare the reset expansion plan / 输入 yes 后才能准备清空数据扩容计划')
    context=Context(service.store,record,service.emit)
    adapter=service.adapter(record['package'],context)
    worker=Expander(service,context,adapter)
    if record.get('expand_android_growth_attempted') and not record.get('expand_android_growth_restored'):
        raise RecoveryRequired('Restore Android maintenance boot before preparing a reset / 请先完成 Android 维护启动恢复，再准备清空数据方案')
    device=worker.ready()
    if device.get('readiness')!='ready' or worker.layout_state()!='after':
        raise RecoveryRequired('Reconnect the same Android and verify the committed GPT first / 请先连接同一 Android 设备并确认已提交的 GPT')
    record['plan'].update(wipe=True,data_action='reset',preservation_error=offer['reason'],two_stage=True)
    record['expand'].update(action='reset',reset_authorized=True)
    offer.update(state='confirmed',confirmed_at=time.time())
    context.stage('plan','ready')
    return service.public(record)
