from .core import *


MESHY_SOURCE_MATERIAL_ROLES = ('BaseColor', 'ExtraR', 'Roughness', 'Metallic')
MESHY_SOURCE_PACKAGE_ROLES = set(MESHY_SOURCE_MATERIAL_ROLES) | {'Extra', 'Normal'}


def _texture_set_token(value):
  value = stripped_material_name(str(value or ''))
  return ''.join(character for character in value.casefold() if character.isalnum())


def _normalized_texture_set_entries(entries, label):
  if not isinstance(entries, dict):
    raise RuntimeError(f'{label} must be a texture-set dictionary')
  normalized = {}
  for name, entry in entries.items():
    token = _texture_set_token(name)
    if not token or token in normalized:
      raise RuntimeError(f'{label} contains an ambiguous Texture Set: {name}')
    normalized[token] = (str(name), entry)
  return normalized


def _normalized_texture_set_names(values, label):
  normalized = {}
  for name in values:
    token = _texture_set_token(name)
    if not token or token in normalized:
      raise RuntimeError(f'{label} contains an ambiguous Texture Set: {name}')
    normalized[token] = str(name)
  return normalized


def _source_material_plan_digest(plan):
  digest = hashlib.sha256()
  for role, image_path in sorted(plan.items()):
    digest.update(str(role).encode('utf-8'))
    digest.update(b'\0')
    digest.update(file_hash(Path(image_path)).encode('ascii'))
    digest.update(b'\0')
  return digest.hexdigest()


def _resolved_path_key(path):
  return os.path.normcase(str(Path(path).resolve()))


def verified_meshy_painter_source_plans(scene, texture_sets, texture_dir):
  """Build Meshy Painter source plans only from state plus immutable stage 2.

  Returning ``None`` means this is an ordinary, non-Meshy scene.  Once Meshy
  state exists, no directory scan is allowed: every requested working file must
  be named by ``state.painter_package.maps`` and match the verified archive.
  """
  from .meshy_pipeline import (
    load_pipeline_state,
    pipeline_stage_index,
    required_canonical_roles_from_state,
    verify_source_archive_receipt,
  )
  from .meshy_pipeline_contract import verify_immutable_snapshot_set_archive

  state = load_pipeline_state(scene)
  if not state:
    return None
  if pipeline_stage_index(state['stage']) < pipeline_stage_index(
    'BAKE_BASELINE_ARCHIVED'
  ):
    raise RuntimeError(
      f'Meshy Painter source package requires BAKE_BASELINE_ARCHIVED, '
      f'not {state["stage"]}'
    )
  verify_source_archive_receipt(
    (state.get('archive') or {}).get('source_original') or {}
  )
  baseline = (state.get('archive') or {}).get('bake_baseline') or {}
  snapshot_dir_value = baseline.get('snapshot_dir')
  if not snapshot_dir_value:
    raise RuntimeError('Meshy state has no stage-2 snapshot directory')
  snapshot_dir = Path(snapshot_dir_value).resolve()
  manifest = verify_immutable_snapshot_set_archive(snapshot_dir)
  recorded_manifest = baseline.get('snapshot_manifest')
  expected_manifest = snapshot_dir / 'manifest.json'
  if not recorded_manifest or Path(recorded_manifest).resolve() != expected_manifest.resolve():
    raise RuntimeError('Meshy stage-2 manifest path differs from the snapshot directory')
  if baseline.get('snapshot_manifest_sha256') != file_hash(expected_manifest):
    raise RuntimeError('Meshy stage-2 manifest SHA-256 pin differs from disk')
  pinned_entries = manifest['files']
  if baseline.get('snapshot_entries') != pinned_entries:
    raise RuntimeError('Meshy stage-2 entry pins differ from the verified manifest')
  if int(baseline.get('snapshot_files', -1)) != len(pinned_entries):
    raise RuntimeError('Meshy stage-2 snapshot file count differs from the manifest')
  manifest_entries = {entry['path']: entry for entry in manifest['files']}

  package = state.get('painter_package') or {}
  resolution = int(scene.substance_tools_baking.resolution)
  if (
    int(baseline.get('resolution', 0)) != resolution
    or int(package.get('resolution', 0)) != resolution
  ):
    raise RuntimeError('Meshy stage-2 resolution pin differs from Blender settings')
  package_maps = package.get('maps')
  expected_sets = {str(name) for name in texture_sets}
  validate_exact_texture_set_ids(
    expected_sets,
    'Current low Texture Set IDs',
  )
  _normalized_texture_set_entries(
    {name: True for name in expected_sets},
    'Current low Texture Set IDs',
  )
  pinned_texture_sets = package.get('painter_texture_sets')
  if (
    not isinstance(pinned_texture_sets, list)
    or not pinned_texture_sets
    or any(not isinstance(name, str) for name in pinned_texture_sets)
    or pinned_texture_sets != sorted(set(pinned_texture_sets))
  ):
    raise RuntimeError(
      'Meshy state has no unique stage-2 Painter Texture Set pin'
    )
  pinned_texture_sets = {str(name) for name in pinned_texture_sets}
  validate_exact_texture_set_ids(
    pinned_texture_sets,
    'Stage-2 Painter Texture Set IDs',
  )
  _normalized_texture_set_entries(
    {name: True for name in pinned_texture_sets},
    'Stage-2 Painter Texture Set IDs',
  )
  if pinned_texture_sets != expected_sets:
    raise RuntimeError(
      'Current low Texture Sets differ from the stage-2 pin: '
      f'current={sorted(expected_sets)}, stage2={sorted(pinned_texture_sets)}'
    )
  if isinstance(package_maps, dict):
    validate_exact_texture_set_ids(
      package_maps,
      'State-pinned Painter Texture Set IDs',
    )
    _normalized_texture_set_entries(
      package_maps,
      'State-pinned Painter Texture Set IDs',
    )
  if not isinstance(package_maps, dict) or not set(package_maps).issubset(
    pinned_texture_sets
  ):
    raise RuntimeError(
      'Meshy state Painter map sets are not a subset of the stage-2 Texture Sets: '
      f'maps={sorted(package_maps or {})}, stage2={sorted(pinned_texture_sets)}'
    )

  texture_dir = Path(texture_dir).resolve()
  package_fbx = package.get('fbx')
  if not isinstance(package_fbx, dict) or set(package_fbx) != {'low', 'high'}:
    raise RuntimeError('Meshy state Painter package must contain low/high FBX')
  verified_fbx = {}
  for role in ('low', 'high'):
    raw_path = Path(package_fbx[role])
    if not raw_path.is_absolute() or raw_path.is_symlink():
      raise RuntimeError(f'Meshy Painter {role} FBX must be an absolute regular file')
    path = raw_path.resolve()
    expected_parent = texture_dir.parent / role
    if path.parent != expected_parent or not path.is_file():
      raise RuntimeError(f'Meshy Painter {role} FBX is outside its package: {path}')
    logical = f'{role}/{path.name}'
    entry = manifest_entries.get(logical)
    if entry is None:
      raise RuntimeError(f'Meshy Painter {role} FBX is not in stage 2: {logical}')
    observed = {'size': path.stat().st_size, 'sha256': file_hash(path)}
    if observed != entry['backup']:
      raise RuntimeError(f'Meshy Painter {role} FBX differs from stage 2: {path}')
    verified_fbx[role] = str(path)

  material_maps = {}
  normal_maps = {}
  represented_texture_entries = set()
  for texture_set in sorted(package_maps):
    roles = package_maps.get(texture_set)
    if not isinstance(roles, dict) or not roles:
      raise RuntimeError(f'Meshy Painter map plan is invalid: {texture_set}')
    observed_roles = set(roles)
    if not observed_roles.issubset(MESHY_SOURCE_PACKAGE_ROLES):
      raise RuntimeError(
        f'Meshy Painter map roles differ from the exact contract for '
        f'{texture_set}: {sorted(observed_roles)}'
      )
    extra_transport = {'Extra', 'ExtraR', 'Roughness', 'Metallic'}
    observed_extra = observed_roles & extra_transport
    if observed_extra and observed_extra != extra_transport:
      raise RuntimeError(
        f'Meshy Extra transport must be complete or absent for {texture_set}: '
        f'{sorted(observed_extra)}'
      )

    verified_roles = {}
    for role, path_value in roles.items():
      raw_path = Path(path_value)
      if not raw_path.is_absolute() or raw_path.is_symlink():
        raise RuntimeError(
          f'Meshy Painter map must be an absolute regular file: {path_value}'
        )
      path = raw_path.resolve()
      if path.parent != texture_dir or not path.is_file():
        raise RuntimeError(
          f'Meshy Painter map is outside texture_dir or missing: {path}'
        )
      expected_name = f'{source_map_bake_name(texture_set, role)}.png'
      if path.name != expected_name:
        raise RuntimeError(
          f'Meshy Painter map role/name mismatch: {role} -> {path.name}'
        )
      logical = f'texture/{path.name}'
      represented_texture_entries.add(logical)
      entry = manifest_entries.get(logical)
      if entry is None:
        raise RuntimeError(
          f'Meshy Painter map is not in the stage-2 manifest: {logical}'
        )
      observed = {'size': path.stat().st_size, 'sha256': file_hash(path)}
      if observed != entry['backup']:
        raise RuntimeError(
          f'Meshy Painter working map differs from stage-2: {path}'
        )
      verified_roles[role] = str(path)

    material_plan = {
      role: verified_roles[role]
      for role in MESHY_SOURCE_MATERIAL_ROLES
      if role in verified_roles
    }
    if material_plan:
      material_maps[texture_set] = material_plan
    if 'Normal' in verified_roles:
      normal_maps[texture_set] = {
        'source_normal_texture': verified_roles['Normal'],
        'normal_convention': package.get('normal_convention', 'OPENGL'),
        'basis': package.get('normal_basis', 'LOW_TANGENT'),
      }

  manifest_texture_entries = {
    logical for logical in manifest_entries
    if Path(logical).parts and Path(logical).parts[0] == 'texture'
  }
  if represented_texture_entries != manifest_texture_entries:
    raise RuntimeError(
      'Meshy state Painter maps differ from stage-2 texture membership: '
      f'state={sorted(represented_texture_entries)}, '
      f'archive={sorted(manifest_texture_entries)}'
    )

  contract_logical = 'contract/painter_package.json'
  expected_manifest_entries = {
    contract_logical,
    f'low/{Path(verified_fbx["low"]).name}',
    f'high/{Path(verified_fbx["high"]).name}',
    *represented_texture_entries,
  }
  if set(manifest_entries) != expected_manifest_entries:
    raise RuntimeError(
      'Meshy stage-2 membership differs from the exact Painter package contract'
    )
  contract_path = snapshot_dir / Path(contract_logical)
  try:
    package_contract = json.loads(contract_path.read_text(encoding='utf-8'))
  except (OSError, UnicodeError, ValueError) as error:
    raise RuntimeError('Meshy archived Painter package contract is unreadable') from error
  expected_map_roles = {
    texture_set: sorted(roles)
    for texture_set, roles in sorted(package_maps.items())
  }
  canonical_output_roles = {
    texture_set: sorted(roles)
    for texture_set, roles in sorted(
      required_canonical_roles_from_state(state).items()
    )
  }
  expected_contract = {
    'contract': 'meshy-painter-package-v1',
    'painter_texture_sets': sorted(pinned_texture_sets),
    'source_map_texture_sets': sorted(package_maps),
    'map_roles': expected_map_roles,
    'fbx': {
      'low': Path(verified_fbx['low']).name,
      'high': Path(verified_fbx['high']).name,
    },
    'resolution': resolution,
    'normal_convention': package.get('normal_convention'),
    'normal_basis': package.get('normal_basis'),
  }
  if package_contract != expected_contract:
    raise RuntimeError(
      'Meshy archived Painter package contract differs from scene state'
    )

  return {
    'state': state,
    'canonical_texture_sets': sorted(expected_sets),
    'source_material_maps': material_maps,
    'source_normal_mesh_maps': normal_maps,
    'canonical_output_roles': canonical_output_roles,
    'fbx': verified_fbx,
    'snapshot_dir': str(snapshot_dir),
    'resolution': resolution,
  }


def validate_meshy_painter_source_receipts(
  request,
  expected_material_maps,
  expected_normal_maps,
  canonical_texture_sets=None,
  expected_resolution=None,
):
  """Validate Painter's per-Texture-Set source results, never aggregate guesses."""
  if request.get('meshy_contract_version') != 1:
    raise RuntimeError('Painter response has no Meshy contract version 1')
  if request.get('strict_bake_settings') is not True:
    raise RuntimeError('Painter response did not preserve strict bake settings')

  requested_material = request.get('source_material_maps') or {}
  requested_normal = request.get('source_normal_mesh_maps') or {}
  if requested_material != expected_material_maps:
    raise RuntimeError('Painter source material request differs from verified stage 2')
  if requested_normal != expected_normal_maps:
    raise RuntimeError('Painter source Normal request differs from verified stage 2')
  if request.get('source_material_hashes') != hash_nested_existing_paths(
    expected_material_maps
  ):
    raise RuntimeError('Painter source material hashes differ from verified stage 2')
  if request.get('source_normal_mesh_hashes') != hash_nested_existing_paths(
    expected_normal_maps
  ):
    raise RuntimeError('Painter source Normal hashes differ from verified stage 2')

  canonical_texture_sets = set(canonical_texture_sets or ()) or (
    set(expected_material_maps) | set(expected_normal_maps)
  )
  validate_exact_texture_set_ids(
    canonical_texture_sets,
    'Expected Meshy Painter Texture Set IDs',
  )
  expected_bake_entries = _normalized_texture_set_entries(
    {name: True for name in canonical_texture_sets},
    'Expected Meshy Painter Texture Sets',
  )
  expected_settings = {
    'antialiasing': 'X2',
    'match': 'BY_MESH_NAME',
    'id_source': 'MATERIAL_COLOR',
  }
  settings_result = request.get('bake_settings_result') or {}
  if settings_result.get('contract') != 'meshy-bake-settings-v1':
    raise RuntimeError('Painter bake-settings receipt has the wrong contract')
  if settings_result.get('strict') is not True:
    raise RuntimeError('Painter bake-settings receipt is not strict')
  if settings_result.get('exact') is not True:
    raise RuntimeError('Painter bake-settings receipt is not exact')
  if settings_result.get('requested') != expected_settings:
    raise RuntimeError('Painter bake-settings request labels are not canonical')
  actual_bake_entries = _normalized_texture_set_entries(
    settings_result.get('texture_sets') or {},
    'Painter bake-settings result',
  )
  if set(actual_bake_entries) != set(expected_bake_entries):
    raise RuntimeError(
      'Painter configured bake-settings Texture Sets differ from the Meshy package'
    )
  if int(settings_result.get('configured_texture_set_count', -1)) != len(
    expected_bake_entries
  ):
    raise RuntimeError('Painter configured Texture Set count is inconsistent')
  for token, (expected_name, _unused) in expected_bake_entries.items():
    actual_name, entry = actual_bake_entries[token]
    if not isinstance(entry, dict):
      raise RuntimeError(f'Painter bake-settings result is invalid: {actual_name}')
    if entry.get('configured') is not True:
      raise RuntimeError(f'Painter did not configure bake settings for {expected_name}')
    if entry.get('set_call_succeeded') is not True:
      raise RuntimeError(f'Painter bake-settings set call failed for {expected_name}')
    observed = {key: entry.get(key) for key in expected_settings}
    if observed != expected_settings:
      raise RuntimeError(
        f'Painter bake-settings labels differ for {expected_name}: {observed}'
      )
    if expected_resolution is not None and int(entry.get('resolution', 0)) != int(
      expected_resolution
    ):
      raise RuntimeError(
        f'Painter bake-settings resolution differs for {expected_name}'
      )

  expected_entries = _normalized_texture_set_entries(
    expected_material_maps,
    'expected Meshy source maps',
  )
  layer_result = request.get('source_layer_result') or {}
  actual_entries = _normalized_texture_set_entries(
    layer_result.get('texture_sets') or {},
    'Painter source layer result',
  )
  if set(actual_entries) != set(expected_entries):
    raise RuntimeError(
      'Painter managed source layer Texture Sets differ from Meshy stage 2'
  )
  total_layers = 0
  for token, (expected_name, expected_plan) in expected_entries.items():
    actual_name, entry = actual_entries[token]
    if not isinstance(entry, dict):
      raise RuntimeError(f'Painter source layer result is invalid: {actual_name}')
    expected_channels = set(expected_plan)
    if set(entry.get('channels') or ()) != expected_channels:
      raise RuntimeError(
        f'Painter source channels differ for {expected_name}: '
        f'{entry.get("channels")}'
      )
    expected_digest = _source_material_plan_digest(expected_plan)
    if entry.get('digest') != expected_digest:
      raise RuntimeError(
        f'Painter source layer digest differs for {expected_name}'
      )
    layers = int(entry.get('layers', 0))
    if layers <= 0:
      raise RuntimeError(f'Painter created no managed layer for {expected_name}')
    total_layers += layers
  if int(layer_result.get('managed_layer_count', -1)) != total_layers:
    raise RuntimeError('Painter managed layer total differs from per-set results')

  expected_normal_tokens = set(_normalized_texture_set_entries(
    expected_normal_maps,
    'expected Meshy source Normals',
  ))
  normal_result = request.get('source_normal_mesh_map_result') or {}
  assigned = _normalized_texture_set_names(
    normal_result.get('assigned_texture_sets') or (),
    'Painter assigned source Normals',
  )
  omitted = _normalized_texture_set_names(
    normal_result.get('normal_baker_omitted_texture_sets') or (),
    'Painter Normal-baker omissions',
  )
  if set(assigned) != expected_normal_tokens:
    raise RuntimeError('Painter assigned source Normal Texture Sets differ from stage 2')
  if set(omitted) != expected_normal_tokens:
    raise RuntimeError('Painter Normal-baker omissions differ from source Normals')
  if int(normal_result.get('assigned_count', -1)) != len(assigned):
    raise RuntimeError('Painter source Normal assigned count is inconsistent')
  if int(normal_result.get('normal_baker_omitted_count', -1)) != len(omitted):
    raise RuntimeError('Painter Normal-baker omitted count is inconsistent')
  assignment_entries = _normalized_texture_set_entries(
    normal_result.get('assignments') or {},
    'Painter source Normal assignments',
  )
  if set(assignment_entries) != expected_normal_tokens:
    raise RuntimeError('Painter source Normal assignment receipts differ from stage 2')
  expected_normal_entries = _normalized_texture_set_entries(
    expected_normal_maps,
    'Expected Meshy source Normals',
  )
  for token, (expected_name, expected_plan) in expected_normal_entries.items():
    actual_name, assignment = assignment_entries[token]
    if not isinstance(assignment, dict):
      raise RuntimeError(f'Painter source Normal assignment is invalid: {actual_name}')
    source_path = Path(expected_plan.get('source_normal_texture', ''))
    expected_sha256 = file_hash(source_path)
    resource_token = ''.join(
      character if character.isalnum() else '_'
      for character in actual_name
    ).strip('_')
    expected_resource_name = (
      f'ST_{resource_token}_SourceNormal_{expected_sha256[:12]}'
    )
    if assignment.get('source_sha256') != expected_sha256:
      raise RuntimeError(
        f'Painter source Normal digest differs for {expected_name}'
      )
    if assignment.get('resource_name') != expected_resource_name:
      raise RuntimeError(
        f'Painter source Normal resource name differs for {expected_name}'
      )
    resource_identity = assignment.get('resource_identity')
    if not isinstance(resource_identity, str) or (
      expected_resource_name not in resource_identity
    ):
      raise RuntimeError(
        f'Painter source Normal resource identity differs for {expected_name}'
      )
  return {
    'texture_sets': sorted(name for name, _entry in expected_entries.values()),
    'managed_layer_count': total_layers,
    'source_normal_texture_sets': sorted(expected_normal_maps),
  }


def meshy_expected_source_state(source_contract):
  canonical_sets = list(source_contract['canonical_texture_sets'])
  validate_exact_texture_set_ids(
    canonical_sets,
    'Meshy export source-state Texture Set IDs',
  )
  material = {
    texture_set: {
      'channels': sorted(plan),
      'digest': _source_material_plan_digest(plan),
    }
    for texture_set, plan in sorted(
      source_contract['source_material_maps'].items()
    )
  }
  normal = {}
  for texture_set, plan in sorted(
    source_contract['source_normal_mesh_maps'].items()
  ):
    source_sha256 = file_hash(Path(plan['source_normal_texture']))
    resource_token = ''.join(
      character if character.isalnum() else '_'
      for character in f'{MATERIAL_PREFIX}{texture_set}'
    ).strip('_')
    normal[texture_set] = {
      'source_sha256': source_sha256,
      'resource_name': (
        f'ST_{resource_token}_SourceNormal_{source_sha256[:12]}'
      ),
    }
  return {
    'contract': 'meshy-source-state-v1',
    'canonical_texture_sets': sorted(canonical_sets),
    'material': material,
    'normal': normal,
  }


def validate_meshy_export_source_state_receipt(result, expected):
  receipt = result.get('source_state_receipt')
  exact_expected = dict(expected)
  exact_expected['exact'] = True
  if receipt != exact_expected:
    raise RuntimeError(
      'Painter export-time source layer/Normal state differs from the '
      'state-pinned Meshy source contract'
    )
  return receipt


def validate_meshy_bake_request_settings(settings):
  expected = {
    'antialiasing': 'X2',
    'match': 'BY_MESH_NAME',
    'id_source': 'MATERIAL_COLOR',
  }
  observed = {key: settings.get(key) for key in expected}
  if observed != expected:
    raise RuntimeError(
      f'Meshy strict Painter settings differ from the contract: {observed}'
    )


def validate_meshy_export_apply_entry(
  scene,
  low_objects,
  texture_dir,
  export_result_path=None,
):
  """Return a mutation decision for the Meshy Export-and-Apply button.

  Terminal pipeline stages are immutable.  They may only produce a read-only
  no-op after the canonical file hashes and exact managed material graph pass.
  """
  from .meshy_pipeline import load_pipeline_state

  state = load_pipeline_state(scene)
  if not state:
    return {'action': 'PROCEED', 'state': {}}
  stage = state.get('stage')
  mutable_stages = {
    'BAKE_BASELINE_ARCHIVED',
    'SOURCE_LAYER_READY',
    'EXPORT_STAGED',
  }
  if stage in mutable_stages:
    return {'action': 'PROCEED', 'state': state}
  if stage not in {'CANONICAL_APPLIED', 'VERIFIED'}:
    raise RuntimeError(
      'Meshy Painter export requires BAKE_BASELINE_ARCHIVED through '
      f'EXPORT_STAGED, not {stage}'
    )

  texture_dir = Path(texture_dir).resolve()
  source_contract = verified_meshy_painter_source_plans(
    scene,
    low_texture_set_names(low_objects),
    texture_dir,
  )
  canonical_sets = source_contract['canonical_texture_sets']
  canonical_output_roles = source_contract['canonical_output_roles']
  expected_paths = {
    _resolved_path_key(
      texture_dir / f'{TEXTURE_PREFIX}{texture_set}_{role}.png'
    ): (texture_set, role)
    for texture_set in canonical_sets
    for role in canonical_output_roles[texture_set]
  }
  checkpoint = (state.get('checkpoints') or {}).get('CANONICAL_APPLIED') or {}
  recorded_texture_dir = checkpoint.get('texture_dir')
  if not recorded_texture_dir or Path(recorded_texture_dir).resolve() != texture_dir:
    raise RuntimeError('Meshy canonical texture directory differs from its receipt')
  entries = checkpoint.get('canonical_files') or []
  recorded = {}
  for entry in entries:
    if not isinstance(entry, dict) or not entry.get('path'):
      raise RuntimeError('Meshy canonical file receipt is invalid')
    path = Path(entry['path']).resolve()
    key = _resolved_path_key(path)
    if path.parent != texture_dir or key in recorded:
      raise RuntimeError('Meshy canonical file receipt has an outside or duplicate path')
    recorded[key] = entry
  if set(recorded) != set(expected_paths):
    raise RuntimeError(
      'Meshy canonical file receipt differs from the source-backed map set'
    )
  for key, entry in recorded.items():
    path = Path(entry['path']).resolve()
    if not path.is_file() or path.stat().st_size != int(entry.get('size', -1)) or (
      file_hash(path) != entry.get('sha256')
    ):
      raise RuntimeError(f'Meshy canonical texture changed after apply: {path}')

  managed_roles = verify_painter_material_roles(
    low_objects,
    texture_dir,
    canonical_output_roles,
  )
  if checkpoint.get('managed_roles') != managed_roles:
    raise RuntimeError('Meshy managed material roles differ from the apply receipt')
  if int(checkpoint.get('applied_materials', -1)) != len(managed_roles):
    raise RuntimeError('Meshy applied material count differs from the apply receipt')

  if export_result_path and Path(export_result_path).is_file():
    result = read_json(export_result_path, {})
    if result.get('status') == 'SUCCESS':
      filtered = filter_meshy_painter_export_result(
        result,
        canonical_sets,
        canonical_output_roles,
      )
      replacements = painter_export_canonical_replacements(filtered)
      if replacements:
        staging = {}
        for source, target in replacements:
          target_key = _resolved_path_key(target)
          if source == target or source.parent != texture_dir or target_key in staging:
            raise RuntimeError('Meshy terminal stage has invalid new Painter staging')
          staging[target_key] = source
        if set(staging) != set(expected_paths):
          raise RuntimeError('Meshy terminal stage has an incomplete new Painter export')
        changed = [
          str(source)
          for key, source in staging.items()
          if file_hash(source) != recorded[key].get('sha256')
        ]
        if changed:
          raise RuntimeError(
            'Meshy terminal stage has a changed Painter export; start a new '
            f'pipeline revision first: {changed}'
          )
  return {
    'action': 'NOOP',
    'state': state,
    'canonical_files': len(recorded),
    'managed_materials': len(managed_roles),
  }


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
      obj for obj in painter_collection_meshes(low_collection) if obj in selected
    ]
    high_selected = [
      obj for obj in painter_collection_meshes(high_collection) if obj in selected
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
        launch_painter(painter_path, paths['spp'])
      except Exception as error:
        self.report({'ERROR'}, f'Could not open Painter project: {error}')
        return {'CANCELLED'}
      self.report({'INFO'}, f'Opening Painter project: {paths["spp"].name}')
      return {'FINISHED'}

    _, low_collection, high_collection, alpha_collection = ensure_baking_collections(
      context.scene
    )
    low_objects = painter_collection_meshes(low_collection)
    alpha_objects = painter_collection_meshes(alpha_collection)
    alpha_ids = {obj.as_pointer() for obj in alpha_objects}
    high_objects = [
      obj for obj in painter_collection_meshes(high_collection)
      if obj.as_pointer() not in alpha_ids
    ]
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}

    props = context.scene.substance_tools_baking
    hide_solidify_rim = bool(props.painter_low_hide_solidify_rim)
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

    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable in add-on preferences')
      return {'CANCELLED'}
    template_path = None
    if self.action == 'CREATE':
      template_path = unreal_template_path(painter_path)
      if not template_path.is_file():
        self.report(
          {'ERROR'},
          f'Painter Unreal Engine template was not found: {template_path}',
        )
        return {'CANCELLED'}

    for directory in (paths['low_dir'], paths['high_dir'], paths['texture_dir']):
      directory.mkdir(parents=True, exist_ok=True)

    texture_sets = low_texture_set_names(low_objects)
    try:
      meshy_source_contract = verified_meshy_painter_source_plans(
        context.scene,
        texture_sets,
        paths['texture_dir'],
      )
    except Exception as error:
      self.report({'ERROR'}, f'Meshy Painter source package failed: {error}')
      return {'CANCELLED'}
    settings = {
      'resolution': int(props.resolution),
      'antialiasing': props.antialiasing,
      'match': props.match,
      'cage': 'AUTOMATIC',
      'id_source': props.id_source,
      'painter_low_hide_solidify_rim': hide_solidify_rim,
      'low_as_high_texture_sets': low_as_high_texture_sets,
      'back_normal_mesh_maps': (
        {} if meshy_source_contract else back_normal_mesh_map_plan(
          texture_sets,
          paths['texture_dir'],
        )
      ),
      'mesh_maps': [
        'Normal', 'WorldSpaceNormal', 'ID', 'AO',
        'Curvature', 'Position', 'Thickness',
      ],
    }
    if meshy_source_contract:
      try:
        validate_meshy_bake_request_settings(settings)
      except Exception as error:
        self.report({'ERROR'}, str(error))
        return {'CANCELLED'}
    settings_hash = hashlib.sha256(
      json.dumps(settings, sort_keys=True).encode('utf-8')
    ).hexdigest()

    try:
      previous_request = read_json(paths['texture_dir'] / PAINTER_REQUEST, {})
      previous_low_hashes = previous_request.get('low_hashes', {})
      previous_high_hashes = previous_request.get('high_hashes', {})
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
        planned_texture_sets = list(plan.get('texture_sets', texture_sets))
        if meshy_source_contract:
          if set(planned_texture_sets) != set(texture_sets):
            raise RuntimeError(
              'Checked bake plan Texture Sets differ from Meshy state IDs'
            )
        else:
          texture_sets = planned_texture_sets
      else:
        low_hashes = {}
        changed_low_texture_sets = []
        for texture_set, objects in low_objects_by_texture_set(low_objects).items():
          texture_low_hash = fast_content_hash(
            objects,
            strip_material_prefix=True,
            id_source=props.id_source if not high_objects else 'NONE',
            normalize_solidify_plus_fill_rim=hide_solidify_rim,
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
      if meshy_source_contract:
        package_low_hash = file_hash(Path(meshy_source_contract['fbx']['low']))
        low_hashes = {
          texture_set: package_low_hash for texture_set in texture_sets
        }
        low_hash = hashlib.sha256(
          json.dumps(low_hashes, sort_keys=True).encode('utf-8')
        ).hexdigest()
        changed_low_texture_sets = sorted(
          texture_set for texture_set in texture_sets
          if previous_low_hashes.get(texture_set) != package_low_hash
        )
        low_changed = bool(changed_low_texture_sets)
      high_hash_cache = {}
      if not meshy_source_contract and (
        low_changed or not paths['low_fbx'].is_file()
      ):
        export_objects_to_fbx(
          low_objects,
          paths['low_fbx'],
          strip_material_prefix=False,
          id_source=props.id_source if not high_objects else 'NONE',
          solidify_plus_fill_rim=False if hide_solidify_rim else None,
        )
      high_entries = []
      if meshy_source_contract:
        package_high = meshy_source_contract['fbx']['high']
        package_high_hash = file_hash(Path(package_high))
        high_hashes = {
          texture_set: package_high_hash for texture_set in texture_sets
        }
        changed_high_texture_sets = sorted(
          texture_set for texture_set in texture_sets
          if previous_high_hashes.get(texture_set) != package_high_hash
        )
        low_groups = low_objects_by_texture_set(low_objects)
        for texture_set in texture_sets:
          high_entries.append({
            'texture_set': texture_set,
            'bases': sorted({
              match_base(obj.name, 'low').casefold()
              for obj in low_groups.get(texture_set, ())
            }),
            'fbx': package_high,
            'fbxs': [package_high],
            'hash': package_high_hash,
            'changed': texture_set in changed_high_texture_sets,
            'immutable_stage2': True,
          })
      elif high_objects:
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
      if meshy_source_contract:
        source_material_maps = meshy_source_contract['source_material_maps']
        source_normal_mesh_maps = meshy_source_contract[
          'source_normal_mesh_maps'
        ]
      else:
        source_material_maps = painter_source_map_plan(
          texture_sets,
          paths['texture_dir'],
        )
        source_normal_mesh_maps = painter_source_normal_mesh_map_plan(
          texture_sets,
          paths['texture_dir'],
        )
      # Keep the legacy field while Painter installations migrate to the
      # versioned multi-channel source-material contract.
      base_color_maps = {
        texture_set: roles['BaseColor']
        for texture_set, roles in source_material_maps.items()
        if roles.get('BaseColor')
      }
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
    source_material_hashes = hash_nested_existing_paths(source_material_maps)
    source_normal_mesh_hashes = hash_nested_existing_paths(source_normal_mesh_maps)
    source_material_changed = (
      source_material_hashes
      != previous_request.get('source_material_hashes', {})
    )
    source_normal_mesh_changed = (
      source_normal_mesh_hashes
      != previous_request.get('source_normal_mesh_hashes', {})
    )
    changed_source_normal_texture_sets = sorted(
      texture_set
      for texture_set, role_hashes in source_normal_mesh_hashes.items()
      if role_hashes
      != previous_request.get('source_normal_mesh_hashes', {}).get(texture_set)
    )
    meshy_contract_changed = bool(meshy_source_contract) and (
      previous_request.get('meshy_contract_version') != 1
      or previous_request.get('strict_bake_settings') is not True
    )
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
      rebake_source.extend(changed_source_normal_texture_sets)
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
      and not source_material_changed
      and not source_normal_mesh_changed
      and not meshy_contract_changed
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
      'low_fbx': (
        meshy_source_contract['fbx']['low']
        if meshy_source_contract
        else str(paths['low_fbx'].resolve())
      ),
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
          f"{json.dumps(source_material_hashes, sort_keys=True)}:"
          f"{json.dumps(source_normal_mesh_hashes, sort_keys=True)}:"
          f"{json.dumps(back_normal_hashes, sort_keys=True)}"
        ).encode('utf-8')
      ).hexdigest(),
      'spp_existed': spp_existed,
      'base_color_maps': base_color_maps,
      'base_color_hashes': base_color_hashes,
      'base_color_changed': base_color_changed,
      'source_material_maps': source_material_maps,
      'source_material_hashes': source_material_hashes,
      'source_material_changed': source_material_changed,
      'source_normal_mesh_maps': source_normal_mesh_maps,
      'source_normal_mesh_hashes': source_normal_mesh_hashes,
      'source_normal_mesh_changed': source_normal_mesh_changed,
      'alpha_color_maps': alpha_color_maps,
      'alpha_color_hashes': alpha_color_hashes,
      'alpha_color_changed': alpha_color_changed,
      'back_normal_hashes': back_normal_hashes,
      'back_normal_changed': back_normal_changed,
      'settings': settings,
    }
    if meshy_source_contract:
      request['meshy_contract_version'] = 1
      request['strict_bake_settings'] = True
    # Written to both the texture dir and the low dir because the Painter
    # plugin's _request_candidates() looks next to the open .spp (texture dir)
    # AND next to the last imported mesh (low dir); writing both guarantees a hit.
    request_paths = (
      paths['texture_dir'] / PAINTER_REQUEST,
      paths['low_dir'] / PAINTER_REQUEST,
    )
    for request_path in request_paths:
      write_json(request_path, request)

    try:
      if self.action == 'CREATE':
        request['template'] = str(template_path)
        for request_path in request_paths:
          write_json(request_path, request)
        write_json(pending_request_path(), request)
        # The Painter startup plugin consumes the pending request and creates
        # the project through project.create(template_file_path=...).
        launch_painter(painter_path)
      elif not painter_is_running(painter_path):
        launch_painter(painter_path, paths['spp'])
    except Exception as error:
      for request_path in request_paths:
        request_path.unlink(missing_ok=True)
      if self.action == 'CREATE':
        pending_request_path().unlink(missing_ok=True)
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
    low_objects = painter_collection_meshes(low_collection)
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}

    paths = baking_paths()
    paths['low_dir'].mkdir(parents=True, exist_ok=True)
    props = context.scene.substance_tools_baking
    try:
      export_objects_to_fbx(
        low_objects,
        paths['low_fbx'],
        strip_material_prefix=False,
        solidify_plus_fill_rim=(
          False if props.painter_low_hide_solidify_rim else None
        ),
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
      launch_painter(painter_path, paths['spp'])

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
  low_objects = painter_collection_meshes(low_collection)
  if not low_objects:
    raise RuntimeError("The 'Baking/low' collection has no mesh objects")
  alpha_ids = {
    obj.as_pointer() for obj in painter_collection_meshes(alpha_collection)
  }
  high_objects = [
    obj for obj in painter_collection_meshes(high_collection)
    if obj.as_pointer() not in alpha_ids
  ]

  props = context.scene.substance_tools_baking
  hide_solidify_rim = bool(props.painter_low_hide_solidify_rim)
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

  meshy_source_contract = verified_meshy_painter_source_plans(
    context.scene,
    all_texture_sets,
    paths['texture_dir'],
  )
  settings = {
    'resolution': int(props.resolution),
    'antialiasing': props.antialiasing,
    'match': props.match,
    'cage': 'AUTOMATIC',
    'id_source': props.id_source,
    'painter_low_hide_solidify_rim': hide_solidify_rim,
    'low_as_high_texture_sets': low_as_high_texture_set_names(low_objects, high_objects),
    'back_normal_mesh_maps': (
      {} if meshy_source_contract else back_normal_mesh_map_plan(
        all_texture_sets,
        paths['texture_dir'],
      )
    ),
    'mesh_maps': [
      'Normal', 'WorldSpaceNormal', 'ID', 'AO',
      'Curvature', 'Position', 'Thickness',
    ],
  }
  if meshy_source_contract:
    validate_meshy_bake_request_settings(settings)
  if meshy_source_contract:
    source_material_maps = meshy_source_contract['source_material_maps']
    source_normal_mesh_maps = meshy_source_contract['source_normal_mesh_maps']
  else:
    source_material_maps = painter_source_map_plan(
      all_texture_sets,
      paths['texture_dir'],
    )
    source_normal_mesh_maps = painter_source_normal_mesh_map_plan(
      all_texture_sets,
      paths['texture_dir'],
    )

  high_entries = []
  if meshy_source_contract:
    package_high = meshy_source_contract['fbx']['high']
    low_groups = low_objects_by_texture_set(low_objects)
    for texture_set in bake_texture_sets:
      high_entries.append({
        'texture_set': texture_set,
        'bases': sorted({
          match_base(obj.name, 'low').casefold()
          for obj in low_groups.get(texture_set, ())
        }),
        'fbx': package_high,
        'fbxs': [package_high],
        'immutable_stage2': True,
      })
  else:
    export_objects_to_fbx(
      low_objects,
      paths['low_fbx'],
      strip_material_prefix=False,
      id_source=props.id_source if not high_objects else 'NONE',
      solidify_plus_fill_rim=False if hide_solidify_rim else None,
    )
  if high_objects and not meshy_source_contract:
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
    'low_fbx': (
      meshy_source_contract['fbx']['low']
      if meshy_source_contract
      else str(paths['low_fbx'].resolve())
    ),
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
    'source_material_maps': source_material_maps,
    'source_material_hashes': hash_nested_existing_paths(source_material_maps),
    'source_normal_mesh_maps': source_normal_mesh_maps,
    'source_normal_mesh_hashes': hash_nested_existing_paths(source_normal_mesh_maps),
    'settings': settings,
    'pipeline_hash': f'bake-{time.time_ns()}',
  }
  if meshy_source_contract:
    request['meshy_contract_version'] = 1
    request['strict_bake_settings'] = True
  for request_path in (
    paths['texture_dir'] / PAINTER_REQUEST,
    paths['low_dir'] / PAINTER_REQUEST,
  ):
    write_json(request_path, request)

  if not painter_is_running(painter_path):
    launch_painter(painter_path, paths['spp'])
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
      low_texture_set_names(painter_collection_meshes(low_collection)),
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
      low_texture_set_names(painter_collection_meshes(low_collection)),
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
    low_objects = painter_collection_meshes(low_collection)
    alpha_ids = {
      obj.as_pointer() for obj in painter_collection_meshes(alpha_collection)
    }
    high_objects = [
      obj for obj in painter_collection_meshes(high_collection)
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
    low_objects = painter_collection_meshes(low_collection)
    alpha_objects = painter_collection_meshes(alpha_collection)
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
  """Push baked source material / Alpha Detail maps to Painter as fill layers

  Looks for split Base Color, Extra.R, Roughness, Metallic and Alpha Detail PNGs and
  asks Painter (action=APPLY_MAPS) to (re)apply them as fill layers and save.
  Whatever exists is updated; whatever is missing is skipped. No baking, no reload.
  """
  bl_idname = 'st.send_painter_maps'
  bl_label = 'Send Source Maps & Detail'
  bl_options = {'REGISTER'}

  def execute(self, context):
    if not bpy.data.filepath:
      self.report({'ERROR'}, 'Save the .blend file first')
      return {'CANCELLED'}
    paths = baking_paths()
    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    texture_sets = low_texture_set_names(painter_collection_meshes(low_collection))

    try:
      meshy_source_contract = verified_meshy_painter_source_plans(
        context.scene,
        texture_sets,
        paths['texture_dir'],
      )
    except Exception as error:
      self.report({'ERROR'}, f'Meshy Painter source package failed: {error}')
      return {'CANCELLED'}
    if meshy_source_contract:
      self.report(
        {'INFO'},
        'Meshy source maps are managed automatically by Create/Update Painter '
        '(read-only no-op)',
      )
      return {'FINISHED'}
    if not paths['spp'].is_file():
      self.report({'ERROR'}, 'Painter project does not exist; use Create in Painter first')
      return {'CANCELLED'}
    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable in add-on preferences')
      return {'CANCELLED'}
    source_material_maps = (
      meshy_source_contract['source_material_maps']
      if meshy_source_contract else painter_source_map_plan(
        texture_sets,
        paths['texture_dir'],
      )
    )
    source_normal_mesh_maps = (
      meshy_source_contract['source_normal_mesh_maps']
      if meshy_source_contract else painter_source_normal_mesh_map_plan(
        texture_sets,
        paths['texture_dir'],
      )
    )
    base_color_maps = {
      texture_set: roles['BaseColor']
      for texture_set, roles in source_material_maps.items()
      if roles.get('BaseColor')
    }
    alpha_color_maps = {
      texture_set: str(
        (paths['texture_dir'] / f'{alpha_color_bake_name(texture_set)}.png').resolve()
      )
      for texture_set in texture_sets
      if (paths['texture_dir'] / f'{alpha_color_bake_name(texture_set)}.png').is_file()
    }
    if not source_material_maps and not alpha_color_maps:
      self.report({'INFO'}, 'No baked source material / Alpha maps found to send')
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
      'source_material_maps': source_material_maps,
      'source_material_hashes': hash_nested_existing_paths(source_material_maps),
      'source_normal_mesh_maps': source_normal_mesh_maps,
      'source_normal_mesh_hashes': hash_nested_existing_paths(
        source_normal_mesh_maps
      ),
      'alpha_color_maps': alpha_color_maps,
    }
    for request_path in (
      paths['texture_dir'] / PAINTER_REQUEST,
      paths['low_dir'] / PAINTER_REQUEST,
    ):
      write_json(request_path, request)
    if not painter_is_running(painter_path):
      launch_painter(painter_path, paths['spp'])

    self.report(
      {'INFO'},
      f'Sent {len(source_material_maps)} source material set(s) + '
      f'{len(alpha_color_maps)} Alpha map(s) to Painter',
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
    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    low_objects = painter_collection_meshes(low_collection)
    if not low_objects:
      self.report({'ERROR'}, "The 'Baking/low' collection has no mesh objects")
      return {'CANCELLED'}
    try:
      entry_decision = validate_meshy_export_apply_entry(
        context.scene,
        low_objects,
        paths['texture_dir'],
        paths['texture_dir'] / PAINTER_EXPORT_RESULT,
      )
    except Exception as error:
      self.report({'ERROR'}, f'Meshy Painter export gate failed: {error}')
      return {'CANCELLED'}
    if entry_decision['action'] == 'NOOP':
      self.report(
        {'INFO'},
        'Meshy canonical files and materials still match exactly '
        '(read-only no-op)',
      )
      return {'FINISHED'}
    expected_source_state = None
    if entry_decision['state']:
      try:
        source_contract = verified_meshy_painter_source_plans(
          context.scene,
          low_texture_set_names(low_objects),
          paths['texture_dir'],
        )
        painter_request = read_json(
          paths['texture_dir'] / PAINTER_REQUEST,
          {},
        )
        if painter_request.get('status') != 'SUCCESS':
          raise RuntimeError('Painter source-layer request is not SUCCESS yet')
        validate_meshy_painter_source_receipts(
          painter_request,
          source_contract['source_material_maps'],
          source_contract['source_normal_mesh_maps'],
          source_contract['canonical_texture_sets'],
          source_contract['resolution'],
        )
        expected_source_state = meshy_expected_source_state(source_contract)
      except Exception as error:
        self.report({'ERROR'}, f'Meshy Painter source-state gate failed: {error}')
        return {'CANCELLED'}
    if not paths['spp'].is_file():
      self.report({'ERROR'}, 'Create the Painter project first')
      return {'CANCELLED'}
    painter_path = get_preferences(context)['painter_path']
    if not painter_path or not Path(painter_path).is_file():
      self.report({'ERROR'}, 'Set a valid Substance Painter executable')
      return {'CANCELLED'}
    props = context.scene.substance_tools_baking
    preset_name = painter_export_preset_name(props.painter_export_preset)
    try:
      preset_path = ensure_painter_export_preset(preset_name)
    except Exception as error:
      preset_path = None
      if not painter_inline_export_preset_variants(preset_name):
        self.report({'ERROR'}, f'Painter export preset install failed: {error}')
        return {'CANCELLED'}

    self._request_id = str(time.time_ns())
    request_path = paths['texture_dir'] / PAINTER_EXPORT_REQUEST
    result_path = paths['texture_dir'] / PAINTER_EXPORT_RESULT
    if result_path.is_file():
      result_path.unlink()
    export_request = {
      'request_id': self._request_id,
      'spp': str(paths['spp'].resolve()),
      'texture_dir': str(paths['texture_dir'].resolve()),
      'preset': preset_name,
      'preset_path': str(preset_path.resolve()) if preset_path else '',
      'inline_presets': painter_inline_export_preset_variants(preset_name),
    }
    from .meshy_pipeline import load_pipeline_state
    if load_pipeline_state(context.scene):
      export_request['meshy_contract_version'] = 1
      export_request['strict_bake_settings'] = True
      export_request['expected_source_state'] = expected_source_state
    write_json(request_path, export_request)

    if not painter_is_running(painter_path):
      try:
        launch_painter(painter_path, paths['spp'])
      except Exception as error:
        request_path.unlink(missing_ok=True)
        self.report({'ERROR'}, f'Could not open Substance Painter: {error}')
        return {'CANCELLED'}

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

    _, low_collection, _, _ = ensure_baking_collections(context.scene)
    low_objects = painter_collection_meshes(low_collection)
    meshy_state = None
    apply_result = result
    try:
      from .meshy_pipeline import (
        STATE_PROPERTY,
        advance_pipeline_state,
        load_pipeline_state,
        store_pipeline_state,
        verify_source_archive_receipt,
      )
      from .meshy_pipeline_contract import verify_immutable_snapshot_set_archive

      meshy_state = load_pipeline_state(context.scene)
      if meshy_state:
        if meshy_state['stage'] not in {
          'BAKE_BASELINE_ARCHIVED',
          'SOURCE_LAYER_READY',
          'EXPORT_STAGED',
        }:
          raise RuntimeError(
            'Meshy Painter apply requires BAKE_BASELINE_ARCHIVED through '
            f'EXPORT_STAGED, not {meshy_state["stage"]}'
          )
        verify_source_archive_receipt(
          meshy_state['archive']['source_original']
        )
        baseline = meshy_state['archive'].get('bake_baseline') or {}
        verify_immutable_snapshot_set_archive(baseline.get('snapshot_dir', ''))

        painter_request = read_json(
          baking_paths()['texture_dir'] / PAINTER_REQUEST,
          {},
        )
        if painter_request.get('status') != 'SUCCESS':
          raise RuntimeError('Painter source-layer request is not SUCCESS yet')
        source_contract = verified_meshy_painter_source_plans(
          context.scene,
          low_texture_set_names(low_objects),
          baking_paths()['texture_dir'],
        )
        painter_source_receipt = validate_meshy_painter_source_receipts(
          painter_request,
          source_contract['source_material_maps'],
          source_contract['source_normal_mesh_maps'],
          source_contract['canonical_texture_sets'],
          source_contract['resolution'],
        )
        expected_source_state = meshy_expected_source_state(source_contract)
        source_state_receipt = validate_meshy_export_source_state_receipt(
          result,
          expected_source_state,
        )
        layer_result = painter_request.get('source_layer_result') or {}
        normal_result = painter_request.get('source_normal_mesh_map_result') or {}
        if meshy_state['stage'] == 'BAKE_BASELINE_ARCHIVED':
          meshy_state['painter'] = {
            'source_layer_result': layer_result,
            'source_normal_mesh_map_result': normal_result,
            'verified_source_receipt': painter_source_receipt,
          }
          meshy_state = advance_pipeline_state(
            meshy_state,
            'SOURCE_LAYER_READY',
            meshy_state['painter'],
          )
          store_pipeline_state(context.scene, meshy_state)

        apply_result = filter_meshy_painter_export_result(
          result,
          source_contract['canonical_texture_sets'],
          source_contract['canonical_output_roles'],
        )
        export_receipt = validate_meshy_painter_export_group(
          apply_result,
          low_objects,
          expected_resolution=int(context.scene.substance_tools_baking.resolution),
          canonical_texture_sets=source_contract['canonical_texture_sets'],
          required_roles_by_texture_set=source_contract[
            'canonical_output_roles'
          ],
        )
        export_receipt['source_state_receipt'] = source_state_receipt
        if meshy_state['stage'] == 'SOURCE_LAYER_READY':
          meshy_state = advance_pipeline_state(
            meshy_state,
            'EXPORT_STAGED',
            export_receipt,
          )
          store_pipeline_state(context.scene, meshy_state)
    except Exception as error:
      self.report({'ERROR'}, f'Meshy Painter apply gate failed: {error}')
      return {'CANCELLED'}

    commit_state = None
    if meshy_state:
      previous_meshy_state = meshy_state
      previous_state_raw = context.scene.get(STATE_PROPERTY)

      def restore_previous_state_raw():
        if previous_state_raw is None:
          if STATE_PROPERTY in context.scene:
            del context.scene[STATE_PROPERTY]
        else:
          context.scene[STATE_PROPERTY] = previous_state_raw

      def commit_state(pending_receipt):
        canonical_state = advance_pipeline_state(
          previous_meshy_state,
          'CANONICAL_APPLIED',
          {
            'applied_materials': pending_receipt['applied'],
            'texture_dir': str(baking_paths()['texture_dir']),
            'managed_roles': pending_receipt['managed_roles'],
            'canonical_files': [
              {
                'path': str(Path(path).resolve()),
                'size': Path(path).stat().st_size,
                'sha256': file_hash(Path(path)),
              }
              for path in sorted(set(pending_receipt['canonical_files']))
            ],
          },
        )
        try:
          store_pipeline_state(context.scene, canonical_state)
        except Exception:
          restore_previous_state_raw()
          raise

        def rollback_state():
          restore_previous_state_raw()

        return rollback_state

    try:
      apply_receipt = apply_painter_export_transaction(
        apply_result,
        low_objects,
        baking_paths()['texture_dir'],
        meshy_mode=bool(meshy_state),
        canonical_texture_sets=(
          source_contract['canonical_texture_sets'] if meshy_state else None
        ),
        required_roles_by_texture_set=(
          source_contract['canonical_output_roles'] if meshy_state else None
        ),
        before_commit=commit_state,
      )
    except PainterApplyNoMaterialsError:
      # Apply looks files up as T_<material-without-M_>_<role>.png. If Painter's
      # Texture Set names no longer match the Blender material names (e.g. the
      # material was renamed after the Painter project was created and never
      # reimported), nothing matches. The transaction has already restored every
      # canonical file and kept the Painter staging files for a safe retry.
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
      return {'CANCELLED'} if meshy_state else {'FINISHED'}
    except Exception as error:
      self.report({'ERROR'}, f'Painter canonical apply failed and was rolled back: {error}')
      return {'CANCELLED'}
    applied = apply_receipt['applied']
    canonical_files = apply_receipt['canonical_files']
    context.scene.substance_tools_baking.base_color_source = 'PAINTER'
    self.report(
      {'INFO'},
      f'완료 (done): Painter의 원본 기반 채널을 재질 {applied}개에 적용 '
      f'(exported & applied to {applied} material(s))',
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
    low_objects = painter_collection_meshes(low_collection)
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
