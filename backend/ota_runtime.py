"""Desktop module context for the shared portable-runtime assembler."""
from .resources import resource as _resource
_source = _resource('generator/ota_runtime.py')
exec(compile(_source.read_bytes(), str(_source), 'exec'))
