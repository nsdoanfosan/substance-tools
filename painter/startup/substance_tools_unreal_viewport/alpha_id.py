"""Refresh exact source-backed ID masks inside an admitted native handoff."""
import hashlib
import math
import os
from pathlib import Path

import substance_painter.layerstack as layers
import substance_painter.project as project
import substance_painter.resource as resources
import substance_painter.textureset as texturesets


def _walk(nodes):
    for node in nodes:
        yield node
        if isinstance(node, layers.GroupLayerNode):
            yield from _walk(node.sub_layers())


def _prepare(request):
    expected = request.get('spp')
    if (not expected or not project.is_open()
            or os.path.normcase(os.path.abspath(project.file_path()))
            != os.path.normcase(os.path.abspath(expected))):
        raise ValueError('The exact saved Painter project must be open')
    if project.is_busy() or not project.is_in_edition_state():
        raise RuntimeError('Painter has an unfinished native operation')
    bindings = request.get('alpha_id_selections', [])
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 64:
        raise ValueError('One to sixty-four exact ID bindings are required')
    prepared = []
    seen = set()
    for binding in bindings:
        name = binding.get('effect_name', '')
        palette = binding.get('colors', [])
        if (not isinstance(name, str) or not name or len(name) > 128
                or not isinstance(palette, list) or not 1 <= len(palette) <= 16
                or any(not isinstance(c, (list, tuple)) or len(c) != 3
                       or any(isinstance(v, bool) or not isinstance(v, (int, float))
                              or not math.isfinite(v) or not 0 <= v <= 1 for v in c)
                       for c in palette)):
            raise ValueError('A bounded effect name and RGB palette are required')
        stack = texturesets.TextureSet.from_name(binding['texture_set_name']).get_stack()
        uid = int(binding['layer_uid'])
        key = (binding['texture_set_name'], uid, name)
        if key in seen:
            raise ValueError('Duplicate ID target binding')
        seen.add(key)
        found_layers = [n for n in _walk(layers.get_root_layer_nodes(stack)) if n.uid() == uid]
        if len(found_layers) != 1:
            raise ValueError('Layer UID is absent from the exact Texture Set')
        layer = found_layers[0]
        if layer.get_name() != binding.get('expected_layer_name'):
            raise ValueError('The inspected layer name changed')
        path = Path(binding['texture_path']).resolve(strict=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix.lower() != '.png' or digest != binding.get('expected_sha256'):
            raise ValueError('ID PNG bytes differ from the verified source')
        effects = layer.mask_effects() if layer.has_mask() else []
        found = [e for e in effects if e.get_name() == name]
        if len(found) > 1 or (found and not isinstance(found[0], layers.ColorSelectionEffectNode)):
            raise ValueError('The named mask is ambiguous or is not Color Selection')
        prepared.append((binding, layer, path, digest, effects, found))
    return prepared


def apply_id_selections(request):
    # All targets and source hashes must pass before any resource import or edit.
    prepared = _prepare(request)
    from substance_painter.colormanagement import Color, GenericColorSpace
    receipts = []
    for binding, layer, path, digest, effects, found in prepared:
        identifier = resources.import_project_resource(str(path), resources.Usage.TEXTURE).identifier()
        with layers.ScopedModification('Source-backed ID color selection'):
            if not layer.has_mask():
                layer.add_mask(layers.MaskBackground.Black)
            effect = found[0] if found else layers.insert_color_selection_effect(
                layers.InsertPosition.above_node(effects[0]) if effects else
                layers.InsertPosition.inside_node(layer, layers.NodeStack.Mask))
            effect.set_name(binding['effect_name'])
            effect.set_parameters(layers.ColorSelectionEffectParams(
                id_mask=identifier, output_value=1.0, hardness=1.0, tolerance=0.1,
                background_color=layers.ColorSelectionBackgroundColor.Black,
                colors=[Color(*c, color_space=GenericColorSpace.Working) for c in binding['colors']]))
            effect.set_blending_mode(layers.BlendingMode.Normal)
            effect.set_opacity(1.0)
        actual = effect.get_parameters()
        if actual.id_mask != identifier or len(actual.colors) != len(binding['colors']):
            raise RuntimeError('Native Color Selection readback differs')
        receipts.append({'texture_set': binding['texture_set_name'], 'layer_uid': layer.uid(),
                         'effect_uid': effect.uid(), 'sha256': digest,
                         'preserved_previous_effect_uids': [e.uid() for e in effects]})
    return receipts
