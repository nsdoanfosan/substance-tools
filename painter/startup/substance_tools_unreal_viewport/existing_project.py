"""Bounded existing-project handoff; never discard an open project's changes."""
from pathlib import Path

def handle_existing_project(project, request, log):
    if request.get('action') != 'UPDATE' or not request.get('open_existing_project'):
        return False
    target = Path(request.get('spp', ''))
    if target.suffix.lower() != '.spp' or not target.is_file():
        raise ValueError('Existing-project handoff target is not an existing SPP')
    if project.is_busy():
        return True
    if project.is_open():
        current = project.file_path()
        if current and Path(current).resolve() == target.resolve():
            return True
        if not project.is_in_edition_state():
            return True
        if project.needs_saving():
            if not request.get('preserve_open_project') or not current or not Path(current).is_file():
                log('Existing-project handoff waits for the open project to be saved')
                return True
            project.save()
            log('Saved open Painter project before requested handoff: ' + current)
            return True
        project.close()
        return True
    project.open(str(target))
    log('Opened requested existing Painter project: ' + str(target))
    return True
