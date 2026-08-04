import json
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import speedtree_handoff_contract as contract


class SpeedTreeHandoffContractTests(unittest.TestCase):
    def test_tree_axes_preserve_existing_leaf_first_and_stem_rules(self):
        for row in contract.golden_vectors()["tree_axes"]:
            with self.subTest(name=row["name"]):
                part = contract.classify_tree_part(row["name"])
                self.assertEqual(
                    (
                        part,
                        contract.classify_tree_shading(
                            row["name"], tree_part=part
                        ),
                    ),
                    (row["tree_part"], row["tree_shading"]),
                )

    def test_production_groups_and_instance_profile_are_separate_namespaces(self):
        for row in contract.golden_vectors()["production_groups"]:
            with self.subTest(material=row["material"]):
                intent = contract.build_material_intent(row["material"])
                self.assertEqual(intent["production_group_base"], row["base"])
                self.assertEqual(intent["production_group_tokens"], row["tokens"])

        dead_material = contract.build_material_intent("M_Leaf_common_grass_01_dead")
        self.assertEqual(dead_material["instance_profile"], "")
        self.assertNotIn("profile_target_name", dead_material)

        profiled = contract.build_material_intent(
            "M_stem_common_01", instance_profile="Dead"
        )
        self.assertEqual(profiled["instance_profile"], "dead")
        self.assertEqual(profiled["material_instance_base"], "stem_common_01")
        self.assertEqual(profiled["profile_target_name"], "MI_stem_common_01_dead")

    def test_tree_texture_policy_is_shared_and_shading_specific(self):
        for row in contract.golden_vectors()["tree_texture_policy"]:
            with self.subTest(param=row["param"], shading=row["tree_shading"]):
                self.assertEqual(
                    contract.tree_texture_param_allowed(
                        row["param"], row["tree_shading"]
                    ),
                    row["allowed"],
                )

    def test_profile_validation_and_contract_revision(self):
        self.assertEqual(contract.contract_version(), 3)
        self.assertEqual(contract.golden_vectors()["contract_version"], 3)
        self.assertEqual(contract.normalize_instance_profile("Dead"), "dead")
        with self.assertRaises(ValueError):
            contract.normalize_instance_profile("../dead")
        intent = contract.build_material_intent(
            "M_stem_common_01", instance_profile="dead"
        )
        self.assertEqual(contract.validate_material_intent(intent), intent)
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            contract.validate_material_intent({**intent, "contract_version": 999})

        descriptor = contract.build_sidecar_descriptor("SK_CommonGrass")
        self.assertEqual(
            contract.validate_sidecar_descriptor(descriptor, "SK_CommonGrass"),
            descriptor,
        )
        with self.assertRaisesRegex(ValueError, "mesh mismatch"):
            contract.validate_sidecar_descriptor(descriptor, "SK_OtherGrass")

    def test_machine_rules_match_documented_tree_paths(self):
        payload = json.loads(
            (REPO / "pipeline_contract.json").read_text(encoding="utf-8")
        )
        machine = contract.tree_unreal_preset()
        documented = payload["unreal_handoff_sidecar"]["tree_material_layer_contract"]
        self.assertEqual(machine["masters_by_shading"], documented["masters"])
        self.assertEqual(machine["layer_instance_folder"], documented["instance_folder"])
        self.assertEqual(
            machine["layer_parents_by_part"],
            {
                part: values["parent"]
                for part, values in documented["parts"].items()
            },
        )
        wind = contract.dynamic_wind_rules()
        self.assertEqual(wind["asset_kind"], "speedtree")
        self.assertTrue(wind["filename_suffix"].endswith(".json"))
        ownership = contract.asset_ownership_rules()
        self.assertEqual(ownership["base_mi"], "pipeline_managed")
        self.assertEqual(
            ownership["profile_mi"],
            "user_managed_create_once_then_immutable",
        )

    def test_production_group_suffix_has_no_token_allowlist(self):
        material_rules = contract.rules()["material_name"]
        self.assertNotIn("production_group_tokens", material_rules)
        self.assertNotIn(
            "tokens", contract.rules()["pcg_atlas_auto_split"]
        )
        self.assertEqual(
            contract.production_group_tokens("M_Leaf_common_grass_01_flower"),
            ["flower"],
        )
        self.assertEqual(
            contract.production_group_tokens(
                "M_Leaf_common_grass_01_user_defined_winter"
            ),
            ["user_defined_winter"],
        )

    def test_production_group_parser_safety_boundary_is_explicit(self):
        suffix_rules = contract.rules()["material_name"]["production_group_suffix"]
        self.assertEqual(suffix_rules["scope"], "pure_material_name_parser_only")
        self.assertIn("provenance", suffix_rules["safety_boundary"])
        self.assertIn("source signatures", suffix_rules["safety_boundary"])
        self.assertTrue(
            contract.rules()["pcg_atlas_auto_split"][
                "requires_provenance_and_matching_source_signature"
            ]
        )

    def test_preflight_envelope_validates_descriptor_source_and_intents(self):
        digest = "a" * 64
        source = {
            "spm": {
                "canonical_path": "C:/Trees/SK_Grass.spm",
                "sha256": digest,
                "size": 10,
                "mtime_ns": 20,
            },
            "stmat": [{
                "canonical_path": "C:/Trees/fbx/SK_Grass.stmat",
                "sha256": "b" * 64,
                "size": 30,
                "mtime_ns": 40,
            }],
        }
        envelope = {
            "kind": "speedtree_material_preflight",
            "schema_version": 1,
            "speedtree_handoff_contract": contract.build_sidecar_descriptor(
                "SK_Grass", source=source
            ),
            "outcome": "ok",
            "source": source,
            "source_fingerprint": "c" * 64,
            "instance_profile": "Dead",
            "material_intents": [{
                "material_name": "M_stem_common_01",
                **contract.build_material_intent(
                    "M_stem_common_01", instance_profile="dead"
                ),
            }],
        }
        self.assertEqual(
            contract.validate_preflight_envelope(envelope, "SK_Grass"),
            envelope,
        )
        broken = {**envelope, "source_fingerprint": "not-a-hash"}
        with self.assertRaisesRegex(ValueError, "source_fingerprint"):
            contract.validate_preflight_envelope(broken, "SK_Grass")


if __name__ == "__main__":
    unittest.main()
