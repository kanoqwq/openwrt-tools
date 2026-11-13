"""Source-owned build assets; no neighbouring firmware checkout is required."""
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE / 'runtime/luci-app-unisoc-ota'
ASSETS = {
    **{name: PLUGIN / 'root/usr/lib/unisoc-ota' / name for name in ('engine.py','boot.py','rpc.py','proxy.sh','uploads.py','builder.py','baseline.py')},
    'unisoc-ota.js': PLUGIN / 'htdocs/luci-static/resources/view/system/unisoc-ota.js',
    'errors.js': PLUGIN / 'htdocs/luci-static/resources/unisoc-ota/errors.js',
    'unisoc-ota.json': PLUGIN / 'root/usr/share/rpcd/acl.d/unisoc-ota.json',
    'storage-e5.sh': HERE / 'runtime/storage-e5.sh',
    'storage-mu300.sh': HERE / 'runtime/storage-mu300.sh',
    'release-index.py': HERE / 'release-index.py',
    'lz4raw-aarch64.so': HERE / 'ota-native/lz4raw-aarch64.so',
    'build.json': HERE / 'ota-native/build.json',
}
ASSETS.update({'generator/'+p.name:p for p in (PLUGIN/'root/usr/lib/unisoc-ota/generator').iterdir()
               if p.is_file() and p.suffix in ('.py','.zip','.json','.md')})


def asset(name):
    path = ASSETS[name]
    if not path.is_file():
        raise ValueError('Missing source-owned installer asset: ' + name)
    return path
