# On-demand High-poly Retopo / Painter Architecture

This pipeline is intentionally split between a Codex skill and several narrow,
deterministic add-on APIs. The skill decides *when* to advance and performs
visual QA. Each add-on mutates only the data in its own domain.

This is an explicitly invoked repair/baking path for an unsatisfactory
high-poly result. The input may come from Meshy, another generator, or an
ordinary imported Blender mesh. Meshy provenance is optional and the existing
`meshy_*` identifiers are retained for compatibility; generation alone never
starts this pipeline automatically. A completed Meshy Smart Topology asset with
valid UVs and maps normally bypasses this path and goes directly through the
Meshy Bridge/Send2UE workflow unless the user explicitly requests rework.

## Responsibility boundary

| Concern | Owning add-on | `meshy-retopo-painter` skill |
|---|---|---|
| Quad target/result | `quad-remesher-workflow-addon`: evaluated triangles, metric bounds, 1,000 rounding, 5k/100k clamp, public QR settings, native-result receipt | Select the intended source, pause for one native **Remesh It**, inspect silhouette/folds |
| UV | `UVgami`: OptCuts Hard Surface profile, asynchronous start/poll, topology pin, packed-UV validation | Inspect seams/packing and advance only after SUCCESS |
| Painter pair | `substance-tools`: archive source, rename `_high`/`_low` once, isolate Low materials, classify `Baking/high` and `Baking/low` | Resolve genuine selection ambiguity |
| Source maps | `substance-tools`: reproject Color and packed Extra; split Extra R/G/B; bake a Low-tangent DirectX Normal | Review bakes and decide whether a manual exception is justified |
| Painter | `substance-tools` plus its Painter plugin: X2/By Mesh Name/Material Color, managed source layer, Normal override | Wait for Painter, inspect receipt and visual result |
| Unreal unit | `ue-unique-export-names-addon`: Empty root, `Export` links, Send2UE asset name/settings | Send only after the final visual gate |
| Backups/apply | `substance-tools`: immutable snapshots and grouped canonical replacement with rollback | Gate apply and never make ad-hoc backup copies |

The ordinary Quad Remesher license is not treated as permission for SDK or
unattended process integration. `st.prepare_meshy_retopo` never calls the QR
operator or executable. The companion is maintained separately at
[`nsdoanfosan/quad-remesher-workflow-addon`](https://github.com/nsdoanfosan/quad-remesher-workflow-addon)
and contains no Exoside source or engine files.

Low UV generation has no Smart Project fallback. Substance Tools resolves the
enabled UVgami package and calls its versioned service; it never imports
UVgami's manager or changes `scene.uvgami` itself. UVgami temporarily applies
OptCuts Hard Surface, Transfer UVs, packing, scale correction, and the requested
pixel margin, captures the asynchronous request, then restores the user's
visible settings. Completion is accepted only from UVgami's terminal SUCCESS
receipt for the same object, pointer, polygon count, and valid 0–1 UV map.

Substance Tools owns the Painter-facing `Baking/high` and `Baking/low`
classification and material isolation, but not retopology or UV generation.
After adoption it calls the optional public
`ue_unique_export_names_addon.api.ensure_painter_low_export_unit(low, asset_base,
scene)` API and stores that receipt. UE Unique Export owns the Low export unit and
Low-to-`Export` mirror; Substance Tools never creates, groups, or links the
`Export` collection directly.

For a standalone static Low, the API keeps the mesh child named `<base>_low` for
Painter's By Mesh Name match and uses an exact top-level Empty `<base>` as the
Send2UE/Unreal asset name. It preserves the child world transform, selects Send2UE
Combine `Child meshes`, and leaves UE Unique `use_immediate_parent_name` off. An
exact-name/type/ownership collision fails rather than creating `.001`. A Low with an
Armature relationship or shape keys is not wrapped or reparented; its hierarchy is
preserved and synchronized. An existing parent is accepted only when it is the exact
top-level Empty `<base>` with no other mesh. Any other non-skeletal parent stops the
operation without mutation.

## State machine

```text
ANALYZED
  -> SOURCE_ARCHIVED
  -> QR_READY
  -> LOW_CREATED
  -> UVGAMI_RUNNING
  -> UV_READY
  -> PAINTER_PACKAGE_READY
  -> BAKE_BASELINE_ARCHIVED
  -> SOURCE_LAYER_READY
  -> EXPORT_STAGED
  -> CANONICAL_APPLIED
  -> VERIFIED
```

The scene receipt is `_substance_tools_meshy_pipeline_state_v2`. Legacy v1 state remains
read-only and is never reinterpreted as v2. A retry must
resume from its first incomplete checkpoint. `VERIFIED` is reserved for the
skill after deterministic checks and visual QA; the add-on stops at
`CANONICAL_APPLIED` by itself.

Creating or linking the export unit does not authorize export. Actual Unreal Handoff and
Send2UE begin only after the pipeline receipt is `VERIFIED`.

## Blender operators

The existing Substance panel remains the primary visible UI. Optional high-poly
Painter actions live in the later, default-collapsed **High-Poly Painter
Transfer (Advanced)** child panel. Quad Remesher and UVgami controls are not duplicated
there; the skill uses the staged orchestration operators or calls each owner's
API directly.

- `st.analyze_meshy_source`: optional, strictly read-only analysis.
- `st.prepare_meshy_retopo`: publish stage 1 and configure QR without running it.
- `st.adopt_retopology_pair`: hidden orchestration operator that validates and
  adopts the selected QR result, classifies High/Low in `Baking/high` and
  `Baking/low`, records only `LOW_CREATED`, and does not resolve or start UVgami.
- `st.finalize_meshy_retopo`: compatibility entry point that runs the same
  adoption stage when needed and then starts UVgami API v1. While UVgami
  is active the state remains `UVGAMI_RUNNING`; run the same operator again
  after completion to validate the transferred UV and advance to `UV_READY`.
- `st.prepare_meshy_low_uv`: alternate start/status/confirmation entry point for
  the same recorded Low UVgami job.
- `st.bake_meshy_source_maps`: one automatic pass over whichever Color, Extra,
  and Normal roles actually exist;
  it also exports the Painter FBXs and publishes stage 2.
- `st.export_baking_to_substance_painter`: create/update the Painter project.
- `st.export_painter_textures_and_apply`: validate the exact source-backed
  Color/Extra/Normal roles for each Texture Set, transactionally canonicalize
  them, and reconnect the Low material without inventing absent optional maps.

Painter source request fields are:

```json
{
  "source_material_maps": {
    "TextureSet": {
      "BaseColor": "...Color_baking.png",
      "ExtraR": "...ExtraR_baking.png",
      "Roughness": "...Roughness_baking.png",
      "Metallic": "...Metallic_baking.png"
    }
  },
  "source_normal_mesh_maps": {
    "TextureSet": {
      "source_normal_texture": "...Normal_baking.png",
      "normal_convention": "DIRECTX",
      "basis": "LOW_TANGENT"
    }
  }
}
```

These roles are optional per Texture Set. If packed Extra exists, its
`Extra`/`ExtraR`/`Roughness`/`Metallic` group is all-or-nothing. A canonical
ASCII-safe Texture Set ID is fixed at adoption and reused for the Low material,
FBX, Painter set, baked maps, export validation, and final filenames; normalized
name collisions are rejected before mutation.

`ExtraR` uses Painter's AO channel only as a transport so the final packed
Extra R byte survives; it is not a semantic claim that every Meshy Extra R is
ambient occlusion. Raw RGB Extra is never assigned to a grayscale Painter
channel because that would use luminance and corrupt G/B.

## Immutable files

```text
<blend-dir>/_painter_archive/<asset>/
  00_source_original_once/
    scene/<original>.blend
    texture/<TextureSet>/<Role>/<original Color, Extra, Normal>
    manifest.json
  10_bake_baseline_once/
    low/<asset>_low.fbx
    high/<asset>_high.fbx
    texture/<Color, Extra, ExtraR, Roughness, Metallic, Normal bakes>
    manifest.json
```

Both stages publish via sibling temporary directories, SHA-256 verification,
manifest-last writes, and atomic rename. Existing stages are archive-verified
without reinterpreting Painter-replaced canonical textures as new originals.

## Regression fixture

`Prop_Huya_Cushion_Purple_01.blend` has 3,044,880 evaluated triangles and a
0.593995 m bounding-box diagonal. Formula v1 must return 25,000 quads and set
`adapt_quad_count=False`. The first pass accepts an actual result within 10%.

Run the pure tests with `python -m unittest discover -s tests -p "test_*.py"`.
Run Blender smoke tests only with Blender `--factory-startup`, an isolated
`BLENDER_USER_CONFIG`, and add-on registration `default_set=False`. Never save
user preferences from a factory-startup process.
