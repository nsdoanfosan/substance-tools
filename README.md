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
|- high
`- alpha
```

`Create in Painter` exports evaluated meshes without changing the Blender source:

- `low/<blend>_low.fbx`
- `high/<blend>_high.fbx`
- `texture/<blend>_SP.spp`

`Pair Selected Low + High` is the explicit naming step. Select exactly one Low
and one or more High meshes that are already in their Baking collections. The
Low becomes `A_low`; one High becomes `A_high`, while several become
`A_high_01`, `A_high_02`, and so on. Existing Low materials keep their names
and receive only a missing `M_` prefix. Selecting more than one Low is rejected.

Low FBX material names have the leading `M_` removed so `M_rock` becomes the
Painter Texture Set `rock`. High-poly Sculpt Face Sets are converted to stable
vertex colors for the ID baker by default. Blender also offers `Existing Vertex
Color` and `Material Color` as ID sources. Conversion happens only on the
temporary export copy; source Blender meshes are not modified. When `high` is
empty and Painter uses Low-as-High, Face Sets on the Low mesh are converted on
the temporary Low export copy instead.

The companion Painter startup plugin:

- reloads the low mesh only when its exported contents changed;
- rebakes High-to-Low Texture Sets only when their high FBX contents or bake
  settings changed;
- rebakes Low Poly as High Texture Sets when their low mesh changed;
- keeps low-only changes in High-to-Low Texture Sets as a reimport/save
  operation without mesh-map baking;
- uses High Definition Mesh, vertex-color IDs, automatic cage, and all standard
  mesh maps;
- defaults to `By Mesh Name`, with `Always` available in Blender;
- saves the SPP after a successful bake;
- returns Painter to Painting mode after a successful bake;
- creates new projects from Painter's bundled `Unreal Engine.spt`, including
  that template's project, viewport, and post-effects defaults.

`Bake Alpha Details` treats meshes in `Baking/alpha` as editable decals or
logos. Name them to match their Low mesh, for example `rock_low` and
`rock_alpha`. If the matching Low mesh has one material, its Painter Texture
Set is selected automatically. If it has several materials, choose the target
for each Alpha object in the panel. Reusing the same material name on the Alpha
object also selects that target automatically.

Each Alpha material must use a Principled BSDF with an image texture connected
to both Base Color and Alpha. The add-on writes one RGBA texture per target,
for example `texture/T_rock_body_Color_alpha.png`: RGB contains the decal color
and A contains its opacity. In Blender this image is mixed over the baked High
Base Color for preview. The overlay is disabled automatically when the final
Painter Base Color is selected, because that export already contains the
Painter layer result.

Painter imports the RGBA image into a top Fill Layer named
`Blender Alpha Details`, enables only Base Color, adds a black mask, and places
`Blender Alpha Mask` as a Fill effect inside that mask. Alpha meshes are never
included in the Painter High FBX or Painter mesh-map bake.

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
Opening launches the existing SPP when Painter is not running. Updating rewrites
the FBX and request data; an open Painter project detects the changed request.
If only High-to-Low Low meshes changed, Painter reimports the Low mesh without
running mesh-map baking, updates the Blender Base Color Fill Layer, and saves
the SPP. If a Low Poly as High Texture Set changed, that Texture Set is rebaked.
If the High mesh or bake settings changed, Painter runs mesh-map baking after
any required Low mesh reimport. If Painter is closed, the existing SPP is opened
first.

`Export Painter Textures & Apply` asks the open Painter project to export with
the `Unreal_V2` preset, waits for completion, reloads the exported maps in
Blender, and connects Color, Normal, packed Extra, Emissive, and Height maps to
the Low materials. The packed Extra texture uses Green for Roughness and Blue
for Metallic. `Use Baked Base Color` / `Use Painter Base Color` switches only
the Low material connection; both source files remain untouched.

Texture export remains manual. Select the existing `Unreal_V2` Painter preset
and export PNG files into the shared `texture` folder.

The Painter plugin is installed from
`painter/startup/substance_tools_unreal_viewport` into:

`Documents/Adobe/Adobe Substance 3D Painter/python/startup/`

# Preferences

In the addon preferences you can configure:

- Substance Painter Path (in case it wasn't automatically detected)

# Supported Substance Versions

The addon is working with:

- Windows CC Substance Painter
- Windows Steam Substance Painter
- MacOS CC Substance Painter
- MacOS Steam Substance Painter
