"""Assets in the explicitly selected live or isolated OTA runtime layout."""
import os
from pathlib import Path


def resource(name):
    base = Path(__file__).resolve().parent.parent
    if os.environ.get('UNISOC_OTA_RAM'):
        path = base/name
    else:
        web = {'unisoc-ota.js':'www/luci-static/resources/view/system/unisoc-ota.js',
               'errors.js':'www/luci-static/resources/unisoc-ota/errors.js',
               'unisoc-ota.json':'usr/share/rpcd/acl.d/unisoc-ota.json'}
        root = base.parents[2]
        source_layout = root.name=='root' and (root.parent/'htdocs').is_dir()
        if source_layout and name in ('lz4raw-aarch64.so','build.json'):
            path=base.parents[5]/'ota-native'/name
        elif source_layout and name in web and web[name].startswith('www/'):
            path=root.parent/'htdocs'/web[name][4:]
        else:
            path = root/web[name] if name in web else base/name
    if not path.is_file():
        raise ValueError('Missing OTA generator asset: '+name)
    return path
