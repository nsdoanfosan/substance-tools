bl_info = {
  'name': 'Substance Import-Export Tools',
  'version': (3, 2, 0),
  'author': 'passivestar',
  'blender': (4, 1, 0),
  'location': '3D View N Panel',
  'description': 'Simplifies Export to Substance Painter',
  'category': 'Import-Export'
}

import bpy

from . import api

from .core import (
  alpha_target_material_items,
  ensure_baking_collections_deferred,
  ensure_baking_collections_on_load,
  sync_exclusive_baking_roles_on_depsgraph,
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
from .meshy_pipeline import (
  CLASSES as MESHY_PIPELINE_CLASSES,
  register_scene_properties as register_meshy_scene_properties,
  unregister_scene_properties as unregister_meshy_scene_properties,
)
from .meshy_source_maps import classes as MESHY_SOURCE_MAP_CLASSES
from .properties import (
  SubstanceToolsBakingSettings,
  SubstanceToolsPreferences,
  TextureSetBakeItem,
)
from .ui import (
  SubstanceToolsExportStatusPanel,
  SubstanceToolsMeshyPainterPanel,
  SubstanceToolsPanel,
)

classes = (
  TextureSetBakeItem,
  SubstanceToolsBakingSettings,
  *MESHY_PIPELINE_CLASSES,
  *MESHY_SOURCE_MAP_CLASSES,
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
  SubstanceToolsMeshyPainterPanel,
  SubstanceToolsPreferences,
)


def remove_handler_by_name(handler_list, handler):
  handler_name = getattr(handler, '__name__', '')
  handler_module = getattr(handler, '__module__', '')
  for existing in list(handler_list):
    if (
      getattr(existing, '__name__', '') == handler_name
      and getattr(existing, '__module__', '') == handler_module
    ):
      handler_list.remove(existing)


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
  register_meshy_scene_properties()
  bpy.types.Scene.substance_tools_bake_selection = bpy.props.CollectionProperty(
    type=TextureSetBakeItem
  )
  remove_handler_by_name(
    bpy.app.handlers.load_post,
    ensure_baking_collections_on_load,
  )
  bpy.app.handlers.load_post.append(ensure_baking_collections_on_load)
  remove_handler_by_name(
    bpy.app.handlers.depsgraph_update_post,
    sync_exclusive_baking_roles_on_depsgraph,
  )
  bpy.app.handlers.depsgraph_update_post.append(sync_exclusive_baking_roles_on_depsgraph)
  if not bpy.app.timers.is_registered(ensure_baking_collections_deferred):
    bpy.app.timers.register(ensure_baking_collections_deferred, first_interval=0.0)


def unregister():
  if bpy.app.timers.is_registered(ensure_baking_collections_deferred):
    bpy.app.timers.unregister(ensure_baking_collections_deferred)
  remove_handler_by_name(
    bpy.app.handlers.depsgraph_update_post,
    sync_exclusive_baking_roles_on_depsgraph,
  )
  remove_handler_by_name(
    bpy.app.handlers.load_post,
    ensure_baking_collections_on_load,
  )
  if hasattr(bpy.types.Scene, 'substance_tools_bake_selection'):
    del bpy.types.Scene.substance_tools_bake_selection
  unregister_meshy_scene_properties()
  if hasattr(bpy.types.Scene, 'substance_tools_baking'):
    del bpy.types.Scene.substance_tools_baking
  if hasattr(bpy.types.Object, 'substance_tools_alpha_target_material'):
    del bpy.types.Object.substance_tools_alpha_target_material
  for c in reversed(classes):
    bpy.utils.unregister_class(c)


if __name__ == '__main__':
  register()
