import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory.recovery import plan, scan


class RecoverySafetyTests(unittest.TestCase):
	def test_current_observation_does_not_warn_but_malformed_one_does(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			observations = home / "inventory" / "observations"
			observations.mkdir(parents=True)
			(observations / "current.json").write_text(json.dumps({"schema": "hermes-inventory-observation", "schema_version": 2}), encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				report = scan()
			self.assertEqual(report["status"], "PASS")
			self.assertFalse(report["duplicate_observations"])
			(observations / "broken.json").write_text("not json", encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				report = scan()
			self.assertEqual(report["status"], "WARN")
			self.assertEqual(report["malformed_observations"], [str(observations / "broken.json")])
	def test_manifest_path_escape_is_reported_without_reading_target(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory)
			item = home / "inventory" / "items" / "INV-1"
			item.mkdir(parents=True)
			(item / "item.json").write_text(json.dumps({"schema": "hermes-inventory-item", "schema_version": 1, "inventory_id": "INV-1", "images": [{"relative_path": "../../outside", "sha256": "x"}]}), encoding="utf-8")
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				report = scan()
			self.assertEqual(report["unsafe_image_paths"], ["../../outside"])

	def test_plan_distinguishes_present_missing_mismatch_and_pending(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory); items = home / "inventory" / "items"
			for item_id, status, entity_id, name in (("A", "synced", "1", "same"), ("B", "synced", "2", "missing"), ("C", "synced", "3", "expected"), ("D", "pending_homebox_sync", None, "pending")):
				path = items / item_id; path.mkdir(parents=True)
				(path / "item.json").write_text(json.dumps({"schema": "hermes-inventory-item", "schema_version": 1, "inventory_id": item_id, "status": status, "item": {"name": name}, "homebox": {"entity_id": entity_id}}))
			with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
				report = plan(homebox_entities=[{"id": "1", "name": "same"}, {"id": "3", "name": "other"}])
			self.assertEqual(report["present_in_both"], ["A"])
			self.assertEqual(report["persistent_missing_from_homebox"], ["B"])
			self.assertEqual(report["homebox_entity_mismatch"], ["C"])
			self.assertEqual(report["pending_homebox_sync"], ["D"])