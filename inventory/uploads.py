"""Plugin-owned pending state for Hermes dashboard image uploads."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inventory.config import (
	DASHBOARD_IMAGES_DIR,
	PENDING_UPLOAD_STATE_PATH,
	PENDING_UPLOAD_TTL_SECONDS,
	UPLOAD_BATCH_WINDOW_SECONDS,
	UPLOAD_STATE_RETENTION_SECONDS,
	UPLOAD_WATCH_INTERVAL_SECONDS,
)


_LOGGER = logging.getLogger("hermes_plugins.hermes_inventory.uploads")
_DASHBOARD_NAME_RE = re.compile(
	r"^dashboard_(?P<date>\d{8})_(?P<time>\d{6})(?:_|\.|$)"
)
_SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_STATE_VERSION = 1
_STABILITY_DELAY_SECONDS = 0.05

_STATE_LOCK = threading.RLock()
_WATCHER_LOCK = threading.Lock()
_WATCHER_STOP = threading.Event()
_WATCHER_THREAD: threading.Thread | None = None
_CLAIMED_BATCHES: set[str] = set()


class PendingUploadError(ValueError):
	"""Raised when pending upload state cannot provide a usable batch."""


@dataclass(frozen=True)
class PendingUploadBatch:
	"""A validated pending batch ready for the existing ingest pipeline."""

	batch_id: str
	image_paths: tuple[Path, ...]
	created_at: str
	updated_at: str
	expires_at: str


def _utc_now(timestamp: float | None = None) -> datetime:
	return datetime.fromtimestamp(
		time.time() if timestamp is None else timestamp,
		tz=UTC,
	)


def _iso(timestamp: float | None = None) -> str:
	return _utc_now(timestamp).isoformat().replace("+00:00", "Z")


def _timestamp(value: str) -> float:
	return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _filename_timestamp(path: Path) -> float | None:
	"""Return the optional timestamp embedded in a dashboard filename."""

	match = _DASHBOARD_NAME_RE.match(path.name)
	if not match:
		return None
	try:
		return datetime.strptime(
			f"{match.group('date')}_{match.group('time')}",
			"%Y%m%d_%H%M%S",
		).replace(tzinfo=UTC).timestamp()
	except ValueError:
		return None


def _default_state() -> dict[str, Any]:
	return {"version": _STATE_VERSION, "batches": []}


def _backup_corrupt_state(path: Path, logger: logging.Logger) -> None:
	if not path.exists():
		return
	backup = path.with_name(
		f"{path.name}.corrupt-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-"
		f"{uuid.uuid4().hex[:8]}"
	)
	try:
		os.replace(path, backup)
		logger.warning("Recovered malformed upload state as %s", backup)
	except OSError as exc:
		logger.warning("Could not back up malformed upload state %s: %s", path, exc)


def _read_state_locked(
	state_path: Path,
	logger: logging.Logger = _LOGGER,
) -> dict[str, Any]:
	if not state_path.exists():
		return _default_state()

	try:
		payload = json.loads(state_path.read_text(encoding="utf-8"))
		if (
			not isinstance(payload, dict)
			or payload.get("version") != _STATE_VERSION
			or not isinstance(payload.get("batches"), list)
		):
			raise ValueError("unexpected state shape")
		return payload
	except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
		logger.warning("Malformed pending upload state %s: %s", state_path, exc)
		_backup_corrupt_state(state_path, logger)
		return _default_state()


def _write_state_locked(state_path: Path, state: dict[str, Any]) -> None:
	state_path.parent.mkdir(parents=True, exist_ok=True)
	fd, temporary_name = tempfile.mkstemp(
		prefix=f".{state_path.name}.",
		suffix=".tmp",
		dir=str(state_path.parent),
	)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(state, handle, indent=2, sort_keys=True)
			handle.write("\n")
			handle.flush()
			try:
				os.fsync(handle.fileno())
			except OSError:
				pass
		os.replace(temporary_name, state_path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def _path_record(path: Path, detected_at: str) -> dict[str, Any]:
	stat = path.stat()
	dashboard_time = _filename_timestamp(path)
	return {
		"path": str(path.resolve()),
		"detected_at": detected_at,
		"dashboard_timestamp": _iso(dashboard_time) if dashboard_time is not None else None,
		"mtime": stat.st_mtime,
		"size": stat.st_size,
	}


def _record_sort_key(record: dict[str, Any]) -> tuple[float, float, str]:
	try:
		primary = _timestamp(record.get("dashboard_timestamp") or record["detected_at"])
	except (KeyError, TypeError, ValueError):
		primary = float("inf")
	try:
		mtime = float(record.get("mtime", 0))
	except (TypeError, ValueError):
		mtime = 0
	return primary, mtime, str(record.get("path", ""))


def _batch_updated_timestamp(batch: dict[str, Any]) -> float:
	try:
		return _timestamp(str(batch["updated_at"]))
	except (KeyError, TypeError, ValueError):
		return 0.0


def _expire_and_prune_locked(
	state: dict[str, Any],
	now: float,
	*,
	ttl_seconds: float = PENDING_UPLOAD_TTL_SECONDS,
	retention_seconds: float = UPLOAD_STATE_RETENTION_SECONDS,
	logger: logging.Logger = _LOGGER,
) -> bool:
	changed = False
	for batch in state["batches"]:
		if not isinstance(batch, dict):
			continue
		if batch.get("status") == "pending":
			try:
				expires = _timestamp(str(batch["expires_at"]))
			except (KeyError, TypeError, ValueError):
				expires = _batch_updated_timestamp(batch) + ttl_seconds
			new_expires = _iso(expires)
			if batch.get("expires_at") != new_expires:
				batch["expires_at"] = new_expires
				changed = True
			if now >= expires:
				batch["status"] = "expired"
				batch["updated_at"] = _iso(now)
				changed = True
				logger.info("Pending upload batch expired: %s", batch.get("batch_id"))

	kept: list[dict[str, Any]] = []
	for batch in state["batches"]:
		if not isinstance(batch, dict):
			changed = True
			continue
		if batch.get("status") == "pending":
			kept.append(batch)
			continue
		if now - _batch_updated_timestamp(batch) <= retention_seconds:
			kept.append(batch)
		else:
			changed = True
	state["batches"] = kept
	return changed


def _supported_dashboard_file(path: Path) -> bool:
	return (
		path.name.startswith("dashboard_")
		and path.suffix.lower() in _SUPPORTED_IMAGES
		and not path.is_symlink()
		and path.is_file()
	)


def _detection_sort_key(path: Path) -> tuple[float, str]:
	filename_time = _filename_timestamp(path)
	if filename_time is not None:
		return filename_time, str(path)
	try:
		return path.stat().st_mtime, str(path)
	except OSError:
		return float("inf"), str(path)


def _is_stable(path: Path, delay_seconds: float = _STABILITY_DELAY_SECONDS) -> bool:
	try:
		first = path.stat()
		if first.st_size <= 0:
			return False
		if delay_seconds > 0:
			time.sleep(delay_seconds)
		second = path.stat()
	except OSError:
		return False
	return (
		first.st_size == second.st_size
		and first.st_mtime_ns == second.st_mtime_ns
	)


def _event_timestamp(path: Path) -> float:
	"""Return the best-known real-world timestamp this file was uploaded at.

	Prefers the timestamp encoded in a ``dashboard_YYYYMMDD_HHMMSS_...``
	filename; falls back to filesystem mtime. This is the timestamp used for
	TTL/age acceptance and batch grouping — NOT ``time.time()`` at scan time,
	which would treat every file discovered in one filesystem scan as
	simultaneous regardless of how old it actually is.
	"""
	filename_time = _filename_timestamp(path)
	if filename_time is not None:
		return filename_time
	try:
		return path.stat().st_mtime
	except OSError:
		return float("-inf")


def _batch_last_event_timestamp(batch: dict[str, Any]) -> float:
	"""Return the most recent EVENT timestamp among a batch's recorded images."""
	best = float("-inf")
	for image in batch.get("images", []):
		if not isinstance(image, dict):
			continue
		ts = None
		dashboard_timestamp = image.get("dashboard_timestamp")
		if dashboard_timestamp:
			try:
				ts = _timestamp(str(dashboard_timestamp))
			except (TypeError, ValueError):
				ts = None
		if ts is None:
			try:
				ts = float(image.get("mtime"))
			except (TypeError, ValueError):
				ts = None
		if ts is not None and ts > best:
			best = ts
	return best


def observe_dashboard_uploads(
	*,
	now: float | None = None,
	images_dir: Path = DASHBOARD_IMAGES_DIR,
	state_path: Path = PENDING_UPLOAD_STATE_PATH,
	batch_window_seconds: float = UPLOAD_BATCH_WINDOW_SECONDS,
	ttl_seconds: float = PENDING_UPLOAD_TTL_SECONDS,
	retention_seconds: float = UPLOAD_STATE_RETENTION_SECONDS,
	stability_delay_seconds: float = _STABILITY_DELAY_SECONDS,
	logger: logging.Logger = _LOGGER,
) -> int:
	"""Record complete new, RECENT dashboard images and return the count recorded.

	A file is only ever recorded as a new pending upload when its own event
	timestamp (filename timestamp, else mtime) is within
	``[0, ttl_seconds]`` of *now*. This prevents old files already sitting in
	*images_dir* from being mistaken for new uploads merely because their
	prior state record was pruned or never existed, and prevents multiple
	unrelated historical uploads discovered in the same filesystem scan from
	being merged into one batch.
	"""

	current = time.time() if now is None else now
	try:
		entries = list(images_dir.resolve(strict=True).iterdir())
	except (FileNotFoundError, NotADirectoryError):
		return 0
	except OSError as exc:
		logger.warning("Could not inspect dashboard image directory %s: %s", images_dir, exc)
		return 0

	with _STATE_LOCK:
		state_missing = not state_path.exists()
		state = _read_state_locked(state_path, logger)
		state_missing = state_missing or not state_path.exists()
		changed = state_missing or _expire_and_prune_locked(
			state,
			current,
			ttl_seconds=ttl_seconds,
			retention_seconds=retention_seconds,
			logger=logger,
		)
		recorded_paths = {
			str(image.get("path"))
			for batch in state["batches"]
			if isinstance(batch, dict)
			for image in batch.get("images", [])
			if isinstance(image, dict)
		}

		candidates = sorted(
			(
				path.resolve()
				for path in entries
				if _supported_dashboard_file(path)
				and str(path.resolve()) not in recorded_paths
			),
			key=_detection_sort_key,
		)
		new_count = 0
		for path in candidates:
			if not _is_stable(path, stability_delay_seconds):
				continue

			event_timestamp = _event_timestamp(path)
			age = current - event_timestamp
			if not (0 <= age <= ttl_seconds):
				# Too old (or clock-skewed into the future) to be a genuine
				# new upload. Never resurrect stale files as pending; leave
				# them unrecorded so a legitimate recent upload elsewhere in
				# the directory is still considered on its own merits.
				continue

			try:
				detected_at = _iso(current)
				record = _path_record(path, detected_at)
			except OSError:
				continue

			pending = [
				batch
				for batch in state["batches"]
				if isinstance(batch, dict) and batch.get("status") == "pending"
			]

			target_batch = None
			target_last_event = float("-inf")
			for candidate_batch in pending:
				last_event = _batch_last_event_timestamp(candidate_batch)
				if last_event == float("-inf"):
					continue
				if abs(event_timestamp - last_event) <= batch_window_seconds:
					if target_batch is None or last_event > target_last_event:
						target_batch = candidate_batch
						target_last_event = last_event

			if target_batch is not None:
				target_batch.setdefault("images", []).append(record)
				target_batch["images"].sort(key=_record_sort_key)
				target_batch["updated_at"] = detected_at
				target_batch["expires_at"] = _iso(current + ttl_seconds)
				logger.info(
					"Added dashboard image to pending batch %s",
					target_batch.get("batch_id"),
				)
			else:
				target_batch = {
					"batch_id": f"{datetime.fromtimestamp(current, UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
					"created_at": detected_at,
					"updated_at": detected_at,
					"expires_at": _iso(current + ttl_seconds),
					"status": "pending",
					"images": [record],
				}
				state["batches"].append(target_batch)
				logger.info(
					"Created pending dashboard upload batch %s",
					target_batch["batch_id"],
				)
			recorded_paths.add(str(path))
			new_count += 1
			changed = True

		if changed:
			_write_state_locked(state_path, state)
		return new_count


def _batch_to_result(
	batch: dict[str, Any],
	images_dir: Path = DASHBOARD_IMAGES_DIR,
) -> PendingUploadBatch:
	images = batch.get("images", [])
	images_root = images_dir.resolve()
	paths: list[Path] = []
	for image in images:
		if not isinstance(image, dict) or not isinstance(image.get("path"), str):
			raise PendingUploadError("Pending upload batch contains malformed image metadata")
		raw_path = Path(image["path"])
		path = raw_path.resolve()
		if (
			raw_path.is_symlink()
			or not path.is_file()
			or path.parent != images_root
			or path.suffix.lower() not in _SUPPORTED_IMAGES
			or not path.name.startswith("dashboard_")
		):
			raise PendingUploadError(
				f"Pending dashboard image is missing or invalid: {path}"
			)
		paths.append(path)
	if not paths:
		raise PendingUploadError("Pending upload batch contains no images")
	batch_id = str(batch.get("batch_id", "")).strip()
	if not batch_id:
		raise PendingUploadError("Pending upload batch has no batch ID")
	return PendingUploadBatch(
		batch_id=batch_id,
		image_paths=tuple(paths),
		created_at=str(batch.get("created_at", "")),
		updated_at=str(batch.get("updated_at", "")),
		expires_at=str(batch.get("expires_at", "")),
	)


def _count_dashboard_candidates(images_dir: Path) -> int:
	try:
		entries = images_dir.resolve(strict=True).iterdir()
	except OSError:
		return 0
	return sum(1 for path in entries if _supported_dashboard_file(path))


def resolve_pending_upload_batch(
	*,
	now: float | None = None,
	images_dir: Path = DASHBOARD_IMAGES_DIR,
	state_path: Path = PENDING_UPLOAD_STATE_PATH,
	ttl_seconds: float = PENDING_UPLOAD_TTL_SECONDS,
	retention_seconds: float = UPLOAD_STATE_RETENTION_SECONDS,
	batch_window_seconds: float = UPLOAD_BATCH_WINDOW_SECONDS,
	stability_delay_seconds: float = _STABILITY_DELAY_SECONDS,
	claim: bool = False,
	logger: logging.Logger = _LOGGER,
) -> PendingUploadBatch:
	"""Resolve the newest valid unexpired pending batch.

	If no valid pending batch exists yet, this reconciles *images_dir*
	inline (the same logic the background watcher runs) before giving up.
	This removes the race where a user asks to inventory an upload
	immediately after it lands, before the watcher's next poll tick — or
	when the watcher has not run at all.
	"""

	current = time.time() if now is None else now
	attempted_reconciliation = False
	pending_batch_count = 0
	total_batch_count = 0

	while True:
		with _STATE_LOCK:
			state_missing = not state_path.exists()
			state = _read_state_locked(state_path, logger)
			state_missing = state_missing or not state_path.exists()
			changed = state_missing or _expire_and_prune_locked(
				state,
				current,
				ttl_seconds=ttl_seconds,
				retention_seconds=retention_seconds,
				logger=logger,
			)
			if changed:
				_write_state_locked(state_path, state)

			pending = [
				batch
				for batch in state["batches"]
				if isinstance(batch, dict)
				and batch.get("status") == "pending"
				and str(batch.get("batch_id", "")) not in _CLAIMED_BATCHES
			]
			pending.sort(key=_batch_updated_timestamp, reverse=True)
			pending_batch_count = len(pending)
			total_batch_count = len(state["batches"])

			if pending:
				batch = pending[0]
				try:
					result = _batch_to_result(batch, images_dir)
				except PendingUploadError:
					batch["status"] = "expired"
					batch["updated_at"] = _iso(current)
					_write_state_locked(state_path, state)
					raise
				try:
					expires_at = _timestamp(result.expires_at)
				except (TypeError, ValueError):
					expires_at = 0
				if expires_at > current:
					if claim:
						_CLAIMED_BATCHES.add(result.batch_id)
					return result
				batch["status"] = "expired"
				batch["updated_at"] = _iso(current)
				_write_state_locked(state_path, state)

		if attempted_reconciliation:
			break
		attempted_reconciliation = True
		try:
			observe_dashboard_uploads(
				now=current,
				images_dir=images_dir,
				state_path=state_path,
				batch_window_seconds=batch_window_seconds,
				ttl_seconds=ttl_seconds,
				retention_seconds=retention_seconds,
				stability_delay_seconds=stability_delay_seconds,
				logger=logger,
			)
		except Exception:
			logger.exception("Inline reconciliation before pending resolution failed")
			break

	error = PendingUploadError("No recent pending dashboard upload was found.")
	error.debug = {
		"state_file": str(state_path),
		"pending_batch_count": pending_batch_count,
		"total_batch_count": total_batch_count,
		"candidate_count": _count_dashboard_candidates(images_dir),
	}
	raise error


def mark_pending_upload_consumed(
	batch_id: str,
	*,
	state_path: Path = PENDING_UPLOAD_STATE_PATH,
	logger: logging.Logger = _LOGGER,
) -> None:
	"""Mark a staged pending batch consumed, including duplicate outcomes."""

	with _STATE_LOCK:
		state = _read_state_locked(state_path, logger)
		for batch in state["batches"]:
			if isinstance(batch, dict) and batch.get("batch_id") == batch_id:
				if batch.get("status") == "pending":
					batch["status"] = "consumed"
					batch["updated_at"] = _iso()
					_write_state_locked(state_path, state)
					logger.info("Pending upload batch consumed: %s", batch_id)
					_CLAIMED_BATCHES.discard(batch_id)
					return
				raise PendingUploadError(
					f"Pending upload batch is not pending: {batch_id}"
				)
		raise PendingUploadError(f"Pending upload batch not found: {batch_id}")


def release_pending_upload_claim(batch_id: str) -> None:
	with _STATE_LOCK:
		_CLAIMED_BATCHES.discard(batch_id)


def _watcher_loop(
	stop_event: threading.Event,
	interval_seconds: float,
	logger: logging.Logger,
) -> None:
	while not stop_event.is_set():
		try:
			observe_dashboard_uploads(logger=logger)
		except Exception:
			logger.exception("Pending upload watcher exception")
		stop_event.wait(max(interval_seconds, 0.05))


def start_pending_upload_watcher(
	*,
	logger: logging.Logger = _LOGGER,
	interval_seconds: float = UPLOAD_WATCH_INTERVAL_SECONDS,
) -> bool:
	"""Start the one process-local daemon watcher, if not already active."""

	global _WATCHER_THREAD
	with _WATCHER_LOCK:
		if _WATCHER_THREAD is not None and _WATCHER_THREAD.is_alive():
			logger.info("Pending upload watcher already active")
			return False
		_WATCHER_STOP.clear()
		_WATCHER_THREAD = threading.Thread(
			target=_watcher_loop,
			args=(_WATCHER_STOP, interval_seconds, logger),
			name="hermes-inventory-upload-watcher",
			daemon=True,
		)
		_WATCHER_THREAD.start()
		logger.info("Pending upload watcher started")
		return True


def stop_pending_upload_watcher() -> None:
	"""Signal the daemon watcher to stop; intended for tests and process teardown."""

	_WATCHER_STOP.set()
# End of module.




















