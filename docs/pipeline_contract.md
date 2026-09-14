# Pipeline Contract

This document is the shared contract for the Blender -> Substance Painter -> Unreal
pipeline. It exists because the three participating codebases are coupled by
names, folders, JSON files, and path assumptions rather than by Python imports.

When a convention changes in one repository, the other repositories may not
raise a load error. The failure mode is often "nothing happened", which is the
hardest kind of bug to debug. Treat this file and `pipeline_contract.json` as
the source of truth before changing any pipeline-facing names.

Sections below separate **Current behavior** (verified against the code on
2026-06-27) from **Target direction** (what we want but have not built yet).
Do not read a target as if it were current behavior. When the code changes,
re-verify and update this file and `pipeline_contract.json` together.

## Participating Repositories

- `substance-tools` owns the Blender baking workflow and Painter request JSON.
- `ue-unique-export-names-addon` owns the final Send to Unreal handoff behavior
  around unique export names and protected Painter-generated assets.
- `BlenderTools` / Send to Unreal owns the `Export` collection semantics and
  the actual Unreal export operation.

Do not change a shared convention in one repository without checking the other
two.

## Shared Conventions (Current behavior)

Verified against the code on 2026-09-03.

Collections. Role is decided by COLLECTION MEMBERSHIP, not by object name:

- Baking root collection: `Baking`
- Low / High / Alpha collections: `low`, `high`, `alpha` (children of `Baking`;
  the actual collection names are `low`/`high`/`alpha`, not `Baking/low`)
- Send to Unreal collection: `Export`

On-disk layout, where `<base>` is the .blend folder and `<asset>` is the .blend
filename stem (not a per-object name):

- Low FBX: `<base>/low/<asset>_low.fbx`
- High FBX: `<base>/high/<asset>_high.fbx`
- Painter project: `<base>/texture/<asset>_SP.spp`

Names and prefixes:

- Material prefix: `M_`
- Texture prefix: `T_`
- Painter export preset: `Unreal_V2`
- Painter Texture Set name: the Low material name with exactly one leading `M_`
  stripped
- Exported texture pattern: `T_<texture_set>_<map>.png`

Role-pairing suffix. Used to pair low<->high (baking) and low<->alpha (alpha
detail) objects, NOT to classify which collection an object belongs to. A
trailing `low`/`high`/`alpha` token is stripped and the remaining base is
compared. The separator is optional (`_`, `.`, `-`, a space, or none), an
optional trailing number is allowed, and a Blender `.001` duplicate suffix is
removed first; matching is case-insensitive. So `rock_low` pairs with
`rock_high` and `rock_alpha`, and `rock_high_01` / `rock_high1` also reduce to
base `rock`. Regex: `(?i)(?:[_. -]?(low|high|alpha))(?:[_. -]?\d+)?$`.

These strings should not be retyped casually. If they become runtime-shared,
load them from `pipeline_contract.json` or a small common module rather than
duplicating literals.

### Painter Retopo Low export unit

The Painter pairing suffix and the Unreal asset name intentionally live on different
objects for a standalone static retopologized asset:

```text
<base>              # top-level Empty; Send2UE/Unreal asset name
`-- <base>_low      # mesh; Painter pairs it with <base>_high
```

`ue_unique_export_names_addon.api.ensure_painter_low_export_unit()` owns this unit. It
links the Empty and child into `Export`, preserves the child world transform, and selects
Send2UE Combine `Child meshes`. UE Unique `use_immediate_parent_name` stays off. The Empty
must be top-level and contain no unrelated mesh; exact name/type/ownership collisions fail
instead of producing `.001`.

Only a standalone static Low is wrapped automatically. Armature relationships and shape
keys are preserved and synchronized without reparenting. An existing parent is accepted
only when it is the exact top-level Empty `<base>` with no other mesh; any incompatible
non-skeletal parent stops without mutation. Substance Tools must not duplicate the grouping
or `Export` mutation. Regardless of when the unit is prepared, actual Unreal Handoff and
Send2UE are gated on Meshy pipeline state `VERIFIED`.

## Silent Failure Risk

This pipeline currently has several convention-only links:

- Blender collections named `Baking`, `low`, `high`, `alpha`
- Send to Unreal collection named `Export`
- Material and texture prefixes `M_` and `T_`
- Painter Texture Set names derived from Blender Low material names
- Texture Sets ending in `_back` are treated as backsides; their front Texture
  Set is the same name without `_back` (for example `sibuki_01_back` uses
  `sibuki_01`)
- JSON request files exchanged through the `texture` and `low` folders plus
  `%LOCALAPPDATA%/SubstanceTools/` (see the JSON IPC Contract for exact paths)
- Unreal path anchoring such as `Forestportfolio` -> `/Game/Meshes`

If any of these change on only one side, there may be no import error, syntax
error, or obvious exception. The likely symptom is an empty collection, zero
textures applied, a skipped Painter action, or an Unreal import landing in the
wrong path.

When debugging "nothing happened", check the contract before changing behavior.

## Unreal Handoff Sidecar Contract

`ue-unique-export-names-addon` writes one JSON sidecar per export asset unit for Send to
Unreal and the Unreal material setup script. An ungrouped unit uses its mesh name; an
Empty-grouped unit uses the top-level Empty name and does not publish competing child-mesh
sidecars. The current sidecar schema is version 3 and includes material entries plus a
`cleanup` object.

The `cleanup.source_material_names` and `cleanup.source_texture_names` arrays
are precomputed by the producer. They describe FBX-import source assets that may
be removed from the imported mesh folder after Unreal material instances and
canonical textures are set up. Consumers should prefer these arrays when present
and fall back to deriving cleanup names from `materials` only for older sidecars.

## SpeedTree Production Group Naming Contract

SpeedTree handoff contract version 2 derives production groups from material
structure, not from a hardcoded list such as `green`, `yellow`, or `dead`.
After Blender duplicate and `.stmat` suffix cleanup, the parser finds the last
independent numeric segment. When a non-empty suffix follows that segment through
an allowed separator, the name through the number is the production-group base
and the entire remaining suffix is one case-folded group value.

| Material | Base | Production group |
| --- | --- | --- |
| `M_Leaf_common_grass_01_green` | `M_Leaf_common_grass_01` | `green` |
| `M_Leaf_common_grass_01_dead` | `M_Leaf_common_grass_01` | `dead` |
| `M_Leaf_common_grass_01_winter_dry` | `M_Leaf_common_grass_01` | `winter_dry` |
| `M_stem_common_01` | `M_stem_common_01` | none |
| `M_Branch_deadbranch_01` | `M_Branch_deadbranch_01` | none |

The compatibility field `production_group_tokens` remains an array, but it now
contains either no value or exactly one complete suffix. Words before the final
number are never stripped, and suffixes are accepted regardless of whether an
Atlas Leaf Mesh Builder generated them or a user supplied a collection name.
The regex, separators, normalization, cardinality, and safety boundary live in
`pipeline_contract.json`; consumers must not recreate a token allowlist.

This is a pure material-name parser only. Its result does not establish SPM or
STMAT provenance, does not compare a source signature, and does not authorize an
atlas split or source mutation. Application runtimes must separately enforce
`pcg_atlas_auto_split.requires_provenance_and_matching_source_signature` before
using parsed production groups for those operations. Atlas Builder's automatic
classifier labels do not restrict user collection names, and SpeedTree
`instance_profile` is a separate namespace.

## Path Mapping Contract

The current Unreal handoff has historically assumed a simple anchor:

```text
Forestportfolio -> /Game/Meshes
```

This is fragile. It works only while the local folder tree mirrors the Unreal
content tree below that anchor. Character or special-purpose subtrees can break
the assumption without changing any code.

Target direction:

- Support multiple local-to-Unreal mappings.
- Allow a sidecar file next to the `.blend` to override the default mapping.
- Fail loudly when no mapping matches, instead of silently falling back to a
  plausible but wrong path.

Suggested sidecar shape:

```json
{
  "unreal_path_mappings": [
    {
      "local_anchor": "Forestportfolio",
      "unreal_anchor": "/Game/Meshes"
    },
    {
      "local_anchor": "Forestportfolio/Characters",
      "unreal_anchor": "/Game/Characters"
    }
  ]
}
```

## JSON IPC Contract

The Blender add-on and the Painter startup plugin communicate through JSON
files. Painter polls every `0.5` seconds (`QTimer.setInterval(500)`). Every file
is written to a `.<name>.tmp` sibling and then `os.replace()`d into place, so a
reader never sees a half-written file.

### Current behavior (verified 2026-06-27)

The files are NOT all in one folder:

| File | Location |
| --- | --- |
| `pending_request.json` | `%LOCALAPPDATA%/SubstanceTools/` (then `%TEMP%`, then home) |
| `.substance_tools_request.json` | BOTH `<base>/texture` and `<base>/low` |
| `.substance_tools_bake_plan.json` | `<base>/texture` |
| `.substance_tools_export_request.json` | `<base>/texture` |
| `.substance_tools_export_result.json` | `<base>/texture` |

`.substance_tools_request.json` is written to two places on purpose: the Painter
plugin's `_request_candidates()` looks for it next to the open `.spp` (the
`texture` dir) AND next to the last imported mesh (the `low` dir), so writing
both guarantees a hit regardless of which the plugin resolves first.

Status values the code actually uses (in the export result file, written by the
Painter plugin and read by Blender):

- `SUCCESS` - completed. Blender treats anything other than `SUCCESS` as failure.
- `ERROR` - ran but did not succeed.
- `FAILED` - could not be processed; the plugin skips any request whose `status`
  is `FAILED`, so it is not retried forever.

Identity and staleness are already handled: each request carries a `request_id`
(`str(time.time_ns())`) with a `pipeline_hash` fallback, and the plugin skips a
request whose marker equals the last one it processed. It also re-checks that
the request's `spp` matches the open project before acting. Other real payload
fields include `low_hash`, `low_hashes`, and `changed_low_texture_sets`.

### Target direction (not yet implemented)

This is a pragmatic bridge, not a robust RPC system, and race conditions remain
part of the design space. Genuine gaps (NOT written today - do not assume
present): explicit `created_at` / `updated_at` timestamps and `PENDING` /
`RUNNING` lifecycle states. The existing rules to preserve:

- A failed request must stay marked `FAILED` so it is not retried forever.
- A stale request must not be treated as a fresh request.
- Blender should time out and report a clear error when Painter does not answer.
- Painter should save enough metadata to let Blender compute the next plan
  deterministically.

## Bake Plan Contract

`Check Bake Plan` is the source of truth for `Update Painter`. `Update Painter`
must not silently recalculate a different plan.

Important behavior:

- High-to-Low Texture Sets:
  - Low-only changes reload the Low mesh.
  - Low-only changes do not rebake mesh maps.
  - High changes rebake mesh maps.
- Low Poly as High Texture Sets:
  - Low changes rebake mesh maps.
- `_back` Texture Sets:
  - They follow Low Poly as High change behavior.
  - Low changes rebake mesh maps instead of reload-only.
  - The matching front Texture Set's Normal mesh map is assigned to the `_back`
    Texture Set.
  - Normal baker is disabled for that `_back` Texture Set once the source Normal
    mesh map is assigned; the remaining mesh maps are baked.
- Low hashes are tracked per Texture Set.
- Stable source mesh array hashes are preferred for baselines. Avoid evaluated
  mesh hashes for stored baselines unless the instability is understood and
  intentional.

If this behavior changes, update the contract and run a real Painter round trip.

## Solidify Plus Rim Handling

Current behavior. Low meshes may use `Solidify Plus 1.41` to generate an
inner/back shell plus rim faces for the final Unreal export. Painter should not
receive the final rim faces for baking, because those side faces can change the
low-poly surface used by ray projection and mesh-map generation. That can move
curvature/AO/normal-driven smart material masks and make seam-based texturing
look different from the intended low source.

Do not solve this by exporting the Low mesh with Solidify disabled. Disabling
the modifier removes the generated inner/back shell as well as the rim, which
is a different mesh than the intended Painter target. The `Hide Solidify Rim in
Painter Low` toggle is enabled by default; when enabled, the Painter Low export
evaluates Solidify Plus with the shell intact but temporarily sets `Fill Rim` to
`False` for the export copy only, then restores the user's modifier settings
immediately after export.

Final Blender-to-Unreal export keeps the normal user-facing state, including
`Fill Rim=True`, so the shipped mesh still contains the rim. This split exists
to avoid a human-maintained pair of "Painter Low without rim" and "Final Low
with rim" objects per asset. The one editable source object remains the truth;
the export mode decides whether rim faces are included.

Implementation rule:

- Substance/Painter Low FBX with the default toggle enabled: Solidify Plus
  evaluated, `Fill Rim=False`.
- Substance/Painter Low FBX with the toggle disabled: current Blender state.
- Final/Send to Unreal FBX: current Blender state, usually `Fill Rim=True`.
- Never use "disable all modifiers" as the rimless Painter path; it drops the
  back shell and changes the baking target.

## Validation Requirements

Any large rewrite of protection logic, bake-plan logic, JSON request handling,
or Send to Unreal handoff logic needs at least one real end-to-end test:

1. Open a real Blender asset with `Baking/low`.
2. Run `Check Bake Plan`.
3. Run `Update Painter` against an existing Painter project.
4. Confirm High-to-Low low-only edits reload Low without mesh-map baking.
5. Confirm Low Poly as High low edits rebake.
6. Run `Export Painter Textures & Apply`.
7. Confirm the expected `T_<texture_set>_<map>.png` maps are applied.
8. Confirm the `Baking/low` hierarchy is linked into `Export` for Send to
   Unreal without duplicating or renaming Painter Low data.
9. For any Low object using `Solidify Plus 1.41`, confirm the Painter Low FBX
   has the inner/back shell but no filled rim faces, and the final Export path
   still keeps the rim.

This is especially important after large commits that rewrite protection logic.
For example, if a branch contains a `d0f438c`-style rewrite, do not trust static
review alone. Run the real Painter Low asset round trip.

## Change Policy

Before changing a shared convention:

1. Update `pipeline_contract.json`.
2. Update this document.
3. Update every repository that reads or assumes the convention.
4. Add a loud error when a required convention is missing.
5. Run the real round-trip validation above when behavior changes.

README files may summarize the workflow, but this document is the contract.
