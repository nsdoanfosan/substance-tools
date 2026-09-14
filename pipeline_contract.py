import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def pipeline_contract():
  path = Path(__file__).with_name('pipeline_contract.json')
  return json.loads(path.read_text(encoding='utf-8'))


def collection_name(key, default):
  return pipeline_contract().get('blender_collections', {}).get(key, default)


def naming_value(key, default):
  return pipeline_contract().get('naming', {}).get(key, default)


def unreal_path_mapping():
  return pipeline_contract().get('unreal_path_mapping', {}).get('current_default', {})


def integration_api(name):
  """Return one versioned cross-add-on API declaration."""
  return dict(pipeline_contract().get('integration_apis', {}).get(name, {}))
