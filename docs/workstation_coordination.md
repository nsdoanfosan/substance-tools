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
**and an exact native Blend save** succeed. Applying textures in memory does not
produce `Native saved receipt`. A failure, timeout or uncertain launch retains a recovery reservation
and the native request/receipt. Verify that the native work stopped or completed
before owner recovery; cancel that old bound phase and enqueue a distinct phase
for a new native attempt.

## Blender apply: persistence before completion

Coordinated apply first writes a small, atomic journal under
`~/.substance-tools/workstation-apply/<phase hash>.json`, protected by the existing
cross-process publication lock. It contains the original parent phase, exact
native request/target/execution ticket, export receipt, and application result;
no owner token or production-file copy is written. The original goal and resume
checkpoint remain in Workstation Queue and are never replaced.

- `applying`: written **before** native application. A crash/failure here is
  uncertain and requires owner reconciliation, never automatic re-export/apply.
- `applied_awaiting_save`: canonical files/material application succeeded. A
  per-request nonce is placed in the affected scene, but the Blender phase and
  its scope remain held. Operator `FINISHED` means the modal ended, not that the
  coordinated phase completed.
- `saved`: a paired Blender `save_pre`/`save_post` for the exact original target
  saw the same scene nonce and a changed, nonempty on-disk file. Evidence includes
  path, file size, nanosecond modification/creation stamps and event time. Only
  this evidence can call `complete_handoff` for that exact execution binding.
- `completed`: queue accepted the saved receipt. Retries are no-ops. If the queue
  was unavailable, a timer/repeated dispatch can retry **receipt delivery only**,
  provided the saved file fingerprint still matches. It never runs native work.

The integration never calls a save operator, checks out a file, overwrites
unrelated dirty data, changes user preferences, or adds a backup copy. The owner
must first follow the existing save authorization and Perforce recovery-point
rules, then explicitly save the original Blend through the normal workflow.
Blender's save handlers observe that save; they do not grant permission to save.
Manual export/apply without phase IDs retains its previous behavior.

`save_post_fail` drops the in-flight evidence and retains recovery ownership.
Canceling a save dialog before any native event leaves the result awaiting save.
Save As to another path, a changed request/ticket, loading another file, or a late
callback cannot release the original reservation. A disk/journal error retains
the checkpoint and never manufactures a saved receipt. The handler contract is
documented in [Blender's Application Handlers API](https://docs.blender.org/api/current/bpy.app.handlers.html).

After reload/retry, an existing journal is inspected before any Painter dispatch
or scene validation. If the unsaved changes were lost on reload (scene marker
absent), **stop for owner recovery**: saving the old file cannot prove that apply
survived. Keep the original journal/export evidence, inspect the saved file and
original goal, and use the existing explicit recovery/cancel/new-phase procedure
only after the old native operation is known to have stopped. Do not delete the
journal or mint another request simply to retry. A saved receipt whose file was
subsequently replaced also requires owner reconciliation. No background process
reopens a Blend, repeats apply, or weakens queue admission.

The 45-second timer renews only a matching, loaded awaiting-save execution, or
retries already verified receipt delivery. Unloading the target leaves its
reservation to the normal queue recovery rules. Receipt tests execute the actual
operator modal/core transaction against fake native state; an optional second
run uses the real bridge and disposable SQLite databases. They never import a
production addon, operate a running app, or read/write the production queue DB.

The addon loads `pipeline_bridge.py` from
`~/Documents/GitHub/workstation-queue`, or `WORKSTATION_QUEUE_REPO` when set.
Cached queue modules from a different checkout are rejected. Install/update the
plugin while Painter is idle; this integration does not force a running project
or editor to restart. `WORKSTATION_QUEUE_TEST_REPO` can point the isolated contract
test to a checkout containing the bridge; its database and native targets are
temporary and it never starts Blender or Painter.
