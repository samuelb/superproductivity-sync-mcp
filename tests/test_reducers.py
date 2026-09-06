from superproductivity_sync_mcp import reducers as r
from superproductivity_sync_mcp.mutations import default_task

from .conftest import base_state

TODAY = "2026-09-07"


def test_add_task_top_of_project_and_tag_and_today():
    st = base_state()
    task = {
        **default_task(1),
        "id": "n1",
        "title": "New",
        "projectId": "INBOX_PROJECT",
        "tagIds": ["g1"],
        "dueDay": TODAY,
    }
    r.add_task(
        st,
        task,
        is_add_to_bottom=False,
        is_add_to_backlog=False,
        today=TODAY,
        default_task=default_task(1),
    )
    assert st["task"]["entities"]["n1"]["projectId"] == "INBOX_PROJECT"
    assert st["project"]["entities"]["INBOX_PROJECT"]["taskIds"] == ["n1", "t1"]
    assert st["tag"]["entities"]["g1"]["taskIds"] == ["n1", "t1"]
    assert st["tag"]["entities"]["TODAY"]["taskIds"] == ["n1", "t1"]
    assert "2026-09-07" not in st["planner"]["days"]


def test_add_task_future_day_goes_to_planner_not_today():
    st = base_state()
    task = {
        **default_task(1),
        "id": "n2",
        "title": "Later",
        "projectId": "p1",
        "tagIds": [],
        "dueDay": "2026-09-09",
    }
    r.add_task(
        st,
        task,
        is_add_to_bottom=True,
        is_add_to_backlog=False,
        today=TODAY,
        default_task=default_task(1),
    )
    assert st["planner"]["days"]["2026-09-09"] == ["t2", "n2"]
    assert "n2" not in st["tag"]["entities"]["TODAY"]["taskIds"]
    assert st["project"]["entities"]["p1"]["taskIds"] == ["t2", "t3", "n2"]


def test_add_sub_task_inherits_and_recalcs_parent():
    st = base_state()
    sub = {
        **default_task(1),
        "id": "s2",
        "title": "Sub",
        "projectId": "",
        "tagIds": ["g1"],
        "timeEstimate": 900000,
    }
    r.add_sub_task(st, sub, "t1")
    ent = st["task"]["entities"]["s2"]
    assert ent["parentId"] == "t1" and ent["tagIds"] == [] and ent["projectId"] == "INBOX_PROJECT"
    parent = st["task"]["entities"]["t1"]
    assert parent["subTaskIds"] == ["s1", "s2"]
    # estimate = sum of open sub tasks' remaining time: s1 (1800000-600000) + s2 900000
    assert parent["timeEstimate"] == 1200000 + 900000
    assert parent["timeSpentOnDay"] == {"2026-09-06": 600000} and parent["timeSpent"] == 600000


def test_update_task_done_sets_done_on_and_keeps_today_order():
    st = base_state()
    r.update_task(st, "t1", {"isDone": True, "doneOn": 5}, today=TODAY, now_ms=99)
    t = st["task"]["entities"]["t1"]
    assert t["isDone"] is True and t["doneOn"] == 5 and t["modified"] == 99
    assert st["tag"]["entities"]["TODAY"]["taskIds"] == ["t1"]
    r.update_task(st, "t1", {"isDone": False}, today=TODAY, now_ms=100)
    assert "doneOn" not in st["task"]["entities"]["t1"]


def test_update_task_tags():
    st = base_state()
    r.update_task(st, "t2", {"tagIds": ["g1"]}, today=TODAY, now_ms=1)
    assert st["tag"]["entities"]["g1"]["taskIds"] == ["t2", "t1"]
    r.update_task(st, "t1", {"tagIds": []}, today=TODAY, now_ms=1)
    assert st["tag"]["entities"]["g1"]["taskIds"] == ["t2"]


def test_update_subtask_done_recalcs_parent_estimate():
    st = base_state()
    r.update_task(st, "s1", {"isDone": True}, today=TODAY, now_ms=1)
    assert st["task"]["entities"]["t1"]["timeEstimate"] == 0


def test_plan_task_for_day_moves_between_today_and_planner():
    st = base_state()
    t1 = st["task"]["entities"]["t1"]
    r.plan_task_for_day(st, t1, "2026-09-09", is_add_to_top=False, today=TODAY)
    assert st["task"]["entities"]["t1"]["dueDay"] == "2026-09-09"
    assert st["tag"]["entities"]["TODAY"]["taskIds"] == []
    assert st["planner"]["days"]["2026-09-09"] == ["t2", "t1"]
    # sub task s1 collapsed under parent on that day (was not there anyway)
    r.plan_task_for_day(st, st["task"]["entities"]["t2"], TODAY, is_add_to_top=True, today=TODAY)
    assert st["tag"]["entities"]["TODAY"]["taskIds"] == ["t2"]
    assert st["planner"]["days"]["2026-09-09"] == ["t1"]
    assert st["task"]["entities"]["t2"]["dueDay"] == TODAY


def test_schedule_with_time_and_unschedule():
    st = base_state()
    r.schedule_task_with_time(st, "t2", 1757260800000, is_scheduled_for_today=True)
    t2 = st["task"]["entities"]["t2"]
    assert t2["dueWithTime"] == 1757260800000 and "dueDay" not in t2
    assert st["tag"]["entities"]["TODAY"]["taskIds"] == ["t2", "t1"]
    assert st["planner"]["days"]["2026-09-09"] == []
    r.unschedule_task(st, "t2")
    assert "dueWithTime" not in st["task"]["entities"]["t2"]
    assert st["tag"]["entities"]["TODAY"]["taskIds"] == ["t1"]


def test_delete_task_cascades():
    st = base_state()
    t1 = st["task"]["entities"]["t1"]
    tws = {**t1, "subTasks": [st["task"]["entities"]["s1"]]}
    r.delete_task(st, tws)
    assert "t1" not in st["task"]["entities"] and "s1" not in st["task"]["entities"]
    assert st["task"]["ids"] == ["t2", "t3"]
    assert st["project"]["entities"]["INBOX_PROJECT"]["taskIds"] == []
    assert st["tag"]["entities"]["g1"]["taskIds"] == [] and st["tag"]["entities"]["TODAY"]["taskIds"] == []


def test_delete_subtask_copies_times_to_parent_when_last():
    st = base_state()
    s1 = st["task"]["entities"]["s1"]
    r.delete_task(st, {**s1, "subTasks": []})
    parent = st["task"]["entities"]["t1"]
    assert parent["subTaskIds"] == []
    assert parent["timeSpentOnDay"] == {"2026-09-06": 600000} and parent["timeEstimate"] == 1800000


def test_move_to_other_project_cleans_sections_and_lists():
    st = base_state()
    t2 = {**st["task"]["entities"]["t2"], "subTasks": []}
    r.move_to_other_project(st, t2, "INBOX_PROJECT")
    assert st["project"]["entities"]["p1"]["taskIds"] == ["t3"]
    assert st["project"]["entities"]["INBOX_PROJECT"]["taskIds"] == ["t1", "t2"]
    assert st["section"]["entities"]["sec1"]["taskIds"] == []
    assert st["task"]["entities"]["t2"]["projectId"] == "INBOX_PROJECT"


def test_tag_add_updates_menu_tree_once():
    st = base_state()
    r.add_tag(st, {"id": "g2", "title": "x", "taskIds": []})
    r.add_tag(st, {"id": "g2", "title": "x", "taskIds": []})
    assert st["menuTree"]["tagTree"] == [{"k": "t", "id": "g1"}, {"k": "t", "id": "g2"}]


def test_notes():
    st = base_state()
    r.add_note(
        st,
        {
            "id": "n2",
            "projectId": "p1",
            "isPinnedToToday": True,
            "content": "c",
            "created": 1,
            "modified": 1,
        },
    )
    assert st["note"]["ids"] == ["n2", "n1"] and st["note"]["todayOrder"] == ["n2"]
    assert st["project"]["entities"]["p1"]["noteIds"] == ["n2", "n1"]
    r.delete_note(st, "n2", "p1")
    assert st["note"]["ids"] == ["n1"] and st["note"]["todayOrder"] == []
    assert st["project"]["entities"]["p1"]["noteIds"] == ["n1"]
