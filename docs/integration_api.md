# Blender Pipeline Integration APIs

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
3. Validate the result and create Painter High/Low collections/materials.
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
