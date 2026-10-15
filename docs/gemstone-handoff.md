# Gemstone handoff contract

`pipeline_contract.json` → `unreal_handoff_sidecar.gemstone_material_layer`
defines the project paths shared by UE Unique and Send to Unreal.

A delimited `Gemstone` token in the final Blender material slot name selects a
dedicated crystal background layer and separate MI/MYI. The source mesh, rig,
High/Low pairing names and Painter source maps remain separate concerns.
The generated sidecar omits Blender/Painter appearance maps for this slot;
Unreal owns crystal color and detail. Existing Unreal controls are retained
when the mesh is sent again.

Consumers fall back to their bundled default paths when this contract section
is absent, so older installations do not fail to load.
