import bpy, bmesh, re, subprocess, os, time, traceback
import colorsys
import hashlib
import json
from array import array
from collections import defaultdict
from pathlib import Path

ADDON_MODULE_NAME = __package__.split('.')[0] if __package__ else __name__
# @Util

BAKING_COLLECTION = 'Baking'
LOW_COLLECTION = 'low'
HIGH_COLLECTION = 'high'
ALPHA_COLLECTION = 'alpha'
# Send to Unreal (send2ue) export set: ToolInfo.EXPORT_COLLECTION = 'Export'.
SEND2UE_EXPORT_COLLECTION = 'Export'
PAINTER_REQUEST = '.substance_tools_request.json'
BAKE_PLAN = '.substance_tools_bake_plan.json'
PENDING_REQUEST = 'pending_request.json'
PAINTER_EXPORT_REQUEST = '.substance_tools_export_request.json'
PAINTER_EXPORT_RESULT = '.substance_tools_export_result.json'
EXPORT_PRESET_NAME = 'Unreal_V2'
BACK_TEXTURE_SET_SUFFIX = '_back'


def clean_name(value):
  value = re.sub(r'[^0-9A-Za-z_]+', '_', str(value or '')).strip('_')
  return value or 'Asset'


def fbx_filename_from_object_name(value):
  value = re.sub(r'[<>:"/\\|?*]+', '_', str(value or '')).strip()
  value = value.rstrip('. ')
  return value or 'Object'


def ensure_baking_collections(scene=None):
  if scene is None:
    scene = getattr(bpy.context, 'scene', None)
  if scene is None and bpy.data.scenes:
    scene = bpy.data.scenes[0]
  if scene is None:
    return None, None, None, None
  root = bpy.data.collections.get(BAKING_COLLECTION)
  if root is None:
    root = bpy.data.collections.new(BAKING_COLLECTION)
  if root.name not in {collection.name for collection in scene.collection.children}:
    scene.collection.children.link(root)

  children = {}
  for name in (LOW_COLLECTION, HIGH_COLLECTION, ALPHA_COLLECTION):
    collection = bpy.data.collections.get(name)
    if collection is None:
      collection = bpy.data.collections.new(name)
    if collection.name not in {child.name for child in root.children}:
      root.children.link(collection)
    children[name] = collection
  return (
    root,
    children[LOW_COLLECTION],
    children[HIGH_COLLECTION],
    children[ALPHA_COLLECTION],
  )


@bpy.app.handlers.persistent
def ensure_baking_collections_on_load(_unused):
  for scene in bpy.data.scenes:
    _, low_collection, _, _ = ensure_baking_collections(scene)
    if low_collection is not None:
      sync_bake_selection(scene, low_texture_set_names(collection_meshes(low_collection)))


def ensure_baking_collections_deferred():
  for scene in bpy.data.scenes:
    _, low_collection, _, _ = ensure_baking_collections(scene)
    if low_collection is not None:
      sync_bake_selection(scene, low_texture_set_names(collection_meshes(low_collection)))
  return None


def get_baking_collections():
  """Look up the baking collections without creating or linking anything.

  Use this in UI draw code: a load handler and a deferred timer already create
  the collections, and Blender discourages modifying data during draw().
  """
  return (
    bpy.data.collections.get(BAKING_COLLECTION),
    bpy.data.collections.get(LOW_COLLECTION),
    bpy.data.collections.get(HIGH_COLLECTION),
    bpy.data.collections.get(ALPHA_COLLECTION),
  )


def painter_low_export_hierarchy():
  """Return the low meshes, parent chains, and Armature modifier rigs."""
  low_objects = set()
  baking_collection = bpy.data.collections.get(BAKING_COLLECTION)
  low_collection = None
  if baking_collection is not None:
    low_collection = next(
      (
        child for child in baking_collection.children
        if child.name == LOW_COLLECTION
      ),
      None,
    )

  if low_collection is not None:
    for obj in low_collection.all_objects:
      if obj.type != 'MESH':
        continue
      low_objects.add(obj)
      for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object is not None:
          rig = modifier.object
          low_objects.add(rig)
          rig_parent = rig.parent
          while rig_parent is not None:
            low_objects.add(rig_parent)
            rig_parent = rig_parent.parent
      parent = obj.parent
      while parent is not None:
        low_objects.add(parent)
        parent = parent.parent
  return low_objects


def export_status_groups():
  """Classify Send to Unreal members without changing scene structure."""
  export_collection = bpy.data.collections.get(SEND2UE_EXPORT_COLLECTION)
  if export_collection is None:
    return (), (), ()

  export_objects = set(export_collection.all_objects)
  low_objects = painter_low_export_hierarchy()
  low_auto = export_objects & low_objects
  remaining = export_objects - low_auto
  linked = {
    obj for obj in remaining
    if any(collection != export_collection for collection in obj.users_collection)
  }
  export_only = remaining - linked

  sort_key = lambda obj: obj.name.casefold()
  return (
    tuple(sorted(low_auto, key=sort_key)),
    tuple(sorted(linked, key=sort_key)),
    tuple(sorted(export_only, key=sort_key)),
  )


def blend_asset_name():
  if bpy.data.filepath:
    return clean_name(Path(bpy.data.filepath).stem)
  return clean_name(bpy.context.scene.name)


def baking_paths():
  base = Path(bpy.path.abspath('//')).resolve()
  asset = blend_asset_name()
  low_dir = base / LOW_COLLECTION
  high_dir = base / HIGH_COLLECTION
  texture_dir = base / 'texture'
  return {
    'asset': asset,
    'low_dir': low_dir,
    'high_dir': high_dir,
    'texture_dir': texture_dir,
    'low_fbx': low_dir / f'{asset}_low.fbx',
    'high_fbx': high_dir / f'{asset}_high.fbx',
    'spp': texture_dir / f'{asset}_SP.spp',
    'bake_plan': texture_dir / BAKE_PLAN,
  }


def bundled_export_preset_path(name=EXPORT_PRESET_NAME):
  return Path(__file__).resolve().parent / 'painter' / 'export-presets' / f'{name}.spexp'


def user_export_preset_path(name=EXPORT_PRESET_NAME):
  return (
    Path.home()
    / 'Documents'
    / 'Adobe'
    / 'Adobe Substance 3D Painter'
    / 'assets'
    / 'export-presets'
    / f'{name}.spexp'
  )


def ensure_painter_export_preset(name=EXPORT_PRESET_NAME):
  source_path = bundled_export_preset_path(name)
  if not source_path.is_file():
    raise FileNotFoundError(f'Bundled Painter export preset was not found: {source_path}')
  target_path = user_export_preset_path(name)
  source_bytes = source_path.read_bytes()
  try:
    if target_path.is_file() and target_path.read_bytes() == source_bytes:
      return target_path
  except OSError:
    pass
  target_path.parent.mkdir(parents=True, exist_ok=True)
  temporary_path = target_path.with_name(f'.{target_path.name}.tmp')
  temporary_path.write_bytes(source_bytes)
  os.replace(temporary_path, target_path)
  return target_path


def pending_request_path():
  base = Path(
    os.environ.get('LOCALAPPDATA')
    or os.environ.get('TEMP')
    or Path.home()
  )
  return base / 'SubstanceTools' / PENDING_REQUEST


def unreal_template_path(painter_path):
  return (
    Path(painter_path).resolve().parent
    / 'resources'
    / 'starter_assets'
    / 'templates'
    / 'Unreal Engine.spt'
  )


def write_json(path, value):
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary_path = path.with_name(f'.{path.name}.tmp')
  temporary_path.write_text(
    json.dumps(value, indent=2, ensure_ascii=False),
    encoding='utf-8',
  )
  os.replace(temporary_path, path)


def read_json(path, default=None):
  try:
    return json.loads(Path(path).read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return default


def load_or_reload_image(path):
  path = Path(path).resolve()
  for image in bpy.data.images:
    image_path = bpy.path.abspath(image.filepath_raw or image.filepath)
    if image_path and Path(image_path).resolve() == path:
      image.reload()
      image.name = path.stem
      return image
  image = bpy.data.images.load(str(path), check_existing=True)
  image.name = path.stem
  return image


def collection_meshes(collection):
  return sorted(
    [obj for obj in collection.all_objects if obj.type == 'MESH'],
    key=lambda obj: obj.name_full,
  )


def stripped_material_name(name):
  return name[2:] if name.startswith('M_') else name


def stable_color(value):
  digest = hashlib.sha256(str(value).encode('utf-8')).digest()
  hue = int.from_bytes(digest[:2], 'big') / 65535.0
  saturation = 0.55 + digest[2] / 255.0 * 0.35
  lightness = 0.42 + digest[3] / 255.0 * 0.18
  red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
  return (red, green, blue, 1.0)


def socket_contains_image_texture(socket, visited=None):
  if socket is None or not socket.is_linked:
    return False
  if visited is None:
    visited = set()
  for link in socket.links:
    node = link.from_node
    if node in visited:
      continue
    visited.add(node)
    if node.type == 'TEX_IMAGE' and node.image is not None:
      return True
    for input_socket in node.inputs:
      if socket_contains_image_texture(input_socket, visited):
        return True
  return False


def material_has_base_color_texture(material):
  if material is None or not material.use_nodes or material.node_tree is None:
    return False
  for node in material.node_tree.nodes:
    if node.type != 'BSDF_PRINCIPLED':
      continue
    base_color = node.inputs.get('Base Color')
    if socket_contains_image_texture(base_color):
      return True
  return False


def high_has_base_color_textures(high_objects):
  return any(
    material_has_base_color_texture(slot.material)
    for obj in high_objects
    for slot in obj.material_slots
    if slot.material
  )


def low_texture_set_names(low_objects):
  return sorted({
    stripped_material_name(slot.material.name)
    for obj in low_objects
    for slot in obj.material_slots
    if slot.material
  })


def sync_bake_selection(scene, texture_sets):
  """Mirror the bake-selection list onto the current Texture Sets.

  Preserves each set's existing checked state; new sets default to checked,
  vanished sets are dropped. Never called from panel draw (it writes data).
  """
  if scene is None:
    return
  selection = getattr(scene, 'substance_tools_bake_selection', None)
  if selection is None:
    return
  previous = {item.name: item.bake for item in selection}
  selection.clear()
  for texture_set in texture_sets:
    item = selection.add()
    item.name = texture_set
    item.bake = previous.get(texture_set, True)


def is_back_texture_set(texture_set):
  return str(texture_set).lower().endswith(BACK_TEXTURE_SET_SUFFIX)


def low_as_high_texture_set_names(low_objects, high_objects):
  high_bases = {
    match_base(obj.name, 'high').lower()
    for obj in high_objects
  }
  return sorted({
    stripped_material_name(slot.material.name)
    for obj in low_objects
    for slot in obj.material_slots
    if slot.material
    if (
      match_base(obj.name, 'low').lower() not in high_bases
      or is_back_texture_set(stripped_material_name(slot.material.name))
    )
  })


def low_objects_by_texture_set(low_objects):
  objects_by_texture_set = defaultdict(list)
  for obj in low_objects:
    for slot in obj.material_slots:
      if not slot.material:
        continue
      texture_set = stripped_material_name(slot.material.name)
      if obj not in objects_by_texture_set[texture_set]:
        objects_by_texture_set[texture_set].append(obj)
  return {
    texture_set: sorted(objects, key=lambda item: item.name_full)
    for texture_set, objects in objects_by_texture_set.items()
  }


def high_entries_by_texture_set(low_objects, high_objects, high_dir, asset):
  high_by_base = {
    match_base(obj.name, 'high').lower(): obj
    for obj in high_objects
  }
  entries = defaultdict(lambda: {'objects': [], 'bases': set()})
  for low in low_objects:
    base = match_base(low.name, 'low').lower()
    high = high_by_base.get(base)
    if high is None:
      continue
    for slot in low.material_slots:
      if not slot.material:
        continue
      texture_set = stripped_material_name(slot.material.name)
      if high not in entries[texture_set]['objects']:
        entries[texture_set]['objects'].append(high)
      entries[texture_set]['bases'].add(base)

  result = []
  for texture_set in sorted(entries):
    objects = sorted(entries[texture_set]['objects'], key=lambda obj: obj.name_full)
    fbxs = [
      high_dir / f"{fbx_filename_from_object_name(obj.name)}.fbx"
      for obj in objects
    ]
    result.append({
      'texture_set': texture_set,
      'bases': sorted(entries[texture_set]['bases']),
      'objects': objects,
      'fbx': fbxs[0] if len(fbxs) == 1 else None,
      'fbxs': fbxs,
    })
  return result


def base_color_bake_name(texture_set):
  return f'T_{clean_name(texture_set)}_Color_baking'


def alpha_color_bake_name(texture_set):
  return f'T_{clean_name(texture_set)}_Color_alpha'


def back_normal_mesh_map_plan(texture_sets, texture_dir):
  texture_set_lookup = {name.lower(): name for name in texture_sets}
  plan = {}
  for texture_set in texture_sets:
    if not texture_set.lower().endswith(BACK_TEXTURE_SET_SUFFIX):
      continue
    source_name = texture_set[:-len(BACK_TEXTURE_SET_SUFFIX)]
    source_texture_set = texture_set_lookup.get(source_name.lower(), source_name)
    source_normal_texture = (
      Path(texture_dir) / f'T_{clean_name(source_texture_set)}_Normal.png'
    )
    plan[texture_set] = {
      'source_texture_set': source_texture_set,
      'source_normal_texture': str(source_normal_texture.resolve()),
    }
  return plan


def hash_existing_back_normal_sources(back_normal_mesh_maps):
  return {
    texture_set: file_hash(Path(entry['source_normal_texture']))
    for texture_set, entry in back_normal_mesh_maps.items()
    if Path(entry['source_normal_texture']).is_file()
  }


def material_has_alpha_texture(material):
  if material is None or not material.use_nodes or material.node_tree is None:
    return False
  for node in material.node_tree.nodes:
    if node.type != 'BSDF_PRINCIPLED':
      continue
    if socket_contains_image_texture(node.inputs.get('Alpha')):
      return True
  return False


def object_role_base(name, role):
  name = re.sub(r'\.\d{3}$', '', name)
  return match_base(name, role)


def matching_low_objects(alpha_object, low_objects):
  alpha_base = object_role_base(alpha_object.name, 'alpha').lower()
  return [
    low
    for low in low_objects
    if object_role_base(low.name, 'low').lower() == alpha_base
  ]


def alpha_target_material_items(alpha_object, _context):
  if alpha_object is None:
    return [('AUTO', 'Auto', 'Use the only or same-named Low material')]
  _, low_collection, _, _ = ensure_baking_collections()
  low_objects = collection_meshes(low_collection) if low_collection else []
  materials = []
  seen = set()
  for low in matching_low_objects(alpha_object, low_objects):
    for material in low.data.materials:
      if material is None or material.name in seen:
        continue
      seen.add(material.name)
      materials.append((
        material.name,
        stripped_material_name(material.name),
        f"Painter Texture Set: {stripped_material_name(material.name)}",
      ))
  return [
    ('AUTO', 'Auto', 'Use the only or same-named Low material'),
    *materials,
  ]


def resolve_alpha_target(alpha_object, low_objects):
  matching_lows = matching_low_objects(alpha_object, low_objects)
  if not matching_lows:
    raise RuntimeError(
      f"No Low mesh matches '{alpha_object.name}'. "
      "Use names such as rock_low and rock_alpha"
    )
  candidates = {
    material.name: material
    for low in matching_lows
    for material in low.data.materials
    if material is not None
  }
  explicit = getattr(alpha_object, 'substance_tools_alpha_target_material', '')
  if explicit and explicit != 'AUTO' and explicit in candidates:
    return matching_lows, candidates[explicit]
  if len(candidates) == 1:
    return matching_lows, next(iter(candidates.values()))
  alpha_material_names = {
    material.name
    for material in alpha_object.data.materials
    if material is not None
  }
  shared = sorted(alpha_material_names & set(candidates))
  if len(shared) == 1:
    return matching_lows, candidates[shared[0]]
  choices = ', '.join(stripped_material_name(name) for name in sorted(candidates))
  raise RuntimeError(
    f"Choose Target Material for '{alpha_object.name}' ({choices})"
  )


def _alpha_mix_name(principled):
  return f'__SubstanceToolsAlphaMix_{principled.name}'


def _alpha_gate_name(principled):
  return f'__SubstanceToolsAlphaGate_{principled.name}'


def _alpha_image_node_name(material):
  return f'__SubstanceToolsAlpha_{clean_name(stripped_material_name(material.name))}'


def replace_socket_link(node_tree, from_socket, to_socket):
  for link in list(to_socket.links):
    node_tree.links.remove(link)
  node_tree.links.new(from_socket, to_socket)


def _ensure_alpha_mix(material, principled, alpha_image=None):
  node_tree = material.node_tree
  base_color = principled.inputs.get('Base Color')
  if base_color is None:
    return None
  mix_name = _alpha_mix_name(principled)
  mix = node_tree.nodes.get(mix_name)
  if mix is None or mix.type != 'MIX_RGB':
    previous_socket = base_color.links[0].from_socket if base_color.is_linked else None
    previous_color = tuple(base_color.default_value)
    mix = node_tree.nodes.new('ShaderNodeMixRGB')
    mix.name = mix_name
    mix.label = 'Substance Tools Alpha Overlay'
    mix.blend_type = 'MIX'
    mix.inputs[0].default_value = 0.0
    mix.inputs[1].default_value = previous_color
    if previous_socket is not None:
      replace_socket_link(node_tree, previous_socket, mix.inputs[1])
    replace_socket_link(node_tree, mix.outputs['Color'], base_color)
  elif not any(
    link.from_node == mix and link.to_socket == base_color
    for link in base_color.links
  ):
    replace_socket_link(node_tree, mix.outputs['Color'], base_color)

  if alpha_image is not None:
    image_node = _ensure_alpha_bake_image_node(material, alpha_image)
    gate_name = _alpha_gate_name(principled)
    gate = node_tree.nodes.get(gate_name)
    if gate is None or gate.type != 'MATH':
      gate = node_tree.nodes.new('ShaderNodeMath')
      gate.name = gate_name
      gate.label = 'Substance Tools Alpha Visibility'
      gate.operation = 'MULTIPLY'
      gate.inputs[1].default_value = 0.0
    replace_socket_link(node_tree, image_node.outputs['Alpha'], gate.inputs[0])
    replace_socket_link(node_tree, gate.outputs['Value'], mix.inputs[0])
    replace_socket_link(node_tree, image_node.outputs['Color'], mix.inputs[2])
  return mix

def _ensure_alpha_bake_image_node(material, alpha_image):
  node_tree = material.node_tree
  image_name = _alpha_image_node_name(material)
  image_node = node_tree.nodes.get(image_name)
  if image_node is None or image_node.type != 'TEX_IMAGE':
    image_node = node_tree.nodes.new('ShaderNodeTexImage')
    image_node.name = image_name
  image_node.label = 'Baked Alpha Detail'
  image_node.image = alpha_image
  image_node.interpolation = 'Linear'
  return image_node


def set_material_alpha_overlay_enabled(material, enabled):
  if material is None or material.node_tree is None:
    return
  value = 1.0 if enabled else 0.0
  for node in list(material.node_tree.nodes):
    if node.type == 'MATH' and node.name.startswith('__SubstanceToolsAlphaGate_'):
      node.inputs[1].default_value = value


def clear_principled_emission(material, principled):
  if material is None or material.node_tree is None:
    return
  node_tree = material.node_tree
  emission = principled.inputs.get('Emission Color') or principled.inputs.get('Emission')
  if emission is not None:
    for link in list(emission.links):
      node_tree.links.remove(link)
    try:
      emission.default_value = (0.0, 0.0, 0.0, 1.0)
    except (TypeError, ValueError):
      pass
  strength = principled.inputs.get('Emission Strength')
  if strength is not None:
    for link in list(strength.links):
      node_tree.links.remove(link)
    strength.default_value = 0.0


def connect_alpha_bake_to_material(material, image, enabled=True):
  if material is None:
    return 0
  material.use_nodes = True
  if material.node_tree is None:
    return 0
  connected = 0
  for principled in (
    node for node in material.node_tree.nodes
    if node.type == 'BSDF_PRINCIPLED'
  ):
    if _ensure_alpha_mix(material, principled, image) is not None:
      connected += 1
  set_material_alpha_overlay_enabled(material, enabled)
  return connected


def connect_base_color_bake_to_low_materials(low_objects, image):
  connected = 0
  materials = {
    slot.material
    for obj in low_objects
    for slot in obj.material_slots
    if slot.material
  }
  for material in materials:
    material.use_nodes = True
    node_tree = material.node_tree
    if node_tree is None:
      continue
    image_node = node_tree.nodes.get(image.name)
    if image_node is None or image_node.type != 'TEX_IMAGE':
      image_node = node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = image.name
    image_node.label = 'Baked High Base Color'
    image_node.image = image
    image_node.interpolation = 'Linear'
    for principled in (
      node for node in node_tree.nodes
      if node.type == 'BSDF_PRINCIPLED'
    ):
      base_color = principled.inputs.get('Base Color')
      if base_color is None:
        continue
      mix = _ensure_alpha_mix(material, principled)
      target = mix.inputs[1] if mix is not None else base_color
      replace_socket_link(node_tree, image_node.outputs['Color'], target)
      set_material_alpha_overlay_enabled(material, True)
      connected += 1
  return connected


def set_material_base_color_image(material, image):
  material.use_nodes = True
  node_tree = material.node_tree
  if node_tree is None:
    return 0
  image_node = node_tree.nodes.get(image.name)
  if image_node is None or image_node.type != 'TEX_IMAGE':
    image_node = node_tree.nodes.new('ShaderNodeTexImage')
    image_node.name = image.name
  image_node.label = image.name
  image_node.image = image
  connected = 0
  for principled in (
    node for node in node_tree.nodes
    if node.type == 'BSDF_PRINCIPLED'
  ):
    base_color = principled.inputs.get('Base Color')
    if base_color is not None:
      mix = _ensure_alpha_mix(material, principled)
      target = mix.inputs[1] if mix is not None else base_color
      replace_socket_link(node_tree, image_node.outputs['Color'], target)
      connected += 1
  return connected


def ensure_black_base_color_bake(texture_set, texture_dir, resolution):
  image_name = base_color_bake_name(texture_set)
  image_path = Path(texture_dir) / f'{image_name}.png'
  image_path.parent.mkdir(parents=True, exist_ok=True)
  image = bpy.data.images.get(image_name)
  if image is None:
    image = bpy.data.images.new(
      image_name,
      width=resolution,
      height=resolution,
      alpha=False,
      float_buffer=False,
    )
  elif list(image.size) != [resolution, resolution]:
    image.scale(resolution, resolution)
  image.generated_color = (0.0, 0.0, 0.0, 1.0)
  image.filepath_raw = str(image_path)
  image.file_format = 'PNG'
  _fill_image(image, (0.0, 0.0, 0.0, 1.0))
  image.save()
  return image_path


def force_material_opaque(material):
  """Make the material render fully opaque so opacity can't affect the viewport.

  Disconnects any link into the Principled BSDF Alpha input and resets it to 1.0
  (the baked game textures don't drive viewport opacity), and sets an opaque
  blend mode. Alpha = 1.0 guarantees opacity regardless of the blend/render mode.
  """
  if material is None or material.node_tree is None:
    return
  node_tree = material.node_tree
  for principled in (
    node for node in node_tree.nodes
    if node.type == 'BSDF_PRINCIPLED'
  ):
    alpha = principled.inputs.get('Alpha')
    if alpha is None:
      continue
    for link in list(alpha.links):
      node_tree.links.remove(link)
    alpha.default_value = 1.0
  if hasattr(material, 'blend_method'):
    try:
      material.blend_method = 'OPAQUE'
    except (TypeError, AttributeError):
      pass


def apply_painter_textures_to_low(low_objects, texture_dir):
  texture_dir = Path(texture_dir)
  applied = 0
  materials = {
    slot.material
    for obj in low_objects
    for slot in obj.material_slots
    if slot.material
  }
  for material in materials:
    texture_set = clean_name(stripped_material_name(material.name))
    paths = {
      role: texture_dir / f'T_{texture_set}_{role}.png'
      for role in ('Color', 'Extra', 'Normal', 'Emissive', 'Height')
    }
    images = {
      role: load_or_reload_image(path)
      for role, path in paths.items()
      if path.is_file()
    }
    if not images:
      continue
    material.use_nodes = True
    node_tree = material.node_tree
    if node_tree is None:
      continue
    principled_nodes = [
      node for node in node_tree.nodes
      if node.type == 'BSDF_PRINCIPLED'
    ]
    if images.get('Color') is not None:
      applied += set_material_base_color_image(material, images['Color'])
      set_material_alpha_overlay_enabled(material, False)
    if images.get('Normal') is not None:
      normal_image = images['Normal']
      normal_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(normal_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = normal_image.name
      image_node.image = normal_image
      normal_node = node_tree.nodes.get('Painter Normal') or node_tree.nodes.new('ShaderNodeNormalMap')
      normal_node.name = 'Painter Normal'
      node_tree.links.new(image_node.outputs['Color'], normal_node.inputs['Color'])
      for principled in principled_nodes:
        node_tree.links.new(normal_node.outputs['Normal'], principled.inputs['Normal'])
    if images.get('Extra') is not None:
      extra_image = images['Extra']
      extra_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(extra_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = extra_image.name
      image_node.image = extra_image
      separate = node_tree.nodes.get('Painter Extra Channels') or node_tree.nodes.new('ShaderNodeSeparateColor')
      separate.name = 'Painter Extra Channels'
      node_tree.links.new(image_node.outputs['Color'], separate.inputs['Color'])
      for principled in principled_nodes:
        node_tree.links.new(separate.outputs['Green'], principled.inputs['Roughness'])
        node_tree.links.new(separate.outputs['Blue'], principled.inputs['Metallic'])
    if images.get('Emissive') is not None:
      emissive_image = images['Emissive']
      image_node = node_tree.nodes.get(emissive_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = emissive_image.name
      image_node.image = emissive_image
      for principled in principled_nodes:
        emission = principled.inputs.get('Emission Color') or principled.inputs.get('Emission')
        if emission is not None:
          node_tree.links.new(image_node.outputs['Color'], emission)
    else:
      for principled in principled_nodes:
        clear_principled_emission(material, principled)
    if images.get('Height') is not None:
      height_image = images['Height']
      height_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(height_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = height_image.name
      image_node.image = height_image
      image_node.label = 'Painter Height'
    force_material_opaque(material)
  return applied


def canonicalize_painter_export_files(result):
  canonical_paths = []
  for files in result.get('textures', {}).values():
    for file_value in files:
      source = Path(file_value)
      if not source.is_file():
        continue
      stem = source.stem
      if stem.startswith('T_M_'):
        canonical_stem = f'T_{stem[4:]}'
      elif stem.startswith('M_'):
        canonical_stem = f'T_{stem[2:]}'
      elif stem.startswith('T_'):
        canonical_stem = stem
      else:
        canonical_stem = f'T_{stem}'
      target = source.with_name(f'{canonical_stem}{source.suffix.lower()}')
      if source.resolve() != target.resolve():
        os.replace(source, target)
      canonical_paths.append(str(target.resolve()))
  return canonical_paths


def add_high_id_colors(mesh, object_name, attribute_name='Color'):
  face_set_attribute = next(
    (
      mesh.attributes.get(name)
      for name in ('.sculpt_face_set', 'sculpt_face_set', 'face_set')
      if mesh.attributes.get(name) is not None
      and mesh.attributes.get(name).domain == 'FACE'
    ),
    None,
  )
  for color_attribute in list(mesh.color_attributes):
    mesh.color_attributes.remove(color_attribute)
  color_attribute = mesh.color_attributes.new(
    name=attribute_name,
    type='BYTE_COLOR',
    domain='CORNER',
  )
  fallback = stable_color(object_name)
  for polygon in mesh.polygons:
    face_set = (
      face_set_attribute.data[polygon.index].value
      if face_set_attribute is not None
      else object_name
    )
    color = stable_color(f'{object_name}:{face_set}') if face_set_attribute else fallback
    for loop_index in polygon.loop_indices:
      color_attribute.data[loop_index].color = color
  mesh.color_attributes.active_color = color_attribute
  mesh.color_attributes.render_color_index = mesh.color_attributes.find(color_attribute.name)


def add_high_id_preview_colors(source_objects):
  for source in source_objects:
    if source.type != 'MESH' or source.data is None:
      continue
    add_high_id_colors(source.data, source.name, attribute_name='ST_FaceSet_ID')


def duplicate_for_export(source_objects, collection, strip_material_prefix=False, id_source='NONE'):
  depsgraph = bpy.context.evaluated_depsgraph_get()
  duplicates = []
  temporary_materials = []
  renamed_materials = []
  material_copies = {}
  for source in source_objects:
    evaluated = source.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(
      evaluated,
      preserve_all_data_layers=True,
      depsgraph=depsgraph,
    )
    mesh.name = source.data.name
    duplicate = bpy.data.objects.new(source.name, mesh)
    duplicate.matrix_world = source.matrix_world.copy()
    collection.objects.link(duplicate)

    if strip_material_prefix:
      for index, material in enumerate(list(mesh.materials)):
        if material is None:
          continue
        if not material.name.startswith('M_'):
          continue
        copied = material_copies.get(material)
        if copied is None:
          target_name = stripped_material_name(material.name)
          blocker = bpy.data.materials.get(target_name)
          if blocker is not None and blocker is not material:
            original_name = blocker.name
            blocker.name = f'__SubstanceToolsBackup_{blocker.name}'
            renamed_materials.append((blocker, original_name))
          copied = material.copy()
          copied.name = target_name
          material_copies[material] = copied
          temporary_materials.append(copied)
        mesh.materials[index] = copied

    if id_source == 'FACE_SETS':
      add_high_id_colors(source.data, source.name, attribute_name='ST_FaceSet_ID')
      add_high_id_colors(mesh, source.name)
    duplicates.append(duplicate)
  return duplicates, temporary_materials, renamed_materials


def export_objects_to_fbx(source_objects, filepath, strip_material_prefix=False, id_source='NONE'):
  filepath.parent.mkdir(parents=True, exist_ok=True)
  temporary_collection = bpy.data.collections.new('__SubstanceToolsExport')
  bpy.context.scene.collection.children.link(temporary_collection)
  duplicates = []
  temporary_materials = []
  renamed_materials = []
  previous_selection = list(bpy.context.selected_objects)
  previous_active = bpy.context.view_layer.objects.active
  try:
    duplicates, temporary_materials, renamed_materials = duplicate_for_export(
      source_objects,
      temporary_collection,
      strip_material_prefix=strip_material_prefix,
      id_source=id_source,
    )
    bpy.ops.object.select_all(action='DESELECT')
    for duplicate in duplicates:
      duplicate.select_set(True)
    bpy.context.view_layer.objects.active = duplicates[0]
    bpy.ops.export_scene.fbx(
      filepath=str(filepath),
      use_selection=True,
      object_types={'MESH'},
      mesh_smooth_type='EDGE',
      use_mesh_modifiers=False,
      use_mesh_edges=True,
      use_tspace=True,
      add_leaf_bones=False,
      apply_scale_options='FBX_SCALE_ALL',
      bake_anim=False,
      bake_space_transform=True,
      colors_type='LINEAR',
    )
  finally:
    bpy.ops.object.select_all(action='DESELECT')
    for duplicate in duplicates:
      mesh = duplicate.data
      bpy.data.objects.remove(duplicate, do_unlink=True)
      if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    for material in temporary_materials:
      if material.users == 0:
        bpy.data.materials.remove(material)
    for material, original_name in renamed_materials:
      material.name = original_name
    bpy.data.collections.remove(temporary_collection)
    for obj in previous_selection:
      if obj.name in bpy.context.view_layer.objects:
        obj.select_set(True)
    if previous_active and previous_active.name in bpy.context.view_layer.objects:
      bpy.context.view_layer.objects.active = previous_active


def bake_high_base_color_to_low(
  low_objects,
  high_objects,
  texture_dir,
  resolution,
  match='BY_MESH_NAME',
  fixed_output_name=None,
):
  if not high_objects or not high_has_base_color_textures(high_objects):
    return {}

  texture_dir.mkdir(parents=True, exist_ok=True)
  temporary_collection = bpy.data.collections.new('__SubstanceToolsBaseColorBake')
  bpy.context.scene.collection.children.link(temporary_collection)
  low_duplicates = []
  high_duplicates = []
  temporary_materials = []
  bake_images = {}
  baked_texture_sets = set()
  previous_selection = list(bpy.context.selected_objects)
  previous_active = bpy.context.view_layer.objects.active
  previous_engine = bpy.context.scene.render.engine
  bake = bpy.context.scene.render.bake
  previous_bake = {
    'use_selected_to_active': bake.use_selected_to_active,
    'use_clear': bake.use_clear,
    'margin': bake.margin,
    'cage_extrusion': bake.cage_extrusion,
    'max_ray_distance': bake.max_ray_distance,
  }
  try:
    low_duplicates, _, _ = duplicate_for_export(low_objects, temporary_collection)
    high_duplicates, _, _ = duplicate_for_export(high_objects, temporary_collection)

    material_images = {}
    for source, duplicate in zip(low_objects, low_duplicates):
      if not duplicate.data.uv_layers:
        raise RuntimeError(f"Low-poly mesh has no UV map: {source.name}")
      for index, material in enumerate(list(duplicate.data.materials)):
        if material is None:
          continue
        copied = material.copy()
        copied.use_nodes = True
        if copied.node_tree is None:
          copied.use_nodes = True
        texture_set = stripped_material_name(material.name)
        image = material_images.get(texture_set)
        if image is None:
          image_path = (
            texture_dir / fixed_output_name
            if fixed_output_name
            else texture_dir / f'T_{clean_name(texture_set)}_Color.png'
          )
          image_name = (
            Path(fixed_output_name).stem
            if fixed_output_name
            else f'__SubstanceTools_{texture_set}_Color'
          )
          image = bpy.data.images.get(image_name) if fixed_output_name else None
          if image is None:
            image = bpy.data.images.new(
              image_name,
              width=resolution,
              height=resolution,
              alpha=False,
              float_buffer=False,
            )
          elif image.size[:] != [resolution, resolution]:
            image.scale(resolution, resolution)
          image.generated_color = (0.0, 0.0, 0.0, 1.0)
          image.filepath_raw = str(image_path)
          image.file_format = 'PNG'
          material_images[texture_set] = image
          bake_images[texture_set] = image_path
        image_node = copied.node_tree.nodes.new('ShaderNodeTexImage')
        image_node.name = '__SubstanceToolsBakeTarget'
        image_node.image = image
        copied.node_tree.nodes.active = image_node
        for node in copied.node_tree.nodes:
          node.select = node == image_node
        duplicate.data.materials[index] = copied
        temporary_materials.append(copied)

    bpy.context.scene.render.engine = 'CYCLES'
    bake.use_selected_to_active = True
    bake.use_clear = False
    bake.margin = max(8, min(64, resolution // 128))
    bounds = [obj.dimensions.length for obj in low_duplicates + high_duplicates]
    bake.cage_extrusion = max(bounds, default=1.0) * 0.01
    bake.max_ray_distance = 0.0

    high_by_base = defaultdict(list)
    for source, duplicate in zip(high_objects, high_duplicates):
      high_by_base[match_base(source.name, 'high').lower()].append(duplicate)

    for source, low_duplicate in zip(low_objects, low_duplicates):
      sources = (
        high_by_base.get(match_base(source.name, 'low').lower(), [])
        if match == 'BY_MESH_NAME'
        else high_duplicates
      )
      if not sources:
        continue
      bpy.ops.object.select_all(action='DESELECT')
      for high_duplicate in sources:
        high_duplicate.select_set(True)
      low_duplicate.select_set(True)
      bpy.context.view_layer.objects.active = low_duplicate
      bpy.ops.object.bake(
        type='DIFFUSE',
        pass_filter={'COLOR'},
        use_selected_to_active=True,
      )
      baked_texture_sets.update(
        stripped_material_name(material.name)
        for material in source.data.materials
        if material
      )

    for texture_set in baked_texture_sets:
      image_path = bake_images[texture_set]
      image = material_images[texture_set]
      image.save()
      if not image_path.is_file():
        raise RuntimeError(f'Base Color bake was not written: {image_path}')
    return {
      name: str(bake_images[name].resolve())
      for name in sorted(baked_texture_sets)
    }
  finally:
    bpy.context.scene.render.engine = previous_engine
    for key, value in previous_bake.items():
      setattr(bake, key, value)
    bpy.ops.object.select_all(action='DESELECT')
    for duplicate in low_duplicates + high_duplicates:
      mesh = duplicate.data
      bpy.data.objects.remove(duplicate, do_unlink=True)
      if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    for material in temporary_materials:
      if material.users == 0:
        bpy.data.materials.remove(material)
    for image in list(material_images.values()) if 'material_images' in locals() else []:
      if not fixed_output_name and image.users == 0:
        bpy.data.images.remove(image)
    bpy.data.collections.remove(temporary_collection)
    for obj in previous_selection:
      if obj.name in bpy.context.view_layer.objects:
        obj.select_set(True)
    if previous_active and previous_active.name in bpy.context.view_layer.objects:
      bpy.context.view_layer.objects.active = previous_active


def _keep_only_material_faces(mesh, material_index):
  editable = bmesh.new()
  try:
    editable.from_mesh(mesh)
    remove_faces = [
      face for face in editable.faces
      if face.material_index != material_index
    ]
    if remove_faces:
      bmesh.ops.delete(editable, geom=remove_faces, context='FACES')
    editable.to_mesh(mesh)
    mesh.update()
  finally:
    editable.free()


def _painter_color_socket(material, principled):
  expected_name = f'T_{clean_name(stripped_material_name(material.name))}_Color'
  fallback = None
  for node in material.node_tree.nodes:
    if node.type != 'TEX_IMAGE' or node.image is None:
      continue
    image_name = node.image.name
    if image_name == expected_name:
      return node.outputs.get('Color')
    if (
      fallback is None
      and image_name.startswith('T_')
      and image_name.endswith('_Color')
      and '_Color_baking' not in image_name
      and '_Color_alpha' not in image_name
    ):
      fallback = node.outputs.get('Color')
  return fallback


def _make_emission_material(material, channel, temporary_materials):
  copied = material.copy()
  copied.use_nodes = True
  node_tree = copied.node_tree
  principled = next(
    (node for node in node_tree.nodes if node.type == 'BSDF_PRINCIPLED'),
    None,
  )
  output = next(
    (node for node in node_tree.nodes if node.type == 'OUTPUT_MATERIAL'),
    None,
  )
  if principled is None or output is None:
    raise RuntimeError(
      f"Alpha material needs Principled BSDF and Material Output: {material.name}"
    )
  if channel == 'COLOR':
    source = _painter_color_socket(copied, principled) or principled.inputs.get('Base Color')
  else:
    source = principled.inputs.get('Alpha')
  emission = node_tree.nodes.new('ShaderNodeEmission')
  emission.name = f'__SubstanceToolsAlpha{channel}'
  if source is not None and source.is_linked:
    node_tree.links.new(source.links[0].from_socket, emission.inputs['Color'])
  elif channel == 'ALPHA' and source is not None:
    value = float(source.default_value)
    emission.inputs['Color'].default_value = (value, value, value, 1.0)
  elif source is not None:
    emission.inputs['Color'].default_value = tuple(source.default_value)
  node_tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
  temporary_materials.append(copied)
  return copied


def _prepare_alpha_source_duplicates(
  alpha_objects,
  collection,
  channel,
  temporary_materials,
):
  duplicates, _, _ = duplicate_for_export(alpha_objects, collection)
  for duplicate in duplicates:
    for index, material in enumerate(list(duplicate.data.materials)):
      if material is None:
        continue
      duplicate.data.materials[index] = _make_emission_material(
        material,
        channel,
        temporary_materials,
      )
  return duplicates


def _prepare_alpha_low_duplicate(
  low_object,
  target_material,
  collection,
  image,
  temporary_materials,
):
  duplicates, _, _ = duplicate_for_export([low_object], collection)
  duplicate = duplicates[0]
  material_index = next(
    (
      index for index, material in enumerate(low_object.data.materials)
      if material == target_material
    ),
    None,
  )
  if material_index is None:
    raise RuntimeError(
      f"{target_material.name} is not assigned to {low_object.name}"
    )
  _keep_only_material_faces(duplicate.data, material_index)
  duplicate.data.materials.clear()
  target = bpy.data.materials.new('__SubstanceToolsAlphaBakeTarget')
  target.use_nodes = True
  image_node = target.node_tree.nodes.new('ShaderNodeTexImage')
  image_node.name = '__SubstanceToolsAlphaBakeTarget'
  image_node.image = image
  target.node_tree.nodes.active = image_node
  image_node.select = True
  duplicate.data.materials.append(target)
  temporary_materials.append(target)
  return duplicate


def _bake_alpha_pass(
  entries,
  image,
  channel,
  temporary_collection,
  temporary_materials,
  margin,
):
  alpha_objects = list(dict.fromkeys(entry[0] for entry in entries))
  source_duplicates = _prepare_alpha_source_duplicates(
    alpha_objects,
    temporary_collection,
    channel,
    temporary_materials,
  )
  duplicate_by_source = dict(zip(alpha_objects, source_duplicates))
  low_duplicates = []
  try:
    for low_object in dict.fromkeys(
      low for _, matching_lows, _ in entries for low in matching_lows
    ):
      relevant_entries = [
        entry for entry in entries if low_object in entry[1]
      ]
      target_material = relevant_entries[0][2]
      low_duplicate = _prepare_alpha_low_duplicate(
        low_object,
        target_material,
        temporary_collection,
        image,
        temporary_materials,
      )
      low_duplicates.append(low_duplicate)
      sources = [
        duplicate_by_source[alpha_object]
        for alpha_object, matching_lows, _ in relevant_entries
        if low_object in matching_lows
      ]
      bpy.ops.object.select_all(action='DESELECT')
      for source in sources:
        source.select_set(True)
      low_duplicate.select_set(True)
      bpy.context.view_layer.objects.active = low_duplicate
      bpy.context.scene.render.bake.margin = margin
      bpy.ops.object.bake(
        type='EMIT',
        use_selected_to_active=True,
      )
  finally:
    for duplicate in source_duplicates + low_duplicates:
      mesh = duplicate.data
      bpy.data.objects.remove(duplicate, do_unlink=True)
      if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _copy_mask_to_alpha(color_image, mask_image):
  pixel_count = len(color_image.pixels)
  chunk_size = 1024 * 1024
  for offset in range(0, pixel_count, chunk_size):
    end = min(pixel_count, offset + chunk_size)
    color_chunk = list(color_image.pixels[offset:end])
    mask_chunk = mask_image.pixels[offset:end]
    first_alpha = (4 - (offset % 4) + 3) % 4
    for index in range(first_alpha, len(color_chunk), 4):
      color_chunk[index] = mask_chunk[index - 3]
    color_image.pixels[offset:end] = color_chunk
  color_image.update()


def _fill_image(image, color):
  pixel_count = len(image.pixels)
  chunk_size = 1024 * 1024
  pixels_per_chunk = chunk_size // 4
  full_chunk = list(color) * pixels_per_chunk
  for offset in range(0, pixel_count, chunk_size):
    end = min(pixel_count, offset + chunk_size)
    chunk = full_chunk[:end - offset]
    image.pixels[offset:end] = chunk
  image.update()


def bake_alpha_details_to_low(
  low_objects,
  alpha_objects,
  texture_dir,
  resolution,
  cage_extrusion,
  max_ray_distance,
):
  if not alpha_objects:
    return {}
  entries_by_texture_set = defaultdict(list)
  for alpha_object in alpha_objects:
    matching_lows, target_material = resolve_alpha_target(
      alpha_object,
      low_objects,
    )
    texture_set = stripped_material_name(target_material.name)
    entries_by_texture_set[texture_set].append(
      (alpha_object, matching_lows, target_material)
    )

  texture_dir.mkdir(parents=True, exist_ok=True)
  temporary_collection = bpy.data.collections.new('__SubstanceToolsAlphaBake')
  bpy.context.scene.collection.children.link(temporary_collection)
  temporary_materials = []
  temporary_images = []
  result = {}
  previous_selection = list(bpy.context.selected_objects)
  previous_active = bpy.context.view_layer.objects.active
  previous_engine = bpy.context.scene.render.engine
  bake = bpy.context.scene.render.bake
  previous_bake = {
    'use_selected_to_active': bake.use_selected_to_active,
    'use_clear': bake.use_clear,
    'margin': bake.margin,
    'cage_extrusion': bake.cage_extrusion,
    'max_ray_distance': bake.max_ray_distance,
  }
  try:
    bpy.context.scene.render.engine = 'CYCLES'
    bake.use_selected_to_active = True
    bake.use_clear = False
    bake.cage_extrusion = max(0.0, float(cage_extrusion))
    bake.max_ray_distance = max(0.0, float(max_ray_distance))
    for texture_set, entries in entries_by_texture_set.items():
      image_name = alpha_color_bake_name(texture_set)
      image_path = texture_dir / f'{image_name}.png'
      color_image = bpy.data.images.get(image_name)
      if color_image is None:
        color_image = bpy.data.images.new(
          image_name,
          width=resolution,
          height=resolution,
          alpha=True,
          float_buffer=False,
        )
      elif list(color_image.size) != [resolution, resolution]:
        color_image.scale(resolution, resolution)
      color_image.generated_color = (0.0, 0.0, 0.0, 0.0)
      color_image.alpha_mode = 'STRAIGHT'
      color_image.filepath_raw = str(image_path)
      color_image.file_format = 'PNG'
      _fill_image(color_image, (0.0, 0.0, 0.0, 0.0))

      mask_image = bpy.data.images.new(
        f'__SubstanceToolsAlphaMask_{clean_name(texture_set)}',
        width=resolution,
        height=resolution,
        alpha=False,
        float_buffer=False,
      )
      mask_image.generated_color = (0.0, 0.0, 0.0, 1.0)
      temporary_images.append(mask_image)
      _fill_image(mask_image, (0.0, 0.0, 0.0, 1.0))

      _bake_alpha_pass(
        entries,
        color_image,
        'COLOR',
        temporary_collection,
        temporary_materials,
        max(8, min(64, resolution // 128)),
      )
      _bake_alpha_pass(
        entries,
        mask_image,
        'ALPHA',
        temporary_collection,
        temporary_materials,
        0,
      )
      _copy_mask_to_alpha(color_image, mask_image)
      color_image.save()
      if not image_path.is_file():
        raise RuntimeError(f'Alpha detail bake was not written: {image_path}')
      result[texture_set] = str(image_path.resolve())
    return result
  finally:
    bpy.context.scene.render.engine = previous_engine
    for key, value in previous_bake.items():
      setattr(bake, key, value)
    bpy.ops.object.select_all(action='DESELECT')
    for material in temporary_materials:
      if material.users == 0:
        bpy.data.materials.remove(material)
    for image in temporary_images:
      if image.users == 0:
        bpy.data.images.remove(image)
    bpy.data.collections.remove(temporary_collection)
    for obj in previous_selection:
      if obj.name in bpy.context.view_layer.objects:
        obj.select_set(True)
    if previous_active and previous_active.name in bpy.context.view_layer.objects:
      bpy.context.view_layer.objects.active = previous_active


def file_hash(path):
  digest = hashlib.sha256()
  with path.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
      digest.update(chunk)
  return digest.hexdigest()


def _hash_update_value(digest, value):
  digest.update(repr(value).encode('utf-8'))
  digest.update(b'\n')


def _rounded_tuple(values):
  return tuple(round(float(value), 6) for value in values)


def _hash_mesh_object(digest, obj):
  mesh = obj.data
  _hash_update_value(digest, ('object', obj.name))
  _hash_update_value(
    digest,
    ('matrix_world', [_rounded_tuple(row) for row in obj.matrix_world]),
  )
  _hash_update_value(
    digest,
    ('materials', [material.name if material else '' for material in mesh.materials]),
  )
  _hash_update_value(digest, ('vertices', len(mesh.vertices)))
  for vertex in mesh.vertices:
    _hash_update_value(digest, _rounded_tuple(vertex.co))
  _hash_update_value(digest, ('edges', len(mesh.edges)))
  for edge in mesh.edges:
    _hash_update_value(digest, tuple(edge.vertices))
  _hash_update_value(digest, ('polygons', len(mesh.polygons)))
  for polygon in mesh.polygons:
    _hash_update_value(
      digest,
      (
        tuple(polygon.vertices),
        polygon.material_index,
        polygon.use_smooth,
      ),
    )
  for uv_layer in sorted(mesh.uv_layers, key=lambda layer: layer.name):
    _hash_update_value(digest, ('uv', uv_layer.name, len(uv_layer.data)))
    for item in uv_layer.data:
      _hash_update_value(digest, _rounded_tuple(item.uv))
  for color_attribute in sorted(mesh.color_attributes, key=lambda attr: attr.name):
    _hash_update_value(
      digest,
      (
        'color_attribute',
        color_attribute.name,
        color_attribute.domain,
        color_attribute.data_type,
        len(color_attribute.data),
      ),
    )
    for item in color_attribute.data:
      color = getattr(item, 'color', None)
      value = color if color is not None else getattr(item, 'value', None)
      if value is not None:
        _hash_update_value(digest, _rounded_tuple(value))


def _hash_attribute_data(digest, attribute):
  _hash_update_value(
    digest,
    (
      'attribute',
      attribute.name,
      attribute.domain,
      attribute.data_type,
      len(attribute.data),
    ),
  )
  for item in attribute.data:
    for name in ('value', 'vector', 'color'):
      if not hasattr(item, name):
        continue
      value = getattr(item, name)
      try:
        iter(value)
      except TypeError:
        _hash_update_value(digest, value)
      else:
        _hash_update_value(digest, _rounded_tuple(value))
      break


def _hash_modifier_summary(digest, obj):
  for modifier in obj.modifiers:
    _hash_update_value(
      digest,
      (
        'modifier',
        modifier.name,
        modifier.type,
        modifier.show_viewport,
        modifier.show_render,
      ),
    )
    for prop in modifier.bl_rna.properties:
      if prop.is_readonly or prop.identifier in {'name', 'rna_type'}:
        continue
      try:
        value = getattr(modifier, prop.identifier)
      except Exception:
        continue
      if isinstance(value, (str, int, float, bool)):
        _hash_update_value(digest, (prop.identifier, value))
      elif hasattr(value, 'name'):
        _hash_update_value(digest, (prop.identifier, value.name))


def _hash_source_mesh_object(
  digest,
  obj,
  strip_material_prefix=False,
  id_source='NONE',
  include_modifier_summary=False,
):
  mesh = obj.data
  _hash_update_value(digest, ('object', obj.name, id_source))
  _hash_update_value(
    digest,
    ('matrix_world', [_rounded_tuple(row) for row in obj.matrix_world]),
  )
  if include_modifier_summary:
    _hash_modifier_summary(digest, obj)
  material_names = []
  for material in mesh.materials:
    name = material.name if material else ''
    material_names.append(stripped_material_name(name) if strip_material_prefix else name)
  _hash_update_value(digest, ('materials', material_names))
  _hash_update_value(digest, ('vertices', len(mesh.vertices)))
  for vertex in mesh.vertices:
    _hash_update_value(digest, _rounded_tuple(vertex.co))
  _hash_update_value(digest, ('edges', len(mesh.edges)))
  for edge in mesh.edges:
    _hash_update_value(digest, tuple(edge.vertices))
  _hash_update_value(digest, ('polygons', len(mesh.polygons)))
  for polygon in mesh.polygons:
    _hash_update_value(
      digest,
      (
        tuple(polygon.vertices),
        polygon.material_index,
        polygon.use_smooth,
      ),
    )
  for uv_layer in sorted(mesh.uv_layers, key=lambda layer: layer.name):
    _hash_update_value(digest, ('uv', uv_layer.name, len(uv_layer.data)))
    for item in uv_layer.data:
      _hash_update_value(digest, _rounded_tuple(item.uv))
  for color_attribute in sorted(mesh.color_attributes, key=lambda attr: attr.name):
    _hash_attribute_data(digest, color_attribute)
  if id_source == 'FACE_SETS':
    for attribute in sorted(mesh.attributes, key=lambda attr: attr.name):
      if (
        attribute.domain == 'FACE'
        and attribute.name in {'.sculpt_face_set', 'sculpt_face_set', 'face_set'}
      ):
        _hash_attribute_data(digest, attribute)


def source_content_hash(
  source_objects,
  strip_material_prefix=False,
  id_source='NONE',
  include_modifier_summary=False,
):
  digest = hashlib.sha256()
  for obj in sorted(source_objects, key=lambda item: item.name_full):
    _hash_source_mesh_object(
      digest,
      obj,
      strip_material_prefix=strip_material_prefix,
      id_source=id_source,
      include_modifier_summary=include_modifier_summary,
    )
  return digest.hexdigest()


def _hash_fast_mesh_signature(
  digest,
  obj,
  strip_material_prefix=False,
  id_source='NONE',
):
  mesh = obj.data
  _hash_update_value(digest, ('object', obj.name, obj.type, id_source))
  _hash_update_value(digest, ('data', mesh.name))
  _hash_update_value(
    digest,
    ('matrix_world', [_rounded_tuple(row) for row in obj.matrix_world]),
  )
  _hash_update_value(digest, ('dimensions', _rounded_tuple(obj.dimensions)))
  _hash_update_value(
    digest,
    ('bound_box', [_rounded_tuple(corner) for corner in obj.bound_box]),
  )
  _hash_update_value(
    digest,
    ('counts', len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops)),
  )
  if mesh.vertices:
    values = array('f', [0.0]) * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get('co', values)
    digest.update(b'vertex_co\0')
    digest.update(values.tobytes())
  if mesh.edges:
    values = array('i', [0]) * (len(mesh.edges) * 2)
    mesh.edges.foreach_get('vertices', values)
    digest.update(b'edge_vertices\0')
    digest.update(values.tobytes())
  if mesh.polygons:
    values = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('material_index', values)
    digest.update(b'polygon_material_index\0')
    digest.update(values.tobytes())
    values = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('loop_start', values)
    digest.update(b'polygon_loop_start\0')
    digest.update(values.tobytes())
    values = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('loop_total', values)
    digest.update(b'polygon_loop_total\0')
    digest.update(values.tobytes())
    smooth_values = array('b', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('use_smooth', smooth_values)
    digest.update(b'polygon_use_smooth\0')
    digest.update(smooth_values.tobytes())
  if mesh.loops:
    values = array('i', [0]) * len(mesh.loops)
    mesh.loops.foreach_get('vertex_index', values)
    digest.update(b'loop_vertex_index\0')
    digest.update(values.tobytes())
  material_names = []
  for material in mesh.materials:
    name = material.name if material else ''
    material_names.append(stripped_material_name(name) if strip_material_prefix else name)
  _hash_update_value(digest, ('materials', material_names))
  for uv_layer in sorted(mesh.uv_layers, key=lambda layer: layer.name):
    _hash_update_value(digest, ('uv', uv_layer.name, len(uv_layer.data)))
    if uv_layer.data:
      values = array('f', [0.0]) * (len(uv_layer.data) * 2)
      uv_layer.data.foreach_get('uv', values)
      digest.update(b'uv\0')
      digest.update(values.tobytes())
  _hash_update_value(
    digest,
    ('uv_layers', [(layer.name, len(layer.data)) for layer in mesh.uv_layers]),
  )
  _hash_update_value(
    digest,
    (
      'attributes',
      [
        (attribute.name, attribute.domain, attribute.data_type, len(attribute.data))
        for attribute in mesh.attributes
      ],
    ),
  )
  _hash_update_value(
    digest,
    (
      'color_attributes',
      [
        (attribute.name, attribute.domain, attribute.data_type, len(attribute.data))
        for attribute in mesh.color_attributes
      ],
    ),
  )
  for color_attribute in sorted(mesh.color_attributes, key=lambda attr: attr.name):
    if color_attribute.data and hasattr(color_attribute.data[0], 'color'):
      values = array('f', [0.0]) * (len(color_attribute.data) * 4)
      color_attribute.data.foreach_get('color', values)
      digest.update(b'color_attribute_color\0')
      digest.update(values.tobytes())
  if id_source == 'FACE_SETS':
    for attribute in sorted(mesh.attributes, key=lambda attr: attr.name):
      if (
        attribute.domain == 'FACE'
        and attribute.name in {'.sculpt_face_set', 'sculpt_face_set', 'face_set'}
      ):
        _hash_update_value(digest, ('face_set', attribute.name, len(attribute.data)))
        values = array('i', [0]) * len(attribute.data)
        attribute.data.foreach_get('value', values)
        digest.update(b'face_set_value\0')
        digest.update(values.tobytes())
  _hash_modifier_summary(digest, obj)


def fast_content_hash(source_objects, strip_material_prefix=False, id_source='NONE'):
  digest = hashlib.sha256()
  for obj in sorted(source_objects, key=lambda item: item.name_full):
    _hash_fast_mesh_signature(
      digest,
      obj,
      strip_material_prefix=strip_material_prefix,
      id_source=id_source,
    )
  return digest.hexdigest()


def _hash_mesh_arrays(digest, mesh, object_name, strip_material_prefix=False, id_source='NONE'):
  _hash_update_value(digest, ('object', object_name, id_source))
  material_names = []
  for material in mesh.materials:
    name = material.name if material else ''
    material_names.append(stripped_material_name(name) if strip_material_prefix else name)
  _hash_update_value(digest, ('materials', material_names))
  _hash_update_value(
    digest,
    ('counts', len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops)),
  )
  if mesh.vertices:
    values = array('f', [0.0]) * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get('co', values)
    digest.update(b'vertex_co\0')
    digest.update(values.tobytes())
  if mesh.edges:
    values = array('i', [0]) * (len(mesh.edges) * 2)
    mesh.edges.foreach_get('vertices', values)
    digest.update(b'edge_vertices\0')
    digest.update(values.tobytes())
  if mesh.polygons:
    values = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('material_index', values)
    digest.update(b'polygon_material_index\0')
    digest.update(values.tobytes())
    values = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('loop_start', values)
    digest.update(b'polygon_loop_start\0')
    digest.update(values.tobytes())
    values = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('loop_total', values)
    digest.update(b'polygon_loop_total\0')
    digest.update(values.tobytes())
    smooth_values = array('b', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('use_smooth', smooth_values)
    digest.update(b'polygon_use_smooth\0')
    digest.update(smooth_values.tobytes())
  if mesh.loops:
    values = array('i', [0]) * len(mesh.loops)
    mesh.loops.foreach_get('vertex_index', values)
    digest.update(b'loop_vertex_index\0')
    digest.update(values.tobytes())
  for uv_layer in sorted(mesh.uv_layers, key=lambda layer: layer.name):
    _hash_update_value(digest, ('uv', uv_layer.name, len(uv_layer.data)))
    if uv_layer.data:
      values = array('f', [0.0]) * (len(uv_layer.data) * 2)
      uv_layer.data.foreach_get('uv', values)
      digest.update(b'uv\0')
      digest.update(values.tobytes())
  for color_attribute in sorted(mesh.color_attributes, key=lambda attr: attr.name):
    _hash_update_value(
      digest,
      (
        'color_attribute',
        color_attribute.name,
        color_attribute.domain,
        color_attribute.data_type,
        len(color_attribute.data),
      ),
    )
    if color_attribute.data and hasattr(color_attribute.data[0], 'color'):
      values = array('f', [0.0]) * (len(color_attribute.data) * 4)
      color_attribute.data.foreach_get('color', values)
      digest.update(b'color_attribute_color\0')
      digest.update(values.tobytes())
  if id_source == 'FACE_SETS':
    for attribute in sorted(mesh.attributes, key=lambda attr: attr.name):
      if (
        attribute.domain == 'FACE'
        and attribute.name in {'.sculpt_face_set', 'sculpt_face_set', 'face_set'}
      ):
        _hash_update_value(digest, ('face_set', attribute.name, len(attribute.data)))
        values = array('i', [0]) * len(attribute.data)
        attribute.data.foreach_get('value', values)
        digest.update(b'face_set_value\0')
        digest.update(values.tobytes())


def evaluated_content_hash(source_objects, strip_material_prefix=False, id_source='NONE'):
  depsgraph = bpy.context.evaluated_depsgraph_get()
  digest = hashlib.sha256()
  for obj in sorted(source_objects, key=lambda item: item.name_full):
    _hash_update_value(
      digest,
      ('matrix_world', obj.name, [_rounded_tuple(row) for row in obj.matrix_world]),
    )
    _hash_modifier_summary(digest, obj)
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(
      preserve_all_data_layers=True,
      depsgraph=depsgraph,
    )
    try:
      _hash_mesh_arrays(
        digest,
        mesh,
        obj.name,
        strip_material_prefix=strip_material_prefix,
        id_source=id_source,
      )
    finally:
      evaluated.to_mesh_clear()
  return digest.hexdigest()


def load_bake_plan(paths, context):
  plan = read_json(paths['bake_plan'], {})
  if plan:
    return plan
  try:
    return json.loads(context.scene.get('substance_tools_bake_plan_preview', '{}'))
  except ValueError:
    return {}


def export_content_hash(source_objects, strip_material_prefix=False, id_source='NONE'):
  """Hash the deterministic contents exported to Painter, not FBX file bytes."""
  temporary_collection = bpy.data.collections.new('__SubstanceToolsHash')
  bpy.context.scene.collection.children.link(temporary_collection)
  duplicates = []
  temporary_materials = []
  renamed_materials = []
  try:
    duplicates, temporary_materials, renamed_materials = duplicate_for_export(
      source_objects,
      temporary_collection,
      strip_material_prefix=strip_material_prefix,
      id_source=id_source,
    )
    digest = hashlib.sha256()
    for duplicate in sorted(duplicates, key=lambda obj: obj.name_full):
      _hash_mesh_object(digest, duplicate)
    return digest.hexdigest()
  finally:
    for duplicate in duplicates:
      mesh = duplicate.data
      bpy.data.objects.remove(duplicate, do_unlink=True)
      if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    for material in temporary_materials:
      if material.users == 0:
        bpy.data.materials.remove(material)
    for material, original_name in renamed_materials:
      material.name = original_name
    bpy.data.collections.remove(temporary_collection)


def match_base(name, suffix):
  return re.sub(
    rf'(?i)(?:[_. -]?{suffix})(?:[_. -]?\d+)?$',
    '',
    name,
  )


def unmatched_mesh_names(low_objects, high_objects):
  low_names = {match_base(obj.name, 'low').lower(): obj.name for obj in low_objects}
  high_names = {match_base(obj.name, 'high').lower(): obj.name for obj in high_objects}
  return (
    [low_names[key] for key in sorted(low_names.keys() - high_names.keys())],
    [high_names[key] for key in sorted(high_names.keys() - low_names.keys())],
  )

def detect_substance_painter_path():
  paths = []

  current_os = os.name

  if current_os == 'posix':
    # MacOS
    paths.extend([
        f'/Applications/Adobe Substance 3D Painter.app/Contents/MacOS/Adobe Substance 3D Painter',
        f'/Applications/Adobe Substance 3D Painter/Adobe Substance 3D Painter.app/Contents/MacOS/Adobe Substance 3D Painter',
        f'~/Library/Application Support/Steam/steamapps/common/Substance 3D Painter/Adobe Substance 3D Painter.app/Contents/MacOS/Adobe Substance 3D Painter'
    ])
    # MacOS with year
    for year in range(2020, 2026):
      paths.extend([
          f'/Applications/Adobe Substance 3D Painter {year}.app/Contents/MacOS/Adobe Substance 3D Painter',
          f'/Applications/Adobe Substance 3D Painter/Adobe Substance 3D Painter {year}.app/Contents/MacOS/Adobe Substance 3D Painter',
          f'~/Library/Application Support/Steam/steamapps/common/Substance 3D Painter {year}/Adobe Substance 3D Painter.app/Contents/MacOS/Adobe Substance 3D Painter'
      ])
  elif current_os == 'nt':
    # Windows
    for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
      paths.extend([
          # CC
          f'{letter}:\\Program Files\\Adobe\\Adobe Substance 3D Painter\\Adobe Substance 3D Painter.exe',
          f'{letter}:\\Program Files (x86)\\Adobe\\Adobe Substance 3D Painter\\Adobe Substance 3D Painter.exe',

          # Steam without 3D
          f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance Painter\\Adobe Substance 3D Painter.exe',
          f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance Painter\\Adobe Substance 3D Painter.exe',

          # Steam with 3D
          f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance 3D Painter\\Adobe Substance 3D Painter.exe',
          f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance 3D Painter\\Adobe Substance 3D Painter.exe',
      ])
      # Windows with year
      for year in range(2020, 2026):
        paths.extend([
            # CC
            f'{letter}:\\Program Files\\Adobe\\Adobe Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe',
            f'{letter}:\\Program Files (x86)\\Adobe\\Adobe Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe',

            # Steam without 3D
            f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance Painter {year}\\Adobe Substance 3D Painter.exe',
            f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance Painter {year}\\Adobe Substance 3D Painter.exe',

            # Steam with 3D
            f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe',
            f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe',
        ])

  # Check each path for the current operating system and return the first one that exists
  for path in paths:
    path = os.path.expanduser(path)
    try:
      if Path(path).exists():
        return path
    except Exception as e:
      pass

  # If none of the paths exist, return an empty string
  return ''


def painter_is_running(painter_path):
  executable_name = Path(painter_path).name
  try:
    if os.name == 'nt':
      result = subprocess.run(
        ['tasklist', '/FI', f'IMAGENAME eq {executable_name}', '/FO', 'CSV', '/NH'],
        capture_output=True,
        text=True,
        encoding='mbcs',
        errors='replace',
        check=False,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
      )
      output = result.stdout or ''
      return result.returncode == 0 and executable_name.lower() in output.lower()
    result = subprocess.run(
      ['pgrep', '-f', str(Path(painter_path).resolve())],
      capture_output=True,
      check=False,
    )
    return result.returncode == 0
  except OSError:
    return False


# Scanning every drive for the Painter executable is slow, so do it once.
_DETECTED_PAINTER_PATH = detect_substance_painter_path()

# Mock data for testing through blender text editor without installing
mocks = {
  'painter_path': _DETECTED_PAINTER_PATH,
}

def get_preferences(context):
  if __name__ == '__main__':
    return mocks
  else:
    prefs = context.preferences.addons[ADDON_MODULE_NAME].preferences
    return {
      'painter_path': prefs.painter_path,
    }

def create_material_for_object(obj):
  material = bpy.data.materials.new(name=obj.name)
  material.use_nodes = True
  material.node_tree.nodes.clear()
  principled_bsdf = material.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
  material_output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
  material.node_tree.links.new(principled_bsdf.outputs['BSDF'], material_output.inputs['Surface'])
  if len(obj.data.materials) > 0:
    obj.data.materials[0] = material
