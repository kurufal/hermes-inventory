import unittest
from unittest.mock import patch

from inventory.duplicates import check_homebox_duplicates


class DuplicateEnumerationTests(unittest.TestCase):
	def test_duplicate_on_later_authoritative_page_is_detected(self):
		record = {"name": "Camera", "manufacturer": "Hermes", "identifiers": {"serial_number": ["SER-2"]}, "image_hashes": []}
		first = {"id": "first", "name": "Other", "fields": []}
		second = {"id": "second", "name": "Camera", "manufacturer": "Hermes", "fields": [{"name": "Serial Number", "type": "text", "textValue": "SER-2"}]}
		with patch("inventory.homebox.list_all_entities", return_value=[{"id": "first"}, {"id": "second"}]) as enumerate_all, patch("inventory.homebox.get_entity", side_effect=[first, second]):
			result = check_homebox_duplicates(record)
		self.assertEqual(result["classification"], "SAME_PHYSICAL_UNIT")
		enumerate_all.assert_called_once_with()