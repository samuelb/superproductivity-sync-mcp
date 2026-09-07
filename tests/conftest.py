from __future__ import annotations

import copy
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from superproductivity_sync_mcp import codec
from superproductivity_sync_mcp.store import ClientIdentity, SyncStore
from superproductivity_sync_mcp.webdav import NextcloudDav

from .fake_dav import FakeDav, make_app

TZ = ZoneInfo("Europe/Berlin")
BASE = "/remote.php/dav/files/alice/sp"


def base_state() -> dict:
    """A minimal but realistic Super Productivity state snapshot."""
    return {
        "task": {
            "ids": ["t1", "t2", "t3", "s1"],
            "entities": {
                "t1": {
                    "id": "t1",
                    "title": "Write report",
                    "subTaskIds": ["s1"],
                    "timeSpentOnDay": {},
                    "timeSpent": 0,
                    "timeEstimate": 3600000,
                    "isDone": False,
                    "tagIds": ["g1"],
                    "created": 1700000000000,
                    "attachments": [],
                    "projectId": "INBOX_PROJECT",
                    "dueDay": "2026-09-07",
                },
                "s1": {
                    "id": "s1",
                    "title": "Outline",
                    "subTaskIds": [],
                    "timeSpentOnDay": {"2026-09-06": 600000},
                    "timeSpent": 600000,
                    "timeEstimate": 1800000,
                    "isDone": False,
                    "tagIds": [],
                    "created": 1700000000000,
                    "attachments": [],
                    "projectId": "INBOX_PROJECT",
                    "parentId": "t1",
                },
                "t2": {
                    "id": "t2",
                    "title": "Buy milk",
                    "subTaskIds": [],
                    "timeSpentOnDay": {},
                    "timeSpent": 0,
                    "timeEstimate": 0,
                    "isDone": False,
                    "tagIds": [],
                    "created": 1700000000000,
                    "attachments": [],
                    "projectId": "p1",
                    "dueDay": "2026-09-09",
                },
                "t3": {
                    "id": "t3",
                    "title": "Old done",
                    "subTaskIds": [],
                    "timeSpentOnDay": {},
                    "timeSpent": 0,
                    "timeEstimate": 0,
                    "isDone": True,
                    "doneOn": 1700000000000,
                    "tagIds": [],
                    "created": 1700000000000,
                    "attachments": [],
                    "projectId": "p1",
                },
            },
            "currentTaskId": None,
            "selectedTaskId": None,
            "lastCurrentTaskId": None,
            "isDataLoaded": True,
        },
        "project": {
            "ids": ["INBOX_PROJECT", "p1"],
            "entities": {
                "INBOX_PROJECT": {
                    "id": "INBOX_PROJECT",
                    "title": "Inbox",
                    "taskIds": ["t1"],
                    "backlogTaskIds": [],
                    "noteIds": [],
                    "isEnableBacklog": False,
                    "isArchived": False,
                    "advancedCfg": {},
                    "theme": {},
                },
                "p1": {
                    "id": "p1",
                    "title": "Home",
                    "taskIds": ["t2", "t3"],
                    "backlogTaskIds": [],
                    "noteIds": ["n1"],
                    "isEnableBacklog": True,
                    "isArchived": False,
                    "advancedCfg": {},
                    "theme": {},
                },
            },
        },
        "tag": {
            "ids": ["TODAY", "g1"],
            "entities": {
                "TODAY": {
                    "id": "TODAY",
                    "title": "Today",
                    "taskIds": ["t1"],
                    "advancedCfg": {},
                    "theme": {},
                    "created": 1,
                },
                "g1": {
                    "id": "g1",
                    "title": "work",
                    "taskIds": ["t1"],
                    "color": "#123456",
                    "advancedCfg": {},
                    "theme": {},
                    "created": 1,
                },
            },
        },
        "note": {
            "ids": ["n1"],
            "entities": {
                "n1": {
                    "id": "n1",
                    "projectId": "p1",
                    "isPinnedToToday": False,
                    "content": "hello",
                    "created": 1,
                    "modified": 1,
                }
            },
            "todayOrder": [],
        },
        "planner": {"days": {"2026-09-09": ["t2"]}, "addPlannedTasksDialogLastShown": None},
        "menuTree": {"projectTree": [{"k": "p", "id": "p1"}], "tagTree": [{"k": "t", "id": "g1"}]},
        "section": {
            "ids": ["sec1"],
            "entities": {
                "sec1": {
                    "id": "sec1",
                    "contextId": "p1",
                    "contextType": "PROJECT",
                    "title": "S",
                    "taskIds": ["t2"],
                }
            },
        },
        "globalConfig": {
            "misc": {"startOfNextDay": 0, "startOfNextDayTime": "00:00"},
            "tasks": {"defaultProjectId": "INBOX_PROJECT"},
            "sync": {
                "isEnabled": True,
                "syncInterval": 60000,
                "isManualSyncOnly": False,
                "syncProvider": "Nextcloud",
            },
        },
        "reminders": [],
    }


def base_envelope(state: dict | None = None) -> dict:
    return {
        "version": 2,
        "syncVersion": 7,
        "schemaVersion": 4,
        "vectorClock": {"E_abc123": 40, "A_def456": 12},
        "lastModified": 1757200000000,
        "clientId": "E_abc123",
        "state": state or base_state(),
        "archiveYoung": {
            "task": {"ids": [], "entities": {}},
            "timeTracking": {"tag": {}, "project": {}},
            "lastTimeTrackingFlush": 0,
        },
        "archiveOld": {
            "task": {"ids": [], "entities": {}},
            "timeTracking": {"tag": {}, "project": {}},
            "lastTimeTrackingFlush": 0,
        },
        "recentOps": [
            {
                "id": "0191-old",
                "a": "HU",
                "o": "UPD",
                "e": "TASK",
                "d": "t2",
                "ds": ["t2"],
                "p": {
                    "actionPayload": {"task": {"id": "t2", "changes": {"title": "Buy milk"}}},
                    "entityChanges": [],
                },
                "c": "E_abc123",
                "v": {"E_abc123": 40, "A_def456": 12},
                "t": 1757199000000,
                "s": 4,
                "sv": 7,
            }
        ],
        "oldestOpSyncVersion": 7,
    }


@pytest.fixture
def fake_dav() -> FakeDav:
    dav = FakeDav()
    flags = codec.PrefixFlags(False, False, 2)
    dav.files[f"{BASE}/sync-data.json"] = codec.encode_sync_file(base_envelope(), flags, None).encode()
    return dav


@pytest.fixture
def store(fake_dav: FakeDav, tmp_path) -> SyncStore:
    transport = httpx.ASGITransport(app=make_app(fake_dav))
    dav = NextcloudDav(
        "https://cloud.example.com", "alice", "alice", "pw", "/sp", transport=transport, retry_backoff=0.0
    )
    identity = ClientIdentity.load(tmp_path, "M_test01")
    return SyncStore(dav, identity, password=None, allow_plaintext=False, tz=TZ, cache_ttl=0.0)


def decode_remote(fake_dav: FakeDav) -> dict:
    raw = fake_dav.files[f"{BASE}/sync-data.json"].decode()
    return codec.decode_sync_file(raw, None)[1]


__all__ = ["base_state", "base_envelope", "decode_remote", "copy", "json", "TZ", "BASE"]
