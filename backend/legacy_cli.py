"""Bind the compatibility CLI to the same family resources as the GUI host."""
from pathlib import Path
from . import storage
from .model_compat import confirm_mu300_cli
from .package import e5_profile, hotspot_name


def boot_once(root, args):
    """The compatibility menu uses the same reviewed boot plan as every GUI."""
    import io
    import os
    import sys
    from contextlib import ExitStack
    from .service import Service
    from .state import Context, Store, DeviceLock
    from .transport import Adb
    from .ssh_runtime import load_ssh
    root = Path(root).resolve()
    if sys.platform == 'win32':
        directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'OpenWrt-Installer'
    elif sys.platform == 'darwin':
        directory = Path.home()/'Library/Application Support/OpenWrt-Installer'
    else:
        directory = Path.home()/'.local/share/OpenWrt-Installer'
    load_ssh()
    service = Service(root, Store(directory), io.StringIO())
    def dispatch(command, params):
        return service.dispatch({'protocol':1,'command':command,'params':params})
    dispatch('package', {'path':str(root)})
    serial = getattr(args, 'serial', None)
    if not serial:
        devices = [d for d in Adb(root).devices() if d['state'] == 'device']
        if len(devices) == 1:
            serial = devices[0]['serial']
        elif len(devices) > 1:
            for i, device in enumerate(devices, 1):
                print(f"{i}. {device.get('label','Android')} · ••••{device['serial'][-4:]}")
            selected = input('Device number / 设备编号: ').strip()
            if not selected.isdigit() or not 1 <= int(selected) <= len(devices):
                raise ValueError('Invalid device selection / 设备选择无效')
            serial = devices[int(selected)-1]['serial']
        else:
            raise ConnectionError('No Android device / 未发现 Android 设备')
    params = {'serial':serial,'choices':{'method':'boot','repair_boot':getattr(args,'repair_boot',False)}}
    value = dispatch('plan', params)
    if value.get('confirmation_required') == 'model_mismatch':
        print(value['warning'])
        if input('Use this package? Type yes / 仍使用此安装包？输入 yes: ').strip() != 'yes':
            return None
        params.update(model_confirmation='yes',model_token=value['model_token'])
        value = dispatch('plan', params)
    record = service.store.load(value['id'])
    info = record['plan']['boot_check']
    if info['required']:
        print('Boot repair needed / 需要先修复启动:',info['reason'])
        print('Target / 目标:',info['target'],'Kernel / 内核:',info['root']['kernel_release'])
        print('Back up boot, rebuild and verify, then boot once. Keep the installed system, plugins, settings and data.\n'
              '备份并修复 boot，校验后临时启动；保留系统、插件、配置和数据，不重装或重新分区。')
        if input('Repair and boot? Type yes / 修复并启动？输入 yes: ').strip() != 'yes':
            return None
    def emit(kind, data):
        if kind == 'log':
            print(data['message'], flush=True)
    context = Context(service.store, record, emit)
    adapter = service.adapter(record['package'], context)
    try:
        with ExitStack() as locks:
            for identity in sorted({record['device']['serial'],record['device']['cid']}):
                locks.enter_context(DeviceLock(service.store.base,identity))
            return adapter.execute()
    except BaseException:
        context.stage(record['stage'],'failed')
        raise


def bind(module, root):
    root=Path(root).resolve()
    module.GUI=None
    if (root/'desktop/manifest.json').is_file():
        module.package_root=lambda:root
        module._storage=storage
        module.android_profile=confirm_mu300_cli
        original = module.install
        module.install = lambda args, *a, **kw: boot_once(root,args) if getattr(args,'boot_openwrt',False) else original(args,*a,**kw)
        return 'mu300'
    module.B,module.F,module.SCRIPTS=str(root),str(root/'files'),str(root/'scripts')
    module.package_profile=lambda:e5_profile(root)
    module.storage_api=lambda:vars(storage)
    module.default_hotspot_name=lambda:hotspot_name(e5_profile(root)['profile'])
    module.boot_openwrt=lambda args:boot_once(root,args)
    return 'e5'
