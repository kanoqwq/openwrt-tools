"""Scoped, user-invoked repair launchers for extracted macOS distributions."""
from pathlib import Path


APPS = {
    'OpenWrt-Installer.app': 'OpenWrt-Installer-macOS.command',
    'OpenWrt-Diff-Studio.app': 'OpenWrt-Diff-Studio-macOS.command',
    'OpenWrt-Updater.app': 'OpenWrt-Updater-macOS.command',
}

TEMPLATE = r'''#!/bin/sh
# Run only for an extracted OpenWrt package from a source you trust.
set -eu
fail() { printf '\n%s\n' "$*" >&2; exit 1; }
[ "$(/usr/bin/uname -s)" = Darwin ] || fail 'This launcher requires macOS / 此入口仅用于 macOS'
ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)
APP="$ROOT/@APP@"
TOOLS="$ROOT/tools/macos"
[ -d "$APP" ] && [ ! -L "$APP" ] &&
    [ -d "$TOOLS" ] && [ ! -L "$TOOLS" ] &&
    [ -f "$TOOLS/backend/openwrt-backend" ] ||
    fail 'Extract the entire package first / 请先完整解压安装包，不要单独移动 APP 或此入口'
# Release assembly flattens framework links. Never repair a redirected tree.
[ -z "$(/usr/bin/find "$APP" "$TOOLS" -type l -print)" ] ||
    fail 'Unexpected symbolic link; extract the original package again / 目录含异常软链接，请重新解压原包'
ENTRY=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist") ||
    fail 'Invalid application metadata / APP 信息损坏，请重新解压'
case "$ENTRY" in ''|.|..|*/*) fail 'Invalid application entry / APP 启动入口异常';; esac
[ -f "$APP/Contents/MacOS/$ENTRY" ] || fail 'Application entry is missing / 缺少 APP 启动文件'
printf 'Repairing this package only / 仅修复当前包：\n%s\n' "$ROOT"
/bin/chmod -R u+rwX "$APP" "$TOOLS" ||
    fail 'Extract into a writable folder and retry / 请解压到自己的可写目录后重试'
/bin/chmod u+x "$APP/Contents/MacOS/$ENTRY" "$TOOLS/backend/openwrt-backend"
if [ -f "$TOOLS/adb" ]; then /bin/chmod u+x "$TOOLS/adb"; fi
# Delete this one attribute only, including nested libraries. Probe first so
# rerunning a repaired package succeeds without ignoring a real deletion error.
/usr/bin/find "$APP" "$TOOLS" -exec /bin/sh -ec '
    for path do
        if /usr/bin/xattr -p com.apple.quarantine "$path" >/dev/null 2>&1; then
            /usr/bin/xattr -d com.apple.quarantine "$path"
        fi
    done
' sh {} +
/usr/bin/open "$APP"
printf 'Opened / 已打开：%s\n' '@APP@'
'''


def content(app):
    if app not in APPS:
        raise ValueError('Unknown macOS entry')
    return TEMPLATE.replace('@APP@', app).encode('utf-8')


def attach(root, app):
    target = Path(root) / APPS[app]
    target.write_bytes(content(app))
    target.chmod(0o755)
    return target.name
