"""Resume the owned native canonical transaction from an exact completed export."""
import hashlib,json
from pathlib import Path
import bpy
from . import core, workstation_coordination as workstation
from .meshy_pipeline import load_pipeline_state

def resume_existing_export_apply(*, workstation_phase_id, workstation_apply_phase_id, expected_request_id, expected_resolution):
    if load_pipeline_state(bpy.context.scene):
        raise RuntimeError('Immutable Meshy packages must use their existing strict recovery route')
    paths=core.baking_paths()
    bridge=workstation._helper._bridge()
    store=bridge.Store(bridge.db_path())
    with store._write() as (connection,now):
        row=connection.execute('SELECT * FROM native_handoffs WHERE phase_id=?',(workstation_phase_id,)).fetchone()
        if row is None or row['state']!='completed':
            raise RuntimeError('Exact completed native export receipt required')
        metadata=json.loads(row['metadata'])
    if metadata['request_id']!=str(expected_request_id) or metadata['pipeline']!=workstation._helper.PIPELINE:
        raise RuntimeError('Completed native export identity changed')
    if workstation._helper._path(metadata['target'])!=workstation._helper._path(paths['spp']):
        raise RuntimeError('Completed export belongs to another SPP')
    if any('export_painter_textures_and_apply' in o.bl_idname.lower() for w in bpy.context.window_manager.windows for o in w.modal_operators):
        raise RuntimeError('The original native modal operation is still running')
    result=core.read_json(paths['texture_dir']/core.PAINTER_EXPORT_RESULT,{})
    if result.get('request_id')!=metadata['request_id'] or result.get('status')!='SUCCESS':
        raise RuntimeError('Exact successful staged export required')
    parent={'request_id':metadata['request_id'],'spp':str(paths['spp']),'workstation_phase':metadata}
    followup=workstation.start_followup(parent,workstation_apply_phase_id,target=bpy.data.filepath)
    if not followup.get('started'):
        raise RuntimeError('Blender apply phase is not admitted')
    request={'request_id':metadata['request_id'],'spp':str(paths['spp'])}
    workstation.attach_phase(request,workstation_apply_phase_id,pipeline=workstation.APPLY_PIPELINE,
                             target=bpy.data.filepath,resource=followup['phase']['resource'])
    _,low,_,_=core.ensure_baking_collections(bpy.context.scene)
    objects=core.painter_collection_meshes(low)
    sets=core.low_texture_set_names(objects)
    roles={name:['Color','Extra','Normal'] for name in sets}
    filtered=core.filter_meshy_painter_export_result(result,sets,roles)
    core.validate_meshy_painter_export_group(filtered,objects,expected_resolution=expected_resolution,
                                           canonical_texture_sets=sets,required_roles_by_texture_set=roles)
    if not workstation.can_execute(request):raise RuntimeError('Blender apply admission changed')
    # Legacy presets may include fabric Height/Sheen/Opacity outputs. Validate
    # the required core maps above, then retain the complete native export just
    # like the original modal operator (immutable Meshy is rejected at entry).
    receipt=core.apply_painter_export_transaction(result,objects,paths['texture_dir'])
    bpy.context.scene.substance_tools_baking.base_color_source='PAINTER'
    request.update(status='APPLIED_AWAITING_SAVE',parent_phase_id=workstation_phase_id,
                   applied=receipt['applied'],canonical_files=receipt['canonical_files'],
                   files=[{'path':str(p),'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in receipt['canonical_files']])
    core.write_json(paths['texture_dir']/'.substance_tools_apply_receipt.json',request)
    return request

def complete_existing_export_apply(*, expected_request_id):
    paths=core.baking_paths();path=paths['texture_dir']/'.substance_tools_apply_receipt.json'
    receipt=core.read_json(path,{})
    if receipt.get('request_id')!=str(expected_request_id) or receipt.get('status')!='APPLIED_AWAITING_SAVE':
        raise RuntimeError('Exact applied native receipt required')
    if bpy.data.is_dirty:raise RuntimeError('Save the exact original Blender file before completion')
    if workstation._helper._path(receipt.get('workstation_target',''))!=workstation._helper._path(bpy.data.filepath):
        raise RuntimeError('Saved Blender path changed')
    for item in receipt['files']:
        if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()!=item['sha256']:
            raise RuntimeError('Applied canonical texture bytes changed')
    if not workstation.can_execute(receipt):raise RuntimeError('Apply completion admission changed')
    if not workstation.complete(receipt):raise RuntimeError('Native completion receipt was not acknowledged')
    receipt['status']='SUCCESS';receipt['saved_blend_md5']=hashlib.md5(Path(bpy.data.filepath).read_bytes()).hexdigest()
    core.write_json(path,receipt)
    return receipt
