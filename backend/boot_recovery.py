"""Repair an installed system's boot route, as part of the existing boot action.

Planning reads ext4 metadata directly: no root mount, fsck, NV or data write.
The confirmed repair writes only the inactive boot, then uses the existing
misc-only trial selector. The installed root and its module files stay intact.
"""
import hashlib
import re
import shlex
import stat
import struct

from .boot_image import inspect_linux_boot
from .install_evidence import Ext4Evidence
from .state import RecoveryRequired, atomic_json, sha
from .uninstall_boot import archives, entries
from .uninstall_growth import base_boot


def digest_device(adb, path):
    value = adb.shell('sha256sum '+shlex.quote(path), root=True).split()
    if not value or not re.fullmatch('[a-f0-9]{64}', value[0]):
        raise ValueError('Invalid device checksum / 设备校验值无效')
    return value[0]


def boot_files(read, size, family, slot):
    from lz4.block import LZ4BlockError
    info = inspect_linux_boot(read, size, family, slot)
    start = 4096+((info['kernel_bytes']+4095)&~4095)
    count = (info['ramdisk_bytes']+4095)//4096
    raw = read(f'dd if=/dev/block/by-name/boot_{slot} bs=4096 skip={start//4096} count={count} 2>/dev/null')
    if len(raw) != count*4096:
        raise ValueError('Incomplete boot ramdisk / 启动镜像读取不完整')
    files = {}
    try:
        for archive in archives(raw[:info['ramdisk_bytes']]):
            for name, content, mode in entries(archive):
                if name == 'init' or name.startswith(('etc/mu300-', 'etc/e5/')):
                    if stat.S_IFMT(mode) == stat.S_IFREG:
                        files[name] = content
    except (LZ4BlockError, RuntimeError, ValueError) as error:
        raise ValueError('Invalid installed ramdisk / 已安装启动脚本损坏: '+str(error)) from error
    return files


def internal_proof(adapter, device, kernel_release, *, mode='part'):
    """Identify a completed installation without relying on a functioning boot."""
    adb = adapter.adb
    storage = device['storage']
    node = storage.get('ROOT', '') if mode == 'part' else (device.get('tf') or '').rsplit('/',1)[-1]
    if not re.fullmatch(r'(?:mmcblk\d+(?:p[1-9][0-9]*)?|sd[a-z]+[1-9][0-9]*)', node):
        raise RecoveryRequired('Cannot locate the installed internal root for boot repair / 无法定位可修复启动的现有内置系统；请接入原系统盘或导入原安装记录')
    if mode == 'part' and node == storage.get('DISK'):
        raise RecoveryRequired('Internal root cannot be the entire disk / 内置系统目标不能是整块磁盘')
    capacity = storage['ROOT_SIZE']*512 if mode == 'part' else int(adb.shell('blockdev --getsize64 /dev/block/'+node,root=True).strip())
    idle = adb.shell('set -e; n='+node+'; h=$(ls -A /sys/class/block/$n/holders); '
                     'd=$(cat /sys/class/block/$n/dev); '
                     'm=$(awk -v n="$d" \'$3==n {print "BUSY"}\' /proc/self/mountinfo); '
                     'if [ -z "$h$m" ]; then echo IDLE; else echo BUSY; fi', root=True).strip()
    if idle != 'IDLE':
        raise RecoveryRequired('Installed root is in use / 现有系统分区仍被挂载或映射，请先释放占用')
    def read(offset, length):
        first, end = offset//512, (offset+length+511)//512
        raw = adb.read(f'dd if=/dev/block/{node} bs=512 skip={first} count={end-first} 2>/dev/null')
        if len(raw) != (end-first)*512:
            raise ValueError('Incomplete installed-root metadata / 已安装系统元数据读取不完整')
        return raw[offset%512:offset%512+length]
    # A pending journal is not replayed by boot repair; normal Linux boot owns it.
    fs = Ext4Evidence(read, capacity, pending_journal=True)
    prefix = 'openwrt/' if adapter.kind == 'mu300' else ''
    paths = {'.mu300/boot-os': None} if prefix else {}
    if mode == 'part':
        paths.update({'openwrt/etc/mu300/desktop-mode':b'part'} if prefix else {'etc/e5/part-root':None})
    paths[prefix+'etc/openwrt_release'] = None
    markers = {}
    for path, expected in paths.items():
        value = fs.file(path).strip()
        if expected is not None and value != expected:
            raise RecoveryRequired('Installed root layout differs / 已安装系统布局不符: '+path)
        if path.endswith('openwrt_release') and not re.search(rb"(?m)^DISTRIB_ID=['\"]OpenWrt['\"]$", value):
            raise RecoveryRequired('OpenWrt identity is missing / 缺少 OpenWrt 系统标记')
        if path == '.mu300/boot-os' and value not in (b'android', b'openwrt'):
            raise RecoveryRequired('Unknown installed boot selector / 现有启动选择标记无效')
        if path.endswith('part-root'):
            disk = storage.get('DISK') or re.sub(r'(?:p)?\d+$', '', node)
            if value not in (('e5root /dev/'+disk).encode(), ('e5root /dev/block/'+disk).encode()):
                raise RecoveryRequired('Installed root belongs to another disk / 已安装系统标记指向另一块磁盘')
        markers[path] = hashlib.sha256(value).hexdigest()
    module_dir = prefix+'lib/modules/'+kernel_release
    directories = fs.directory(prefix+'lib/modules')
    if kernel_release not in directories:
        raise RecoveryRequired('Use a package matching the installed kernel modules / 请使用与现有系统内核模块版本匹配的完整包修复启动；本次未写入系统: '+kernel_release)
    available = fs.directory(module_dir)
    candidates = ['sipc-core.ko', 'sprd-cpufreq-v2-driver.ko', 'sipc_wwan.ko']
    selected = next((name for name in candidates if name in available), None)
    if selected is None:
        for folder in ('extra', 'modem', 'modules'):
            if folder not in available:
                continue
            sub = fs.directory(module_dir+'/'+folder)
            selected = next((folder+'/'+name for name in candidates if name in sub), None)
            if selected:
                break
    if not selected:
        raise RecoveryRequired('Cannot verify the installed kernel module ABI / 未找到可核对版本的现有基带或调频模块，请使用原版本安装包修复')
    module = fs.file(module_dir+'/'+selected, limit=2 << 20)
    vermagic = re.search(rb'(?:^|\0)vermagic=([^\0]+)\0', module)
    if not vermagic or vermagic[1].split()[0].decode('ascii') != kernel_release:
        raise RecoveryRequired('Installed module version differs / 已安装模块版本与修复包不符')
    return {'node':node, 'start':storage.get('ROOT_START') if mode == 'part' else None, 'capacity':capacity,
            'uuid':fs.super[104:120].hex(), 'markers':markers, 'kernel_release':kernel_release,
            'module_vermagic':vermagic[1].decode('ascii')}


def prepare(adapter, device, *, force=False):
    slot = device['slot'].removeprefix('_')
    if slot not in ('a', 'b'):
        raise ValueError('Android slot is unknown / Android 槽位未知')
    other = 'b' if slot == 'a' else 'a'
    target = '/dev/block/by-name/boot_'+other
    size = int(adapter.adb.shell('blockdev --getsize64 '+target, root=True).strip())
    if size != 64 << 20:
        raise ValueError('Unsupported boot partition size / 启动分区大小不受支持')
    before = digest_device(adapter.adb, target)
    header = adapter.adb.read(f'dd if={target} bs=512 count=8 2>/dev/null')
    command = header[44:1580].split(b'\0',1)[0].split() if header[:8] == b'ANDROID!' else []
    if b'openwrt.uninstall=1' in command or any(v.startswith(b'e5.part_prepare=') for v in command):
        raise RecoveryRequired('Offline partition work is pending; resume its record / 已有未完成的离线分区操作，请继续原记录')
    reason = None
    def read_boot(command):
        try:
            return adapter.adb.read(command)
        except ValueError as error:
            # A failed staged/pulled binary checksum is a transport fault,
            # never evidence that the boot on the device needs replacement.
            raise ConnectionError('Boot read could not be verified / 启动镜像读取校验失败: '+str(error)) from error
    try:
        files = boot_files(read_boot, size, adapter.kind, other)
    except ValueError as error:
        reason = str(error)
        files = {}
    init = files.get('init', b'')
    if b'part_prepare' in init and 'etc/mu300-desktop-prepare' in files:
        raise RecoveryRequired('Partition preparation is pending; resume the installation / 分区准备尚未完成，请继续原安装流程')
    if reason is None:
        if adapter.kind == 'mu300':
            mode = files.get('etc/mu300-desktop-root-mode', b'').strip()
            healthy = (mode in (b'part', b'install-part') and device.get('existing') and
                       b'/etc/mu300-desktop-storage.sh' in init or
                       mode in (b'legacy', b'data-image') and bool(init) or
                       files.get('etc/mu300-root-target', b'').strip() == b'auto' and b'ROOT_OFFSET=' in init or
                       (mode == b'sd' or files.get('etc/mu300-root-target', b'').strip() == b'sd') and bool(device.get('tf')))
        else:
            declared = files.get('etc/e5/linux-slot', other.encode()).strip()
            # E5's common init also supports userdata images and legacy roots;
            # their absence from the partition inventory is not boot damage.
            healthy = bool(init) and declared == other.encode()
        if not healthy:
            reason = 'Installed boot does not match the available system disk / 当前启动镜像与现有系统盘布局不匹配'
    if force:
        reason = 'Rebuild requested for a failed boot / 已选择重建启动镜像以处理启动失败'
    info = {'version':1, 'required':bool(reason), 'target':target, 'slot':other,
            'android_slot':slot, 'before_sha256':before}
    image = None
    if reason:
        base = base_boot(adapter)
        kernel_bytes = struct.unpack_from('<I',base,8)[0]
        match = re.search(rb'Linux version ([^\s\0]+)', base[4096:4096+kernel_bytes])
        if not match:
            raise ValueError('Package kernel version is unavailable / 安装包内核版本无法识别')
        release = match[1].decode('ascii')
        sd_route = files.get('etc/mu300-desktop-root-mode',b'').strip() == b'sd' or files.get('etc/mu300-root-target',b'').strip() == b'sd'
        mode = 'sd' if device.get('tf') and (sd_route or not device.get('existing')) else 'part'
        proof = internal_proof(adapter, device, release, mode=mode)
        live = adapter.adb.read('dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32 2>/dev/null')
        if adapter.kind == 'mu300':
            module = adapter.legacy.module(adapter.root)
            image = module.build(base, adapter.root/'desktop/files/boot', mode, live, slot)
        else:
            image = adapter.legacy.slot_api()['bind_boot'](base, live, slot)
        if len(image) != size or image[4096:4096+kernel_bytes] != base[4096:4096+kernel_bytes]:
            raise ValueError('Invalid rebuilt boot / 重建启动镜像校验失败')
        info.update(reason=reason, layout=mode, root=proof, sha256=hashlib.sha256(image).hexdigest())
    if digest_device(adapter.adb, target) != before:
        raise RecoveryRequired('Boot changed during inspection; check again / 检查期间启动分区发生变化，请重新检查')
    return info, image


def persist(context, info, image):
    if image is not None:
        path = context.directory/'boot-repair.img'
        path.write_bytes(image)
        if sha(path) != info['sha256']:
            raise ValueError('Boot repair staging failed')


def repair(adapter, device, info):
    ctx = adapter.ctx
    image = ctx.directory/'boot-repair.img'
    if not image.is_file() or sha(image) != info['sha256']:
        raise RecoveryRequired('Prepared boot image is missing or changed; inspect a new plan / 修复镜像缺失或变化，请重新检查启动计划')
    if internal_proof(adapter, device, info['root']['kernel_release'],mode=info['layout']) != info['root']:
        raise RecoveryRequired('Installed root changed after confirmation / 确认后系统分区身份或模块版本发生变化，请重新检查')
    destination = ctx.directory/'boot-repair-backup'
    destination.mkdir(exist_ok=True)
    before = destination/('boot_'+info['slot']+'.img')
    def backup():
        adapter.stage('boot_repair_backup')
        if not before.is_file() or sha(before) != info['before_sha256'] or not (destination/'misc.bin').is_file():
            if digest_device(adapter.adb, info['target']) != info['before_sha256']:
                raise RecoveryRequired('Boot changed before backup; inspect again / 备份前启动镜像发生变化，请重新检查')
            try:
                adapter.adb.backup_partition('boot_'+info['slot'], before, ctx)
                adapter.adb.backup_partition('misc', destination/'misc.bin', ctx)
            finally:
                adapter.adb.cleanup_backup(ctx)
        if sha(before) != info['before_sha256']:
            raise RecoveryRequired('Original boot backup failed verification / 原启动镜像备份校验失败')
        atomic_json(destination/'checksums.json', {p.name:sha(p) for p in destination.iterdir() if p.is_file() and p.name != 'checksums.json'})
    ctx.effect('backup-boot-repair', backup, retry_safe=True)
    if not before.is_file() or sha(before) != info['before_sha256']:
        raise RecoveryRequired('Original boot backup is missing or damaged / 原启动镜像备份缺失或损坏')
    remote = '/data/local/tmp/openwrt-boot-repair-'+ctx.record['id']
    if not re.fullmatch(r'/data/local/tmp/openwrt-boot-repair-[a-f0-9-]+', remote):
        raise ValueError('Invalid boot repair transaction ID')
    worker = remote+'/write.sh'
    def write():
        adapter.stage('boot_repair_write')
        # A disconnected ADB client can leave its remote shell running. Do not
        # start another dd until that exact transaction worker has exited.
        active = adapter.adb.shell('for p in /proc/[0-9]*/cmdline; do '
            's=$(tr "\\000" "\\n" < "$p" 2>/dev/null | sed -n "2p"); '
            '[ "$s" != '+shlex.quote(worker)+' ] || echo BUSY; done; true', root=True).strip()
        if 'BUSY' in active.split():
            raise RecoveryRequired('Boot repair is still running; retry after it finishes / 设备仍在执行此次启动修复，请稍后重试检查结果')
        actual = digest_device(adapter.adb, info['target'])
        if actual == info['sha256']:
            return 'BOOT-REPAIR-VERIFIED'
        previous = ctx.record['effects'].get('boot-repair-write', {})
        if actual != info['before_sha256'] and not previous.get('resume_authorized'):
            raise RecoveryRequired('Boot changed before repair / 修复前启动镜像发生变化，请重新检查')
        # adb push runs as shell, not su. Keep its private staging shell-owned;
        # root is needed only for the eventual block write.
        adapter.adb.shell('umask 077; mkdir -p '+remote)
        adapter.adb.call('push', str(image), remote+'/boot.img', timeout=120)
        if digest_device(adapter.adb, remote+'/boot.img') != info['sha256']:
            raise ValueError('Boot upload checksum failed / 启动修复镜像上传校验失败')
        script = ('#!/system/bin/sh\nset -e\n'
                  '[ "$(getprop ro.boot.slot_suffix)" = "_'+info['android_slot']+'" ]\n'
                  'test "$(sha256sum '+remote+'/boot.img | cut -d " " -f1)" = '+info['sha256']+'\n'
                  'test "$(sha256sum '+info['target']+' | cut -d " " -f1)" = '+actual+'\n'
                  'dd if='+remote+'/boot.img of='+info['target']+' bs=1048576 conv=notrunc,fsync\n'
                  'test "$(sha256sum '+info['target']+' | cut -d " " -f1)" = '+info['sha256']+'\n'
                  'echo BOOT-REPAIR-VERIFIED\n')
        local = ctx.directory/'boot-repair-write.sh'
        local.write_text(script, encoding='utf-8', newline='\n')
        adapter.adb.call('push', str(local), worker, timeout=30)
        ctx.record['boot_repair_write_attempted'] = True
        ctx.store.save(ctx.record)
        output = adapter.adb.shell('sh '+worker, root=True, timeout=180)
        if 'BOOT-REPAIR-VERIFIED' not in output.splitlines():
            raise RecoveryRequired('Boot repair did not report verified completion / 启动修复未返回校验成功，请重试检查')
        return 'BOOT-REPAIR-VERIFIED'
    previous = ctx.record['effects'].get('boot-repair-write', {})
    resumed = previous.get('state') == 'started' and ctx.record.get('boot_repair_write_attempted') is True
    # Keep the original effect evidence when retry_safe installs its new record.
    def operation():
        if resumed:
            ctx.record['effects']['boot-repair-write']['resume_authorized'] = True
        return write()
    ctx.effect('boot-repair-write', operation, retry_safe=True)
    if digest_device(adapter.adb, info['target']) != info['sha256']:
        raise RecoveryRequired('Repaired boot changed before trial selection / 修复后启动镜像发生变化，尚未切槽')
    adapter.adb.shell('rm -f '+remote+'/boot.img '+worker+'; rmdir '+remote, root=True)
