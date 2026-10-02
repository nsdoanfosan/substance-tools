# Shared workstation admission for the native Painter pipeline

The existing Substance Tools operators still own FBX/source-map export, Painter
CREATE/UPDATE, mesh-map baking, project saves, and transactional Blender texture
application. Workstation Queue admits these stages and records their native
receipts. It does not replace them with a second project-building workflow or a
second native request queue.

For an automated stage, enqueue and begin a **heavy** owner phase with the same
task document, resource `painter` and `exclusive=["editor"]`. Wait until its
actual admission succeeds before calling the additive API returned by
`get_painter_transfer_api(1)`:

- `dispatch_painter_request(workstation_phase_id=..., action="UPDATE")` uses the
  existing CREATE/UPDATE operator. UPDATE remains UPDATE and reuses the existing
  project handler, which preserves an unrelated dirty open project.
- `dispatch_painter_bake(workstation_phase_id=..., texture_sets=...)` uses the
  existing selected/all bake path.
- `dispatch_painter_export_apply(workstation_phase_id=...,
  workstation_apply_phase_id=...)` uses the existing export/apply modal flow.

Each phase binds one immutable native request. Bake, export, and a retry with a
new native request ID require distinct phases. Dispatch receipts have
`dispatch_only: true`; they are not proof of completed baking or applied textures.
Existing manual buttons, whose hidden phase ID is empty, retain their native
behavior.

For export/apply, first begin the parent Painter phase. Then enqueue a separate
heavy Blender phase for the **same owner and task**, resource `blender` (or
`blender:<original blend path>`) and exclusive `editor`, and provide both phase
IDs. Enqueuing the child after the parent begins prevents the child from taking
FIFO priority over its own predecessor. The existing modal keeps the exact
Painter SUCCESS result while the Blender phase waits. It checks admission at
most once every 45 seconds and does not alter the Blender scene, canonical
textures or material bindings while waiting. Once admitted, transactional apply
runs under the separate Blender reservation. Changing the open Blender file
while waiting cancels apply and preserves the export result.
The generic `blender` resource is accepted; a path-specific Blender resource
must match the original `.blend` before child admission or native binding.
Current ownership is checked again before scene checkpoints and the canonical
transaction, so validation cannot turn a revoked phase into later mutations.

Native request JSON carries a public `workstation_phase` binding with
`phase_id`, `ticket_id`, `provider`, `session_id`, `resource`, `pipeline`,
`request_id`, and `target`. No owner token is serialized. The execution ticket
prevents a late callback from releasing a later recovered owner execution. The
binding is treated as opaque metadata and checked against the exact native ID
and target before opening/reimporting a project, configuring/baking, or exporting.
Delayed save/layer callbacks check that same binding before changing Painter;
revocation does not silently reschedule the old callback. A coordinated CREATE
exception records exact FAILED/recovery evidence and is not retried on each poll.
Denied admission keeps the request, its ID and the native processing marker
unchanged. Existing UPDATE payloads are not changed to CREATE.

Every updated producer publishes the native single-slot JSON with a short
cross-process OS file lock, rechecks the slot, and uses an atomic replacement.
An unfinished request is never replaced. An identical dispatch preserves even
its saved SUCCESS/FAILED receipt. A different request can reuse a proven terminal
slot. The small `.publish.lock` file uses a first-byte OS lock, not a queue or
lease: the OS releases it when the publishing process exits or crashes. Older external producers
that do not use this publisher cannot participate in its lock.

Painter sends a database-only heartbeat at most every 45 seconds. A final saved
native SUCCESS receipt releases its exact phase; a metadata-match no-op does not
pretend a new save completed. Its receipt explicitly reports
`native_completion_kind: verified_noop` and
`workstation_completion_requires_owner: true`. The owner verifies that the saved
state meets the original goal, then explicitly finishes that phase with the
no-op evidence; it must not leave a heavy reservation active merely because the
native receipt says SUCCESS. A saved callback records `native_completion_kind:
saved` and clears the owner-reconciliation flag. Painter export releases only its parent phase.
Blender releases the apply phase only after transactional canonical application
succeeds. A failure, timeout or uncertain launch retains a recovery reservation
and the native request/receipt. Verify that the native work stopped or completed
before owner recovery; cancel that old bound phase and enqueue a distinct phase
for a new native attempt.

The addon loads `pipeline_bridge.py` from
`~/Documents/GitHub/workstation-queue`, or `WORKSTATION_QUEUE_REPO` when set.
Cached queue modules from a different checkout are rejected. Install/update the
plugin while Painter is idle; this integration does not force a running project
or editor to restart. `WORKSTATION_QUEUE_TEST_REPO` can point the isolated contract
test to a checkout containing the bridge; its database and native targets are
temporary and it never starts Blender or Painter.
