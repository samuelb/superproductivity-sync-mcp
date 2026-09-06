"""Tool-level mutations: validate input, apply the reducer mirror to the
snapshot and emit the matching operation(s)."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from . import reducers as r
from .ids import nanoid
from .timeutil import day_time_to_ms, db_date_str, resolve_day

if TYPE_CHECKING:
    from .store import MutationContext


class MutationError(ValueError):
    """User-facing validation error."""


class NotFoundError(MutationError):
    pass


# --- defaults (work-context.const.ts, project.const.ts, tag.const.ts) --------

WORKLOG_EXPORT_DEFAULTS = {
    "cols": ["DATE", "START", "END", "TIME_CLOCK", "TITLES_INCLUDING_SUB"],
    "roundWorkTimeTo": None,
    "roundStartTimeTo": None,
    "roundEndTimeTo": None,
    "separateTasksBy": " | ",
    "groupBy": "DATE",
}
DEFAULT_PROJECT_COLOR = "#29a1aa"
DEFAULT_TAG_COLOR = "#a05db1"
WORK_CONTEXT_DEFAULT_THEME = {
    "isAutoContrast": True,
    "isDisableBackgroundTint": False,
    "primary": DEFAULT_TAG_COLOR,
    "huePrimary": "500",
    "accent": "#ff4081",
    "hueAccent": "500",
    "warn": "#e11826",
    "hueWarn": "500",
    "backgroundImageDark": None,
    "backgroundImageLight": None,
    "backgroundOverlayOpacity": 20,
    "backgroundImageBlur": 0,
}
TAG_COLORS = [
    "#a05db1",
    "#29a1aa",
    "#e91e63",
    "#3f51b5",
    "#009688",
    "#ff9800",
    "#795548",
    "#607d8b",
    "#8bc34a",
    "#f44336",
    "#00bcd4",
    "#9c27b0",
]


def default_task(now_ms: int) -> dict[str, Any]:
    return {
        "id": "",
        "subTaskIds": [],
        "timeSpentOnDay": {},
        "timeSpent": 0,
        "timeEstimate": 0,
        "isDone": False,
        "title": "",
        "tagIds": [],
        "created": now_ms,
        "attachments": [],
    }


def _work_context_common() -> dict[str, Any]:
    return {
        "advancedCfg": {"worklogExportSettings": dict(WORKLOG_EXPORT_DEFAULTS)},
        "theme": dict(WORK_CONTEXT_DEFAULT_THEME),
        "taskIds": [],
        "icon": None,
        "id": "",
        "title": "",
    }


def default_project() -> dict[str, Any]:
    return {
        "isHiddenFromMenu": False,
        "isArchived": False,
        "isDone": False,
        "doneOn": None,
        "isEnableBacklog": False,
        "backlogTaskIds": [],
        "noteIds": [],
        **_work_context_common(),
        "theme": {**WORK_CONTEXT_DEFAULT_THEME, "primary": DEFAULT_PROJECT_COLOR},
    }


def default_tag(now_ms: int) -> dict[str, Any]:
    return {
        "color": None,
        "created": now_ms,
        **_work_context_common(),
        "icon": None,
        "title": "",
        "id": "",
        "theme": {**WORK_CONTEXT_DEFAULT_THEME, "primary": DEFAULT_TAG_COLOR},
    }


# --- lookups ----------------------------------------------------------------


def require_task(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = r.get_entity(r.slice_(state, "task"), task_id)
    if task is None:
        raise NotFoundError(f"Task '{task_id}' not found (it may be archived or deleted)")
    return task


def require_project(state: dict[str, Any], project_id: str) -> dict[str, Any]:
    project = r.get_entity(r.slice_(state, "project"), project_id)
    if project is None:
        raise NotFoundError(f"Project '{project_id}' not found")
    return project


def require_note(state: dict[str, Any], note_id: str) -> dict[str, Any]:
    note = r.get_entity(r.slice_(state, "note"), note_id)
    if note is None:
        raise NotFoundError(f"Note '{note_id}' not found")
    return note


def validate_tag_ids(state: dict[str, Any], tag_ids: list[str] | None) -> list[str]:
    if not tag_ids:
        return []
    tags = r.slice_(state, "tag")
    out: list[str] = []
    for tid in tag_ids:
        if tid == r.TODAY_TAG_ID:
            raise MutationError("'TODAY' is a virtual tag; schedule the task for today instead")
        if r.get_entity(tags, tid) is None:
            raise NotFoundError(f"Tag '{tid}' not found")
        out.append(tid)
    return r.unique(out)


def default_project_id(state: dict[str, Any]) -> str:
    projects = r.slice_(state, "project")
    cfg = (state.get("globalConfig") or {}).get("tasks") or {}
    candidate = cfg.get("defaultProjectId") or r.INBOX_PROJECT_ID
    if r.get_entity(projects, candidate) is not None:
        return candidate
    if r.get_entity(projects, r.INBOX_PROJECT_ID) is not None:
        return r.INBOX_PROJECT_ID
    for pid in projects["ids"]:
        p = projects["entities"].get(pid)
        if p and not p.get("isArchived"):
            return pid
    raise MutationError("No project available to add the task to")


def task_with_subtasks(state: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    tasks = r.slice_(state, "task")
    subs = [st for sid in task.get("subTaskIds") or [] if (st := r.get_entity(tasks, sid))]
    return {**task, "subTasks": subs}


def _clean_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        raise MutationError("Title must not be empty")
    return t


# --- tasks ------------------------------------------------------------------


def create_task(
    ctx: MutationContext,
    *,
    title: str,
    project_id: str | None = None,
    notes: str | None = None,
    tag_ids: list[str] | None = None,
    due_day: str | None = None,
    time_estimate_ms: int | None = None,
    parent_task_id: str | None = None,
    add_to_bottom: bool = False,
) -> dict[str, Any]:
    state = ctx.state
    title = _clean_title(title)
    extra: dict[str, Any] = {}
    if notes:
        extra["notes"] = notes
    if time_estimate_ms:
        extra["timeEstimate"] = int(time_estimate_ms)

    if parent_task_id:
        parent = require_task(state, parent_task_id)
        if parent.get("parentId"):
            raise MutationError("Sub-tasks cannot have sub-tasks of their own")
        if due_day or tag_ids or project_id:
            raise MutationError("Sub-tasks inherit project and tags from the parent and cannot be scheduled")
        task = {
            **default_task(ctx.now_ms),
            "title": title,
            "id": nanoid(),
            "projectId": parent.get("projectId") or "",
            "tagIds": [],
            **extra,
        }
        r.add_sub_task(state, task, parent_task_id)
        ctx.emit("addSubTask", task["id"], {"task": task, "parentId": parent_task_id})
        return r.get_entity(r.slice_(state, "task"), task["id"]) or task

    pid = project_id or default_project_id(state)
    project = require_project(state, pid)
    if project.get("isArchived"):
        raise MutationError(f"Project '{project.get('title')}' is archived")
    tags = validate_tag_ids(state, tag_ids)
    task = {
        **default_task(ctx.now_ms),
        "title": title,
        "id": nanoid(),
        "projectId": pid,
        "tagIds": tags,
        **extra,
    }
    if due_day:
        task["dueDay"] = resolve_day(due_day, ctx.today)
    r.add_task(
        state,
        task,
        is_add_to_bottom=add_to_bottom,
        is_add_to_backlog=False,
        today=ctx.today,
        default_task=default_task(ctx.now_ms),
    )
    ctx.emit(
        "addTask",
        task["id"],
        {
            "task": task,
            "workContextId": pid,
            "workContextType": "PROJECT",
            "isAddToBacklog": False,
            "isAddToBottom": add_to_bottom,
        },
    )
    return r.get_entity(r.slice_(state, "task"), task["id"]) or task


def update_task(
    ctx: MutationContext,
    task_id: str,
    *,
    title: str | None = None,
    notes: str | None = None,
    is_done: bool | None = None,
    time_estimate_ms: int | None = None,
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    state = ctx.state
    task = require_task(state, task_id)
    changes: dict[str, Any] = {}
    if title is not None:
        changes["title"] = _clean_title(title)
    if notes is not None:
        changes["notes"] = notes
    if time_estimate_ms is not None:
        if time_estimate_ms < 0:
            raise MutationError("time estimate must not be negative")
        changes["timeEstimate"] = int(time_estimate_ms)
    if tag_ids is not None:
        if task.get("parentId"):
            raise MutationError("Sub-tasks cannot carry tags; tag the parent task instead")
        changes["tagIds"] = validate_tag_ids(state, tag_ids)
    if is_done is not None:
        changes["isDone"] = bool(is_done)
        if is_done:
            changes["doneOn"] = ctx.now_ms
    if not changes:
        raise MutationError("Nothing to update")
    r.update_task(state, task_id, changes, today=ctx.today, now_ms=ctx.now_ms)
    ctx.emit("updateTask", task_id, {"task": {"id": task_id, "changes": changes}})
    return require_task(state, task_id)


def schedule_task(ctx: MutationContext, task_id: str, *, day: str, time_hhmm: str | None = None) -> dict[str, Any]:
    state = ctx.state
    task = require_task(state, task_id)
    if task.get("parentId"):
        raise MutationError("Sub-tasks cannot be scheduled; schedule the parent task")
    resolved = resolve_day(day, ctx.today)
    # The op must carry the task as the app would have dispatched it, i.e. as it
    # was *before* the reducer ran; the reducer mirror mutates `task` in place.
    dispatched = copy.deepcopy(task)
    if time_hhmm:
        due_ms = day_time_to_ms(resolved, time_hhmm, ctx.tz)
        is_for_today = db_date_str(due_ms - ctx.start_of_next_day_diff_ms, ctx.tz) == ctx.today
        r.schedule_task_with_time(state, task_id, due_ms, is_scheduled_for_today=is_for_today)
        ctx.emit(
            "scheduleTaskWithTime",
            task_id,
            {"task": dispatched, "dueWithTime": due_ms, "isMoveToBacklog": False},
        )
    else:
        r.plan_task_for_day(state, task, resolved, is_add_to_top=False, today=ctx.today)
        ctx.emit("planTaskForDay", task_id, {"task": dispatched, "day": resolved, "isAddToTop": False})
    return require_task(state, task_id)


def unschedule_task(ctx: MutationContext, task_id: str) -> dict[str, Any]:
    state = ctx.state
    require_task(state, task_id)
    r.unschedule_task(state, task_id)
    ctx.emit("unscheduleTask", task_id, {"id": task_id, "today": ctx.today})
    return require_task(state, task_id)


def move_task_to_project(ctx: MutationContext, task_id: str, project_id: str) -> dict[str, Any]:
    state = ctx.state
    task = require_task(state, task_id)
    if task.get("parentId"):
        raise MutationError("Move the parent task instead; sub-tasks follow their parent")
    target = require_project(state, project_id)
    if target.get("isArchived"):
        raise MutationError(f"Project '{target.get('title')}' is archived")
    if task.get("projectId") == project_id:
        raise MutationError("Task is already in that project")
    tws = task_with_subtasks(state, task)
    dispatched = copy.deepcopy(tws)  # pre-reducer snapshot, see schedule_task
    r.move_to_other_project(state, tws, project_id)
    ctx.emit("moveToOtherProject", task_id, {"task": dispatched, "targetProjectId": project_id})
    return require_task(state, task_id)


def delete_task(ctx: MutationContext, task_id: str) -> dict[str, Any]:
    state = ctx.state
    task = require_task(state, task_id)
    tws = copy.deepcopy(task_with_subtasks(state, task))  # pre-reducer snapshot, see schedule_task
    r.delete_task(state, tws)
    ctx.emit("deleteTask", task_id, {"task": tws})
    return {"deleted": task_id, "deletedSubTasks": [s["id"] for s in tws["subTasks"]]}


# --- projects / tags -----------------------------------------------------------


def create_project(ctx: MutationContext, *, title: str, is_enable_backlog: bool = False) -> dict[str, Any]:
    state = ctx.state
    project = {
        **default_project(),
        "title": _clean_title(title),
        "isEnableBacklog": bool(is_enable_backlog),
        "id": nanoid(),
    }
    r.add_project(state, project)
    ctx.emit("addProject", project["id"], {"project": project})
    return project


def update_project(
    ctx: MutationContext,
    project_id: str,
    *,
    title: str | None = None,
    is_archived: bool | None = None,
) -> dict[str, Any]:
    state = ctx.state
    project = require_project(state, project_id)
    if title is not None:
        changes = {"title": _clean_title(title)}
        r.update_project(state, project_id, changes)
        ctx.emit("updateProject", project_id, {"project": {"id": project_id, "changes": changes}})
    if is_archived is not None and bool(project.get("isArchived")) != is_archived:
        if project_id == r.INBOX_PROJECT_ID:
            raise MutationError("The Inbox project cannot be archived")
        if is_archived:
            r.archive_project(state, project_id)
            ctx.emit("archiveProject", project_id, {"id": project_id})
        else:
            r.unarchive_project(state, project_id)
            ctx.emit("unarchiveProject", project_id, {"id": project_id})
    if not ctx.ops:
        raise MutationError("Nothing to update")
    return require_project(state, project_id)


def create_tag(ctx: MutationContext, *, title: str, color: str | None = None) -> dict[str, Any]:
    import random

    state = ctx.state
    title = _clean_title(title)
    tags = r.slice_(state, "tag")
    for tid in tags["ids"]:
        t = tags["entities"].get(tid) or {}
        if (t.get("title") or "").strip().lower() == title.lower():
            raise MutationError(f"A tag named '{t.get('title')}' already exists (id {tid})")
    tag = {
        **default_tag(ctx.now_ms),
        "id": nanoid(),
        "title": title,
        "created": ctx.now_ms,
        "icon": None,
        "taskIds": [],
        "color": color or random.choice(TAG_COLORS),  # noqa: S311
    }
    r.add_tag(state, tag)
    ctx.emit("addTag", tag["id"], {"tag": tag})
    return tag


# --- notes ---------------------------------------------------------------------


def create_note(
    ctx: MutationContext, *, content: str, project_id: str | None = None, pin_to_today: bool = False
) -> dict[str, Any]:
    state = ctx.state
    if not content.strip():
        raise MutationError("Note content must not be empty")
    if project_id:
        require_project(state, project_id)
    note = {
        "id": nanoid(),
        "projectId": project_id or None,
        "isPinnedToToday": bool(pin_to_today),
        "content": content,
        "created": ctx.now_ms,
        "modified": ctx.now_ms,
    }
    r.add_note(state, note)
    ctx.emit("addNote", note["id"], {"note": note})
    return note


def update_note(ctx: MutationContext, note_id: str, *, content: str) -> dict[str, Any]:
    state = ctx.state
    require_note(state, note_id)
    changes = {"content": content, "modified": ctx.now_ms}
    r.update_note(state, note_id, changes)
    ctx.emit("updateNote", note_id, {"note": {"id": note_id, "changes": changes}})
    return require_note(state, note_id)


def delete_note(ctx: MutationContext, note_id: str) -> dict[str, Any]:
    state = ctx.state
    note = require_note(state, note_id)
    r.delete_note(state, note_id, note.get("projectId"))
    ctx.emit(
        "deleteNote",
        note_id,
        {
            "id": note_id,
            "projectId": note.get("projectId"),
            "isPinnedToToday": bool(note.get("isPinnedToToday")),
        },
    )
    return {"deleted": note_id}
