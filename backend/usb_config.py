"""Prepare only the selected wired Android transport before a confirmed task."""
import re
import shlex
import subprocess
import time


class UsbPreparationError(RuntimeError):
    """Stop before backup/writes when the selected ADB device cannot be verified."""


def functions(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise UsbPreparationError('Cannot read the current USB composition / 无法读取当前 USB 组合，请重新连接设备后重试')
    parts = [part.strip() for part in value.strip().split(',')]
    if any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', part) for part in parts):
        raise UsbPreparationError('Invalid USB composition; no change was made / USB 组合格式异常，未执行更改')
    return parts


def without_mtp(value):
    current = functions(value)
    if not any(part.lower() == 'mtp' for part in current):
        return None
    target = [part for part in current if part.lower() != 'mtp']
    if not any(part.lower() == 'adb' for part in target):
        target.append('adb')
    if any(part.lower() == 'none' for part in target):
        raise UsbPreparationError('Invalid USB composition; no change was made / USB 组合格式异常，未执行更改')
    return ','.join(target)


def prepare(adb, boot_id, *, notify=None, boundary=None, timeout=120):
    # Kept here rather than in devices(): discovery, authorization, planning and
    # wireless ADB must never mutate the target's USB functions.
    from .transport import wireless_serial
    if not adb.serial or wireless_serial(adb.serial):
        return {'changed': False}
    if adb.su is None:
        raise UsbPreparationError('Verify root before preparing USB / 请先完成 root 检查再准备 USB 连接')
    check = boundary or (lambda: None)
    check()
    original = adb.shell('getprop sys.usb.config').strip()
    target = without_mtp(original)
    if target is None:
        return {'changed': False, 'config': original}
    if not boot_id:
        raise UsbPreparationError('Cannot verify device identity before USB reconnection / 无法核实设备身份，未更改 USB 组合')
    def notice(code):
        if notify:
            notify({'code': code})
    notice('usb_preparing')
    started = time.monotonic()
    deadline = started + timeout
    stable = 0
    last_error = None
    try:
        check()
        try:
            # Exactly one write. A USB disconnect can precede the shell's exit
            # status. Reconnection is authoritative; vendor USB properties may
            # retain MTP or lag behind the actual working transport.
            adb.shell('setprop sys.usb.config '+shlex.quote(target), root=True, timeout=min(10, timeout))
        except (ConnectionError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
            last_error = error
        driver = getattr(adb, 'driver', None)
        if driver is not None:
            driver.reset_connection()
        while time.monotonic() < deadline:
            check()
            try:
                devices = adb.devices()
                selected = next((d for d in devices if d['serial'] == adb.serial), None)
                if not selected or selected['state'] != 'device':
                    stable = 0
                else:
                    budget = max(.1, min(5, deadline-time.monotonic()))
                    state = adb.shell("printf 'CONFIG=%s\\nSTATE=%s\\nBOOT=%s\\n' \"$(getprop sys.usb.config)\" \"$(getprop sys.usb.state)\" \"$(cat /proc/sys/kernel/random/boot_id)\"", timeout=budget)
                    fields = dict(line.split('=', 1) for line in state.splitlines() if '=' in line)
                    if fields.get('BOOT') and fields['BOOT'] != boot_id:
                        raise UsbPreparationError('Device rebooted or changed during USB reconnection; inspect again / USB 重连期间设备已重启或发生变化，请重新检查')
                    ready = fields.get('BOOT') == boot_id
                    stable = stable+1 if ready else 0
                    if stable >= 2:
                        if adb.shell('id -u', root=True, timeout=budget).strip() != '0':
                            raise UsbPreparationError('Root is unavailable after USB reconnection / USB 重连后 root 不可用，请重新授权')
                        notice('usb_ready')
                        return {'changed': True, 'config': fields.get('CONFIG', ''), 'requested_config': target}
            except (ConnectionError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
                last_error = error
                stable = 0
            time.sleep(min(.5, max(0, deadline-time.monotonic())))
        raise UsbPreparationError('USB reconnection timed out; reconnect the cable and allow USB/ADB access, then retry / USB 重连超时，请重新插线、允许 USB／ADB 授权后重试') from last_error
    except BaseException:
        notice('usb_prepare_failed')
        raise
