"""Super Productivity MCP server.

Acts as an additional Super Productivity sync client: it reads the shared
``sync-data.json`` from Nextcloud (WebDAV), exposes the task state through the
Model Context Protocol and writes changes back as operation-log entries that
every other Super Productivity installation replays on its next sync.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("superproductivity-sync-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0"
