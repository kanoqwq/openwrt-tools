"""Desktop module context for the shared clean release-index format."""
from .resources import resource as _resource
_source = _resource('generator/release_index.py')
exec(compile(_source.read_bytes(), str(_source), 'exec'))
