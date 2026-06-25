import bpy, bmesh, re, subprocess, os, time, traceback
import colorsys
import hashlib
import json
from array import array
from collections import defaultdict
from pathlib import Path

bl_info = {
  'name': 'Substance Import-Export Tools',
  'version': (3, 0, 0),
  'author': 'passivestar',
  'blender': (4, 1, 0),
  'location': '3D View N Panel',
  'description': 'Simplifies Export to Substance Painter',
  'category': 'Import-Export'
}

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
    prefs = context.preferences.addons[__name__].preferences
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
  else:
    obj.data.materials.append(material)

# @Operators

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


# @UI

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


# @Preferences

class SubstanceToolsPreferences(bpy.types.AddonPreferences):
  bl_idname = __name__

  painter_path: bpy.props.StringProperty(name='Substance Painter Executable', default=_DETECTED_PAINTER_PATH, subtype='FILE_PATH')

  def draw(self, context):
    layout = self.layout
    layout.prop(self, 'painter_path')

# @Register

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

  SubstanceToolsPreferences
)

def register():
  for c in classes: bpy.utils.register_class(c)
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
  for c in reversed(classes): bpy.utils.unregister_class(c)

if __name__ == '__main__': register()
