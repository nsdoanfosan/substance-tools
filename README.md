# Substance Import-Export Tools

A Blender add-on that drives a **Blender → Substance Painter → Unreal** baking
and texturing pipeline. Meshes are organised in a fixed `Baking` collection
layout, sent to Painter for mesh-map baking and texturing, and the resulting
maps are loaded back onto the Blender materials. Changes are tracked per
Texture Set so only what actually changed is re-baked.

> Originally derived from
> [passivestar/substance-tools](https://github.com/passivestar/substance-tools)
> and licensed under GPLv3. It has since been almost entirely rewritten through
> 3.0.0 (2026): the original collection-based workflow (`Export and Open in
> Painter` / `Load Painter Textures`, Node Wrangler dependency, custom Textures
> Path) was removed, and the only workflow now is the `Baking` collection
> pipeline described below.

## Installation

### Blender add-on

- `Edit -> Preferences -> Add-ons -> Install...`
- Select the add-on zip (or this repository's `__init__.py`).
- In the 3D view press `N` and open the **Substance** tab.

### Painter companion plugin

The Blender add-on talks to Painter through a startup plugin. Copy

`painter/startup/substance_tools_unreal_viewport`

into

`Documents/Adobe/Adobe Substance 3D Painter/python/startup/`

and restart Painter. The two halves communicate over file-based JSON in the
project's `texture` folder (`.substance_tools_request.json`,
`.substance_tools_bake_plan.json`, and the export request/result files). The
plugin polls every 0.5 s and performs project creation, mesh reimport,
mesh-map baking, and `Unreal_V2` texture export automatically.

## Collection layout

The add-on creates this layout automatically:

```text
Baking
|- low      # the meshes that get UVs, textures, and the final bake target
|- high     # high-poly source for High-to-Low baking (optional per asset)
`- alpha    # decal/logo meshes baked as editable alpha overlays (optional)
```

Everything the pipeline sends to Painter comes from these collections — there
is no "pick a collection in the outliner" step anymore. Source meshes are never
modified; exports run on temporary evaluated copies.

## Panel: Substance Painter Tools

### High to Low Baking

- **Pair Selected Low + High** — the explicit naming step. Select exactly one
  Low and one or more High meshes already in their Baking collections. The Low
  becomes `A_low`; a single High becomes `A_high`, several become `A_high_01`,
  `A_high_02`, … Existing Low materials keep their names and only receive a
  missing `M_` prefix. Selecting more than one Low is rejected.
- **Group Selected Meshes** — parents selected meshes under an Empty. If one
  Empty is in the selection, every selected mesh is moved under it (even meshes
  already parented to another Empty). Otherwise a new Empty is created from the
  active mesh's name, and meshes that are already inside an Empty are rejected.
- **Toggle Export Link** — links/unlinks the selected objects **and their whole
  child hierarchy** (meshes, curves, empties — any type) in the
  [Send to Unreal](https://github.com/poly-hammer/BlenderTools) `Export`
  collection, in one direction: if everything gathered is already in `Export`
  they are all unlinked, otherwise the missing ones are linked in. Objects keep
  their original collection too. Unlinking never deletes — an object whose only
  home was `Export` is moved to the scene root. The `Baking/low` set is never
  touched.
- **Bake Resolution** — `512` … `8192` (default `2048`).
- **High-Low Matching** — `By Mesh Name` (match `rock_low` ↔ `rock_high`,
  default) or `Always` (every high projects to every low).
- **ID Source** — how the Painter ID map is generated:
  - `High Face Sets` (default): convert High-poly Sculpt Face Sets to stable
    temporary vertex colors;
  - `Existing Vertex Color`: use the High-poly vertex color attribute as-is;
  - `Material Color`: use High-poly material colors.

  Conversion happens only on the temporary export copy. When `high` is empty
  and a Texture Set bakes Low-as-High, Face Sets on the Low mesh are converted
  on the temporary Low copy instead.

### Bake Plan (incremental baking)

Change detection is **per Texture Set**, hashing mesh contents directly (not
FBX file bytes), so editing one asset doesn't force a full re-bake.

- **Check Bake Plan** — computes what the next Painter update would do and shows
  it per material/Texture Set:
  - `High-to-Low` vs `Low Poly as High` (whether the Texture Set has a matching
    high-poly mesh);
  - `Rebake`, `Low changed: reload only`, `High changed`, `Settings changed`,
    or `Low baseline missing`.

  It writes `.substance_tools_bake_plan.json` next to the project.

> **Check Bake Plan is required before `Update Painter`.** Running an update
> without a current plan is rejected with *"Run Check Bake Plan before Update
> Painter"*. After every successful create/update the plan is reset to a clean
> baseline; re-run Check Bake Plan after editing meshes again.

### Base color & alpha bakes

- **Bake Base Color** — Cycles Selected-to-Active diffuse bake transferring an
  image connected upstream of the High-poly Principled BSDF Base Color onto the
  Low UVs. Output is named with a `_baking` suffix, e.g.
  `texture/T_rock_Color_baking.png`, and connected to the Low materials. Painter
  imports the same file into a bottom Fill Layer `Blender High Base Color`.
  Currently requires exactly one Low-poly Texture Set.
- **Bake Alpha Details** — treats `Baking/alpha` meshes as editable decals/logos.
  Name them to match their Low mesh (`rock_low` ↔ `rock_alpha`). Each Alpha
  material must use a Principled BSDF with one image texture wired to **both**
  Base Color and Alpha. One RGBA texture is written per target, e.g.
  `texture/T_rock_body_Color_alpha.png` (RGB = color, A = opacity). When a Low
  mesh has multiple materials, pick the target Texture Set per Alpha object in
  the **Alpha Targets** sub-panel. Painter imports it into a top Fill Layer
  `Blender Alpha Details` with a `Blender Alpha Mask`. Alpha meshes are never
  included in the High FBX or the mesh-map bake.

### Painter project buttons

- **Create in Painter** (when no `.spp` exists) — creates the project from
  Painter's bundled `Unreal Engine.spt` template and bakes every Texture Set.
  Once the project exists this button becomes **Open Painter Project**.
- **Update Painter** (enabled once the project exists) — rewrites the FBX and
  request data; the open Painter project applies the plan:
  - only High-to-Low Low meshes changed → reimport Low without baking, update
    the Blender Base Color Fill Layer, save;
  - a Low Poly as High Texture Set changed → that Texture Set is re-baked;
  - High mesh or bake settings changed → mesh-map baking runs after any
    required Low reimport.

  If Painter is closed it is launched on the existing project first.
- **Export Painter Textures & Apply** (enabled once the project exists) — asks
  the open project to export with the `Unreal_V2` preset, waits (5-minute
  timeout), reloads the maps, and connects **Color, Normal, packed Extra,
  Emissive, and Height** to the Low materials. The packed Extra texture uses
  **Green = Roughness, Blue = Metallic**.
- **Base Color Source** — `Use Painter Base Color` / `Use Baked Base Color`
  switches only the Low material connection; both source files stay untouched.
  The Painter Base Color already contains the alpha-overlay result, so the
  Blender overlay preview is disabled when it is selected.

The companion Painter plugin saves the `.spp` after a successful bake, returns
Painter to Painting mode, and marks failed requests so they are not retried in
a loop.

## Panel: Export Status

A read-only sub-panel that classifies the `Send to Unreal` `Export` collection:

- **Low Auto** — the `Baking/low` meshes (plus their parent/armature chain) that
  belong to the Painter export; managed automatically, not toggleable here.
- **Linked** — objects in `Export` that also live in another collection.
- **Export Only** — objects whose only home is `Export`.

The hierarchy is shown with indentation, clicking a row selects that object, and
objects that are hidden (and would therefore not export) are flagged with a
warning.

## Handoff to Send to Unreal

This add-on is paired with
[UE Unique Export Names](https://github.com/nsdoanfosan/ue-unique-export-names-addon)
for the final Blender-to-Unreal handoff.

After the Painter round trip is complete:

1. Use `Export Painter Textures & Apply` so the `Unreal_V2` maps are present in
   the shared `texture` folder and connected to the `Baking/low` materials.
2. Do not rename the Low materials afterward. Painter Texture Set names match
   the Blender material names with the leading `M_` removed.
3. `UE Unique Export Names` automatically links the `Baking/low` meshes and
   their complete parent chains into `Export`. No handoff button is required.
4. Export with Send to Unreal. Enable `Combine > Child meshes` when an Empty and
   its child meshes should become one Unreal asset.

`UE Unique Export Names` does not copy or rename the Painter Low data. It links
the existing `Baking/low` hierarchy into `Export` automatically and protects its
objects, mesh data, materials, images, and texture files from its External
workflow.

## Naming conventions

Unreal naming drives the whole pipeline:

- Material `M_rock` → the `M_` prefix is stripped on the Low FBX → Painter
  Texture Set `rock`.
- Exported textures are `T_<set>_<map>`, e.g. `T_rock_Color.png`.
- Low/High meshes are paired as `A_low` / `A_high` (or `A_high_01`, …).

## Typical workflow

1. Put meshes into `Baking/low` (and `Baking/high`, `Baking/alpha` as needed).
2. **Pair Selected Low + High** to name each bake pair.
3. (Optional) **Group Selected Meshes** and **Toggle Export Link** for the
   Send to Unreal export set.
4. Set **Bake Resolution**, **High-Low Matching**, **ID Source**.
5. **Create in Painter** — first full bake; texture in Painter.
6. After editing meshes: **Check Bake Plan** → **Update Painter** (only the
   changed Texture Sets re-bake).
7. **Export Painter Textures & Apply** to bring the maps back onto the Low
   materials.
8. Toggle **Base Color Source** between the Painter and baked result as needed.

## Preferences

- **Substance Painter Path** — set this if the executable wasn't auto-detected.

## Supported Substance versions

- Windows CC Substance Painter
- Windows Steam Substance Painter
- macOS CC Substance Painter
- macOS Steam Substance Painter
