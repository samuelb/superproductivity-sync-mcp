"""MCP tool surface."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from . import mutations as m
from . import queries as q
from .config import Settings
from .reducers import StateError
from .store import ConflictError, SyncError, SyncStore, UnreadableSyncFile
from .syncfile import SyncFormatError
from .timeutil import InputError
from .webdav import AuthFailed, WebDavError

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
You are connected to the user's Super Productivity task planner through its Nextcloud
sync file. Reads reflect the last sync of the user's devices; writes are appended to the
shared operation log and appear on every device on its next sync (usually within a minute).

Conventions:
- Ids are opaque strings; always pass the exact id returned by a previous tool call.
- Days are ISO dates (YYYY-MM-DD) in the user's time zone; `today`, `tomorrow` and `+N`
  are accepted where a day is expected. Times are HH:MM (24h).
- Durations are minutes.
- 'TODAY' is a virtual tag: a task is in Today when its due day is today. Use
  `schedule_task` instead of assigning the TODAY tag.
- Prefer `get_today` for "what should I do today", `get_planner` for the week ahead and
  `list_tasks` with filters for everything else. Call `get_task` before editing a task.
- Every write rewrites the whole sync file: create several tasks with one `create_tasks`
  call rather than repeated `create_task` calls.
"""


class NewTask(BaseModel):
    """One task for ``create_tasks``; the fields of ``create_task``."""

    title: str
    project_id: str | None = None
    notes: Annotated[str | None, Field(description="Markdown notes")] = None
    tag_ids: list[str] | None = None
    due_day: Annotated[str | None, Field(description="YYYY-MM-DD, 'today', 'tomorrow' or '+N'")] = None
    time_estimate_minutes: Annotated[int | None, Field(ge=0)] = None
    parent_task_id: Annotated[str | None, Field(description="An existing task, not one from this batch")] = None


RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _min_to_ms(minutes: int | None) -> int | None:
    return None if minutes is None else int(minutes) * 60000


def tool_errors[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Turn the failures a tool can anticipate into ``ToolError``.

    The SDK reports a ``ToolError`` to the caller verbatim (``isError`` result) and
    logs it at INFO; any other exception is a crash: the caller only sees
    "Error executing tool <name>" and the server logs a traceback. Everything
    below is expected at runtime and must carry a message the agent can act on.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except (m.MutationError, InputError) as e:
            # The caller's mistake (unknown id, bad day string, empty title, ...).
            # Deliberately not every ValueError: one raised by the codec, the op
            # builder or the WebDAV client is a bug and must surface as a crash.
            raise ToolError(str(e)) from e
        except ConflictError as e:
            log.warning("%s: %s", fn.__name__, e)
            raise ToolError(
                "Another device is writing to the sync file right now; the change was not "
                f"applied. Retry in a few seconds. ({e})"
            ) from e
        except AuthFailed as e:
            log.error("%s: %s", fn.__name__, e)
            raise ToolError(f"Nextcloud rejected the server's credentials; this needs the operator. ({e})") from e
        except UnreadableSyncFile as e:
            log.error("%s: %s", fn.__name__, e)
            raise ToolError(
                f"The sync file cannot be read with this server's settings; this needs the operator. ({e})"
            ) from e
        except (SyncError, WebDavError) as e:
            log.warning("%s: %s", fn.__name__, e)
            raise ToolError(f"Could not reach or read the Nextcloud sync file; retry later. ({e})") from e
        except (StateError, SyncFormatError) as e:
            log.error("%s: %s", fn.__name__, e)
            raise ToolError(
                f"The sync file's content is not in the expected shape; this needs the operator. ({e})"
            ) from e

    return wrapper  # type: ignore[return-value]


def build_server(store: SyncStore, settings: Settings) -> MCPServer:
    server = MCPServer(
        name="super-productivity",
        title="Super Productivity",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    async def snapshot():
        """Read-only view of the (cached) snapshot; never pass it to store.mutate."""
        return store.read_context_for(await store.load())

    def task_out(ctx, task: dict[str, Any]) -> dict[str, Any]:
        return q.task_summary(ctx.state, task, tz=ctx.tz, today=ctx.today, include_subtasks=True)

    # ------------------------------------------------------------------ reads

    @server.tool(
        annotations=RO,
        description="High-level overview: today's counts, projects and tags with ids.",
    )
    @tool_errors
    async def get_overview() -> dict[str, Any]:
        ctx = await snapshot()
        return q.overview(ctx.state, tz=ctx.tz, today=ctx.today, now_ms=ctx.now_ms)

    @server.tool(annotations=RO, description="List projects (id, title, task counts).")
    @tool_errors
    async def list_projects(
        include_archived: Annotated[bool, Field(description="Include archived projects")] = False,
    ) -> dict[str, Any]:
        ctx = await snapshot()
        return {"projects": q.list_projects(ctx.state, include_archived=include_archived)}

    @server.tool(annotations=RO, description="List tags (id, title, color, task count).")
    @tool_errors
    async def list_tags() -> dict[str, Any]:
        ctx = await snapshot()
        return {"tags": q.list_tags(ctx.state)}

    @server.tool(
        annotations=RO,
        description=(
            "List tasks with optional filters. `due` accepts a day ('today', 'tomorrow', '+N' or "
            "YYYY-MM-DD), 'overdue' or 'unscheduled'. tag_id 'TODAY' lists the tasks in Today. Sub-tasks "
            "are omitted unless include_subtasks is true."
        ),
    )
    @tool_errors
    async def list_tasks(
        project_id: Annotated[str | None, Field(description="Only tasks of this project")] = None,
        tag_id: Annotated[str | None, Field(description="Only tasks carrying this tag")] = None,
        include_done: Annotated[bool, Field(description="Include completed tasks")] = False,
        due: Annotated[
            str | None,
            Field(description="'today' | 'tomorrow' | '+N' | YYYY-MM-DD | 'overdue' | 'unscheduled'"),
        ] = None,
        query: Annotated[str | None, Field(description="Case-insensitive substring of title or notes")] = None,
        include_subtasks: Annotated[bool, Field(description="Also list sub-tasks")] = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        ctx = await snapshot()
        tasks = q.list_tasks(
            ctx.state,
            tz=ctx.tz,
            today=ctx.today,
            project_id=project_id,
            tag_id=tag_id,
            include_done=include_done,
            due=due,
            query=query,
            include_subtasks=include_subtasks,
            limit=limit,
        )
        return {"today": ctx.today, "count": len(tasks), "tasks": tasks}

    @server.tool(annotations=RO, description="Full details of one task including notes and sub-tasks.")
    @tool_errors
    async def get_task(task_id: str) -> dict[str, Any]:
        ctx = await snapshot()
        task = m.require_task(ctx.state, task_id)
        return task_out(ctx, task)

    @server.tool(
        annotations=RO,
        description="Today's plan: open tasks in the user's order, tasks done today, and overdue tasks.",
    )
    @tool_errors
    async def get_today() -> dict[str, Any]:
        ctx = await snapshot()
        return q.today_view(ctx.state, tz=ctx.tz, today=ctx.today, now_ms=ctx.now_ms)

    @server.tool(annotations=RO, description="Tasks planned per day for the next N days (starting today).")
    @tool_errors
    async def get_planner(days: Annotated[int, Field(ge=1, le=60)] = 7) -> dict[str, Any]:
        ctx = await snapshot()
        return q.planner_view(ctx.state, tz=ctx.tz, today=ctx.today, days=days)

    @server.tool(annotations=RO, description="List notes, optionally only those of one project.")
    @tool_errors
    async def list_notes(project_id: str | None = None) -> dict[str, Any]:
        ctx = await snapshot()
        return {"notes": q.list_notes(ctx.state, project_id=project_id, tz=ctx.tz)}

    @server.tool(
        annotations=RO,
        description="Search tasks, projects, tags and notes by text. Returns ids usable with `fetch`.",
    )
    @tool_errors
    async def search(query: str) -> dict[str, Any]:
        ctx = await snapshot()
        return {"results": q.search(ctx.state, query, tz=ctx.tz, today=ctx.today)}

    @server.tool(annotations=RO, description="Fetch one document returned by `search` (e.g. 'task:<id>').")
    @tool_errors
    async def fetch(id: str) -> dict[str, Any]:  # noqa: A002 (name required by ChatGPT connectors)
        ctx = await snapshot()
        try:
            return q.fetch(ctx.state, id, tz=ctx.tz, today=ctx.today)
        except KeyError as e:
            raise m.NotFoundError(f"Unknown document id {id!r}") from e

    @server.tool(
        annotations=RO,
        description="Sync file status: version counters, last writer, encryption flags.",
    )
    @tool_errors
    async def sync_status(
        refresh: Annotated[bool, Field(description="Bypass the short-lived cache")] = False,
    ) -> dict[str, Any]:
        sf = await store.load(force=refresh)
        ctx = store.read_context_for(sf)
        data = sf.data
        return {
            "clientId": store.identity.client_id,
            "today": ctx.today,
            "timezone": str(ctx.tz),
            "syncVersion": data.get("syncVersion"),
            "schemaVersion": data.get("schemaVersion"),
            "lastModified": q.ms_to_iso(data.get("lastModified"), ctx.tz),
            "lastWriterClientId": data.get("clientId"),
            "recentOps": len(data.get("recentOps") or []),
            "vectorClock": data.get("vectorClock"),
            "compressed": sf.flags.is_compressed,
            "encrypted": sf.flags.is_encrypted,
            "sizeBytes": len(sf.raw.encode("utf-8")),
        }

    # ----------------------------------------------------------------- writes

    @server.tool(
        annotations=RW,
        description=(
            "Create a task. Defaults to the user's default project (usually Inbox). Pass parent_task_id to "
            "create a sub-task (sub-tasks inherit project and tags and cannot be scheduled)."
        ),
    )
    @tool_errors
    async def create_task(
        title: str,
        project_id: str | None = None,
        notes: Annotated[str | None, Field(description="Markdown notes")] = None,
        tag_ids: list[str] | None = None,
        due_day: Annotated[str | None, Field(description="YYYY-MM-DD, 'today', 'tomorrow' or '+N'")] = None,
        time_estimate_minutes: Annotated[int | None, Field(ge=0)] = None,
        parent_task_id: str | None = None,
        add_to_bottom: Annotated[bool, Field(description="Append at the end instead of the top")] = False,
    ) -> dict[str, Any]:
        return await store.mutate(
            lambda ctx: task_out(
                ctx,
                m.create_task(
                    ctx,
                    title=title,
                    project_id=project_id,
                    notes=notes,
                    tag_ids=tag_ids,
                    due_day=due_day,
                    time_estimate_ms=_min_to_ms(time_estimate_minutes),
                    parent_task_id=parent_task_id,
                    add_to_bottom=add_to_bottom,
                ),
            )
        )

    @server.tool(
        annotations=RW,
        description=(
            f"Create up to {m.MAX_BATCH} tasks in one write, all or nothing, in the given order. Each item takes "
            "the fields of create_task; parent_task_id must name an existing task. Much faster than repeated "
            "create_task calls."
        ),
    )
    @tool_errors
    async def create_tasks(
        tasks: Annotated[list[NewTask], Field(min_length=1, max_length=m.MAX_BATCH)],
        add_to_bottom: Annotated[bool, Field(description="Append at the end instead of the top")] = False,
    ) -> dict[str, Any]:
        specs = [
            {
                "title": t.title,
                "project_id": t.project_id,
                "notes": t.notes,
                "tag_ids": t.tag_ids,
                "due_day": t.due_day,
                "time_estimate_ms": _min_to_ms(t.time_estimate_minutes),
                "parent_task_id": t.parent_task_id,
            }
            for t in tasks
        ]

        def run(ctx) -> dict[str, Any]:
            created = m.create_tasks(ctx, specs, add_to_bottom=add_to_bottom)
            return {"count": len(created), "tasks": [task_out(ctx, t) for t in created]}

        return await store.mutate(run)

    @server.tool(
        annotations=RW,
        description="Update title, notes, done state, time estimate or tags of a task. Only given fields change.",
    )
    @tool_errors
    async def update_task(
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        is_done: bool | None = None,
        time_estimate_minutes: Annotated[int | None, Field(ge=0)] = None,
        tag_ids: Annotated[list[str] | None, Field(description="Replaces the full tag list")] = None,
    ) -> dict[str, Any]:
        return await store.mutate(
            lambda ctx: task_out(
                ctx,
                m.update_task(
                    ctx,
                    task_id,
                    title=title,
                    notes=notes,
                    is_done=is_done,
                    time_estimate_ms=_min_to_ms(time_estimate_minutes),
                    tag_ids=tag_ids,
                ),
            )
        )

    @server.tool(
        annotations=RW,
        description="Schedule a task for a day (adds it to Today when day is today) and optionally a time.",
    )
    @tool_errors
    async def schedule_task(
        task_id: str,
        day: Annotated[str, Field(description="YYYY-MM-DD, 'today', 'tomorrow' or '+N'")],
        time: Annotated[str | None, Field(description="HH:MM (24h) in the user's time zone")] = None,
    ) -> dict[str, Any]:
        return await store.mutate(lambda ctx: task_out(ctx, m.schedule_task(ctx, task_id, day=day, time_hhmm=time)))

    @server.tool(
        annotations=RW,
        description="Remove day/time scheduling from a task (also removes it from Today).",
    )
    @tool_errors
    async def unschedule_task(task_id: str) -> dict[str, Any]:
        return await store.mutate(lambda ctx: task_out(ctx, m.unschedule_task(ctx, task_id)))

    @server.tool(annotations=RW, description="Move a task (with its sub-tasks) to another project.")
    @tool_errors
    async def move_task_to_project(task_id: str, project_id: str) -> dict[str, Any]:
        return await store.mutate(lambda ctx: task_out(ctx, m.move_task_to_project(ctx, task_id, project_id)))

    @server.tool(annotations=DESTRUCTIVE, description="Permanently delete a task and its sub-tasks.")
    @tool_errors
    async def delete_task(task_id: str) -> dict[str, Any]:
        return await store.mutate(lambda ctx: m.delete_task(ctx, task_id))

    @server.tool(annotations=RW, description="Create a project.")
    @tool_errors
    async def create_project(title: str, is_enable_backlog: bool = False) -> dict[str, Any]:
        return await store.mutate(
            lambda ctx: q.project_summary(
                ctx.state, m.create_project(ctx, title=title, is_enable_backlog=is_enable_backlog)
            )
        )

    @server.tool(
        annotations=RW,
        description="Rename a project and/or archive (is_archived=true) or unarchive it.",
    )
    @tool_errors
    async def update_project(
        project_id: str, title: str | None = None, is_archived: bool | None = None
    ) -> dict[str, Any]:
        return await store.mutate(
            lambda ctx: q.project_summary(
                ctx.state, m.update_project(ctx, project_id, title=title, is_archived=is_archived)
            )
        )

    @server.tool(annotations=RW, description="Create a tag. Color is an optional hex string like #29a1aa.")
    @tool_errors
    async def create_tag(title: str, color: str | None = None) -> dict[str, Any]:
        return await store.mutate(lambda ctx: q.tag_summary(ctx.state, m.create_tag(ctx, title=title, color=color)))

    @server.tool(
        annotations=RW,
        description="Create a (markdown) note, optionally attached to a project or pinned to Today.",
    )
    @tool_errors
    async def create_note(content: str, project_id: str | None = None, pin_to_today: bool = False) -> dict[str, Any]:
        return await store.mutate(
            lambda ctx: q.note_summary(
                ctx.state,
                m.create_note(ctx, content=content, project_id=project_id, pin_to_today=pin_to_today),
                ctx.tz,
            )
        )

    @server.tool(annotations=RW, description="Replace the content of a note.")
    @tool_errors
    async def update_note(note_id: str, content: str) -> dict[str, Any]:
        return await store.mutate(
            lambda ctx: q.note_summary(ctx.state, m.update_note(ctx, note_id, content=content), ctx.tz)
        )

    @server.tool(annotations=DESTRUCTIVE, description="Permanently delete a note.")
    @tool_errors
    async def delete_note(note_id: str) -> dict[str, Any]:
        return await store.mutate(lambda ctx: m.delete_note(ctx, note_id))

    return server
