import pytest

from superproductivity_sync_mcp import mutations as m
from superproductivity_sync_mcp.codec import PrefixFlags
from superproductivity_sync_mcp.ops import validate_compact_op
from superproductivity_sync_mcp.store import MutationContext, SyncFile

from .conftest import TZ, base_envelope


def berlin_ms(y, mo, d, h=0, mi=0) -> int:
    from datetime import datetime

    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp() * 1000)


def ctx_for(now_ms: int | None = None) -> MutationContext:
    now_ms = berlin_ms(2026, 9, 7, 18, 0) if now_ms is None else now_ms
    env = base_envelope()
    sf = SyncFile(raw="", flags=PrefixFlags(False, False, 2), data=env, rev="x", strong_etag='"x"')
    return MutationContext(sf, TZ, now_ms)


def test_today_respects_start_of_next_day():
    ctx = ctx_for()
    assert ctx.today == "2026-09-07"
    env = base_envelope()
    env["state"]["globalConfig"]["misc"]["startOfNextDayTime"] = "20:00"
    sf = SyncFile(raw="", flags=PrefixFlags(False, False, 2), data=env, rev="x", strong_etag='"x"')
    # 01:00 on the 8th with day starting at 20:00 => still the 7th
    ctx2 = MutationContext(sf, TZ, berlin_ms(2026, 9, 8, 1, 0))
    assert ctx2.today == "2026-09-07"


def test_schedule_with_time_payload():
    ctx = ctx_for()
    m.schedule_task(ctx, "t2", day="tomorrow", time_hhmm="09:30")
    op = ctx.ops[0]
    assert op.action == "scheduleTaskWithTime"
    assert op.action_payload["task"]["id"] == "t2"
    assert op.action_payload["dueWithTime"] == berlin_ms(2026, 9, 8, 9, 30)
    assert op.action_payload["isMoveToBacklog"] is False
    assert "dueDay" not in ctx.state["task"]["entities"]["t2"]


def test_schedule_day_uses_plan_task_for_day():
    ctx = ctx_for()
    m.schedule_task(ctx, "t2", day="today")
    assert ctx.ops[0].action == "planTaskForDay"
    assert ctx.ops[0].action_payload == {
        "task": ctx.ops[0].action_payload["task"],
        "day": "2026-09-07",
        "isAddToTop": False,
    }
    assert ctx.state["tag"]["entities"]["TODAY"]["taskIds"] == ["t1", "t2"]


def test_subtask_rules():
    ctx = ctx_for()
    with pytest.raises(m.MutationError):
        m.create_task(ctx, title="x", parent_task_id="s1")
    with pytest.raises(m.MutationError):
        m.update_task(ctx, "s1", tag_ids=["g1"])
    with pytest.raises(m.MutationError):
        m.schedule_task(ctx, "s1", day="today")
    sub = m.create_task(ctx, title="deep", parent_task_id="t1")
    assert sub["parentId"] == "t1" and sub["projectId"] == "INBOX_PROJECT"
    assert ctx.ops[-1].action == "addSubTask" and ctx.ops[-1].action_payload["parentId"] == "t1"


def test_validation_errors():
    ctx = ctx_for()
    with pytest.raises(m.NotFoundError):
        m.require_task(ctx.state, "nope")
    with pytest.raises(m.MutationError):
        m.create_task(ctx, title="   ")
    with pytest.raises(m.NotFoundError):
        m.create_task(ctx, title="x", tag_ids=["ghost"])
    with pytest.raises(m.MutationError):
        m.create_task(ctx, title="x", tag_ids=["TODAY"])
    with pytest.raises(m.MutationError):
        m.update_project(ctx, "INBOX_PROJECT", is_archived=True)


def test_delete_task_payload_has_subtasks_and_passes_checkpoint_a():
    ctx = ctx_for()
    out = m.delete_task(ctx, "t1")
    assert out == {"deleted": "t1", "deletedSubTasks": ["s1"]}
    op = ctx.ops[0]
    assert op.action == "deleteTask" and op.action_payload["task"]["subTasks"][0]["id"] == "s1"
    from superproductivity_sync_mcp.ops import build_compact_op

    compact = build_compact_op(
        op,
        client_id="M_test01",
        vector_clock={"M_test01": 1},
        timestamp_ms=1,
        schema_version=4,
        sync_version=8,
    )
    validate_compact_op(compact)
    assert compact["o"] == "DEL" and compact["d"] == "t1"


def test_project_and_tag_and_note_flow():
    ctx = ctx_for()
    p = m.create_project(ctx, title="New", is_enable_backlog=True)
    assert p["isEnableBacklog"] and p["theme"]["primary"] == "#29a1aa" and p["taskIds"] == []
    m.update_project(ctx, p["id"], title="Renamed", is_archived=True)
    assert [o.action for o in ctx.ops] == ["addProject", "updateProject", "archiveProject"]
    with pytest.raises(m.MutationError):
        m.create_task(ctx, title="x", project_id=p["id"])  # archived
    t = m.create_tag(ctx, title="urgent!", color="#ff0000")
    assert t["color"] == "#ff0000" and ctx.state["menuTree"]["tagTree"][-1] == {
        "k": "t",
        "id": t["id"],
    }
    with pytest.raises(m.MutationError):
        m.create_tag(ctx, title="URGENT!")
    n = m.create_note(ctx, content="remember", project_id="p1")
    assert ctx.state["project"]["entities"]["p1"]["noteIds"][0] == n["id"]
    m.update_note(ctx, n["id"], content="changed")
    m.delete_note(ctx, n["id"])
    assert ctx.ops[-1].action_payload == {
        "id": n["id"],
        "projectId": "p1",
        "isPinnedToToday": False,
    }


def test_emitted_payload_is_frozen_before_reducer_runs():
    """Ops must carry the pre-reducer entity, like the app's dispatched action."""
    ctx = ctx_for()
    before = dict(ctx.state["task"]["entities"]["t2"])
    m.schedule_task(ctx, "t2", day="tomorrow")
    payload_task = ctx.ops[0].action_payload["task"]
    assert payload_task is not ctx.state["task"]["entities"]["t2"]
    assert payload_task["dueDay"] == before["dueDay"] == "2026-09-09"
    assert ctx.state["task"]["entities"]["t2"]["dueDay"] == "2026-09-08"

    ctx = ctx_for()
    m.schedule_task(ctx, "t1", day="today", time_hhmm="10:00")
    assert ctx.ops[0].action_payload["task"]["dueDay"] == "2026-09-07"
    assert "dueDay" not in ctx.state["task"]["entities"]["t1"]

    ctx = ctx_for()
    m.move_task_to_project(ctx, "t2", "INBOX_PROJECT")
    assert ctx.ops[0].action_payload["task"]["projectId"] == "p1"
    assert ctx.state["task"]["entities"]["t2"]["projectId"] == "INBOX_PROJECT"
