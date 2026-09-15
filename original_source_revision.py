"""Separate regenerated originals without replacing earlier immutable archives."""
import re
from pathlib import Path


def original_stage_parent(source_root):
  root = Path(source_root).resolve()
  if root.name == '00_source_original_once':
    return root.parent
  if root.parent.name == '00_source_original_revisions':
    revision = validate_revision_id(root.name)
    return root.parent.parent / 'source_revision_stages' / revision
  raise ValueError('Expected a verified original-source archive directory')


def validate_revision_id(value):
  if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
    raise ValueError('Source revision ID must be a short path-safe identifier')
  return value


def configure_original_source_revision(scene, *, revision_id, reason):
  from .meshy_pipeline import load_pipeline_state
  revision_id = validate_revision_id(revision_id)
  if not isinstance(reason, str) or not reason.strip():
    raise ValueError('A source regeneration reason is required')
  current = scene.get('st_original_source_revision', '')
  if load_pipeline_state(scene) and current != revision_id:
    raise ValueError('An active repair must retain its recorded original; use a fresh scene')
  scene['st_original_source_revision'] = revision_id
  scene['st_original_source_revision_reason'] = reason.strip()
  return {'revision_id': revision_id, 'reason': reason.strip(),
          'previous_original_preserved': True, 'scene': scene.name}
