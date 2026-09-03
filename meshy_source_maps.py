"""Bake Meshy source material maps into the paired low-poly UV layout.

The public entry point is :class:`BakeMeshySourceMapsOperator` with idname
``st.bake_meshy_source_maps``.  One invocation discovers all supported source
roles and handles them together; there are intentionally no separate Extra or
Normal operators.

All shader edits happen on evaluated temporary duplicates.  The source high
and destination low objects, materials, node trees, UV layers, selection, and
render/bake settings are left untouched.  Files are first produced in a
private staging directory, archived through the pure immutable snapshot
contract, and only then committed to the ordinary Painter handoff paths.
"""
from __future__ import annotations

from array import array
from collections import defaultdict
from pathlib import Path
import json
import math
import os
import re
import shutil
import struct
import tempfile
import traceback
import uuid

import bpy

from .core import (
  baking_paths,
  clean_name,
  duplicate_for_export,
  export_objects_to_fbx,
  get_baking_collections,
  painter_collection_meshes,
  source_map_bake_name,
  stripped_material_name,
)
from .meshy_pipeline_contract import (
  MANIFEST_FILENAME,
  file_manifest,
  publish_immutable_snapshot_set,
  verify_immutable_snapshot_set_archive,
)
from .meshy_pipeline import (
  MATERIAL_TEXTURE_SET_PROPERTY,
  advance_pipeline_state,
  load_pipeline_state,
  pipeline_stage_index,
  store_pipeline_state,
  validate_adopted_pair,
  verify_source_archive_receipt,
)


_LOW_NAME = re.compile(r'^(?P<base>.+)_low$')
_HIGH_NAME = re.compile(r'^(?P<base>.+)_high(?:_(?P<part>[0-9]+))?$')
_ROLE_ALIASES = {
  'basecolor': 'BaseColor',
  'base_color': 'BaseColor',
  'albedo': 'BaseColor',
  'color': 'BaseColor',
  'diffuse': 'BaseColor',
  'extra': 'Extra',
  'metallicroughness': 'Extra',
  'metallic_roughness': 'Extra',
  'metalrough': 'Extra',
  'orm': 'Extra',
  'occlusionroughnessmetallic': 'Extra',
  'occlusion_roughness_metallic': 'Extra',
  'normal': 'Normal',
  'normalmap': 'Normal',
  'normal_map': 'Normal',
}
_ROLE_SUFFIXES = {
  'BaseColor': ('_color', '_basecolor', '_base_color', '_albedo', '_diffuse'),
  'Extra': ('_extra', '_orm', '_metallicroughness', '_metallic_roughness'),
  'Normal': ('_normal', '_normalmap', '_normal_map'),
}
_IMAGE_ROLE_PROPERTY = '_ue_unique_export_original_name'


def _normalized_role(value):
  token = re.sub(r'[^0-9a-z_]+', '', str(value or '').strip().casefold())
  return _ROLE_ALIASES.get(token)


def _image_identity_text(image):
  values = [getattr(image, 'name', '')]
  filepath = getattr(image, 'filepath_raw', '') or getattr(image, 'filepath', '')
  if filepath:
    try:
      values.append(Path(bpy.path.abspath(filepath)).stem)
    except (OSError, RuntimeError, ValueError):
      values.append(Path(filepath).stem)
  return tuple(str(value).casefold() for value in values if value)


def _node_image_role(node):
  image = getattr(node, 'image', None)
  if image is None:
    return None
  role = _normalized_role(image.get(_IMAGE_ROLE_PROPERTY, ''))
  if role:
    return role
  for candidate in _image_identity_text(image):
    for fallback_role, suffixes in _ROLE_SUFFIXES.items():
      if any(candidate.endswith(suffix) for suffix in suffixes):
        return fallback_role
  return None


def _upstream_image_nodes(socket, visited=None):
  """Return image nodes reachable upstream from a shader input socket."""
  if socket is None or not getattr(socket, 'is_linked', False):
    return set()
  if visited is None:
    visited = set()
  result = set()
  for link in socket.links:
    node = link.from_node
    pointer = node.as_pointer()
    if pointer in visited:
      continue
    visited.add(pointer)
    if node.type == 'TEX_IMAGE' and node.image is not None:
      result.add(node)
      continue
    for input_socket in node.inputs:
      result.update(_upstream_image_nodes(input_socket, visited))
  return result


def _single_node(candidates, material, role, evidence):
  candidates = {node for node in candidates if node.image is not None}
  if len(candidates) > 1:
    names = ', '.join(sorted(node.image.name for node in candidates))
    raise RuntimeError(
      f"Ambiguous {role} source in material '{material.name}' ({evidence}): {names}"
    )
  return next(iter(candidates), None)


def discover_material_source_nodes(material):
  """Discover BaseColor, packed Extra, and Normal image nodes in one material.

  Metadata wins over graph connectivity, and connectivity wins over filename
  suffixes.  Ambiguous evidence is rejected instead of silently selecting one
  bitmap.
  """
  if material is None or not material.use_nodes or material.node_tree is None:
    return {}
  nodes = material.node_tree.nodes
  image_nodes = [
    node for node in nodes
    if node.type == 'TEX_IMAGE' and node.image is not None
  ]
  result = {}

  for role in ('BaseColor', 'Extra', 'Normal'):
    metadata_nodes = {
      node for node in image_nodes
      if _normalized_role(node.image.get(_IMAGE_ROLE_PROPERTY, '')) == role
    }
    node = _single_node(metadata_nodes, material, role, 'image metadata')
    if node is not None:
      result[role] = node

  principled_nodes = [node for node in nodes if node.type == 'BSDF_PRINCIPLED']
  if 'BaseColor' not in result:
    connected = set()
    for principled in principled_nodes:
      connected.update(_upstream_image_nodes(principled.inputs.get('Base Color')))
    node = _single_node(connected, material, 'BaseColor', 'Principled Base Color')
    if node is not None:
      result['BaseColor'] = node

  if 'Extra' not in result:
    roughness_nodes = set()
    metallic_nodes = set()
    for principled in principled_nodes:
      roughness_nodes.update(_upstream_image_nodes(principled.inputs.get('Roughness')))
      metallic_nodes.update(_upstream_image_nodes(principled.inputs.get('Metallic')))
    connected = roughness_nodes & metallic_nodes
    node = _single_node(connected, material, 'Extra', 'shared Roughness/Metallic input')
    if node is not None:
      result['Extra'] = node

  if 'Normal' not in result:
    connected = set()
    for principled in principled_nodes:
      connected.update(_upstream_image_nodes(principled.inputs.get('Normal')))
    node = _single_node(connected, material, 'Normal', 'Principled Normal')
    if node is not None:
      result['Normal'] = node

  for role in ('BaseColor', 'Extra', 'Normal'):
    if role in result:
      continue
    fallback = {node for node in image_nodes if _node_image_role(node) == role}
    node = _single_node(fallback, material, role, 'standard suffix')
    if node is not None:
      result[role] = node
  return result


def _strict_pair_map(low_objects, high_objects):
  low_by_base = {}
  for obj in low_objects:
    match = _LOW_NAME.fullmatch(obj.name)
    if match is None:
      raise RuntimeError(
        f"Low mesh must end exactly in '_low': {obj.name}"
      )
    base = match.group('base')
    if base in low_by_base:
      raise RuntimeError(f"Duplicate Low pair base '{base}'")
    low_by_base[base] = obj

  high_by_base = defaultdict(list)
  for obj in high_objects:
    match = _HIGH_NAME.fullmatch(obj.name)
    if match is None or (
      match.group('part') is not None and int(match.group('part')) == 0
    ):
      raise RuntimeError(
        f"High mesh must end in '_high' or '_high_<number>': {obj.name}"
      )
    high_by_base[match.group('base')].append(obj)

  low_bases = set(low_by_base)
  high_bases = set(high_by_base)
  if low_bases != high_bases:
    missing_high = sorted(low_bases - high_bases)
    missing_low = sorted(high_bases - low_bases)
    raise RuntimeError(
      'High/Low name pairs do not match exactly; '
      f'missing High={missing_high}, missing Low={missing_low}'
    )
  return {
    base: (low_by_base[base], sorted(high_by_base[base], key=lambda obj: obj.name))
    for base in sorted(low_bases)
  }


def _material_texture_sets(obj):
  return {
    str(
      material.get(MATERIAL_TEXTURE_SET_PROPERTY)
      or stripped_material_name(material.name)
    )
    for material in obj.data.materials
    if material is not None
  }


def _validate_clean_texture_set_names(texture_sets):
  by_clean = defaultdict(list)
  for texture_set in texture_sets:
    by_clean[clean_name(texture_set).casefold()].append(texture_set)
  collisions = [values for values in by_clean.values() if len(values) > 1]
  if collisions:
    raise RuntimeError(
      f'Texture Set names collide after filename normalization: {collisions}'
    )


def _validate_low_uv(obj):
  mesh = obj.data
  uv_layer = mesh.uv_layers.active if mesh is not None else None
  if uv_layer is None:
    raise RuntimeError(f"Low mesh has no active UV map: {obj.name}")
  if not mesh.polygons:
    raise RuntimeError(f"Low mesh has no polygons: {obj.name}")

  epsilon = 1.0e-7
  outside = 0
  for loop_data in uv_layer.data:
    u, v = float(loop_data.uv.x), float(loop_data.uv.y)
    if not math.isfinite(u) or not math.isfinite(v):
      raise RuntimeError(f"Low mesh has non-finite UV coordinates: {obj.name}")
    if u < -epsilon or u > 1.0 + epsilon or v < -epsilon or v > 1.0 + epsilon:
      outside += 1
  if outside:
    raise RuntimeError(
      f"Low mesh has {outside} UV loop(s) outside the 0-1 tile: {obj.name}"
    )

  zero_area_faces = []
  for polygon in mesh.polygons:
    points = [uv_layer.data[index].uv for index in polygon.loop_indices]
    twice_area = 0.0
    for index, point in enumerate(points):
      following = points[(index + 1) % len(points)]
      twice_area += float(point.x) * float(following.y)
      twice_area -= float(following.x) * float(point.y)
    if abs(twice_area) <= 1.0e-12:
      zero_area_faces.append(polygon.index)
      if len(zero_area_faces) == 8:
        break
  if zero_area_faces:
    raise RuntimeError(
      f"Low mesh has zero-area UV face(s) {zero_area_faces}: {obj.name}"
    )
  return uv_layer.name


def _build_source_bindings(pair_map):
  """Map each High material slot to its corresponding Low Texture Set."""
  bindings = {}
  texture_sets = set()
  for base, (low, highs) in pair_map.items():
    low_sets = _material_texture_sets(low)
    if not low_sets:
      raise RuntimeError(f"Low mesh has no material / Texture Set: {low.name}")
    texture_sets.update(low_sets)
    for high in highs:
      high_materials = {
        material for material in high.data.materials if material is not None
      }
      if not high_materials:
        raise RuntimeError(f"High mesh has no material: {high.name}")
      for material in high_materials:
        material_set = str(
          material.get(MATERIAL_TEXTURE_SET_PROPERTY)
          or stripped_material_name(
            re.sub(r'^__SubstanceToolsHigh_', '', material.name)
          )
        )
        if material_set in low_sets:
          texture_set = material_set
        elif len(low_sets) == 1:
          texture_set = next(iter(low_sets))
        else:
          raise RuntimeError(
            f"High material '{material.name}' cannot be assigned to one of Low "
            f"Texture Sets {sorted(low_sets)} for pair '{base}'"
          )
        bindings[(high.as_pointer(), material.as_pointer())] = {
          'texture_set': texture_set,
          'nodes': discover_material_source_nodes(material),
        }
  _validate_clean_texture_set_names(texture_sets)
  return bindings, sorted(texture_sets)


def _new_bake_image(image_name, path, resolution, colorspace, clear_color):
  image = bpy.data.images.new(
    image_name,
    width=resolution,
    height=resolution,
    alpha=False,
    float_buffer=False,
  )
  image.generated_color = clear_color
  image.filepath_raw = str(path)
  image.file_format = 'PNG'
  image.colorspace_settings.name = colorspace
  pixel_count = resolution * resolution
  repeated = array('f', clear_color) * pixel_count
  image.pixels.foreach_set(repeated)
  image.update()
  return image


def _copy_materials_for_duplicates(source_objects, duplicates):
  temporary_materials = []
  slot_records = []
  for source, duplicate in zip(source_objects, duplicates):
    for index, original in enumerate(list(duplicate.data.materials)):
      if original is None:
        continue
      copied = original.copy()
      copied.name = f'__ST_MeshyBake_{original.name}'
      duplicate.data.materials[index] = copied
      temporary_materials.append(copied)
      slot_records.append((source, duplicate, index, original, copied))
  return temporary_materials, slot_records


def _set_low_targets(low_slot_records, image_by_set, dummy_image):
  target_nodes = []
  for _source, _duplicate, _index, original, copied in low_slot_records:
    copied.use_nodes = True
    if copied.node_tree is None:
      raise RuntimeError(f"Unable to create node tree for '{copied.name}'")
    texture_set = clean_name(
      original.get(MATERIAL_TEXTURE_SET_PROPERTY)
      or stripped_material_name(original.name)
    )
    image = image_by_set.get(texture_set, dummy_image)
    node = copied.node_tree.nodes.new('ShaderNodeTexImage')
    node.name = '__ST_MeshyBakeTarget'
    node.image = image
    copied.node_tree.nodes.active = node
    for candidate in copied.node_tree.nodes:
      candidate.select = candidate == node
    target_nodes.append((copied, node))
  return target_nodes


def _remove_target_nodes(target_nodes):
  for material, node in target_nodes:
    node_tree = material.node_tree
    if node_tree is not None and node.name in node_tree.nodes:
      node_tree.nodes.remove(node)


def _copied_source_node(copied_material, original_node):
  if original_node is None or copied_material.node_tree is None:
    return None
  candidate = copied_material.node_tree.nodes.get(original_node.name)
  if candidate is None or candidate.type != 'TEX_IMAGE' or candidate.image is None:
    raise RuntimeError(
      f"Temporary material lost source image node '{original_node.name}'"
    )
  return candidate


def _configure_emission_source(copied_material, source_node):
  node_tree = copied_material.node_tree
  if node_tree is None:
    raise RuntimeError(f"Material has no node tree: {copied_material.name}")
  output = node_tree.nodes.get('__ST_MeshyBakeOutput')
  if output is None:
    output = node_tree.nodes.new('ShaderNodeOutputMaterial')
    output.name = '__ST_MeshyBakeOutput'
  output.is_active_output = True
  emission = node_tree.nodes.get('__ST_MeshyBakeEmission')
  if emission is None:
    emission = node_tree.nodes.new('ShaderNodeEmission')
    emission.name = '__ST_MeshyBakeEmission'
  for link in list(output.inputs['Surface'].links):
    node_tree.links.remove(link)
  for link in list(emission.inputs['Color'].links):
    node_tree.links.remove(link)
  if source_node is None:
    emission.inputs['Color'].default_value = (0.0, 0.0, 0.0, 1.0)
  else:
    node_tree.links.new(source_node.outputs['Color'], emission.inputs['Color'])
  emission.inputs['Strength'].default_value = 1.0
  node_tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])


def _normal_is_connected(material, source_node):
  if material.node_tree is None or source_node is None:
    return False
  for principled in (
    node for node in material.node_tree.nodes
    if node.type == 'BSDF_PRINCIPLED'
  ):
    if source_node in _upstream_image_nodes(principled.inputs.get('Normal')):
      return True
  return False


def _configure_default_normal_source(copied_material, source_node):
  """Connect an otherwise unconnected tagged Normal as OpenGL tangent data."""
  node_tree = copied_material.node_tree
  if node_tree is None or source_node is None:
    return
  output = node_tree.nodes.get('__ST_MeshyNormalOutput')
  if output is None:
    output = node_tree.nodes.new('ShaderNodeOutputMaterial')
    output.name = '__ST_MeshyNormalOutput'
  output.is_active_output = True
  principled = node_tree.nodes.get('__ST_MeshyNormalSurface')
  if principled is None:
    principled = node_tree.nodes.new('ShaderNodeBsdfPrincipled')
    principled.name = '__ST_MeshyNormalSurface'
  normal_map = node_tree.nodes.get('__ST_MeshyNormalMap')
  if normal_map is None:
    normal_map = node_tree.nodes.new('ShaderNodeNormalMap')
    normal_map.name = '__ST_MeshyNormalMap'
  normal_map.space = 'TANGENT'
  for socket in (output.inputs['Surface'], principled.inputs['Normal'], normal_map.inputs['Color']):
    for link in list(socket.links):
      node_tree.links.remove(link)
  node_tree.links.new(source_node.outputs['Color'], normal_map.inputs['Color'])
  node_tree.links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
  node_tree.links.new(principled.outputs['BSDF'], output.inputs['Surface'])


def _role_presence(bindings, role):
  result = set()
  for binding in bindings.values():
    if role in binding['nodes']:
      result.add(binding['texture_set'])
  return result


def _image_paths_for_role(texture_sets, role, directory):
  return {
    texture_set: directory / f'{source_map_bake_name(texture_set, role)}.png'
    for texture_set in texture_sets
  }


def _select_pair_for_bake(low_duplicate, high_duplicates):
  bpy.ops.object.select_all(action='DESELECT')
  for high in high_duplicates:
    high.select_set(True)
  low_duplicate.select_set(True)
  bpy.context.view_layer.objects.active = low_duplicate


def _run_selected_to_active_pass(
  pair_duplicates,
  low_slot_records,
  role,
  enabled_sets,
  output_dir,
  resolution,
  temporary_images,
):
  colorspace = 'sRGB' if role == 'BaseColor' else 'Non-Color'
  clear_color = (
    (0.5, 0.5, 1.0, 1.0) if role == 'Normal'
    else (0.0, 0.0, 0.0, 1.0)
  )
  output_paths = _image_paths_for_role(enabled_sets, role, output_dir)
  image_by_set = {
    texture_set: _new_bake_image(
      f'__ST_Meshy_{clean_name(texture_set)}_{role}_{uuid.uuid4().hex}',
      path,
      resolution,
      colorspace,
      clear_color,
    )
    for texture_set, path in output_paths.items()
  }
  temporary_images.extend(image_by_set.values())
  dummy_image = _new_bake_image(
    f'__ST_MeshyDummy_{role}_{uuid.uuid4().hex}',
    output_dir / f'.dummy_{role}.png',
    4,
    colorspace,
    clear_color,
  )
  temporary_images.append(dummy_image)
  target_nodes = _set_low_targets(low_slot_records, image_by_set, dummy_image)
  try:
    for _base, (low_duplicate, high_duplicates) in pair_duplicates.items():
      _select_pair_for_bake(low_duplicate, high_duplicates)
      if role == 'Normal':
        bpy.ops.object.bake(
          type='NORMAL',
          normal_space='TANGENT',
          normal_r='POS_X',
          # Painter projects created from the Unreal template consume DirectX
          # tangent normals. Bake that convention explicitly at stage 2; the
          # Blender apply boundary performs the documented 1-G display flip.
          normal_g='NEG_Y',
          normal_b='POS_Z',
          use_selected_to_active=True,
        )
      else:
        bpy.ops.object.bake(type='EMIT', use_selected_to_active=True)
    for texture_set, image in image_by_set.items():
      image.save()
      path = output_paths[texture_set]
      if not path.is_file():
        raise RuntimeError(f'{role} bake was not written: {path}')
    return output_paths, image_by_set
  finally:
    _remove_target_nodes(target_nodes)


def _split_pixels_fallback(source_image, output_image, channel):
  rgba = array('f', [0.0]) * (len(source_image.pixels))
  source_image.pixels.foreach_get(rgba)
  split = array('f', [0.0]) * len(rgba)
  for index in range(0, len(rgba), 4):
    value = rgba[index + channel]
    split[index] = value
    split[index + 1] = value
    split[index + 2] = value
    split[index + 3] = 1.0
  output_image.pixels.foreach_set(split)


def _split_pixels(source_image, output_image, channel):
  try:
    import numpy
  except ImportError:
    _split_pixels_fallback(source_image, output_image, channel)
    return
  rgba = numpy.empty(len(source_image.pixels), dtype=numpy.float32)
  source_image.pixels.foreach_get(rgba)
  rgba = rgba.reshape((-1, 4))
  split = numpy.empty_like(rgba)
  split[:, 0] = rgba[:, channel]
  split[:, 1] = rgba[:, channel]
  split[:, 2] = rgba[:, channel]
  split[:, 3] = 1.0
  output_image.pixels.foreach_set(split.ravel())


def _split_extra_images(
  extra_images,
  output_dir,
  resolution,
  temporary_images,
):
  outputs = defaultdict(dict)
  for texture_set, source_image in extra_images.items():
    for role, channel in (('ExtraR', 0), ('Roughness', 1), ('Metallic', 2)):
      path = output_dir / f'{source_map_bake_name(texture_set, role)}.png'
      image = _new_bake_image(
        f'__ST_Meshy_{clean_name(texture_set)}_{role}_{uuid.uuid4().hex}',
        path,
        resolution,
        'Non-Color',
        (0.0, 0.0, 0.0, 1.0),
      )
      temporary_images.append(image)
      _split_pixels(source_image, image, channel)
      image.update()
      image.save()
      if not path.is_file():
        raise RuntimeError(f'{role} split was not written: {path}')
      outputs[texture_set][role] = path
  return dict(outputs)


def _restore_selection(view_layer, selected, active):
  try:
    bpy.ops.object.select_all(action='DESELECT')
  except RuntimeError:
    pass
  available = {obj.as_pointer() for obj in view_layer.objects}
  for obj in selected:
    if obj.as_pointer() in available:
      try:
        obj.select_set(True)
      except RuntimeError:
        pass
  if active is not None and active.as_pointer() in available:
    view_layer.objects.active = active


def _safe_remove_image(image):
  try:
    if image.name in bpy.data.images:
      bpy.data.images.remove(image)
  except (ReferenceError, RuntimeError):
    pass


def _stage2_layout(paths, staged_fbx, baked_outputs):
  staged_sources = {
    f'low/{paths["low_fbx"].name}': staged_fbx['low'],
    f'high/{paths["high_fbx"].name}': staged_fbx['high'],
  }
  destinations = {
    f'low/{paths["low_fbx"].name}': paths['low_fbx'],
    f'high/{paths["high_fbx"].name}': paths['high_fbx'],
  }
  for texture_set, roles in baked_outputs.items():
    for _role, staged_path in roles.items():
      logical = f'texture/{staged_path.name}'
      staged_sources[logical] = staged_path
      destinations[logical] = paths['texture_dir'] / staged_path.name
  return staged_sources, destinations


def _same_file_content(left, right):
  if not left.is_file() or not right.is_file():
    return False
  return file_manifest(left) == file_manifest(right)


def _commit_staged_files(staged_sources, destinations, rollback_dir):
  """Commit the whole working set and roll it back as a group on failure."""
  changes = [
    logical for logical, staged in staged_sources.items()
    if not _same_file_content(staged, destinations[logical])
  ]
  if not changes:
    return {'written': [], 'reused': sorted(staged_sources)}

  backups = {}
  installed = []
  rollback_dir.mkdir(parents=True, exist_ok=True)
  try:
    for logical in changes:
      destination = destinations[logical]
      destination.parent.mkdir(parents=True, exist_ok=True)
      if destination.exists():
        backup = rollback_dir / logical
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup)
        if file_manifest(destination) != file_manifest(backup):
          raise RuntimeError(f'Rollback copy verification failed: {destination}')
        backups[logical] = backup

    for logical in changes:
      staged = staged_sources[logical]
      destination = destinations[logical]
      incoming = destination.with_name(
        f'.{destination.name}.incoming-{uuid.uuid4().hex}'
      )
      shutil.copy2(staged, incoming)
      if file_manifest(staged) != file_manifest(incoming):
        incoming.unlink(missing_ok=True)
        raise RuntimeError(f'Staged copy verification failed: {destination}')
      os.replace(incoming, destination)
      installed.append(logical)
      if file_manifest(staged) != file_manifest(destination):
        raise RuntimeError(f'Committed file verification failed: {destination}')
  except Exception:
    for logical in reversed(installed):
      destination = destinations[logical]
      backup = backups.get(logical)
      if backup is not None and backup.is_file():
        os.replace(backup, destination)
      elif destination.is_file():
        destination.unlink()
    raise
  return {
    'written': sorted(changes),
    'reused': sorted(set(staged_sources) - set(changes)),
  }


_ARCHIVED_ROLE_SUFFIXES = {
  '_Color_baking': 'BaseColor',
  '_Extra_baking': 'Extra',
  '_ExtraR_baking': 'ExtraR',
  '_Roughness_baking': 'Roughness',
  '_Metallic_baking': 'Metallic',
  '_Normal_baking': 'Normal',
}
_PACKAGE_CONTRACT_LOGICAL = 'contract/painter_package.json'


def _png_dimensions(path):
  with Path(path).open('rb') as stream:
    header = stream.read(24)
  if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
    raise RuntimeError(f'Immutable source-map is not a decodable PNG header: {path}')
  width, height = struct.unpack('>II', header[16:24])
  if width <= 0 or height <= 0:
    raise RuntimeError(f'Immutable source-map has invalid dimensions: {path}')
  return width, height


def _restore_stage2_snapshot(
  snapshot_dir,
  base_dir,
  expected_resolution=None,
  expected_manifest_files=None,
  expected_manifest_sha256=None,
  expected_fbx_names=None,
  expected_painter_texture_sets=None,
):
  """Preflight an immutable baseline, then restore/verify its working set."""
  snapshot_dir = Path(snapshot_dir).resolve()
  base_dir = Path(base_dir).resolve()
  manifest = verify_immutable_snapshot_set_archive(snapshot_dir)
  manifest_path = snapshot_dir / MANIFEST_FILENAME
  manifest_sha256 = file_manifest(manifest_path)['sha256']
  if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
    raise RuntimeError('Immutable baseline manifest differs from its scene receipt')
  if expected_manifest_files is not None:
    expected = sorted(expected_manifest_files, key=lambda entry: entry['path'].casefold())
    observed = sorted(manifest['files'], key=lambda entry: entry['path'].casefold())
    if observed != expected:
      raise RuntimeError('Immutable baseline entries differ from their scene receipt')
  archived = {
    entry['path']: snapshot_dir / Path(entry['path'])
    for entry in manifest['files']
  }
  contract_path = archived.get(_PACKAGE_CONTRACT_LOGICAL)
  if contract_path is None:
    raise RuntimeError('Immutable baseline is missing its Painter package contract')
  try:
    package_contract = json.loads(contract_path.read_text(encoding='utf-8'))
  except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise RuntimeError('Immutable Painter package contract is unreadable') from exc
  if (
    not isinstance(package_contract, dict)
    or package_contract.get('contract') != 'meshy-painter-package-v1'
  ):
    raise RuntimeError('Immutable Painter package contract is invalid')
  painter_texture_sets = package_contract.get('painter_texture_sets')
  if (
    not isinstance(painter_texture_sets, list)
    or painter_texture_sets != sorted(set(painter_texture_sets))
    or not painter_texture_sets
  ):
    raise RuntimeError('Immutable Painter Texture Set pin is invalid')
  if (
    expected_painter_texture_sets is not None
    and list(expected_painter_texture_sets) != painter_texture_sets
  ):
    raise RuntimeError(
      'Immutable Painter FBX Texture Sets differ from the current Low'
    )
  destinations = {}
  fbx = {}
  maps = defaultdict(dict)
  observed_resolutions = set()
  for logical, archived_path in archived.items():
    if logical == _PACKAGE_CONTRACT_LOGICAL:
      continue
    relative = Path(logical)
    if len(relative.parts) != 2:
      raise RuntimeError(f'Unexpected immutable baseline member: {logical}')
    destination = (base_dir / relative).resolve()
    try:
      destination.relative_to(base_dir)
    except ValueError as exc:
      raise RuntimeError(f'Immutable baseline path escapes the working root: {logical}') from exc
    if relative.parts[0] in {'low', 'high'} and destination.suffix.casefold() == '.fbx':
      role = relative.parts[0]
      if role in fbx:
        raise RuntimeError(f'Immutable baseline has multiple {role} FBX files')
      if expected_fbx_names and destination.name != expected_fbx_names.get(role):
        raise RuntimeError(f'Immutable baseline has an unexpected {role} FBX name')
      fbx[role] = str(destination.resolve())
      destinations[logical] = destination
      continue
    if relative.parts[0] != 'texture' or destination.suffix.casefold() != '.png':
      raise RuntimeError(f'Unexpected immutable baseline member: {logical}')
    stem = destination.stem
    match = next(
      (
        (suffix, role)
        for suffix, role in _ARCHIVED_ROLE_SUFFIXES.items()
        if stem.endswith(suffix)
      ),
      None,
    )
    if match is None or not stem.startswith('T_'):
      raise RuntimeError(f'Unrecognized source-map baseline name: {logical}')
    suffix, role = match
    texture_set = stem[2:-len(suffix)]
    if role in maps[texture_set]:
      raise RuntimeError(f'Duplicate {texture_set}/{role} immutable source map')
    width, height = _png_dimensions(archived_path)
    if width != height:
      raise RuntimeError(f'Immutable source-map is not square: {logical}')
    observed_resolutions.add(width)
    maps[texture_set][role] = str(destination.resolve())
    destinations[logical] = destination
  if set(fbx) != {'low', 'high'} or not maps:
    raise RuntimeError('Immutable baseline is missing its FBX or source-map package')
  if len(observed_resolutions) != 1:
    raise RuntimeError('Immutable baseline source maps do not share one resolution')
  actual_resolution = next(iter(observed_resolutions))
  if int(package_contract.get('resolution', 0)) != actual_resolution:
    raise RuntimeError('Immutable Painter package resolution pin is inconsistent')
  if expected_resolution is not None and actual_resolution != int(expected_resolution):
    raise RuntimeError(
      f'Immutable baseline is {actual_resolution}px, not recorded {expected_resolution}px'
    )
  for texture_set, roles in maps.items():
    split_roles = {'ExtraR', 'Roughness', 'Metallic'}
    if ('Extra' in roles) != split_roles.issubset(roles):
      raise RuntimeError(
        f'Immutable baseline has an incomplete packed Extra group for {texture_set}'
      )
  if not set(maps).issubset(set(painter_texture_sets)):
    raise RuntimeError('Source-map Texture Sets are outside the Painter FBX pin')
  if package_contract.get('source_map_texture_sets') != sorted(maps):
    raise RuntimeError('Immutable source-map Texture Set pin is inconsistent')
  if (
    package_contract.get('normal_convention') != 'DIRECTX'
    or package_contract.get('normal_basis') != 'LOW_TANGENT'
  ):
    raise RuntimeError('Immutable Painter Normal convention pin is inconsistent')
  expected_map_roles = package_contract.get('map_roles')
  observed_map_roles = {
    texture_set: sorted(roles)
    for texture_set, roles in sorted(maps.items())
  }
  if expected_map_roles != observed_map_roles:
    raise RuntimeError('Immutable Painter package role pin is inconsistent')
  expected_fbx_contract = package_contract.get('fbx') or {}
  if any(
    Path(fbx[role]).name != expected_fbx_contract.get(role)
    for role in ('low', 'high')
  ):
    raise RuntimeError('Immutable Painter package FBX pin is inconsistent')

  # No mutable file is touched until every member, name, role, and dimension
  # has passed the semantic preflight above.
  with tempfile.TemporaryDirectory(
    prefix='.st_meshy_restore_',
    dir=base_dir,
  ) as raw:
    commit = _commit_staged_files(
      {logical: archived[logical] for logical in destinations},
      destinations,
      Path(raw) / 'rollback',
    )
  return {
    'texture_sets': sorted(maps),
    'painter_texture_sets': list(painter_texture_sets),
    'maps': {key: dict(sorted(value.items())) for key, value in sorted(maps.items())},
    'fbx': fbx,
    'snapshot_dir': str(snapshot_dir),
    'snapshot_manifest': str((snapshot_dir / MANIFEST_FILENAME).resolve()),
    'snapshot_manifest_sha256': manifest_sha256,
    'snapshot_entries': manifest['files'],
    'snapshot_files': len(manifest['files']),
    'written': commit['written'],
    'reused': commit['reused'],
    'normal_convention': 'DIRECTX',
    'normal_basis': 'LOW_TANGENT',
    'extra_channels': {'R': 'ExtraR', 'G': 'Roughness', 'B': 'Metallic'},
    'resolution': actual_resolution,
    'recovered_from_immutable_baseline': True,
  }


def _configure_bake_state(scene, resolution):
  bake = scene.render.bake
  bake_property_names = (
    'use_selected_to_active',
    'use_clear',
    'margin',
    'cage_extrusion',
    'max_ray_distance',
    'target',
    'use_cage',
    'cage_object',
  )
  previous = {
    'engine': scene.render.engine,
    'bake': {
      name: getattr(bake, name)
      for name in bake_property_names
      if hasattr(bake, name)
    },
    'cycles_samples': getattr(getattr(scene, 'cycles', None), 'samples', None),
  }
  scene.render.engine = 'CYCLES'
  bake.use_selected_to_active = True
  bake.use_clear = False
  bake.margin = max(8, min(64, resolution // 128))
  bake.cage_extrusion = 0.0
  bake.max_ray_distance = 0.0
  if hasattr(bake, 'target'):
    bake.target = 'IMAGE_TEXTURES'
  if hasattr(bake, 'use_cage'):
    bake.use_cage = False
  if hasattr(bake, 'cage_object'):
    bake.cage_object = None
  if getattr(scene, 'cycles', None) is not None:
    scene.cycles.samples = 1
  return previous


def _set_pair_cage_extrusion(scene, all_duplicates):
  bounds = [obj.dimensions.length for obj in all_duplicates]
  scene.render.bake.cage_extrusion = max(bounds, default=1.0) * 0.01


def _restore_bake_state(scene, previous):
  scene.render.engine = previous['engine']
  bake = scene.render.bake
  for key, value in previous['bake'].items():
    setattr(bake, key, value)
  if previous['cycles_samples'] is not None and getattr(scene, 'cycles', None) is not None:
    scene.cycles.samples = previous['cycles_samples']


def bake_meshy_source_maps(context, resolution, archive_parent=None):
  """Bake, archive, and stage every detected source map in one transaction."""
  if not bpy.data.filepath:
    raise RuntimeError('Save the .blend file before baking Meshy source maps')
  if context.mode != 'OBJECT':
    raise RuntimeError('Meshy source maps can only be baked in Object Mode')
  resolution = int(resolution)
  if resolution < 16 or resolution > 16384:
    raise RuntimeError(f'Unsupported bake resolution: {resolution}')

  _root, low_collection, high_collection, _alpha = get_baking_collections()
  if low_collection is None or high_collection is None:
    raise RuntimeError("Baking/low and Baking/high collections are required")
  low_objects = painter_collection_meshes(low_collection)
  high_objects = painter_collection_meshes(high_collection)
  if not low_objects or not high_objects:
    raise RuntimeError("Baking/low and Baking/high must both contain mesh objects")

  pair_map = _strict_pair_map(low_objects, high_objects)
  for low, _highs in pair_map.values():
    _validate_low_uv(low)
  bindings, texture_sets = _build_source_bindings(pair_map)
  enabled_by_role = {
    role: _role_presence(bindings, role)
    for role in ('BaseColor', 'Extra', 'Normal')
  }
  if not any(enabled_by_role.values()):
    raise RuntimeError('No BaseColor, Extra, or Normal source image was detected')

  paths = baking_paths()
  base_dir = Path(bpy.path.abspath('//')).resolve()
  selected = list(context.selected_objects)
  active = context.view_layer.objects.active
  previous_bake = None
  temporary_collection = None
  low_duplicates = []
  high_duplicates = []
  temporary_materials = []
  temporary_images = []

  with tempfile.TemporaryDirectory(prefix='.st_meshy_source_maps_', dir=base_dir) as raw:
    staging_root = Path(raw)
    staged_texture_dir = staging_root / 'texture'
    staged_texture_dir.mkdir(parents=True, exist_ok=True)
    staged_fbx = {
      'low': staging_root / 'low' / paths['low_fbx'].name,
      'high': staging_root / 'high' / paths['high_fbx'].name,
    }
    staged_fbx['low'].parent.mkdir(parents=True, exist_ok=True)
    staged_fbx['high'].parent.mkdir(parents=True, exist_ok=True)

    try:
      # MATERIAL_COLOR avoids the legacy FACE_SETS path, which can add an
      # attribute to the source mesh.  Export itself still operates on copies.
      export_objects_to_fbx(
        low_objects,
        staged_fbx['low'],
        id_source='MATERIAL_COLOR',
      )
      export_objects_to_fbx(
        high_objects,
        staged_fbx['high'],
        id_source='MATERIAL_COLOR',
      )

      temporary_collection = bpy.data.collections.new('__ST_MeshySourceMapBake')
      context.scene.collection.children.link(temporary_collection)
      low_duplicates, _low_export_materials, _low_renamed = duplicate_for_export(
        low_objects,
        temporary_collection,
      )
      high_duplicates, _high_export_materials, _high_renamed = duplicate_for_export(
        high_objects,
        temporary_collection,
      )
      # The two calls above cannot create temporary materials without prefix
      # stripping, but keep the assertion explicit if core behavior changes.
      if _low_export_materials or _low_renamed or _high_export_materials or _high_renamed:
        raise RuntimeError('Unexpected material mutation while duplicating bake meshes')

      for duplicate in low_duplicates:
        _validate_low_uv(duplicate)
      low_materials, low_slot_records = _copy_materials_for_duplicates(
        low_objects, low_duplicates
      )
      high_materials, high_slot_records = _copy_materials_for_duplicates(
        high_objects, high_duplicates
      )
      temporary_materials.extend(low_materials)
      temporary_materials.extend(high_materials)

      low_duplicate_by_pointer = {
        source.as_pointer(): duplicate
        for source, duplicate in zip(low_objects, low_duplicates)
      }
      high_duplicate_by_pointer = {
        source.as_pointer(): duplicate
        for source, duplicate in zip(high_objects, high_duplicates)
      }
      pair_duplicates = {
        base: (
          low_duplicate_by_pointer[low.as_pointer()],
          [high_duplicate_by_pointer[high.as_pointer()] for high in highs],
        )
        for base, (low, highs) in pair_map.items()
      }

      previous_bake = _configure_bake_state(context.scene, resolution)
      _set_pair_cage_extrusion(
        context.scene,
        low_duplicates + high_duplicates,
      )

      # Record copied source nodes per temporary High material.  Normal is
      # baked first while the copied original shader graph is still intact.
      copied_bindings = []
      for source, _duplicate, _index, original, copied in high_slot_records:
        binding = bindings[(source.as_pointer(), original.as_pointer())]
        copied_bindings.append({
          'texture_set': binding['texture_set'],
          'original_nodes': binding['nodes'],
          'copied': copied,
        })

      for binding in copied_bindings:
        normal_node = binding['original_nodes'].get('Normal')
        copied_node = _copied_source_node(binding['copied'], normal_node)
        if copied_node is not None and not _normal_is_connected(binding['copied'], copied_node):
          _configure_default_normal_source(binding['copied'], copied_node)

      baked_outputs = defaultdict(dict)
      if enabled_by_role['Normal']:
        output_paths, _normal_images = _run_selected_to_active_pass(
          pair_duplicates,
          low_slot_records,
          'Normal',
          enabled_by_role['Normal'],
          staged_texture_dir,
          resolution,
          temporary_images,
        )
        for texture_set, path in output_paths.items():
          baked_outputs[texture_set]['Normal'] = path

      for role in ('BaseColor', 'Extra'):
        enabled_sets = enabled_by_role[role]
        if not enabled_sets:
          continue
        for binding in copied_bindings:
          original_node = binding['original_nodes'].get(role)
          copied_node = _copied_source_node(binding['copied'], original_node)
          _configure_emission_source(binding['copied'], copied_node)
        output_paths, output_images = _run_selected_to_active_pass(
          pair_duplicates,
          low_slot_records,
          role,
          enabled_sets,
          staged_texture_dir,
          resolution,
          temporary_images,
        )
        output_role = 'BaseColor' if role == 'BaseColor' else 'Extra'
        for texture_set, path in output_paths.items():
          baked_outputs[texture_set][output_role] = path
        if role == 'Extra':
          split_outputs = _split_extra_images(
            output_images,
            staged_texture_dir,
            resolution,
            temporary_images,
          )
          for texture_set, role_paths in split_outputs.items():
            baked_outputs[texture_set].update(role_paths)

      staged_sources, destinations = _stage2_layout(
        paths,
        staged_fbx,
        dict(baked_outputs),
      )
      package_contract_path = staging_root / Path(_PACKAGE_CONTRACT_LOGICAL)
      package_contract_path.parent.mkdir(parents=True, exist_ok=True)
      package_contract_path.write_text(
        json.dumps(
          {
            'contract': 'meshy-painter-package-v1',
            'painter_texture_sets': sorted(texture_sets),
            'source_map_texture_sets': sorted(baked_outputs),
            'map_roles': {
              texture_set: sorted(roles)
              for texture_set, roles in sorted(baked_outputs.items())
            },
            'fbx': {
              'low': paths['low_fbx'].name,
              'high': paths['high_fbx'].name,
            },
            'resolution': resolution,
            'normal_convention': 'DIRECTX',
            'normal_basis': 'LOW_TANGENT',
          },
          ensure_ascii=False,
          sort_keys=True,
          separators=(',', ':'),
        ),
        encoding='utf-8',
      )
      staged_sources[_PACKAGE_CONTRACT_LOGICAL] = package_contract_path
      archive_parent = (
        Path(archive_parent).resolve()
        if archive_parent is not None
        else base_dir / '_painter_archive' / paths['asset']
      )
      archive_parent.mkdir(parents=True, exist_ok=True)
      snapshot_dir = archive_parent / '10_bake_baseline_once'
      manifest = publish_immutable_snapshot_set(staged_sources, snapshot_dir)
      commit = _commit_staged_files(
        {logical: staged_sources[logical] for logical in destinations},
        destinations,
        staging_root / 'rollback',
      )

      return {
        'texture_sets': sorted(baked_outputs),
        'painter_texture_sets': sorted(texture_sets),
        'maps': {
          texture_set: {
            role: str(destinations[f'texture/{path.name}'].resolve())
            for role, path in sorted(roles.items())
          }
          for texture_set, roles in sorted(baked_outputs.items())
        },
        'fbx': {
          'low': str(paths['low_fbx'].resolve()),
          'high': str(paths['high_fbx'].resolve()),
        },
        'snapshot_dir': str(snapshot_dir.resolve()),
        'snapshot_manifest': str((snapshot_dir / MANIFEST_FILENAME).resolve()),
        'snapshot_manifest_sha256': file_manifest(
          snapshot_dir / MANIFEST_FILENAME
        )['sha256'],
        'snapshot_entries': manifest['files'],
        'snapshot_files': len(manifest['files']),
        'written': commit['written'],
        'reused': commit['reused'],
        'normal_convention': 'DIRECTX',
        'normal_basis': 'LOW_TANGENT',
        'extra_channels': {'R': 'ExtraR', 'G': 'Roughness', 'B': 'Metallic'},
        'resolution': resolution,
      }
    finally:
      if previous_bake is not None:
        _restore_bake_state(context.scene, previous_bake)
      _restore_selection(context.view_layer, selected, active)
      for duplicate in low_duplicates + high_duplicates:
        try:
          mesh = duplicate.data
          bpy.data.objects.remove(duplicate, do_unlink=True)
          if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        except (ReferenceError, RuntimeError):
          pass
      for material in temporary_materials:
        try:
          if material.users == 0:
            bpy.data.materials.remove(material)
        except (ReferenceError, RuntimeError):
          pass
      for image in temporary_images:
        _safe_remove_image(image)
      if temporary_collection is not None:
        try:
          bpy.data.collections.remove(temporary_collection)
        except (ReferenceError, RuntimeError):
          pass
      _restore_selection(context.view_layer, selected, active)


class BakeMeshySourceMapsOperator(bpy.types.Operator):
  """Bake detected Color, Extra, and Normal source maps to Low UVs"""

  bl_idname = 'st.bake_meshy_source_maps'
  bl_label = 'Bake Meshy Source Maps'
  bl_options = {'REGISTER'}

  @classmethod
  def poll(cls, context):
    return context.scene is not None and context.mode == 'OBJECT'

  def execute(self, context):
    settings = getattr(context.scene, 'substance_tools_baking', None)
    resolution = int(getattr(settings, 'resolution', 2048))
    try:
      state = load_pipeline_state(context.scene)
      archive_parent = None
      stage2_snapshot = None
      painter_texture_sets = None
      if state:
        stage_index = pipeline_stage_index(state['stage'])
        if stage_index < pipeline_stage_index('UV_READY'):
          raise RuntimeError(
            f'Meshy source-map bake requires UV_READY, not {state["stage"]}'
          )
        adopted_pair = validate_adopted_pair(state)
        painter_texture_sets = sorted(_material_texture_sets(adopted_pair['low']))
        source_archive = (state.get('archive') or {}).get('source_original') or {}
        verify_source_archive_receipt(source_archive)
        source_root_value = source_archive.get('root')
        if not source_root_value:
          raise RuntimeError('Meshy state has no immutable source archive root')
        source_root = Path(source_root_value).resolve()
        if source_root.name != '00_source_original_once':
          raise RuntimeError(
            f'Unexpected immutable source archive directory: {source_root}'
          )
        archive_parent = source_root.parent
        stage2_snapshot = archive_parent / '10_bake_baseline_once'
        if stage_index >= pipeline_stage_index('BAKE_BASELINE_ARCHIVED'):
          baseline = (state.get('archive') or {}).get('bake_baseline') or {}
          snapshot_dir = baseline.get('snapshot_dir')
          if not snapshot_dir:
            raise RuntimeError('Recorded bake baseline has no snapshot directory')
          if Path(snapshot_dir).resolve() != stage2_snapshot.resolve():
            raise RuntimeError('Recorded stage-2 archive is outside the source asset root')
          recorded_resolution = int(baseline.get('resolution') or 0)
          if not recorded_resolution:
            raise RuntimeError('Recorded bake baseline has no source-map resolution')
          if resolution != recorded_resolution:
            raise RuntimeError(
              f'Immutable bake baseline is {recorded_resolution}px; '
              f'current request is {resolution}px and requires a new pipeline version'
            )
          package = state.get('painter_package') or {}
          recorded_sets = package.get('painter_texture_sets')
          if recorded_sets is not None and list(recorded_sets) != painter_texture_sets:
            raise RuntimeError(
              'Recorded Painter FBX Texture Sets differ from the current Low'
            )
          restored = _restore_stage2_snapshot(
            snapshot_dir,
            Path(bpy.path.abspath('//')).resolve(),
            expected_resolution=recorded_resolution,
            expected_manifest_files=baseline.get('snapshot_entries'),
            expected_manifest_sha256=baseline.get('snapshot_manifest_sha256'),
            expected_fbx_names={
              'low': Path(state['painter_package']['fbx']['low']).name,
              'high': Path(state['painter_package']['fbx']['high']).name,
            },
            expected_painter_texture_sets=painter_texture_sets,
          )
          changed = len(restored['written'])
          if recorded_sets is None:
            package['painter_texture_sets'] = painter_texture_sets
            state['painter_package'] = package
            store_pipeline_state(context.scene, state)
          self.report(
            {'WARNING'} if changed else {'INFO'},
            (
              f'Restored {changed} working package file(s) from immutable baseline'
              if changed
              else f'Immutable Meshy bake baseline and working package are valid: {snapshot_dir}'
            ),
          )
          return {'FINISHED'}
      if state and stage2_snapshot.exists():
        # Recovery for a crash/failure after the immutable baseline was
        # published but before its scene receipt was stored.  Never rebake and
        # compare a potentially nondeterministic FBX against that baseline.
        receipt = _restore_stage2_snapshot(
          stage2_snapshot,
          Path(bpy.path.abspath('//')).resolve(),
          expected_resolution=resolution,
          expected_painter_texture_sets=painter_texture_sets,
          expected_fbx_names={
            'low': baking_paths()['low_fbx'].name,
            'high': baking_paths()['high_fbx'].name,
          },
        )
      else:
        receipt = bake_meshy_source_maps(
          context,
          resolution,
          archive_parent=archive_parent,
        )
      if painter_texture_sets is not None:
        receipt['painter_texture_sets'] = painter_texture_sets
    except Exception as error:
      self.report({'ERROR'}, f'Meshy source-map bake failed: {error}')
      traceback.print_exc()
      return {'CANCELLED'}

    context.scene['_substance_tools_meshy_source_maps_receipt'] = json.dumps(
      receipt,
      ensure_ascii=False,
      sort_keys=True,
    )
    map_count = sum(len(roles) for roles in receipt['maps'].values())
    if state:
      state['painter_package'] = {
        'fbx': receipt['fbx'],
        'maps': receipt['maps'],
        'resolution': receipt['resolution'],
        'painter_texture_sets': receipt['painter_texture_sets'],
        'normal_convention': receipt['normal_convention'],
        'normal_basis': receipt['normal_basis'],
      }
      state = advance_pipeline_state(
        state,
        'PAINTER_PACKAGE_READY',
        {
          'texture_sets': receipt['texture_sets'],
          'map_count': map_count,
        },
      )
      state.setdefault('archive', {})['bake_baseline'] = {
        'snapshot_dir': receipt['snapshot_dir'],
        'snapshot_manifest': receipt['snapshot_manifest'],
        'snapshot_manifest_sha256': receipt['snapshot_manifest_sha256'],
        'snapshot_entries': receipt['snapshot_entries'],
        'snapshot_files': receipt['snapshot_files'],
        'resolution': receipt['resolution'],
      }
      state = advance_pipeline_state(
        state,
        'BAKE_BASELINE_ARCHIVED',
        state['archive']['bake_baseline'],
      )
      store_pipeline_state(context.scene, state)
    self.report(
      {'INFO'},
      f"Baked {map_count} source maps for {len(receipt['texture_sets'])} "
      f"Texture Set(s); immutable baseline: {receipt['snapshot_dir']}",
    )
    return {'FINISHED'}


classes = (BakeMeshySourceMapsOperator,)
