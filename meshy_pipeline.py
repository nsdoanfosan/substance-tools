"""Deterministic Blender-side preparation for the Meshy retopo workflow.

This module orchestrates the provider-owned Quad Remesher and UVgami APIs,
adopts the user-created result for Painter, and records explicit asynchronous
checkpoints. The user still creates the retopo through Quad Remesher's native
*Remesh It* action; this module contains neither add-on's implementation.

The module is kept separate from the Painter handoff so that its mutations are
small, undoable, and independently testable.  It does not save a blend file or
Blender preferences and it never replaces a source file.
"""

from array import array
from collections import defaultdict
import hashlib
import importlib
import json
import os
import re
import struct
import sys
from pathlib import Path

import bpy

from . import meshy_pipeline_contract as contract
from .pipeline_contract import integration_api
from .core import (
  COLLECTION_ROLE_PROPERTY,
  HIGH_COLLECTION,
  LOW_COLLECTION,
  TEXTURE_PREFIX,
  clean_name,
  ensure_baking_collections,
  stripped_material_name,
  verify_painter_material_roles,
)


LEGACY_STATE_PROPERTY = '_substance_tools_meshy_pipeline_state_v1'
STATE_PROPERTY = '_substance_tools_meshy_pipeline_state_v2'
SOURCE_ID_PROPERTY = '_substance_tools_meshy_source_id_v1'
LOW_ID_PROPERTY = '_substance_tools_meshy_low_id_v1'
LOW_UV_RECEIPT_PROPERTY = '_substance_tools_meshy_low_uv_receipt_v1'
MATERIAL_TEXTURE_SET_PROPERTY = '_substance_tools_texture_set_v1'
STATE_SCHEMA_VERSION = 2
UV_METHOD_VERSION = 'optcuts_hard_surface_packed_v1'
LEGACY_UV_METHOD_VERSION = 'uvgami_optcuts_hard_surface_v1'
UVGAMI_WORKFLOW_API_VERSION = 1

PIPELINE_STAGES = (
  'ANALYZED',
  'SOURCE_ARCHIVED',
  'QR_READY',
  'LOW_CREATED',
  'UVGAMI_RUNNING',
  'UV_READY',
  'PAINTER_PACKAGE_READY',
  'BAKE_BASELINE_ARCHIVED',
  'SOURCE_LAYER_READY',
  'EXPORT_STAGED',
  'CANONICAL_APPLIED',
  'VERIFIED',
)

_ROLE_ORDER = {'Color': 0, 'Extra': 1, 'Normal': 2, None: 9}
_ROLE_FROM_IMAGE_PROPERTY = {
  'base_color': 'Color',
  'basecolor': 'Color',
  'color': 'Color',
  'metallic_roughness': 'Extra',
  'metallicroughness': 'Extra',
  'extra': 'Extra',
  'normal': 'Normal',
}


class MeshyPipelineError(RuntimeError):
  """A deterministic pipeline precondition was not met."""


def pipeline_stage_index(stage):
  try:
    return PIPELINE_STAGES.index(str(stage))
  except ValueError as exc:
    raise MeshyPipelineError(f'Unknown Meshy pipeline stage: {stage!r}') from exc


def canonical_asset_base(value):
  """Use the same canonical token shape required by UE Unique's public API."""
  name = re.sub(r'_+', '_', clean_name(value)).strip('_')
  return name or 'Asset'


def object_base_name(value):
  """Return a stable bake-pair base from source/retopo object names."""
  name = re.sub(r'\.\d{3}$', '', str(value or '').strip())
  name = re.sub(r'^(?:Retopo_)+', '', name, flags=re.IGNORECASE)
  name = re.sub(
    r'_(?:high|low)(?:_\d{2})?$',
    '',
    name,
    flags=re.IGNORECASE,
  )
  return canonical_asset_base(name)


def validate_target_override(value):
  """Compatibility wrapper; the QR bridge owns target validation."""
  bridge = _resolve_quad_remesher_workflow_api()
  try:
    return bridge['validate_target_override'](value)
  except Exception as exc:
    raise MeshyPipelineError(str(exc)) from exc


def _resolve_quad_remesher_workflow_api():
  """Resolve the QR owner's versioned API before any pipeline mutation."""
  spec = integration_api('quad_remesher_workflow')
  module_name = spec.get('module')
  getter_name = spec.get('getter')
  service_id = spec.get('service_id')
  expected_version = spec.get('version')
  functions = tuple(spec.get('functions') or ())
  if (
    not module_name
    or not getter_name
    or service_id != 'quad-remesher.workflow'
    or not isinstance(expected_version, int)
    or not functions
  ):
    raise MeshyPipelineError(
      'The quad_remesher_workflow integration declaration is incomplete'
    )
  try:
    owner = importlib.import_module(module_name)
    getter = getattr(owner, getter_name)
    service = getter(expected_version)
  except Exception as exc:
    raise MeshyPipelineError(
      'Quad Remesher Workflow Bridge is unavailable or failed to import: '
      f'{exc}'
    ) from exc
  if (
    not isinstance(service, dict)
    or service.get('service_id') != service_id
    or service.get('version') != expected_version
  ):
    raise MeshyPipelineError(
      'Quad Remesher Workflow API identity or version mismatch'
    )
  missing = [name for name in functions if not callable(service.get(name))]
  if missing:
    raise MeshyPipelineError(
      'Quad Remesher Workflow API functions are missing: ' + ', '.join(missing)
    )
  return {
    **service,
    'expected_api_version': expected_version,
    'analyze': service['analyze_target'],
    'configure': service['configure_request'],
  }


def _decode_pipeline_state(raw, expected_schema):
  if not isinstance(raw, str):
    raise MeshyPipelineError('Meshy pipeline state is not JSON text')
  try:
    state = json.loads(raw)
  except (TypeError, json.JSONDecodeError) as exc:
    raise MeshyPipelineError('Meshy pipeline state JSON is invalid') from exc
  if not isinstance(state, dict):
    raise MeshyPipelineError('Meshy pipeline state must be a JSON object')
  if state.get('schema_version') != expected_schema:
    raise MeshyPipelineError('Meshy pipeline state schema is unsupported')
  pipeline_stage_index(state.get('stage'))
  return state


def load_pipeline_state(scene, allow_legacy=False):
  raw = scene.get(STATE_PROPERTY)
  if not raw:
    legacy_raw = scene.get(LEGACY_STATE_PROPERTY)
    if not legacy_raw:
      return {}
    if not allow_legacy:
      raise MeshyPipelineError(
        'This scene contains a read-only Meshy pipeline v1 state. '
        'It is kept separate so the current add-on cannot reinterpret or '
        'overwrite its archives'
      )
    state = _decode_pipeline_state(legacy_raw, 1)
    state['_legacy_read_only'] = True
    return state
  return _decode_pipeline_state(raw, STATE_SCHEMA_VERSION)


def store_pipeline_state(scene, state):
  payload = dict(state)
  payload['schema_version'] = STATE_SCHEMA_VERSION
  pipeline_stage_index(payload.get('stage'))
  scene[STATE_PROPERTY] = json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
  )
  return payload


def advance_pipeline_state(state, stage, receipt=None):
  """Advance a state without skipping or regressing a checkpoint."""
  current = pipeline_stage_index(state['stage'])
  requested = pipeline_stage_index(stage)
  if requested < current:
    raise MeshyPipelineError(
      f'Refusing to regress Meshy state from {state["stage"]} to {stage}'
    )
  if requested > current + 1:
    raise MeshyPipelineError(
      f'Refusing to skip Meshy state from {state["stage"]} to {stage}'
    )
  result = dict(state)
  result['stage'] = stage
  checkpoints = dict(result.get('checkpoints') or {})
  if receipt is not None:
    checkpoints[stage] = receipt
  result['checkpoints'] = checkpoints
  return result


def _single_selected_mesh(context):
  meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
  if len(meshes) != 1:
    raise MeshyPipelineError('Select exactly one mesh object')
  return meshes[0]


def context_depsgraph(scene=None):
  context = bpy.context
  if scene is not None and context.scene is not scene:
    for window in getattr(context.window_manager, 'windows', ()):  # pragma: no branch
      if window.scene is scene:
        with context.temp_override(window=window, scene=scene):
          return context.evaluated_depsgraph_get()
  return context.evaluated_depsgraph_get()


def _hash_foreach(digest, collection, property_name, width, typecode):
  values = array(typecode, [0]) * (len(collection) * width)
  if values:
    collection.foreach_get(property_name, values)
    digest.update(values.tobytes())


def _stable_socket_default(socket):
  if not hasattr(socket, 'default_value'):
    return None
  value = socket.default_value
  if isinstance(value, (bool, int, float, str)):
    return value
  try:
    return [float(component) for component in value]
  except (TypeError, ValueError):
    return str(value)


def _material_graph_record(material):
  if material is None or not material.use_nodes or material.node_tree is None:
    return None
  special_properties = (
    'operation', 'blend_type', 'data_type', 'mode', 'space', 'uv_map',
    'interpolation', 'projection', 'extension', 'projection_blend',
    'vector_type', 'invert', 'clamp', 'convert_from', 'convert_to',
  )
  nodes = []
  for node in sorted(material.node_tree.nodes, key=lambda item: item.name):
    properties = {
      name: getattr(node, name)
      for name in special_properties
      if hasattr(node, name) and isinstance(getattr(node, name), (bool, int, float, str))
    }
    image_record = None
    if node.type == 'TEX_IMAGE' and node.image is not None:
      image_record = {
        'name': node.image.name,
        'path': _resolved_image_path(node.image),
        'colorspace': node.image.colorspace_settings.name,
      }
    nodes.append({
      'name': node.name,
      'bl_idname': node.bl_idname,
      'mute': bool(node.mute),
      'properties': properties,
      'image': image_record,
      'inputs': [
        {
          'name': socket.name,
          'identifier': socket.identifier,
          'default': _stable_socket_default(socket),
        }
        for socket in node.inputs
      ],
    })
  links = sorted(
    (
      link.from_node.name,
      link.from_socket.identifier,
      link.to_node.name,
      link.to_socket.identifier,
    )
    for link in material.node_tree.links
  )
  return {'nodes': nodes, 'links': links}


def mesh_object_content_signature(obj, scene, source_images=None):
  """Return stable evaluated-geometry, UV, and source-material signatures."""
  if obj is None or obj.type != 'MESH':
    raise MeshyPipelineError('Content signatures require a mesh object')
  depsgraph = context_depsgraph(scene)
  evaluated = obj.evaluated_get(depsgraph)
  mesh = None
  try:
    mesh = evaluated.to_mesh(
      preserve_all_data_layers=True,
      depsgraph=depsgraph,
    )
    mesh.calc_loop_triangles()
    geometry = hashlib.sha256()
    geometry.update(b'meshy-evaluated-geometry-v1\0')
    geometry.update(struct.pack(
      '<6Q',
      len(mesh.vertices),
      len(mesh.edges),
      len(mesh.loops),
      len(mesh.polygons),
      len(mesh.loop_triangles),
      len(obj.material_slots),
    ))
    geometry.update(struct.pack(
      '<16d',
      *(float(value) for row in obj.matrix_world for value in row),
    ))
    _hash_foreach(geometry, mesh.vertices, 'co', 3, 'f')
    _hash_foreach(geometry, mesh.edges, 'vertices', 2, 'i')
    _hash_foreach(geometry, mesh.loops, 'vertex_index', 1, 'i')
    _hash_foreach(geometry, mesh.polygons, 'loop_start', 1, 'i')
    _hash_foreach(geometry, mesh.polygons, 'loop_total', 1, 'i')
    _hash_foreach(geometry, mesh.polygons, 'material_index', 1, 'i')
    _hash_foreach(geometry, mesh.polygons, 'use_smooth', 1, 'b')
    if len(mesh.edges) and hasattr(mesh.edges[0], 'use_edge_sharp'):
      _hash_foreach(geometry, mesh.edges, 'use_edge_sharp', 1, 'b')
    corner_normals = getattr(mesh, 'corner_normals', None)
    if corner_normals is not None:
      _hash_foreach(geometry, corner_normals, 'vector', 3, 'f')

    uv = hashlib.sha256()
    uv.update(b'meshy-evaluated-uv-v1\0')
    uv.update(struct.pack('<Q', len(mesh.uv_layers)))
    for index, layer in enumerate(mesh.uv_layers):
      uv.update(struct.pack('<Q', index))
      uv.update(layer.name.encode('utf-8', errors='surrogatepass'))
      uv.update(b'\0')
      uv.update(b'1' if layer.active_render else b'0')
      _hash_foreach(uv, layer.data, 'uv', 2, 'f')
  finally:
    if mesh is not None:
      evaluated.to_mesh_clear()

  if source_images is None:
    source_images = discover_source_images(obj)
  source_records = []
  for entry in source_images:
    record = {
      'texture_set': clean_name(entry.get('texture_set')),
      'role': entry.get('role'),
      'colorspace': entry.get('colorspace'),
      'packed': bool(entry.get('packed')),
      'file_path': entry.get('file_path'),
    }
    if entry.get('file_path'):
      record['file'] = contract.file_manifest(entry['file_path'])
    source_records.append(record)
  material_records = [
    {
      'texture_set': material_texture_set(slot.material),
      'graph': _material_graph_record(slot.material),
    }
    if slot.material is not None else None
    for slot in obj.material_slots
  ]
  material_graph_payload = json.dumps(
    {
      'contract': 'meshy-material-graph-v1',
      'materials': material_records,
    },
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
  ).encode('utf-8')
  source_file_payload = json.dumps(
    {
      'contract': 'meshy-source-files-v1',
      'source_images': source_records,
    },
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
  ).encode('utf-8')
  material_graph_hash = hashlib.sha256(material_graph_payload).hexdigest()
  source_files_hash = hashlib.sha256(source_file_payload).hexdigest()
  material_hash = hashlib.sha256(
    f'{material_graph_hash}:{source_files_hash}'.encode('ascii')
  ).hexdigest()
  composite = hashlib.sha256(
    f'{geometry.hexdigest()}:{uv.hexdigest()}:{material_hash}'.encode('ascii')
  ).hexdigest()
  return {
    'contract': 'meshy-content-v1',
    'geometry_sha256': geometry.hexdigest(),
    'uv_sha256': uv.hexdigest(),
    'material_graph_sha256': material_graph_hash,
    'source_files_sha256': source_files_hash,
    'source_material_sha256': material_hash,
    'composite_sha256': composite,
  }


def validate_content_signature(
  obj,
  scene,
  expected,
  *,
  include_material,
  label,
  include_source_files=True,
):
  observed = mesh_object_content_signature(obj, scene)
  required = ['geometry_sha256', 'uv_sha256']
  if include_material:
    required.append('material_graph_sha256')
    if include_source_files:
      required.append('source_files_sha256')
  changed = [key for key in required if observed.get(key) != (expected or {}).get(key)]
  if changed:
    raise MeshyPipelineError(
      f'{label} content changed after its checkpoint: {", ".join(changed)}'
    )
  return observed


def _socket_by_name(node, socket_name):
  return next((socket for socket in node.inputs if socket.name == socket_name), None)


def _upstream_images(socket):
  if socket is None:
    return set()
  result = set()
  stack = [link.from_node for link in socket.links]
  visited = set()
  while stack:
    node = stack.pop()
    pointer = node.as_pointer()
    if pointer in visited:
      continue
    visited.add(pointer)
    if node.type == 'TEX_IMAGE' and node.image is not None:
      result.add(node.image)
      continue
    for input_socket in node.inputs:
      stack.extend(link.from_node for link in input_socket.links)
  return result


def _role_from_image_property(image):
  raw = image.get('_ue_unique_export_original_name')
  if raw is None:
    return None
  key = re.sub(r'[^a-z0-9]+', '_', str(raw).casefold()).strip('_')
  return _ROLE_FROM_IMAGE_PROPERTY.get(key)


def _role_from_image_name(image):
  value = f'{image.name} {Path(image.filepath or "").stem}'.casefold()
  if re.search(r'(?:^|[_\W])normal(?:$|[_\W])', value):
    return 'Normal'
  if re.search(r'(?:^|[_\W])extra(?:$|[_\W])', value):
    return 'Extra'
  if re.search(r'(?:^|[_\W])(?:color|basecolor|base_color)(?:$|[_\W])', value):
    return 'Color'
  return None


def _image_file_path(image):
  if image.source != 'FILE' or not image.filepath:
    return None
  try:
    value = bpy.path.abspath(image.filepath, library=image.library)
  except TypeError:
    value = bpy.path.abspath(image.filepath)
  path = Path(value).resolve()
  return path if path.is_file() else None


def material_texture_set(material):
  if material is None:
    return None
  return clean_name(str(
    material.get(MATERIAL_TEXTURE_SET_PROPERTY)
    or stripped_material_name(
      re.sub(r'^__SubstanceToolsHigh_', '', material.name)
    )
  ))


def discover_source_images(obj):
  """Discover standard source roles independently for each Texture Set."""
  discovered_by_key = {}
  role_paths = defaultdict(set)
  for slot_index, slot in enumerate(obj.material_slots):
    material = slot.material
    if material is None or not material.use_nodes or material.node_tree is None:
      continue
    texture_set = material_texture_set(material)
    images = {}
    graph_roles = {}
    for node in material.node_tree.nodes:
      if node.type == 'TEX_IMAGE' and node.image is not None:
        images[node.image.as_pointer()] = node.image
    for node in material.node_tree.nodes:
      if node.type != 'BSDF_PRINCIPLED':
        continue
      for image in _upstream_images(_socket_by_name(node, 'Base Color')):
        graph_roles.setdefault(image.as_pointer(), set()).add('Color')
      roughness = _upstream_images(_socket_by_name(node, 'Roughness'))
      metallic = _upstream_images(_socket_by_name(node, 'Metallic'))
      for image in roughness | metallic:
        graph_roles.setdefault(image.as_pointer(), set()).add('Extra')
      for image in _upstream_images(_socket_by_name(node, 'Normal')):
        graph_roles.setdefault(image.as_pointer(), set()).add('Normal')
    for pointer, image in sorted(images.items(), key=lambda item: item[1].name.casefold()):
      explicit_role = _role_from_image_property(image)
      connected_roles = graph_roles.get(pointer, set())
      if explicit_role is not None:
        role = explicit_role
      elif len(connected_roles) == 1:
        role = next(iter(connected_roles))
      elif len(connected_roles) > 1:
        raise MeshyPipelineError(
          f'Image {image.name!r} is connected to conflicting roles in {texture_set}'
        )
      else:
        role = _role_from_image_name(image)
      file_path = _image_file_path(image)
      if role is not None and file_path is not None:
        role_paths[(texture_set, role)].add(
          os.path.normcase(os.fspath(file_path))
        )
      key = (texture_set.casefold(), pointer)
      record = discovered_by_key.setdefault(key, {
        'texture_set': texture_set,
        'material_slots': [],
        'material_name': material.name,
        'image_name': image.name,
        'role': role,
        'file_path': os.fspath(file_path) if file_path is not None else None,
        'packed': bool(image.packed_file),
        'colorspace': image.colorspace_settings.name,
      })
      record['material_slots'].append(slot_index)
  ambiguous = [
    f'{texture_set}/{role}'
    for (texture_set, role), paths in role_paths.items()
    if len(paths) > 1
  ]
  if ambiguous:
    raise MeshyPipelineError(
      'Multiple external images claim the same Texture Set role: '
      + ', '.join(sorted(ambiguous))
    )
  return sorted(
    discovered_by_key.values(),
    key=lambda item: (
      item['texture_set'].casefold(),
      _ROLE_ORDER.get(item['role'], 8),
      (item['file_path'] or item['image_name']).casefold(),
    ),
  )


def analyze_mesh_object(obj, scene, target_override=0):
  if obj is None or obj.type != 'MESH':
    raise MeshyPipelineError('The source must be a mesh object')
  bridge = _resolve_quad_remesher_workflow_api()
  try:
    qr_analysis = bridge['analyze'](
      obj,
      scene=scene,
      target_override=target_override,
    )
  except Exception as exc:
    raise MeshyPipelineError(
      f'Quad Remesher target analysis failed: {exc}'
    ) from exc
  if (
    not isinstance(qr_analysis, dict)
    or qr_analysis.get('api_version') != bridge['expected_api_version']
    or qr_analysis.get('status') != 'SUCCESS'
  ):
    raise MeshyPipelineError('Quad Remesher target analysis receipt is invalid')
  target_override = int(qr_analysis['target_override'])
  triangles = int(qr_analysis['evaluated_triangles'])
  source_images = discover_source_images(obj)
  unsupported_payloads = [
    entry
    for entry in source_images
    if entry.get('role') in {'Color', 'Extra', 'Normal'}
    and (entry.get('packed') or not entry.get('file_path'))
  ]
  if unsupported_payloads:
    details = ', '.join(
      f'{entry["texture_set"]}/{entry["role"]}:{entry["image_name"]}'
      for entry in unsupported_payloads
    )
    raise MeshyPipelineError(
      'Meshy source maps must be unpacked external files before archiving: '
      + details
    )
  texture_sets_by_clean = defaultdict(set)
  for entry in source_images:
    texture_sets_by_clean[clean_name(entry['texture_set']).casefold()].add(
      entry['texture_set']
    )
  collisions = [
    sorted(values)
    for values in texture_sets_by_clean.values()
    if len(values) > 1
  ]
  if collisions:
    raise MeshyPipelineError(
      f'Texture Set names collide after filename normalization: {collisions}'
    )
  content_signature = mesh_object_content_signature(
    obj,
    scene,
    source_images=source_images,
  )
  return {
    'object_name': obj.name,
    'mesh_name': obj.data.name,
    'base_name': object_base_name(obj.name),
    'source_polygon_count': len(obj.data.polygons),
    'evaluated_triangles': triangles,
    'bbox_axes_m': qr_analysis['bbox_axes_m'],
    'bbox_center_m': qr_analysis['bbox_center_m'],
    'bbox_diagonal_m': qr_analysis['bbox_diagonal_m'],
    'formula_version': qr_analysis['formula_version'],
    'target_mode': qr_analysis['target_mode'],
    'target_quads': qr_analysis['target_quads'],
    'target_override': target_override,
    'quad_remesher_workflow': {
      'api_version': qr_analysis['api_version'],
      'operation': qr_analysis['operation'],
      'status': qr_analysis['status'],
    },
    'materials': [slot.material.name if slot.material else None for slot in obj.material_slots],
    'uv_layers': [layer.name for layer in obj.data.uv_layers],
    'source_images': source_images,
    'content_signature': content_signature,
  }


def _new_analysis_state(analysis):
  return {
    'schema_version': STATE_SCHEMA_VERSION,
    'stage': 'ANALYZED',
    'asset_base': analysis['base_name'],
    'analysis': analysis,
    'source': {
      'object_name': analysis['object_name'],
      'mesh_name': analysis['mesh_name'],
    },
    'checkpoints': {'ANALYZED': analysis},
  }


def _stage_archive_root(asset_base):
  if not bpy.data.filepath:
    raise MeshyPipelineError('Save the blend file before creating the source archive')
  blend_path = Path(bpy.data.filepath).resolve()
  return (
    blend_path.parent
    / '_painter_archive'
    / clean_name(asset_base)
    / '00_source_original_once'
  )


def create_source_archive(asset_base, analysis):
  """Publish the original blend and standard source maps as one exact set."""
  blend_path = Path(bpy.data.filepath).resolve()
  if not blend_path.is_file():
    raise MeshyPipelineError(f'Saved blend file is missing: {blend_path}')
  root = _stage_archive_root(asset_base)
  root.parent.mkdir(parents=True, exist_ok=True)

  source_records = [{
    'logical_path': f'scene/{blend_path.name}',
    'role': 'Blend',
    'source_path': blend_path,
  }]
  for image in analysis.get('source_images') or ():
    role = image.get('role')
    if role not in {'Color', 'Extra', 'Normal'} or not image.get('file_path'):
      continue
    image_path = Path(image['file_path']).resolve()
    texture_set = clean_name(image.get('texture_set') or asset_base)
    source_records.append({
      'logical_path': f'texture/{texture_set}/{role}/{image_path.name}',
      'role': role,
      'source_path': image_path,
    })
  logical_keys = [record['logical_path'].casefold() for record in source_records]
  if len(logical_keys) != len(set(logical_keys)):
    raise MeshyPipelineError(
      'Two source files would use the same case-insensitive archive path'
    )
  source_mapping = {
    record['logical_path']: record['source_path'] for record in source_records
  }
  manifest = contract.publish_immutable_snapshot_set(source_mapping, root)
  manifest_by_path = {entry['path']: entry for entry in manifest['files']}
  entries = []
  for record in source_records:
    entry = manifest_by_path[record['logical_path']]
    entries.append({
      'logical_path': record['logical_path'],
      'role': record['role'],
      'source_path': os.fspath(record['source_path']),
      'source': entry['source'],
      'backup': entry['backup'],
    })
  return {
    'root': os.fspath(root),
    'manifest_path': os.fspath(root / contract.MANIFEST_FILENAME),
    'manifest_sha256': contract.sha256_file(root / contract.MANIFEST_FILENAME),
    'entries': entries,
  }


def verify_source_archive_receipt(archive):
  """Verify archived copies without reinterpreting the current blend as original."""
  root = archive.get('root')
  if not root or not archive.get('entries'):
    raise MeshyPipelineError('Source archive state has no entries')
  manifest = contract.verify_immutable_snapshot_set_archive(root)
  manifest_path = Path(root) / contract.MANIFEST_FILENAME
  if contract.sha256_file(manifest_path) != archive.get('manifest_sha256'):
    raise MeshyPipelineError('Archived source manifest differs from scene state')
  expected = sorted(
    (
      {
        'path': entry['logical_path'],
        'source': entry['source'],
        'backup': entry['backup'],
      }
      for entry in archive['entries']
    ),
    key=lambda entry: entry['path'].casefold(),
  )
  actual = sorted(manifest['files'], key=lambda entry: entry['path'].casefold())
  if expected != actual:
    raise MeshyPipelineError('Archived source entries differ from scene state')
  return manifest


def configure_quad_remesher(scene, target_quads):
  """Compatibility wrapper delegating all vendor settings to the QR owner."""
  bridge = _resolve_quad_remesher_workflow_api()
  try:
    receipt = bridge['configure'](target_quads, scene=scene)
  except Exception as exc:
    raise MeshyPipelineError(
      f'Quad Remesher request configuration failed: {exc}'
    ) from exc
  if (
    not isinstance(receipt, dict)
    or receipt.get('api_version') != bridge['expected_api_version']
    or receipt.get('status') != 'READY_FOR_NATIVE_REMESH'
    or receipt.get('operator_invoked') is not False
  ):
    raise MeshyPipelineError('Quad Remesher configuration receipt is invalid')
  return receipt


def configure_meshy_painter_defaults(scene):
  settings = getattr(scene, 'substance_tools_baking', None)
  if settings is None:
    raise MeshyPipelineError('Substance Tools baking settings are unavailable')
  requested = {
    'antialiasing': 'X2',
    'match': 'BY_MESH_NAME',
    'id_source': 'MATERIAL_COLOR',
  }
  for name, value in requested.items():
    setattr(settings, name, value)
    if getattr(settings, name) != value:
      raise MeshyPipelineError(f'Could not set Painter {name} to {value}')
  return dict(requested)


def _source_identity(blend_sha256, obj):
  value = f'{blend_sha256}:{obj.name}:{obj.data.name}'.encode('utf-8')
  return hashlib.sha256(value).hexdigest()[:24]


def _find_source_from_state(state):
  source_id = state.get('source', {}).get('stable_id')
  matches = [
    obj for obj in bpy.data.objects
    if obj.type == 'MESH' and obj.get(SOURCE_ID_PROPERTY) == source_id
  ]
  if len(matches) == 1:
    return matches[0]
  if len(matches) > 1:
    raise MeshyPipelineError('Multiple meshes carry the pipeline source identity')
  fallback = bpy.data.objects.get(state.get('source', {}).get('object_name', ''))
  if fallback is None or fallback.type != 'MESH':
    raise MeshyPipelineError('The archived source mesh cannot be found')
  return fallback


def _object_users_of_material(material):
  users = []
  for obj in bpy.data.objects:
    if obj.type != 'MESH':
      continue
    if any(slot.material is material for slot in obj.material_slots):
      users.append(obj)
  return users


def _polygon_used_materials(obj):
  materials = set()
  slots = obj.material_slots
  for polygon in obj.data.polygons:
    index = int(polygon.material_index)
    material = slots[index].material if 0 <= index < len(slots) else None
    if material is None:
      raise MeshyPipelineError(
        f'{obj.name} has polygons assigned to an empty material slot'
      )
    materials.add(material)
  return materials


def _preflight_adoption(source, result, base_name, state):
  high_name = f'{base_name}_high'
  low_name = f'{base_name}_low'
  for name, intended in ((high_name, source), (low_name, result)):
    existing = bpy.data.objects.get(name)
    if existing is not None and existing is not intended:
      raise MeshyPipelineError(f'Object name collision prevents adoption: {name}')
  source_materials = {
    slot.material for slot in source.material_slots if slot.material is not None
  }
  source_used_materials = _polygon_used_materials(source)
  result_used_materials = _polygon_used_materials(result)
  source_used_sets = {
    material_texture_set(material) for material in source_used_materials
  }
  result_used_sets = {
    material_texture_set(material) for material in result_used_materials
  }
  if result_used_sets != source_used_sets:
    raise MeshyPipelineError(
      'Retopology result polygon-used Texture Sets differ from the source '
      f'(source={sorted(source_used_sets)}, result={sorted(result_used_sets)})'
    )
  canonical_materials = defaultdict(list)
  for material in source_materials:
    canonical_materials[material_texture_set(material).casefold()].append(material)
  collisions = [
    sorted(material.name for material in materials)
    for materials in canonical_materials.values()
    if len(materials) > 1
  ]
  if collisions:
    raise MeshyPipelineError(
      'Source material names collide after Texture Set normalization: '
      f'{collisions}'
    )
  result_materials = []
  for slot in result.material_slots:
    material = slot.material
    if material is None or material in result_materials:
      continue
    if material not in source_materials:
      raise MeshyPipelineError(
        f'Retopology result material {material.name!r} is not present on the source'
      )
    external = [
      obj for obj in _object_users_of_material(material)
      if obj not in {source, result}
    ]
    if external:
      names = ', '.join(sorted(obj.name for obj in external))
      raise MeshyPipelineError(
        f'Material {material.name!r} is also used by {names}; isolate it before adoption'
      )
    high_material_name = f'__SubstanceToolsHigh_{material.name}'
    collision = bpy.data.materials.get(high_material_name)
    if collision is not None and collision is not material:
      raise MeshyPipelineError(
        f'Material name collision prevents isolation: {high_material_name}'
      )
    low_material_name = f'M_{material_texture_set(material)}'
    low_collision = bpy.data.materials.get(low_material_name)
    if low_collision is not None and low_collision not in source_materials:
      raise MeshyPipelineError(
        f'Canonical Low material name collision prevents isolation: '
        f'{low_material_name}'
      )
    result_materials.append(material)
  return high_name, low_name, result_materials


def _validate_qr_result_via_owner(scene, source, result, state, base_name):
  """Obtain the QR owner's read-only provenance/topology receipt."""
  bridge = _resolve_quad_remesher_workflow_api()
  request = dict(state.get('qr') or {})
  # Compatibility for QR_READY scenes written before the owner API was split.
  request.setdefault('api_version', bridge['expected_api_version'])
  try:
    receipt = bridge['validate_result'](
      source,
      result,
      request,
      scene=scene,
      asset_base=base_name,
    )
  except Exception as exc:
    raise MeshyPipelineError(
      f'Quad Remesher result validation failed: {exc}'
    ) from exc
  if (
    not isinstance(receipt, dict)
    or receipt.get('api_version') != bridge['expected_api_version']
    or receipt.get('status') != 'SUCCESS'
  ):
    raise MeshyPipelineError('Quad Remesher result validation receipt is invalid')
  return receipt


def _uvgami_api_module_candidates():
  """Discover UVgami's own API beside its registered native operator."""
  candidates = []
  operator_type = getattr(bpy.types, 'UVGAMI_OT_start', None)
  operator_module = str(getattr(operator_type, '__module__', '') or '')
  marker = '.src.ops.start'
  if marker in operator_module:
    candidates.append(f'{operator_module.split(marker, 1)[0]}.api')
  for addon in bpy.context.preferences.addons:
    module_name = str(getattr(addon, 'module', '') or '')
    if module_name.rsplit('.', 1)[-1].casefold() == 'uvgami':
      candidates.append(f'{module_name}.api')
  for module_name, module in tuple(sys.modules.items()):
    if (
      module_name.endswith('.api')
      and 'uvgami' in module_name.casefold()
      and getattr(module, 'UVGAMI_WORKFLOW_API_VERSION', None) is not None
    ):
      candidates.append(module_name)
  return tuple(dict.fromkeys(candidates))


def _resolve_uvgami_workflow_api():
  """Resolve UVgami's provider-owned API without a central service hub."""
  spec = integration_api('uvgami_unwrap')
  expected_version = spec.get('version')
  service_id = spec.get('service_id')
  functions = tuple(spec.get('functions') or ())
  if (
    expected_version != UVGAMI_WORKFLOW_API_VERSION
    or service_id != 'uvgami.unwrap'
    or not functions
  ):
    raise MeshyPipelineError('The UVgami integration declaration is incomplete')
  failures = []
  candidates = _uvgami_api_module_candidates()
  for module_name in candidates:
    try:
      module = sys.modules.get(module_name) or importlib.import_module(module_name)
      getter = getattr(module, 'get_workflow_api', None)
      if not callable(getter):
        raise TypeError('get_workflow_api is missing')
      resolved = getter(expected_version)
      if not isinstance(resolved, dict):
        raise TypeError('get_workflow_api did not return a dictionary')
      if resolved.get('service_id') != service_id:
        raise TypeError('service_id does not match')
      if resolved.get('version') != expected_version:
        raise TypeError('version does not match')
      missing = [name for name in functions if not callable(resolved.get(name))]
      if missing:
        raise TypeError('missing functions: ' + ', '.join(missing))
      return {**resolved, 'module_name': module_name}
    except Exception as exc:
      failures.append(f'{module_name}: {exc}')
  detail = '; '.join(failures) if failures else 'no enabled provider was found'
  raise MeshyPipelineError(
    'UVgami workflow API is unavailable. Enable UVgami, then retry: ' + detail
  )


def _call_uvgami(api, operation, *args, **kwargs):
  try:
    return api[operation](*args, **kwargs)
  except Exception as exc:
    raise MeshyPipelineError(f'UVgami {operation} failed: {exc}') from exc


def validate_low_uv(obj, tolerance=1.0e-6):
  """Compatibility wrapper delegating UV validity to UVgami's owner API."""
  api = _resolve_uvgami_workflow_api()
  return _call_uvgami(
    api,
    'inspect_uv_map',
    obj,
    tolerance=tolerance,
  )


def _activate_only(obj):
  if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
  for candidate in bpy.context.selected_objects:
    candidate.select_set(False)
  obj.select_set(True)
  bpy.context.view_layer.objects.active = obj


def launch_uvgami_low_uv(
  scene,
  low,
  resolution=2048,
  margin_pixels=8,
  *,
  manager=None,
  start_operator=None,
):
  """Start UVgami through its owner API and retain its durable job receipt."""
  if low is None or low.type != 'MESH':
    raise MeshyPipelineError('UVgami Low UV preparation requires a mesh')
  api = _resolve_uvgami_workflow_api()
  kwargs = {
    'scene': scene,
    'resolution': resolution,
    'margin_pixels': margin_pixels,
  }
  if manager is not None:
    kwargs['_manager'] = manager
  if start_operator is not None:
    kwargs['_start_operator'] = start_operator
  provider_receipt = _call_uvgami(api, 'begin_unwrap', [low], **kwargs)
  if (
    not isinstance(provider_receipt, dict)
    or provider_receipt.get('service_id') != api['service_id']
    or provider_receipt.get('api_version') != api['version']
    or provider_receipt.get('status') != 'RUNNING'
    or not isinstance(provider_receipt.get('job'), dict)
  ):
    raise MeshyPipelineError('UVgami returned an invalid begin receipt')
  job = dict(provider_receipt['job'])
  pinned_objects = job.get('objects') or ()
  if len(pinned_objects) != 1:
    raise MeshyPipelineError('UVgami did not pin exactly one Low object')
  pinned = pinned_objects[0]
  return {
    'method': UV_METHOD_VERSION,
    'status': 'RUNNING',
    'low_object': low.name,
    'low_object_pointer': int(low.as_pointer()),
    'polygon_count': int(pinned.get('polygon_count', -1)),
    'settings': dict(job.get('settings') or {}),
    'provider_service_id': api['service_id'],
    'provider_api_version': api['version'],
    'provider_job': job,
  }


def confirm_uvgami_low_uv(scene, low, pending, *, manager=None):
  """Poll UVgami's durable job and store its terminal owner receipt."""
  if not isinstance(pending, dict) or pending.get('method') not in {
    UV_METHOD_VERSION,
    LEGACY_UV_METHOD_VERSION,
  }:
    raise MeshyPipelineError('The recorded UVgami Low unwrap request is missing')
  if low is None or low.type != 'MESH' or low.name != pending.get('low_object'):
    raise MeshyPipelineError('The named Low object for the UVgami request is missing')
  if int(low.as_pointer()) != int(pending.get('low_object_pointer', -1)):
    raise MeshyPipelineError('The Low object was replaced during the UVgami request')
  api = _resolve_uvgami_workflow_api()
  job = pending.get('provider_job')
  if not isinstance(job, dict):
    job = {
      'service_id': api['service_id'],
      'api_version': api['version'],
      'job_id': 'legacy-scene-request',
      'profile': UV_METHOD_VERSION,
      'objects': [{
        'name': low.name,
        'pointer': int(pending.get('low_object_pointer', -1)),
        'polygon_count': int(pending.get('polygon_count', -1)),
      }],
      'settings': dict(pending.get('settings') or {}),
    }
  kwargs = {'_manager': manager} if manager is not None else {}
  provider_receipt = _call_uvgami(api, 'poll_unwrap', job, **kwargs)
  if not isinstance(provider_receipt, dict):
    raise MeshyPipelineError('UVgami returned an invalid poll receipt')
  if provider_receipt.get('status') == 'RUNNING':
    return None
  if (
    provider_receipt.get('service_id') != api['service_id']
    or provider_receipt.get('api_version') != api['version']
    or provider_receipt.get('status') != 'SUCCESS'
  ):
    raise MeshyPipelineError('UVgami returned a non-success terminal receipt')
  reports = provider_receipt.get('objects') or ()
  report = next(
    (entry for entry in reports if entry.get('object') == low.name),
    None,
  )
  if not isinstance(report, dict) or not report.get('valid'):
    raise MeshyPipelineError('UVgami terminal receipt has no valid Low UV report')
  settings = dict(provider_receipt.get('settings') or job.get('settings') or {})
  receipt = {
    **report,
    'created': True,
    'method': UV_METHOD_VERSION,
    'resolution': int(settings.get('resolution', 0)),
    'margin_pixels': int(settings.get('margin_pixels', 0)),
    'margin': float(settings.get('margin', 0.0)),
    'polygon_count': int(report.get('polygon_count', -1)),
    'manager_summary': list(provider_receipt.get('manager_summary') or ()),
    'provider_service_id': api['service_id'],
    'provider_api_version': api['version'],
    'provider_receipt': provider_receipt,
  }
  low[LOW_UV_RECEIPT_PROPERTY] = json.dumps(
    receipt,
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
  )
  return receipt


def _link_object_exclusively(obj, collections):
  keep = set(collections)
  for collection in collections:
    if collection not in obj.users_collection:
      collection.objects.link(obj)
  for collection in list(obj.users_collection):
    if collection not in keep:
      collection.objects.unlink(obj)


def _capture_collection_transaction_state():
  """Capture collection links/properties that adoption is allowed to touch."""
  collections = tuple(bpy.data.collections)
  scenes = tuple(bpy.data.scenes)
  return {
    collection: {
      'parents': tuple(
        parent for parent in collections
        if parent.children.get(collection.name) is collection
      ),
      'scenes': tuple(
        item for item in scenes
        if item.collection.children.get(collection.name) is collection
      ),
      'has_role': COLLECTION_ROLE_PROPERTY in collection,
      'role': collection.get(COLLECTION_ROLE_PROPERTY),
    }
    for collection in collections
  }


def _restore_collection_transaction_state(snapshot):
  """Restore existing collection topology and remove only transaction-created data."""
  existing = set(snapshot)
  current = tuple(bpy.data.collections)

  for collection, saved in snapshot.items():
    if collection not in bpy.data.collections.values():
      raise RuntimeError(
        f'Pre-existing collection was removed during adoption: {collection.name}'
      )
    expected_parents = set(saved['parents'])
    for parent in tuple(bpy.data.collections):
      linked = parent.children.get(collection.name) is collection
      if linked and parent not in expected_parents:
        parent.children.unlink(collection)
      elif not linked and parent in expected_parents:
        parent.children.link(collection)
    expected_scenes = set(saved['scenes'])
    for item in bpy.data.scenes:
      linked = item.collection.children.get(collection.name) is collection
      if linked and item not in expected_scenes:
        item.collection.children.unlink(collection)
      elif not linked and item in expected_scenes:
        item.collection.children.link(collection)
    if saved['has_role']:
      collection[COLLECTION_ROLE_PROPERTY] = saved['role']
    elif COLLECTION_ROLE_PROPERTY in collection:
      del collection[COLLECTION_ROLE_PROPERTY]

  created = [collection for collection in current if collection not in existing]
  for parent in tuple(bpy.data.collections):
    for child in tuple(parent.children):
      if child in created:
        parent.children.unlink(child)
  for item in bpy.data.scenes:
    for child in tuple(item.collection.children):
      if child in created:
        item.collection.children.unlink(child)
  for collection in created:
    if collection.objects or collection.children:
      raise RuntimeError(
        f'Created collection is not empty during rollback: {collection.name}'
      )
    bpy.data.collections.remove(collection)


def _resolve_low_export_api():
  """Resolve the declared UE Unique API and reject incompatible majors."""
  spec = integration_api('painter_low_export_sync')
  module_name = spec.get('module')
  getter_name = spec.get('getter')
  service_id = spec.get('service_id')
  function_name = spec.get('function')
  expected_version = spec.get('version')
  if (
    not module_name
    or not getter_name
    or not service_id
    or not function_name
    or not isinstance(expected_version, int)
  ):
    raise MeshyPipelineError(
      'The painter_low_export_sync integration declaration is incomplete'
    )
  try:
    api = importlib.import_module(module_name)
  except ModuleNotFoundError as exc:
    missing_names = {module_name, module_name.partition('.')[0]}
    if getattr(exc, 'name', None) not in missing_names:
      raise MeshyPipelineError(
        f'{module_name} is installed but failed to import: {exc}'
      ) from exc
    return ({
      'available': False,
      'synced': False,
      'reason': 'optional_addon_unavailable',
      'error': str(exc),
      'expected_api_version': expected_version,
    }, None)
  except Exception as exc:
    raise MeshyPipelineError(
      f'{module_name} is installed but failed to import: {exc}'
    ) from exc
  getter = getattr(api, getter_name, None)
  if not callable(getter):
    raise MeshyPipelineError(
      f'{module_name} does not provide required API getter {getter_name!r}'
    )
  try:
    service = getter(expected_version)
  except Exception as exc:
    raise MeshyPipelineError(
      f'{module_name} API version is incompatible with required version '
      f'{expected_version}: {exc}'
    ) from exc
  if (
    not isinstance(service, dict)
    or service.get('service_id') != service_id
    or service.get('version') != expected_version
  ):
    raise MeshyPipelineError(f'{module_name} returned an incompatible API identity')
  sync = service.get(function_name)
  if not callable(sync):
    raise MeshyPipelineError(
      f'{module_name} does not provide required function {function_name!r}'
    )
  observed_version = service['version']
  return ({
    'available': True,
    'service_id': service_id,
    'expected_api_version': expected_version,
    'observed_api_version': observed_version,
  }, sync)


def _sync_low_export_via_ue_unique(
  scene,
  low_object,
  asset_base,
  resolved_api=None,
):
  """Ask UE Unique to create/sync the safe Empty-based Low export unit."""
  api_status, sync = resolved_api or _resolve_low_export_api()
  if not api_status.get('available') or not callable(sync):
    return dict(api_status)
  try:
    receipt = sync(low_object, asset_base, scene=scene)
  except Exception as exc:
    raise MeshyPipelineError(
      f'UE Unique Painter Low export-unit preparation failed: {exc}'
    ) from exc
  if not isinstance(receipt, dict):
    raise MeshyPipelineError(
      'UE Unique Painter Low export-unit API returned a non-dictionary receipt'
    )
  expected_version = api_status.get('expected_api_version')
  if receipt.get('api_version') != expected_version:
    raise MeshyPipelineError(
      'UE Unique Painter Low export-unit receipt version '
      f'{receipt.get("api_version")!r} is incompatible with {expected_version!r}'
    )
  if receipt.get('service_id') != api_status.get('service_id'):
    raise MeshyPipelineError(
      'UE Unique Painter Low export-unit receipt service identity is incompatible'
    )
  merged = {'available': True, **receipt, **api_status}
  if merged.get('synced') is not True:
    raise MeshyPipelineError(
      'UE Unique Painter Low export-unit API did not confirm Export synchronization'
    )
  return merged


def _low_export_handoff_ready(receipt):
  if not isinstance(receipt, dict):
    return False
  if not receipt.get('available') or receipt.get('synced') is not True:
    return False
  if receipt.get('handoff_ready') is not True:
    return False
  combine = receipt.get('combine_assets') or {}
  if not combine.get('available') or combine.get('value') != 'child_meshes':
    return False
  immediate = combine.get('use_immediate_parent_name') or {}
  return not immediate.get('available') or immediate.get('value') is False


def _refresh_recorded_low_export_unit(scene, state, *, force=False):
  """Retry the optional export owner after it or Send2UE becomes available."""
  low_state = state.get('low') or {}
  low_name = low_state.get('low_object')
  low = bpy.data.objects.get(low_name or '')
  if low is None or low.type != 'MESH':
    raise MeshyPipelineError('The recorded Painter Low is missing')
  current = low_state.get('ue_unique_export_sync') or {}
  if not force and _low_export_handoff_ready(current):
    return current, False
  base_name = canonical_asset_base(state.get('asset_base'))
  refreshed = _sync_low_export_via_ue_unique(scene, low, base_name)
  changed = refreshed != current
  low_state['ue_unique_export_sync'] = refreshed
  state['low'] = low_state
  return refreshed, changed


def _require_low_export_handoff_ready(receipt):
  if _low_export_handoff_ready(receipt):
    return
  if not receipt.get('available'):
    reason = receipt.get('reason') or 'optional owner unavailable'
    raise MeshyPipelineError(
      f'Unreal Handoff export unit is not ready: {reason}'
    )
  if receipt.get('unit_status') == 'PRESERVED_SKELETAL_HIERARCHY':
    raise MeshyPipelineError(
      'The Low has skeletal or shape-key semantics; preserve its hierarchy and '
      'resolve its Unreal asset naming explicitly before Verify'
    )
  combine = receipt.get('combine_assets') or {}
  if not combine.get('available'):
    raise MeshyPipelineError(
      'Unreal Handoff export unit is linked, but Send2UE is not enabled; '
      'enable Send to Unreal and run Verify again'
    )
  raise MeshyPipelineError(
    'Unreal Handoff export unit is not the exact top-level Empty/Child Meshes unit'
  )


def _isolate_low_materials(source, result, materials):
  original_names = {material: material.name for material in materials}
  texture_sets = {
    material: material_texture_set(material)
    for material in materials
  }
  copies = {}
  try:
    for material in materials:
      copied = material.copy()
      copied.name = f'__SubstanceToolsTmpLow_{clean_name(material.name)}'
      if copied is material:
        raise MeshyPipelineError(f'Failed to copy material {material.name!r}')
      if (
        material.node_tree is not None
        and copied.node_tree is material.node_tree
      ):
        raise MeshyPipelineError(
          f'Material copy shares its node tree: {material.name!r}'
        )
      copies[material] = copied
    for material in materials:
      original_name = original_names[material]
      texture_set = texture_sets[material]
      low_material_name = f'M_{texture_set}'
      material.name = f'__SubstanceToolsHigh_{original_name}'
      copies[material].name = low_material_name
      material[MATERIAL_TEXTURE_SET_PROPERTY] = texture_set
      copies[material][MATERIAL_TEXTURE_SET_PROPERTY] = texture_set
      if copies[material].name != low_material_name:
        raise MeshyPipelineError(
          f'Could not reserve canonical Low material name {low_material_name!r}'
        )
    if result.data is source.data:
      result.data = result.data.copy()
    for slot in result.material_slots:
      if slot.material in copies:
        slot.material = copies[slot.material]
    shared_materials = {
      slot.material for slot in source.material_slots if slot.material is not None
    } & {
      slot.material for slot in result.material_slots if slot.material is not None
    }
    if shared_materials:
      raise MeshyPipelineError('Low still shares material data with High')
    return [
      {
        'original_name': original_names[material],
        'canonical_name': f'M_{texture_sets[material]}',
        'texture_set': texture_sets[material],
        'high_material': material.name,
        'low_material': copies[material].name,
      }
      for material in materials
    ]
  except Exception:
    for slot in result.material_slots:
      for original, copied in copies.items():
        if slot.material is copied:
          slot.material = original
    for material, original_name in original_names.items():
      material.name = original_name
    for copied in copies.values():
      if copied.users == 0:
        bpy.data.materials.remove(copied)
    raise


def _validated_topology_owner_receipt(receipt):
  """Validate only the fields Painter pairing consumes from a topology owner."""
  if not isinstance(receipt, dict) or receipt.get('status') != 'SUCCESS':
    raise MeshyPipelineError('The topology-owner receipt is not a SUCCESS receipt')
  required = ('actual_low_polygons', 'actual_low_triangles', 'topology', 'bbox')
  missing = [name for name in required if name not in receipt]
  if missing:
    raise MeshyPipelineError(
      'The topology-owner receipt is incomplete: ' + ', '.join(missing)
    )
  if int(receipt['actual_low_polygons']) <= 0 or int(
    receipt['actual_low_triangles']
  ) <= 0:
    raise MeshyPipelineError('The topology-owner receipt has an empty result')
  if not isinstance(receipt['topology'], dict) or not isinstance(
    receipt['bbox'], dict
  ):
    raise MeshyPipelineError('The topology-owner receipt payload is malformed')
  return receipt


def adopt_retopology_pair(
  scene,
  source,
  result,
  state,
  topology_owner_receipt,
  resolution=2048,
  margin_pixels=8,
):
  """Create the Painter High/Low pair from an owner-validated retopology."""
  base_name = canonical_asset_base(state['asset_base'])
  topology_validation = _validated_topology_owner_receipt(
    topology_owner_receipt
  )
  low_export_api = _resolve_low_export_api()
  high_name, low_name, materials = _preflight_adoption(
    source,
    result,
    base_name,
    state,
  )
  validate_content_signature(
    source,
    scene,
    state['source'].get('content_signature'),
    include_material=True,
    label='High source',
  )
  original_result_data = result.data
  original_names = {source: source.name, result: result.name}
  original_collections = {
    source: list(source.users_collection),
    result: list(result.users_collection),
  }
  original_selected = {
    obj: bool(obj.select_get()) for obj in bpy.context.view_layer.objects
  }
  original_active = bpy.context.view_layer.objects.active
  original_object_properties = {
    (source, SOURCE_ID_PROPERTY): (
      SOURCE_ID_PROPERTY in source,
      source.get(SOURCE_ID_PROPERTY),
    ),
    (result, LOW_ID_PROPERTY): (
      LOW_ID_PROPERTY in result,
      result.get(LOW_ID_PROPERTY),
    ),
    (result, LOW_UV_RECEIPT_PROPERTY): (
      LOW_UV_RECEIPT_PROPERTY in result,
      result.get(LOW_UV_RECEIPT_PROPERTY),
    ),
  }
  original_material_state = {
    material: {
      'name': material.name,
      'has_texture_set': MATERIAL_TEXTURE_SET_PROPERTY in material,
      'texture_set': material.get(MATERIAL_TEXTURE_SET_PROPERTY),
    }
    for material in materials
  }
  collection_transaction = _capture_collection_transaction_state()
  prepared_mesh = None
  isolated_copies = set()
  try:
    # Keep the selected QR topology intact while making its datablock private.
    # UVgami later transfers only UV coordinates back onto this mesh.
    prepared_mesh = result.data.copy()
    result.data = prepared_mesh
    material_receipt = _isolate_low_materials(source, result, materials)
    isolated_copies = {
      slot.material
      for slot in result.material_slots
      if slot.material is not None and slot.material not in materials
    }
    source.name = high_name
    result.name = low_name
    if source.name != high_name or result.name != low_name:
      raise MeshyPipelineError('Blender introduced a numeric suffix during pair naming')

    _, low_collection, high_collection, _ = ensure_baking_collections(scene)
    _link_object_exclusively(source, (high_collection,))
    # UE Unique Export owns Low -> Export synchronization.  This add-on only
    # classifies the pair in Baking/high and Baking/low.
    _link_object_exclusively(result, (low_collection,))
    result[LOW_ID_PROPERTY] = state['source']['stable_id']
    source[SOURCE_ID_PROPERTY] = state['source']['stable_id']
    _activate_only(result)
    receipt = {
      'high_object': source.name,
      'low_object': result.name,
      'actual_low_polygons': topology_validation['actual_low_polygons'],
      'actual_low_triangles': topology_validation['actual_low_triangles'],
      'topology': topology_validation['topology'],
      'bbox': topology_validation['bbox'],
      'topology_owner_receipt': topology_validation,
      'materials': material_receipt,
      'material_slots': [
        {
          'slot': index,
          'material': slot.material.name if slot.material is not None else None,
          'texture_set': (
            material_texture_set(slot.material)
            if slot.material is not None else None
          ),
        }
        for index, slot in enumerate(result.material_slots)
      ],
    }
    # Optional owner absence is recorded, while an installed-owner failure is
    # fatal. Keep this last so adoption rollback stays narrowly scoped.
    receipt['ue_unique_export_sync'] = _sync_low_export_via_ue_unique(
      scene,
      result,
      base_name,
      resolved_api=low_export_api,
    )
  except Exception as error:
    try:
      # Detach all prepared material copies before restoring their source names.
      result.data = original_result_data
      for copied in isolated_copies:
        copied.name = f'__SubstanceToolsRollback_{copied.as_pointer()}'
      for material, saved in original_material_state.items():
        material.name = saved['name']
        if saved['has_texture_set']:
          material[MATERIAL_TEXTURE_SET_PROPERTY] = saved['texture_set']
        elif MATERIAL_TEXTURE_SET_PROPERTY in material:
          del material[MATERIAL_TEXTURE_SET_PROPERTY]
      for (obj, key), (existed, value) in original_object_properties.items():
        if existed:
          obj[key] = value
        elif key in obj:
          del obj[key]
      result.name = f'__SubstanceToolsRollbackObject_{result.as_pointer()}'
      source.name = original_names[source]
      result.name = original_names[result]
      for obj, expected_collections in original_collections.items():
        expected = set(expected_collections)
        for collection in expected_collections:
          if collection not in obj.users_collection:
            collection.objects.link(obj)
        for collection in list(obj.users_collection):
          if collection not in expected:
            collection.objects.unlink(obj)
      for obj, was_selected in original_selected.items():
        obj.select_set(was_selected)
      bpy.context.view_layer.objects.active = original_active
      if prepared_mesh is not None and prepared_mesh.users == 0:
        bpy.data.meshes.remove(prepared_mesh)
      for copied in isolated_copies:
        if copied.users == 0:
          bpy.data.materials.remove(copied)
      _restore_collection_transaction_state(collection_transaction)
    except Exception as rollback_error:
      raise MeshyPipelineError(
        f'Adoption failed ({error}); rollback also failed ({rollback_error})'
      ) from error
    raise
  if original_result_data.users == 0:
    bpy.data.meshes.remove(original_result_data)
  return receipt


def adopt_qr_result(scene, source, result, state, resolution=2048, margin_pixels=8):
  """Compatibility route: validate with the QR owner, then pair for Painter."""
  base_name = canonical_asset_base(state['asset_base'])
  topology_receipt = _validate_qr_result_via_owner(
    scene,
    source,
    result,
    state,
    base_name,
  )
  return adopt_retopology_pair(
    scene,
    source,
    result,
    state,
    topology_receipt,
    resolution=resolution,
    margin_pixels=margin_pixels,
  )


def recover_committed_adoption(scene, state):
  """Recover a name commit without repeating geometry or material scans."""
  base_name = canonical_asset_base(state['asset_base'])
  high = bpy.data.objects.get(f'{base_name}_high')
  low = bpy.data.objects.get(f'{base_name}_low')
  if high is None and low is None:
    return None
  if high is None or low is None or high.type != 'MESH' or low.type != 'MESH':
    raise MeshyPipelineError('A partial committed High/Low adoption was found')
  return {
    'high_object': high.name,
    'low_object': low.name,
    'actual_low_polygons': len(low.data.polygons),
    'ue_unique_export_sync': _sync_low_export_via_ue_unique(
      scene,
      low,
      base_name,
    ),
    'recovered_after_state_write_gap': True,
  }


def validate_adopted_pair(state):
  """Resolve the once-named pair without expensive checkpoint rescans."""
  low_state = state.get('low') or {}
  high = bpy.data.objects.get(low_state.get('high_object', ''))
  low = bpy.data.objects.get(low_state.get('low_object', ''))
  if high is None or low is None or high.type != 'MESH' or low.type != 'MESH':
    raise MeshyPipelineError('The recorded High/Low pair is incomplete')
  return {
    'high': high,
    'low': low,
    'uv': dict(low_state.get('uv') or {}),
  }


def _resolved_image_path(image):
  if image is None or not image.filepath:
    return None
  try:
    value = bpy.path.abspath(image.filepath, library=image.library)
  except TypeError:
    value = bpy.path.abspath(image.filepath)
  return os.path.normcase(os.fspath(Path(value).resolve()))


def _socket_uses_image_path(socket, expected_path):
  expected = os.path.normcase(os.fspath(Path(expected_path).resolve()))
  stack = [link.from_node for link in socket.links]
  visited = set()
  while stack:
    node = stack.pop()
    pointer = node.as_pointer()
    if pointer in visited:
      continue
    visited.add(pointer)
    if node.type == 'TEX_IMAGE' and _resolved_image_path(node.image) == expected:
      return True
    for input_socket in node.inputs:
      stack.extend(link.from_node for link in input_socket.links)
  return False


def validate_final_material_roles(
  low_objects,
  texture_dir,
  required_roles_by_set=None,
):
  """Verify only the canonical roles that existed in the source package."""
  texture_dir = Path(texture_dir).resolve()
  required_roles_by_set = {
    str(name): set(roles)
    for name, roles in (required_roles_by_set or {}).items()
  }
  materials = sorted(
    {
      slot.material
      for obj in low_objects
      for slot in obj.material_slots
      if slot.material is not None
    },
    key=lambda material: material.name.casefold(),
  )
  if not materials:
    raise MeshyPipelineError('The recorded Low has no materials to verify')
  material_sets = {
    material: material_texture_set(material) for material in materials
  }
  try:
    verified = verify_painter_material_roles(
      low_objects,
      texture_dir,
      required_roles_by_set,
      material_texture_sets=material_sets,
    )
  except RuntimeError as exc:
    raise MeshyPipelineError(str(exc)) from exc
  return [
    {
      'material': material.name,
      'texture_set': texture_set,
      'roles': verified[f'M_{texture_set}'],
    }
    for material, texture_set in sorted(
      material_sets.items(),
      key=lambda item: item[1].casefold(),
    )
    if required_roles_by_set.get(texture_set)
  ]


def required_canonical_roles_from_state(state):
  """Return per-Texture-Set canonical outputs backed by the source package."""
  package = state.get('painter_package') or {}
  texture_sets = package.get('painter_texture_sets')
  maps = package.get('maps')
  if not isinstance(texture_sets, list) or not texture_sets:
    raise MeshyPipelineError('Painter package has no Texture Set list')
  if not isinstance(maps, dict) or not set(maps).issubset(set(texture_sets)):
    raise MeshyPipelineError('Painter package map sets are invalid')
  required = {}
  extra_transport = {'Extra', 'ExtraR', 'Roughness', 'Metallic'}
  for texture_set in texture_sets:
    package_roles = set((maps.get(texture_set) or {}).keys())
    observed_extra = package_roles & extra_transport
    if observed_extra and observed_extra != extra_transport:
      raise MeshyPipelineError(
        f'Painter package Extra transport is incomplete for {texture_set}'
      )
    roles = set()
    if 'BaseColor' in package_roles:
      roles.add('Color')
    if observed_extra:
      roles.add('Extra')
    if 'Normal' in package_roles:
      roles.add('Normal')
    required[str(texture_set)] = roles
  return required


def validate_final_meshy_pipeline(scene, state):
  """Read-only deterministic checks used before recording ``VERIFIED``."""
  if state['stage'] not in {'CANONICAL_APPLIED', 'VERIFIED'}:
    raise MeshyPipelineError(
      f'Final verification requires CANONICAL_APPLIED, not {state["stage"]}'
    )
  verify_source_archive_receipt(state['archive']['source_original'])
  baseline = (state.get('archive') or {}).get('bake_baseline') or {}
  snapshot_dir = baseline.get('snapshot_dir')
  if not snapshot_dir:
    raise MeshyPipelineError('The immutable bake baseline is not recorded')
  baseline_manifest = contract.verify_immutable_snapshot_set_archive(snapshot_dir)
  if contract.sha256_file(Path(snapshot_dir) / contract.MANIFEST_FILENAME) != baseline.get(
    'snapshot_manifest_sha256'
  ):
    raise MeshyPipelineError('Immutable bake manifest differs from its scene receipt')
  expected_baseline_entries = baseline.get('snapshot_entries')
  if expected_baseline_entries is None or sorted(
    baseline_manifest['files'], key=lambda entry: entry['path'].casefold()
  ) != sorted(expected_baseline_entries, key=lambda entry: entry['path'].casefold()):
    raise MeshyPipelineError('Immutable bake entries differ from their scene receipt')

  pair = validate_adopted_pair(state)
  handoff = (state.get('low') or {}).get('ue_unique_export_sync') or {}
  _require_low_export_handoff_ready(handoff)
  expected_high = f'{state["asset_base"]}_high'
  expected_low = f'{state["asset_base"]}_low'
  if pair['high'].name != expected_high or pair['low'].name != expected_low:
    raise MeshyPipelineError(
      f'High/Low names differ from the contract: {expected_high}, {expected_low}'
    )
  if pair['uv'].get('method') != UV_METHOD_VERSION or not pair['uv'].get('valid'):
    raise MeshyPipelineError('The one-time UVgami completion receipt is missing')

  painter_defaults = {
    'antialiasing': scene.substance_tools_baking.antialiasing,
    'match': scene.substance_tools_baking.match,
    'id_source': scene.substance_tools_baking.id_source,
  }
  expected_defaults = {
    'antialiasing': 'X2',
    'match': 'BY_MESH_NAME',
    'id_source': 'MATERIAL_COLOR',
  }
  if painter_defaults != expected_defaults:
    raise MeshyPipelineError(
      f'Painter settings changed after preparation: {painter_defaults}'
    )

  canonical_receipt = (state.get('checkpoints') or {}).get('CANONICAL_APPLIED') or {}
  required_roles_by_set = required_canonical_roles_from_state(state)
  texture_dir_value = canonical_receipt.get('texture_dir')
  if not texture_dir_value:
    raise MeshyPipelineError('CANONICAL_APPLIED has no texture directory')
  texture_dir = Path(texture_dir_value).resolve()
  expected_paths = {
    os.path.normcase(str(
      (texture_dir / f'{TEXTURE_PREFIX}{texture_set}_{role}.png').resolve()
    ))
    for texture_set, roles in required_roles_by_set.items()
    for role in roles
  }
  canonical_entries = canonical_receipt.get('canonical_files') or []
  if not canonical_entries or not expected_paths:
    raise MeshyPipelineError('CANONICAL_APPLIED has no final texture hash receipt')
  verified_files = []
  observed_paths = set()
  for entry in canonical_entries:
    path = Path(entry.get('path', '')).resolve()
    path_key = os.path.normcase(str(path))
    if path_key in observed_paths:
      raise MeshyPipelineError(f'Canonical texture is duplicated: {path}')
    observed_paths.add(path_key)
    if not path.is_file():
      raise MeshyPipelineError(f'Canonical texture is missing: {path}')
    observed_size = path.stat().st_size
    observed_hash = contract.sha256_file(path)
    if observed_size != int(entry.get('size', -1)) or observed_hash != entry.get('sha256'):
      raise MeshyPipelineError(f'Canonical texture changed after apply: {path}')
    verified_files.append({
      'path': str(path),
      'size': observed_size,
      'sha256': observed_hash,
    })
  if observed_paths != expected_paths:
    raise MeshyPipelineError(
      'Canonical texture receipt differs from the source role plan '
      f'(expected={sorted(expected_paths)}, observed={sorted(observed_paths)})'
    )

  material_roles = validate_final_material_roles(
    [pair['low']],
    texture_dir,
    required_roles_by_set,
  )
  painter_result = state.get('painter') or {}
  managed_layers = int(
    (painter_result.get('source_layer_result') or {}).get('managed_layer_count', 0)
  )
  normal_assigned = int(
    (painter_result.get('source_normal_mesh_map_result') or {}).get('assigned_count', 0)
  )
  expected_managed_sets = sorted(
    texture_set for texture_set, roles in required_roles_by_set.items()
    if roles & {'Color', 'Extra'}
  )
  expected_normal_sets = sorted(
    texture_set for texture_set, roles in required_roles_by_set.items()
    if 'Normal' in roles
  )
  verified_source = painter_result.get('verified_source_receipt') or {}
  if (
    sorted(verified_source.get('texture_sets') or ()) != expected_managed_sets
    or int(verified_source.get('managed_layer_count', -1))
    != len(expected_managed_sets)
    or sorted(verified_source.get('source_normal_texture_sets') or ())
    != expected_normal_sets
    or managed_layers != len(expected_managed_sets)
    or normal_assigned != len(expected_normal_sets)
  ):
    raise MeshyPipelineError(
      'Painter managed source receipts differ from the optional source role plan'
    )

  actual = int((state.get('low') or {}).get('actual_low_polygons', 0))
  target = int((state.get('analysis') or {}).get('target_quads') or 0)
  variance = abs(actual - target) / target if target else 0.0
  return {
    'verification_contract': 'meshy_final_v1',
    'visual_qa_confirmed': True,
    'high_object': pair['high'].name,
    'low_object': pair['low'].name,
    'target_quads': target,
    'actual_low_polygons': actual,
    'target_variance': round(variance, 9),
    'target_within_ten_percent': variance <= 0.10,
    'uv': pair['uv'],
    'required_roles_by_set': {
      name: sorted(roles) for name, roles in sorted(required_roles_by_set.items())
    },
    'painter_defaults': painter_defaults,
    'managed_layer_count': managed_layers,
    'source_normal_assigned_count': normal_assigned,
    'canonical_files': verified_files,
    'material_roles': material_roles,
    'unreal_handoff': handoff,
    'dry_noop_safe': True,
  }


def _pipeline_settings(scene):
  return getattr(scene, 'substance_tools_meshy_pipeline', None)


def _quad_target_override(scene):
  settings = getattr(scene, 'qr_workflow', None)
  return int(getattr(settings, 'target_override', 0) or 0)


class MeshyPipelineSettings(bpy.types.PropertyGroup):
  uv_resolution: bpy.props.IntProperty(
    name='UV Reference Resolution',
    default=2048,
    min=256,
    max=16384,
  )
  uv_margin_pixels: bpy.props.IntProperty(
    name='UV Margin (px)',
    default=8,
    min=1,
    max=128,
  )


def _continue_uvgami_low_uv(scene, state):
  """Start or confirm the asynchronous UVgami job for a named Low object."""
  if state.get('stage') not in {'LOW_CREATED', 'UVGAMI_RUNNING'}:
    raise MeshyPipelineError(
      'UVgami Low preparation requires LOW_CREATED or UVGAMI_RUNNING, '
      f'not {state.get("stage")}'
    )
  low_state = state.get('low') or {}
  low_name = str(low_state.get('low_object') or '')
  low = bpy.data.objects.get(low_name)
  if low is None or low.type != 'MESH':
    raise MeshyPipelineError(f'The named Low mesh is missing: {low_name or "<unset>"}')
  pending = low_state.get('uvgami')
  if pending:
    # Compatibility for v2 scenes saved before UVGAMI_RUNNING became an
    # explicit checkpoint: publish the already-started request once, then poll.
    if state['stage'] == 'LOW_CREATED':
      state = advance_pipeline_state(state, 'UVGAMI_RUNNING', pending)
      store_pipeline_state(scene, state)
    receipt = confirm_uvgami_low_uv(scene, low, pending)
    if receipt is None:
      _activate_only(low)
      return state, 'RUNNING', None
    updated_low = dict(low_state)
    updated_low['uv'] = receipt
    updated_low['uvgami'] = {**pending, 'status': 'COMPLETE'}
    updated_state = dict(state)
    updated_state['low'] = updated_low
    updated_state = advance_pipeline_state(updated_state, 'UV_READY', receipt)
    store_pipeline_state(scene, updated_state)
    _activate_only(low)
    return updated_state, 'COMPLETE', receipt

  if state['stage'] == 'UVGAMI_RUNNING':
    raise MeshyPipelineError('UVGAMI_RUNNING has no recorded UVgami request')

  settings = _pipeline_settings(scene)
  resolution = settings.uv_resolution if settings is not None else 2048
  margin_pixels = settings.uv_margin_pixels if settings is not None else 8
  request = launch_uvgami_low_uv(
    scene,
    low,
    resolution=resolution,
    margin_pixels=margin_pixels,
  )
  updated_low = dict(low_state)
  updated_low['uvgami'] = request
  updated_state = dict(state)
  updated_state['low'] = updated_low
  updated_state = advance_pipeline_state(
    updated_state,
    'UVGAMI_RUNNING',
    request,
  )
  store_pipeline_state(scene, updated_state)
  return updated_state, 'RUNNING', request


class AnalyzeMeshySourceOperator(bpy.types.Operator):
  """Analyze one source mesh without changing its geometry or materials"""
  bl_idname = 'st.analyze_meshy_source'
  bl_label = 'Analyze High-Poly Source'
  bl_options = {'REGISTER'}

  def execute(self, context):
    try:
      source = _single_selected_mesh(context)
      override = _quad_target_override(context.scene)
      analysis = analyze_mesh_object(source, context.scene, override)
      # Deliberately do not persist the analysis.  A scene custom property would
      # dirty the .blend and make the subsequent immutable-source preflight
      # indistinguishable from a real unsaved geometry/material edit.  Prepare
      # recomputes and records the same deterministic analysis after archiving.
      if analysis['target_mode'] == 'SKIP_ALREADY_LOW':
        self.report(
          {'INFO'},
          f'{source.name}: {analysis["evaluated_triangles"]:,} tris; already Low '
          '(read-only analysis)',
        )
      else:
        self.report(
          {'INFO'},
          f'{source.name}: {analysis["evaluated_triangles"]:,} tris -> '
          f'{analysis["target_quads"]:,} target quads (read-only analysis)',
        )
      return {'FINISHED'}
    except Exception as exc:
      self.report({'ERROR'}, str(exc))
      return {'CANCELLED'}


class PrepareMeshyRetopoOperator(bpy.types.Operator):
  """Archive the clean source and configure QR; never run remeshing"""
  bl_idname = 'st.prepare_meshy_retopo'
  bl_label = 'Prepare High-Poly Retopo'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    try:
      source = _single_selected_mesh(context)
      override = _quad_target_override(context.scene)
      state = load_pipeline_state(context.scene)
      if state and pipeline_stage_index(state['stage']) >= pipeline_stage_index('QR_READY'):
        verify_source_archive_receipt(state['archive']['source_original'])
        source = _find_source_from_state(state)
        validate_content_signature(
          source,
          context.scene,
          state['source'].get('content_signature'),
          include_material=True,
          label='High source',
          include_source_files=(
            pipeline_stage_index(state['stage'])
            < pipeline_stage_index('CANONICAL_APPLIED')
          ),
        )
        if pipeline_stage_index(state['stage']) > pipeline_stage_index('QR_READY'):
          self.report({'INFO'}, f'Pipeline is already at {state["stage"]}')
          return {'FINISHED'}
        state['painter_defaults'] = configure_meshy_painter_defaults(context.scene)
        store_pipeline_state(context.scene, state)
        qr_receipt = configure_quad_remesher(
          context.scene,
          state['analysis']['target_quads'],
        )
        _activate_only(source)
        self.report(
          {'INFO'},
          f'QR is ready at {state["analysis"]["target_quads"]:,} quads; '
          'press Remesh It once',
        )
        return {'FINISHED'}
      if not state:
        analysis = analyze_mesh_object(source, context.scene, override)
        state = _new_analysis_state(analysis)
      else:
        if state.get('asset_base') != object_base_name(source.name):
          raise MeshyPipelineError(
            'Another retopo/Painter asset already owns this scene state; '
            'finish it or start the other asset in a separate scene'
          )
        analysis = state['analysis']
      if analysis['target_mode'] == 'SKIP_ALREADY_LOW':
        raise MeshyPipelineError('Source is already at or below the low-poly gate')
      if pipeline_stage_index(state['stage']) < pipeline_stage_index('SOURCE_ARCHIVED'):
        if bpy.data.is_dirty:
          raise MeshyPipelineError(
            'Save the blend once before publishing the immutable original archive'
          )
        archive = create_source_archive(state['asset_base'], analysis)
        blend_entry = next(entry for entry in archive['entries'] if entry['role'] == 'Blend')
        stable_id = _source_identity(blend_entry['source']['sha256'], source)
        existing_id = source.get(SOURCE_ID_PROPERTY)
        if existing_id not in {None, stable_id}:
          raise MeshyPipelineError('Selected source already belongs to another pipeline')
        source[SOURCE_ID_PROPERTY] = stable_id
        state['source'] = {
          'object_name': source.name,
          'mesh_name': source.data.name,
          'stable_id': stable_id,
          'content_signature': analysis['content_signature'],
        }
        state['archive'] = {'source_original': archive}
        state = advance_pipeline_state(
          state,
          'SOURCE_ARCHIVED',
          {'archive_root': archive['root'], 'entry_count': len(archive['entries'])},
        )
        store_pipeline_state(context.scene, state)
      else:
        verify_source_archive_receipt(state['archive']['source_original'])
        pinned_source = _find_source_from_state(state)
        if pinned_source is not source:
          raise MeshyPipelineError(
            'Select the source mesh recorded by the active retopo/Painter pipeline'
          )
        validate_content_signature(
          pinned_source,
          context.scene,
          state['source'].get('content_signature'),
          include_material=True,
          label='High source',
        )
      state['painter_defaults'] = configure_meshy_painter_defaults(context.scene)
      qr_receipt = configure_quad_remesher(
        context.scene,
        state['analysis']['target_quads'],
      )
      qr_receipt['preexisting_mesh_objects'] = sorted(
        obj.name for obj in bpy.data.objects if obj.type == 'MESH'
      )
      state['qr'] = qr_receipt
      state = advance_pipeline_state(state, 'QR_READY', qr_receipt)
      store_pipeline_state(context.scene, state)
      _activate_only(source)
      self.report(
        {'INFO'},
        f'QR ready: {state["analysis"]["target_quads"]:,} quads. '
        'Use Quad Remesher > Remesh It once, then Finalize',
      )
      return {'FINISHED'}
    except Exception as exc:
      self.report({'ERROR'}, str(exc))
      return {'CANCELLED'}


class FinalizeMeshyRetopoOperator(bpy.types.Operator):
  """Adopt the selected QR result, then start or confirm UVgami"""
  bl_idname = 'st.finalize_meshy_retopo'
  bl_label = 'Finalize Retopo + UV'
  bl_options = {'REGISTER', 'UNDO'}

  def execute(self, context):
    try:
      state = load_pipeline_state(context.scene)
      if not state:
        raise MeshyPipelineError('Run Prepare High-Poly Retopo first')
      stage = pipeline_stage_index(state['stage'])
      if stage >= pipeline_stage_index('LOW_CREATED'):
        _, changed = _refresh_recorded_low_export_unit(context.scene, state)
        if changed:
          store_pipeline_state(context.scene, state)
      if stage >= pipeline_stage_index('UV_READY'):
        pair = validate_adopted_pair(state)
        _activate_only(pair['low'])
        self.report({'INFO'}, 'High/Low are already named and UVgami is already ready')
        return {'FINISHED'}
      if state['stage'] in {'LOW_CREATED', 'UVGAMI_RUNNING'}:
        state, uv_status, uv_receipt = _continue_uvgami_low_uv(
          context.scene,
          state,
        )
        if uv_status == 'RUNNING':
          self.report(
            {'INFO'},
            'UVgami OptCuts is still running; run Finalize again when it finishes',
          )
        else:
          self.report(
            {'INFO'},
            f'UVgami Low UV ready: {uv_receipt["layer"]}',
          )
        return {'FINISHED'}
      if state['stage'] != 'QR_READY':
        raise MeshyPipelineError(
          f'Finalize requires QR_READY, not {state["stage"]}'
        )
      verify_source_archive_receipt(
        (state.get('archive') or {}).get('source_original') or {}
      )
      receipt = recover_committed_adoption(context.scene, state)
      if receipt is not None:
        state['low'] = receipt
        state = advance_pipeline_state(state, 'LOW_CREATED', receipt)
        store_pipeline_state(context.scene, state)
      else:
        # Ask UVgami's owner to preflight before the pairing transaction.
        settings = _pipeline_settings(context.scene)
        resolution = settings.uv_resolution if settings is not None else 2048
        margin = settings.uv_margin_pixels if settings is not None else 8
        result = _single_selected_mesh(context)
        source = _find_source_from_state(state)
        uvgami_api = _resolve_uvgami_workflow_api()
        preflight = _call_uvgami(
          uvgami_api,
          'preflight_unwrap',
          [result],
          scene=context.scene,
          resolution=resolution,
          margin_pixels=margin,
        )
        if (
          not isinstance(preflight, dict)
          or preflight.get('service_id') != uvgami_api['service_id']
          or preflight.get('api_version') != uvgami_api['version']
          or preflight.get('status') != 'READY'
        ):
          raise MeshyPipelineError('UVgami returned an invalid preflight receipt')
        receipt = adopt_qr_result(context.scene, source, result, state)
        state['low'] = receipt
        state = advance_pipeline_state(state, 'LOW_CREATED', receipt)
        store_pipeline_state(context.scene, state)

      requested = state['analysis']['target_quads']
      actual = receipt['actual_low_polygons']
      variance = abs(actual - requested) / requested if requested else 0.0
      suffix = ' (outside +/-10%)' if variance > 0.10 else ''
      state, uv_status, _ = _continue_uvgami_low_uv(context.scene, state)
      self.report(
        {'WARNING'} if variance > 0.10 else {'INFO'},
        f'Adopted {receipt["high_object"]} / {receipt["low_object"]}: '
        f'{actual:,} polygons{suffix}; UVgami Hard Surface started. '
        'Run Finalize again after it finishes',
      )
      return {'FINISHED'}
    except Exception as exc:
      self.report({'ERROR'}, str(exc))
      return {'CANCELLED'}


class PrepareMeshyLowUVOperator(bpy.types.Operator):
  """Start or confirm UVgami for the once-named Low without touching High"""
  bl_idname = 'st.prepare_meshy_low_uv'
  bl_label = 'Prepare Low UV'
  bl_options = {'REGISTER', 'UNDO'}

  force_rebuild: bpy.props.BoolProperty(
    name='Rebuild UV',
    default=False,
  )

  def execute(self, context):
    try:
      state = load_pipeline_state(context.scene)
      if not state:
        raise MeshyPipelineError('No Meshy High/Low adoption is recorded')
      if (
        self.force_rebuild
        and pipeline_stage_index(state['stage']) > pipeline_stage_index('UV_READY')
      ):
        raise MeshyPipelineError(
          'Low UV cannot be rebuilt after the immutable bake baseline exists'
        )
      if pipeline_stage_index(state['stage']) >= pipeline_stage_index('UV_READY'):
        if self.force_rebuild:
          raise MeshyPipelineError(
            'The accepted UVgami result is already checkpointed; start a new '
          'retopo run to replace it'
        )
      if pipeline_stage_index(state['stage']) >= pipeline_stage_index('LOW_CREATED'):
        _, changed = _refresh_recorded_low_export_unit(context.scene, state)
        if changed:
          store_pipeline_state(context.scene, state)
      if pipeline_stage_index(state['stage']) >= pipeline_stage_index('UV_READY'):
        pair = validate_adopted_pair(state)
        _activate_only(pair['low'])
        self.report({'INFO'}, 'UVgami Low UV is already ready')
        return {'FINISHED'}
      state, uv_status, receipt = _continue_uvgami_low_uv(context.scene, state)
      if uv_status == 'RUNNING':
        self.report(
          {'INFO'},
          'UVgami OptCuts is running; run this check again when it finishes',
        )
      else:
        self.report({'INFO'}, f'UVgami Low UV ready: {receipt["layer"]}')
      return {'FINISHED'}
    except Exception as exc:
      self.report({'ERROR'}, str(exc))
      return {'CANCELLED'}


class MeshyPipelineStatusOperator(bpy.types.Operator):
  """Report the current structured Meshy pipeline checkpoint"""
  bl_idname = 'st.meshy_pipeline_status'
  bl_label = 'Retopo / Painter Status'

  def execute(self, context):
    try:
      state = load_pipeline_state(context.scene, allow_legacy=True)
      if not state:
        self.report({'INFO'}, 'No retopo/Painter pipeline state in this scene')
      else:
        target = state.get('analysis', {}).get('target_quads')
        target_text = f', target {target:,}' if target else ''
        legacy_text = ' (legacy v1, read-only)' if state.get('_legacy_read_only') else ''
        self.report(
          {'INFO'},
          f'{state.get("asset_base", "Asset")}: {state["stage"]}'
          f'{target_text}{legacy_text}',
        )
      return {'FINISHED'}
    except Exception as exc:
      self.report({'ERROR'}, str(exc))
      return {'CANCELLED'}


class VerifyMeshyPipelineOperator(bpy.types.Operator):
  """Record VERIFIED after deterministic checks and explicit visual QA"""
  bl_idname = 'st.verify_meshy_pipeline'
  bl_label = 'Verify Retopo / Painter Pipeline'
  bl_options = {'REGISTER'}

  visual_qa_confirmed: bpy.props.BoolProperty(
    name='Visual QA Confirmed',
    description='Confirm silhouette, folds, seams, and channel appearance were reviewed',
    default=False,
    options={'SKIP_SAVE'},
  )

  def execute(self, context):
    try:
      if not self.visual_qa_confirmed:
        raise MeshyPipelineError(
          'Visual QA confirmation is required before recording VERIFIED'
        )
      state = load_pipeline_state(context.scene)
      if not state:
        raise MeshyPipelineError('No Meshy pipeline state in this scene')
      handoff, changed = _refresh_recorded_low_export_unit(
        context.scene,
        state,
        force=True,
      )
      if changed:
        store_pipeline_state(context.scene, state)
      _require_low_export_handoff_ready(handoff)
      receipt = validate_final_meshy_pipeline(context.scene, state)
      if state['stage'] == 'VERIFIED':
        self.report(
          {'INFO'},
          'VERIFIED receipt and current outputs still match (read-only no-op)',
        )
        return {'FINISHED'}
      state = advance_pipeline_state(state, 'VERIFIED', receipt)
      store_pipeline_state(context.scene, state)
      self.report(
        {'INFO'},
        f'VERIFIED {receipt["low_object"]}: '
        f'{receipt["actual_low_polygons"]:,} polygons, '
        f'{len(receipt["canonical_files"])} canonical files',
      )
      return {'FINISHED'}
    except Exception as exc:
      self.report({'ERROR'}, str(exc))
      return {'CANCELLED'}


CLASSES = (
  MeshyPipelineSettings,
  AnalyzeMeshySourceOperator,
  PrepareMeshyRetopoOperator,
  FinalizeMeshyRetopoOperator,
  PrepareMeshyLowUVOperator,
  MeshyPipelineStatusOperator,
  VerifyMeshyPipelineOperator,
)


def register_scene_properties():
  if not hasattr(bpy.types.Scene, 'substance_tools_meshy_pipeline'):
    bpy.types.Scene.substance_tools_meshy_pipeline = bpy.props.PointerProperty(
      type=MeshyPipelineSettings
    )


def unregister_scene_properties():
  if hasattr(bpy.types.Scene, 'substance_tools_meshy_pipeline'):
    del bpy.types.Scene.substance_tools_meshy_pipeline


def register():
  for cls in CLASSES:
    bpy.utils.register_class(cls)
  register_scene_properties()


def unregister():
  unregister_scene_properties()
  for cls in reversed(CLASSES):
    bpy.utils.unregister_class(cls)


__all__ = [
  'CLASSES',
  'FinalizeMeshyRetopoOperator',
  'LEGACY_STATE_PROPERTY',
  'LOW_ID_PROPERTY',
  'LOW_UV_RECEIPT_PROPERTY',
  'MATERIAL_TEXTURE_SET_PROPERTY',
  'MeshyPipelineError',
  'MeshyPipelineSettings',
  'PIPELINE_STAGES',
  'SOURCE_ID_PROPERTY',
  'STATE_PROPERTY',
  'STATE_SCHEMA_VERSION',
  'UV_METHOD_VERSION',
  'UVGAMI_WORKFLOW_API_VERSION',
  'adopt_retopology_pair',
  'adopt_qr_result',
  'advance_pipeline_state',
  'analyze_mesh_object',
  'confirm_uvgami_low_uv',
  'configure_quad_remesher',
  'configure_meshy_painter_defaults',
  'create_source_archive',
  'discover_source_images',
  'load_pipeline_state',
  'launch_uvgami_low_uv',
  'mesh_object_content_signature',
  'object_base_name',
  'recover_committed_adoption',
  'required_canonical_roles_from_state',
  'register_scene_properties',
  'store_pipeline_state',
  'unregister_scene_properties',
  'validate_adopted_pair',
  'validate_content_signature',
  'validate_final_material_roles',
  'validate_final_meshy_pipeline',
  'validate_low_uv',
  'validate_target_override',
  'verify_source_archive_receipt',
]
