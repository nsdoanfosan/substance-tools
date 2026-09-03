import bpy, bmesh, re, subprocess, os, time, traceback
import colorsys
import hashlib
import json
import shutil
import tempfile
from array import array
from collections import defaultdict
from pathlib import Path
from .pipeline_contract import collection_name, naming_value

ADDON_MODULE_NAME = __package__.split('.')[0] if __package__ else __name__
# @Util

BAKING_COLLECTION = collection_name('baking_root', 'Baking')
LOW_COLLECTION = collection_name('low', 'low')
HIGH_COLLECTION = collection_name('high', 'high')
ALPHA_COLLECTION = collection_name('alpha', 'alpha')
# Send to Unreal (send2ue) export set: ToolInfo.EXPORT_COLLECTION = 'Export'.
SEND2UE_EXPORT_COLLECTION = collection_name('send_to_unreal_export', 'Export')
COLLECTION_ROLE_PROPERTY = 'substance_tools_role'
PAINTER_REQUEST = '.substance_tools_request.json'
BAKE_PLAN = '.substance_tools_bake_plan.json'
PENDING_REQUEST = 'pending_request.json'
PAINTER_EXPORT_REQUEST = '.substance_tools_export_request.json'
PAINTER_EXPORT_RESULT = '.substance_tools_export_result.json'
EXPORT_PRESET_NAME = naming_value('painter_export_preset', 'Unreal_V2')
CLOTH_EXPORT_PRESET_NAME = naming_value('painter_cloth_export_preset', 'Unreal_V2_Cloth')
MATERIAL_PREFIX = naming_value('material_prefix', 'M_')
TEXTURE_PREFIX = naming_value('texture_prefix', 'T_')
BACK_TEXTURE_SET_SUFFIX = naming_value('back_texture_set_suffix', '_back')
SOLIDIFY_PLUS_NAME_PREFIX = 'Solidify Plus'
SOLIDIFY_PLUS_FILL_RIM_SOCKET = 'Fill Rim'
PAINTER_EXPORT_PRESET_ITEMS = (
  ('UNREAL_V2', EXPORT_PRESET_NAME, 'Color, Normal, packed Extra, Emissive, Height'),
  (
    'UNREAL_V2_CLOTH',
    CLOTH_EXPORT_PRESET_NAME,
    'Unreal V2 plus Sheen Color, Sheen Opacity, Sheen Roughness',
  ),
)
PAINTER_TEXTURE_ROLES = ('Color', 'Extra', 'Normal', 'Emissive', 'Height')
MESHY_PAINTER_CANONICAL_ROLES = ('Color', 'Extra', 'Normal')
PAINTER_CLOTH_TEXTURE_ROLES = PAINTER_TEXTURE_ROLES + (
  'SheenColor',
  'SheenOpacity',
  'SheenRoughness',
)
BAKING_ROLE_COLLECTIONS = (LOW_COLLECTION, HIGH_COLLECTION, ALPHA_COLLECTION)
_BAKING_ROLE_MEMBERSHIP = {}
_BAKING_ROLE_COLLECTION_SIGNATURE = ()
_BAKING_ROLE_SYNCING = False


def clean_name(value):
  value = re.sub(r'[^0-9A-Za-z_]+', '_', str(value or '')).strip('_')
  return value or 'Asset'


def fbx_filename_from_object_name(value):
  value = re.sub(r'[<>:"/\\|?*]+', '_', str(value or '')).strip()
  value = value.rstrip('. ')
  return value or 'Object'


def collection_role(collection):
  if collection is None:
    return None
  role = collection.get(COLLECTION_ROLE_PROPERTY)
  if role in BAKING_ROLE_COLLECTIONS:
    return role
  name = collection.name.casefold()
  for role_name in BAKING_ROLE_COLLECTIONS:
    if name == role_name.casefold():
      return role_name
  return None


def find_baking_role_child(root, role_name):
  if root is None:
    return None
  for child in root.children:
    if collection_role(child) == role_name:
      child[COLLECTION_ROLE_PROPERTY] = role_name
      return child
  return None


def ensure_baking_role_child(root, role_name):
  child = find_baking_role_child(root, role_name)
  if child is not None:
    return child

  existing = bpy.data.collections.get(role_name)
  if existing is not None and existing.users == 0:
    child = existing
  else:
    child = bpy.data.collections.new(role_name)
  child[COLLECTION_ROLE_PROPERTY] = role_name
  if child.name not in {collection.name for collection in root.children}:
    root.children.link(child)
  return child


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
    children[name] = ensure_baking_role_child(root, name)
  return (
    root,
    children[LOW_COLLECTION],
    children[HIGH_COLLECTION],
    children[ALPHA_COLLECTION],
  )


def baking_role_children(scene=None):
  root, low_collection, high_collection, alpha_collection = get_baking_collections()
  if root is None and scene is not None:
    root = bpy.data.collections.get(BAKING_COLLECTION)
  if root is None:
    return None, {}

  roles = {
    LOW_COLLECTION: [],
    HIGH_COLLECTION: [],
    ALPHA_COLLECTION: [],
  }
  for child in root.children:
    role = collection_role(child)
    if role in roles:
      child[COLLECTION_ROLE_PROPERTY] = role
      roles[role].append(child)
  for role, collection in (
    (LOW_COLLECTION, low_collection),
    (HIGH_COLLECTION, high_collection),
    (ALPHA_COLLECTION, alpha_collection),
  ):
    if collection is not None and collection not in roles[role]:
      roles[role].append(collection)
  return root, roles


def object_baking_roles(obj, roles):
  memberships = set()
  object_collections = set(obj.users_collection)
  for role, collections in roles.items():
    for collection in collections:
      if collection in object_collections:
        memberships.add(role)
        break
  return memberships


def baking_role_collection_signature(roles):
  """Return the role membership state without scanning unrelated objects."""
  entries = set()
  for role, collections in roles.items():
    for collection in collections:
      collection_pointer = collection.as_pointer()
      for obj in collection.objects:
        entries.add((role, collection_pointer, obj.as_pointer()))
  return tuple(sorted(entries))


def depsgraph_requires_baking_role_sync(scene, depsgraph):
  """Only request a full role sync when baking collection membership changed."""
  if _BAKING_ROLE_SYNCING:
    return False

  _, roles = baking_role_children(scene)
  if not roles:
    return False

  collection_or_scene_updated = False
  for update in depsgraph.updates:
    id_data = getattr(update.id, 'original', None) or update.id
    if isinstance(id_data, bpy.types.Object):
      previous = _BAKING_ROLE_MEMBERSHIP.get(id_data.as_pointer(), set())
      if object_baking_roles(id_data, roles) != previous:
        return True
    elif isinstance(id_data, (bpy.types.Collection, bpy.types.Scene)):
      collection_or_scene_updated = True

  if not collection_or_scene_updated:
    return False
  return baking_role_collection_signature(roles) != _BAKING_ROLE_COLLECTION_SIGNATURE


def unlink_from_export_preserving_visibility(obj, export_collection, scene=None):
  if export_collection is None or export_collection not in obj.users_collection:
    return False
  if scene is None:
    scene = getattr(bpy.context, 'scene', None)
  if len(obj.users_collection) <= 1 and scene is not None:
    scene.collection.objects.link(obj)
  try:
    export_collection.objects.unlink(obj)
    return True
  except RuntimeError:
    return False


def remember_baking_role_membership(scene=None):
  global _BAKING_ROLE_MEMBERSHIP, _BAKING_ROLE_COLLECTION_SIGNATURE
  _, roles = baking_role_children(scene)
  if not roles:
    _BAKING_ROLE_MEMBERSHIP = {}
    _BAKING_ROLE_COLLECTION_SIGNATURE = ()
    return
  tracked = {}
  for role, collections in roles.items():
    for collection in collections:
      for obj in collection.objects:
        tracked.setdefault(obj.as_pointer(), set()).add(role)
  _BAKING_ROLE_MEMBERSHIP = tracked
  _BAKING_ROLE_COLLECTION_SIGNATURE = baking_role_collection_signature(roles)


def sync_exclusive_baking_roles(scene=None):
  global _BAKING_ROLE_MEMBERSHIP, _BAKING_ROLE_COLLECTION_SIGNATURE, _BAKING_ROLE_SYNCING
  if _BAKING_ROLE_SYNCING:
    return
  _, roles = baking_role_children(scene)
  if not roles:
    return

  role_priority = (LOW_COLLECTION, HIGH_COLLECTION, ALPHA_COLLECTION)
  export_collection = bpy.data.collections.get(SEND2UE_EXPORT_COLLECTION)
  _BAKING_ROLE_SYNCING = True
  try:
    next_membership = {}
    for obj in bpy.data.objects:
      previous = _BAKING_ROLE_MEMBERSHIP.get(obj.as_pointer(), set())
      memberships = object_baking_roles(obj, roles)
      if len(memberships) > 1:
        new_roles = [role for role in role_priority if role in memberships - previous]
        if new_roles:
          keep_role = new_roles[-1]
        else:
          keep_role = next(
            role for role in reversed(role_priority)
            if role in memberships
          )
        for role, collections in roles.items():
          if role == keep_role:
            continue
          for collection in collections:
            if collection in obj.users_collection and len(obj.users_collection) > 1:
              try:
                collection.objects.unlink(obj)
              except RuntimeError:
                pass
        memberships = object_baking_roles(obj, roles)
      if (
        export_collection is not None
        and export_collection in obj.users_collection
        and (
          HIGH_COLLECTION in memberships
          or ALPHA_COLLECTION in memberships
          or (LOW_COLLECTION in previous and LOW_COLLECTION not in memberships)
        )
      ):
        unlink_from_export_preserving_visibility(obj, export_collection, scene)
      if memberships:
        next_membership[obj.as_pointer()] = memberships
    _BAKING_ROLE_MEMBERSHIP = next_membership
    _BAKING_ROLE_COLLECTION_SIGNATURE = baking_role_collection_signature(roles)
  finally:
    _BAKING_ROLE_SYNCING = False


@bpy.app.handlers.persistent
def ensure_baking_collections_on_load(_unused):
  for scene in bpy.data.scenes:
    _, low_collection, _, _ = ensure_baking_collections(scene)
    if low_collection is not None:
      sync_bake_selection(
        scene,
        low_texture_set_names(painter_collection_meshes(low_collection)),
      )
    sync_exclusive_baking_roles(scene)
  remember_baking_role_membership()


def ensure_baking_collections_deferred():
  for scene in bpy.data.scenes:
    _, low_collection, _, _ = ensure_baking_collections(scene)
    if low_collection is not None:
      sync_bake_selection(
        scene,
        low_texture_set_names(painter_collection_meshes(low_collection)),
      )
    sync_exclusive_baking_roles(scene)
  remember_baking_role_membership()
  return None


@bpy.app.handlers.persistent
def sync_exclusive_baking_roles_on_depsgraph(_scene, _depsgraph):
  if depsgraph_requires_baking_role_sync(_scene, _depsgraph):
    sync_exclusive_baking_roles(_scene)


def get_baking_collections():
  """Look up the baking collections without creating or linking anything.

  Use this in UI draw code: a load handler and a deferred timer already create
  the collections, and Blender discourages modifying data during draw().
  """
  root = bpy.data.collections.get(BAKING_COLLECTION)
  children = {}
  if root is not None:
    for child in root.children:
      role = collection_role(child)
      if role in BAKING_ROLE_COLLECTIONS and role not in children:
        children[role] = child
  return (
    root,
    children.get(LOW_COLLECTION),
    children.get(HIGH_COLLECTION),
    children.get(ALPHA_COLLECTION),
  )


def painter_low_export_hierarchy():
  """Return the low meshes, parent chains, and Armature modifier rigs."""
  low_objects = set()
  baking_collection = bpy.data.collections.get(BAKING_COLLECTION)
  low_collection = find_baking_role_child(baking_collection, LOW_COLLECTION)

  if low_collection is not None:
    for obj in low_collection.all_objects:
      if obj.type != 'MESH' or is_painter_guide_mesh(obj):
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


def painter_export_preset_items():
  return tuple(
    (identifier, name, description)
    for identifier, name, description in PAINTER_EXPORT_PRESET_ITEMS
  )


def painter_export_preset_name(identifier):
  for item_identifier, name, _description in PAINTER_EXPORT_PRESET_ITEMS:
    if identifier == item_identifier:
      return name
  return EXPORT_PRESET_NAME


def _export_channel(dest_channel, src_map_name, src_channel=None, src_map_type='documentMap'):
  return {
    'destChannel': dest_channel,
    'srcChannel': src_channel or dest_channel,
    'srcMapType': src_map_type,
    'srcMapName': src_map_name,
  }


def _export_map_parameters():
  return {
    'fileFormat': 'png',
    'bitDepth': '8',
    'dithering': False,
  }


def _export_rgb_map(file_name, src_map_name, src_map_type='documentMap'):
  return {
    'fileName': file_name,
    'parameters': _export_map_parameters(),
    'channels': [
      _export_channel('R', src_map_name, src_map_type=src_map_type),
      _export_channel('G', src_map_name, src_map_type=src_map_type),
      _export_channel('B', src_map_name, src_map_type=src_map_type),
    ],
  }


def _export_luminance_map(file_name, src_map_name, src_map_type='documentMap'):
  return {
    'fileName': file_name,
    'parameters': _export_map_parameters(),
    'channels': [
      _export_channel('L', src_map_name, src_channel='L', src_map_type=src_map_type),
    ],
  }


def _unreal_v2_inline_maps():
  return [
    _export_rgb_map('$textureSet_Color', 'baseColor'),
    _export_rgb_map('$textureSet_Normal', 'Normal_DirectX', src_map_type='virtualMap'),
    {
      'fileName': '$textureSet_Extra',
      'parameters': _export_map_parameters(),
      'channels': [
        _export_channel('R', 'AO_Mixed', src_channel='L', src_map_type='virtualMap'),
        _export_channel('G', 'roughness', src_channel='L'),
        _export_channel('B', 'metallic', src_channel='L'),
      ],
    },
    _export_rgb_map('$textureSet_Emissive', 'emissive'),
    _export_luminance_map('$textureSet_Height', 'height'),
  ]


def painter_inline_export_preset_variants(name):
  if name != CLOTH_EXPORT_PRESET_NAME:
    return ()
  return ({
    'name': name,
    'maps': _unreal_v2_inline_maps() + [
      _export_rgb_map('$textureSet_SheenColor', 'sheencolor'),
      _export_luminance_map('$textureSet_SheenOpacity', 'sheenopacity'),
      _export_luminance_map('$textureSet_SheenRoughness', 'sheenroughness'),
    ],
  },)


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


def is_painter_guide_mesh(obj):
  """Treat wire-only viewport meshes as non-exporting Painter guides.

  The decision is intentionally independent of object names. Temporary H-key,
  viewport, and render visibility are not used because they can hide otherwise
  valid bake meshes during ordinary scene work.
  """
  return obj.type == 'MESH' and obj.display_type == 'WIRE'


def painter_collection_meshes(collection):
  return [
    obj for obj in collection_meshes(collection)
    if not is_painter_guide_mesh(obj)
  ]


def stripped_material_name(name):
  return name[len(MATERIAL_PREFIX):] if name.startswith(MATERIAL_PREFIX) else name


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
  high_by_base = defaultdict(list)
  for obj in high_objects:
    high_by_base[match_base(obj.name, 'high').lower()].append(obj)
  entries = defaultdict(lambda: {'objects': [], 'bases': set()})
  for low in low_objects:
    base = match_base(low.name, 'low').lower()
    matching_highs = high_by_base.get(base, ())
    if not matching_highs:
      continue
    for slot in low.material_slots:
      if not slot.material:
        continue
      texture_set = stripped_material_name(slot.material.name)
      for high in matching_highs:
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


def source_map_bake_name(texture_set, role):
  role_names = {
    'BaseColor': 'Color_baking',
    'Extra': 'Extra_baking',
    'ExtraR': 'ExtraR_baking',
    'Roughness': 'Roughness_baking',
    'Metallic': 'Metallic_baking',
    'Normal': 'Normal_baking',
  }
  if role not in role_names:
    raise ValueError(f'Unsupported source-map role: {role}')
  return f'T_{clean_name(texture_set)}_{role_names[role]}'


def painter_source_map_plan(texture_sets, texture_dir):
  """Return only complete, independently sampled Painter source channels.

  Extra is deliberately transported as three split grayscale images. Painter
  grayscale channels must not receive the packed RGB image because Painter
  would sample luminance and corrupt the original per-channel values.
  """
  texture_dir = Path(texture_dir)
  result = {}
  for texture_set in texture_sets:
    role_paths = {
      role: texture_dir / f'{source_map_bake_name(texture_set, role)}.png'
      for role in ('BaseColor', 'ExtraR', 'Roughness', 'Metallic')
    }
    existing = {
      role: str(path.resolve())
      for role, path in role_paths.items()
      if path.is_file()
    }
    if existing:
      result[texture_set] = existing
  return result


def painter_source_normal_mesh_map_plan(texture_sets, texture_dir):
  texture_dir = Path(texture_dir)
  result = {}
  for texture_set in texture_sets:
    path = texture_dir / f'{source_map_bake_name(texture_set, "Normal")}.png'
    if path.is_file():
      result[texture_set] = {
        'source_normal_texture': str(path.resolve()),
        'normal_convention': 'DIRECTX',
        'basis': 'LOW_TANGENT',
      }
  return result


def hash_nested_existing_paths(plan):
  result = {}
  for texture_set, entry in plan.items():
    if isinstance(entry, dict):
      role_hashes = {}
      for role, path_or_entry in entry.items():
        if isinstance(path_or_entry, dict):
          path_value = path_or_entry.get('source_normal_texture') or path_or_entry.get('path')
        else:
          path_value = path_or_entry
        if not path_value or role in {'normal_convention', 'basis'}:
          continue
        path = Path(path_value)
        if path.is_file():
          role_hashes[role] = file_hash(path)
      if role_hashes:
        result[texture_set] = role_hashes
    elif entry:
      path = Path(entry)
      if path.is_file():
        result[texture_set] = file_hash(path)
  return result


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
  low_objects = painter_collection_meshes(low_collection) if low_collection else []
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


def node_has_links(node):
  return any(socket.links for socket in node.inputs) or any(
    socket.links for socket in node.outputs
  )


def image_path_exists(image):
  source = image.filepath_raw or image.filepath
  return bool(source and Path(bpy.path.abspath(source)).is_file())


def stale_unlinked_image_nodes(material, preserve_images=None):
  if material is None or material.node_tree is None:
    return []
  preserve_images = set(preserve_images or ())
  stale_nodes = []
  for node in material.node_tree.nodes:
    if node.type != 'TEX_IMAGE' or node.image is None:
      continue
    if node.image in preserve_images:
      continue
    if node_has_links(node):
      continue
    if image_path_exists(node.image):
      continue
    stale_nodes.append(node)
  return stale_nodes


def remove_stale_unlinked_image_nodes(material, preserve_images=None):
  if material is None or material.node_tree is None:
    return 0
  stale_nodes = stale_unlinked_image_nodes(material, preserve_images)
  for node in stale_nodes:
    material.node_tree.nodes.remove(node)
  return len(stale_nodes)


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


def connect_painter_directx_normal(node_tree, image_node, principled_nodes):
  """Connect a Painter DirectX normal map to Blender with an explicit Y flip."""
  separate = (
    node_tree.nodes.get('Painter Normal DirectX Channels')
    or node_tree.nodes.new('ShaderNodeSeparateColor')
  )
  separate.name = 'Painter Normal DirectX Channels'
  separate.mode = 'RGB'
  invert_green = (
    node_tree.nodes.get('Painter Normal DirectX Green Flip')
    or node_tree.nodes.new('ShaderNodeMath')
  )
  invert_green.name = 'Painter Normal DirectX Green Flip'
  invert_green.operation = 'SUBTRACT'
  invert_green.inputs[0].default_value = 1.0
  combine = (
    node_tree.nodes.get('Painter Normal OpenGL')
    or node_tree.nodes.new('ShaderNodeCombineColor')
  )
  combine.name = 'Painter Normal OpenGL'
  combine.mode = 'RGB'
  normal_node = (
    node_tree.nodes.get('Painter Normal')
    or node_tree.nodes.new('ShaderNodeNormalMap')
  )
  normal_node.name = 'Painter Normal'
  normal_node.space = 'TANGENT'
  replace_socket_link(node_tree, image_node.outputs['Color'], separate.inputs['Color'])
  replace_socket_link(
    node_tree,
    separate.outputs['Green'],
    invert_green.inputs[1],
  )
  replace_socket_link(node_tree, separate.outputs['Red'], combine.inputs['Red'])
  replace_socket_link(node_tree, invert_green.outputs['Value'], combine.inputs['Green'])
  replace_socket_link(node_tree, separate.outputs['Blue'], combine.inputs['Blue'])
  replace_socket_link(node_tree, combine.outputs['Color'], normal_node.inputs['Color'])
  for principled in principled_nodes:
    replace_socket_link(node_tree, normal_node.outputs['Normal'], principled.inputs['Normal'])
  return normal_node


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
    material_applied = False
    texture_set = clean_name(stripped_material_name(material.name))
    paths = {
      role: texture_dir / f'{TEXTURE_PREFIX}{texture_set}_{role}.png'
      for role in PAINTER_CLOTH_TEXTURE_ROLES
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
      set_material_base_color_image(material, images['Color'])
      set_material_alpha_overlay_enabled(material, False)
      material_applied = True
    if images.get('Normal') is not None:
      normal_image = images['Normal']
      normal_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(normal_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = normal_image.name
      image_node.image = normal_image
      connect_painter_directx_normal(node_tree, image_node, principled_nodes)
      material_applied = True
    if images.get('Extra') is not None:
      extra_image = images['Extra']
      extra_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(extra_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = extra_image.name
      image_node.image = extra_image
      separate = node_tree.nodes.get('Painter Extra Channels') or node_tree.nodes.new('ShaderNodeSeparateColor')
      separate.name = 'Painter Extra Channels'
      replace_socket_link(node_tree, image_node.outputs['Color'], separate.inputs['Color'])
      for principled in principled_nodes:
        replace_socket_link(node_tree, separate.outputs['Green'], principled.inputs['Roughness'])
        replace_socket_link(node_tree, separate.outputs['Blue'], principled.inputs['Metallic'])
      material_applied = True
    if images.get('Emissive') is not None:
      emissive_image = images['Emissive']
      image_node = node_tree.nodes.get(emissive_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = emissive_image.name
      image_node.image = emissive_image
      for principled in principled_nodes:
        emission = principled.inputs.get('Emission Color') or principled.inputs.get('Emission')
        if emission is not None:
          replace_socket_link(node_tree, image_node.outputs['Color'], emission)
      material_applied = True
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
      material_applied = True
    if images.get('SheenColor') is not None:
      sheen_color_image = images['SheenColor']
      image_node = node_tree.nodes.get(sheen_color_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = sheen_color_image.name
      image_node.image = sheen_color_image
      image_node.label = 'Painter Sheen Color'
      for principled in principled_nodes:
        sheen_color = (
          principled.inputs.get('Sheen Tint')
          or principled.inputs.get('Sheen Color')
          or principled.inputs.get('Sheen')
        )
        if sheen_color is not None:
          replace_socket_link(node_tree, image_node.outputs['Color'], sheen_color)
      material_applied = True
    if images.get('SheenOpacity') is not None:
      sheen_opacity_image = images['SheenOpacity']
      sheen_opacity_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(sheen_opacity_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = sheen_opacity_image.name
      image_node.image = sheen_opacity_image
      image_node.label = 'Painter Sheen Opacity'
      separate = node_tree.nodes.get('Painter Sheen Opacity Channel') or node_tree.nodes.new('ShaderNodeSeparateColor')
      separate.name = 'Painter Sheen Opacity Channel'
      replace_socket_link(node_tree, image_node.outputs['Color'], separate.inputs['Color'])
      for principled in principled_nodes:
        sheen_opacity = (
          principled.inputs.get('Sheen Weight')
          or principled.inputs.get('Sheen Opacity')
        )
        if sheen_opacity is not None:
          replace_socket_link(node_tree, separate.outputs['Red'], sheen_opacity)
      material_applied = True
    if images.get('SheenRoughness') is not None:
      sheen_roughness_image = images['SheenRoughness']
      sheen_roughness_image.colorspace_settings.name = 'Non-Color'
      image_node = node_tree.nodes.get(sheen_roughness_image.name) or node_tree.nodes.new('ShaderNodeTexImage')
      image_node.name = sheen_roughness_image.name
      image_node.image = sheen_roughness_image
      image_node.label = 'Painter Sheen Roughness'
      separate = node_tree.nodes.get('Painter Sheen Roughness Channel') or node_tree.nodes.new('ShaderNodeSeparateColor')
      separate.name = 'Painter Sheen Roughness Channel'
      replace_socket_link(node_tree, image_node.outputs['Color'], separate.inputs['Color'])
      for principled in principled_nodes:
        sheen_roughness = principled.inputs.get('Sheen Roughness')
        if sheen_roughness is not None:
          replace_socket_link(node_tree, separate.outputs['Red'], sheen_roughness)
      material_applied = True
    force_material_opaque(material)
    remove_stale_unlinked_image_nodes(material, preserve_images=images.values())
    if material_applied:
      applied += 1
  return applied


def _ensure_managed_shader_node(node_tree, name, node_type):
  node = node_tree.nodes.get(name)
  if node is not None and node.bl_idname != node_type:
    node_tree.nodes.remove(node)
    node = None
  if node is None:
    node = node_tree.nodes.new(node_type)
    node.name = name
  return node


def normalize_meshy_required_roles(required_roles_by_texture_set, texture_sets):
  """Return an exact per-Texture-Set Color/Extra/Normal role contract.

  ``None`` preserves the legacy three-map behavior for ordinary callers.  A
  staged Meshy request passes the roles actually present in its immutable
  source package so optional Extra or Normal maps are never invented.
  """
  texture_sets = validate_exact_texture_set_ids(
    texture_sets,
    'Meshy canonical Texture Set IDs',
  )
  allowed = set(MESHY_PAINTER_CANONICAL_ROLES)
  if required_roles_by_texture_set is None:
    return {texture_set: set(allowed) for texture_set in texture_sets}
  if not isinstance(required_roles_by_texture_set, dict):
    raise RuntimeError('Meshy required roles must be a Texture Set dictionary')
  declared_sets = validate_exact_texture_set_ids(
    required_roles_by_texture_set,
    'Meshy required-role Texture Set IDs',
  )
  if declared_sets != texture_sets:
    raise RuntimeError(
      'Meshy required-role Texture Sets differ from the canonical set: '
      f'roles={sorted(declared_sets)}, canonical={sorted(texture_sets)}'
    )
  normalized = {}
  for texture_set in sorted(texture_sets):
    raw_roles = required_roles_by_texture_set.get(texture_set)
    if isinstance(raw_roles, str) or not isinstance(raw_roles, (list, tuple, set)):
      raise RuntimeError(f'Meshy roles must be an array for {texture_set}')
    roles = {str(role) for role in raw_roles}
    unsupported = roles - allowed
    if unsupported:
      raise RuntimeError(
        f'Unsupported Meshy Painter roles for {texture_set}: {sorted(unsupported)}'
      )
    normalized[texture_set] = roles
  return normalized


def apply_meshy_painter_textures_to_material(
  material,
  texture_set,
  texture_dir,
  *,
  images=None,
  required_roles=None,
):
  """Apply the source-backed Meshy roles to one candidate material.

  This intentionally does not touch emission, blend mode, Alpha, or unrelated
  image nodes.  Call it on a copied material and publish that copy only after the
  complete file/material transaction has passed verification.
  """
  texture_dir = Path(texture_dir).resolve()
  required_roles = set(required_roles or MESHY_PAINTER_CANONICAL_ROLES)
  unsupported = required_roles - set(MESHY_PAINTER_CANONICAL_ROLES)
  if unsupported:
    raise RuntimeError(
      f'Unsupported Meshy Painter roles for {texture_set}: {sorted(unsupported)}'
    )
  if not required_roles:
    raise PainterApplyNoMaterialsError(
      f'Meshy Painter has no source-backed roles for material {texture_set}'
    )
  paths = {
    role: texture_dir / f'{TEXTURE_PREFIX}{texture_set}_{role}.png'
    for role in required_roles
  }
  missing = [role for role, path in paths.items() if not path.is_file()]
  if missing:
    if len(missing) == len(paths):
      raise PainterApplyNoMaterialsError(
        f'Meshy Painter textures did not match material {texture_set}'
      )
    raise RuntimeError(
      f'Meshy Painter material group is incomplete for {texture_set}: '
      + ', '.join(missing)
    )
  material.use_nodes = True
  node_tree = material.node_tree
  if node_tree is None:
    raise RuntimeError(f'Meshy Painter material has no node tree: {material.name}')
  principled_nodes = [
    node for node in node_tree.nodes if node.type == 'BSDF_PRINCIPLED'
  ]
  if not principled_nodes:
    raise RuntimeError(f'Meshy Painter material has no Principled BSDF: {material.name}')

  if images is None:
    images = {role: load_or_reload_image(path) for role, path in paths.items()}
  elif set(images) != set(paths) or any(images[role] is None for role in paths):
    raise RuntimeError(
      f'Meshy Painter transaction images are incomplete for {texture_set}'
    )
  if 'Color' in required_roles:
    images['Color'].colorspace_settings.name = 'sRGB'
    color_node = _ensure_managed_shader_node(
      node_tree,
      'Painter Color',
      'ShaderNodeTexImage',
    )
    color_node.image = images['Color']
    color_node.label = 'Painter Color'
    for principled in principled_nodes:
      base_color = principled.inputs.get('Base Color')
      if base_color is None:
        continue
      target = base_color
      if base_color.is_linked:
        upstream = base_color.links[0].from_node
        if upstream.name.startswith('__SubstanceToolsAlphaMix_'):
          target = upstream.inputs[1]
      replace_socket_link(node_tree, color_node.outputs['Color'], target)

  if 'Extra' in required_roles:
    extra_image = images['Extra']
    extra_image.colorspace_settings.name = 'Non-Color'
    extra_node = _ensure_managed_shader_node(
      node_tree,
      'Painter Extra',
      'ShaderNodeTexImage',
    )
    extra_node.image = extra_image
    extra_node.label = 'Painter Extra'
    extra_separate = _ensure_managed_shader_node(
      node_tree,
      'Painter Extra Channels',
      'ShaderNodeSeparateColor',
    )
    extra_separate.mode = 'RGB'
    replace_socket_link(
      node_tree,
      extra_node.outputs['Color'],
      extra_separate.inputs['Color'],
    )
    for principled in principled_nodes:
      replace_socket_link(
        node_tree,
        extra_separate.outputs['Green'],
        principled.inputs['Roughness'],
      )
      replace_socket_link(
        node_tree,
        extra_separate.outputs['Blue'],
        principled.inputs['Metallic'],
      )

  if 'Normal' in required_roles:
    normal_image = images['Normal']
    normal_image.colorspace_settings.name = 'Non-Color'
    normal_node = _ensure_managed_shader_node(
      node_tree,
      'Painter Normal Texture',
      'ShaderNodeTexImage',
    )
    normal_node.image = normal_image
    normal_node.label = 'Painter Normal (DirectX)'
    for name, node_type in (
      ('Painter Normal DirectX Channels', 'ShaderNodeSeparateColor'),
      ('Painter Normal DirectX Green Flip', 'ShaderNodeMath'),
      ('Painter Normal OpenGL', 'ShaderNodeCombineColor'),
      ('Painter Normal', 'ShaderNodeNormalMap'),
    ):
      _ensure_managed_shader_node(node_tree, name, node_type)
    connect_painter_directx_normal(node_tree, normal_node, principled_nodes)
  return 1


class MeshyMaterialApplyTransaction:
  """Prepare copied materials and atomically publish them through mesh slots."""

  def __init__(
    self,
    low_objects,
    texture_dir,
    canonical_texture_sets,
    required_roles_by_texture_set=None,
  ):
    self.low_objects = list(low_objects)
    self.texture_dir = Path(texture_dir).resolve()
    self.slot_records = []
    self.original_names = {}
    self.candidates = {}
    self.owned_images = {}
    self.texture_sets = {}
    self.canonical_texture_sets = validate_exact_texture_set_ids(
      canonical_texture_sets,
      'Meshy canonical Texture Set IDs',
    )
    self.required_roles_by_texture_set = normalize_meshy_required_roles(
      required_roles_by_texture_set,
      self.canonical_texture_sets,
    )
    self.swapped = False
    self.committed = False
    self._collect_slots()

  def _collect_slots(self):
    allowed_data = {
      obj.data for obj in self.low_objects
      if obj.type == 'MESH' and obj.data is not None
    }
    for obj in bpy.data.objects:
      if obj.type == 'MESH' and obj.data in allowed_data and obj not in self.low_objects:
        raise RuntimeError(
          f'Meshy low mesh data is shared outside the apply set: {obj.name}'
        )
    seen_slots = set()
    for obj in self.low_objects:
      if obj.type != 'MESH' or obj.data is None:
        continue
      for index, material in enumerate(obj.data.materials):
        if material is None:
          continue
        key = (obj.data.as_pointer(), index)
        if key in seen_slots:
          continue
        seen_slots.add(key)
        self.slot_records.append((obj.data, index, material))
    if not self.slot_records:
      raise PainterApplyNoMaterialsError('Meshy Painter apply has no low material slots')

    target_keys = {
      (data.as_pointer(), index)
      for data, index, _material in self.slot_records
    }
    originals = {material for _data, _index, material in self.slot_records}
    for mesh in bpy.data.meshes:
      for index, material in enumerate(mesh.materials):
        if material in originals and (mesh.as_pointer(), index) not in target_keys:
          raise RuntimeError(
            f'Meshy low material is shared outside the apply set: {material.name}'
          )
    for material in originals:
      slot_users = sum(
        1 for mesh in bpy.data.meshes
        for candidate in mesh.materials
        if candidate == material
      )
      if material.users != slot_users:
        raise RuntimeError(
          f'Meshy low material has non-slot users and cannot be swapped safely: '
          f'{material.name}'
        )
      self.original_names[material] = material.name
      self.texture_sets[material] = stripped_material_name(material.name)
    material_sets = validate_exact_texture_set_ids(
      self.texture_sets.values(),
      'Meshy low material Texture Set IDs',
    )
    if material_sets != self.canonical_texture_sets:
      raise RuntimeError(
        'Meshy low material Texture Set IDs differ from the state-pinned IDs: '
        f'materials={sorted(material_sets)}, '
        f'state={sorted(self.canonical_texture_sets)}'
      )
    active_materials = {
      material
      for material, texture_set in self.texture_sets.items()
      if self.required_roles_by_texture_set[texture_set]
    }
    self.slot_records = [
      record for record in self.slot_records if record[2] in active_materials
    ]
    self.original_names = {
      material: name
      for material, name in self.original_names.items()
      if material in active_materials
    }
    self.texture_sets = {
      material: texture_set
      for material, texture_set in self.texture_sets.items()
      if material in active_materials
    }
    if not self.slot_records:
      raise PainterApplyNoMaterialsError(
        'Meshy Painter apply has no source-backed material roles'
      )

  def _images_for_texture_set(self, texture_set):
    images = {}
    for role in self.required_roles_by_texture_set[texture_set]:
      path = (
        self.texture_dir / f'{TEXTURE_PREFIX}{texture_set}_{role}.png'
      ).resolve()
      image = self.owned_images.get(path)
      if image is None:
        image = bpy.data.images.load(str(path), check_existing=False)
        image.name = (
          f'__ST_PainterApply_{clean_name(texture_set)}_{role}_'
          f'{image.as_pointer():x}'
        )
        self.owned_images[path] = image
      images[role] = image
    return images

  def _discard_owned_images(self):
    for image in list(self.owned_images.values()):
      try:
        if image.users == 0:
          bpy.data.images.remove(image)
      except (ReferenceError, RuntimeError):
        pass
    self.owned_images.clear()

  def prepare(self):
    try:
      for original in sorted(self.original_names, key=lambda value: value.name_full):
        candidate = original.copy()
        candidate.name = f'__ST_PainterCandidate_{original.as_pointer():x}'
        self.candidates[original] = candidate
        apply_meshy_painter_textures_to_material(
          candidate,
          self.texture_sets[original],
          self.texture_dir,
          images=self._images_for_texture_set(self.texture_sets[original]),
          required_roles=self.required_roles_by_texture_set[
            self.texture_sets[original]
          ],
        )
      managed_roles = verify_painter_material_roles(
        [],
        self.texture_dir,
        self.required_roles_by_texture_set,
        material_texture_sets={
          candidate: self.texture_sets[original]
          for original, candidate in self.candidates.items()
        },
      )
      return len(self.candidates), managed_roles
    except Exception:
      self.rollback()
      raise

  def swap(self):
    if self.swapped:
      return
    try:
      for original, original_name in self.original_names.items():
        original.name = f'__ST_PainterRollback_{original.as_pointer():x}'
        self.candidates[original].name = original_name
      self.swapped = True
      for data, index, original in self.slot_records:
        data.materials[index] = self.candidates[original]
      still_used = [
        original.name for original in self.original_names
        if original.users != 0
      ]
      if still_used:
        raise RuntimeError(
          f'Meshy material slot swap left original users: {still_used}'
        )
    except Exception:
      self.rollback()
      raise

  def rollback(self):
    if self.committed:
      return
    if self.swapped:
      for data, index, original in self.slot_records:
        data.materials[index] = original
    for original, original_name in self.original_names.items():
      candidate = self.candidates.get(original)
      if candidate is not None:
        candidate.name = f'__ST_DiscardedCandidate_{candidate.as_pointer():x}'
      original.name = original_name
    for candidate in list(self.candidates.values()):
      if candidate.users == 0:
        bpy.data.materials.remove(candidate)
    self.candidates.clear()
    self._discard_owned_images()
    self.swapped = False

  def commit(self):
    self.committed = True
    for original in list(self.original_names):
      try:
        if original.users == 0:
          bpy.data.materials.remove(original)
      except (ReferenceError, RuntimeError):
        pass


def painter_export_canonical_replacements(result):
  replacements = []
  for files in result.get('textures', {}).values():
    for file_value in files:
      source = Path(file_value).resolve()
      if not source.is_file():
        continue
      stem = source.stem
      if stem.startswith(f'{TEXTURE_PREFIX}{MATERIAL_PREFIX}'):
        prefix_length = len(TEXTURE_PREFIX) + len(MATERIAL_PREFIX)
        canonical_stem = f'{TEXTURE_PREFIX}{stem[prefix_length:]}'
      elif stem.startswith(MATERIAL_PREFIX):
        canonical_stem = f'{TEXTURE_PREFIX}{stem[len(MATERIAL_PREFIX):]}'
      elif stem.startswith(TEXTURE_PREFIX):
        canonical_stem = stem
      else:
        canonical_stem = f'{TEXTURE_PREFIX}{stem}'
      target = source.with_name(f'{canonical_stem}{source.suffix.lower()}').resolve()
      replacements.append((source, target))
  return replacements


def filter_meshy_painter_export_result(
  result,
  canonical_texture_sets,
  required_roles_by_texture_set=None,
):
  """Select only state-pinned, source-backed exports for Meshy apply.

  Painter presets may additionally emit Height, Emissive, or cloth maps.  Those
  files are outside the Meshy replacement contract, so they remain untouched in
  Painter staging and never enter the canonical file transaction.
  """
  expected_sets = validate_exact_texture_set_ids(
    canonical_texture_sets,
    'Meshy canonical Texture Set IDs',
  )
  role_contract = normalize_meshy_required_roles(
    required_roles_by_texture_set,
    expected_sets,
  )
  filtered_textures = {}
  for group, files in (result.get('textures') or {}).items():
    if not isinstance(files, (list, tuple)):
      raise RuntimeError('Painter export result texture entries must be lists')
    selected = []
    for file_value in files:
      single_result = {'textures': {'candidate': [file_value]}}
      replacements = painter_export_canonical_replacements(single_result)
      if not replacements:
        continue
      _source, target = replacements[0]
      role = painter_export_role(target)
      texture_set = painter_export_texture_set(target, role)
      if texture_set in expected_sets and role in role_contract[texture_set]:
        selected.append(file_value)
    if selected:
      filtered_textures[group] = selected
  filtered = dict(result)
  filtered['textures'] = filtered_textures
  return filtered


def validate_meshy_painter_export_group(
  result,
  low_objects,
  expected_resolution=None,
  *,
  canonical_texture_sets=None,
  required_roles_by_texture_set=None,
):
  replacements = painter_export_canonical_replacements(result)
  expected_sets = (
    validate_exact_texture_set_ids(
      canonical_texture_sets,
      'Meshy canonical Texture Set IDs',
    )
    if canonical_texture_sets is not None else {
      clean_name(stripped_material_name(slot.material.name))
      for obj in low_objects
      for slot in obj.material_slots
      if slot.material
    }
  )
  role_contract = normalize_meshy_required_roles(
    required_roles_by_texture_set,
    expected_sets,
  )
  active_expected_sets = {
    texture_set for texture_set, roles in role_contract.items() if roles
  }
  recognized_roles = set(MESHY_PAINTER_CANONICAL_ROLES)
  present = defaultdict(set)
  decoded = []
  try:
    for source, target in replacements:
      if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f'Painter export is missing or empty: {source}')
      role = next(
        (value for value in recognized_roles if target.stem.endswith(f'_{value}')),
        None,
      )
      if role is not None:
        texture_set = target.stem[len(TEXTURE_PREFIX):-len(f'_{role}')]
        present[texture_set].add(role)
        image = bpy.data.images.load(str(source), check_existing=False)
        decoded.append(image)
        width, height = (int(value) for value in image.size[:])
        if width <= 0 or height <= 0 or width != height:
          raise RuntimeError(f'Painter export has invalid dimensions: {source}')
        if expected_resolution and (width != int(expected_resolution) or height != int(expected_resolution)):
          raise RuntimeError(
            f'Painter export resolution differs from {expected_resolution}: '
            f'{source} is {width}x{height}'
          )
    missing = {
      texture_set: sorted(role_contract[texture_set] - present.get(texture_set, set()))
      for texture_set in sorted(expected_sets)
      if role_contract[texture_set] - present.get(texture_set, set())
    }
    if missing:
      details = '; '.join(
        f'{texture_set}: {", ".join(roles)}' for texture_set, roles in missing.items()
      )
      raise RuntimeError(f'Painter export group is incomplete ({details})')
    if canonical_texture_sets is not None:
      validate_exact_texture_set_ids(present, 'Painter export Texture Set IDs')
      if set(present) != active_expected_sets:
        raise RuntimeError(
          'Painter export Texture Set IDs differ from the state-pinned IDs: '
          f'export={sorted(present)}, '
          f'source-backed={sorted(active_expected_sets)}'
        )
      non_exact = {
        texture_set: sorted(roles)
        for texture_set, roles in present.items()
        if roles != role_contract[texture_set]
      }
      if non_exact:
        raise RuntimeError(
          f'Painter export roles differ from the source-backed contract: {non_exact}'
        )
    return {
      'texture_sets': sorted(active_expected_sets),
      'roles': {key: sorted(value) for key, value in sorted(present.items())},
      'files': len(replacements),
    }
  finally:
    for image in decoded:
      if image.users == 0:
        bpy.data.images.remove(image)


def remove_painter_export_source_files(result):
  """Remove non-canonical Painter export names after a successful apply."""
  removed = []
  for source, target in painter_export_canonical_replacements(result):
    if source != target and source.is_file():
      source.unlink()
      removed.append(str(source))
  return removed


def painter_export_role(path):
  stem = Path(path).stem
  return next(
    (
      role for role in sorted(PAINTER_CLOTH_TEXTURE_ROLES, key=len, reverse=True)
      if stem.endswith(f'_{role}')
    ),
    None,
  )


def painter_export_texture_set(path, role=None):
  path = Path(path)
  role = role or painter_export_role(path)
  if role is None or not path.stem.startswith(TEXTURE_PREFIX):
    return None
  suffix = f'_{role}'
  return path.stem[len(TEXTURE_PREFIX):-len(suffix)]


def painter_texture_set_match_token(value):
  value = stripped_material_name(str(value or ''))
  return ''.join(character for character in value.casefold() if character.isalnum())


def validate_exact_texture_set_ids(values, label):
  tokens = defaultdict(list)
  for value in values:
    canonical = str(value)
    token = painter_texture_set_match_token(canonical)
    if not canonical or not token:
      raise RuntimeError(f'{label} contains an empty Texture Set ID')
    tokens[token].append(canonical)
  collisions = {
    token: names for token, names in tokens.items()
    if len(set(names)) > 1
  }
  if collisions:
    raise RuntimeError(
      f'{label} contains Painter name collisions: {collisions}'
    )
  noncanonical = [
    name for names in tokens.values() for name in names
    if clean_name(name) != name
  ]
  if noncanonical:
    raise RuntimeError(
      f'{label} contains non-canonical Texture Set IDs: {sorted(noncanonical)}'
    )
  return {name for names in tokens.values() for name in names}


def _normalized_path(path):
  return os.path.normcase(str(Path(path).resolve()))


def _painter_result_source_paths(result):
  paths = []
  for files in result.get('textures', {}).values():
    if not isinstance(files, (list, tuple)):
      raise RuntimeError('Painter export result texture entries must be lists')
    paths.extend(Path(value).resolve() for value in files)
  return paths


class PainterCanonicalApplyTransaction:
  """Keep canonical texture replacement rollback data alive through material apply.

  ``install`` copies, verifies, and installs Painter exports but deliberately keeps
  both the original Painter files and rollback copies.  Only ``commit`` removes
  the Painter-prefixed sources and transaction directory.  ``rollback`` restores
  every canonical target as well as any staging source removed by a failed commit.
  """

  PREFIX = '.substance_tools_apply_'

  def __init__(
    self,
    result,
    *,
    texture_dir=None,
    allowed_roles=None,
    expected_texture_sets=None,
    expected_roles_by_texture_set=None,
    require_noncanonical_sources=False,
  ):
    self.result = result
    self.texture_dir = Path(texture_dir).resolve() if texture_dir else None
    self.allowed_roles = set(allowed_roles or ())
    self.expected_texture_sets = validate_exact_texture_set_ids(
      expected_texture_sets or (),
      'Expected low materials',
    )
    self.expected_roles_by_texture_set = (
      normalize_meshy_required_roles(
        expected_roles_by_texture_set,
        self.expected_texture_sets,
      )
      if expected_roles_by_texture_set is not None else None
    )
    if self.expected_roles_by_texture_set is not None:
      contracted_roles = {
        role
        for roles in self.expected_roles_by_texture_set.values()
        for role in roles
      }
      if self.allowed_roles and self.allowed_roles != contracted_roles:
        raise RuntimeError(
          'Painter transaction allowed roles differ from its per-set contract'
        )
      self.allowed_roles = contracted_roles
    self.require_noncanonical_sources = bool(require_noncanonical_sources)
    self.replacements = painter_export_canonical_replacements(result)
    self.transaction_parent = None
    self.transaction_root = None
    self.source_copies = {}
    self.target_backups = {}
    self.target_hashes = {}
    self.installed_targets = []
    self.removed_sources = []
    self.state = 'NEW'
    self._validate_plan()

  @property
  def canonical_files(self):
    return [str(target) for _source, target in self.replacements]

  def _validate_plan(self):
    raw_sources = _painter_result_source_paths(self.result)
    if self.texture_dir is not None:
      if not self.texture_dir.is_dir():
        raise RuntimeError(f'Painter texture directory does not exist: {self.texture_dir}')
      for source in raw_sources:
        if source.parent != self.texture_dir:
          raise RuntimeError(
            f'Painter export source must be directly inside texture_dir: {source}'
          )
        if not source.is_file():
          raise RuntimeError(f'Painter export source is missing: {source}')
      if len(raw_sources) != len(self.replacements):
        raise RuntimeError('Painter export result contains a missing source file')

    target_sources = defaultdict(set)
    for source, target in self.replacements:
      target_sources[_normalized_path(target)].add(_normalized_path(source))
    collisions = [target for target, sources in target_sources.items() if len(sources) > 1]
    if collisions:
      raise RuntimeError(
        'Painter export maps multiple files to the same canonical target: '
        + ', '.join(sorted(collisions))
      )
    if not self.replacements:
      raise RuntimeError('Painter export contains no existing texture files')

    self.transaction_parent = self.replacements[0][1].parent
    if any(target.parent != self.transaction_parent for _source, target in self.replacements):
      raise RuntimeError('Painter canonicalization requires one texture directory')
    if self.texture_dir is not None and self.transaction_parent != self.texture_dir:
      raise RuntimeError(
        f'Painter canonical targets must be directly inside texture_dir: '
        f'{self.transaction_parent}'
      )

    if self.allowed_roles:
      observed_roles = set()
      roles_by_texture_set = defaultdict(set)
      role_counts_by_texture_set = defaultdict(lambda: defaultdict(int))
      for source, target in self.replacements:
        role = painter_export_role(target)
        if role not in self.allowed_roles:
          raise RuntimeError(
            f'Painter export role is not allowed in this transaction: {source.name}'
          )
        if self.require_noncanonical_sources and source == target:
          raise RuntimeError(
            f'Painter staging source must not already be canonical: {source.name}'
          )
        observed_roles.add(role)
        texture_set = painter_export_texture_set(target, role)
        if not texture_set:
          raise RuntimeError(f'Painter export has no canonical Texture Set: {source.name}')
        roles_by_texture_set[texture_set].add(role)
        role_counts_by_texture_set[texture_set][role] += 1
      missing_roles = self.allowed_roles - observed_roles
      if missing_roles:
        raise RuntimeError(
          'Painter export transaction is missing required roles: '
          + ', '.join(sorted(missing_roles))
        )
      if self.expected_texture_sets:
        validate_exact_texture_set_ids(
          roles_by_texture_set,
          'Painter export',
        )
        if set(roles_by_texture_set) != self.expected_texture_sets:
          raise RuntimeError(
            'Painter export Texture Sets differ from low materials: '
            f'export={sorted(roles_by_texture_set)}, '
            f'low={sorted(self.expected_texture_sets)}'
          )
        expected_roles = self.expected_roles_by_texture_set or {
          texture_set: set(self.allowed_roles)
          for texture_set in self.expected_texture_sets
        }
        incomplete = {
          texture_set: {
            role: role_counts_by_texture_set[texture_set].get(role, 0)
            for role in sorted(expected_roles[texture_set])
            if role_counts_by_texture_set[texture_set].get(role, 0) != 1
          }
          for texture_set in sorted(self.expected_texture_sets)
          if roles_by_texture_set.get(texture_set, set()) != expected_roles[texture_set]
          or any(
            count != 1
            for count in role_counts_by_texture_set[texture_set].values()
          )
        }
        if incomplete:
          raise RuntimeError(
            'Painter export does not contain exactly one managed role group per '
            f'Texture Set: {incomplete}'
          )

  def _copy_verified(self, source, target, label):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if file_hash(source) != file_hash(target):
      raise RuntimeError(f'Painter {label} verification failed: {source}')

  def _cleanup(self):
    root = self.transaction_root
    if root is None or not root.exists():
      return
    if (
      root.parent != self.transaction_parent
      or not root.name.startswith(self.PREFIX)
    ):
      raise RuntimeError(f'Refusing to clean unexpected transaction path: {root}')
    shutil.rmtree(root)

  def _restore_sources(self):
    for source in self.removed_sources:
      backup = self.source_copies.get(source)
      if backup is None or not backup.is_file():
        raise RuntimeError(f'Painter staging source backup is missing: {source}')
      restore_path = self.transaction_root / 'restore_source' / source.name
      self._copy_verified(backup, restore_path, 'source restore staging')
      os.replace(restore_path, source)
      if file_hash(source) != file_hash(backup):
        raise RuntimeError(f'Painter staging source restore failed: {source}')

  def _restore_targets(self):
    for target in reversed(self.installed_targets):
      backup = self.target_backups.get(target)
      if backup is None:
        if target.is_file():
          target.unlink()
        continue
      restore_path = self.transaction_root / 'restore_target' / target.name
      self._copy_verified(backup, restore_path, 'target restore staging')
      os.replace(restore_path, target)
      if file_hash(target) != file_hash(backup):
        raise RuntimeError(f'Painter canonical rollback failed: {target}')

  def _reload_restored_images(self):
    restored = {_normalized_path(target) for target in self.installed_targets}
    for image in bpy.data.images:
      image_path = bpy.path.abspath(image.filepath_raw or image.filepath)
      if image_path and _normalized_path(image_path) in restored:
        try:
          image.reload()
        except RuntimeError:
          pass

  def install(self):
    if self.state != 'NEW':
      raise RuntimeError(f'Painter transaction cannot install from {self.state}')
    self.transaction_root = Path(tempfile.mkdtemp(
      prefix=self.PREFIX,
      dir=str(self.transaction_parent),
    )).resolve()
    try:
      for index, (source, target) in enumerate(self.replacements):
        source_hash = file_hash(source)
        self.target_hashes[target] = source_hash
        if source != target:
          source_copy = (
            self.transaction_root / 'source' / f'{index:04d}{source.suffix.lower()}'
          )
          self._copy_verified(source, source_copy, 'source staging')
          self.source_copies[source] = source_copy
        if source == target or (
          target.is_file() and source_hash == file_hash(target)
        ):
          continue
        if target.is_file():
          backup = (
            self.transaction_root / 'rollback' / f'{index:04d}{target.suffix.lower()}'
          )
          self._copy_verified(target, backup, 'rollback')
          self.target_backups[target] = backup
        install_path = (
          self.transaction_root / 'install' / f'{index:04d}{target.suffix.lower()}'
        )
        self._copy_verified(source, install_path, 'install staging')
        os.replace(install_path, target)
        self.installed_targets.append(target)
        if file_hash(target) != source_hash:
          raise RuntimeError(f'Painter canonical verification failed: {target}')
      self.state = 'INSTALLED'
      return self.canonical_files
    except Exception:
      self.state = 'FAILED'
      try:
        self._restore_targets()
        self._restore_sources()
        self._reload_restored_images()
      finally:
        self._cleanup()
      raise

  def rollback(self):
    if self.state in {'ROLLED_BACK', 'COMMITTED'}:
      return
    try:
      self._restore_targets()
      self._restore_sources()
      self._reload_restored_images()
    finally:
      self._cleanup()
      self.state = 'ROLLED_BACK'

  def commit(self, *, remove_sources=True):
    if self.state != 'INSTALLED':
      raise RuntimeError(f'Painter transaction cannot commit from {self.state}')
    try:
      for target, expected_hash in self.target_hashes.items():
        if not target.is_file() or file_hash(target) != expected_hash:
          raise RuntimeError(f'Painter canonical file changed before commit: {target}')
      if remove_sources:
        for source, target in self.replacements:
          if source == target or not source.is_file():
            continue
          source.unlink()
          self.removed_sources.append(source)
      self._cleanup()
      self.state = 'COMMITTED'
      return self.canonical_files
    except Exception:
      self.rollback()
      raise


def begin_painter_canonical_apply_transaction(
  result,
  *,
  texture_dir=None,
  allowed_roles=None,
  expected_texture_sets=None,
  expected_roles_by_texture_set=None,
  require_noncanonical_sources=False,
):
  transaction = PainterCanonicalApplyTransaction(
    result,
    texture_dir=texture_dir,
    allowed_roles=allowed_roles,
    expected_texture_sets=expected_texture_sets,
    expected_roles_by_texture_set=expected_roles_by_texture_set,
    require_noncanonical_sources=require_noncanonical_sources,
  )
  transaction.install()
  return transaction


def _socket_has_upstream_node(socket, target_node, visited=None):
  visited = set() if visited is None else visited
  for link in socket.links:
    node = link.from_node
    if node == target_node:
      return True
    if node in visited:
      continue
    visited.add(node)
    if any(
      _socket_has_upstream_node(input_socket, target_node, visited)
      for input_socket in node.inputs
    ):
      return True
  return False


def _socket_has_direct_source(socket, node, socket_name):
  return any(
    link.from_node == node and link.from_socket.name == socket_name
    for link in socket.links
  )


def _managed_image_node(node_tree, name, expected_path, colorspace):
  node = node_tree.nodes.get(name)
  if node is None or node.type != 'TEX_IMAGE' or node.image is None:
    raise RuntimeError(f'Painter managed image node is missing: {name}')
  image_path = bpy.path.abspath(node.image.filepath_raw or node.image.filepath)
  if _normalized_path(image_path) != _normalized_path(expected_path):
    raise RuntimeError(f'Painter managed image path differs: {name}')
  if node.image.colorspace_settings.name != colorspace:
    raise RuntimeError(
      f'Painter managed image colorspace differs for {name}: '
      f'{node.image.colorspace_settings.name}'
    )
  return node


def verify_painter_material_roles(
  low_objects,
  texture_dir,
  required_roles,
  *,
  material_texture_sets=None,
):
  texture_dir = Path(texture_dir).resolve()
  role_contract = None
  if isinstance(required_roles, dict):
    role_contract = normalize_meshy_required_roles(
      required_roles,
      required_roles,
    )
    active_role_contract = {
      texture_set: roles
      for texture_set, roles in role_contract.items()
      if roles
    }
    shared_required_roles = None
  else:
    shared_required_roles = set(required_roles)
  material_texture_sets = material_texture_sets or {}
  materials = set(material_texture_sets) or {
    slot.material
    for obj in low_objects
    for slot in obj.material_slots
    if slot.material
  }
  verified = {}
  represented_texture_sets = set()
  for material in materials:
    texture_set = (
      str(material_texture_sets[material])
      if material in material_texture_sets
      else clean_name(stripped_material_name(material.name))
    )
    if role_contract is not None:
      if texture_set not in role_contract:
        raise RuntimeError(
          f'Painter material Texture Set is outside the role contract: {texture_set}'
        )
      if not role_contract[texture_set]:
        continue
      material_required_roles = role_contract[texture_set]
    else:
      material_required_roles = shared_required_roles
    if material.node_tree is None:
      raise RuntimeError(f'Painter material has no node tree: {material.name}')
    node_tree = material.node_tree
    principled_nodes = [
      node for node in node_tree.nodes if node.type == 'BSDF_PRINCIPLED'
    ]
    if not principled_nodes:
      raise RuntimeError(f'Painter material has no Principled BSDF: {material.name}')
    represented_texture_sets.add(texture_set)
    node_names = {
      'Color': 'Painter Color',
      'Extra': 'Painter Extra',
      'Normal': 'Painter Normal Texture',
    }
    role_colorspaces = {
      'Color': 'sRGB',
      'Extra': 'Non-Color',
      'Normal': 'Non-Color',
    }
    image_nodes = {
      role: _managed_image_node(
        node_tree,
        node_names[role],
        texture_dir / f'{TEXTURE_PREFIX}{texture_set}_{role}.png',
        role_colorspaces[role],
      )
      for role in material_required_roles
    }

    if 'Color' in material_required_roles:
      for principled in principled_nodes:
        if not _socket_has_upstream_node(
          principled.inputs['Base Color'], image_nodes['Color']
        ):
          raise RuntimeError(
            f'Painter Color is not connected to {material.name}'
          )
    if 'Extra' in material_required_roles:
      separate = node_tree.nodes.get('Painter Extra Channels')
      if (
        separate is None
        or separate.type != 'SEPARATE_COLOR'
        or separate.mode != 'RGB'
      ):
        raise RuntimeError(f'Painter Extra channel node is missing: {material.name}')
      if not _socket_has_direct_source(
        separate.inputs['Color'], image_nodes['Extra'], 'Color'
      ):
        raise RuntimeError(f'Painter Extra image is not connected: {material.name}')
      for principled in principled_nodes:
        roughness_links = principled.inputs['Roughness'].links
        metallic_links = principled.inputs['Metallic'].links
        if not any(
          link.from_node == separate and link.from_socket.name == 'Green'
          for link in roughness_links
        ):
          raise RuntimeError(f'Painter Extra.G is not Roughness: {material.name}')
        if not any(
          link.from_node == separate and link.from_socket.name == 'Blue'
          for link in metallic_links
        ):
          raise RuntimeError(f'Painter Extra.B is not Metallic: {material.name}')
    if 'Normal' in material_required_roles:
      separate = node_tree.nodes.get('Painter Normal DirectX Channels')
      green_flip = node_tree.nodes.get('Painter Normal DirectX Green Flip')
      combine = node_tree.nodes.get('Painter Normal OpenGL')
      normal_map = node_tree.nodes.get('Painter Normal')
      if (
        separate is None
        or separate.type != 'SEPARATE_COLOR'
        or separate.mode != 'RGB'
      ):
        raise RuntimeError(f'Painter Normal channel node is missing: {material.name}')
      if green_flip is None or green_flip.type != 'MATH' or (
        green_flip.operation != 'SUBTRACT'
      ) or abs(float(green_flip.inputs[0].default_value) - 1.0) > 1e-6:
        raise RuntimeError(f'Painter Normal green flip is invalid: {material.name}')
      if (
        combine is None
        or combine.type != 'COMBINE_COLOR'
        or combine.mode != 'RGB'
      ):
        raise RuntimeError(f'Painter Normal combine node is missing: {material.name}')
      if normal_map is None or normal_map.type != 'NORMAL_MAP' or (
        normal_map.space != 'TANGENT'
      ):
        raise RuntimeError(f'Painter tangent Normal Map is invalid: {material.name}')
      exact_links = (
        (separate.inputs['Color'], image_nodes['Normal'], 'Color'),
        (green_flip.inputs[1], separate, 'Green'),
        (combine.inputs['Red'], separate, 'Red'),
        (combine.inputs['Green'], green_flip, 'Value'),
        (combine.inputs['Blue'], separate, 'Blue'),
        (normal_map.inputs['Color'], combine, 'Color'),
      )
      if not all(
        _socket_has_direct_source(socket, node, socket_name)
        for socket, node, socket_name in exact_links
      ):
        raise RuntimeError(f'Painter Normal direct chain is invalid: {material.name}')
      for principled in principled_nodes:
        if not _socket_has_direct_source(
          principled.inputs['Normal'], normal_map, 'Normal'
        ):
          raise RuntimeError(
            f'Painter Normal is not connected to {material.name}'
          )
    receipt_name = (
      f'{MATERIAL_PREFIX}{texture_set}'
      if material in material_texture_sets
      else material.name
    )
    verified[receipt_name] = sorted(material_required_roles)
  if not verified:
    raise RuntimeError('Painter apply found no low materials to verify')
  if role_contract is not None and represented_texture_sets != set(active_role_contract):
    raise RuntimeError(
      'Painter material Texture Sets differ from the role contract: '
      f'materials={sorted(represented_texture_sets)}, '
      f'contract={sorted(active_role_contract)}'
    )
  return verified


class PainterApplyNoMaterialsError(RuntimeError):
  pass


def apply_painter_export_transaction(
  result,
  low_objects,
  texture_dir,
  *,
  meshy_mode=False,
  canonical_texture_sets=None,
  required_roles_by_texture_set=None,
  before_commit=None,
):
  expected_texture_sets = set(canonical_texture_sets or ()) or {
    stripped_material_name(slot.material.name)
    for obj in low_objects
    for slot in obj.material_slots
    if slot.material
  }
  required_roles_by_texture_set = (
    normalize_meshy_required_roles(
      required_roles_by_texture_set,
      expected_texture_sets,
    )
    if meshy_mode else None
  )
  required_roles = (
    {
      role
      for roles in required_roles_by_texture_set.values()
      for role in roles
    }
    if meshy_mode else set(MESHY_PAINTER_CANONICAL_ROLES)
  )
  transaction_role_contract = (
    {
      texture_set: roles
      for texture_set, roles in required_roles_by_texture_set.items()
      if roles
    }
    if meshy_mode else None
  )
  transaction_texture_sets = (
    set(transaction_role_contract) if meshy_mode else expected_texture_sets
  )
  transaction_result = (
    filter_meshy_painter_export_result(
      result,
      expected_texture_sets,
      required_roles_by_texture_set,
    )
    if meshy_mode else result
  )
  material_transaction = (
    MeshyMaterialApplyTransaction(
      low_objects,
      texture_dir,
      expected_texture_sets,
      required_roles_by_texture_set,
    )
    if meshy_mode else None
  )
  transaction = begin_painter_canonical_apply_transaction(
    transaction_result,
    texture_dir=texture_dir if meshy_mode else None,
    allowed_roles=required_roles if meshy_mode else None,
    expected_texture_sets=transaction_texture_sets if meshy_mode else None,
    expected_roles_by_texture_set=(
      transaction_role_contract if meshy_mode else None
    ),
    require_noncanonical_sources=meshy_mode,
  )
  rollback_before_commit = None
  try:
    if meshy_mode:
      applied, managed_roles = material_transaction.prepare()
      material_transaction.swap()
    else:
      applied = apply_painter_textures_to_low(low_objects, texture_dir)
      managed_roles = {}
    if applied == 0:
      raise PainterApplyNoMaterialsError('Painter textures did not match any low material')
    pending_receipt = {
      'applied': applied,
      'canonical_files': list(transaction.canonical_files),
      'managed_roles': managed_roles,
    }
    if before_commit is not None:
      rollback_before_commit = before_commit(pending_receipt)
    canonical_files = transaction.commit(remove_sources=True)
    if material_transaction is not None:
      material_transaction.commit()
    pending_receipt['canonical_files'] = canonical_files
    return pending_receipt
  except Exception as error:
    rollback_errors = []
    for label, rollback_action in (
      ('material', material_transaction.rollback if material_transaction else None),
      ('file', transaction.rollback),
      (
        'checkpoint',
        rollback_before_commit if callable(rollback_before_commit) else None,
      ),
    ):
      if rollback_action is None:
        continue
      try:
        rollback_action()
      except Exception as rollback_error:
        rollback_errors.append(f'{label}: {rollback_error}')
    if rollback_errors:
      raise RuntimeError(
        'Painter apply failed and rollback was incomplete: '
        + '; '.join(rollback_errors)
      ) from error
    raise


def canonicalize_painter_export_files(result, *, remove_sources=True):
  """Compatibility API for callers that need an immediate file-only commit."""
  if not painter_export_canonical_replacements(result):
    return []
  transaction = begin_painter_canonical_apply_transaction(result)
  return transaction.commit(remove_sources=remove_sources)


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


def solidify_plus_fill_rim_socket_id(modifier):
  if modifier.type != 'NODES' or not modifier.name.startswith(SOLIDIFY_PLUS_NAME_PREFIX):
    return None
  node_group = getattr(modifier, 'node_group', None)
  interface = getattr(node_group, 'interface', None)
  items = getattr(interface, 'items_tree', ()) if interface is not None else ()
  for item in items:
    if (
      getattr(item, 'item_type', None) == 'SOCKET'
      and getattr(item, 'in_out', None) == 'INPUT'
      and getattr(item, 'socket_type', None) == 'NodeSocketBool'
      and item.name == SOLIDIFY_PLUS_FILL_RIM_SOCKET
    ):
      return getattr(item, 'identifier', None)
  return None


def geometry_nodes_input_state(modifier, socket_id):
  inputs = getattr(getattr(modifier, 'properties', None), 'inputs', None)
  if inputs is not None:
    try:
      input_group = inputs[socket_id]
      if 'value' in input_group:
        return True, input_group['value']
      return False, None
    except (KeyError, TypeError):
      pass
  try:
    if socket_id in modifier.keys():
      return True, modifier.get(socket_id)
  except (AttributeError, TypeError):
    pass
  return False, None


def set_geometry_nodes_input_value(modifier, socket_id, value):
  inputs = getattr(getattr(modifier, 'properties', None), 'inputs', None)
  if inputs is not None:
    try:
      inputs[socket_id]['value'] = value
      return
    except (KeyError, TypeError):
      pass
  modifier[socket_id] = value


def delete_geometry_nodes_input_value(modifier, socket_id):
  inputs = getattr(getattr(modifier, 'properties', None), 'inputs', None)
  if inputs is not None:
    try:
      input_group = inputs[socket_id]
      if 'value' in input_group:
        del input_group['value']
      return
    except (KeyError, TypeError):
      pass
  try:
    if socket_id in modifier.keys():
      del modifier[socket_id]
  except (AttributeError, TypeError):
    pass


def set_solidify_plus_fill_rim(source_objects, enabled):
  restore = []
  for obj in source_objects:
    for modifier in getattr(obj, 'modifiers', ()):
      socket_id = solidify_plus_fill_rim_socket_id(modifier)
      if not socket_id:
        continue
      had_value, old_value = geometry_nodes_input_state(modifier, socket_id)
      restore.append((obj, modifier, socket_id, had_value, old_value))
      set_geometry_nodes_input_value(modifier, socket_id, bool(enabled))
      obj.update_tag(refresh={'DATA'})
  if restore:
    bpy.context.view_layer.update()
  return restore


def refresh_solidify_plus_modifier(obj, modifier):
  obj.update_tag(refresh={'DATA'})
  if modifier.show_viewport:
    modifier.show_viewport = False
    bpy.context.view_layer.update()
    modifier.show_viewport = True
  obj.update_tag(refresh={'DATA'})


def restore_solidify_plus_fill_rim(restore):
  for obj, modifier, socket_id, had_value, old_value in reversed(restore):
    if had_value:
      set_geometry_nodes_input_value(modifier, socket_id, old_value)
    else:
      delete_geometry_nodes_input_value(modifier, socket_id)
    refresh_solidify_plus_modifier(obj, modifier)
  if restore:
    bpy.context.view_layer.update()


def duplicate_for_export(
  source_objects,
  collection,
  strip_material_prefix=False,
  id_source='NONE',
  solidify_plus_fill_rim=None,
):
  depsgraph = bpy.context.evaluated_depsgraph_get()
  duplicates = []
  temporary_materials = []
  renamed_materials = []
  material_copies = {}
  rim_restore = []
  try:
    if solidify_plus_fill_rim is not None:
      rim_restore = set_solidify_plus_fill_rim(source_objects, solidify_plus_fill_rim)
      depsgraph = bpy.context.evaluated_depsgraph_get()
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
          if not material.name.startswith(MATERIAL_PREFIX):
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
  finally:
    restore_solidify_plus_fill_rim(rim_restore)
  return duplicates, temporary_materials, renamed_materials


def export_objects_to_fbx(
  source_objects,
  filepath,
  strip_material_prefix=False,
  id_source='NONE',
  solidify_plus_fill_rim=None,
):
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
      solidify_plus_fill_rim=solidify_plus_fill_rim,
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
      # Painter computes its own MikkTSpace tangent basis. Blender-exported
      # FBX bitangents can be non-unit after axis/scale conversion, which makes
      # Painter warn and normalize them on every import.
      use_tspace=False,
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
      and image_name.startswith(TEXTURE_PREFIX)
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


def _hash_modifier_summary(digest, obj, normalize_solidify_plus_fill_rim=False):
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
    fill_rim_socket = (
      solidify_plus_fill_rim_socket_id(modifier)
      if normalize_solidify_plus_fill_rim
      else None
    )
    inputs = getattr(getattr(modifier, 'properties', None), 'inputs', None)
    interface = getattr(getattr(modifier, 'node_group', None), 'interface', None)
    if inputs is not None and interface is not None:
      for item in interface.items_tree:
        if (
          getattr(item, 'item_type', None) != 'SOCKET'
          or getattr(item, 'in_out', None) != 'INPUT'
        ):
          continue
        had_value, value = geometry_nodes_input_state(modifier, item.identifier)
        if not had_value:
          continue
        if item.identifier == fill_rim_socket:
          value = False
        if hasattr(value, 'name'):
          value = value.name
        elif not isinstance(value, (str, int, float, bool, type(None))):
          try:
            value = list(value)
          except TypeError:
            value = str(value)
        _hash_update_value(digest, ('geometry_nodes_input', item.identifier, value))
    try:
      id_property_keys = sorted(modifier.keys())
    except (AttributeError, TypeError):
      id_property_keys = []
    for key in id_property_keys:
      value = False if key == fill_rim_socket else modifier[key]
      if not isinstance(value, (str, int, float, bool)):
        try:
          value = list(value)
        except TypeError:
          value = str(value)
      _hash_update_value(digest, ('id_property', key, value))


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
  normalize_solidify_plus_fill_rim=False,
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
  _hash_modifier_summary(
    digest,
    obj,
    normalize_solidify_plus_fill_rim=normalize_solidify_plus_fill_rim,
  )


def fast_content_hash(
  source_objects,
  strip_material_prefix=False,
  id_source='NONE',
  normalize_solidify_plus_fill_rim=False,
):
  digest = hashlib.sha256()
  rim_restore = []
  try:
    if normalize_solidify_plus_fill_rim:
      rim_restore = set_solidify_plus_fill_rim(source_objects, False)
    for obj in sorted(source_objects, key=lambda item: item.name_full):
      _hash_fast_mesh_signature(
        digest,
        obj,
        strip_material_prefix=strip_material_prefix,
        id_source=id_source,
        normalize_solidify_plus_fill_rim=normalize_solidify_plus_fill_rim,
      )
  finally:
    restore_solidify_plus_fill_rim(rim_restore)
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


_PAINTER_INHERITED_ENVIRONMENT_VARIABLES = (
  'BLENDER_SYSTEM_SCRIPTS',
  'BLENDER_USER_CONFIG',
  'BLENDER_USER_SCRIPTS',
  'OCIO',
  'PYTHONHOME',
  'PYTHONPATH',
  'QT_PLUGIN_PATH',
  'QT_QPA_PLATFORM_PLUGIN_PATH',
)


def painter_launch_environment():
  """Return an environment that is safe for Painter's embedded runtimes.

  Blender sets OCIO to its bundled color configuration and may also be started
  with Blender-, Python-, or Qt-specific overrides. Letting Painter inherit
  those values can make it terminate during startup before Python plugins are
  loaded.
  """
  environment = os.environ.copy()
  for variable in _PAINTER_INHERITED_ENVIRONMENT_VARIABLES:
    environment.pop(variable, None)
  return environment


def launch_painter(painter_path, project_path=None):
  command = [str(painter_path)]
  if project_path is not None:
    command.append(str(project_path))
  return subprocess.Popen(command, env=painter_launch_environment())


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
