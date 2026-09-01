import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from inventory.backup import _safe_sources, create_backup, verify_backup


class BackupTests(unittest.TestCase):
	def test_backup_excludes_only_backup_subtree_and_verifies(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			home = Path(temporary_directory); root = home / "inventory"
			(root / "items" / "INV-1").mkdir(parents=True); (root / "items" / "INV-1" / "item.json").write_text("{}")
			(root / "catalog.json").write_text("{}")
			(root / "backups").mkdir(); (root / "backups" / "old.zip").write_bytes(b"old")
			with patch.dict(os.environ, {"HERMES_HOME": str(home), "INVENTORY_BASE_DIR": str(root)}, clear=False):
				result = create_backup()
				self.assertEqual(result["status"], "WARN")
				with zipfile.ZipFile(result["path"]) as archive:
					self.assertIn("inventory/catalog.json", archive.namelist())
					self.assertNotIn("inventory/backups/old.zip", archive.namelist())
				self.assertEqual(verify_backup(Path(result["path"]))["status"], "PASS")

	def test_verification_rejects_backslash_and_parent_traversal(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			path = Path(temporary_directory) / "attack.zip"
			with zipfile.ZipFile(path, "w") as archive:
				archive.writestr("..\\outside.txt", "x")
			self.assertEqual(verify_backup(path)["status"], "FAIL")

	def test_safe_sources_keeps_siblings_when_backup_is_inside_root(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory); (root / "backups").mkdir(); (root / "backups" / "old.zip").write_bytes(b"x"); (root / "catalog.json").write_bytes(b"x")
			self.assertEqual([path.name for path in _safe_sources(root, root / "backups")], ["catalog.json"])