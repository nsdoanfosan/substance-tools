import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class IntegrationApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads(
            (REPO / "pipeline_contract.json").read_text(encoding="utf-8")
        )

    def test_painter_transfer_api_is_narrow_and_versioned(self):
        spec = self.contract["integration_apis"]["painter_transfer"]
        self.assertEqual(spec["owner"], "substance-tools")
        self.assertEqual(spec["service_id"], "substance.painter_transfer")
        self.assertEqual(spec["module"], "substance_tools.api")
        self.assertEqual(spec["getter"], "get_painter_transfer_api")
        self.assertEqual(spec["version"], 1)
        self.assertEqual(
            set(spec["functions"]),
            {
                "adopt_retopology_pair",
                "inspect_transfer_state",
                "get_capabilities",
            },
        )

    def test_topology_and_uv_services_have_separate_owners(self):
        qr = self.contract["integration_apis"]["quad_remesher_workflow"]
        uv = self.contract["integration_apis"]["uvgami_unwrap"]
        self.assertEqual(qr["owner"], "quad-remesher-workflow-addon")
        self.assertEqual(qr["module"], "quad_remesher_workflow_addon.api")
        self.assertEqual(uv["owner"], "UVgami")
        self.assertEqual(uv["service_id"], "uvgami.unwrap")
        self.assertIn("preflight_unwrap", uv["functions"])

    def test_low_export_sync_has_one_external_owner(self):
        spec = self.contract["integration_apis"]["painter_low_export_sync"]
        self.assertEqual(spec["owner"], "ue-unique-export-names-addon")
        self.assertEqual(
            spec["service_id"], "unreal-handoff.painter-low-export"
        )
        self.assertEqual(spec["module"], "ue_unique_export_names_addon.api")
        self.assertEqual(spec["getter"], "get_painter_low_export_api")
        self.assertEqual(spec["function"], "ensure_painter_low_export_unit")
        self.assertEqual(spec["version"], 2)


if __name__ == "__main__":
    unittest.main()
