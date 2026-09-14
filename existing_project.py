"""Queue the current verified Blender UPDATE for Painter's native project handoff."""
from pathlib import Path
from . import core

def queue_existing_painter_project(*, preserve_open_project=False):
    paths = core.baking_paths()
    request = core.read_json(paths['texture_dir'] / core.PAINTER_REQUEST, {})
    if request.get('action') != 'UPDATE' or request.get('status') in {'SUCCESS', 'FAILED'}:
        raise ValueError('A pending native UPDATE request is required')
    if Path(request.get('spp', '')).resolve() != paths['spp'].resolve() or not paths['spp'].is_file():
        raise ValueError('UPDATE target differs from the current Blender project')
    existing = core.read_json(core.pending_request_path(), {})
    if existing and existing.get('request_id') != request.get('request_id'):
        raise ValueError('Another Painter handoff is already pending')
    ticket = dict(request, open_existing_project=True,
                  preserve_open_project=bool(preserve_open_project))
    core.write_json(core.pending_request_path(), ticket)
    return {'request_id': ticket['request_id'], 'spp': str(paths['spp']),
            'preserve_open_project': bool(preserve_open_project)}
