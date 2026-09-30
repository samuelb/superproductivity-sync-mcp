"""Pure state transitions mirroring Super Productivity's NgRx (meta-)reducers.

The sync file carries a full state snapshot next to the operation log. When we
append an operation we must also apply the very same change to the snapshot,
otherwise a client that bootstraps from the snapshot would disagree with one
that replayed the operation. Every function here is a faithful port of the
corresponding reducer path in the app (see docs/adr/0003 for the mapping) and
mutates ``state`` in place. ``UNSET`` deletes a key — the JSON equivalent of
NgRx writing ``undefined``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

TODAY_TAG_ID = "TODAY"
IN_PROGRESS_TAG_ID = "KANBAN_IN_PROGRESS"
INBOX_PROJECT_ID = "INBOX_PROJECT"
INFINITY = 1 << 30


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover
        return "UNSET"


UNSET = _Unset()


class StateError(Exception):
    pass


# --- generic entity-state helpers -------------------------------------------


def unique(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def slice_(state: dict[str, Any], key: str) -> dict[str, Any]:
    es = state.get(key)
    if not isinstance(es, dict) or not isinstance(es.get("entities"), dict):
        raise StateError(f"State slice '{key}' is missing or malformed")
    es.setdefault("ids", [])
    return es


def get_entity(es: dict[str, Any], entity_id: str | None) -> dict[str, Any] | None:
    if not entity_id:
        return None
    ent = es["entities"].get(entity_id)
    return ent if isinstance(ent, dict) and ent.get("id") == entity_id else None


def add_one(es: dict[str, Any], ent: dict[str, Any]) -> bool:
    eid = ent["id"]
    if eid in es["entities"]:
        return False
    es["ids"].append(eid)
    es["entities"][eid] = ent
    return True


def update_one(es: dict[str, Any], entity_id: str, changes: dict[str, Any]) -> None:
    ent = get_entity(es, entity_id)
    if ent is None:
        return
    for k, v in changes.items():
        if v is UNSET:
            ent.pop(k, None)
        else:
            ent[k] = v


def remove_many(es: dict[str, Any], ids: list[str]) -> None:
    id_set = set(ids)
    for i in ids:
        es["entities"].pop(i, None)
    es["ids"] = [i for i in es["ids"] if i not in id_set]


def calc_total_time_spent(time_spent_on_day: dict[str, Any] | None) -> int:
    total = 0
    for v in (time_spent_on_day or {}).values():
        if v:
            total += int(v)
    return total


def sum_sub_task_time_left(sub_tasks: list[dict[str, Any]]) -> int:
    total = 0
    for st in sub_tasks:
        if not st.get("isDone"):
            total += max(0, int(st.get("timeEstimate") or 0) - int(st.get("timeSpent") or 0))
    return total


# --- task time helpers (task.reducer.util.ts) --------------------------------


def recalc_time_spent_for_parent(tasks: dict[str, Any], parent_id: str) -> None:
    parent = get_entity(tasks, parent_id)
    if parent is None:
        return
    per_day: dict[str, int] = {}
    for sid in parent.get("subTaskIds", []):
        st = get_entity(tasks, sid)
        if not st:
            continue
        for day, v in (st.get("timeSpentOnDay") or {}).items():
            if v:
                per_day[day] = per_day.get(day, 0) + int(v)
    update_one(tasks, parent_id, {"timeSpentOnDay": per_day, "timeSpent": calc_total_time_spent(per_day)})


def recalc_time_estimate_for_parent(
    tasks: dict[str, Any], parent_id: str, upd: tuple[str, dict[str, Any]] | None = None
) -> None:
    parent = get_entity(tasks, parent_id)
    if parent is None:
        return
    subs: list[dict[str, Any]] = []
    for sid in parent.get("subTaskIds", []):
        st = get_entity(tasks, sid)
        if not st:
            continue
        if upd and upd[0] == sid:
            merged = dict(st)
            for k, v in upd[1].items():
                if v is UNSET:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            subs.append(merged)
        else:
            subs.append(st)
    update_one(tasks, parent_id, {"timeEstimate": sum_sub_task_time_left(subs)})


def recalc_times_for_parent(tasks: dict[str, Any], parent_id: str) -> None:
    recalc_time_estimate_for_parent(tasks, parent_id)
    recalc_time_spent_for_parent(tasks, parent_id)


def remove_task_from_parent_side_effects(
    tasks: dict[str, Any], task: dict[str, Any], copy_times_after_last: bool = False
) -> None:
    parent = get_entity(tasks, task.get("parentId"))
    if parent is None:
        return
    parent_id: str = parent["id"]
    was_last = len(parent.get("subTaskIds", [])) == 1
    changes: dict[str, Any] = {"subTaskIds": [i for i in parent.get("subTaskIds", []) if i != task["id"]]}
    if was_last and copy_times_after_last:
        changes["timeSpentOnDay"] = task.get("timeSpentOnDay", {})
        changes["timeEstimate"] = task.get("timeEstimate", 0)
    update_one(tasks, parent_id, changes)
    if not (was_last and copy_times_after_last):
        recalc_time_spent_for_parent(tasks, parent_id)
        recalc_time_estimate_for_parent(tasks, parent_id)


def delete_task_helper(tasks: dict[str, Any], task: dict[str, Any]) -> None:
    remove_many(tasks, [task["id"]])
    current = tasks.get("currentTaskId")
    if current == task["id"]:
        current = None
    if task.get("parentId"):
        remove_task_from_parent_side_effects(tasks, task, True)
    payload_sub_ids = list(task.get("subTaskIds") or [])
    state_sub_ids = [i for i in tasks["ids"] if (tasks["entities"].get(i) or {}).get("parentId") == task["id"]]
    all_sub_ids = unique(payload_sub_ids + state_sub_ids)
    if all_sub_ids:
        remove_many(tasks, all_sub_ids)
        if current in all_sub_ids:
            current = None
    tasks["currentTaskId"] = current


# --- list / project / tag / planner helpers (task-shared-helpers.ts) ----------


def add_task_to_list(task_ids: list[str], task_id: str, is_add_to_bottom: bool) -> list[str]:
    if task_id in task_ids:
        return list(task_ids)
    return [*task_ids, task_id] if is_add_to_bottom else [task_id, *task_ids]


def remove_from_list(task_ids: list[str], to_remove: list[str]) -> list[str]:
    rs = set(to_remove)
    return [i for i in task_ids if i not in rs]


def collect_task_and_sub_task_ids(
    state: dict[str, Any], parent_ids: list[str], payload_sub_ids: list[str] | None = None
) -> list[str]:
    tasks = slice_(state, "task")
    parent_set = set(parent_ids)
    out = unique([*parent_ids, *(payload_sub_ids or [])])
    for pid in parent_ids:
        p = get_entity(tasks, pid)
        for sid in (p or {}).get("subTaskIds", []):
            if sid not in out:
                out.append(sid)
    for tid in tasks["ids"]:
        t = tasks["entities"].get(tid) or {}
        if t.get("parentId") in parent_set and tid not in out:
            out.append(tid)
    return out


def remove_tasks_from_all_tags(state: dict[str, Any], task_ids: list[str]) -> None:
    if not task_ids:
        return
    tags = slice_(state, "tag")
    ts = set(task_ids)
    for tag_id in tags["ids"]:
        tag = tags["entities"].get(tag_id)
        if tag and any(i in ts for i in tag.get("taskIds", [])):
            tag["taskIds"] = [i for i in tag["taskIds"] if i not in ts]


def _planner_days(state: dict[str, Any]) -> dict[str, list[str]] | None:
    planner = state.get("planner")
    if not isinstance(planner, dict) or not isinstance(planner.get("days"), dict):
        return None
    return planner["days"]


def remove_tasks_from_planner_days(state: dict[str, Any], task_ids: list[str]) -> None:
    days = _planner_days(state)
    if days is None or not task_ids:
        return
    ts = set(task_ids)
    for day, ids in list(days.items()):
        filtered = [i for i in ids if i not in ts]
        if len(filtered) != len(ids):
            days[day] = filtered


def remove_tasks_from_single_planner_day(state: dict[str, Any], task_ids: list[str], day: str) -> None:
    days = _planner_days(state)
    if days is None or not task_ids or day not in days:
        return
    days[day] = remove_from_list(days[day], task_ids)


def add_task_to_planner_day(state: dict[str, Any], task_id: str, day: str, position: int = 0) -> None:
    planner = state.get("planner")
    if not isinstance(planner, dict):
        planner = {}
        state["planner"] = planner
    days = planner.get("days")
    if not isinstance(days, dict):
        days = {}
        planner["days"] = days
    for d, ids in list(days.items()):
        if task_id in ids:
            days[d] = [i for i in ids if i != task_id]
    target = days.get(day) or []
    pos = min(position, len(target))
    days[day] = unique([*target[:pos], task_id, *target[pos:]])


def filter_out_today_tag(tag_ids: list[str]) -> list[str]:
    return [i for i in tag_ids if i != TODAY_TAG_ID]


def _sections_remove(state: dict[str, Any], task_ids: list[str], context: tuple[str, str] | None = None) -> None:
    """Drop ``task_ids`` from every section, or only from those of ``(contextType, contextId)``."""
    sections = state.get("section")
    if not isinstance(sections, dict) or not isinstance(sections.get("entities"), dict):
        return
    ts = set(task_ids)
    for sid in sections.get("ids", []):
        s = sections["entities"].get(sid)
        if not s:
            continue
        if context is not None and (s.get("contextType"), s.get("contextId")) != context:
            continue
        ids = s.get("taskIds") or []
        filtered = [i for i in ids if i not in ts]
        if len(filtered) != len(ids):
            s["taskIds"] = filtered


def _today_task_ids(state: dict[str, Any]) -> list[str]:
    tags = state.get("tag")
    if not isinstance(tags, dict) or not isinstance(tags.get("entities"), dict):
        return []
    return list((get_entity(tags, TODAY_TAG_ID) or {}).get("taskIds") or [])


def prune_sections_left_today[F: Callable[..., None]](fn: F) -> F:
    """Port of ``sectionSharedMetaReducer``'s post-reducer step (section-shared.reducer.ts).

    The meta-reducer wraps every action: ids that left ``TODAY_TAG.taskIds``,
    plus their sub-tasks, are stripped from the TODAY-tag sections. Wrap every
    reducer here that writes the TODAY tag's ``taskIds``.
    """

    @functools.wraps(fn)
    def wrapper(state: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        before = _today_task_ids(state)
        fn(state, *args, **kwargs)
        # The meta-reducer is a no-op unless every slice it reads exists.
        if not before or not all(isinstance(state.get(k), dict) for k in ("task", "tag", "project", "section")):
            return
        after = set(_today_task_ids(state))
        removed = [i for i in before if i not in after]
        if removed:
            affected = collect_task_and_sub_task_ids(state, removed)
            _sections_remove(state, affected, context=("TAG", TODAY_TAG_ID))

    return wrapper  # type: ignore[return-value]


# --- TASK: add / update / delete (task-shared-crud.reducer.ts) --------------


@prune_sections_left_today
def add_task(
    state: dict[str, Any],
    task: dict[str, Any],
    *,
    is_add_to_bottom: bool,
    is_add_to_backlog: bool,
    today: str,
    default_task: dict[str, Any],
) -> None:
    tasks = slice_(state, "task")
    projects = slice_(state, "project")
    tags = slice_(state, "tag")
    should_add_to_today = task.get("dueDay") == today
    task_tag_ids = filter_out_today_tag(list(task.get("tagIds") or []))
    new_task = {
        **default_task,
        **task,
        "tagIds": task_tag_ids,
        "timeSpent": calc_total_time_spent(task.get("timeSpentOnDay") or {}),
        "projectId": task.get("projectId") or "",
    }
    add_one(tasks, new_task)

    project = get_entity(projects, task.get("projectId"))
    if project is not None and not task.get("parentId"):
        target = "backlogTaskIds" if is_add_to_backlog and project.get("isEnableBacklog") else "taskIds"
        project[target] = add_task_to_list(project.get(target) or [], task["id"], is_add_to_bottom)

    tag_ids_to_update = [*new_task["tagIds"], *([TODAY_TAG_ID] if should_add_to_today else [])]
    for tag_id in tag_ids_to_update:
        tag = get_entity(tags, tag_id)
        if tag is not None:
            tag["taskIds"] = add_task_to_list(tag.get("taskIds") or [], task["id"], is_add_to_bottom)

    due_day = task.get("dueDay")
    if due_day and due_day != today:
        planner = state.get("planner")
        if not isinstance(planner, dict):
            planner = {}
            state["planner"] = planner
        days = planner.setdefault("days", {})
        existing = days.get(due_day) or []
        days[due_day] = unique([*existing, task["id"]] if is_add_to_bottom else [task["id"], *existing])


def add_sub_task(state: dict[str, Any], task: dict[str, Any], parent_id: str) -> None:
    """Port of ``on(addSubTask)`` in task.reducer.ts."""
    tasks = slice_(state, "task")
    parent = get_entity(tasks, parent_id)
    if parent is None:
        raise StateError(f"Parent task {parent_id} not found")
    new_task = dict(task)
    parent_subs = parent.get("subTaskIds") or []
    if not parent_subs and not (task.get("timeSpentOnDay") or {}):
        new_task["timeSpentOnDay"] = dict(parent.get("timeSpentOnDay") or {})
        new_task["timeSpent"] = calc_total_time_spent(parent.get("timeSpentOnDay") or {})
    if not parent_subs and not task.get("timeEstimate"):
        new_task["timeEstimate"] = parent.get("timeEstimate", 0)
    new_task["parentId"] = parent_id
    new_task["tagIds"] = []
    new_task["projectId"] = parent.get("projectId")
    add_one(tasks, new_task)
    if task["id"] not in parent_subs:
        parent["subTaskIds"] = [*parent_subs, task["id"]]
    if tasks.get("currentTaskId") == parent_id:
        tasks["currentTaskId"] = task["id"]
    recalc_times_for_parent(tasks, parent_id)


def _handle_tag_updates(state: dict[str, Any], task_id: str, old: list[str], new: list[str]) -> None:
    tags = slice_(state, "tag")
    old_set, new_set = set(old), set(new)
    for tag_id in old:
        if tag_id in new_set or tag_id == TODAY_TAG_ID:
            continue
        tag = get_entity(tags, tag_id)
        if tag is not None:
            tag["taskIds"] = [i for i in tag.get("taskIds") or [] if i != task_id]
    for tag_id in new:
        if tag_id in old_set or tag_id == TODAY_TAG_ID:
            continue
        tag = get_entity(tags, tag_id)
        if tag is not None:
            tag["taskIds"] = unique([task_id, *(tag.get("taskIds") or [])])


@prune_sections_left_today
def update_task(state: dict[str, Any], task_id: str, changes: dict[str, Any], *, today: str, now_ms: int) -> None:
    """Port of ``handleUpdateTask`` for the change keys this server emits
    (title, notes, isDone/doneOn, timeEstimate, tagIds)."""
    tasks = slice_(state, "task")
    current = get_entity(tasks, task_id)
    if current is None:
        return
    changes = dict(changes)

    # removeInProgressTagOnCompletion
    if changes.get("isDone") is True:
        tag_ids = changes["tagIds"] if isinstance(changes.get("tagIds"), list) else current.get("tagIds", [])
        if IN_PROGRESS_TAG_ID in tag_ids:
            changes["tagIds"] = [t for t in tag_ids if t != IN_PROGRESS_TAG_ID]

    # projectId changes travel via moveToOtherProject; mirror the guard anyway.
    if "projectId" in changes:
        pid = changes["projectId"]
        projects = slice_(state, "project")
        if (
            not isinstance(pid, str)
            or current.get("parentId")
            or not (pid == "" or get_entity(projects, pid) is not None)
        ):
            changes.pop("projectId")

    if isinstance(changes.get("tagIds"), list):
        _handle_tag_updates(state, task_id, list(current.get("tagIds") or []), changes["tagIds"])

    # updateTimeSpentForTask (not emitted by us, but keep parity)
    if changes.get("timeSpentOnDay"):
        tsod = changes["timeSpentOnDay"]
        update_one(tasks, task_id, {"timeSpentOnDay": tsod, "timeSpent": calc_total_time_spent(tsod)})
        if current.get("parentId"):
            recalc_time_spent_for_parent(tasks, current["parentId"])

    # updateTimeEstimateForTask
    new_estimate = changes.get("timeEstimate")
    if isinstance(new_estimate, (int, float)) or "isDone" in changes:
        if isinstance(new_estimate, (int, float)):
            update_one(tasks, task_id, {"timeEstimate": new_estimate})
        if current.get("parentId"):
            recalc_time_estimate_for_parent(tasks, current["parentId"], (task_id, changes))

    # updateDoneOnForTask
    is_to_done = changes.get("isDone") is True
    is_to_undone = changes.get("isDone") is False
    if is_to_done:
        done_on = changes["doneOn"] if isinstance(changes.get("doneOn"), (int, float)) else now_ms
        update_one(tasks, task_id, {"doneOn": done_on})
    elif is_to_undone:
        update_one(tasks, task_id, {"doneOn": UNSET})

    update_one(tasks, task_id, {**changes, "modified": now_ms})

    tags = slice_(state, "tag")
    if is_to_done and not current.get("parentId") and current.get("dueDay") == today:
        today_tag = get_entity(tags, TODAY_TAG_ID)
        if today_tag is not None and task_id not in today_tag.get("taskIds", []):
            today_tag["taskIds"] = [*today_tag.get("taskIds", []), task_id]

    after = get_entity(tasks, task_id) or {}
    has_due_day_change = "dueDay" in changes
    if has_due_day_change:
        new_due_day = changes["dueDay"]
    elif is_to_done and after.get("dueDay") != current.get("dueDay"):
        new_due_day = after.get("dueDay")
    else:
        new_due_day = UNSET
    if new_due_day is not UNSET and new_due_day != current.get("dueDay"):
        old_due_day = current.get("dueDay")
        remove_tasks_from_planner_days(state, [task_id])
        if new_due_day and new_due_day != today and not is_to_done:
            add_task_to_planner_day(state, task_id, new_due_day, INFINITY)
        today_tag = get_entity(tags, TODAY_TAG_ID)
        if today_tag is not None:
            if old_due_day == today and new_due_day != today:
                today_tag["taskIds"] = [i for i in today_tag.get("taskIds", []) if i != task_id]
                if TODAY_TAG_ID in (after.get("tagIds") or []):
                    update_one(tasks, task_id, {"tagIds": filter_out_today_tag(after["tagIds"])})
            elif old_due_day != today and new_due_day == today:
                if task_id not in today_tag.get("taskIds", []):
                    today_tag["taskIds"] = [*today_tag.get("taskIds", []), task_id]


def _calendar_dismissals(tasks_list: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out = []
    for t in tasks_list:
        if t.get("issueType") == "ICAL" and t.get("issueProviderId") and t.get("issueId"):
            out.append((str(t["issueProviderId"]), str(t["issueId"])))
    return out


def _apply_dismissals(tasks: dict[str, Any], dismissals: list[tuple[str, str]]) -> None:
    if not dismissals:
        return
    by_provider = dict(tasks.get("dismissedCalendarAutoImportEventIdsByProvider") or {})
    changed = False
    for provider_id, issue_id in dismissals:
        current = list(by_provider.get(provider_id) or [])
        if issue_id not in current:
            by_provider[provider_id] = sorted([*current, issue_id])
            changed = True
    if changed:
        tasks["dismissedCalendarAutoImportEventIdsByProvider"] = by_provider


@prune_sections_left_today
def delete_task(state: dict[str, Any], task_with_subtasks: dict[str, Any]) -> None:
    """Port of ``handleDeleteTask`` plus the section meta-reducer's pruning."""
    task = task_with_subtasks
    tasks = slice_(state, "task")
    affected = collect_task_and_sub_task_ids(state, [task["id"]])
    _sections_remove(state, affected)
    delete_task_helper(tasks, task)
    _apply_dismissals(tasks, _calendar_dismissals([task, *(task.get("subTasks") or [])]))
    projects = slice_(state, "project")
    project = get_entity(projects, task.get("projectId"))
    if project is not None:
        project["taskIds"] = remove_from_list(project.get("taskIds") or [], [task["id"]])
        project["backlogTaskIds"] = remove_from_list(project.get("backlogTaskIds") or [], [task["id"]])
    remove_tasks_from_all_tags(state, [task["id"], *(task.get("subTaskIds") or [])])


# --- TASK: scheduling (task-shared-scheduling + planner reducers) ------------


@prune_sections_left_today
def unschedule_task(state: dict[str, Any], task_id: str) -> None:
    """Port of ``handleUnScheduleTask`` (without isLeaveInToday) + planner.reducer's handler."""
    # planner.reducer: on(unscheduleTask) removes the task from every day.
    remove_tasks_from_planner_days(state, [task_id])
    tasks = slice_(state, "task")
    if get_entity(tasks, task_id) is None:
        return
    update_one(tasks, task_id, {"dueDay": UNSET, "dueWithTime": UNSET, "remindAt": UNSET})
    tags = slice_(state, "tag")
    today_tag = get_entity(tags, TODAY_TAG_ID)
    if today_tag is not None and task_id in today_tag.get("taskIds", []):
        today_tag["taskIds"] = [i for i in today_tag["taskIds"] if i != task_id]


@prune_sections_left_today
def schedule_task_with_time(
    state: dict[str, Any], task_id: str, due_with_time: int, *, is_scheduled_for_today: bool
) -> None:
    """Port of ``handleScheduleTaskWithTime`` + planner.reducer's handler (no remindAt)."""
    # planner.reducer: on(scheduleTaskWithTime) removes the task from every day,
    # independently of the meta-reducer's early returns below.
    remove_tasks_from_planner_days(state, [task_id])
    tasks = slice_(state, "task")
    current = get_entity(tasks, task_id)
    if current is None:
        return
    tags = slice_(state, "tag")
    today_tag = get_entity(tags, TODAY_TAG_ID)
    in_today = bool(today_tag and task_id in today_tag.get("taskIds", []))
    if (
        current.get("dueWithTime") == due_with_time
        # `currentTask.remindAt === remindAt` with remindAt undefined: a stored null does not match.
        and "remindAt" not in current
        and is_scheduled_for_today == in_today
    ):
        return
    update_one(tasks, task_id, {"dueWithTime": due_with_time, "dueDay": UNSET, "remindAt": UNSET})
    if today_tag is not None and is_scheduled_for_today != in_today:
        today_tag["taskIds"] = (
            unique([task_id, *today_tag.get("taskIds", [])])
            if is_scheduled_for_today
            else [i for i in today_tag.get("taskIds", []) if i != task_id]
        )


@prune_sections_left_today
def plan_task_for_day(
    state: dict[str, Any], task: dict[str, Any], day: str, *, is_add_to_top: bool, today: str
) -> None:
    """Port of ``handlePlanTaskForDay`` (planner-shared) + task.reducer's handler."""
    tasks = slice_(state, "task")
    tags = slice_(state, "tag")
    current = get_entity(tasks, task["id"])
    if current is None:
        return
    today_tag = get_entity(tags, TODAY_TAG_ID)
    current_tag_ids = list(current.get("tagIds") or [])
    has_invalid_today = TODAY_TAG_ID in current_tag_ids
    if today_tag is not None:
        if day == today:
            others = [i for i in today_tag.get("taskIds", []) if i != task["id"]]
            today_tag["taskIds"] = unique([task["id"], *others] if is_add_to_top else [*others, task["id"]])
            if has_invalid_today:
                update_one(tasks, task["id"], {"tagIds": filter_out_today_tag(current_tag_ids)})
        elif task["id"] in today_tag.get("taskIds", []):
            today_tag["taskIds"] = [i for i in today_tag["taskIds"] if i != task["id"]]
            if has_invalid_today:
                update_one(tasks, task["id"], {"tagIds": filter_out_today_tag(current_tag_ids)})
    remove_tasks_from_planner_days(state, [task["id"]])
    if day != today:
        add_task_to_planner_day(state, task["id"], day, 0 if is_add_to_top else INFINITY)
        sub_ids = list(task.get("subTaskIds") or [])
        if sub_ids:
            remove_tasks_from_single_planner_day(state, sub_ids, day)
    # task.reducer: dueDay = day, dueWithTime/remindAt cleared
    update_one(tasks, task["id"], {"dueDay": day, "dueWithTime": UNSET, "remindAt": UNSET})


# --- TASK: move to other project (project-shared + section reducers) ---------


def move_to_other_project(state: dict[str, Any], task: dict[str, Any], target_project_id: str) -> None:
    tasks = slice_(state, "task")
    projects = slice_(state, "project")
    canonical = get_entity(tasks, task["id"])
    old_project_id = (canonical or {}).get("projectId") or task.get("projectId")
    if old_project_id and old_project_id != target_project_id:
        _sections_remove(state, collect_task_and_sub_task_ids(state, [task["id"]]), ("PROJECT", old_project_id))
    sub_ids = unique([*((canonical or {}).get("subTaskIds") or []), *(task.get("subTaskIds") or [])])
    all_ids = unique([task["id"], *sub_ids])
    current_project = get_entity(projects, old_project_id)
    if current_project is not None:
        current_project["taskIds"] = remove_from_list(current_project.get("taskIds") or [], all_ids)
        current_project["backlogTaskIds"] = remove_from_list(current_project.get("backlogTaskIds") or [], all_ids)
    target = get_entity(projects, target_project_id)
    if target is not None:
        target["taskIds"] = unique([*(target.get("taskIds") or []), task["id"]])
    for tid in all_ids:
        update_one(tasks, tid, {"projectId": target_project_id})


# --- PROJECT / TAG / NOTE ---------------------------------------------------


def add_project(state: dict[str, Any], project: dict[str, Any]) -> None:
    add_one(slice_(state, "project"), project)


def update_project(state: dict[str, Any], project_id: str, changes: dict[str, Any]) -> None:
    update_one(slice_(state, "project"), project_id, changes)


def archive_project(state: dict[str, Any], project_id: str) -> None:
    if project_id == INBOX_PROJECT_ID:
        return
    update_one(slice_(state, "project"), project_id, {"isArchived": True})


def unarchive_project(state: dict[str, Any], project_id: str) -> None:
    update_one(slice_(state, "project"), project_id, {"isArchived": False, "isDone": False, "doneOn": None})


def _tree_contains(tree: list[dict[str, Any]], item_id: str, kind: str) -> bool:
    for node in tree:
        if node.get("k") == kind and node.get("id") == item_id:
            return True
        if node.get("k") == "f" and _tree_contains(node.get("children") or [], item_id, kind):
            return True
    return False


def add_tag(state: dict[str, Any], tag: dict[str, Any]) -> None:
    add_one(slice_(state, "tag"), tag)
    menu_tree = state.get("menuTree")
    if isinstance(menu_tree, dict):
        tag_tree = menu_tree.get("tagTree")
        if not isinstance(tag_tree, list):
            tag_tree = []
            menu_tree["tagTree"] = tag_tree
        if not _tree_contains(tag_tree, tag["id"], "t"):
            tag_tree.append({"k": "t", "id": tag["id"]})


def add_note(state: dict[str, Any], note: dict[str, Any]) -> None:
    notes = slice_(state, "note")
    if note["id"] in notes["entities"]:
        return
    notes["entities"][note["id"]] = note
    notes["ids"] = [note["id"], *notes["ids"]]
    today_order = notes.setdefault("todayOrder", [])
    if note.get("isPinnedToToday"):
        notes["todayOrder"] = [note["id"], *today_order]
    project = get_entity(slice_(state, "project"), note.get("projectId"))
    if project is not None:
        project["noteIds"] = [note["id"], *(project.get("noteIds") or [])]


def update_note(state: dict[str, Any], note_id: str, changes: dict[str, Any]) -> None:
    notes = slice_(state, "note")
    if get_entity(notes, note_id) is None:
        return
    if "isPinnedToToday" in changes:
        order = [i for i in notes.get("todayOrder") or [] if i != note_id]
        notes["todayOrder"] = [note_id, *order] if changes["isPinnedToToday"] else order
    update_one(notes, note_id, changes)


def delete_note(state: dict[str, Any], note_id: str, project_id: str | None) -> None:
    notes = slice_(state, "note")
    remove_many(notes, [note_id])
    notes["todayOrder"] = [i for i in notes.get("todayOrder") or [] if i != note_id]
    project = get_entity(slice_(state, "project"), project_id)
    if project is not None:
        project["noteIds"] = [i for i in (project.get("noteIds") or []) if i != note_id]
