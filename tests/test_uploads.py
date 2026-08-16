"""Tests for safe recent dashboard upload resolution."""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from inventory.uploads import (
	RecentDashboardUploadError,
	mark_dashboard_uploads_consumed,
	resolve_recent_dashboard_uploads,
)


class DashboardUploadResolverTests(unittest.TestCase):
	def setUp(self):
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name)
		self.images = self.root / "images"
		self.state = self.root / "dashboard-upload-state.json"
		self.images.mkdir()
		self.now = datetime(2026, 8, 16, 3, 20, tzinfo=UTC).timestamp()

	def tearDown(self):
		self.temporary_directory.cleanup()

	def add_image(self, name, *, mtime=None):
		path = self.images / name
		path.write_bytes(b"image")
		if mtime is not None:
			os.utime(path, (mtime, mtime))
		return path

	def resolve(self, **kwargs):
		return resolve_recent_dashboard_uploads(
			now=self.now,
			images_dir=self.images,
			state_path=self.state,
			**kwargs,
		)

	def test_selects_one_recent_dashboard_image(self):
		path = self.add_image("dashboard_20260816_031900_item.png")

		self.assertEqual(self.resolve(), [path.resolve()])

	def test_ignores_old_non_dashboard_and_unsupported_files(self):
		self.add_image("dashboard_20260816_030900_old.png")
		self.add_image("photo_20260816_031915.png")
		self.add_image("dashboard_20260816_031915_old.txt")

		with self.assertRaisesRegex(RecentDashboardUploadError, "No unconsumed"):
			self.resolve()

	def test_groups_recent_upload_burst_in_chronological_order(self):
		first = self.add_image("dashboard_20260816_031912_first.jpg")
		second = self.add_image("dashboard_20260816_031918_second.png")
		third = self.add_image("dashboard_20260816_031922_third.webp")
		self.add_image("dashboard_20260816_031850_earlier.bmp")

		self.assertEqual(self.resolve(), [first.resolve(), second.resolve(), third.resolve()])

	def test_does_not_group_images_outside_burst(self):
		old = self.add_image("dashboard_20260816_031900_old.jpg")
		new = self.add_image("dashboard_20260816_031915_new.jpg")

		self.assertEqual(self.resolve(burst_seconds=10), [new.resolve()])
		self.assertNotIn(old.resolve(), self.resolve(burst_seconds=10))

	def test_excludes_consumed_files(self):
		consumed = self.add_image("dashboard_20260816_031915_consumed.png")
		available = self.add_image("dashboard_20260816_031916_available.png")
		mark_dashboard_uploads_consumed(
			[consumed.resolve()],
			now=self.now,
			state_path=self.state,
		)

		self.assertEqual(self.resolve(), [available.resolve()])

	def test_reports_when_no_recent_image_exists(self):
		with self.assertRaisesRegex(RecentDashboardUploadError, "No unconsumed"):
			self.resolve()

	def test_malformed_filename_falls_back_to_mtime(self):
		path = self.add_image("dashboard_not-a-timestamp.png", mtime=self.now - 5)

		self.assertEqual(self.resolve(), [path.resolve()])

	def test_unavailable_mtime_is_skipped_without_crashing(self):
		path = self.add_image("dashboard_not-a-timestamp.png")
		with patch("inventory.uploads.Path.stat", side_effect=OSError("gone")):
			with self.assertRaises(RecentDashboardUploadError):
				self.resolve()
		self.assertTrue(path.exists())

	def test_state_entries_older_than_retention_are_pruned_on_write(self):
		old_path = self.root / "old.png"
		self.state.write_text(
			json.dumps({"consumed": {str(old_path): self.now - 8 * 24 * 60 * 60}}),
			encoding="utf-8",
		)
		available = self.add_image("dashboard_20260816_031916_available.png")
		mark_dashboard_uploads_consumed(
			[available.resolve()],
			now=self.now,
			state_path=self.state,
		)

		payload = json.loads(self.state.read_text(encoding="utf-8"))
		self.assertNotIn(str(old_path), payload["consumed"])
		self.assertIn(str(available.resolve()), payload["consumed"])

	def test_malformed_state_is_reported_clearly(self):
		self.state.write_text("[]", encoding="utf-8")

		with self.assertRaisesRegex(RecentDashboardUploadError, "state is malformed"):
			self.resolve()


if __name__ == "__main__":
	unittest.main()
