# Source-backed cloth details

`Bake Alpha Details` keeps source color and tangent-space DirectX normal maps
separate. A connected image-backed Normal Map opts a detail into the normal bake.
Painter receives an explicit grayscale coverage mask and a managed
`Blender Alpha Normals` layer using Normal Map Detail blending. Existing material
layers stay in place. Removing the normal connection and rebaking retires only
that managed companion.

For independently colored seams and buttons, mark the alpha source object with
`substance_tools_alpha_id_only = True`. It writes `T_<TextureSet>_ID_alpha.png`
without painting its palette into the cloth's Base Color or Normal. Optional source
properties `substance_tools_alpha_id_resolution`,
`substance_tools_alpha_cage_extrusion`, and
`substance_tools_alpha_max_ray_distance` bound the projection for narrow bands.
Uncovered ID RGB is cleared so padding cannot select unrelated fabric.

The scene JSON property `substance_tools_id_selections` can bind those source IDs
to inspected existing Painter layers. Each entry specifies `texture_set_name`,
`layer_uid`, `expected_layer_name`, `effect_name`, and `colors` (one to sixteen RGB
triples in working color space). Update Painter supplies the exact PNG path/hash.
The native handoff validates every target and source before editing ID effects;
it reuses the named Color Selection and retains old paint effects. This native
path does not require a separate MCP bridge plugin.

Set scene `substance_tools_bake_back_normals = True` when back Texture Sets should
be baked from their own High meshes instead of receiving a front-normal reuse
plan. Update Painter still requires a fresh, matching checked bake plan.

Automatic SDK baking remains the default. For the explicit UI-start workaround,
the export operator's hidden `manual_bake_start=True` configures the bake and
waits for a native AboutToStart event. It retains the native job token and existing
request/project/phase guards; an unmatched end or a second native job cannot
authorize a save or completion.
