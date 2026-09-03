"""Versioned API owned by Substance Tools' Painter transfer boundary.

Quad Remesher and UVgami expose their own APIs. This module only owns the
Painter High/Low material-and-collection pairing and its resulting receipt.
"""

from __future__ import annotations

import bpy

from .meshy_pipeline import (
  MeshyPipelineError,
  _resolve_low_export_api,
  adopt_retopology_pair as _adopt_retopology_pair,
  load_pipeline_state,
)


PAINTER_TRANSFER_API_VERSION = 1
PAINTER_TRANSFER_SERVICE_ID = 'substance.painter_transfer'


__all__ = (
  'IntegrationApiContractError',
  'PAINTER_TRANSFER_API_VERSION',
  'PAINTER_TRANSFER_SERVICE_ID',
  'adopt_retopology_pair',
  'get_capabilities',
  'get_painter_transfer_api',
  'inspect_transfer_state',
)


class IntegrationApiContractError(RuntimeError):
  """Raised before mutation when a provider-owned dependency is incompatible."""


def _scene(scene):
  resolved = scene or bpy.context.scene
  if resolved is None:
    raise ValueError('A Blender scene is required')
  return resolved


def _receipt(operation, payload, *, status='SUCCESS'):
  return {
    **dict(payload or {}),
    'service_id': PAINTER_TRANSFER_SERVICE_ID,
    'api_version': PAINTER_TRANSFER_API_VERSION,
    'operation': operation,
    'status': status,
  }


def get_painter_transfer_api(version=PAINTER_TRANSFER_API_VERSION):
  """Return the exact supported Painter-transfer service."""
  if version != PAINTER_TRANSFER_API_VERSION:
    raise IntegrationApiContractError(
      f'Painter transfer API {version!r} is incompatible with '
      f'{PAINTER_TRANSFER_API_VERSION}'
    )
  return {
    'service_id': PAINTER_TRANSFER_SERVICE_ID,
    'version': PAINTER_TRANSFER_API_VERSION,
    'adopt_retopology_pair': adopt_retopology_pair,
    'inspect_transfer_state': inspect_transfer_state,
    'get_capabilities': get_capabilities,
  }


def get_capabilities():
  """Describe the narrow owner boundary without probing other add-ons."""
  return _receipt(
    'get_capabilities',
    {
      'owns': [
        'Baking/high and Baking/low classification',
        'Painter-safe High/Low names and isolated Low materials',
        'Painter transfer checkpoint state',
      ],
      'delegates': [
        'quad-remesher.workflow',
        'uvgami.unwrap',
        'unreal-handoff.painter-low-export',
      ],
    },
  )


def inspect_transfer_state(*, scene=None):
  """Read the current Painter-transfer checkpoint without mutation."""
  state = load_pipeline_state(_scene(scene), allow_legacy=True)
  if not state:
    return _receipt(
      'inspect_transfer_state',
      {'available': False, 'asset_base': None, 'stage': None},
    )
  return _receipt(
    'inspect_transfer_state',
    {
      'available': True,
      'asset_base': state.get('asset_base'),
      'stage': state.get('stage'),
      'legacy_read_only': bool(state.get('_legacy_read_only')),
    },
  )


def _optional_low_export_api_status():
  """Preflight UE Unique without taking ownership of its export work."""
  try:
    status, _ = _resolve_low_export_api()
  except MeshyPipelineError as exc:
    raise IntegrationApiContractError(
      f'Invalid painter_low_export_sync integration: {exc}'
    ) from exc
  return status


def adopt_retopology_pair(
  source,
  result,
  state,
  topology_owner_receipt,
  *,
  scene=None,
  resolution=2048,
  margin_pixels=8,
):
  """Create Painter's canonical High/Low pair from an owner-approved result."""
  optional_api = _optional_low_export_api_status()
  adoption = _adopt_retopology_pair(
    _scene(scene),
    source,
    result,
    state,
    topology_owner_receipt,
    resolution=resolution,
    margin_pixels=margin_pixels,
  )
  export_sync = dict(adoption.get('ue_unique_export_sync') or {})
  if not optional_api['available']:
    export_sync.update(optional_api)
  else:
    observed = export_sync.get('api_version')
    expected = optional_api['expected_api_version']
    if observed != expected:
      export_sync.update({
        'available': True,
        'synced': False,
        'error': (
          f'UE Unique sync receipt version {observed!r} did not match {expected}'
        ),
        **optional_api,
      })
    else:
      export_sync.update(optional_api)
      export_sync.setdefault('synced', 'error' not in export_sync)
  adoption['ue_unique_export_sync'] = export_sync
  return _receipt('adopt_retopology_pair', adoption)
