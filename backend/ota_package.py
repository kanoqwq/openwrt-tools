"""Desktop module context for the shared desktop/device release reader."""
from .resources import resource as _resource
_source = _resource('generator/ota_package.py')
exec(compile(_source.read_bytes(), str(_source), 'exec'))
