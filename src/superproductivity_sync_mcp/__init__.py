"""Super Productivity MCP server.

Acts as an additional Super Productivity sync client: it reads the shared
``sync-data.json`` from Nextcloud (WebDAV), exposes the task state through the
Model Context Protocol and writes changes back as operation-log entries that
every other Super Productivity installation replays on its next sync.
"""

__version__ = "0.1.0"
