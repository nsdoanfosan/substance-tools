# Substance Import-Export Tools

<img width="960" alt="substancetools" src="https://github.com/passivestar/substance-tools/assets/60579014/0e13aa12-3ddd-4151-bbbc-dae41137027a">

https://github.com/passivestar/substance-tools/assets/60579014/b47d8e04-7535-4510-aed2-9c4569880b02

Join our [discord](https://discord.gg/pPHQ5HQ) for discussion!

## Installation

- Click on "Releases" on the right and download zip
- Go to `Edit -> Preferences -> Addons`
- Press `Install...`
- Select the archive

In 3D view press N. You'll find new buttons in the menu on the right on the "Substance" tab

## Baking workflow

The add-on creates this collection layout automatically:

```text
Baking
|- low
`- high
```

`Create in Painter` exports evaluated meshes without changing the Blender source:

- `low/<blend>_low.fbx`
- `high/<blend>_high.fbx`
- `texture/<blend>_SP.spp`

Low FBX material names have the leading `M_` removed so `M_rock` becomes the
Painter Texture Set `rock`. High-poly Sculpt Face Sets are converted to stable
vertex colors for the ID baker by default. Blender also offers `Existing Vertex
Color` and `Material Color` as ID sources. Conversion happens only on the
temporary export copy; source Blender meshes are not modified. When `high` is
empty and Painter uses Low-as-High, Face Sets on the Low mesh are converted on
the temporary Low export copy instead.

The companion Painter startup plugin:

- reloads the low mesh only when its exported contents changed;
- rebakes only when low/high FBX contents or bake settings changed;
- uses High Definition Mesh, vertex-color IDs, automatic cage, and all standard
  mesh maps;
- defaults to `By Mesh Name`, with `Always` available in Blender;
- saves the SPP after a successful bake;
- returns Painter to Painting mode after a successful bake;
- creates new projects from Painter's bundled `Unreal Engine.spt`, including
  that template's project, viewport, and post-effects defaults.

`Bake Base Color` transfers an image texture connected upstream of the
High-poly Principled BSDF Base Color to the Low UVs with a Cycles
Selected-to-Active diffuse-color bake. It uses the existing texture naming
rule with a `_baking` suffix, for example
`texture/T_rock_Color_baking.png`, then connects that image to the Low
materials' Principled BSDF Base Color inputs. Painter imports the same file and
assigns it to a bottom Fill Layer named `Blender High Base Color`, with only
Base Color enabled. This button currently requires exactly one Low-poly
Texture Set.

When no SPP exists, the primary button is `Create in Painter`. Once the SPP
exists it becomes `Open Painter Project`, while `Update Painter` is available.
Opening launches the existing SPP when Painter is not running. Updating rewrites the FBX and
request data; an open Painter project detects the changed request and performs
one mesh reimport without running mesh-map baking. It then updates the Blender
Base Color Fill Layer and saves the SPP. If Painter is closed, the existing SPP
is opened first.

`Export Painter Textures & Apply` asks the open Painter project to export with
the `Unreal_V2` preset, waits for completion, reloads the exported maps in
Blender, and connects Color, Normal, packed Extra, Emissive, and Height maps to
the Low materials. The packed Extra texture uses Green for Roughness and Blue
for Metallic. `Use Baked Base Color` / `Use Painter Base Color` switches only
the Low material connection; both source files remain untouched.

If the automated export cannot be used, the fallback is manual Painter export:
select the existing `Unreal_V2` preset and export PNG files into the same shared
`texture` folder.

The Painter plugin is installed from
`painter/startup/substance_tools_unreal_viewport` into:

`Documents/Adobe/Adobe Substance 3D Painter/python/startup/`

## Handoff to Send to Unreal

This fork is paired with
[`UE Unique Export Names`](https://github.com/nsdoanfosan/ue-unique-export-names-addon)
for the final Blender-to-Unreal handoff.

After the Painter round trip is complete:

1. Use `Export Painter Textures & Apply` so the `Unreal_V2` maps are present in
   the shared `texture` folder and connected to the `Baking/low` materials.
2. Do not rename the Low materials afterward. Painter Texture Set names match
   Blender material names with the leading `M_` removed.
3. `UE Unique Export Names` automatically links the `Baking/low` meshes and
   their complete parent chains into `Export`. No handoff button is required.
4. Export with Send to Unreal. Enable `Combine > Child meshes` when an Empty and
   its child meshes should become one Unreal asset.

`UE Unique Export Names` does not copy or rename the Painter Low data. It links
the existing `Baking/low` hierarchy into `Export` automatically and protects
its objects, mesh data, materials, images, and texture files from its External
workflow.

# Usage

- Put objects you want to texture into a collection and give them materials. Individual materials will become texture sets in Painter! If an object doesn't have a material it will be created automatically
- Click on the collection you want to texture in the outliner
- Press the `Export and Open in Painter` button
- When you're done, export textures from Painter (`Ctrl+Shift+E`), and press `Load Painter Textures` in Blender ✨

Tip:
- In Blender you can link objects to a collection instead of moving them if you hold `Ctrl` when you drag them in the outliner. This way you can create collections specifically for Substance Painter export and group assets however you like!

# Preferences

In the addon preferences you can configure:

- Substance Painter Path (in case it wasn't automatically detected)
- Textures Path. Path to the directory where all of the subdirectories for collections will be created. Leave blank to create them in the same directory as the blend file

# Supported Substance Versions

The addon is working with:

- Windows CC Substance Painter
- Windows Steam Substance Painter
- MacOS CC Substance Painter
- MacOS Steam Substance Painter
