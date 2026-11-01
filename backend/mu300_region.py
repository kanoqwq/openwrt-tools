"""Upstream MU300's ext4-in-a-GPT-gap layout, never a guessed partition alias.

Reference: dikeckaan/mu300-linux main tools/storage.sh and boot/init,
2026-11-01. All offsets below are in 512-byte sysfs sectors, including UFS.
"""
import struct
from . import gpt, storage
from .state import RecoveryRequired

LAYOUT = 'mu300-upstream-region-v1'
LEGACY_OFFSET = 27762098176 // 512


def geometry(info, start, size):
    """Prove a bounded raw extent behind userdata and outside every GPT entry."""
    data = [p for p in info['parts'] if p['name'] == 'userdata']
    if (len(data) != 1 or type(start) is not int or type(size) is not int or
            start % 4096 or size < 4194304 or size % 8 or
            any(p['name'] in ('mu300root', 'e5root') for p in info['parts'])):
        raise RecoveryRequired('Unsupported upstream MU300 region / 不支持的上游 MU300 系统区域')
    end = max(p['end']+1 for p in info['parts'])
    factor = info['sector_size']//512
    usable = (struct.unpack_from('<Q', info['first'], info['sector_size']+48)[0]+1)*factor
    if (data[0]['end']+1 != end or start not in ((end//4096+1)*4096, LEGACY_OFFSET) or
            not end <= start < start+size <= usable):
        raise RecoveryRequired('MU300 region overlaps partitions or GPT / MU300 系统区域与分区或 GPT 重叠')
    return {'n': 0, 'name': 'mu300root', 'start': start, 'end': start+size-1, 'layout': LAYOUT}


def headers(adapter, device, info):
    """Only read the upstream starts within validated gaps; return exact labels."""
    if adapter.kind != 'mu300':
        return []
    disk = storage.disk_node(device['storage']['NODE'])
    end = max(p['end']+1 for p in info['parts'])
    found = []
    for start in sorted({(end//4096+1)*4096, LEGACY_OFFSET}):
        if not any(a <= start and start+4 <= a+n for a,n in info['gaps']):
            continue
        raw = adapter.adb.read(f'dd if=/dev/block/{disk} bs=512 skip={start} count=4 2>/dev/null')
        if len(raw) != 2048:
            raise RecoveryRequired('Incomplete MU300 region read / MU300 系统区域读取不完整')
        sb = raw[1024:]
        if sb[56:58] != b'\x53\xef' or sb[120:136].rstrip(b'\0') != b'mu300root':
            continue
        shift = struct.unpack_from('<I',sb,24)[0]
        blocks = struct.unpack_from('<I',sb,4)[0]
        if struct.unpack_from('<I',sb,96)[0] & 0x80:
            blocks |= struct.unpack_from('<I',sb,336)[0] << 32
        if shift > 2:
            raise RecoveryRequired('Unsupported MU300 filesystem block size / 不支持的 MU300 文件系统块大小')
        found.append((start,blocks*(1024 << shift)//512))
    return found


def discover(adapter, device, info):
    if adapter.kind != 'mu300' or device['storage'].get('ROOT'):
        return None
    found = [geometry(info,start,size) for start,size in headers(adapter,device,info)]
    if len(found) > 1:
        raise RecoveryRequired('Multiple upstream MU300 roots found / 检测到多个上游 MU300 系统，无法自动选择')
    return found[0] if found else None


def layout(current, total, start, size):
    from .uninstall import table, with_table
    info = gpt.gpt_space(*current,total)
    info['first'] = current[0]
    root = geometry(info,start,size)
    # The reviewed plan returns only up to the proven filesystem's end.
    # Unrelated space beyond it is not silently consumed.
    data = next(p for p in info['parts'] if p['name'] == 'userdata')
    target = {**data, 'end': root['end']}
    updated = bytearray(table(current[0]))
    factor = info['sector_size']//512
    struct.pack_into('<Q',updated,(data['n']-1)*128+40,(target['end']+1)//factor-1)
    return with_table(current,updated,total), target, root, True


def marker_paths(proof):
    os = proof.get('upstream_os')
    if os not in ('openwrt','openwrt-luci'):
        raise RecoveryRequired('Unsupported upstream system / 不支持的上游系统身份')
    return {'.mu300/boot-os',os+'/etc/openwrt_release'}


def idle_script(disk):
    # A loop of the whole disk is upstream's normal root. Do not inspect or
    # detach someone else's loop automatically, even when it is not mounted.
    return ('set -e; for f in /sys/class/block/loop*/loop/backing_file; do '
            '[ -r "$f" ] || continue; b=$(cat "$f"); '
            'case "$b" in /dev/'+disk+'|/dev/block/'+disk+') echo BUSY; exit 0;; esac; done; '
            '[ -z "$(ls -A /sys/class/block/'+disk+'/holders)" ]; echo IDLE')


def journal_script(disk, start, capacity):
    """Replay only a previously identified idle ext4, through a bounded loop."""
    offset = start*512
    return ('set -eu; [ "$('+idle_script(disk)+')" = IDLE ]; '
            '[ -x /system/bin/e2fsck ]; loop=$(losetup -f); '
            'case "$loop" in /dev/block/loop[0-9]*|/dev/loop[0-9]*) ;; *) exit 1;; esac; '
            'losetup -o '+str(offset)+' -S '+str(capacity)+' "$loop" /dev/block/'+disk+'; '
            'trap \'losetup -d "$loop"\' EXIT; n=${loop##*/}; '
            '[ "$(cat /sys/class/block/$n/loop/offset)" = '+str(offset)+' ]; '
            '[ "$(cat /sys/class/block/$n/size)" = '+str(capacity//512)+' ]; '
            'case "$(cat /sys/class/block/$n/loop/backing_file)" in /dev/'+disk+'|/dev/block/'+disk+') ;; *) exit 1;; esac; '
            'rc=0; /system/bin/e2fsck -p -E journal_only "$loop" </dev/null || rc=$?; '
            '[ "$rc" = 0 ] || [ "$rc" = 1 ]; sync')
