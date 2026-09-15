"""Explicit, immutable source-map revisions without repeating geometry stages."""
from pathlib import Path
import copy
import json
import re
from .original_source_revision import original_stage_parent


def revision_parent(source_root, revision_id):
    source_root = Path(source_root).resolve()
    original_parent = original_stage_parent(source_root)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', str(revision_id)):
        raise ValueError('Revision ID must be a safe unique token of 8–80 characters')
    parent = (original_parent / 'source_map_revisions' / revision_id).resolve()
    if not parent.is_relative_to(original_parent):
        raise ValueError('Revision archive escapes the source asset root')
    return parent


def active_archive_parent(state, source_root):
    revision_id = state.get('source_map_revision_id')
    return revision_parent(source_root, revision_id) if revision_id else original_stage_parent(source_root)


def revise_source_maps(context, *, reason, revision_id, resolution=None):
    from . import core, meshy_pipeline as mp, meshy_source_maps as sm
    from .meshy_pipeline_contract import verify_immutable_snapshot_set_archive, file_manifest
    if not str(reason).strip():
        raise ValueError('A source-map revision requires a concrete failure/correction reason')
    state = mp.load_pipeline_state(context.scene)
    if not state or mp.pipeline_stage_index(state['stage']) < mp.pipeline_stage_index('BAKE_BASELINE_ARCHIVED'):
        raise ValueError('A completed source-map baseline is required before revision')
    source_archive = state['archive']['source_original']
    mp.verify_source_archive_receipt(source_archive)
    parent = revision_parent(source_archive['root'], revision_id)
    if state.get('source_map_revision_id') == revision_id:
        verify_immutable_snapshot_set_archive(state['archive']['bake_baseline']['snapshot_dir'])
        return {'reused': True, 'stage': state['stage'], 'revision_id': revision_id}
    pair = mp.validate_adopted_pair(state)
    high = mp.validate_content_signature(pair['high'], context.scene, state['source']['content_signature'],
                                         include_material=False, label='Revision High')
    low = mp.mesh_object_content_signature(pair['low'], context.scene, source_images=[])
    signatures = {name: {k: sig[k] for k in ('geometry_sha256', 'uv_sha256')}
                  for name, sig in [('high', high), ('low', low)]}
    resolve_cage = getattr(core, 'resolve_source_bake_collision', None)
    cage = resolve_cage(pair['low']) if callable(resolve_cage) else None
    projection = {'max_ray_distance': float(pair['low'].get('st_source_bake_max_ray_distance', 0.0))}
    if cage is not None:
        projection['cage'] = cage.name
        projection['cage_geometry'] = mp.mesh_object_content_signature(cage, context.scene, source_images=[])['geometry_sha256']
    baseline = state['archive']['bake_baseline']
    verify_immutable_snapshot_set_archive(baseline['snapshot_dir'])
    if file_manifest(baseline['snapshot_manifest'])['sha256'] != baseline['snapshot_manifest_sha256']:
        raise ValueError('Recorded prior baseline manifest changed')
    resolution = int(resolution or baseline['resolution'])
    request = {'revision_id': revision_id, 'reason': str(reason), 'resolution': resolution,
               'asset_base': state['asset_base'], 'signatures': signatures, 'projection': projection,
               'previous_baseline': baseline}
    request_path = parent / 'revision_request.json'
    previous_path = parent / 'previous_pipeline.json'
    if parent.exists():
        if not request_path.exists() or json.loads(request_path.read_text(encoding='utf-8')) != request:
            raise ValueError('Existing revision has different inputs; inspect it instead of overwriting')
        if not (parent / '10_bake_baseline_once').exists():
            raise ValueError('Previous revision attempt is incomplete; inspect it before another request')
    else:
        parent.mkdir(parents=True, exist_ok=False)
        previous_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    if (parent / '10_bake_baseline_once').exists():
        receipt = sm._restore_stage2_snapshot(parent / '10_bake_baseline_once',
            Path(context.blend_data.filepath).parent, expected_resolution=resolution,
            expected_painter_texture_sets=state['painter_package']['painter_texture_sets'],
            expected_fbx_names={k: Path(v).name for k, v in state['painter_package']['fbx'].items()})
    else:
        receipt = sm.bake_meshy_source_maps(context, resolution, archive_parent=parent)
    for name in ('high', 'low'):
        observed = mp.mesh_object_content_signature(pair[name], context.scene, source_images=[])
        if any(observed[k] != signatures[name][k] for k in signatures[name]):
            raise ValueError('Revision changed geometry/UV: ' + name)
    updated = copy.deepcopy(state)
    updated['source_map_revision_id'] = revision_id
    updated.setdefault('source_map_revision_history', []).append({
        'revision_id': revision_id, 'reason': str(reason), 'previous_state': str(previous_path),
        'previous_baseline': copy.deepcopy(baseline)})
    updated['painter_package'] = {k: receipt[k] for k in
        ('fbx', 'maps', 'resolution', 'painter_texture_sets', 'normal_convention', 'normal_basis')}
    updated['painter_package']['source_projection'] = receipt.get('source_projection', {})
    updated['archive']['bake_baseline'] = {k: receipt[k] for k in
        ('snapshot_dir', 'snapshot_manifest', 'snapshot_manifest_sha256', 'snapshot_entries', 'snapshot_files', 'resolution')}
    # This owner operation explicitly invalidates only the downstream bake/apply
    # checkpoint. The complete prior state and immutable files remain available.
    downstream = set(mp.PIPELINE_STAGES[mp.pipeline_stage_index('PAINTER_PACKAGE_READY'):])
    updated['checkpoints'] = {k: v for k, v in updated.get('checkpoints', {}).items() if k not in downstream}
    updated['stage'] = 'UV_READY'
    updated = mp.advance_pipeline_state(updated, 'PAINTER_PACKAGE_READY',
        {'revision_id': revision_id, 'reason': str(reason), 'texture_sets': receipt['texture_sets']})
    updated = mp.advance_pipeline_state(updated, 'BAKE_BASELINE_ARCHIVED', updated['archive']['bake_baseline'])
    updated.pop('painter', None)
    mp.store_pipeline_state(context.scene, updated)
    context.scene['_substance_tools_meshy_source_maps_receipt'] = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    return {'reused': False, 'stage': updated['stage'], 'revision_id': revision_id,
            'snapshot_dir': receipt['snapshot_dir'], 'previous_state': str(previous_path)}
