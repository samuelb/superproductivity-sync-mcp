# ADR-0003: Mirror the app's reducers when updating the snapshot

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `src/superproductivity_sync_mcp/reducers.py`, `src/superproductivity_sync_mcp/mutations.py`, app files
  `root-store/meta/task-shared-meta-reducers/*.ts`, `features/tasks/store/task.reducer*.ts`,
  `features/planner/store/planner.reducer.ts`, `features/menu-tree/store/menu-tree.reducer.ts`

## Context

A client that bootstraps (or detects a gap) installs the snapshot and marks all
ops in the file as applied. A client that is up to date replays only the new
ops. Both must end in the same state, so the snapshot must reflect exactly what
the reducers do with the op.

## Decision

Emit only these actions and port their reducer paths 1:1 (`UNSET` deletes a
key, the JSON equivalent of NgRx writing `undefined`):

| Emitted action (code) | Mirrored reducer paths |
| --- | --- |
| `[Task Shared] addTask` (HA) | `handleAddTask` (project list, tag lists, TODAY, planner day) |
| `[Task] Add SubTask` (TA) | `on(addSubTask)` incl. time inheritance and parent recalculation |
| `[Task Shared] updateTask` (HU) | `handleUpdateTask` for title/notes/isDone/timeEstimate/tagIds (doneOn, parent estimate, TODAY and planner consistency, in-progress tag removal) |
| `[Task Shared] deleteTask` (HD) | section pruning, `deleteTaskHelper`, project lists, all tags, calendar dismissals |
| `[Planner] Plan Task for Day` (LP) | `handlePlanTaskForDay` + task reducer (dueDay set, dueWithTime/remindAt cleared) |
| `[Task Shared] scheduleTaskWithTime` (HS) | `handleScheduleTaskWithTime` + planner removal |
| `[Task Shared] unscheduleTask` (HSX) | `handleUnScheduleTask` + planner removal |
| `[Task Shared] moveToOtherProject` (HMP) | `handleMoveToOtherProject` + section cleanup |
| `[Project] Add/Update/Archive/Unarchive Project` (PA/PU/PX/PR) | project reducer |
| `[Tag] Add/Update Tag` (GA/GU) | tag reducer + menu-tree `addTag` |
| `[Note] Add/Update/Delete Note` (NA/NU/ND) | note reducer + project `noteIds` |

Field clearing never travels as `undefined` in a payload (JSON drops it); we
use dedicated actions (`unscheduleTask`, `planTaskForDay`) like the app does.

## Consequences

- Adding a tool that changes state requires: the action code from
  `action-type-codes.ts`, the payload shape from the action creator, and the
  reducer port plus a test. `tests/test_reducers.py` pins the behaviour.
- "today" is computed in `SP_TIMEZONE` with the user's start-of-next-day
  setting, mirroring `getDbDateStr`/`getStartOfNextDayDiffMs`.
- Reducer behaviour must be re-verified against the app source when Super
  Productivity changes; this server tracks 18.21.x.
