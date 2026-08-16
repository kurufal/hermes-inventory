"""Tests for plugin-owned pending dashboard upload state."""

import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from inventory import uploads
from inventory.uploads import (
	PendingUploadError,
	mark_pending_upload_consumed,
	observe_dashboard_uploads,
	resolve_pending_upload_batch,
	start_pending_upload_watcher,
	stop_pending_upload_watcher,
)


class PendingUploadTests(unittest.TestCase):
	def setUp(self):
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name)
		self.images = self.root / "images"
		self.state = self.root / "pending-uploads.json"
		self.images.mkdir()
		self.now = datetime(2026, 8, 16, 8, 41, tzinfo=UTC).timestamp()

	def tearDown(self):
		stop_pending_upload_watcher()
		self.temporary_directory.cleanup()

	def add_image(self, name, content=b"image"):
		path = self.images / name
		path.write_bytes(content)
		return path

	def observe(self, **kwargs):
		options = {
			"now": self.now,
			"images_dir": self.images,
			"state_path": self.state,
			"stability_delay_seconds": 0,
		}
		options.update(kwargs)
		return observe_dashboard_uploads(**options)

	def read_state(self):
		return json.loads(self.state.read_text(encoding="utf-8"))

	def resolve_batch(self, **kwargs):
		return resolve_pending_upload_batch(
			images_dir=self.images,
			state_path=self.state,
			**kwargs,
		)

	def test_new_dashboard_image_creates_pending_batch(self):
		path = self.add_image("dashboard_20260816_084100_front.jpg")

		self.assertEqual(self.observe(), 1)
		state = self.read_state()
		self.assertEqual(state["version"], 1)
		self.assertEqual(len(state["batches"]), 1)
		batch = state["batches"][0]
		self.assertEqual(batch["status"], "pending")
		self.assertEqual(batch["images"][0]["path"], str(path.resolve()))
		self.assertEqual(batch["images"][0]["size"], len(b"image"))
		self.assertEqual(batch["images"][0]["dashboard_timestamp"], "2026-08-16T08:41:00Z")

	def test_non_dashboard_and_unsupported_files_are_ignored(self):
		self.add_image("photo.jpg")
		self.add_image("dashboard_20260816_084100_notes.txt")

		self.assertEqual(self.observe(), 0)
		self.assertEqual(self.read_state(), {"version": 1, "batches": []})

	def test_malformed_dashboard_filename_uses_detected_time(self):
		path = self.add_image("dashboard_not-a-timestamp.png")

		self.assertEqual(self.observe(), 1)
		image = self.read_state()["batches"][0]["images"][0]
		self.assertIsNone(image["dashboard_timestamp"])
		self.assertEqual(image["path"], str(path.resolve()))

	def test_multiple_uploads_within_batch_window_form_one_batch(self):
		first = self.add_image("dashboard_20260816_084100_front.jpg")
		self.assertEqual(self.observe(now=self.now), 1)
		second = self.add_image("dashboard_20260816_084103_back.jpg")
		self.assertEqual(
			observe_dashboard_uploads(
				now=self.now + 3,
				images_dir=self.images,
				state_path=self.state,
				stability_delay_seconds=0,
			),
			1,
		)

		batches = self.read_state()["batches"]
		self.assertEqual(len(batches), 1)
		self.assertEqual(
			[item["path"] for item in batches[0]["images"]],
			[str(first.resolve()), str(second.resolve())],
		)

	def test_upload_outside_batch_window_creates_second_batch(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)
		self.add_image("dashboard_20260816_084120_other.jpg")
		self.assertEqual(self.observe(now=self.now + 20), 1)

		self.assertEqual(len(self.read_state()["batches"]), 2)

	def test_newest_pending_batch_resolves_with_chronological_images(self):
		self.add_image("dashboard_20260816_084100_old.jpg")
		self.observe(now=self.now)
		new_first = self.add_image("dashboard_20260816_084120_front.jpg")
		new_second = self.add_image("dashboard_20260816_084122_label.jpg")
		self.observe(now=self.now + 20)

		batch = self.resolve_batch(now=self.now + 20)
		self.assertEqual(
			list(batch.image_paths),
			[new_first.resolve(), new_second.resolve()],
		)

	def test_expired_batch_is_not_returned(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)

		with self.assertRaisesRegex(PendingUploadError, "No recent pending"):
			self.resolve_batch(now=self.now + 301, ttl_seconds=300)
		self.assertEqual(self.read_state()["batches"][0]["status"], "expired")

	def test_consumed_batch_is_not_returned(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)
		batch = self.resolve_batch(now=self.now)
		mark_pending_upload_consumed(batch.batch_id, state_path=self.state)

		with self.assertRaises(PendingUploadError):
			self.resolve_batch(now=self.now)
		self.assertEqual(self.read_state()["batches"][0]["status"], "consumed")

	def test_consuming_batch_persists_valid_json(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)
		batch = self.resolve_batch(now=self.now)
		mark_pending_upload_consumed(batch.batch_id, state_path=self.state)

		payload = json.loads(self.state.read_text(encoding="utf-8"))
		self.assertEqual(payload["batches"][0]["status"], "consumed")

	def test_malformed_state_is_backed_up_and_recovered(self):
		self.state.write_text("not json", encoding="utf-8")
		path = self.add_image("dashboard_20260816_084100_front.jpg")

		self.assertEqual(self.observe(now=self.now), 1)
		self.assertTrue(list(self.root.glob("pending-uploads.json.corrupt-*")))
		self.assertEqual(self.read_state()["batches"][0]["images"][0]["path"], str(path.resolve()))

	def test_old_consumed_and_expired_records_are_pruned(self):
		old_time = self.now - 90000
		self.state.write_text(
			json.dumps(
				{
					"version": 1,
					"batches": [
						{
							"batch_id": "old-consumed",
							"created_at": uploads._iso(old_time),
							"updated_at": uploads._iso(old_time),
							"expires_at": uploads._iso(old_time),
							"status": "consumed",
							"images": [],
						},
						{
							"batch_id": "old-expired",
							"created_at": uploads._iso(old_time),
							"updated_at": uploads._iso(old_time),
							"expires_at": uploads._iso(old_time),
							"status": "expired",
							"images": [],
						},
				],
				}
			),
			encoding="utf-8",
		)
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)

		self.assertEqual(len(self.read_state()["batches"]), 1)
		self.assertEqual(self.read_state()["batches"][0]["status"], "pending")

	def test_missing_image_referenced_by_pending_state_is_rejected(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)
		path = self.images / "dashboard_20260816_084100_front.jpg"
		path.unlink()

		with self.assertRaisesRegex(PendingUploadError, "missing or invalid"):
			self.resolve_batch(now=self.now)
		self.assertEqual(self.read_state()["batches"][0]["status"], "expired")

	def test_detection_then_unrelated_time_keeps_batch_pending_until_ttl(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)

		batch = self.resolve_batch(now=self.now + 100, ttl_seconds=300)
		self.assertEqual(batch.batch_id, self.read_state()["batches"][0]["batch_id"])

	def test_ttl_expiration_prevents_resolution(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		self.observe(now=self.now)

		with self.assertRaises(PendingUploadError):
			self.resolve_batch(now=self.now + 301, ttl_seconds=300)

	def test_stability_check_skips_file_that_changes_during_check(self):
		path = self.add_image("dashboard_20260816_084100_front.jpg")
		original_stat = Path.stat
		calls = {"count": 0}

		def changing_stat():
			if calls["count"] == 1:
				path.write_bytes(b"changed")
			calls["count"] += 1
			return original_stat(path)

		with patch("inventory.uploads.Path.stat", side_effect=changing_stat):
			self.assertFalse(uploads._is_stable(path, delay_seconds=0))

	def test_thread_safe_concurrent_observation_records_one_image(self):
		self.add_image("dashboard_20260816_084100_front.jpg")
		results = []
		threads = [
			threading.Thread(target=lambda: results.append(self.observe()))
			for _ in range(4)
		]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()

		self.assertEqual(sum(results), 1)
		self.assertEqual(len(self.read_state()["batches"][0]["images"]), 1)


class WatcherLifecycleTests(unittest.TestCase):
	def tearDown(self):
		stop_pending_upload_watcher()
		with uploads._WATCHER_LOCK:
			thread = uploads._WATCHER_THREAD
		if thread is not None:
			thread.join(timeout=1)
			uploads._WATCHER_THREAD = None

	def test_duplicate_watcher_startup_is_prevented(self):
		entered = threading.Event()

		def blocking_loop(stop_event, interval_seconds, logger):
			del interval_seconds, logger
			entered.set()
			stop_event.wait(1)

		with patch.object(uploads, "_watcher_loop", side_effect=blocking_loop) as loop:
			self.assertTrue(start_pending_upload_watcher(interval_seconds=1))
			self.assertTrue(entered.wait(1))
			self.assertFalse(start_pending_upload_watcher(interval_seconds=1))
			stop_pending_upload_watcher()
			with uploads._WATCHER_LOCK:
				thread = uploads._WATCHER_THREAD
			thread.join(timeout=1)
			loop.assert_called_once()

	def test_watcher_detects_new_file(self):
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			images = root / "images"
			state = root / "pending-uploads.json"
			images.mkdir()
			path = images / "dashboard_20260816_084100_front.jpg"
			path.write_bytes(b"image")

			def observe_temp(**kwargs):
				return observe_dashboard_uploads(
					images_dir=images,
					state_path=state,
					stability_delay_seconds=0,
					**kwargs,
				)

			with patch.object(
				uploads,
				"observe_dashboard_uploads",
				side_effect=observe_temp,
			) as observe:
				self.assertTrue(start_pending_upload_watcher(interval_seconds=0.01))
				deadline = time.monotonic() + 1
				while (
					(observe.call_count == 0 or not state.exists())
					and time.monotonic() < deadline
				):
					time.sleep(0.01)
				stop_pending_upload_watcher()
				self.assertGreater(observe.call_count, 0)
				self.assertTrue(state.exists())


if __name__ == "__main__":
	unittest.main()
