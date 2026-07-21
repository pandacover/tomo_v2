import unittest

from tomo_core.telegram_media import photo_attachment_from_message


class TelegramMediaTests(unittest.TestCase):
    def test_selects_largest_valid_photo_and_safe_metadata(self):
        attachment = photo_attachment_from_message({"photo": [{"file_id": "small", "width": 90, "height": 90}, {"file_id": "large", "width": 900, "height": 900, "file_size": 1234}]})
        self.assertEqual(attachment.file_id, "large")
        self.assertEqual(attachment.metadata["file_size"], 1234)

    def test_rejects_malformed_photo_arrays(self):
        self.assertIsNone(photo_attachment_from_message({"photo": [{"file_id": ""}]}))
