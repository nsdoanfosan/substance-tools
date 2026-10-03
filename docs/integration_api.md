# Blender Pipeline Integration APIs

Painter editing can use 1024px Texture Sets while exporting a separate production size. `dispatch_painter_export_apply(..., export_resolution=4096)` and the existing operator's hidden `export_resolution` argument publish that size in the original native request. Zero defaults to Blender's configured baking/output resolution. The Painter plugin sets export `sizeLog2` explicitly and leaves working Texture Set sizes unchanged. Already-published requests without the field preserve their existing defaults. Native export still requires its own admitted heavy phase and the original request/receipt identity.

The workflow uses direct, provider-owned, versioned APIs. There is no central
runtime registry: each consumer imports the enabled provider, asks for an exact
major version, and stores that provider's receipt.

## Ownership

| Capability | Owner | Service ID |
|---|---|---|
| Target formula, public Quad Remesher settings, native-result provenance | `quad-remesher-workflow-addon` | `quad-remesher.workflow` |
| OptCuts Hard Surface profile, start/poll, UV validation | `UVgami` | `uvgami.unwrap` |
| Painter High/Low collections, material isolation, Painter checkpoints | `substance-tools` | `substance.painter_transfer` |
| Empty export root, `Export` links, Send2UE naming/settings | `ue-unique-export-names-addon` | `unreal-handoff.painter-low-export` |

`pipeline_contract.json` is this repository's consumer declaration, not a
shared hub. It pins the provider IDs, major versions, and callable names that
Substance Tools accepts.

## Direct services

Quad Remesher exposes version 1:

```python
from quad_remesher_workflow_addon import api as qr_api

qr = qr_api.get_workflow_api(1)
request = qr["capture_pre_remesh"](source, scene=scene)
# The user runs the native Remesh It action once.
topology = qr["validate_result"](
    source, retopo, request, scene=scene, asset_base=asset_base
)
```

Substance Tools accepts any retopology whose owner already issued the required
topology receipt. It does not call Quad Remesher from this public API:

```python
from substance_tools import api as painter_api

painter = painter_api.get_painter_transfer_api(1)
pair = painter["adopt_retopology_pair"](
    source,
    retopo,
    qr_ready_state,
    topology,
    scene=scene,
)
```

UVgami exposes version 1 from its enabled package's sibling `api` module. The
package name can vary for Blender Extensions, so Substance Tools derives it
from `bpy.types.UVGAMI_OT_start.__module__`. UVgami itself owns
`preflight_unwrap`, `begin_unwrap`, `poll_unwrap`, `inspect_uv_map`, its live
manager, and temporary profile changes. Its deterministic profile enables
OptCuts Hard Surface and restores the user's visible UVgami settings after the
request is captured.

UE Unique exposes version 2:

```python
from ue_unique_export_names_addon import api as unreal_api

handoff = unreal_api.get_painter_low_export_api(2)
unit = handoff["ensure_painter_low_export_unit"](
    low, asset_base, scene=scene
)
```

The Low stays `<asset_base>_low` for Painter By Mesh Name. A safe standalone
static Low becomes the child of exact top-level Empty `<asset_base>` for Unreal;
the owner links the unit into `Export`, preserves world transform, selects
Send2UE `Child meshes`, and disables immediate-parent naming. Rigged,
shape-keyed, or incompatible existing hierarchies are preserved or rejected by
that owner rather than being rewritten by Substance Tools.

## Receipts and versions

Every provider receipt is JSON-serializable and includes `service_id`,
`api_version`, `operation`, and `status`. Consumers reject an unsupported major
or wrong service identity before mutation. Additive fields may keep the major;
renamed fields, changed side effects, or changed failure semantics require a
new major.

The workflow remains staged:

1. Archive/analyze and configure Quad Remesher.
2. Pause for one native **Remesh It** action.
3. Validate the result and create Painter High/Low collections/materials. The
   hidden `st.adopt_retopology_pair` adapter stops here at `LOW_CREATED`.
4. Ask UVgami to unwrap; poll its asynchronous receipt.
5. Bake available Color, Extra, and Normal source maps.
6. Create/update Painter, then export/apply textures.
7. Complete visual QA and hand the verified Low unit to Unreal.

No API turns the sequence into an unattended one-button operation. UI operators
are adapters; the Codex skill chooses checkpoints and performs visual QA.

## Test policy

- Pure contract tests run without Blender.
- Blender mutation tests use `--factory-startup` and
  `default_set=False, persistent=False`.
- Tests never save user preferences.
- Cross-add-on smoke tests validate service/version rejection, transactional
  rollback, `<base>_low` plus Empty `<base>`, and actual Send2UE asset naming.

## Native multi-set bake lifecycle

The Painter startup plugin tracks `bake_selected_textures_async` separately from
the native bake. Returning from that call records `dispatched`, not `started`.
The returned StopSource stays owned by the execution; the matching
`BakingProcessAboutToStart.stop_source` acknowledges its native start. This does
not establish that garbage collection caused a missing start signal.

The additive `bake_lifecycle` field in matching request receipts records start,
progress, end, and save milestones. Updates are bounded to state changes and
10% progress increments. Each observer checks plugin generation, active request
object/ID, target SPP, immutable phase metadata, and current admission. SDK
progress/end events carry no job identity, so an additional foreign start after
acknowledgment makes completion ambiguous and blocks automatic saving. This
guard lasts through native end, queued save, and durable success publication.
If a foreign start arrives inside an already invoked native save, its written
file is retained but SUCCESS/completion is withheld with ownership held.

Timeouts diagnose `native_start_unacknowledged` after 30 seconds,
`native_progress_stalled` after 600 seconds without changed progress,
`native_end_unacknowledged` after 90 seconds at progress 1 without an end event,
and `native_save_unacknowledged` after 120 seconds awaiting save. These are
uncertain states, not native failure/success. They retain the exact request and
ownership; they never replay the bake, click UI, cancel a job, save again, or
release workstation scopes. A late matching native signal may resume that same
execution. `bake_lifecycle_status()` exposes the current milestone and ages for
inspection; Qt diagnostics cannot run while Painter's main thread is blocked.

Native successful end defers saving to a subsequent Qt turn. SUCCESS requires
the requested project to save, report clean/idle, and publish an owned durable
success receipt. Before-save and before-completion guards recheck that the native
job is still unambiguous. Exhausting busy-save waits or failing to verify busy
state holds the multi-set execution without invoking native save. Save or receipt
uncertainty keeps the processing slot and
requires owner recovery. Deferral avoids nested save in the event handler; it
does not prove the cause of an existing app hang. Single-set JavaScript baking
keeps its existing behavior in this change.

After independently verifying the external operation has stopped, the owner
may call `acknowledge_native_bake_stopped(request_id,
external_stop_confirmed=True)`. This requires a matching active execution and
recovery diagnosis, persists FAILED in its owned durable copies, and only then
clears that execution's observers/processing slot. Each write must succeed, and
terminal rereads must match immutable ID/action/SPP/phase plus the exact failure
reason. The current execution is rechecked immediately before clearing it;
replacement or write failure keeps it held and does not notify phase failure.
A timeout, missing event, or
`project.is_busy() == False` alone is not stop evidence. This API neither deletes
the pending ticket nor releases workstation scopes nor initiates another attempt;
any new attempt needs a separately admitted request. A receipt-write failure
keeps the execution held for inspection. Global pending-ticket failure propagation
is a separate issue (#15/PR16), unchanged here; this API does not repair or replay
that slot, and passing these tests is not production recovery evidence.

Synthetic SDK callback tests cover dispatch without start, synchronous callbacks,
unmatched/ambiguous jobs, duplicate/late events, phase/target/generation changes,
save uncertainty and explicit reconciliation. Fixture phase mocks have separate
names and never replace `unittest.TestCase.fail`; a negative assertion regression
checks incorrect string/tuple/list/dict comparisons actually raise. They do not validate the SDK's
native scheduling behavior or diagnose an already hung production process.

# Explicit source-map revisions

`get_painter_transfer_api(1)['revise_source_maps'](reason=..., revision_id=..., resolution=...)`
publishes a corrected source-map baseline after a verified baseline exists. Use a
unique safe request ID and a concrete failure/correction reason. Correct projection
inputs through the owner's existing controls before this call. It verifies unchanged
High/Low geometry and UVs, preserves the original baseline and complete previous
pipeline state, then uses the existing atomic baker under a separate revision archive.
Only downstream Painter/bake/apply checkpoints are invalidated; no SDF, retopology or
unwrap is repeated. A published revision with a state-write gap is restored rather
than baked again. Incomplete attempts and changed request inputs require inspection.
