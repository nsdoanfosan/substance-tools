from .core import *
class PairSelectedBakingMeshesOperator(bpy.types.Operator):
  """Rename one selected Low and one or more selected High meshes as a bake pair"""
  bl_idname = 'st.pair_selected_baking_meshes'
  bl_label = 'Pair Selected Low + High'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    _, low_collection, high_collection, _ = ensure_baking_collections(
      context.scene
    )
    selected = {obj for obj in context.selected_objects if obj.type == 'MESH'}
    low_selected = [
      obj for obj in collection_meshes(low_collection) if obj in selected
    ]
    high_selected = [
      obj for obj in collection_meshes(high_collection) if obj in selected
    ]
    if len(low_selected) > 1:
      self.report(
        {'ERROR'},
        'Multiple Low meshes are selected. Select exactly one Low mesh',
      )
      return {'CANCELLED'}
    if len(low_selected) != 1:
      self.report({'ERROR'}, 'Select one mesh from Baking/low')
      return {'CANCELLED'}
    if not high_selected:
      self.report({'ERROR'}, 'Select at least one mesh from Baking/high')
      return {'CANCELLED'}
    low_object = low_selected[0]
    if low_object in high_selected:
      self.report({'ERROR'}, 'Low and High must be different mesh objects')
      return {'CANCELLED'}
    base_name = clean_name(object_role_base(low_object.name, 'low'))
    low_object.name = f'{base_name}_low'
    high_selected = sorted(high_selected, key=lambda obj: obj.name_full)
    if len(high_selected) == 1:
      high_selected[0].name = f'{base_name}_high'
    else:
      for index, high_object in enumerate(high_selected, 1):
        high_object.name = f'{base_name}_high_{index:02d}'

    normalized = 0
    if not any(material is not None for material in low_object.data.materials):
      create_material_for_object(low_object)
      low_object.data.materials[0].name = f'M_{base_name}'
      normalized += 1
    for material in {
      material for material in low_object.data.materials if material is not None
    }:
      target_name = f'M_{stripped_material_name(material.name)}'
      if material.name != target_name:
        material.name = target_name
        normalized += 1
    self.report(
      {'INFO'},
      f'Paired {low_object.name} with {len(high_selected)} High mesh(es); '
      f'normalized {normalized} Low material name(s)',
    )
    return {'FINISHED'}


class GroupSelectedMeshesOperator(bpy.types.Operator):
  """Group selected meshes under an Empty.

  If one Empty is included in the selection, every selected mesh is moved under
  it (even meshes already parented to another Empty). Otherwise a new Empty is
  created from the active mesh name; in that case meshes already inside an Empty
  are rejected.
  """
  bl_idname = 'st.group_selected_meshes'
  bl_label = 'Group Selected Meshes'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    selected = list(context.selected_objects)
    meshes = [obj for obj in selected if obj.type == 'MESH']
    empties = [obj for obj in selected if obj.type == 'EMPTY']

    if not meshes:
      self.report({'ERROR'}, 'Select at least one mesh')
      return {'CANCELLED'}
    if len(empties) > 1:
      self.report({'ERROR'}, 'Select at most one Empty as the group target')
      return {'CANCELLED'}

    target_empty = empties[0] if empties else None

    if target_empty is not None:
      # An Empty in the selection is an explicit target: move every selected
      # mesh under it, even meshes that already belong to another Empty.
      ordered = sorted(meshes, key=lambda obj: obj.name_full)
      added = 0
      for obj in ordered:
        if obj.parent == target_empty:
          continue
        world_matrix = obj.matrix_world.copy()
        obj.parent = target_empty
        obj.matrix_world = world_matrix
        added += 1

      bpy.ops.object.select_all(action='DESELECT')
      target_empty.select_set(True)
      for obj in ordered:
        obj.select_set(True)
      context.view_layer.objects.active = target_empty
      self.report(
        {'INFO'},
        f'Added {added} mesh(es) to {target_empty.name}',
      )
      return {'FINISHED'}

    active = context.view_layer.objects.active
    if active is None or active.type != 'MESH' or active not in meshes:
      self.report({'ERROR'}, 'Make one selected mesh active')
      return {'CANCELLED'}

    # No target Empty selected: refuse to build a new group from meshes that are
    # already inside an Empty (select that Empty to move them instead).
    for mesh in meshes:
      if mesh.parent is not None and mesh.parent.type == 'EMPTY':
        self.report(
          {'ERROR'},
          f'{mesh.name} is already inside an Empty: {mesh.parent.name}',
        )
        return {'CANCELLED'}

    original_active_name = re.sub(r'\.\d{3}$', '', active.name)
    group_name = clean_name(object_role_base(original_active_name, 'low'))
    ordered = [active] + sorted(
      (obj for obj in meshes if obj != active),
      key=lambda obj: obj.name_full,
    )
    existing_group = bpy.data.objects.get(group_name)
    if existing_group is not None and existing_group != active:
      self.report({'ERROR'}, f'Object name already exists: {group_name}')
      return {'CANCELLED'}

    _, low_collection, high_collection, alpha_collection = (
      ensure_baking_collections(context.scene)
    )
    preferred_collections = (
      low_collection,
      high_collection,
      alpha_collection,
    )
    target_collection = next(
      (
        collection for collection in preferred_collections
        if active.name in collection.all_objects
      ),
      active.users_collection[0] if active.users_collection else None,
    )
    if target_collection is None:
      target_collection = context.scene.collection

    active_world = active.matrix_world.copy()
    if active.name == group_name:
      child_name = f'{group_name}_child'
      existing_child = bpy.data.objects.get(child_name)
      if existing_child is not None and existing_child != active:
        self.report({'ERROR'}, f'Object name already exists: {child_name}')
        return {'CANCELLED'}
      active.name = child_name

    empty = bpy.data.objects.new(group_name, None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = 0.5
    target_collection.objects.link(empty)
    empty.matrix_world = active_world

    for obj in ordered:
      world_matrix = obj.matrix_world.copy()
      obj.parent = empty
      obj.matrix_world = world_matrix

    bpy.ops.object.select_all(action='DESELECT')
    empty.select_set(True)
    for obj in ordered:
      obj.select_set(True)
    context.view_layer.objects.active = empty
    self.report(
      {'INFO'},
      f'Grouped {len(ordered)} mesh(es) under {empty.name}',
    )
    return {'FINISHED'}


class ToggleExportLinkOperator(bpy.types.Operator):
  """Link or unlink the selected objects in the Send to Unreal 'Export' collection.

  Every selected object plus its whole child hierarchy (meshes, curves, empties,
  anything) is toggled together in one direction: if every gathered object is
  already in 'Export' they are all unlinked; otherwise the ones still missing are
  linked in (they stay in their current collection too). Unlinking never deletes
  an object — if 'Export' was its only home it is moved to the scene root so it
  stays visible. The Baking/low set used for the Painter export is never touched.
  """
  bl_idname = 'st.toggle_export_link'
  bl_label = 'Toggle Export Link'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    # Gather every selected object plus its full child hierarchy, regardless of
    # type, so a parented group links or unlinks as one unit.
    targets = set()
    for obj in context.selected_objects:
      targets.add(obj)
      targets.update(obj.children_recursive)
    if not targets:
      self.report({'ERROR'}, 'Select at least one object')
      return {'CANCELLED'}

    export_collection = bpy.data.collections.get(SEND2UE_EXPORT_COLLECTION)
    if export_collection is None:
      export_collection = bpy.data.collections.new(SEND2UE_EXPORT_COLLECTION)
      context.scene.collection.children.link(export_collection)

    ordered = sorted(targets, key=lambda o: o.name_full)
    low_auto = painter_low_export_hierarchy()
    protected = [obj for obj in ordered if obj in low_auto]
    ordered = [obj for obj in ordered if obj not in low_auto]
    if not ordered:
      self.report(
        {'WARNING'},
        'Low Auto objects are managed by Baking/low and cannot be toggled here',
      )
      return {'CANCELLED'}

    # One direction for the whole selection: if every object is already in
    # 'Export', unlink them all; otherwise link whatever is still missing.
    all_in_export = all(
      obj.name in export_collection.objects for obj in ordered
    )
    if all_in_export:
      for obj in ordered:
        export_collection.objects.unlink(obj)
        # Keep the object visible if 'Export' was its only collection.
        if not obj.users_collection:
          context.scene.collection.objects.link(obj)
      message = f"'Export': unlinked {len(ordered)} object(s)"
    else:
      linked = 0
      for obj in ordered:
        if obj.name not in export_collection.objects:
          export_collection.objects.link(obj)
          linked += 1
      message = f"'Export': linked {linked} object(s)"
    if protected:
      message += f'; skipped {len(protected)} Low Auto object(s)'
    self.report({'INFO'}, message)
    return {'FINISHED'}


class ExportBakingToSubstancePainterOperator(bpy.types.Operator):
  """Export Baking/low and Baking/high, then create or update the Painter project"""
  bl_idname = 'st.export_baking_to_substance_painter'
  bl_label = 'Send Baking Meshes to Substance Painter'
  bl_options = {'REGISTER'}

  action: bpy.props.EnumProperty(
    name='Action',
    items=[
      ('CREATE', 'Create in Painter', 'Create a new Painter project'),
      ('OPEN', 'Open Painter Project', 'Open the existing Painter project'),
      ('UPDATE', 'Update Painter', 'Update an existing Painter project'),
    ],
    default='CREATE',
    options={'HIDDEN'},
  )

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file before exporting')
      return {'CANCELLED'}

    paths = baking_paths()
    spp_existed = paths['spp'].exists()
    if self.action == 'OPEN':
      if not spp_existed:
        self.report({'ERROR'}, 'Painter project does not exist')
        return {'CANCELLED'}
      painter_path = get_preferences(context)['painter_path']
      if not painter_path or not Path(painter_path).is_file():
        self.report({'ERROR'}, 'Set a valid Substance Painter executable')
        return {'CANCELLED'}
      if painter_is_running(painter_path):
        self.report({'INFO'}, 'Substance Painter is already running')
        return {'FINISHED'}
      try:
        subprocess.Popen([painter_path, str(paths['spp'])])
      except Exception as error:
        self.report({'ERROR'}, f'Could not open Painter project: {error}')
        return {'CANCELLED'}
      self.report({'INFO'}, f'Opening Painter project: {paths["spp"].name}')
      return {'FINISHED'}

    _, low_collection, high_collection, alpha_collection = ensure_baking_collections(
      context.scene
    )
    low_objects = collection_meshes(low_collection)
    alpha_objects = collection_meshes(alpha_collection)
    alpha_ids = {obj.as_pointer() for obj in alpha_objects}
    high_objects = [
      obj for obj in collection_meshes(high_collection)
      if obj.as_pointer() not in alpha_ids
    ]
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}

    props = context.scene.substance_tools_baking
    low_as_high_texture_sets = low_as_high_texture_set_names(
      low_objects,
      high_objects,
    )
    if props.match == 'BY_MESH_NAME' and high_objects:
      unmatched_low, unmatched_high = unmatched_mesh_names(low_objects, high_objects)
      if unmatched_high:
        self.report(
          {'ERROR'},
          'Fix Baking high/low names before sending to Painter. '
          + 'High without Low: ' + ', '.join(unmatched_high),
        )
        return {'CANCELLED'}

    if self.action == 'CREATE' and spp_existed:
      self.report(
        {'ERROR'},
        'Painter project already exists; use Update Painter instead',
      )
      return {'CANCELLED'}
    if self.action == 'UPDATE' and not spp_existed:
      self.report(
        {'ERROR'},
        'Painter project does not exist; use Create in Painter first',
      )
      return {'CANCELLED'}

    for directory in (paths['low_dir'], paths['high_dir'], paths['texture_dir']):
      directory.mkdir(parents=True, exist_ok=True)

    settings = {
      'resolution': int(props.resolution),
      'antialiasing': props.antialiasing,
      'match': props.match,
      'cage': 'AUTOMATIC',
      'id_source': props.id_source,
      'low_as_high_texture_sets': low_as_high_texture_sets,
      'back_normal_mesh_maps': back_normal_mesh_map_plan(
        low_texture_set_names(low_objects),
        paths['texture_dir'],
      ),
      'mesh_maps': [
        'Normal', 'WorldSpaceNormal', 'ID', 'AO',
        'Curvature', 'Position', 'Thickness',
      ],
    }
    settings_hash = hashlib.sha256(
      json.dumps(settings, sort_keys=True).encode('utf-8')
    ).hexdigest()

    try:
      previous_request = read_json(paths['texture_dir'] / PAINTER_REQUEST, {})
      previous_low_hashes = previous_request.get('low_hashes', {})
      previous_high_hashes = previous_request.get('high_hashes', {})
      texture_sets = low_texture_set_names(low_objects)
      plan = load_bake_plan(paths, context)
      plan_valid = (
        self.action == 'UPDATE'
        and bool(plan)
        and 'low_hash' in plan
        and 'high_hashes' in plan
      )
      if self.action == 'UPDATE' and not plan_valid:
        self.report({'ERROR'}, 'Run Check Bake Plan before Update Painter')
        return {'CANCELLED'}
      if plan_valid:
        low_hash = plan.get('low_hash', '')
        low_hashes = dict(plan.get('low_hashes', {}))
        low_changed = bool(plan.get('low_changed'))
        changed_low_texture_sets = list(plan.get('changed_low_texture_sets', []))
        high_hashes = dict(plan.get('high_hashes', {}))
        changed_high_texture_sets = list(plan.get('changed_high_texture_sets', []))
        changed_back_normal_texture_sets = list(
          plan.get('changed_back_normal_texture_sets', [])
        )
        texture_sets = list(plan.get('texture_sets', texture_sets))
      else:
        low_hashes = {}
        changed_low_texture_sets = []
        for texture_set, objects in low_objects_by_texture_set(low_objects).items():
          texture_low_hash = fast_content_hash(
            objects,
            strip_material_prefix=True,
            id_source=props.id_source if not high_objects else 'NONE',
          )
          low_hashes[texture_set] = texture_low_hash
          if texture_low_hash != previous_low_hashes.get(texture_set):
            changed_low_texture_sets.append(texture_set)
        low_hash = hashlib.sha256(
          json.dumps(low_hashes, sort_keys=True).encode('utf-8')
        ).hexdigest()
        low_changed = bool(changed_low_texture_sets)
        high_hashes = {}
        changed_high_texture_sets = []
        changed_back_normal_texture_sets = []
      high_hash_cache = {}
      if low_changed or not paths['low_fbx'].is_file():
        export_objects_to_fbx(
          low_objects,
          paths['low_fbx'],
          strip_material_prefix=False,
          id_source=props.id_source if not high_objects else 'NONE',
        )
      high_entries = []
      if high_objects:
        for entry in high_entries_by_texture_set(
          low_objects,
          high_objects,
          paths['high_dir'],
          paths['asset'],
        ):
          texture_set = entry['texture_set']
          if plan_valid and texture_set in high_hashes:
            high_hash = high_hashes.get(texture_set, '')
            changed = texture_set in changed_high_texture_sets
          elif plan_valid:
            raise RuntimeError(
              f"Check Bake Plan is missing High data for {texture_set}"
            )
          else:
            cache_key = tuple(obj.name_full for obj in entry['objects'])
            high_hash = high_hash_cache.get(cache_key)
            if high_hash is None:
              high_hash = fast_content_hash(
                entry['objects'],
                id_source=props.id_source,
              )
              high_hash_cache[cache_key] = high_hash
            high_hashes[texture_set] = high_hash
            changed = high_hash != previous_high_hashes.get(texture_set)
          missing_fbxs = [
            fbx for fbx in entry['fbxs']
            if not fbx.is_file()
          ]
          if changed or missing_fbxs:
            for obj, fbx in zip(entry['objects'], entry['fbxs']):
              if changed or not fbx.is_file():
                export_objects_to_fbx(
                  [obj],
                  fbx,
                  id_source=props.id_source,
                )
            if texture_set not in changed_high_texture_sets:
              changed_high_texture_sets.append(texture_set)
          resolved_fbxs = [
            str(fbx.resolve())
            for fbx in entry['fbxs']
          ]
          high_entries.append({
            'texture_set': texture_set,
            'bases': entry['bases'],
            'fbx': resolved_fbxs[0] if len(resolved_fbxs) == 1 else '',
            'fbxs': resolved_fbxs,
            'hash': high_hash,
            'changed': changed,
          })
      elif paths['high_fbx'].exists():
        paths['high_fbx'].unlink()
      baked_base_color = (
        paths['texture_dir'] / f'{base_color_bake_name(texture_sets[0])}.png'
        if len(texture_sets) == 1
        else None
      )
      base_color_maps = (
        {texture_sets[0]: str(baked_base_color.resolve())}
        if baked_base_color is not None and baked_base_color.is_file()
        else {}
      )
      alpha_color_maps = {
        texture_set: str(
          (
            paths['texture_dir']
            / f'{alpha_color_bake_name(texture_set)}.png'
          ).resolve()
        )
        for texture_set in texture_sets
        if (
          paths['texture_dir']
          / f'{alpha_color_bake_name(texture_set)}.png'
        ).is_file()
      }
    except Exception as error:
      self.report({'ERROR'}, f'Export or Base Color bake failed: {error}')
      traceback.print_exc()
      return {'CANCELLED'}

    settings_changed = settings_hash != previous_request.get('settings_hash', '')
    back_normal_mesh_maps = settings.get('back_normal_mesh_maps', {})
    back_normal_hashes = hash_existing_back_normal_sources(back_normal_mesh_maps)
    if not plan_valid:
      changed_back_normal_texture_sets = sorted(
        texture_set
        for texture_set in back_normal_mesh_maps
        if (
          texture_set in back_normal_hashes
          and back_normal_hashes.get(texture_set)
          != previous_request.get('back_normal_hashes', {}).get(texture_set)
        )
      )
    if settings_changed or self.action == 'CREATE' or not spp_existed:
      rebake_source = texture_sets
    else:
      rebake_source = list(changed_high_texture_sets)
      rebake_source.extend(
        texture_set for texture_set in changed_low_texture_sets
        if texture_set in low_as_high_texture_sets
      )
      rebake_source.extend(changed_back_normal_texture_sets)
    rebake_texture_sets = sorted(set(rebake_source))
    reload_only_texture_sets = sorted(
      set(changed_low_texture_sets) - set(rebake_texture_sets)
    )
    base_color_hashes = {
      texture_set: file_hash(Path(image_path))
      for texture_set, image_path in base_color_maps.items()
    }
    base_color_hash = hashlib.sha256(
      json.dumps(base_color_hashes, sort_keys=True).encode('utf-8')
    ).hexdigest()
    alpha_color_hashes = {
      texture_set: file_hash(Path(image_path))
      for texture_set, image_path in alpha_color_maps.items()
    }
    alpha_color_hash = hashlib.sha256(
      json.dumps(alpha_color_hashes, sort_keys=True).encode('utf-8')
    ).hexdigest()
    base_color_changed = (
      base_color_hashes != previous_request.get('base_color_hashes', {})
    )
    alpha_color_changed = (
      alpha_color_hashes != previous_request.get('alpha_color_hashes', {})
    )
    back_normal_changed = (
      back_normal_hashes != previous_request.get('back_normal_hashes', {})
    )
    no_painter_work_needed = (
      self.action == 'UPDATE'
      and not low_changed
      and not changed_high_texture_sets
      and not changed_back_normal_texture_sets
      and not settings_changed
      and not base_color_changed
      and not alpha_color_changed
      and not back_normal_changed
    )
    if no_painter_work_needed:
      preview = {
        'version': 1,
        'blend_file': str(Path(bpy.data.filepath).resolve()),
        'low_hash': low_hash,
        'low_hashes': low_hashes,
        'low_changed': False,
        'low_baseline_missing': False,
        'changed_low_texture_sets': [],
        'high_hashes': high_hashes,
        'changed_high_texture_sets': [],
        'changed_back_normal_texture_sets': [],
        'back_normal_hashes': back_normal_hashes,
        'rebake_texture_sets': [],
        'reload_only_texture_sets': [],
        'texture_sets': texture_sets,
        'settings_hash': settings_hash,
        'settings_changed': False,
      }
      context.scene['substance_tools_bake_plan_preview'] = json.dumps(
        preview,
        sort_keys=True,
      )
      write_json(paths['bake_plan'], preview)
      self.report({'INFO'}, 'No checked bake changes. Run Check Bake Plan after editing.')
      return {'FINISHED'}
    request = {
      'version': 1,
      'request_id': str(time.time_ns()),
      'action': self.action,
      'blend_file': str(Path(bpy.data.filepath).resolve()),
      'low_fbx': str(paths['low_fbx'].resolve()),
      'high_fbx': (
        high_entries[0]['fbx']
        if len(high_entries) == 1 and len(high_entries[0].get('fbxs', [])) == 1
        else ''
      ),
      'high_entries': high_entries,
      'spp': str(paths['spp'].resolve()),
      'texture_dir': str(paths['texture_dir'].resolve()),
      'low_hash': low_hash,
      'low_hashes': low_hashes,
      'low_changed': low_changed,
      'low_baseline_missing': False,
      'changed_low_texture_sets': changed_low_texture_sets,
      'high_hash': hashlib.sha256(
        json.dumps(high_hashes, sort_keys=True).encode('utf-8')
      ).hexdigest(),
      'high_hashes': high_hashes,
      'changed_high_texture_sets': changed_high_texture_sets,
      'changed_back_normal_texture_sets': changed_back_normal_texture_sets,
      'rebake_texture_sets': rebake_texture_sets,
      'reload_only_texture_sets': reload_only_texture_sets,
      'bake_plan_used': bool(plan_valid),
      'settings_hash': settings_hash,
      'settings_changed': settings_changed,
      'pipeline_hash': hashlib.sha256(
        (
          f"{low_hash}:{json.dumps(high_hashes, sort_keys=True)}:"
          f"{settings_hash}:{base_color_hash}:{alpha_color_hash}:"
          f"{json.dumps(back_normal_hashes, sort_keys=True)}"
        ).encode('utf-8')
      ).hexdigest(),
      'spp_existed': spp_existed,
      'base_color_maps': base_color_maps,
      'base_color_hashes': base_color_hashes,
      'base_color_changed': base_color_changed,
      'alpha_color_maps': alpha_color_maps,
      'alpha_color_hashes': alpha_color_hashes,
      'alpha_color_changed': alpha_color_changed,
      'back_normal_hashes': back_normal_hashes,
      'back_normal_changed': back_normal_changed,
      'settings': settings,
    }
    # Written to both the texture dir and the low dir because the Painter
    # plugin's _request_candidates() looks next to the open .spp (texture dir)
    # AND next to the last imported mesh (low dir); writing both guarantees a hit.
    request_paths = (
      paths['texture_dir'] / PAINTER_REQUEST,
      paths['low_dir'] / PAINTER_REQUEST,
    )
    for request_path in request_paths:
      write_json(request_path, request)

    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable in add-on preferences')
      return {'CANCELLED'}

    try:
      if self.action == 'CREATE':
        template_path = unreal_template_path(painter_path)
        if not template_path.is_file():
          self.report(
            {'ERROR'},
            f'Painter Unreal Engine template was not found: {template_path}',
          )
          return {'CANCELLED'}
        request['template'] = str(template_path)
        for request_path in request_paths:
          write_json(request_path, request)
        write_json(pending_request_path(), request)
        # The Painter startup plugin consumes the pending request and creates
        # the project through project.create(template_file_path=...).
        command = [painter_path]
        subprocess.Popen(command)
      elif not painter_is_running(painter_path):
        subprocess.Popen([painter_path, str(paths['spp'])])
    except Exception as error:
      self.report({'ERROR'}, f'Error opening Substance Painter: {error}')
      return {'CANCELLED'}

    clean_plan = {
      'version': 1,
      'blend_file': str(Path(bpy.data.filepath).resolve()),
      'texture_sets': texture_sets,
      'low_hash': low_hash,
      'low_hashes': low_hashes,
      'low_changed': False,
      'low_baseline_missing': False,
      'changed_low_texture_sets': [],
      'high_hashes': high_hashes,
      'changed_high_texture_sets': [],
      'changed_back_normal_texture_sets': [],
      'back_normal_hashes': back_normal_hashes,
      'rebake_texture_sets': [],
      'reload_only_texture_sets': [],
      'settings_hash': settings_hash,
      'settings_changed': False,
    }
    context.scene['substance_tools_bake_plan_preview'] = json.dumps(
      clean_plan,
      sort_keys=True,
    )
    write_json(paths['bake_plan'], clean_plan)

    if self.action == 'CREATE':
      self.report({'INFO'}, 'Creating a new Painter project')
    else:
      self.report(
        {'INFO'},
        'Painter update requested; the open project will reimport once',
      )
    return {'FINISHED'}


class ReloadMeshOperator(bpy.types.Operator):
  """Re-export the low FBX as-is. Reload it yourself inside Painter.

  Only writes <asset>_low.fbx (material names kept, no M_ stripping). Does NOT
  talk to Painter — load the FBX with Painter's Edit > Project Configuration
  (mesh reload). The automated reload request was dropped because it was flaky.
  """
  bl_idname = 'st.reload_mesh'
  bl_label = 'Reload Mesh'
  bl_options = {'REGISTER'}

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file before exporting the mesh')
      return {'CANCELLED'}

    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    low_objects = collection_meshes(low_collection)
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}

    paths = baking_paths()
    paths['low_dir'].mkdir(parents=True, exist_ok=True)
    try:
      export_objects_to_fbx(
        low_objects,
        paths['low_fbx'],
        strip_material_prefix=False,
      )
    except Exception as error:
      self.report({'ERROR'}, f'Could not export Low FBX: {error}')
      return {'CANCELLED'}

    self.report(
      {'INFO'},
      f'Low FBX exported: {paths["low_fbx"].name} — reload it in Painter',
    )
    return {'FINISHED'}


class StripMaterialPrefixOperator(bpy.types.Operator):
  """Ask the open Painter project to drop the M_ prefix from its Texture Set names

  Sends a one-shot STRIP_PREFIX request; the Painter plugin renames every
  Texture Set whose name still starts with M_ and saves the project. Safe to
  press repeatedly (already-clean names are left untouched).
  """
  bl_idname = 'st.strip_material_prefix'
  bl_label = 'Strip M_ Prefix'
  bl_options = {'REGISTER'}

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file first')
      return {'CANCELLED'}

    paths = baking_paths()
    if not paths['spp'].is_file():
      self.report({'ERROR'}, 'Painter project does not exist; use Create in Painter first')
      return {'CANCELLED'}

    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable in add-on preferences')
      return {'CANCELLED'}

    paths['texture_dir'].mkdir(parents=True, exist_ok=True)
    paths['low_dir'].mkdir(parents=True, exist_ok=True)
    request = {
      'version': 1,
      'request_id': str(time.time_ns()),
      'action': 'STRIP_PREFIX',
      'blend_file': str(Path(bpy.data.filepath).resolve()),
      'spp': str(paths['spp'].resolve()),
      'texture_dir': str(paths['texture_dir'].resolve()),
    }
    # Written to both the texture dir and the low dir because the Painter
    # plugin's _request_candidates() looks next to the open .spp (texture dir)
    # AND next to the last imported mesh (low dir).
    for request_path in (
      paths['texture_dir'] / PAINTER_REQUEST,
      paths['low_dir'] / PAINTER_REQUEST,
    ):
      write_json(request_path, request)

    if not painter_is_running(painter_path):
      subprocess.Popen([painter_path, str(paths['spp'])])

    self.report({'INFO'}, 'Strip M_ Prefix requested')
    return {'FINISHED'}


def send_painter_bake_request(context, selected=None):
  """Export meshes and ask Painter to reload + bake mesh maps via the JSON request.

  ``selected`` is a set of Texture Set names to bake; ``None`` bakes all of them.
  Each baked set is High-to-Low when it has a High pair, else Low-Poly-as-High
  (Painter decides per set). No incremental Bake Plan — bakes exactly what is asked.
  Raises RuntimeError with a user-facing message on any validation/export failure.
  Returns the number of Texture Sets queued for baking.
  """
  if not bpy.data.filepath:
    raise RuntimeError('Save the .blend file before baking')
  paths = baking_paths()
  if not paths['spp'].is_file():
    raise RuntimeError('Painter project does not exist; use Create in Painter first')
  painter_path = get_preferences(context)['painter_path']
  if not painter_path or not Path(painter_path).is_file():
    raise RuntimeError('Set a valid Substance Painter executable in add-on preferences')

  _, low_collection, high_collection, alpha_collection = ensure_baking_collections(
    context.scene
  )
  low_objects = collection_meshes(low_collection)
  if not low_objects:
    raise RuntimeError("The 'Baking/low' collection has no mesh objects")
  alpha_ids = {obj.as_pointer() for obj in collection_meshes(alpha_collection)}
  high_objects = [
    obj for obj in collection_meshes(high_collection)
    if obj.as_pointer() not in alpha_ids
  ]

  props = context.scene.substance_tools_baking
  all_texture_sets = low_texture_set_names(low_objects)
  if selected is None:
    bake_texture_sets = list(all_texture_sets)
  else:
    bake_texture_sets = [ts for ts in all_texture_sets if ts in selected]
  if not bake_texture_sets:
    raise RuntimeError('No Texture Sets selected to bake')
  bake_set = set(bake_texture_sets)

  for directory in (paths['low_dir'], paths['high_dir'], paths['texture_dir']):
    directory.mkdir(parents=True, exist_ok=True)

  settings = {
    'resolution': int(props.resolution),
    'antialiasing': props.antialiasing,
    'match': props.match,
    'cage': 'AUTOMATIC',
    'id_source': props.id_source,
    'low_as_high_texture_sets': low_as_high_texture_set_names(low_objects, high_objects),
    'back_normal_mesh_maps': back_normal_mesh_map_plan(all_texture_sets, paths['texture_dir']),
    'mesh_maps': [
      'Normal', 'WorldSpaceNormal', 'ID', 'AO',
      'Curvature', 'Position', 'Thickness',
    ],
  }

  export_objects_to_fbx(
    low_objects,
    paths['low_fbx'],
    strip_material_prefix=False,
    id_source=props.id_source if not high_objects else 'NONE',
  )
  high_entries = []
  if high_objects:
    for entry in high_entries_by_texture_set(
      low_objects, high_objects, paths['high_dir'], paths['asset']
    ):
      if entry['texture_set'] not in bake_set:
        continue
      for obj, fbx in zip(entry['objects'], entry['fbxs']):
        export_objects_to_fbx([obj], fbx, id_source=props.id_source)
      resolved_fbxs = [str(fbx.resolve()) for fbx in entry['fbxs']]
      high_entries.append({
        'texture_set': entry['texture_set'],
        'bases': entry['bases'],
        'fbx': resolved_fbxs[0] if len(resolved_fbxs) == 1 else '',
        'fbxs': resolved_fbxs,
      })

  request = {
    'version': 1,
    'request_id': str(time.time_ns()),
    'action': 'UPDATE',
    'blend_file': str(Path(bpy.data.filepath).resolve()),
    'low_fbx': str(paths['low_fbx'].resolve()),
    'high_fbx': (
      high_entries[0]['fbx']
      if len(high_entries) == 1 and len(high_entries[0].get('fbxs', [])) == 1
      else ''
    ),
    'high_entries': high_entries,
    'spp': str(paths['spp'].resolve()),
    'texture_dir': str(paths['texture_dir'].resolve()),
    'spp_existed': True,
    'low_changed': True,
    'rebake_texture_sets': bake_texture_sets,
    'reload_only_texture_sets': [],
    'texture_sets': all_texture_sets,
    'settings': settings,
    'pipeline_hash': f'bake-{time.time_ns()}',
  }
  for request_path in (
    paths['texture_dir'] / PAINTER_REQUEST,
    paths['low_dir'] / PAINTER_REQUEST,
  ):
    write_json(request_path, request)

  if not painter_is_running(painter_path):
    subprocess.Popen([painter_path, str(paths['spp'])])
  return len(bake_texture_sets)


class BakeAllInPainterOperator(bpy.types.Operator):
  """Bake every Texture Set in Painter now (Low + High), no selection needed"""
  bl_idname = 'st.bake_all_in_painter'
  bl_label = 'Bake All (Low + High)'
  bl_options = {'REGISTER'}

  def execute(self, context):
    try:
      count = send_painter_bake_request(context, selected=None)
    except Exception as error:
      self.report({'ERROR'}, str(error))
      traceback.print_exc()
      return {'CANCELLED'}
    self.report({'INFO'}, f'Baking all {count} Texture Set(s) in Painter')
    return {'FINISHED'}


class RefreshBakeSelectionOperator(bpy.types.Operator):
  """Rebuild the bake list from the current Baking/low Texture Sets"""
  bl_idname = 'st.refresh_bake_selection'
  bl_label = 'Refresh List'
  bl_options = {'REGISTER'}

  def execute(self, context):
    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    sync_bake_selection(
      context.scene,
      low_texture_set_names(collection_meshes(low_collection)),
    )
    return {'FINISHED'}


class BakeSelectedInPainterOperator(bpy.types.Operator):
  """Bake only the checked Texture Sets in Painter"""
  bl_idname = 'st.bake_selected_in_painter'
  bl_label = 'Bake Selected'
  bl_options = {'REGISTER'}

  def execute(self, context):
    scene = context.scene
    _, low_collection, _, _ = ensure_baking_collections(scene)
    sync_bake_selection(
      scene,
      low_texture_set_names(collection_meshes(low_collection)),
    )
    selected = {item.name for item in scene.substance_tools_bake_selection if item.bake}
    try:
      count = send_painter_bake_request(context, selected=selected)
    except Exception as error:
      self.report({'ERROR'}, str(error))
      traceback.print_exc()
      return {'CANCELLED'}
    self.report({'INFO'}, f'Baking {count} selected Texture Set(s) in Painter')
    return {'FINISHED'}


class BakeBaseColorToLowOperator(bpy.types.Operator):
  """Bake High-poly Base Color to the Low-poly UVs"""
  bl_idname = 'st.bake_base_color_to_low'
  bl_label = 'Bake Base Color'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file before baking')
      return {'CANCELLED'}

    _, low_collection, high_collection, alpha_collection = ensure_baking_collections(
      context.scene
    )
    low_objects = collection_meshes(low_collection)
    alpha_ids = {
      obj.as_pointer() for obj in collection_meshes(alpha_collection)
    }
    high_objects = [
      obj for obj in collection_meshes(high_collection)
      if obj.as_pointer() not in alpha_ids
    ]
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}
    if not high_objects:
      self.report({'ERROR'}, "The 'Baking/high' collection has no mesh objects")
      return {'CANCELLED'}

    texture_sets = low_texture_set_names(low_objects)
    if len(texture_sets) != 1:
      self.report(
        {'ERROR'},
        'Bake Base Color currently requires exactly one Low-poly Texture Set',
      )
      return {'CANCELLED'}
    if not high_has_base_color_textures(high_objects):
      self.report(
        {'ERROR'},
        'No image texture was found upstream of High-poly Principled Base Color',
      )
      return {'CANCELLED'}

    props = context.scene.substance_tools_baking
    paths = baking_paths()
    bake_name = base_color_bake_name(texture_sets[0])
    bake_filename = f'{bake_name}.png'
    try:
      result = bake_high_base_color_to_low(
        low_objects,
        high_objects,
        paths['texture_dir'],
        int(props.resolution),
        props.match,
        bake_filename,
      )
    except Exception as error:
      self.report({'ERROR'}, f'Base Color bake failed: {error}')
      traceback.print_exc()
      return {'CANCELLED'}

    if not result:
      self.report({'ERROR'}, 'No matching High/Low pair was baked')
      return {'CANCELLED'}

    baked_image = bpy.data.images.get(bake_name)
    if baked_image is None:
      self.report({'ERROR'}, f"The baked Blender image '{bake_name}' was not found")
      return {'CANCELLED'}
    final_color_path = (
      paths['texture_dir'] / f'T_{clean_name(texture_sets[0])}_Color.png'
    )
    should_connect = (
      not final_color_path.is_file()
      or props.base_color_source == 'BAKING'
    )
    connected = (
      connect_base_color_bake_to_low_materials(low_objects, baked_image)
      if should_connect
      else 0
    )
    if should_connect:
      props.base_color_source = 'BAKING'
    if not connected:
      self.report(
        {'INFO'},
        'Base Color bake updated; existing Painter Base Color connection was preserved',
      )
      return {'FINISHED'}

    self.report(
      {'INFO'},
      (
        f"Base Color baked to {paths['texture_dir'] / bake_filename} "
        f"and connected to {connected} Low material shader(s)"
      ),
    )
    return {'FINISHED'}


class BakeAlphaDetailsToLowOperator(bpy.types.Operator):
  """Bake Baking/alpha RGBA details to their matching Low Texture Sets"""
  bl_idname = 'st.bake_alpha_details_to_low'
  bl_label = 'Bake Alpha Details'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file before baking')
      return {'CANCELLED'}
    _, low_collection, _, alpha_collection = ensure_baking_collections(
      context.scene
    )
    low_objects = collection_meshes(low_collection)
    alpha_objects = collection_meshes(alpha_collection)
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}
    if not alpha_objects:
      self.report({'ERROR'}, "The 'Baking/alpha' collection has no mesh objects")
      return {'CANCELLED'}
    props = context.scene.substance_tools_baking
    paths = baking_paths()
    try:
      result = bake_alpha_details_to_low(
        low_objects,
        alpha_objects,
        paths['texture_dir'],
        int(props.resolution),
        props.alpha_cage_extrusion,
        props.alpha_max_ray_distance,
      )
    except Exception as error:
      self.report({'ERROR'}, f'Alpha detail bake failed: {error}')
      traceback.print_exc()
      return {'CANCELLED'}
    connected = 0
    for texture_set, image_path in result.items():
      image = load_or_reload_image(image_path)
      for material in {
        material
        for low in low_objects
        for material in low.data.materials
        if material is not None
        and stripped_material_name(material.name) == texture_set
      }:
        connected += connect_alpha_bake_to_material(
          material,
          image,
          enabled=props.base_color_source == 'BAKING',
        )
    self.report(
      {'INFO'},
      f'Baked {len(result)} alpha texture(s) and connected {connected} shader(s)',
    )
    return {'FINISHED'}


class SendPainterMapsOperator(bpy.types.Operator):
  """Push baked Base Color / Alpha Detail maps to Painter as fill layers

  Looks for the baked Base Color and Alpha Detail PNGs in the texture folder and
  asks Painter (action=APPLY_MAPS) to (re)apply them as fill layers and save.
  Whatever exists is updated; whatever is missing is skipped. No baking, no reload.
  """
  bl_idname = 'st.send_painter_maps'
  bl_label = 'Send Base Color & Detail'
  bl_options = {'REGISTER'}

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file first')
      return {'CANCELLED'}
    paths = baking_paths()
    if not paths['spp'].is_file():
      self.report({'ERROR'}, 'Painter project does not exist; use Create in Painter first')
      return {'CANCELLED'}
    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable in add-on preferences')
      return {'CANCELLED'}

    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    texture_sets = low_texture_set_names(collection_meshes(low_collection))

    baked_base_color = (
      paths['texture_dir'] / f'{base_color_bake_name(texture_sets[0])}.png'
      if len(texture_sets) == 1
      else None
    )
    base_color_maps = (
      {texture_sets[0]: str(baked_base_color.resolve())}
      if baked_base_color is not None and baked_base_color.is_file()
      else {}
    )
    alpha_color_maps = {
      texture_set: str(
        (paths['texture_dir'] / f'{alpha_color_bake_name(texture_set)}.png').resolve()
      )
      for texture_set in texture_sets
      if (paths['texture_dir'] / f'{alpha_color_bake_name(texture_set)}.png').is_file()
    }
    if not base_color_maps and not alpha_color_maps:
      self.report({'INFO'}, 'No baked Base Color / Alpha maps found to send')
      return {'CANCELLED'}

    for directory in (paths['texture_dir'], paths['low_dir']):
      directory.mkdir(parents=True, exist_ok=True)
    request = {
      'version': 1,
      'request_id': str(time.time_ns()),
      'action': 'APPLY_MAPS',
      'blend_file': str(Path(bpy.data.filepath).resolve()),
      'spp': str(paths['spp'].resolve()),
      'texture_dir': str(paths['texture_dir'].resolve()),
      'base_color_maps': base_color_maps,
      'alpha_color_maps': alpha_color_maps,
    }
    for request_path in (
      paths['texture_dir'] / PAINTER_REQUEST,
      paths['low_dir'] / PAINTER_REQUEST,
    ):
      write_json(request_path, request)
    if not painter_is_running(painter_path):
      subprocess.Popen([painter_path, str(paths['spp'])])

    self.report(
      {'INFO'},
      f'Sent {len(base_color_maps)} Base Color + {len(alpha_color_maps)} Alpha map(s) to Painter',
    )
    return {'FINISHED'}


class ExportPainterTexturesAndApplyOperator(bpy.types.Operator):
  """Export with Painter Unreal_V2, then connect the results to Low materials"""
  bl_idname = 'st.export_painter_textures_and_apply'
  bl_label = 'Export Painter Textures & Apply'
  bl_options = {'REGISTER', 'UNDO'}

  _timer = None
  _request_id = ''
  _deadline = 0.0
  TIMEOUT_SECONDS = 300

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file first')
      return {'CANCELLED'}
    paths = baking_paths()
    if not paths['spp'].is_file():
      self.report({'ERROR'}, 'Create the Painter project first')
      return {'CANCELLED'}
    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    if not collection_meshes(low_collection):
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}
    try:
      preset_path = ensure_painter_export_preset()
    except Exception as error:
      self.report({'ERROR'}, f'Painter export preset install failed: {error}')
      return {'CANCELLED'}

    self._request_id = str(time.time_ns())
    request_path = paths['texture_dir'] / PAINTER_EXPORT_REQUEST
    result_path = paths['texture_dir'] / PAINTER_EXPORT_RESULT
    if result_path.is_file():
      result_path.unlink()
    write_json(request_path, {
      'request_id': self._request_id,
      'spp': str(paths['spp'].resolve()),
      'texture_dir': str(paths['texture_dir'].resolve()),
      'preset': EXPORT_PRESET_NAME,
      'preset_path': str(preset_path.resolve()),
    })

    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable')
      return {'CANCELLED'}
    if not painter_is_running(painter_path):
      subprocess.Popen([painter_path, str(paths['spp'])])

    self._deadline = time.time() + self.TIMEOUT_SECONDS
    self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
    context.window_manager.modal_handler_add(self)
    self.report({'INFO'}, 'Painter texture export requested')
    return {'RUNNING_MODAL'}

  def modal(self, context, event):
    if event.type != 'TIMER':
      return {'PASS_THROUGH'}
    if time.time() > self._deadline:
      context.window_manager.event_timer_remove(self._timer)
      self._timer = None
      self.report(
        {'ERROR'},
        f'Painter 텍스처 익스포트 응답이 {self.TIMEOUT_SECONDS // 60}분 내에 오지 '
        '않았습니다. Painter가 실행 중이고 프로젝트가 열려 있는지 확인하세요 '
        '(Painter export timed out)',
      )
      return {'CANCELLED'}
    result_path = baking_paths()['texture_dir'] / PAINTER_EXPORT_RESULT
    if not result_path.is_file():
      return {'PASS_THROUGH'}
    try:
      result = json.loads(result_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
      return {'PASS_THROUGH'}
    if result.get('request_id') != self._request_id:
      return {'PASS_THROUGH'}
    context.window_manager.event_timer_remove(self._timer)
    self._timer = None
    if result.get('status') != 'SUCCESS':
      self.report({'ERROR'}, result.get('message', 'Painter texture export failed'))
      return {'CANCELLED'}

    canonicalize_painter_export_files(result)
    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    low_objects = collection_meshes(low_collection)
    applied = apply_painter_textures_to_low(
      low_objects,
      baking_paths()['texture_dir'],
    )
    if applied == 0:
      # Apply looks files up as T_<material-without-M_>_<role>.png. If Painter's
      # Texture Set names no longer match the Blender material names (e.g. the
      # material was renamed after the Painter project was created and never
      # reimported), nothing matches. Surface that clearly instead of a silent "0".
      painter_sets = sorted({key.split('/')[0] for key in result.get('textures', {}) if key})
      expected = sorted({
        clean_name(stripped_material_name(slot.material.name))
        for obj in low_objects
        for slot in obj.material_slots
        if slot.material
      })
      print(f'[Substance Tools] apply 0개 (이름 불일치 가능): Painter={painter_sets}, 기대={expected}')
      self.report(
        {'WARNING'},
        '텍스처를 0개 적용했습니다 — Painter Texture Set 이름과 Blender 머티리얼 이름이 '
        f'어긋났을 수 있습니다. Painter={painter_sets} vs 머티리얼(M_ 제외)={expected}. '
        "substance-tools 패널의 'Update Painter'로 low를 리임포트해 이름을 맞춘 뒤 다시 "
        '실행하세요. (머티리얼 이름 변경은 Painter 왕복을 모두 끝낸 뒤에 하세요) '
        '(applied 0 textures: Painter Texture Set names may differ from material names)',
      )
      return {'FINISHED'}
    context.scene.substance_tools_baking.base_color_source = 'PAINTER'
    self.report(
      {'INFO'},
      f'완료 (done): Painter 텍스처를 Base Color 셰이더 {applied}개에 적용 '
      f'(exported & applied to {applied} shader(s))',
    )
    return {'FINISHED'}

  def cancel(self, context):
    if self._timer is not None:
      context.window_manager.event_timer_remove(self._timer)
      self._timer = None


class ToggleBaseColorSourceOperator(bpy.types.Operator):
  """Switch Low materials between Painter and baked High Base Color"""
  bl_idname = 'st.toggle_base_color_source'
  bl_label = 'Switch Base Color Source'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    low_objects = collection_meshes(low_collection)
    props = context.scene.substance_tools_baking
    target = 'BAKING' if props.base_color_source == 'PAINTER' else 'PAINTER'
    texture_dir = baking_paths()['texture_dir']
    resolution = int(props.resolution)
    materials = {
      slot.material
      for obj in low_objects
      for slot in obj.material_slots
      if slot.material
    }
    connected = 0
    applied_sets = set()
    missing_sets = set()
    for material in materials:
      material_texture_set = clean_name(stripped_material_name(material.name))
      filename = (
        f'{base_color_bake_name(material_texture_set)}.png'
        if target == 'BAKING'
        else f'T_{material_texture_set}_Color.png'
      )
      path = texture_dir / filename
      if not path.is_file():
        if target == 'BAKING':
          path = ensure_black_base_color_bake(
            material_texture_set,
            texture_dir,
            resolution,
          )
        else:
          missing_sets.add(material_texture_set)
          continue
      image = load_or_reload_image(path)
      connected += set_material_base_color_image(material, image)
      applied_sets.add(material_texture_set)
      alpha_path = (
        texture_dir / f'{alpha_color_bake_name(material_texture_set)}.png'
      )
      if target == 'BAKING' and alpha_path.is_file():
        alpha_image = load_or_reload_image(alpha_path)
        connect_alpha_bake_to_material(material, alpha_image, enabled=True)
      else:
        set_material_alpha_overlay_enabled(material, target == 'BAKING')
    if connected == 0:
      missing = ', '.join(sorted(missing_sets))
      self.report(
        {'ERROR'},
        f'No {target.title()} Base Color textures found'
        + (f' ({missing})' if missing else ''),
      )
      return {'CANCELLED'}
    props.base_color_source = target
    message = (
      f'Base Color source: {target.title()} '
      f'({connected} shader(s), {len(applied_sets)} texture set(s))'
    )
    if missing_sets:
      message += f"; skipped missing: {', '.join(sorted(missing_sets))}"
    self.report({'INFO'}, message)
    return {'FINISHED'}


class SelectExportStatusObjectOperator(bpy.types.Operator):
  """Select an object shown in the Export Status list."""
  bl_idname = 'st.select_export_status_object'
  bl_label = 'Select Export Object'
  bl_options = {'INTERNAL'}

  object_name: bpy.props.StringProperty()

  def execute(self, context):
    obj = context.view_layer.objects.get(self.object_name)
    if obj is None:
      self.report({'WARNING'}, f"'{self.object_name}' is not visible in this view layer")
      return {'CANCELLED'}
    if obj.type == 'EMPTY':
      return {'CANCELLED'}

    for selected in tuple(context.selected_objects):
      selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    return {'FINISHED'}
