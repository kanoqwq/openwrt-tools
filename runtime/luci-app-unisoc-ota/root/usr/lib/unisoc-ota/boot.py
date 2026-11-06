"""Boot-v4 differential OTA support, including dynamic Android/Linux slot roles.

Only Linux-slot startup containers are written. Preserve command lines, slot bindings and boot
diagnostic area. A recovery hook runs before switch_root after an interrupted
root transaction. No external LZ4 package is required on the device.
"""
import hashlib
import os
from pathlib import Path
import stat
import struct
import zlib

MAGIC = bytes.fromhex('02214c18')
HOOK = '''
# unisoc-ota-recovery-v2
if [ -s /newroot/etc/unisoc-ota/journal.json ] && grep -Eq '"status":"(applying|rolling_back)"' /newroot/etc/unisoc-ota/journal.json; then
    touch /run/stay
    kill ${persist_job:-} ${timer_job:-} $(jobs -p) 2>/dev/null || :
    ota_ram=/run/unisoc-ota-recovery
    mkdir -p "$ota_ram"
    mount -t tmpfs -o mode=700 tmpfs "$ota_ram" || exec /bin/sh
    cp -a /newroot/etc/unisoc-ota/runtime/. "$ota_ram/" || exec /bin/sh
    mkdir -p "$ota_ram/target" "$ota_ram/dev" "$ota_ram/proc"
    mount --bind /newroot "$ota_ram/target" || exec /bin/sh
    mount --bind /dev "$ota_ram/target/dev" || exec /bin/sh
    mount --bind /dev "$ota_ram/dev" || exec /bin/sh
    mount -t proc proc "$ota_ram/proc" || exec /bin/sh
    chroot "$ota_ram" /usr/bin/python3 /ota/engine.py recover --root /target || exec /bin/sh
    sync
    reboot -f
    exec /bin/sh
fi
'''


_native_decode = None


def block_decode(data):
    global _native_decode
    if _native_decode is None:
        helper = Path(__file__).with_name('lz4raw-aarch64.so')
        # This bundled ELF is for Linux AArch64 devices, not a host DLL/dylib.
        # Windows can show a Bad Image dialog before ctypes raises OSError;
        # choose the compatible decoder before calling the operating-system loader.
        native_host = (hasattr(os, 'uname') and os.uname().sysname == 'Linux' and
                       os.uname().machine.lower() in ('aarch64', 'arm64'))
        if native_host and helper.is_file():
            try:
                import ctypes
            except ImportError:
                # E5's deliberately small Python distribution omits ctypes.
                # Valid boot data still decodes using the standard library;
                # the native decoder is an acceleration, not a dependency.
                _native_decode = block_decode_python
                return _native_decode(data)
            try:
                library = ctypes.CDLL(str(helper))
            except OSError:
                _native_decode = block_decode_python
                return _native_decode(data)
            fn = library.unisoc_lz4_decode
            fn.argtypes = (ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
            fn.restype = ctypes.c_int
            def decode(block):
                buffer = ctypes.create_string_buffer(8 << 20)
                size = fn(block, len(block), buffer, 8 << 20)
                if size < 0: raise ValueError('Invalid LZ4 block')
                return ctypes.string_at(buffer, size)
            _native_decode = decode
        else:
            try:
                import lz4.block
                _native_decode = lambda block: lz4.block.decompress(block, uncompressed_size=8 << 20)
            except ImportError:
                _native_decode = block_decode_python
    return _native_decode(data)


def block_decode_python(data):
    out = bytearray(); at = 0
    while at < len(data):
        token = data[at]; at += 1
        length = token >> 4
        if length == 15:
            while True:
                extra = data[at]; at += 1; length += extra
                if extra != 255: break
        if at + length > len(data): raise ValueError('Truncated LZ4 literals')
        out.extend(data[at:at+length]); at += length
        if at == len(data): break
        offset = int.from_bytes(data[at:at+2], 'little'); at += 2
        if not offset or offset > len(out): raise ValueError('Invalid LZ4 offset')
        length = (token & 15) + 4
        if token & 15 == 15:
            while True:
                extra = data[at]; at += 1; length += extra
                if extra != 255: break
        if len(out) + length > 8 << 20: raise ValueError('Oversized LZ4 block')
        start = len(out)-offset
        pattern = bytes(out[start:start+min(offset, length)])
        out.extend((pattern*((length+len(pattern)-1)//len(pattern)))[:length])
    return bytes(out)


def archives(ram):
    at = 0
    while at < len(ram):
        if ram[at:at+4] != MAGIC: raise ValueError('Unsupported boot ramdisk compression')
        at += 4; out = bytearray()
        while at < len(ram) and ram[at:at+4] != MAGIC:
            if at + 4 > len(ram): raise ValueError('Truncated LZ4 header')
            size = struct.unpack_from('<I', ram, at)[0]; at += 4
            if not size: break
            if size > 9 << 20 or at + size > len(ram): raise ValueError('Invalid LZ4 block')
            out.extend(block_decode(bytes(ram[at:at+size]))); at += size
            if len(out) > 192 << 20: raise ValueError('Oversized boot ramdisk')
        yield bytes(out)


def entries(data):
    at = 0
    while at < len(data):
        if data[at:at+6] not in (b'070701', b'070702') or at+110 > len(data):
            if not data[at:].strip(b'\0'): return
            raise ValueError('Invalid boot CPIO')
        fields = [int(data[at+6+i*8:at+14+i*8], 16) for i in range(13)]
        size, namesize = fields[6], fields[11]; end = at+110+namesize
        if not namesize or end > len(data) or data[end-1] != 0: raise ValueError('Invalid CPIO name')
        name = data[at+110:end-1].decode(); at = (end+3)&~3
        if at+size > len(data): raise ValueError('Truncated CPIO file')
        if name == 'TRAILER!!!': return
        yield name.removeprefix('./'), data[at:at+size], fields[1]
        at = (at+size+3)&~3


def overlay(files):
    def record(name, data, mode, ino):
        name = name.encode()+b'\0'
        fields = [ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0]
        header = b'070701'+b''.join(f'{n:08x}'.encode() for n in fields)+name
        return header+b'\0'*(-len(header)%4)+data+b'\0'*(-len(data)%4)
    directories = set()
    for name in files:
        parts = name.split('/')
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    data = b''; ino = 1
    for name in sorted(directories, key=lambda n:(n.count('/'), n)):
        data += record(name, b'', stat.S_IFDIR|0o755, ino); ino += 1
    for name, value in files.items():
        content, mode = value
        data += record(name, content, mode, ino); ino += 1
    data += record('TRAILER!!!', b'', 0, ino)
    compressed = bytearray(MAGIC)
    for at in range(0, len(data), 8 << 20):
        chunk = data[at:at+(8 << 20)]; length = len(chunk)
        block = bytearray([min(length, 15) << 4])
        if length >= 15:
            extra = length-15
            block.extend(b'\xff'*(extra//255)); block.append(extra%255)
        block.extend(chunk)
        compressed.extend(struct.pack('<I', len(block))+block)
    return bytes(compressed)


def _layout(header, footer, length, family):
    if (family not in ('e5', 'mu300') or length != 64 << 20 or header[:8] != b'ANDROID!' or
            len(header) < 4096 or len(footer) != 64 or struct.unpack_from('<I', header, 40)[0] != 4 or footer[:4] != b'AVBf'):
        raise ValueError('Invalid boot v4 image')
    kernel, size = struct.unpack_from('<II', header, 8)
    start = 4096+((kernel+4095)&~4095)
    original, avb, avb_size = struct.unpack_from('>QQQ', footer, 12)
    limit = (56 if family == 'e5' else 48) << 20
    if not kernel or not size or start+size > avb or avb+avb_size > limit or original != avb:
        raise ValueError('Invalid boot payload bounds')
    return start, size, avb, avb_size, limit


def layout(image, family):
    return _layout(image[:4096], image[-64:], len(image), family)


def check_file(path, family):
    with Path(path).open('rb') as stream:
        stream.seek(0, os.SEEK_END); length = stream.tell()
        stream.seek(0); header = stream.read(4096)
        stream.seek(-64, os.SEEK_END); footer = stream.read(64)
    return _layout(header, footer, length, family)


def _frames(ram):
    at = 0
    while at < len(ram):
        if ram[at:at+4] != MAGIC: raise ValueError('Unsupported boot ramdisk compression')
        at += 4; blocks = []
        while at < len(ram) and ram[at:at+4] != MAGIC:
            if at+4 > len(ram): raise ValueError('Truncated LZ4 header')
            size = struct.unpack_from('<I', ram, at)[0]; at += 4
            if not size: break
            if size > 9 << 20 or at+size > len(ram): raise ValueError('Invalid LZ4 block')
            blocks.append((at, size)); at += size
        yield blocks


class _BlockStream:
    def __init__(self, blocks):
        self.blocks = iter(blocks); self.buffer = memoryview(b''); self.at = 0; self.position = 0

    def take(self, length, keep=True, optional=False):
        remaining = length; result = []
        while remaining:
            if self.at == len(self.buffer):
                try: self.buffer = memoryview(next(self.blocks)); self.at = 0
                except StopIteration:
                    if optional and remaining == length: return None
                    raise ValueError('Truncated CPIO stream')
            count = min(remaining, len(self.buffer)-self.at)
            if keep: result.append(bytes(self.buffer[self.at:self.at+count]))
            self.at += count; self.position += count; remaining -= count
        return b''.join(result) if keep else None


def metadata(image, family, wanted):
    start, size, *_ = layout(image, family)
    ram = memoryview(image)[start:start+size]; result = {}
    try:
        for blocks in _frames(ram):
            stream = _BlockStream(block_decode(bytes(ram[at:at+size])) for at,size in blocks)
            while True:
                header = stream.take(110, optional=True)
                if header is None or not header.strip(b'\0'): break
                if header[:6] not in (b'070701', b'070702'): raise ValueError('Invalid boot CPIO')
                fields = [int(header[6+i*8:14+i*8], 16) for i in range(13)]
                length, namesize = fields[6], fields[11]
                if not 0 < namesize <= 4096: raise ValueError('Invalid CPIO filename')
                raw = stream.take(namesize)
                if raw[-1] != 0: raise ValueError('Invalid CPIO filename')
                name = raw[:-1].decode().removeprefix('./')
                stream.take((-stream.position)%4, keep=False)
                if name == 'TRAILER!!!': break
                if wanted(name): result[name] = (stream.take(length), fields[1])
                else: stream.take(length, keep=False)
                stream.take((-stream.position)%4, keep=False)
    finally:
        ram.release()
    return result


MU300_STORAGE = ('etc/mu300-root-target', 'etc/mu300-desktop-root-mode',
                 'etc/mu300-desktop-identity')

# The Pro canonical image deliberately has no installation-specific storage
# adapter. Its installed init sources these files before mounting OpenWrt.
# Bind the working adapter and its offline filesystem tools, never an old
# kernel/module tree or a complete previous ramdisk.
NATIVE_STORAGE = ('etc/mu300-desktop-storage.sh', 'etc/mu300-internal-ext4.sh',
                  'etc/part-progress.sh', 'etc/part-prepare.sh',
                  'etc/install-part.sh', 'etc/ext4-geometry.sh',
                  'usr/share/mu300-gpt')


def _native_storage(name):
    return name in NATIVE_STORAGE or name.startswith('repair/')


def _role(name):
    return name in MU300_STORAGE or name.startswith('etc/misc-bc-') or name in (
        'etc/e5/android-slot', 'etc/e5/linux-slot', 'etc/e5/modem-slot',
        'etc/mu300/android-slot', 'etc/mu300/linux-slot', 'etc/linux-slot',
        'etc/android-slot', 'etc/mu300-android-slot', 'lib/firmware/android-firmware-slot')


class Binder:
    """Bind device metadata/storage tools, skipping kernels and module payloads."""
    def __init__(self, current, family):
        self.current = current; self.family = family
        self.fields = metadata(current, family, lambda n: n == 'init' or _role(n) or
                               (family == 'mu300' and _native_storage(n)))
        if 'init' not in self.fields: raise ValueError('Boot init missing')
        self.init = self.fields['init'][0].decode().replace('\r\n', '\n')
        self.needs_guard = ('# unisoc-ota-recovery-v2' not in self.init or
                            'mount --bind /dev "$ota_ram/target/dev"' not in self.init)
        self.roles = {n:v for n,v in self.fields.items() if _role(n)}
        self.native = family == 'mu300' and '# U30PRO-MU300 storage bootstrap' in self.init
        if self.native:
            self.roles.update({n:v for n,v in self.fields.items() if _native_storage(n)})
            # prepare/install helpers can call the same verified tools outside
            # /repair. Preserve only aliases actually backed by that bundle.
            # Do not select all /lib or /usr: those could include stale modules.
            aliases = {n.removeprefix('repair/') for n in self.fields if n.startswith('repair/')}
            if aliases:
                self.roles.update(metadata(current, family, lambda n: n in aliases))
        if family == 'mu300':
            # A release boot is TF-only; the installed device may use an
            # internal partition/region instead. Preserve absence too: an old
            # generic boot must not inherit an incoming TF-only restriction.
            for name in MU300_STORAGE:
                self.roles.setdefault(name, (b'', stat.S_IFREG | 0o644))

    def parts(self, image, original=False):
        start, size, avb, avb_size, limit = layout(image, self.family)
        if original:
            if not self.needs_guard: return None
            init = self.init
        else:
            values = metadata(image, self.family, lambda n:n == 'init')
            if 'init' not in values: raise ValueError('Boot init missing')
            init = values['init'][0].decode().replace('\r\n', '\n')
        if self.native and '. /etc/mu300-desktop-storage.sh' in init:
            # Check before the OTA journal permits any boot/root writes. An
            # image hash/readback alone cannot prove its init can mount root.
            required = ('etc/mu300-desktop-storage.sh', 'etc/mu300-internal-ext4.sh')
            if any(n not in self.roles for n in required):
                raise ValueError('Installed native storage adapter is incomplete; no boot was written / '
                                 '已安装的原生存储启动脚本不完整，未写入启动镜像')
        if '# unisoc-ota-recovery-v2' in init:
            old = '    mount --bind /newroot "$ota_ram/target" || exec /bin/sh'
            if 'mount --bind /dev "$ota_ram/target/dev"' not in init:
                if init.count(old) != 1: raise ValueError('Cannot migrate existing boot recovery hook')
                init = init.replace(old, old+'\n    mount --bind /dev "$ota_ram/target/dev" || exec /bin/sh')
        else:
            anchor = ("log 'stage=switch-root platform=u30pro'" if
                      '# U30PRO-MU300 storage bootstrap' in init else
                      '    log "stage=switch-root root=')
            if init.count(anchor) != 1: raise ValueError('Cannot locate boot recovery hook')
            init = init.replace(anchor, HOOK+'\n'+anchor)
        # The original guard already contains its storage tools. Appending
        # another complete copy would waste RAM and boot payload capacity.
        roles = {n:v for n,v in self.roles.items() if not original or _role(n)}
        roles['init'] = (init.encode(), stat.S_IFREG|0o755)
        extra = overlay(roles)
        header = bytearray(image[:4096]); struct.pack_into('<I', header, 12, size+len(extra))
        header[44:1580] = self.current[44:1580]
        new_avb = (start+size+len(extra)+4095)&~4095
        if new_avb+avb_size > limit: raise ValueError('OTA hook overlaps boot diagnostics')
        footer = bytearray(image[-64:]); struct.pack_into('>QQQ', footer, 12, new_avb, new_avb, avb_size)
        return start+size, extra, header, footer, new_avb, bytes(image[avb:avb+avb_size])

    def prepare_file(self, path, original=False):
        import mmap
        # The staged raw image is rebound in place. It is never the device's
        # boot partition. Only small metadata/overlay bytes are held in RAM.
        with Path(path).open('r+b') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
            parts = self.parts(image, original)
            if parts is None: return False
            end, extra, header, footer, avb, avb_data = parts
            stream.seek(end); stream.write(extra); stream.write(b'\0'*(avb-end-len(extra)))
            stream.write(avb_data)
            stream.seek(0); stream.write(header)
            stream.seek(len(image)-64); stream.write(footer)
            stream.flush(); os.fsync(stream.fileno())
        return True


def bind(image, current, family):
    """Convenience byte-buffer API for desktop/tests; device uses prepare_file."""
    binder = Binder(current, family)
    parts = binder.parts(image, image is current or image == current)
    if parts is None: return current
    end, extra, header, footer, avb, avb_data = parts
    result = bytearray(image)
    result[:4096] = header; result[end:end+len(extra)] = extra
    result[end+len(extra):avb] = b'\0'*(avb-end-len(extra))
    result[avb:avb+len(avb_data)] = avb_data
    limit = (56 if family == 'e5' else 48) << 20
    result[limit:-64] = current[limit:-64]; result[-64:] = footer
    return result


def linux_slot(root, family):
    root = Path(root); slots = set()
    for name in (f'etc/{family}/linux-slot', f'run/{family}/linux-slot', 'run/e5linux/linux-slot'):
        path = root/name
        if path.is_file():
            value = path.read_text().strip()
            if value in ('a', 'b'): slots.add(value)
    controls = ('run/e5linux/misc-bc-slot-b-trial.bin', 'etc/e5linux/misc-bc-slot-b-trial.bin') if family == 'e5' else (
        'run/mu300/misc-bc-linux-trial.bin', 'run/mu300/misc-bc-slot-b-trial.bin')
    for name in controls:
        path = root/name
        if path.is_file():
            with path.open('rb') as f: value = f.read(2)
            if value in (b'_a', b'_b'): slots.add(value[1:].decode())
    if len(slots) != 1: raise ValueError('Cannot unambiguously identify Linux boot slot')
    slot = slots.pop()
    for name in (f'etc/{family}/android-slot', f'run/{family}/android-slot'):
        path = root/name
        if path.is_file() and path.read_text().strip() == slot:
            raise ValueError('Android and Linux boot slot declarations conflict')
    return slot


def partition_device(root, name):
    # Compare canonical paths on both sides (relative roots, aliases and Windows 8.3 paths).
    root=Path(root).resolve()
    def device(path):
        resolved = path.resolve()
        if resolved.is_relative_to(root): return resolved
        if resolved.is_relative_to('/dev'):
            mapped = root/'dev'/resolved.relative_to('/dev')
            if mapped.exists(): return mapped
        raise ValueError('Boot device alias escapes /dev')
    for prefix in ('dev/block/by-name', 'dev/disk/by-partlabel'):
        path = root/prefix/name
        if path.exists(): return device(path)
    # OpenWrt may only expose by-name links under /dev/block/platform/...
    for path in (root/'dev').glob('block/platform/**/by-name/'+name):
        if path.exists(): return device(path)
    for path in (root/'sys/class/block').glob('*/uevent'):
        if ('PARTNAME='+name) in path.read_text().splitlines():
            candidate = root/'dev'/path.parent.name
            if candidate.exists(): return candidate
    raise ValueError('Partition is unavailable: '+name)


def target(root,family):
    return partition_device(root,'boot_'+linux_slot(root,family))


def arm_next_linux(root,family):
    """One verified OpenWrt trial for OTA reboot; never change default policy.

    Caller holds the same /run lock used by both families' system switchers.
    No root filesystem mounting or external board helper is required.
    """
    root=Path(root);slot=linux_slot(root,family)
    controls=('run/e5linux/misc-bc-slot-b-trial.bin','etc/e5linux/misc-bc-slot-b-trial.bin') if family=='e5' else (
        'run/mu300/misc-bc-linux-trial.bin','run/mu300/misc-bc-slot-b-trial.bin')
    wanted=next((p.read_bytes() for n in controls if (p:=root/n).is_file()),None)
    def validate(raw):
        if (not raw or len(raw)!=32 or raw[4:8]!=b'BCAB' or raw[8]!=1 or (raw[9]&7)!=2 or
                raw[:2] not in (b'_a',b'_b') or
                struct.unpack_from('<I',raw,28)[0]!=zlib.crc32(raw[:28])):
            raise ValueError('Boot control validation failed; reboot cancelled / 启动控制校验失败，已取消重启')
    validate(wanted)
    status=wanted[12+(slot=='b')*2]
    other=wanted[12+(slot=='a')*2]
    if wanted[:2]!=('_'+slot).encode() or status&15<=other&15 or not(status&128 or (status>>4)&7>=2):
        raise ValueError('OpenWrt trial slot is not bootable / OpenWrt 试启动槽位无效')
    misc=partition_device(root,'misc')
    with misc.open('r+b',buffering=0) as stream:
        stream.seek(2048);before=stream.read(32);validate(before)
        if before==wanted:return {'slot':slot,'changed':False}
        try:
            stream.seek(2048)
            if stream.write(wanted)!=32:raise OSError('Short boot-control write')
            os.fsync(stream.fileno());stream.seek(2048)
            if stream.read(32)!=wanted:raise OSError('Boot-control readback differs')
        except BaseException:
            stream.seek(2048)
            if stream.write(before)!=32:raise OSError('Boot-control restoration write incomplete')
            os.fsync(stream.fileno());stream.seek(2048)
            if stream.read(32)!=before:raise OSError('Boot-control restoration readback differs')
            raise
    return {'slot':slot,'changed':True}


def write(path, image, family):
    *_, limit = layout(image, family)
    view = memoryview(image)
    try:
        with Path(path).open('r+b', buffering=0) as stream:
            for at in range(0, limit, 1 << 20):
                chunk = view[at:min(at+(1 << 20),limit)]
                while chunk:
                    count = stream.write(chunk)
                    if not count: raise OSError('Short boot write')
                    chunk = chunk[count:]
                del chunk
            stream.seek(len(image)-64); stream.write(view[-64:]); os.fsync(stream.fileno())
            stream.seek(0)
            for at in range(0, limit, 1 << 20):
                if stream.read(min(1 << 20, limit-at)) != view[at:min(at+(1 << 20), limit)]:
                    raise ValueError('Boot readback mismatch')
            stream.seek(len(image)-64)
            if stream.read(64) != view[-64:]: raise ValueError('Boot footer readback mismatch')
    finally:
        if 'chunk' in locals(): chunk.release()
        view.release()


def write_file(device, image_path, family):
    import mmap
    with Path(image_path).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
        write(device, image, family)


def signature(path, family):
    *_, limit = check_file(path, family)
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        remaining = limit
        while remaining:
            block = stream.read(min(1 << 20, remaining))
            if not block: raise ValueError('Truncated boot partition')
            result.update(block); remaining -= len(block)
        stream.seek(-64, os.SEEK_END); result.update(stream.read(64))
    return result.hexdigest()


# U30 Pro uses the same MU300 root layout, with native Android-v4 containers.
# Keep this codec in the shared runtime: installer, SSH updater and OTA must
# assemble exactly the same ramdisk/DTB combination on every host platform.
NATIVE_IMAGES = ('boot', 'vendor_boot', 'init_boot')


def native_bounds(data, offset, size):
    if not 0 <= offset <= len(data) or not 0 <= size <= len(data)-offset:
        raise ValueError('Native boot extent exceeds its partition / 原生启动镜像范围越界')
    return data[offset:offset+size]


def native_footer(data):
    if len(data) < 4096 or data[-64:-60] != b'AVBf':
        raise ValueError('Native boot AVB container is missing / 原生启动镜像容器缺失')
    original, offset, size = struct.unpack_from('>QQQ', data, len(data)-52)
    if not size or original > offset or offset+size > len(data)-64:
        raise ValueError('Invalid native boot footer / 原生启动镜像尾部范围无效')
    return bytes(data[offset:offset+size]), bytes(data[-64:])


def native_retain(template, payload):
    metadata, footer = native_footer(template)
    payload += bytes(-len(payload) % 4096)
    end = len(payload)
    if end+len(metadata)+64 > len(template):
        raise ValueError('Native boot payload exceeds partition / 原生启动内容超出分区容量')
    footer = bytearray(footer)
    struct.pack_into('>QQQ', footer, 12, end, end, len(metadata))
    return payload+metadata+bytes(len(template)-64-end-len(metadata))+bytes(footer)


def native_worker_active(execute, busybox, worker):
    """Check only our exact native-writer invocation with packaged applets.

    Android toybox tr can spin on a /proc cmdline whose process disappeared.
    pgrep reads process metadata itself; timeout bounds that single process.
    Explicit result markers also detect su implementations masking exit status.
    """
    import re
    import shlex
    if not re.fullmatch(r'/data/local/tmp/mu300-desktop-[a-f0-9]{12}/run\.sh',worker):
        raise ValueError('Invalid native boot worker receipt')
    previous=worker.rsplit('/',1)[0]+'/busybox'
    pattern=('^'+re.escape(previous)+' (nsenter -t 1 -m '+re.escape(previous)+' )?sh '+re.escape(worker)+'$')
    bb=shlex.quote(busybox)
    command=('rc=0; found=$('+bb+' timeout -s KILL 5 '+bb+' pgrep -f -x '+shlex.quote(pattern)+
        ' 2>/dev/null) || rc=$?; case "$rc" in '
        '0) [ -n "$found" ] && echo NATIVE_BUSY ;; '
        '1) echo NATIVE_IDLE ;; '
        '*) echo NATIVE_CHECK_FAILED; exit 1 ;; esac')
    result=execute(command).strip()
    if result == 'NATIVE_BUSY':return True
    if result == 'NATIVE_IDLE':return False
    raise ValueError('Cannot verify the native writer is idle; no write was started / 无法确认原生写入进程已停止，未启动写入')


def native_boot_parts(data):
    if (len(data) < 4096 or data[:8] != b'ANDROID!' or
            struct.unpack_from('<I', data, 40)[0] != 4 or
            struct.unpack_from('<I', data, 20)[0] != 1584):
        raise ValueError('Native Android boot v4 required / 需要原生 Android v4 启动镜像')
    native_footer(data)
    kernel, ram = struct.unpack_from('<II', data, 8)
    original = struct.unpack_from('>Q', data, len(data)-52)[0]
    if 4096+((kernel+4095)&~4095)+ram > original:
        raise ValueError('Native boot payload overlaps AVB metadata')
    return (native_bounds(data, 4096, kernel),
            native_bounds(data, 4096+((kernel+4095)&~4095), ram))


def native_vendor_parts(data):
    if len(data) < 4096 or data[:8] != b'VNDRBOOT' or struct.unpack_from('<II', data, 8) != (4, 4096):
        raise ValueError('Native vendor_boot v4 required / 需要原生 vendor_boot v4')
    native_footer(data)
    ram_size = struct.unpack_from('<I', data, 24)[0]
    dt_size = struct.unpack_from('<I', data, 2100)[0]
    table_size, count, entry_size, bc_size = struct.unpack_from('<4I', data, 2112)
    if not dt_size or (count, entry_size, table_size) != (1, 108, 108):
        raise ValueError('Unsupported vendor ramdisk table / 不支持的厂商启动片段表')
    dt = 4096+((ram_size+4095)&~4095)
    table_at = dt+((dt_size+4095)&~4095)
    original = struct.unpack_from('>Q', data, len(data)-52)[0]
    if table_at+4096+bc_size > original:
        raise ValueError('Vendor boot payload overlaps AVB metadata')
    table = native_bounds(data, table_at, table_size)
    size, offset = struct.unpack_from('<II', table)
    if size != ram_size or offset:
        raise ValueError('Invalid vendor ramdisk entry / 厂商启动片段范围不一致')
    return (native_bounds(data, 4096, ram_size), native_bounds(data, dt, dt_size),
            native_bounds(data, table_at+4096, bc_size), table)


def native_image(canonical, templates, name):
    if name not in NATIVE_IMAGES:
        raise ValueError('Unknown native boot container')
    kernel, ram = native_boot_parts(canonical)
    command = bytes(canonical[44:1580])
    if b'\0' not in command or command.split(b'\0',1)[0].split()[:1] != [b'loglevel=5'] or not kernel or not ram:
        raise ValueError('Invalid MU300 canonical boot input')
    template = templates[name]
    header = bytearray(template[:4096])
    if name == 'vendor_boot':
        _, dtb, config, table = native_vendor_parts(template)
        product = b'u30pro'
        # Legacy U30 Pro callers may pass an opaque ramdisk. Its handling stays
        # unchanged; the new F50 Pro build contract always emits LZ4+CPIO.
        for archive in archives(ram) if ram[:4] == MAGIC else []:
            for path, content, mode in entries(archive):
                if path == 'etc/mu300-model':
                    if stat.S_IFMT(mode) != stat.S_IFREG or content.strip() not in (b'u30pro', b'f50pro'):
                        raise ValueError('Invalid native product marker')
                    product = content.strip()
        # Keep the canonical boot's historical loglevel=5\0 identity intact.
        # Model-specific kernel parameters belong to the bound vendor command
        # line; strip only our owned tokens so repeated OTA cannot accumulate.
        owned = (b'u30pro_sim_mux.layout=', b'u30pro_ump965x_fgu.battery_present=',
                 b'u30pro_sgm41513.battery_present=', b'f50pro_external_power_only=')
        original_tokens = bytes(header[28:2076]).split(b'\0',1)[0].split()
        tokens = [v for v in original_tokens if not v.startswith(owned)]
        if product == b'f50pro':
            tokens += [b'u30pro_sim_mux.layout=mu3356', b'u30pro_ump965x_fgu.battery_present=0',
                       b'u30pro_sgm41513.battery_present=0', b'f50pro_external_power_only=1']
        cmdline = b' '.join(tokens)
        if len(cmdline) >= 2048:
            raise ValueError('Native vendor command line exceeds capacity')
        if product == b'f50pro' or any(v.startswith(owned) for v in original_tokens):
            header[28:2076] = (cmdline+b'\0').ljust(2048,b'\0')
        table = bytearray(table)
        struct.pack_into('<I', table, 0, len(ram))
        struct.pack_into('<I', header, 24, len(ram))
        payload = (bytes(header)+ram+bytes(-len(ram)%4096)+dtb+
                   bytes(-len(dtb)%4096)+table+bytes(-len(table)%4096)+config)
    else:
        before_kernel, _ = native_boot_parts(template)
        if name == 'init_boot':
            if before_kernel:
                raise ValueError('Unexpected kernel in init_boot / init_boot 不应包含内核')
            kernel, ram = b'', overlay({})
        else:
            header[44:1580] = command
        struct.pack_into('<II', header, 8, len(kernel), len(ram))
        struct.pack_into('<I', header, 1580, 0)
        payload = bytes(header)+kernel+bytes(-len(kernel)%4096)+ram
    return native_retain(template, payload)


def native_images(canonical, templates):
    return {name:native_image(canonical, templates, name) for name in NATIVE_IMAGES}


def native_image_files(canonical, templates, destination, names=NATIVE_IMAGES):
    """Map input containers; construct/write one output at a time, on disk."""
    import contextlib
    import mmap
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        def mapped(path):
            stream = stack.enter_context(Path(path).open('rb'))
            return stack.enter_context(mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ))
        source = mapped(canonical)
        inputs = {name:mapped(path) for name,path in templates.items()}
        outputs = {}
        for name in names:
            path = destination/(name+'.img')
            content = native_image(source, inputs, name)
            with path.open('xb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            del content
            outputs[name] = path
        return outputs


def native_runtime(root, family):
    marker = Path(root)/'etc/mu300/platform'
    return family == 'mu300' and marker.is_file() and marker.read_text().strip() == 'u30pro'


def native_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()


def native_size(path):
    with Path(path).open('rb') as stream:
        return stream.seek(0, os.SEEK_END)


def native_prepare(root, family, backup, guard, final):
    """Back up Linux companions before a durable OTA journal permits writes."""
    import shutil
    if not native_runtime(root, family): return []
    root, backup = Path(root), Path(backup)
    slot = linux_slot(root, family)
    rows, templates = [], {}
    for name in ('vendor_boot', 'init_boot'):
        device = partition_device(root, name+'_'+slot)
        original = backup/(name+'.img')
        with device.open('rb') as source, original.open('xb') as target:
            shutil.copyfileobj(source, target, 1 << 20); target.flush(); os.fsync(target.fileno())
        templates[name] = original
        rows.append(dict(name=name, device=device.relative_to(root).as_posix(),
                         before=native_hash(original), size=native_size(original), backup=original.name))
    for phase, canonical in (('guard', guard), ('final', final)):
        if canonical is None: continue
        if phase == 'final' and canonical == guard:
            for row in rows:
                row['final'],row['final_sha256']=row['guard'],row['guard_sha256']
            continue
        # All original formats are parsed before any partition write. No
        # Android-slot templates or whole rootfs go into these containers.
        outputs = native_image_files(canonical, templates, backup/('native-'+phase), names=templates)
        for row in rows:
            path = outputs[row['name']]
            if native_size(path) != row['size']: raise ValueError('Native image capacity changed')
            row[phase] = path.relative_to(backup).as_posix()
            row[phase+'_sha256'] = native_hash(path)
    return rows


def native_row_paths(root, backup, row, phase):
    import re
    name = row.get('name')
    if name not in ('vendor_boot', 'init_boot'):
        raise ValueError('Unknown native boot recovery partition')
    for key in ('before', 'guard_sha256', 'final_sha256'):
        if key in row and not re.fullmatch('[a-f0-9]{64}', str(row[key])):
            raise ValueError('Invalid native boot recovery checksum')
    if type(row.get('size')) is not int or not 4096 <= row['size'] <= 256 << 20:
        raise ValueError('Invalid native boot recovery capacity')
    value = row.get('device', '')
    if not isinstance(value, str) or not value.startswith('dev/') or '..' in Path(value).parts:
        raise ValueError('Invalid native boot recovery device')
    device = (Path(root)/value).resolve()
    if not device.is_relative_to((Path(root)/'dev').resolve()) or native_size(device) != row['size']:
        raise ValueError('Native boot recovery device/capacity changed')
    expected = name+'.img' if phase == 'backup' else 'native-'+phase+'/'+name+'.img'
    if phase == 'final' and row.get('final') == row.get('guard') and row.get('final_sha256') == row.get('guard_sha256'):
        expected = 'native-guard/'+name+'.img'
    if row.get(phase) != expected:
        raise ValueError('Invalid native boot recovery file')
    source = (Path(backup)/expected).resolve()
    if not source.is_relative_to(Path(backup).resolve()):
        raise ValueError('Native boot recovery file escapes transaction')
    return device, source


def native_scratch_bytes(root, family, has_incoming):
    if not native_runtime(root, family): return 0
    total = 0
    slot = linux_slot(root, family)
    for name in ('vendor_boot', 'init_boot'):
        device = partition_device(root, name+'_'+slot)
        size = native_size(device)
        # Block devices report st_size=0 and need not support mmap. Inspection
        # reads bounded headers/table/footer, not a whole 100 MiB partition.
        with device.open('rb') as stream:
            header=stream.read(4096);stream.seek(-64,os.SEEK_END);footer=stream.read(64)
            if len(header)!=4096 or footer[:4]!=b'AVBf' or not 4096 <= size <= 256 << 20:
                raise ValueError('Native template is incomplete: '+name)
            original,avb,length=struct.unpack_from('>QQQ',footer,12)
            if original!=avb or original<4096 or not length or avb+length>size-64:
                raise ValueError('Invalid native template metadata: '+name)
            if name == 'vendor_boot':
                ram=struct.unpack_from('<I',header,24)[0];dt=struct.unpack_from('<I',header,2100)[0]
                table,count,entry,config=struct.unpack_from('<4I',header,2112)
                offset=4096+((ram+4095)&~4095)+((dt+4095)&~4095)
                if (header[:8]!=b'VNDRBOOT' or struct.unpack_from('<II',header,8)!=(4,4096) or
                        not ram or not dt or (table,count,entry)!=(108,1,108) or offset+4096+config>original):
                    raise ValueError('Invalid vendor boot template')
                stream.seek(offset);record=stream.read(108)
                if len(record)!=108 or struct.unpack_from('<II',record)!=(ram,0):
                    raise ValueError('Invalid vendor ramdisk extent')
            elif (header[:8]!=b'ANDROID!' or struct.unpack_from('<I',header,40)[0]!=4 or
                    struct.unpack_from('<I',header,8)[0] or 4096+struct.unpack_from('<I',header,12)[0]>original):
                raise ValueError('Invalid init_boot template')
        total += size*(3 if has_incoming else 2)
    return total


def native_write_file(device, image, expected):
    """Full companion container write/readback; never used for boot diagnostics."""
    device, image = Path(device), Path(image)
    if native_hash(image) != expected or native_size(image) != native_size(device):
        raise ValueError('Native recovery image checksum/capacity mismatch')
    if native_hash(device) == expected: return
    with image.open('rb') as source, device.open('r+b', buffering=0) as output:
        for chunk in iter(lambda:source.read(1 << 20), b''):
            if output.write(chunk) != len(chunk): raise OSError('Short native boot write')
        os.fsync(output.fileno())
    if native_hash(device) != expected: raise ValueError('Native boot readback mismatch')


def native_commit(root, backup, rows, phase):
    for row in rows:
        if phase not in row: continue
        device, image = native_row_paths(root, backup, row, phase)
        allowed = [row['before'], row.get('guard_sha256'), row.get('final_sha256')]
        if native_hash(device) not in allowed:
            raise ValueError('Native boot changed during update: '+row['name'])
        native_write_file(device, image, row[phase+'_sha256'])


def native_restore(root, backup, rows):
    for row in rows:
        device, image = native_row_paths(root, backup, row, 'backup')
        native_write_file(device, image, row['before'])


def native_rollback_conflicts(root, backup, rows):
    conflicts = []
    for row in rows:
        device, image = native_row_paths(root, backup, row, 'backup')
        if native_hash(image) != row['before']:
            raise ValueError('Native boot recovery backup corrupt: '+row['name'])
        if native_hash(device) not in (row['before'], row.get('final_sha256')):
            conflicts.append(dict(path=row['device'], fields=['boot']))
    return conflicts


def native_inspect_route(read, shell, slot):
    """Bounded Android-side inspection; no filesystem mounts or writes."""
    import re
    if slot not in ('a', 'b'): raise ValueError('Native Linux slot is unknown')
    sizes, headers, payload_ends = {}, {}, {}
    def region(name, offset, length):
        first, count = offset//4096, ((offset%4096)+length+4095)//4096
        raw = read(f'dd if=/dev/block/by-name/{name}_{slot} bs=4096 skip={first} count={count} 2>/dev/null')
        if len(raw) != count*4096: raise ValueError('Incomplete native boot inspection: '+name)
        return raw[offset%4096:offset%4096+length]
    for name in NATIVE_IMAGES:
        sizes[name] = int(shell('blockdev --getsize64 /dev/block/by-name/'+name+'_'+slot).strip())
        if sizes[name] < 4096 or sizes[name] % 4096: raise ValueError('Invalid native boot capacity')
        headers[name] = region(name, 0, 4096)
        footer = region(name, sizes[name]-4096, 4096)[-64:]
        if footer[:4] != b'AVBf': raise ValueError('Native boot footer is unavailable: '+name)
        original, avb, length = struct.unpack_from('>QQQ', footer, 12)
        if original != avb or not length or original < 4096 or avb+length > sizes[name]-64:
            raise ValueError('Native boot footer is invalid: '+name)
        payload_ends[name] = original
    boot, vendor, init = (headers[n] for n in NATIVE_IMAGES)
    if boot[:8] != b'ANDROID!' or init[:8] != b'ANDROID!' or vendor[:8] != b'VNDRBOOT':
        raise ValueError('Native boot container is missing')
    if (struct.unpack_from('<II', vendor, 8) != (4, 4096) or
            any(struct.unpack_from('<I', h, 40)[0] != 4 for h in (boot,init))):
        raise ValueError('Native boot v4 headers differ')
    kernel, ram = struct.unpack_from('<II', boot, 8)
    vendor_ram = struct.unpack_from('<I', vendor, 24)[0]
    if not kernel or not ram or ram != vendor_ram or 4096+((kernel+4095)&~4095)+ram > payload_ends['boot']:
        raise ValueError('Native boot ramdisk copies differ')
    dtb = struct.unpack_from('<I', vendor, 2100)[0]
    table, count, entry, config = struct.unpack_from('<4I', vendor, 2112)
    table_at = 4096+((vendor_ram+4095)&~4095)+((dtb+4095)&~4095)
    if (not dtb or (table,count,entry) != (108,1,108) or table_at+4096+config > payload_ends['vendor_boot'] or
            struct.unpack_from('<II', region('vendor_boot',table_at,108)) != (vendor_ram,0)):
        raise ValueError('Native vendor ramdisk/DTB table is invalid')
    def region_sha(name, offset, length):
        first, count = offset//4096, (length+4095)//4096
        value = shell(f'dd if=/dev/block/by-name/{name}_{slot} bs=4096 skip={first} count={count} 2>/dev/null | head -c {length} | sha256sum').split()
        if not value or not re.fullmatch('[a-f0-9]{64}',value[0]): raise ValueError('Native ramdisk digest unavailable')
        return value[0]
    if region_sha('boot',4096+((kernel+4095)&~4095),ram) != region_sha('vendor_boot',4096,ram):
        raise ValueError('Native boot/vendor_boot ramdisks are inconsistent / 原生启动 ramdisk 不一致')
    init_kernel, init_ram = struct.unpack_from('<II', init, 8)
    if init_kernel or not 0 < init_ram <= 65536 or 4096+init_ram > payload_ends['init_boot']:
        raise ValueError('Native init_boot still contains Android startup')
    if any(list(entries(archive)) for archive in archives(region('init_boot',4096,init_ram))):
        raise ValueError('Native init_boot overrides Linux startup / init_boot 仍有覆盖 Linux 的启动内容')
    return {'slot':slot,'ramdisk_sha256':region_sha('boot',4096+((kernel+4095)&~4095),ram)}
