import unittest
from urllib.error import HTTPError, URLError

from tomo_core.attachment_reader import AttachmentReadError, ControlAttachmentReader
from tomo_core.models import MessageAttachment


class ControlAttachmentReaderTests(unittest.TestCase):
    def test_requires_attachment_file_id(self):
        reader = ControlAttachmentReader("https://control.example", "capability", "owner", "generation")
        with self.assertRaises(AttachmentReadError):
            reader.read(MessageAttachment("image"))

    def test_reads_image_bytes_and_rejects_non_images_or_oversize(self):
        class Headers:
            def __init__(self, mime): self.mime = mime
            def get_content_type(self): return self.mime
        class Response:
            def __init__(self, data, mime="image/jpeg"): self.data, self.headers = data, Headers(mime)
            def read(self, size): return self.data
            def __enter__(self): return self
            def __exit__(self, *args): pass
        attachment = MessageAttachment("image", file_id="file")
        reader = ControlAttachmentReader("https://control.example", "capability", "owner", "generation", lambda request, timeout: Response(b"jpeg"))
        self.assertEqual(reader.read(attachment).data, b"jpeg")
        internal = ControlAttachmentReader("http://tomo.control", "capability", "owner", "generation", lambda request, timeout: Response(b"jpeg"))
        self.assertEqual(internal.read(attachment).data, b"jpeg")
        for response, code in ((Response(b"x", "text/plain"), "attachment_invalid_mime"), (Response(b"x" * (10 * 1024 * 1024 + 1)), "attachment_too_large")):
            with self.subTest(code=code), self.assertRaisesRegex(AttachmentReadError, code):
                ControlAttachmentReader("https://control.example", "capability", "owner", "generation", lambda request, timeout: response).read(attachment)

    def test_reader_classifies_transport_and_invalid_context_without_leaks(self):
        attachment = MessageAttachment("image", file_id="secret-file")
        cases = (
            (HTTPError("https://secret", 401, "secret-body", {}, None), "attachment_auth_failed"),
            (HTTPError("https://secret", 413, "secret-body", {}, None), "attachment_http_4xx"),
            (HTTPError("https://secret", 500, "secret-body", {}, None), "attachment_http_5xx"),
            (URLError("secret-body"), "attachment_transport_failed"),
        )
        for error, code in cases:
            with self.subTest(error=error), self.assertRaises(AttachmentReadError) as raised:
                ControlAttachmentReader("https://control.example", "secret-capability", "owner", "generation", lambda request, timeout: (_ for _ in ()).throw(error)).read(attachment)
            self.assertEqual(str(raised.exception), code)
            self.assertNotIn("secret", str(raised.exception))
        for args in (("not-a-url", "cap", "owner", "generation"), ("http://control.example", "cap", "owner", "generation"), ("https://control.example", "", "owner", "generation")):
            with self.subTest(args=args), self.assertRaisesRegex(AttachmentReadError, "attachment_invalid"):
                ControlAttachmentReader(*args).read(attachment)
