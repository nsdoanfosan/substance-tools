import bpy, glob, re, subprocess, os, traceback
import colorsys
import hashlib
import json
from collections import defaultdict
from pathlib import Path

bl_info = {
  'name': 'Substance Import-Export Tools',
  'version': (2, 0, 2),
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
PAINTER_REQUEST = '.substance_tools_request.json'
PENDING_REQUEST = 'pending_request.json'


def clean_name(value):
  value = re.sub(r'[^0-9A-Za-z_]+', '_', str(value or '')).strip('_')
  return value or 'Asset'


def ensure_baking_collections(scene=None):
  if scene is None:
    scene = getattr(bpy.context, 'scene', None)
  if scene is None and bpy.data.scenes:
    scene = bpy.data.scenes[0]
  if scene is None:
    return None, None, None
  root = bpy.data.collections.get(BAKING_COLLECTION)
  if root is None:
    root = bpy.data.collections.new(BAKING_COLLECTION)
  if root.name not in {collection.name for collection in scene.collection.children}:
    scene.collection.children.link(root)

  children = {}
  for name in (LOW_COLLECTION, HIGH_COLLECTION):
    collection = bpy.data.collections.get(name)
    if collection is None:
      collection = bpy.data.collections.new(name)
    if collection.name not in {child.name for child in root.children}:
      root.children.link(collection)
    children[name] = collection
  return root, children[LOW_COLLECTION], children[HIGH_COLLECTION]


@bpy.app.handlers.persistent
def ensure_baking_collections_on_load(_unused):
  for scene in bpy.data.scenes:
    ensure_baking_collections(scene)


def ensure_baking_collections_deferred():
  for scene in bpy.data.scenes:
    ensure_baking_collections(scene)
  return None


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
  }


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


def add_high_id_colors(mesh, object_name):
  face_set_attribute = next(
    (
      mesh.attributes.get(name)
      for name in ('.sculpt_face_set', 'sculpt_face_set', 'face_set')
      if mesh.attributes.get(name) is not None
      and mesh.attributes.get(name).domain == 'FACE'
    ),
    None,
  )
  color_attribute = mesh.color_attributes.get('Color')
  if color_attribute is not None:
    mesh.color_attributes.remove(color_attribute)
  color_attribute = mesh.color_attributes.new(
    name='Color',
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


def file_hash(path):
  digest = hashlib.sha256()
  with path.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
      digest.update(chunk)
  return digest.hexdigest()


def match_base(name, suffix):
  return re.sub(rf'(?i)(?:[_. -]?{suffix})$', '', name)


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
          f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance Painter\\Adobe Substance 3D Painter.exe'
          f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance Painter\\Adobe Substance 3D Painter.exe'

          # Steam with 3D
          f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance 3D Painter\\Adobe Substance 3D Painter.exe'
          f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance 3D Painter\\Adobe Substance 3D Painter.exe'
      ])
      # Windows with year
      for year in range(2020, 2026):
        paths.extend([
            # CC
            f'{letter}:\\Program Files\\Adobe\\Adobe Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe',
            f'{letter}:\\Program Files (x86)\\Adobe\\Adobe Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe',

            # Steam without 3D
            f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance Painter {year}\\Adobe Substance 3D Painter.exe'
            f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance Painter {year}\\Adobe Substance 3D Painter.exe'

            # Steam with 3D
            f'{letter}:\\Program Files\\Steam\\steamapps\\common\\Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe'
            f'{letter}:\\Program Files (x86)\\Steam\\steamapps\\common\\Substance 3D Painter {year}\\Adobe Substance 3D Painter.exe'
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

def material_needs_setup(material):
  if material.node_tree is None:
    return False
  if len(material.node_tree.nodes) == 2:
    return True
  return False

# Mock data for testing through blender text editor without installing
mocks = {
  'painter_path': detect_substance_painter_path(),
  'textures_path': ''
}

def get_paths(context):
  textures_path = get_preferences(context)["textures_path"]

  if textures_path == '':
    textures_path = Path(bpy.path.abspath('//'))
  else:
    textures_path = Path(textures_path)
  
  collection_name_clean = re.sub(r'[^a-zA-Z0-9_]', '_', bpy.context.view_layer.active_layer_collection.name)

  textures_path_for_collection = textures_path.joinpath('textures_' + collection_name_clean + '/')

  fbx_path = textures_path_for_collection.joinpath(collection_name_clean + '.fbx')
  spp_path = textures_path_for_collection.joinpath(collection_name_clean + '.spp')

  return {
    'fbx': fbx_path,
    'spp': spp_path,
    'directory': textures_path_for_collection,
    'collection_name_clean': collection_name_clean
  }

def get_preferences(context):
  if __name__ == '__main__':
    return mocks
  else:
    prefs = context.preferences.addons[__name__].preferences
    return {
      'painter_path': prefs.painter_path,
      'textures_path': prefs.textures_path
    }

def object_has_material(obj):
  return len(obj.data.materials) > 0 and obj.data.materials[0] is not None

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

class ExportToSubstancePainterOperator(bpy.types.Operator):
  """Export Collection to Substance Painter. Press Ctrl+Shift+R in Painter to reload after re-export"""
  bl_idname, bl_label = 'st.open_in_substance_painter', 'Export Collection to Substance Painter'

  run_painter: bpy.props.BoolProperty(name='Run Substance Painter', default=True)

  def execute(self, context):
    preferences = get_preferences(context)
    painter_path = preferences["painter_path"]

    paths = get_paths(context)
    directory = paths['directory']
    fbx = paths['fbx']
    spp = paths['spp']

    if bpy.data.filepath == '':
      self.report({'ERROR'}, 'File is not saved. Please save your blend file')
      return {'FINISHED'}

    for o in bpy.context.view_layer.active_layer_collection.collection.objects:
      # Check if the object is mesh:
      if o.type != 'MESH':
        self.report({'ERROR'}, f'Object {o.name} is not a mesh')
        return {'FINISHED'}

      # Check if the object has a material and create if necessary:
      if not object_has_material(o):
        create_material_for_object(o)
    
    if not directory.exists():
      directory.mkdir(parents=True, exist_ok=True)

    # Export FBX
    bpy.ops.wm.save_mainfile()
    bpy.ops.export_scene.fbx(
      mesh_smooth_type='EDGE',
      use_mesh_modifiers=True,
      add_leaf_bones=False,
      apply_scale_options='FBX_SCALE_ALL',
      bake_anim_use_nla_strips=False,
      bake_space_transform=True,
      use_active_collection=True,
      filepath=str(fbx)
    )

    # If we only need to export the fbx, we're done
    if not self.run_painter:
      return {'FINISHED'}

    if painter_path == '':
      self.report({'ERROR'}, 'Please specify Substance Painter path in addon preferences')
      return {'FINISHED'}

    # Check if preferences.painter_path exists
    if not Path(painter_path).exists():
      self.report({'ERROR'}, 'Substance Painter path is not valid. Please set the corrent path to Substance Painter in addon preferences')
      return {'FINISHED'}

    # Check if a mac .app and add the executable part automatically first
    if os.name == 'posix' and painter_path.endswith('.app'):
      painter_path = painter_path + '/Contents/MacOS/Adobe Substance 3D Painter'
    
    # Display an error message if the path is a directory
    if os.path.isdir(painter_path):
      self.report({'ERROR'}, 'Substance Painter path is set to a directory. Please set it to the executable file')
      return {'FINISHED'}

    try:
      if os.name == 'nt':
        subprocess.Popen([painter_path, '--mesh', fbx, '--export-path', directory, spp])
      else:
        subprocess.Popen(f'"{painter_path}" --mesh "{fbx}" --export-path "{directory}" "{spp}"', shell=True)

    except Exception as e:
      self.report({'ERROR'}, f'Error opening Substance Painter: {e}')
      return {'FINISHED'}

    return {'FINISHED'}


class ExportBakingToSubstancePainterOperator(bpy.types.Operator):
  """Export Baking/low and Baking/high, then create or update the Painter project"""
  bl_idname = 'st.export_baking_to_substance_painter'
  bl_label = 'Bake in Substance Painter'
  bl_options = {'REGISTER'}

  run_painter: bpy.props.BoolProperty(name='Run Substance Painter', default=True)

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file before exporting')
      return {'CANCELLED'}

    _, low_collection, high_collection = ensure_baking_collections(context.scene)
    low_objects = collection_meshes(low_collection)
    high_objects = collection_meshes(high_collection)
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}

    stripped_names = [
      stripped_material_name(slot.material.name)
      for obj in low_objects
      for slot in obj.material_slots
      if slot.material
    ]
    duplicates = sorted({name for name in stripped_names if stripped_names.count(name) > 1})
    original_names = {
      slot.material.name
      for obj in low_objects
      for slot in obj.material_slots
      if slot.material
    }
    real_collisions = [
      name for name in duplicates
      if len({original for original in original_names if stripped_material_name(original) == name}) > 1
    ]
    if real_collisions:
      self.report(
        {'ERROR'},
        'Material names collide after removing M_: ' + ', '.join(real_collisions),
      )
      return {'CANCELLED'}

    props = context.scene.substance_tools_baking
    paths = baking_paths()
    for directory in (paths['low_dir'], paths['high_dir'], paths['texture_dir']):
      directory.mkdir(parents=True, exist_ok=True)

    try:
      export_objects_to_fbx(
        low_objects,
        paths['low_fbx'],
        strip_material_prefix=True,
        id_source=props.id_source if not high_objects else 'NONE',
      )
      high_hash = ''
      if high_objects:
        export_objects_to_fbx(
          high_objects,
          paths['high_fbx'],
          id_source=props.id_source,
        )
        high_hash = file_hash(paths['high_fbx'])
      elif paths['high_fbx'].exists():
        paths['high_fbx'].unlink()
    except Exception as error:
      self.report({'ERROR'}, f'FBX export failed: {error}')
      traceback.print_exc()
      return {'CANCELLED'}

    settings = {
      'resolution': int(props.resolution),
      'match': props.match,
      'cage': 'AUTOMATIC',
      'id_source': props.id_source,
      'mesh_maps': [
        'Normal', 'WorldSpaceNormal', 'ID', 'AO',
        'Curvature', 'Position', 'Thickness',
      ],
    }
    settings_hash = hashlib.sha256(
      json.dumps(settings, sort_keys=True).encode('utf-8')
    ).hexdigest()
    spp_existed = paths['spp'].exists()
    request = {
      'version': 1,
      'blend_file': str(Path(bpy.data.filepath).resolve()),
      'low_fbx': str(paths['low_fbx'].resolve()),
      'high_fbx': str(paths['high_fbx'].resolve()) if high_objects else '',
      'spp': str(paths['spp'].resolve()),
      'texture_dir': str(paths['texture_dir'].resolve()),
      'low_hash': file_hash(paths['low_fbx']),
      'high_hash': high_hash,
      'settings_hash': settings_hash,
      'pipeline_hash': hashlib.sha256(
        f"{file_hash(paths['low_fbx'])}:{high_hash}:{settings_hash}".encode('utf-8')
      ).hexdigest(),
      'spp_existed': spp_existed,
      'settings': settings,
    }
    request_paths = (
      paths['texture_dir'] / PAINTER_REQUEST,
      paths['low_dir'] / PAINTER_REQUEST,
    )
    for request_path in request_paths:
      write_json(request_path, request)

    if props.match == 'BY_MESH_NAME' and high_objects:
      unmatched_low, unmatched_high = unmatched_mesh_names(low_objects, high_objects)
      if unmatched_low or unmatched_high:
        message = []
        if unmatched_low:
          message.append('Low without High: ' + ', '.join(unmatched_low))
        if unmatched_high:
          message.append('High without Low: ' + ', '.join(unmatched_high))
        self.report({'WARNING'}, ' | '.join(message))

    if not self.run_painter:
      self.report({'INFO'}, 'Low/High FBX and Painter request exported')
      return {'FINISHED'}

    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable in add-on preferences')
      return {'CANCELLED'}

    try:
      if spp_existed:
        command = [painter_path, str(paths['spp'])]
      else:
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
    except Exception as error:
      self.report({'ERROR'}, f'Error opening Substance Painter: {error}')
      return {'CANCELLED'}

    self.report({'INFO'}, 'Exported Baking collections and sent the request to Painter')
    return {'FINISHED'}

class LoadSubstancePainterTexturesOperator(bpy.types.Operator):
  """Load Substance Painter Textures"""
  bl_idname, bl_label, bl_options = 'st.load_substance_painter_textures', 'Load Substance Painter Textures', {'REGISTER', 'UNDO'}

  def execute(self, context):
    preferences = get_preferences(context)

    paths = get_paths(context)
    directory = paths['directory']

    # Check that node wrangler is enabled
    if 'node_wrangler' not in bpy.context.preferences.addons:
      self.report({'ERROR'}, 'Node Wrangler needs to be enabled! Please enable it in Edit -> Preferences -> Add-ons')
      return {'FINISHED'}

    # All of the materials in the blend file
    material_names = [material.name for material in bpy.data.materials]

    # Reload all of the unique images in materials of the current collection
    unique_images = set()
    for obj in bpy.context.view_layer.active_layer_collection.collection.objects:
      if obj.type == 'MESH' and len(obj.data.materials) > 0:
        for material in obj.data.materials:
          if material is not None and material.use_nodes:
            for node in material.node_tree.nodes:
              if node.bl_idname == 'ShaderNodeTexImage' and node.image:
                unique_images.add(node.image)
    for image in unique_images:
      image.reload()

    # Return if the file is not save
    if bpy.data.filepath == '':
      self.report({'ERROR'}, 'File is not saved')
      return {'FINISHED'}

    # Return if the texture folder doesn't exist
    if not directory.exists():
      self.report({'ERROR'}, 'There is no texture folder')
      return {'FINISHED'}

    # Return if there are no materials in the scene
    if len(bpy.data.materials) == 0:
      self.report({'ERROR'}, 'There are no materials in the scene')
      return {'FINISHED'}

    # Iterate through all of the files and group them by texture set name (material)
    texture_sets = defaultdict(list)
    material_names = sorted([material.name for material in bpy.data.materials if material_needs_setup(material)], key=len, reverse=True)
    for texture_file in directory.iterdir():
      # If texture_file is not a common texture file extension, skip it
      if texture_file.suffix not in ['.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.bmp', '.exr']:
        continue
      for material_name in material_names:
        if material_name in texture_file.name:
          texture_sets[material_name].append(texture_file.name)
          break
    # Create an empty mesh object with an empty material slot and set it as active
    # This is needed to be able to use the shader editor to assign textures with node wrangler
    previous_active_object = context.view_layer.objects.active
    temp_mesh = bpy.data.meshes.new(name="TempMesh")
    temp_obj = bpy.data.objects.new(name="TempObject", object_data=temp_mesh)
    temp_obj.data.materials.append(None)
    context.scene.collection.objects.link(temp_obj)
    context.view_layer.objects.active = temp_obj

    # Set area type to node editor
    previous_context = context.area.type
    context.area.type = 'NODE_EDITOR'
    context.area.ui_type = 'ShaderNodeTree'

    # Try catch to make sure that the context is ALWAYS returned to the previous one
    # Otherwise the UI may break
    try:
      # For all of the texture sets that have a material with matching name add nodes via node wrangler
      for texture_set_name, texture_file_names in texture_sets.items():
        if texture_set_name in material_names:
          # Set node editor to current material
          material = bpy.data.materials[texture_set_name]
          context.object.data.materials[0] = material
          context.space_data.node_tree = material.node_tree
          # Select the Principled BSDF node
          for node in context.space_data.node_tree.nodes:
            if node.bl_idname == 'ShaderNodeBsdfPrincipled':
              context.space_data.node_tree.nodes.active = node
              break
          # Add textures to node tree using node wrangler
          directory = str(directory) + os.sep
          files = [{'name':n} for n in texture_file_names]
          bpy.ops.node.nw_add_textures_for_principled(directory=directory, files=files)
    except Exception as e:
      tb = traceback.format_exc()
      self.report({'ERROR'}, f'Error occurred while adding textures: {e}\n{tb}')
    finally:
      context.area.type = previous_context
      context.view_layer.objects.active = previous_active_object
      bpy.data.objects.remove(temp_obj)

    return {'FINISHED'}

# @UI

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
    baking_box.prop(baking, 'resolution')
    baking_box.prop(baking, 'match')
    baking_box.prop(baking, 'id_source')
    row = baking_box.row(align=True)
    row.operator(
      'st.export_baking_to_substance_painter',
      text='Export',
      icon='EXPORT',
    ).run_painter = False
    row.operator(
      'st.export_baking_to_substance_painter',
      text='Bake in Painter',
      icon='WINDOW',
    ).run_painter = True

    paths = get_paths(context)
    fbx = paths['fbx']
    collection_name_clean = paths['collection_name_clean']

    fbx_exists = Path(fbx).exists()

    box_column = layout.box().column(align=True)

    if collection_name_clean == 'Scene_Collection':
      box_column.label(text='Select a collection in the outliner')
    else:
      box_column.label(text=f'Collection: {collection_name_clean}')
      box_column.separator()
      column = box_column.column(align=True)
      column.operator('st.open_in_substance_painter', text=f'Export', icon='EXPORT').run_painter = False
      column.operator('st.open_in_substance_painter', text=f'Export and Open in Painter', icon='WINDOW').run_painter = True

      if fbx_exists:
        # Load textures button
        if 'node_wrangler' in bpy.context.preferences.addons:
          column.operator('st.load_substance_painter_textures', text='Load Painter Textures', icon='IMPORT')
        else:
          column.label(text='Node Wrangler addon needs to be enabled!')
          column.label(text='Please enable it in Edit -> Preferences -> Add-ons')

# @Preferences

class SubstanceToolsPreferences(bpy.types.AddonPreferences):
  bl_idname = __name__

  painter_path: bpy.props.StringProperty(name='Substance Painter Executable', default=detect_substance_painter_path(), subtype='FILE_PATH')
  textures_path: bpy.props.StringProperty(name='Export Path (Blank for blend file path)', default='', subtype='DIR_PATH')

  def draw(self, context):
    layout = self.layout
    layout.prop(self, 'painter_path')
    layout.prop(self, 'textures_path')

# @Register

classes = (
  SubstanceToolsBakingSettings,
  ExportToSubstancePainterOperator,
  ExportBakingToSubstancePainterOperator,
  LoadSubstancePainterTexturesOperator,

  SubstanceToolsPanel,

  SubstanceToolsPreferences
)

def register():
  for c in classes: bpy.utils.register_class(c)
  bpy.types.Scene.substance_tools_baking = bpy.props.PointerProperty(
    type=SubstanceToolsBakingSettings
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
  if hasattr(bpy.types.Scene, 'substance_tools_baking'):
    del bpy.types.Scene.substance_tools_baking
  for c in reversed(classes): bpy.utils.unregister_class(c)

if __name__ == '__main__': register()
