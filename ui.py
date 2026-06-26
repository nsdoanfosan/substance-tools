from .core import *
class SubstanceToolsPanel(bpy.types.Panel):
  """Substance Tools Panel"""
  bl_idname = 'SCENE_PT_substance_tools'
  bl_label = 'Substance Painter Tools'
  bl_space_type = 'VIEW_3D'
  bl_region_type = 'UI'
  bl_category = 'Substance'

  def draw(self, context):
    layout = self.layout
    baking = context.scene.substance_tools_baking

    baking_box = layout.box()
    baking_box.label(text='High to Low Baking', icon='RENDER_STILL')
    baking_box.operator(
      'st.pair_selected_baking_meshes',
      text='Pair Selected Low + High',
      icon='UV_SYNC_SELECT',
    )
    baking_box.operator(
      'st.group_selected_meshes',
      text='Group Selected Meshes',
      icon='OUTLINER_OB_EMPTY',
    )
    baking_box.operator(
      'st.toggle_export_link',
      text='Toggle Export Link',
      icon='LINKED',
    )
    baking_box.prop(baking, 'resolution')
    baking_box.prop(baking, 'antialiasing')
    baking_box.prop(baking, 'match')
    baking_box.prop(baking, 'id_source')
    _, low_collection, high_collection, alpha_collection = get_baking_collections()
    low_objects = collection_meshes(low_collection) if low_collection else []
    alpha_objects = collection_meshes(alpha_collection) if alpha_collection else []
    alpha_ids = {obj.as_pointer() for obj in alpha_objects}
    high_objects = [
      obj for obj in (collection_meshes(high_collection) if high_collection else [])
      if obj.as_pointer() not in alpha_ids
    ]
    painter_project_exists = baking_paths()['spp'].is_file() if bpy.data.filepath else False
    baking_box.operator(
      'st.bake_base_color_to_low',
      text='Bake Base Color',
      icon='IMAGE_DATA',
    )
    row = baking_box.row(align=True)
    row.prop(baking, 'alpha_cage_extrusion')
    row.prop(baking, 'alpha_max_ray_distance')
    baking_box.operator(
      'st.bake_alpha_details_to_low',
      text='Bake Alpha Details',
      icon='IMAGE_DATA',
    )
    send_maps_row = baking_box.row()
    send_maps_row.enabled = painter_project_exists
    send_maps_row.operator(
      'st.send_painter_maps',
      text='Send Base Color & Detail',
      icon='EXPORT',
    )
    if alpha_objects:
      alpha_box = baking_box.box()
      alpha_box.label(text='Alpha Targets', icon='IMAGE_DATA')
      for alpha_object in alpha_objects:
        row = alpha_box.row(align=True)
        if not matching_low_objects(alpha_object, low_objects):
          row.alert = True
          row.label(text=f'{alpha_object.name}: no matching _low')
          continue
        row.prop(
          alpha_object,
          'substance_tools_alpha_target_material',
          text=alpha_object.name,
        )
    primary_action = baking_box.operator(
      'st.export_baking_to_substance_painter',
      text='Open Painter Project' if painter_project_exists else 'Create in Painter',
      icon='WINDOW',
    )
    primary_action.action = 'OPEN' if painter_project_exists else 'CREATE'
    bake_sets_box = baking_box.box()
    header = bake_sets_box.row(align=True)
    header.label(text='Bake Sets', icon='RESTRICT_RENDER_OFF')
    header.operator('st.refresh_bake_selection', text='', icon='FILE_REFRESH')
    selection = context.scene.substance_tools_bake_selection
    low_as_high = set(low_as_high_texture_set_names(low_objects, high_objects))
    if len(selection) == 0:
      hint = bake_sets_box.row()
      hint.enabled = False
      hint.label(text='Press refresh to list Texture Sets')
    else:
      for item in selection:
        item_row = bake_sets_box.row(align=True)
        item_row.prop(item, 'bake', text='')
        item_row.label(text=item.name, icon='MATERIAL')
        mode = item_row.row()
        mode.alignment = 'RIGHT'
        mode.label(text='Low as High' if item.name in low_as_high else 'High -> Low')
    bake_selected_row = bake_sets_box.row()
    bake_selected_row.enabled = painter_project_exists and len(selection) > 0
    bake_selected_row.operator(
      'st.bake_selected_in_painter',
      text='Bake Selected',
      icon='RENDER_STILL',
    )
    bake_all_row = baking_box.row()
    bake_all_row.enabled = painter_project_exists
    bake_all_row.operator(
      'st.bake_all_in_painter',
      text='Bake All (Low + High)',
      icon='RENDER_STILL',
    )
    reload_row = baking_box.row()
    reload_row.enabled = painter_project_exists
    reload_row.operator(
      'st.reload_mesh',
      text='Reload Mesh',
      icon='FILE_REFRESH',
    )
    strip_row = baking_box.row()
    strip_row.enabled = painter_project_exists
    strip_row.operator(
      'st.strip_material_prefix',
      text='Strip M_ Prefix',
      icon='SORTALPHA',
    )
    export_row = baking_box.row()
    export_row.enabled = painter_project_exists
    export_row.operator(
      'st.export_painter_textures_and_apply',
      text='Export Painter Textures & Apply',
      icon='TEXTURE',
    )
    baking_box.label(text=f'Base Color Source: {baking.base_color_source.title()}')
    switch_label = (
      'Use Baked Base Color'
      if baking.base_color_source == 'PAINTER'
      else 'Use Painter Base Color'
    )
    baking_box.operator(
      'st.toggle_base_color_source',
      text=switch_label,
      icon='FILE_REFRESH',
    )


class SubstanceToolsExportStatusPanel(bpy.types.Panel):
  """Read-only visual classification of the Send to Unreal export set."""
  bl_idname = 'SCENE_PT_substance_tools_export_status'
  bl_label = 'Export Status'
  bl_space_type = 'VIEW_3D'
  bl_region_type = 'UI'
  bl_category = 'Substance'
  bl_parent_id = 'SCENE_PT_substance_tools'
  bl_options = {'DEFAULT_CLOSED'}

  @staticmethod
  def hierarchy_rows(objects):
    object_set = set(objects)
    children = {
      obj: sorted(
        (child for child in obj.children if child in object_set),
        key=lambda child: child.name.casefold(),
      )
      for obj in object_set
    }
    roots = sorted(
      (obj for obj in object_set if obj.parent not in object_set),
      key=lambda obj: obj.name.casefold(),
    )
    rows = []
    visited = set()

    def append_branch(obj, depth):
      if obj in visited:
        return
      visited.add(obj)
      rows.append((obj, depth))
      for child in children[obj]:
        append_branch(child, depth + 1)

    for root in roots:
      append_branch(root, 0)
    for obj in sorted(object_set - visited, key=lambda item: item.name.casefold()):
      append_branch(obj, 0)
    return rows

  @staticmethod
  def draw_group(layout, label, objects, icon):
    box = layout.box()
    box.label(text=f'{label} ({len(objects)})', icon=icon)
    if not objects:
      row = box.row()
      row.enabled = False
      row.label(text='None')
      return

    object_set = set(objects)
    for obj, depth in SubstanceToolsExportStatusPanel.hierarchy_rows(objects):
      row = box.row(align=True)
      for _ in range(depth):
        row.separator(factor=0.7)
      if depth:
        row.label(text='', icon='DISCLOSURE_TRI_RIGHT')
      display_name = obj.name
      if depth == 0 and obj.parent is not None and obj.parent not in object_set:
        display_name = f'{obj.parent.name} \u25b8 {obj.name}'
      is_send2ue_asset = obj.type in {'MESH', 'ARMATURE', 'CURVES'}
      if is_send2ue_asset and not obj.visible_get():
        display_name += ' [Hidden - Not Exported]'
        row.alert = True
      if obj.type == 'EMPTY':
        row.label(text=display_name, icon='OUTLINER_OB_EMPTY')
        continue
      operator = row.operator(
        'st.select_export_status_object',
        text=display_name,
        icon='OBJECT_DATA',
        emboss=False,
      )
      operator.object_name = obj.name

  def draw(self, context):
    layout = self.layout
    export_collection = bpy.data.collections.get(SEND2UE_EXPORT_COLLECTION)
    if export_collection is None:
      layout.label(text="No 'Export' collection", icon='INFO')
      return

    low_auto, linked, export_only = export_status_groups()
    export_objects = tuple(export_collection.all_objects)
    hidden_count = sum(
      obj.type in {'MESH', 'ARMATURE', 'CURVES'} and not obj.visible_get()
      for obj in export_objects
    )
    layout.label(
      text=f'{len(export_objects)} Export member(s)',
      icon='EXPORT',
    )
    if hidden_count:
      warning = layout.row()
      warning.alert = True
      warning.label(
        text=f'{hidden_count} hidden object(s) will not export',
        icon='ERROR',
      )
    self.draw_group(layout, 'Low Auto', low_auto, 'COLORSET_03_VEC')
    self.draw_group(layout, 'Linked', linked, 'COLORSET_04_VEC')
    self.draw_group(layout, 'Export Only', export_only, 'COLORSET_01_VEC')
