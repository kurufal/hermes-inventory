"""Documentation contracts for critical installation and path guidance."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
	def test_readme_uses_shared_hermes_dotenv_and_homebox_generated_key(self):
		content = (ROOT / "README.md").read_text(encoding="utf-8")
		for text in ("$HERMES_HOME/.env", "~/.hermes/.env", "HOMEBOX_URL=http://your-homebox-host:3100", "HOMEBOX_API_KEY=your_homebox_generated_key", "HomeBox, not Hermes", "API_SERVER_KEY", "/inventory setup test"):
			self.assertIn(text, content)

	def test_workflow_guide_links_to_setup_instead_of_duplicating_it(self):
		content = (ROOT / "docs/flows.md").read_text(encoding="utf-8")
		self.assertIn("[README](../README.md)", content)
		self.assertNotIn("HOMEBOX_API_KEY=", content)

	def test_mermaid_node_labels_are_quoted(self):
		for path in ROOT.rglob("*.md"):
			for diagram in re.findall(r"```mermaid\s*\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL):
				with self.subTest(path=path):
					self.assertNotRegex(diagram, r'\[(?!")|\{(?!")')

	def test_readme_has_separate_windows_and_container_workflows(self):
		readme = (ROOT / "README.md").read_text(encoding="utf-8")
		for text in (
			"## 1. Install",
			"1.1 Windows Hermes Desktop",
			"%LOCALAPPDATA%\\hermes\\plugins",
			"1.2 Docker / Container Hermes Agent",
			"Docker host path",
			"Container path",
			"/srv/hermes/plugins/hermes-inventory",
			"/opt/hermes/images",
			"INVENTORY_BASE_DIR=/inventory-data",
			"hermes inventory setup --secrets",
		):
			self.assertIn(text, readme)
		self.assertNotIn("INVENTORY_BASE_DIR=Z:\\", readme)

	def test_readme_has_collapsible_numbered_setup_and_execution_context(self):
		readme = (ROOT / "README.md").read_text(encoding="utf-8")
		sections = re.findall(r"<details>\s*<summary>(.*?)</summary>(.*?)</details>", readme, re.DOTALL)
		self.assertEqual(len(sections), 5)
		self.assertEqual(readme.count("<details>"), readme.count("</details>"))
		for text in ("1.1.1", "1.2.1", "## 2. Configure", "2.1.1", "## 3. Verify and Use", "Docker host, not inside the container", "inside the container", "No `docker exec` is needed", "Hermes chat", "persistent named volume", "`localhost` refers to that container"):
			self.assertIn(text, readme)

	def test_readme_describes_request_only_images_and_rejects_chat_secrets(self):
		readme = (ROOT / "README.md").read_text(encoding="utf-8")
		for text in (
			"same message",
			"only the images attached to the current request",
			"No background watcher",
			"Inventory does not scan",
			"never guesses the newest image",
			"does not create new ones",
			"never accepted as slash-command arguments",
		):
			self.assertIn(text, readme)
		self.assertNotIn("Automatic detection watches", readme)