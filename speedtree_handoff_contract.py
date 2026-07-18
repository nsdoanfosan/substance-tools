"""Pure SpeedTree material/handoff rules shared by every pipeline runtime.

This module deliberately has no Blender or Unreal dependency.  Callers pass
plain strings/dicts and receive JSON-serializable values.  Application code is
still responsible for reading bpy data, parsing production SPM structures, or
mutating Unreal assets.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path


CONTRACT_PATH = Path(__file__).with_name("pipeline_contract.json")
VECTORS_PATH = Path(__file__).with_name("speedtree_handoff_vectors.json")


@lru_cache(maxsize=1)
def rules():
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    value = payload.get("speedtree_handoff_rules")
    if not isinstance(value, dict):
        raise RuntimeError("pipeline_contract.json has no speedtree_handoff_rules")
    return value


def contract_version():
    return int(rules().get("contract_version", 0))


def contract_fingerprint():
    encoded = json.dumps(
        rules(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=1)
def golden_vectors():
    value = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    if int(value.get("contract_version", 0)) != contract_version():
        raise RuntimeError("SpeedTree golden-vector contract version mismatch")
    return value


def contract_descriptor():
    return {
        "kind": str(rules().get("sidecar_kind") or "speedtree"),
        "version": contract_version(),
        "fingerprint": contract_fingerprint(),
    }


def build_sidecar_descriptor(mesh_name, source=None):
    mesh_name = str(mesh_name or "").strip()
    if not mesh_name:
        raise ValueError("SpeedTree handoff descriptor requires mesh_name")
    result = {
        **contract_descriptor(),
        "asset_kind": "speedtree",
        "mesh_name": mesh_name,
    }
    if isinstance(source, dict) and source:
        result["source"] = copy.deepcopy(source)
    return result


def validate_sidecar_descriptor(descriptor, expected_mesh_name=""):
    if not isinstance(descriptor, dict):
        raise ValueError("SpeedTree handoff descriptor must be an object")
    expected = contract_descriptor()
    if str(descriptor.get("kind") or "") != expected["kind"]:
        raise ValueError(f"unexpected SpeedTree handoff kind: {descriptor.get('kind')!r}")
    if int(descriptor.get("version", 0)) != expected["version"]:
        raise ValueError(
            "SpeedTree handoff contract version mismatch: "
            f"{descriptor.get('version')} != {expected['version']}"
        )
    if str(descriptor.get("fingerprint") or "") != expected["fingerprint"]:
        raise ValueError("SpeedTree handoff contract fingerprint mismatch")
    if str(descriptor.get("asset_kind") or "") != "speedtree":
        raise ValueError(f"unexpected asset_kind: {descriptor.get('asset_kind')!r}")
    mesh_name = str(descriptor.get("mesh_name") or "").strip()
    if not mesh_name:
        raise ValueError("SpeedTree handoff descriptor has no mesh_name")
    if expected_mesh_name and mesh_name.casefold() != str(expected_mesh_name).strip().casefold():
        raise ValueError(
            f"SpeedTree handoff mesh mismatch: {mesh_name!r} != {expected_mesh_name!r}"
        )
    return copy.deepcopy(descriptor)


def _material_rules():
    return rules().get("material_name") or {}


def _duplicate_suffix_re():
    pattern = str(
        _material_rules().get("blender_duplicate_suffix_pattern") or r"\.\d{3}$"
    )
    return re.compile(pattern)


def normalize_material_name(value, strip_stmat_suffix=True):
    name = _duplicate_suffix_re().sub("", str(value or "").strip())
    if strip_stmat_suffix:
        for suffix in _material_rules().get("stmat_suffixes") or ["_Mat"]:
            suffix = str(suffix or "")
            if suffix and name.casefold().endswith(suffix.casefold()):
                name = name[: -len(suffix)]
                break
    return name


def normalize_material_key(value):
    return re.sub(
        r"[^a-z0-9]+", "", normalize_material_name(value).casefold()
    )


def material_instance_base_name(value):
    name = normalize_material_name(value)
    for prefix in _material_rules().get("instance_prefixes") or ["MI_", "M_"]:
        prefix = str(prefix or "")
        if prefix and name[: len(prefix)].casefold() == prefix.casefold():
            return name[len(prefix):]
    return name


def _name_tokens(values):
    if isinstance(values, str) or values is None:
        values = [values]
    result = []
    for value in values:
        normalized = normalize_material_name(value)
        result.extend(
            token
            for token in re.split(r"[^a-z0-9]+", normalized.casefold())
            if token
        )
    return result


def production_group_tokens(value):
    allowed = {
        str(item).casefold()
        for item in _material_rules().get("production_group_tokens") or []
    }
    result = []
    for token in _name_tokens(value):
        if token not in allowed:
            continue
        canonical = "twig" if token == "twigs" else "stem" if token == "stems" else token
        if canonical not in result:
            result.append(canonical)
    return result


def production_group_base_name(value):
    name = normalize_material_name(value)
    allowed = {
        str(item).casefold()
        for item in _material_rules().get("production_group_tokens") or []
    }
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", name) if part]
    return "_".join(part for part in parts if part.casefold() not in allowed)


def pcg_atlas_auto_split_tokens():
    return tuple(
        str(item).casefold()
        for item in (rules().get("pcg_atlas_auto_split") or {}).get("tokens") or []
    )


def normalize_tree_part(value):
    aliases = (rules().get("tree_part") or {}).get("aliases") or {}
    return aliases.get(str(value or "").strip().casefold())


def classify_tree_part(names, explicit=""):
    normalized = normalize_tree_part(explicit)
    if explicit and not normalized:
        raise ValueError(f"invalid SpeedTree tree_part: {explicit!r}")
    if normalized:
        return normalized

    config = rules().get("tree_part") or {}
    tokens = set(_name_tokens(names))
    by_part = config.get("tokens") or {}
    for part in config.get("precedence") or ["leaf", "branch", "bark"]:
        if tokens.intersection(
            str(item).casefold() for item in by_part.get(part) or []
        ):
            return part
        if part == "branch" and any(
            token.endswith(tuple(config.get("branch_suffixes") or ["branch", "twig"]))
            for token in tokens
        ):
            return part
    return None


def normalize_tree_shading(value):
    aliases = (rules().get("tree_shading") or {}).get("aliases") or {}
    return aliases.get(str(value or "").strip().casefold())


def classify_tree_shading(names, explicit="", tree_part=None):
    normalized = normalize_tree_shading(explicit)
    if explicit and not normalized:
        raise ValueError(f"invalid SpeedTree tree_shading: {explicit!r}")
    if normalized:
        return normalized

    config = rules().get("tree_shading") or {}
    part = tree_part or classify_tree_part(names)
    if part == "leaf":
        return str(config.get("leaf_default") or "foliage")
    if set(_name_tokens(names)).intersection(
        str(item).casefold() for item in config.get("stem_tokens") or ["stem", "stems"]
    ):
        return "stem"
    return str(config.get("default") or "wood")


def normalize_instance_profile(value):
    profile = str(value or "").strip()
    if not profile:
        return ""
    config = rules().get("instance_profile") or {}
    pattern = str(config.get("pattern") or r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    if re.fullmatch(pattern, profile) is None:
        raise ValueError(
            "SpeedTree instance_profile must be one key using only letters, "
            "numbers, '_' or '-'"
        )
    if str(config.get("normalization") or "casefold") == "casefold":
        profile = profile.casefold()
    return profile


def instance_profile_mode():
    return str(
        (rules().get("instance_profile") or {}).get("mode") or "create_or_reuse"
    )


def tree_texture_param_allowed(param, tree_shading):
    policy = rules().get("texture_policy") or {}
    normalized = str(param or "").strip().casefold()
    excluded = {str(item).casefold() for item in policy.get("excluded_params") or []}
    if normalized in excluded:
        return False
    by_shading = policy.get("excluded_by_shading") or {}
    shading_excluded = {
        str(item).casefold() for item in by_shading.get(str(tree_shading or "").casefold()) or []
    }
    return normalized not in shading_excluded


def required_texture_roles():
    return tuple(
        str(item).casefold()
        for item in (rules().get("texture_policy") or {}).get("required_roles") or []
    )


def dynamic_wind_rules():
    return copy.deepcopy(rules().get("dynamic_wind") or {})


def asset_ownership_rules():
    """Return the mutation boundary for each Unreal material asset class."""
    return copy.deepcopy(rules().get("asset_ownership") or {})


def tree_unreal_preset():
    return copy.deepcopy(rules().get("unreal") or {})


def profile_target_name(material_name, profile):
    normalized_profile = normalize_instance_profile(profile)
    if not normalized_profile:
        raise ValueError("profile_target_name requires a non-empty profile")
    base = material_instance_base_name(material_name)
    if not base or "/" in base or "\\" in base or base in {".", ".."}:
        raise ValueError(f"unsafe material instance base name: {base!r}")
    return f"MI_{base}_{normalized_profile}"


def build_material_intent(
    material_name,
    *,
    names=None,
    explicit_tree_part="",
    explicit_tree_shading="",
    instance_profile="",
):
    names = list(names or [material_name])
    part = classify_tree_part(names, explicit_tree_part)
    shading = classify_tree_shading(names, explicit_tree_shading, part)
    profile = normalize_instance_profile(instance_profile)
    result = {
        "contract_version": contract_version(),
        "material_key": normalize_material_key(material_name),
        "material_instance_base": material_instance_base_name(material_name),
        "production_group_base": production_group_base_name(material_name),
        "production_group_tokens": production_group_tokens(material_name),
        "tree_part": part,
        "tree_shading": shading,
        "instance_profile": profile,
    }
    if profile:
        result["material_instance_mode"] = instance_profile_mode()
        result["profile_target_name"] = profile_target_name(material_name, profile)
    return result


def validate_material_intent(intent):
    if not isinstance(intent, dict):
        raise ValueError("SpeedTree material intent must be an object")
    version = int(intent.get("contract_version", 0))
    if version != contract_version():
        raise ValueError(
            f"SpeedTree contract version mismatch: {version} != {contract_version()}"
        )
    part = normalize_tree_part(intent.get("tree_part"))
    if intent.get("tree_part") and not part:
        raise ValueError(f"invalid SpeedTree tree_part: {intent.get('tree_part')!r}")
    shading = normalize_tree_shading(intent.get("tree_shading"))
    if intent.get("tree_shading") and not shading:
        raise ValueError(
            f"invalid SpeedTree tree_shading: {intent.get('tree_shading')!r}"
        )
    profile = normalize_instance_profile(intent.get("instance_profile"))
    mode = str(intent.get("material_instance_mode") or "").strip().casefold()
    if profile and mode != instance_profile_mode():
        raise ValueError(
            f"SpeedTree instance mode mismatch: {mode!r} != {instance_profile_mode()!r}"
        )
    base = str(intent.get("material_instance_base") or "").strip()
    if not base or "/" in base or "\\" in base or base in {".", ".."}:
        raise ValueError(f"unsafe material_instance_base: {base!r}")
    return {
        **intent,
        "tree_part": part,
        "tree_shading": shading,
        "instance_profile": profile,
        "material_instance_mode": mode if profile else "",
        "material_instance_base": base,
    }


def validate_material_intent_for_name(intent, material_name):
    """Validate both intent shape and canonical values for one material name."""
    validated = validate_material_intent(intent)
    expected = build_material_intent(
        material_name,
        explicit_tree_part=validated.get("tree_part") or "",
        explicit_tree_shading=validated.get("tree_shading") or "",
        instance_profile=validated.get("instance_profile") or "",
    )
    for key, expected_value in expected.items():
        if validated.get(key) != expected_value:
            raise ValueError(
                f"SpeedTree material intent {key} mismatch for "
                f"{material_name!r}: {validated.get(key)!r} != "
                f"{expected_value!r}"
            )
    return validated


def preflight_report_rules():
    return copy.deepcopy(rules().get("preflight_report") or {})


def _validate_source_identity(identity, label):
    if not isinstance(identity, dict):
        raise ValueError(f"{label} source identity must be an object")
    config = preflight_report_rules()
    for field in config.get("required_source_identity_fields") or ():
        if not str(identity.get(field) or "").strip():
            raise ValueError(f"{label} source identity has no {field}")
    digest = str(identity.get("sha256") or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"{label} source identity has invalid sha256")
    for field in config.get("diagnostic_source_identity_fields") or ():
        if field in identity:
            try:
                numeric = int(identity[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{label} source identity has invalid {field}"
                ) from exc
            if numeric < 0:
                raise ValueError(f"{label} source identity has negative {field}")
    return copy.deepcopy(identity)


def validate_preflight_envelope(envelope, expected_mesh_name=""):
    """Validate a new Batch/BWR handoff envelope without touching source files."""
    if not isinstance(envelope, dict):
        raise ValueError("SpeedTree preflight envelope must be an object")
    config = preflight_report_rules()
    if str(envelope.get("kind") or "") != str(config.get("kind") or ""):
        raise ValueError(f"unexpected SpeedTree preflight kind: {envelope.get('kind')!r}")
    if int(envelope.get("schema_version", 0)) != int(
        config.get("schema_version", 0)
    ):
        raise ValueError("SpeedTree preflight schema version mismatch")
    if str(envelope.get("outcome") or "") not in set(config.get("outcomes") or ()):
        raise ValueError(f"unexpected SpeedTree preflight outcome: {envelope.get('outcome')!r}")

    source = envelope.get("source")
    if not isinstance(source, dict):
        raise ValueError("SpeedTree preflight envelope has no source object")
    _validate_source_identity(source.get("spm"), "SPM")
    stmat = source.get("stmat")
    if not isinstance(stmat, list):
        raise ValueError("SpeedTree preflight source.stmat must be an array")
    for index, identity in enumerate(stmat):
        _validate_source_identity(identity, f"STMAT[{index}]")

    fingerprint = str(envelope.get("source_fingerprint") or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("SpeedTree preflight source_fingerprint is invalid")

    descriptor = validate_sidecar_descriptor(
        envelope.get("speedtree_handoff_contract"),
        expected_mesh_name=expected_mesh_name,
    )
    descriptor_source = descriptor.get("source")
    if descriptor_source != source:
        raise ValueError(
            "SpeedTree preflight descriptor source does not match envelope source"
        )

    profile = normalize_instance_profile(envelope.get("instance_profile"))
    intents = envelope.get("material_intents")
    if not isinstance(intents, list):
        raise ValueError("SpeedTree preflight material_intents must be an array")
    for index, intent in enumerate(intents):
        material_name = str(
            intent.get("material_name") if isinstance(intent, dict) else ""
        ).strip()
        if not material_name:
            raise ValueError(
                f"SpeedTree preflight material_intents[{index}] has no material_name"
            )
        validated = validate_material_intent_for_name(intent, material_name)
        if validated.get("instance_profile") != profile:
            raise ValueError(
                f"SpeedTree preflight material_intents[{index}] profile mismatch"
            )
    return copy.deepcopy(envelope)


__all__ = [
    "asset_ownership_rules",
    "build_sidecar_descriptor",
    "build_material_intent",
    "classify_tree_part",
    "classify_tree_shading",
    "contract_descriptor",
    "contract_fingerprint",
    "contract_version",
    "dynamic_wind_rules",
    "golden_vectors",
    "instance_profile_mode",
    "material_instance_base_name",
    "normalize_instance_profile",
    "normalize_material_key",
    "normalize_material_name",
    "normalize_tree_part",
    "normalize_tree_shading",
    "pcg_atlas_auto_split_tokens",
    "preflight_report_rules",
    "production_group_base_name",
    "production_group_tokens",
    "profile_target_name",
    "required_texture_roles",
    "rules",
    "tree_texture_param_allowed",
    "tree_unreal_preset",
    "validate_material_intent",
    "validate_material_intent_for_name",
    "validate_preflight_envelope",
    "validate_sidecar_descriptor",
]
