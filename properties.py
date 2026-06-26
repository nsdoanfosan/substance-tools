import bpy

from .core import ADDON_MODULE_NAME, _DETECTED_PAINTER_PATH
class TextureSetBakeItem(bpy.types.PropertyGroup):
  name: bpy.props.StringProperty()
  bake: bpy.props.BoolProperty(name='Bake', default=True)


class SubstanceToolsBakingSettings(bpy.types.PropertyGroup):
  resolution: bpy.props.EnumProperty(
    name='Bake Resolution',
    items=[
      ('512', '512', '512 x 512'),
      ('1024', '1024', '1024 x 1024'),
      ('2048', '2048', '2048 x 2048'),
      ('4096', '4096', '4096 x 4096'),
      ('8192', '8192', '8192 x 8192'),
    ],
    default='2048',
  )
  match: bpy.props.EnumProperty(
    name='High-Low Matching',
    items=[
      ('BY_MESH_NAME', 'By Mesh Name', 'Match names such as rock_low and rock_high'),
      ('ALWAYS', 'Always', 'Every high-poly mesh can project to every low-poly mesh'),
    ],
    default='BY_MESH_NAME',
  )
  antialiasing: bpy.props.EnumProperty(
    name='Antialiasing',
    items=[
      ('NONE', 'None', 'Do not supersample baked mesh maps'),
      ('X2', 'x2', 'Bake mesh maps with 2x antialiasing'),
      ('X4', 'x4', 'Bake mesh maps with 4x antialiasing'),
      ('X8', 'x8', 'Bake mesh maps with 8x antialiasing'),
    ],
    default='NONE',
  )
  id_source: bpy.props.EnumProperty(
    name='ID Source',
    items=[
      (
        'FACE_SETS',
        'High Face Sets',
        'Convert High-poly Sculpt Face Sets to stable temporary vertex colors',
      ),
      (
        'VERTEX_COLOR',
        'Existing Vertex Color',
        'Use the High-poly mesh vertex color attribute without replacing it',
      ),
      (
        'MATERIAL_COLOR',
        'Material Color',
        'Use High-poly material colors in Painter ID baking',
      ),
    ],
    default='FACE_SETS',
  )
  alpha_cage_extrusion: bpy.props.FloatProperty(
    name='Alpha Cage',
    description='Selected-to-active cage extrusion used only for Bake Alpha Details',
    default=0.004,
    min=0.0,
    soft_max=0.02,
    precision=4,
    unit='LENGTH',
  )
  alpha_max_ray_distance: bpy.props.FloatProperty(
    name='Alpha Ray',
    description='Maximum ray distance used only for Bake Alpha Details; use a small value to avoid projection bleed',
    default=0.02,
    min=0.0,
    soft_max=0.05,
    precision=4,
    unit='LENGTH',
  )
  base_color_source: bpy.props.EnumProperty(
    name='Base Color Source',
    items=[
      ('PAINTER', 'Painter', 'Use the final Base Color exported by Painter'),
      ('BAKING', 'Baking', 'Use the High-to-Low baked Base Color'),
    ],
    default='BAKING',
  )

class SubstanceToolsPreferences(bpy.types.AddonPreferences):
  bl_idname = ADDON_MODULE_NAME

  painter_path: bpy.props.StringProperty(name='Substance Painter Executable', default=_DETECTED_PAINTER_PATH, subtype='FILE_PATH')

  def draw(self, context):
    layout = self.layout
    layout.prop(self, 'painter_path')
