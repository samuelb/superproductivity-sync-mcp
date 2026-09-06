"""Operation records as stored in ``recentOps`` (compact format + ``sv``).

Every entry mirrors what ``OperationLogEffects`` produces in the app and
``encodeOperation`` compacts::

    {id, a, o, e, d, ds, p: {actionPayload, entityChanges: []}, c, v, t, s, sv}

Only action types that this server emits are listed. The short codes come
from the app's ``action-type-codes.ts`` and are frozen on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ids import uuid7

OP_CREATE = "CRT"
OP_UPDATE = "UPD"
OP_DELETE = "DEL"
OP_MOVE = "MOV"


@dataclass(frozen=True)
class ActionSpec:
    code: str
    action_type: str
    op_type: str
    entity_type: str


ACTIONS: dict[str, ActionSpec] = {
    "addTask": ActionSpec("HA", "[Task Shared] addTask", OP_CREATE, "TASK"),
    "updateTask": ActionSpec("HU", "[Task Shared] updateTask", OP_UPDATE, "TASK"),
    "deleteTask": ActionSpec("HD", "[Task Shared] deleteTask", OP_DELETE, "TASK"),
    "moveToOtherProject": ActionSpec("HMP", "[Task Shared] moveToOtherProject", OP_UPDATE, "TASK"),
    "scheduleTaskWithTime": ActionSpec("HS", "[Task Shared] scheduleTaskWithTime", OP_UPDATE, "TASK"),
    "unscheduleTask": ActionSpec("HSX", "[Task Shared] unscheduleTask", OP_UPDATE, "TASK"),
    "addSubTask": ActionSpec("TA", "[Task] Add SubTask", OP_CREATE, "TASK"),
    "planTaskForDay": ActionSpec("LP", "[Planner] Plan Task for Day", OP_UPDATE, "PLANNER"),
    "addProject": ActionSpec("PA", "[Project] Add Project", OP_CREATE, "PROJECT"),
    "updateProject": ActionSpec("PU", "[Project] Update Project", OP_UPDATE, "PROJECT"),
    "archiveProject": ActionSpec("PX", "[Project] Archive Project", OP_UPDATE, "PROJECT"),
    "unarchiveProject": ActionSpec("PR", "[Project] Unarchive Project", OP_UPDATE, "PROJECT"),
    "addTag": ActionSpec("GA", "[Tag] Add Tag", OP_CREATE, "TAG"),
    "addNote": ActionSpec("NA", "[Note] Add Note", OP_CREATE, "NOTE"),
    "updateNote": ActionSpec("NU", "[Note] Update Note", OP_UPDATE, "NOTE"),
    "deleteNote": ActionSpec("ND", "[Note] Delete Note", OP_DELETE, "NOTE"),
}

# Payload keys per entity type (entity registry) used by the app's Checkpoint A.
PAYLOAD_KEYS = {
    "TASK": "task",
    "PROJECT": "project",
    "TAG": "tag",
    "NOTE": "note",
    "PLANNER": "planner",
}


@dataclass
class PendingOp:
    action: str
    entity_id: str
    action_payload: dict[str, Any]


def build_compact_op(
    pending: PendingOp,
    *,
    client_id: str,
    vector_clock: dict[str, int],
    timestamp_ms: int,
    schema_version: int,
    sync_version: int,
) -> dict[str, Any]:
    spec = ACTIONS[pending.action]
    op = {
        "id": uuid7(timestamp_ms),
        "a": spec.code,
        "o": spec.op_type,
        "e": spec.entity_type,
        "d": pending.entity_id,
        "ds": [pending.entity_id],
        "p": {"actionPayload": pending.action_payload, "entityChanges": []},
        "c": client_id,
        "v": dict(vector_clock),
        "t": timestamp_ms,
        "s": schema_version,
        "sv": sync_version,
    }
    validate_compact_op(op)
    return op


def validate_compact_op(op: dict[str, Any]) -> None:
    """Port of the app's ``validateOperationPayload`` hard failures (Checkpoint A)."""
    payload = op.get("p")
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a plain object")
    action_payload = payload.get("actionPayload")
    if not isinstance(action_payload, dict):
        raise ValueError("actionPayload must be a non-null object")
    if not isinstance(payload.get("entityChanges"), list):
        raise ValueError("entityChanges must be an array")
    entity_type, op_type = op["e"], op["o"]
    key = PAYLOAD_KEYS.get(entity_type)
    if op_type == OP_CREATE:
        entity = action_payload.get(key) if key else None
        if isinstance(entity, dict) and not isinstance(entity.get("id"), str):
            raise ValueError("CREATE entity missing valid 'id' field")
    elif op_type == OP_DELETE:
        ids = op.get("ds") or []
        if not op.get("d") and not (ids and all(isinstance(i, str) for i in ids)):
            raise ValueError("DELETE requires entityId/entityIds")
    if not isinstance(op.get("c"), str) or len(op["c"]) < 5:
        raise ValueError("clientId too short")
    if not isinstance(op.get("id"), str):
        raise ValueError("op id missing")
