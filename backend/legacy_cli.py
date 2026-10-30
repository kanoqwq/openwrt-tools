"""Bind the compatibility CLI to the same family resources as the GUI host."""
from pathlib import Path
from . import storage
from .model_compat import confirm_mu300_cli
from .package import e5_profile, hotspot_name


def bind(module, root):
    root=Path(root).resolve()
    module.GUI=None
    if (root/'desktop/manifest.json').is_file():
        module.package_root=lambda:root
        module._storage=storage
        module.android_profile=confirm_mu300_cli
        return 'mu300'
    module.B,module.F,module.SCRIPTS=str(root),str(root/'files'),str(root/'scripts')
    module.package_profile=lambda:e5_profile(root)
    module.storage_api=lambda:vars(storage)
    module.default_hotspot_name=lambda:hotspot_name(e5_profile(root)['profile'])
    return 'e5'
