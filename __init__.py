bl_info = {
  'name': 'Substance Import-Export Tools',
  'version': (3, 0, 0),
  'author': 'passivestar',
  'blender': (4, 1, 0),
  'location': '3D View N Panel',
  'description': 'Simplifies Export to Substance Painter',
  'category': 'Import-Export'
}

import bpy

from .core import (
  alpha_target_material_items,
  ensure_baking_collections_deferred,
  ensure_baking_collections_on_load,
)
from .operators import (
  BakeAllInPainterOperator,
  BakeAlphaDetailsToLowOperator,
  BakeBaseColorToLowOperator,
  BakeSelectedInPainterOperator,
  ExportBakingToSubstancePainterOperator,
  ExportPainterTexturesAndApplyOperator,
  GroupSelectedMeshesOperator,
  PairSelectedBakingMeshesOperator,
  RefreshBakeSelectionOperator,
  ReloadMeshOperator,
  SelectExportStatusObjectOperator,
  SendPainterMapsOperator,
  StripMaterialPrefixOperator,
  ToggleBaseColorSourceOperator,
  ToggleExportLinkOperator,
)
from .properties import (
  SubstanceToolsBakingSettings,
  SubstanceToolsPreferences,
  TextureSetBakeItem,
)
from .ui import SubstanceToolsExportStatusPanel, SubstanceToolsPanel

classes = (
  TextureSetBakeItem,
  SubstanceToolsBakingSettings,
  PairSelectedBakingMeshesOperator,
  GroupSelectedMeshesOperator,
  ToggleExportLinkOperator,
  ExportBakingToSubstancePainterOperator,
  ReloadMeshOperator,
  StripMaterialPrefixOperator,
  BakeAllInPainterOperator,
  RefreshBakeSelectionOperator,
  BakeSelectedInPainterOperator,
  BakeBaseColorToLowOperator,
  BakeAlphaDetailsToLowOperator,
  SendPainterMapsOperator,
  ExportPainterTexturesAndApplyOperator,
  ToggleBaseColorSourceOperator,
  SelectExportStatusObjectOperator,
  SubstanceToolsPanel,
  SubstanceToolsExportStatusPanel,
  SubstanceToolsPreferences,
)


def register():
  for c in classes:
    bpy.utils.register_class(c)
  bpy.types.Object.substance_tools_alpha_target_material = bpy.props.EnumProperty(
    name='Target Material',
    description='Low material / Painter Texture Set that receives this alpha detail',
    items=alpha_target_material_items,
  )
  bpy.types.Scene.substance_tools_baking = bpy.props.PointerProperty(
    type=SubstanceToolsBakingSettings
  )
  bpy.types.Scene.substance_tools_bake_selection = bpy.props.CollectionProperty(
    type=TextureSetBakeItem
  )
  if ensure_baking_collections_on_load not in bpy.app.handlers.load_post:
    bpy.app.handlers.load_post.append(ensure_baking_collections_on_load)
  if not bpy.app.timers.is_registered(ensure_baking_collections_deferred):
    bpy.app.timers.register(ensure_baking_collections_deferred, first_interval=0.0)


def unregister():
  if bpy.app.timers.is_registered(ensure_baking_collections_deferred):
    bpy.app.timers.unregister(ensure_baking_collections_deferred)
  if ensure_baking_collections_on_load in bpy.app.handlers.load_post:
    bpy.app.handlers.load_post.remove(ensure_baking_collections_on_load)
  if hasattr(bpy.types.Scene, 'substance_tools_bake_selection'):
    del bpy.types.Scene.substance_tools_bake_selection
  if hasattr(bpy.types.Scene, 'substance_tools_baking'):
    del bpy.types.Scene.substance_tools_baking
  if hasattr(bpy.types.Object, 'substance_tools_alpha_target_material'):
    del bpy.types.Object.substance_tools_alpha_target_material
  for c in reversed(classes):
    bpy.utils.unregister_class(c)


if __name__ == '__main__':
  register()
