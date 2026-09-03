import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import meshy_pipeline_contract as contract


class ImmutableSnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "Hoya Cushion.blend"
        self.source.write_bytes(b"hoya-source-v1\x00texture-data")
        self.destination = self.root / "backup_01"
        self.logical = "source/Hoya Cushion.blend"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _tree_state(directory):
        return {
            path.relative_to(directory).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in directory.rglob("*")
            if path.is_file()
        }

    def test_first_publish_hashes_copy_and_writes_manifest_last(self):
        manifest = contract.publish_immutable_snapshot(
            self.source, self.destination, self.logical
        )
        backup = self.destination / "source" / "Hoya Cushion.blend"
        self.assertEqual(backup.read_bytes(), self.source.read_bytes())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["logical_filename"], self.logical)
        self.assertEqual(manifest["source"], contract.file_manifest(self.source))
        self.assertEqual(
            manifest["backup"]["sha256"], contract.sha256_file(backup)
        )
        self.assertEqual(
            contract.load_snapshot_manifest(self.destination), manifest
        )

    def test_second_publish_is_idempotent_and_performs_no_write(self):
        first = contract.publish_immutable_snapshot(
            self.source, self.destination, self.logical
        )
        before = self._tree_state(self.destination)
        with mock.patch.object(
            contract.shutil,
            "copy2",
            side_effect=AssertionError("idempotent publish attempted a copy"),
        ), mock.patch.object(
            contract,
            "_write_manifest_last",
            side_effect=AssertionError("idempotent publish attempted a manifest write"),
        ):
            second = contract.publish_immutable_snapshot(
                self.source, self.destination, self.logical
            )
        self.assertEqual(second, first)
        self.assertEqual(self._tree_state(self.destination), before)

    def test_changed_source_is_rejected_without_overwriting_snapshot(self):
        contract.publish_immutable_snapshot(
            self.source, self.destination, self.logical
        )
        destination_before = self._tree_state(self.destination)
        self.source.write_bytes(b"hoya-source-v2\x00texture-data")
        with self.assertRaisesRegex(
            contract.SnapshotConflictError, "source hash/size"
        ):
            contract.publish_immutable_snapshot(
                self.source, self.destination, self.logical
            )
        self.assertEqual(self._tree_state(self.destination), destination_before)

    def test_corrupt_backup_is_rejected_without_repair(self):
        contract.publish_immutable_snapshot(
            self.source, self.destination, self.logical
        )
        backup = self.destination / "source" / "Hoya Cushion.blend"
        backup.write_bytes(b"x" * self.source.stat().st_size)
        corrupted_state = self._tree_state(self.destination)
        with self.assertRaisesRegex(
            contract.SnapshotIntegrityError, "backup hash/size"
        ):
            contract.publish_immutable_snapshot(
                self.source, self.destination, self.logical
            )
        self.assertEqual(self._tree_state(self.destination), corrupted_state)

    def test_absolute_and_traversal_logical_names_are_rejected(self):
        unsafe_names = (
            "../outside.blend",
            "nested/../../outside.blend",
            "nested\\..\\outside.blend",
            "/absolute/outside.blend",
            "C:\\absolute\\outside.blend",
            "\\\\server\\share\\outside.blend",
        )
        for index, logical in enumerate(unsafe_names):
            with self.subTest(logical=logical):
                destination = self.root / f"unsafe_{index}"
                with self.assertRaisesRegex(ValueError, "logical_filename"):
                    contract.publish_immutable_snapshot(
                        self.source, destination, logical
                    )
                self.assertFalse(destination.exists())


class ImmutableSnapshotSetContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source_dir = self.root / "inputs"
        source_dir.mkdir()
        self.blend = source_dir / "Huya Cushion.blend"
        self.color = source_dir / "T_Huya_Color.png"
        self.extra = source_dir / "T_Huya_Extra.png"
        self.normal = source_dir / "T_Huya_Normal.png"
        self.blend.write_bytes(b"huya-blend-source")
        self.color.write_bytes(b"color-pixels")
        self.extra.write_bytes(b"extra-rgm-pixels")
        self.normal.write_bytes(b"normal-pixels")
        self.sources = {
            "Huya Cushion.blend": self.blend,
            "texture/T_Huya_Color.png": self.color,
            "texture/T_Huya_Extra.png": self.extra,
            "texture/T_Huya_Normal.png": self.normal,
        }
        self.destination = self.root / "00_source_original_once"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _tree_state(directory):
        return {
            path.relative_to(directory).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in directory.rglob("*")
            if path.is_file()
        }

    def test_first_set_publish_copies_all_and_writes_manifest_last(self):
        original_writer = contract._write_manifest_last
        observed_before_manifest = []

        def asserting_manifest_writer(directory, manifest):
            observed_before_manifest.extend(contract._snapshot_set_files(directory))
            self.assertNotIn(contract.MANIFEST_FILENAME, observed_before_manifest)
            self.assertEqual(set(observed_before_manifest), set(self.sources))
            return original_writer(directory, manifest)

        with mock.patch.object(
            contract, "_write_manifest_last", side_effect=asserting_manifest_writer
        ):
            manifest = contract.publish_immutable_snapshot_set(
                self.sources, self.destination
            )

        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            sorted(self.sources, key=str.casefold),
        )
        for entry in manifest["files"]:
            source = self.sources[entry["path"]]
            backup = self.destination.joinpath(*entry["path"].split("/"))
            self.assertEqual(backup.read_bytes(), source.read_bytes())
            self.assertEqual(entry["source"], contract.file_manifest(source))
            self.assertEqual(entry["backup"], contract.file_manifest(backup))
        self.assertEqual(
            contract.load_snapshot_set_manifest(self.destination), manifest
        )

    def test_existing_exact_set_is_no_write(self):
        first = contract.publish_immutable_snapshot_set(
            self.sources, self.destination
        )
        before = self._tree_state(self.destination)
        with mock.patch.object(
            contract.shutil,
            "copy2",
            side_effect=AssertionError("idempotent set publish attempted a copy"),
        ), mock.patch.object(
            contract,
            "_write_manifest_last",
            side_effect=AssertionError("idempotent set publish attempted a write"),
        ), mock.patch.object(
            contract.os,
            "rename",
            side_effect=AssertionError("idempotent set publish attempted a rename"),
        ):
            second = contract.publish_immutable_snapshot_set(
                self.sources, self.destination
            )
        self.assertEqual(second, first)
        self.assertEqual(self._tree_state(self.destination), before)

    def test_archive_only_verification_survives_deliberate_source_replacement(self):
        first = contract.publish_immutable_snapshot_set(
            self.sources, self.destination
        )
        before = self._tree_state(self.destination)
        self.color.write_bytes(b"painter-replaced-canonical-color")

        verified = contract.verify_immutable_snapshot_set_archive(self.destination)

        self.assertEqual(verified, first)
        self.assertEqual(self._tree_state(self.destination), before)
        with self.assertRaisesRegex(contract.SnapshotConflictError, "source hash/size"):
            contract.verify_immutable_snapshot_set(self.sources, self.destination)

    def test_empty_duplicate_and_unsafe_source_sets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty mapping"):
            contract.publish_immutable_snapshot_set({}, self.destination)

        duplicates = {
            "Texture/T_Huya_Color.png": self.color,
            "texture\\t_huya_COLOR.PNG": self.extra,
        }
        with self.assertRaisesRegex(ValueError, "duplicate normalized/casefold"):
            contract.publish_immutable_snapshot_set(duplicates, self.destination)

        for index, unsafe in enumerate(
            ("../outside.blend", "/absolute.blend", "C:\\absolute.blend")
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "logical_filename"):
                    contract.publish_immutable_snapshot_set(
                        {unsafe: self.blend}, self.root / f"unsafe_set_{index}"
                    )

    def test_changed_source_or_mapping_is_a_hard_conflict(self):
        contract.publish_immutable_snapshot_set(self.sources, self.destination)
        before = self._tree_state(self.destination)
        self.color.write_bytes(b"X" * self.color.stat().st_size)
        with self.assertRaisesRegex(
            contract.SnapshotConflictError, "source hash/size"
        ):
            contract.publish_immutable_snapshot_set(
                self.sources, self.destination
            )
        self.assertEqual(self._tree_state(self.destination), before)

        changed_mapping = dict(self.sources)
        changed_mapping.pop("texture/T_Huya_Color.png")
        with self.assertRaisesRegex(
            contract.SnapshotConflictError, "source mapping differs"
        ):
            contract.verify_immutable_snapshot_set(
                changed_mapping, self.destination
            )

    def test_missing_backup_is_rejected(self):
        contract.publish_immutable_snapshot_set(self.sources, self.destination)
        (self.destination / "texture" / "T_Huya_Extra.png").unlink()
        with self.assertRaisesRegex(
            contract.SnapshotIntegrityError, "file membership"
        ):
            contract.verify_immutable_snapshot_set(
                self.sources, self.destination
            )

    def test_unlisted_extra_file_is_rejected(self):
        contract.publish_immutable_snapshot_set(self.sources, self.destination)
        (self.destination / "unlisted.tmp").write_bytes(b"unexpected")
        with self.assertRaisesRegex(
            contract.SnapshotIntegrityError, "unlisted"
        ):
            contract.verify_immutable_snapshot_set(
                self.sources, self.destination
            )

    def test_same_size_tampered_backup_is_rejected(self):
        contract.publish_immutable_snapshot_set(self.sources, self.destination)
        backup = self.destination / "texture" / "T_Huya_Normal.png"
        backup.write_bytes(b"X" * backup.stat().st_size)
        with self.assertRaisesRegex(
            contract.SnapshotIntegrityError, "backup hash/size"
        ):
            contract.verify_immutable_snapshot_set(
                self.sources, self.destination
            )

    def test_source_change_during_publish_cleans_only_sibling_temp(self):
        original_copy = contract.shutil.copy2

        def copy_then_mutate(source, destination):
            result = original_copy(source, destination)
            if Path(source) == self.normal:
                self.color.write_bytes(b"Y" * self.color.stat().st_size)
            return result

        with mock.patch.object(
            contract.shutil, "copy2", side_effect=copy_then_mutate
        ):
            with self.assertRaisesRegex(
                contract.SnapshotConflictError, "source changed"
            ):
                contract.publish_immutable_snapshot_set(
                    self.sources, self.destination
                )
        self.assertFalse(self.destination.exists())
        self.assertEqual(
            list(self.root.glob(f".{self.destination.name}.tmp-*")), []
        )


if __name__ == "__main__":
    unittest.main()
