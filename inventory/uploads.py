"""Safe resolution and consumption tracking for dashboard image uploads."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from inventory.config import (
	DASHBOARD_BURST_SECONDS,
	DASHBOARD_IMAGES_DIR,
	DASHBOARD_UPLOAD_STATE_PATH,
	DASHBOARD_UPLOAD_WINDOW_SECONDS,
)


_DASHBOARD_NAME_RE = re.compile(
	r"^dashboard_(?P<date>\d{8})_(?P<time>\d{6})(?:_|$)"
)
_SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_STATE_RETENTION_SECONDS = 7 * 24 * 60 * 60


class RecentDashboardUploadError(ValueError):
	"""Raised when no safe recent dashboard upload can be resolved."""


def _filename_timestamp(path: Path) -> float | None:
	match = _DASHBOARD_NAME_RE.match(path.name)
	if not match:
		return None

	try:
		value = datetime.strptime(
			f"{match.group('date')}_{match.group('time')}",
			"%Y%m%d_%H%M%S",
		).replace(tzinfo=UTC)
		return value.timestamp()
	except ValueError:
		return None


def _candidate_timestamp(path: Path) -> float:
	"""Use the Hermes filename timestamp, with mtime as a safe fallback."""

	timestamp = _filename_timestamp(path)
	if timestamp is not None:
		return timestamp

	try:
		mtime = path.stat().st_mtime
	except OSError as exc:
		raise RecentDashboardUploadError(
			f"Could not determine upload time for {path.name}: {exc}"
		) from exc

	if mtime <= 0:
		raise RecentDashboardUploadError(
			f"Could not determine a valid upload time for {path.name}"
		)
	return mtime


def _read_state(path: Path, now: float) -> dict[str, float]:
	if not path.exists():
		return {}

	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise RecentDashboardUploadError(
			f"Dashboard upload state is unreadable: {path}"
		) from exc

	if not isinstance(payload, dict) or "consumed" not in payload:
		raise RecentDashboardUploadError(
			f"Dashboard upload state is malformed: {path}"
		)

	consumed = payload["consumed"]
	if not isinstance(consumed, dict):
		raise RecentDashboardUploadError(
			f"Dashboard upload state is malformed: {path}"
		)

	result: dict[str, float] = {}
	cutoff = now - _STATE_RETENTION_SECONDS
	for raw_path, raw_time in consumed.items():
		if not isinstance(raw_path, str):
			continue
		try:
			if isinstance(raw_time, str):
				stamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
				value = stamp.timestamp()
			else:
				value = float(raw_time)
		except (TypeError, ValueError, OverflowError):
			continue
		if math.isfinite(value) and value >= cutoff:
			result[raw_path] = value
	return result


def _write_state(path: Path, consumed: dict[str, float], now: float) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	cutoff = now - _STATE_RETENTION_SECONDS
	payload = {
		"consumed": {
			raw_path: datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
			for raw_path, value in consumed.items()
			if value >= cutoff
		}
	}

	fd, temporary_name = tempfile.mkstemp(
		prefix=f".{path.name}.",
		suffix=".tmp",
		dir=str(path.parent),
	)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(payload, handle, indent=2, sort_keys=True)
			handle.write("\n")
		os.replace(temporary_name, path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def mark_dashboard_uploads_consumed(
	paths: list[Path],
	*,
	now: float | None = None,
	state_path: Path = DASHBOARD_UPLOAD_STATE_PATH,
) -> None:
	"""Record uploads after they have been staged for inventory processing."""

	current = time.time() if now is None else now
	state = _read_state(state_path, current)
	for path in paths:
		state[str(path.resolve())] = current
	_write_state(state_path, state, current)


def resolve_recent_dashboard_uploads(
	*,
	now: float | None = None,
	images_dir: Path = DASHBOARD_IMAGES_DIR,
	state_path: Path = DASHBOARD_UPLOAD_STATE_PATH,
	window_seconds: int = DASHBOARD_UPLOAD_WINDOW_SECONDS,
	burst_seconds: int = DASHBOARD_BURST_SECONDS,
) -> list[Path]:
	"""Return the newest safe dashboard upload burst in chronological order.

	Only direct children named ``dashboard_*`` with supported image extensions
	are considered. The filename timestamp is preferred because it represents
	the upload operation; filesystem mtime is used only for malformed names.
	"""

	current = time.time() if now is None else now
	if window_seconds < 0 or burst_seconds < 0:
		raise RecentDashboardUploadError("Dashboard upload timing values must be non-negative")

	try:
		images_root = images_dir.resolve(strict=True)
		if not images_root.is_dir():
			raise NotADirectoryError(str(images_root))
		entries = list(images_root.iterdir())
	except FileNotFoundError as exc:
		raise RecentDashboardUploadError(
			f"No recent dashboard uploads found in {images_dir}"
		) from exc
	except OSError as exc:
		raise RecentDashboardUploadError(
			f"Could not inspect dashboard upload directory {images_dir}: {exc}"
		) from exc

	consumed = _read_state(state_path, current)
	candidates: list[tuple[float, Path]] = []
	for path in entries:
		if (
			path.is_symlink()
			or not path.is_file()
			or not path.name.startswith("dashboard_")
			or path.suffix.lower() not in _SUPPORTED_IMAGES
		):
			continue

		resolved = path.resolve()
		if str(resolved) in consumed:
			continue

		try:
			upload_time = _candidate_timestamp(path)
		except RecentDashboardUploadError:
			# A file with a malformed name can still be handled safely by mtime;
			# an unavailable mtime is simply not a usable candidate.
			continue

		age = current - upload_time
		if age < -5 or age > window_seconds:
			continue
		candidates.append((upload_time, resolved))

	if not candidates:
		raise RecentDashboardUploadError(
			f"No unconsumed dashboard image uploaded within the last {window_seconds} seconds"
		)

	candidates.sort(key=lambda item: (item[0], str(item[1])))
	newest_time = candidates[-1][0]
	burst = [
		path
		for upload_time, path in candidates
		if newest_time - upload_time <= burst_seconds
	]
	if not burst:
		raise RecentDashboardUploadError("Could not resolve a safe recent dashboard upload")
	return burst
