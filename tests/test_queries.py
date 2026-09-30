from datetime import datetime

import pytest

from superproductivity_sync_mcp import queries as q
from superproductivity_sync_mcp.timeutil import InputError

from .conftest import TZ, base_state

TODAY = "2026-09-07"


def ms(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp() * 1000)


def state_with_timed_tasks() -> dict:
    """base_state plus: t4 due today with a time, t5 due yesterday with a time, t6 unscheduled."""
    st = base_state()
    for tid, extra in {
        "t4": {"dueWithTime": ms(2026, 9, 7, 15, 30)},
        "t5": {"dueWithTime": ms(2026, 9, 6, 9, 0)},
        "t6": {},
    }.items():
        st["task"]["ids"].append(tid)
        st["task"]["entities"][tid] = {
            "id": tid,
            "title": f"Task {tid}",
            "subTaskIds": [],
            "timeSpentOnDay": {},
            "timeSpent": 0,
            "timeEstimate": 1800000,
            "isDone": False,
            "tagIds": [],
            "created": 1,
            "attachments": [],
            "projectId": "p1",
            **extra,
        }
        st["project"]["entities"]["p1"]["taskIds"].append(tid)
    return st


def ids(tasks: list[dict]) -> list[str]:
    return [t["id"] for t in tasks]


def test_effective_due_day():
    assert q.effective_due_day({"dueDay": "2026-09-09"}, TZ) == "2026-09-09"
    assert q.effective_due_day({"dueWithTime": ms(2026, 9, 7, 23, 59)}, TZ) == "2026-09-07"
    assert q.effective_due_day({"dueWithTime": 0}, TZ) is None
    assert q.effective_due_day({}, TZ) is None


def test_list_tasks_due_filters_include_timed_tasks():
    st = state_with_timed_tasks()
    kw = dict(tz=TZ, today=TODAY)
    assert ids(q.list_tasks(st, due="today", **kw)) == ["t1", "t4"]
    assert ids(q.list_tasks(st, due="2026-09-07", **kw)) == ["t1", "t4"]
    assert ids(q.list_tasks(st, due="overdue", **kw)) == ["t5"]
    assert ids(q.list_tasks(st, due="unscheduled", **kw)) == ["t6"]
    assert ids(q.list_tasks(st, due="unscheduled", include_done=True, **kw)) == ["t3", "t6"]
    assert ids(q.list_tasks(st, due="2026-09-09", **kw)) == ["t2"]


def test_list_tasks_due_accepts_every_day_form_and_rejects_typos():
    """Regression: 'tomorrow', '+N' and typos silently matched nothing."""
    st = state_with_timed_tasks()
    st["task"]["entities"]["t6"]["dueDay"] = "2026-09-08"
    kw = dict(tz=TZ, today=TODAY)
    assert ids(q.list_tasks(st, due="tomorrow", **kw)) == ["t6"]
    assert ids(q.list_tasks(st, due="+2", **kw)) == ["t2"]
    assert ids(q.list_tasks(st, due=" Today ", **kw)) == ["t1", "t4"]
    with pytest.raises(InputError, match="'overdue' or 'unscheduled'"):
        q.list_tasks(st, due="next week", **kw)


def test_list_tasks_today_tag_is_virtual():
    """Regression: tasks never carry TODAY in tagIds, so tag_id='TODAY' listed nothing."""
    st = state_with_timed_tasks()
    kw = dict(tz=TZ, today=TODAY)
    assert ids(q.list_tasks(st, tag_id="TODAY", **kw)) == ids(q.list_tasks(st, due="today", **kw)) == ["t1", "t4"]
    assert "t4" in q.fetch(st, "tag:TODAY", tz=TZ, today=TODAY)["text"]


def test_list_tasks_other_filters_and_ordering():
    st = state_with_timed_tasks()
    kw = dict(tz=TZ, today=TODAY)
    # project order comes from project.taskIds, not the global ids list
    assert ids(q.list_tasks(st, project_id="p1", include_done=True, **kw)) == ["t2", "t3", "t4", "t5", "t6"]
    assert ids(q.list_tasks(st, tag_id="g1", **kw)) == ["t1"]
    assert ids(q.list_tasks(st, query="MILK", **kw)) == ["t2"]
    assert ids(q.list_tasks(st, limit=2, **kw)) == ["t1", "t2"]
    # sub tasks are hidden by default and nested when requested
    assert "s1" not in ids(q.list_tasks(st, **kw))
    with_subs = q.list_tasks(st, include_subtasks=True, **kw)
    assert "s1" in ids(with_subs)
    assert [s["id"] for s in next(t for t in with_subs if t["id"] == "t1")["subTasks"]] == ["s1"]
    assert "t3" not in ids(q.list_tasks(st, **kw))  # t3 is done in the fixture
    assert "t3" in ids(q.list_tasks(st, include_done=True, **kw))


def test_task_summary_fields():
    st = state_with_timed_tasks()
    t5 = q.task_summary(st, st["task"]["entities"]["t5"], tz=TZ, today=TODAY)
    assert t5["isOverdue"] is True and t5["dueDay"] is None
    assert t5["dueWithTime"] == "2026-09-06T09:00+02:00"
    assert t5["project"] == "Home" and t5["timeEstimateMinutes"] == 30
    t1 = q.task_summary(st, st["task"]["entities"]["t1"], tz=TZ, today=TODAY, include_subtasks=True)
    assert t1["tags"] == [{"id": "g1", "title": "work"}]
    assert t1["isOverdue"] is False and t1["subTasks"][0]["id"] == "s1"
    st["task"]["entities"]["t5"]["isDone"] = True
    assert q.task_summary(st, st["task"]["entities"]["t5"], tz=TZ, today=TODAY)["isOverdue"] is False


def test_today_view_matches_list_tasks():
    st = state_with_timed_tasks()
    st["task"]["entities"]["t3"]["dueDay"] = TODAY  # t3 is done in the fixture
    tv = q.today_view(st, tz=TZ, today=TODAY, now_ms=ms(2026, 9, 7, 12))
    # TODAY tag order first, then tasks due today by other means
    assert ids(tv["todo"]) == ["t1", "t4"]
    assert ids(tv["done"]) == ["t3"]
    assert ids(tv["overdue"]) == ["t5"]
    assert tv["estimateRemainingMinutes"] == 60 + 30
    assert set(ids(q.list_tasks(st, due="today", tz=TZ, today=TODAY, include_done=True))) == {"t1", "t3", "t4"}


def test_planner_view():
    st = state_with_timed_tasks()
    st["task"]["entities"]["t6"]["dueWithTime"] = ms(2026, 9, 8, 8, 0)
    pv = q.planner_view(st, tz=TZ, today=TODAY, days=3)
    assert list(pv["days"]) == ["2026-09-07", "2026-09-08", "2026-09-09"]
    assert ids(pv["days"]["2026-09-07"]) == ["t1", "t4"]
    assert ids(pv["days"]["2026-09-08"]) == ["t6"]
    assert ids(pv["days"]["2026-09-09"]) == ["t2"]


def test_overview_and_lists():
    st = state_with_timed_tasks()
    ov = q.overview(st, tz=TZ, today=TODAY, now_ms=ms(2026, 9, 7, 12))
    assert ov["openTasks"] == 5 and ov["todayTodo"] == 2 and ov["overdue"] == 1 and ov["notes"] == 1
    assert [p["id"] for p in ov["projects"]] == ["INBOX_PROJECT", "p1"]
    assert [t["id"] for t in ov["tags"]] == ["g1"]
    st["project"]["entities"]["p1"]["isArchived"] = True
    assert [p["id"] for p in q.list_projects(st, include_archived=False)] == ["INBOX_PROJECT"]
    assert [p["id"] for p in q.list_projects(st, include_archived=True)] == ["INBOX_PROJECT", "p1"]
    tags = q.list_tags(st)
    assert tags[0]["id"] == "TODAY" and tags[0]["isVirtual"] is True
    assert q.list_notes(st, project_id="p1", tz=TZ)[0]["project"] == "Home"
    assert q.list_notes(st, project_id="INBOX_PROJECT", tz=TZ) == []
    p1 = q.project_summary(st, st["project"]["entities"]["p1"])
    assert p1["taskCount"] == 5 and p1["openTaskCount"] == 4 and p1["noteCount"] == 1


def test_search_and_fetch():
    st = base_state()
    res = q.search(st, "home", tz=TZ, today=TODAY)
    assert [r["id"] for r in res] == ["project:p1"]
    res = q.search(st, "hello", tz=TZ, today=TODAY)
    assert res[0]["id"] == "note:n1" and res[0]["url"] == "sp://note/n1"
    doc = q.fetch(st, "task:t1", tz=TZ, today=TODAY)
    assert doc["title"] == "Write report" and doc["metadata"]["subTasks"][0]["id"] == "s1"
    assert q.fetch(st, "t1", tz=TZ, today=TODAY)["id"] == "t1"  # bare id defaults to task
    proj = q.fetch(st, "project:p1", tz=TZ, today=TODAY)
    assert "- [ ] Buy milk (t2)" in proj["text"]
    tag = q.fetch(st, "tag:g1", tz=TZ, today=TODAY)
    assert "Write report (t1)" in tag["text"]
    assert q.fetch(st, "note:n1", tz=TZ, today=TODAY)["text"] == "hello"
    for bad in ("task:zzz", "project:zzz", "note:zzz", "tag:zzz", "thing:1"):
        try:
            q.fetch(st, bad, tz=TZ, today=TODAY)
        except KeyError:
            continue
        raise AssertionError(bad)
