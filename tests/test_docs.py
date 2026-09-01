"""Documentation contracts for critical installation and path guidance."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
	def test_readme_has_separate_windows_and_container_workflows(self):
		readme = (ROOT / "README.md").read_text(encoding="utf-8")
		for text in (
			"## Quick Start: Windows Hermes Desktop",
			"%LOCALAPPDATA%\\hermes\\plugins",
			"## Quick Start: Docker/Container Hermes Agent",
			"HOST PATH:",
			"CONTAINER PATH:",
			"PLUGIN PATH:",
			"/opt/data/images",
			"INVENTORY_BASE_DIR=/inventory-data",
			"hermes inventory setup --secrets",
		):
			self.assertIn(text, readme)
		self.assertNotIn("INVENTORY_BASE_DIR=Z:\\", readme)

	def test_readme_limits_automatic_media_scanning_and_rejects_chat_secrets(self):
		readme = (ROOT / "README.md").read_text(encoding="utf-8")
		for text in (
			"only `$HERMES_HOME/images`",
			"`dashboard_*`, `upload_*`, and `clip_*`",
			"`$HERMES_HOME/media`, `$HERMES_HOME/image_cache`, and `$HERMES_HOME/user_media` are never automatically scanned",
			"never accepted as slash-command arguments",
		):
			self.assertIn(text, readme)