"""Read-only views over the state snapshot, shaped for LLM consumption."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import reducers as r
from .timeutil import InputError, db_date_str, ms_to_iso, resolve_day

TODAY = r.TODAY_TAG_ID


def _entities(state: dict[str, Any], key: str) -> tuple[list[str], dict[str, Any]]:
    """Return ``(ids, entities)`` of a slice *by reference*; callers only read."""
    es = state.get(key) or {}
    return es.get("ids") or [], es.get("entities") or {}


def effective_due_day(task: dict[str, Any], tz: ZoneInfo) -> str | None:
    """The day a task is planned for: ``dueDay``, else the calendar day of ``dueWithTime``."""
    due_day = task.get("dueDay")
    if isinstance(due_day, str) and due_day:
        return due_day
    due_ms = task.get("dueWithTime")
    if isinstance(due_ms, (int, float)) and due_ms > 0:
        return db_date_str(due_ms, tz)
    return None


def _ms_to_min(ms: Any) -> int:
    return int(round((ms or 0) / 60000)) if isinstance(ms, (int, float)) else 0


def task_summary(
    state: dict[str, Any],
    task: dict[str, Any],
    *,
    tz: ZoneInfo,
    today: str,
    include_notes: bool = True,
    include_subtasks: bool = False,
) -> dict[str, Any]:
    _, projects = _entities(state, "project")
    _, tags = _entities(state, "tag")
    _, tasks = _entities(state, "task")
    project = projects.get(task.get("projectId") or "")
    due_day = task.get("dueDay")
    due_ms = task.get("dueWithTime")
    eff_day = effective_due_day(task, tz)
    out: dict[str, Any] = {
        "id": task["id"],
        "title": task.get("title", ""),
        "isDone": bool(task.get("isDone")),
        "projectId": task.get("projectId") or None,
        "project": (project or {}).get("title"),
        "tags": [{"id": t, "title": (tags.get(t) or {}).get("title")} for t in task.get("tagIds") or [] if t != TODAY],
        "dueDay": due_day,
        "dueWithTime": ms_to_iso(due_ms, tz),
        "isOverdue": bool(not task.get("isDone") and eff_day is not None and eff_day < today),
        "timeEstimateMinutes": _ms_to_min(task.get("timeEstimate")),
        "timeSpentMinutes": _ms_to_min(task.get("timeSpent")),
        "parentId": task.get("parentId"),
        "subTaskIds": list(task.get("subTaskIds") or []),
        "created": ms_to_iso(task.get("created"), tz),
        "doneOn": ms_to_iso(task.get("doneOn"), tz),
    }
    if task.get("deadlineDay") or task.get("deadlineWithTime"):
        out["deadline"] = task.get("deadlineDay") or ms_to_iso(task.get("deadlineWithTime"), tz)
    if task.get("repeatCfgId"):
        out["isRepeating"] = True
    if include_notes and task.get("notes"):
        out["notes"] = task["notes"]
    if include_subtasks and task.get("subTaskIds"):
        out["subTasks"] = [
            task_summary(state, st, tz=tz, today=today, include_notes=include_notes)
            for sid in task["subTaskIds"]
            if (st := tasks.get(sid))
        ]
    return out


def project_summary(state: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    _, tasks = _entities(state, "task")
    open_count = sum(1 for tid in project.get("taskIds") or [] if not (tasks.get(tid) or {}).get("isDone", True))
    return {
        "id": project["id"],
        "title": project.get("title", ""),
        "isArchived": bool(project.get("isArchived")),
        "isHiddenFromMenu": bool(project.get("isHiddenFromMenu")),
        "isEnableBacklog": bool(project.get("isEnableBacklog")),
        "taskCount": len(project.get("taskIds") or []),
        "openTaskCount": open_count,
        "backlogTaskCount": len(project.get("backlogTaskIds") or []),
        "noteCount": len(project.get("noteIds") or []),
    }


def tag_summary(state: dict[str, Any], tag: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": tag["id"],
        "title": tag.get("title", ""),
        "color": tag.get("color") or (tag.get("theme") or {}).get("primary"),
        "taskCount": len(tag.get("taskIds") or []),
    }
    if tag["id"] == TODAY:
        out["isVirtual"] = True
        out["note"] = "Membership is derived from task.dueDay; do not assign this tag directly."
    return out


def note_summary(state: dict[str, Any], note: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    _, projects = _entities(state, "project")
    return {
        "id": note["id"],
        "projectId": note.get("projectId"),
        "project": (projects.get(note.get("projectId") or "") or {}).get("title"),
        "isPinnedToToday": bool(note.get("isPinnedToToday")),
        "content": note.get("content", ""),
        "created": ms_to_iso(note.get("created"), tz),
        "modified": ms_to_iso(note.get("modified"), tz),
    }


def list_projects(state: dict[str, Any], *, include_archived: bool) -> list[dict[str, Any]]:
    ids, ents = _entities(state, "project")
    out = []
    for pid in ids:
        p = ents.get(pid)
        if not p or (p.get("isArchived") and not include_archived):
            continue
        out.append(project_summary(state, p))
    return out


def list_tags(state: dict[str, Any]) -> list[dict[str, Any]]:
    ids, ents = _entities(state, "tag")
    return [tag_summary(state, ents[t]) for t in ids if ents.get(t)]


def list_notes(state: dict[str, Any], *, project_id: str | None, tz: ZoneInfo) -> list[dict[str, Any]]:
    ids, ents = _entities(state, "note")
    out = []
    for nid in ids:
        n = ents.get(nid)
        if not n:
            continue
        if project_id is not None and (n.get("projectId") or None) != (project_id or None):
            continue
        out.append(note_summary(state, n, tz))
    return out


def _match(query: str, *fields: Any) -> bool:
    q = query.lower()
    return any(isinstance(f, str) and q in f.lower() for f in fields)


def list_tasks(
    state: dict[str, Any],
    *,
    tz: ZoneInfo,
    today: str,
    project_id: str | None = None,
    tag_id: str | None = None,
    include_done: bool = False,
    due: str | None = None,
    query: str | None = None,
    include_subtasks: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ids, ents = _entities(state, "task")
    _, projects = _entities(state, "project")
    due_keyword = due_day_filter = None
    if due:
        d = due.strip().lower()
        if d in ("overdue", "unscheduled"):
            due_keyword = d
        else:
            try:
                due_day_filter = resolve_day(d, today)
            except InputError as e:
                raise InputError(
                    f"Invalid due filter {due!r}; use 'today', 'tomorrow', '+N', YYYY-MM-DD, 'overdue' or 'unscheduled'"
                ) from e
    today_ids: set[str] = set()
    if tag_id == TODAY:
        # Virtual tag: the TODAY list plus anything due today, as in today_view.
        _, tags = _entities(state, "tag")
        today_ids = set((tags.get(TODAY) or {}).get("taskIds") or [])
    ordered: list[str] = ids
    if project_id:
        p = projects.get(project_id) or {}
        ordered = [*(p.get("taskIds") or []), *(p.get("backlogTaskIds") or [])]
    out: list[dict[str, Any]] = []
    for tid in ordered:
        t = ents.get(tid)
        if not t:
            continue
        if t.get("parentId") and not include_subtasks:
            continue
        if project_id and (t.get("projectId") or "") != project_id:
            continue
        if tag_id == TODAY:
            if tid not in today_ids and effective_due_day(t, tz) != today:
                continue
        elif tag_id and tag_id not in (t.get("tagIds") or []):
            continue
        if not include_done and t.get("isDone"):
            continue
        if due_keyword or due_day_filter:
            due_day = effective_due_day(t, tz)
            if due_keyword == "overdue" and not (due_day and due_day < today and not t.get("isDone")):
                continue
            if due_keyword == "unscheduled" and due_day is not None:
                continue
            if due_day_filter and due_day != due_day_filter:
                continue
        if query and not _match(query, t.get("title"), t.get("notes")):
            continue
        out.append(task_summary(state, t, tz=tz, today=today, include_notes=False, include_subtasks=include_subtasks))
        if len(out) >= limit:
            break
    return out


def today_view(state: dict[str, Any], *, tz: ZoneInfo, today: str, now_ms: int) -> dict[str, Any]:
    ids, ents = _entities(state, "task")
    _, tags = _entities(state, "tag")
    today_tag = tags.get(TODAY) or {}
    ordered = [tid for tid in today_tag.get("taskIds") or [] if ents.get(tid)]
    seen = set(ordered)
    for tid in ids:
        t = ents.get(tid) or {}
        if tid in seen or t.get("parentId"):
            continue
        if effective_due_day(t, tz) == today:
            ordered.append(tid)
            seen.add(tid)
    todo, done = [], []
    for tid in ordered:
        t = ents[tid]
        s = task_summary(state, t, tz=tz, today=today, include_notes=False, include_subtasks=True)
        (done if t.get("isDone") else todo).append(s)
    overdue = []
    for tid in ids:
        t = ents.get(tid) or {}
        if t.get("isDone") or t.get("parentId") or tid in seen:
            continue
        due_day = effective_due_day(t, tz)
        if due_day is not None and due_day < today:
            overdue.append(task_summary(state, t, tz=tz, today=today, include_notes=False))
    return {
        "today": today,
        "todo": todo,
        "done": done,
        "overdue": overdue,
        "estimateRemainingMinutes": sum(max(0, s["timeEstimateMinutes"] - s["timeSpentMinutes"]) for s in todo),
    }


def planner_view(state: dict[str, Any], *, tz: ZoneInfo, today: str, days: int) -> dict[str, Any]:
    ids, ents = _entities(state, "task")
    planner_days = (state.get("planner") or {}).get("days") or {}
    start = date.fromisoformat(today)
    out: dict[str, list[dict[str, Any]]] = {}
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        order = [tid for tid in planner_days.get(d) or [] if ents.get(tid)]
        seen = set(order)
        for tid in ids:
            t = ents.get(tid) or {}
            if tid in seen or t.get("parentId") or t.get("isDone"):
                continue
            if effective_due_day(t, tz) == d:
                order.append(tid)
                seen.add(tid)
        out[d] = [
            task_summary(state, ents[tid], tz=tz, today=today, include_notes=False)
            for tid in order
            if not ents[tid].get("isDone") or d == today
        ]
    return {"today": today, "days": out}


def overview(state: dict[str, Any], *, tz: ZoneInfo, today: str, now_ms: int) -> dict[str, Any]:
    ids, ents = _entities(state, "task")
    open_tasks = [t for tid in ids if (t := ents.get(tid)) and not t.get("isDone") and not t.get("parentId")]
    tv = today_view(state, tz=tz, today=today, now_ms=now_ms)
    return {
        "today": today,
        "timezone": str(tz),
        "openTasks": len(open_tasks),
        "todayTodo": len(tv["todo"]),
        "todayDone": len(tv["done"]),
        "overdue": len(tv["overdue"]),
        "projects": list_projects(state, include_archived=False),
        "tags": [t for t in list_tags(state) if t["id"] != TODAY],
        "notes": len(_entities(state, "note")[0]),
    }


def search(state: dict[str, Any], query: str, *, tz: ZoneInfo, today: str, limit: int = 20) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    ids, ents = _entities(state, "task")
    for tid in ids:
        t = ents.get(tid) or {}
        if _match(query, t.get("title"), t.get("notes")):
            results.append({"id": f"task:{tid}", "title": t.get("title", ""), "url": f"sp://task/{tid}"})
    pids, pents = _entities(state, "project")
    for pid in pids:
        p = pents.get(pid) or {}
        if _match(query, p.get("title")):
            results.append(
                {
                    "id": f"project:{pid}",
                    "title": f"Project: {p.get('title', '')}",
                    "url": f"sp://project/{pid}",
                }
            )
    nids, nents = _entities(state, "note")
    for nid in nids:
        n = nents.get(nid) or {}
        if _match(query, n.get("content")):
            results.append(
                {
                    "id": f"note:{nid}",
                    "title": (n.get("content") or "")[:80],
                    "url": f"sp://note/{nid}",
                }
            )
    tids, tents = _entities(state, "tag")
    for tg in tids:
        t = tents.get(tg) or {}
        if _match(query, t.get("title")):
            results.append({"id": f"tag:{tg}", "title": f"Tag: {t.get('title', '')}", "url": f"sp://tag/{tg}"})
    return results[:limit]


def fetch(state: dict[str, Any], doc_id: str, *, tz: ZoneInfo, today: str) -> dict[str, Any]:
    kind, _, raw_id = doc_id.partition(":")
    if not raw_id:
        kind, raw_id = "task", kind
    if kind == "task":
        _, ents = _entities(state, "task")
        t = ents.get(raw_id)
        if not t:
            raise KeyError(doc_id)
        s = task_summary(state, t, tz=tz, today=today, include_subtasks=True)
        text = f"# {s['title']}\n\n" + (s.get("notes") or "")
        return {
            "id": doc_id,
            "title": s["title"],
            "text": text,
            "url": f"sp://task/{raw_id}",
            "metadata": s,
        }
    if kind == "project":
        _, ents = _entities(state, "project")
        p = ents.get(raw_id)
        if not p:
            raise KeyError(doc_id)
        tasks = list_tasks(state, tz=tz, today=today, project_id=raw_id, include_done=False, limit=200)
        text = f"# Project {p.get('title')}\n\n" + "\n".join(
            f"- [{'x' if t['isDone'] else ' '}] {t['title']} ({t['id']})" for t in tasks
        )
        return {
            "id": doc_id,
            "title": p.get("title", ""),
            "text": text,
            "url": f"sp://project/{raw_id}",
            "metadata": project_summary(state, p),
        }
    if kind == "note":
        _, ents = _entities(state, "note")
        n = ents.get(raw_id)
        if not n:
            raise KeyError(doc_id)
        return {
            "id": doc_id,
            "title": (n.get("content") or "")[:80],
            "text": n.get("content", ""),
            "url": f"sp://note/{raw_id}",
            "metadata": note_summary(state, n, tz),
        }
    if kind == "tag":
        _, ents = _entities(state, "tag")
        t = ents.get(raw_id)
        if not t:
            raise KeyError(doc_id)
        tasks = list_tasks(state, tz=tz, today=today, tag_id=raw_id, limit=200)
        text = f"# Tag {t.get('title')}\n\n" + "\n".join(f"- {x['title']} ({x['id']})" for x in tasks)
        return {
            "id": doc_id,
            "title": t.get("title", ""),
            "text": text,
            "url": f"sp://tag/{raw_id}",
            "metadata": tag_summary(state, t),
        }
    raise KeyError(doc_id)
