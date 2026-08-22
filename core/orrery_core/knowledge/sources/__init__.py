"""Document sources feeding the knowledge index."""

from .confluence import ConfluenceSource as ConfluenceSource
from .confluence import build_from_env as build_from_env
from .filesystem import FilesystemSource as FilesystemSource
from .filesystem import GitSource as GitSource

#: Clearer at the call site than a bare ``build_from_env``.
confluence_from_env = build_from_env
