import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import export_grok_auth


class ExportGrokAuthTests(unittest.TestCase):
    def test_defaults_to_the_standard_grok_auth_path_and_names_the_hosted_variable(self):
        parser = export_grok_auth.build_parser()

        self.assertEqual(parser.parse_args([]).auth_path, Path.home() / ".grok" / "auth.json")
        self.assertIn("TOMO_SUPERGROK_OAUTH_JSON_B64", parser.description)

    def test_exports_auth_json_as_base64_without_printing_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(json.dumps({"access_token": "secret-access"}), encoding="utf-8")
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = export_grok_auth.main([str(auth_path)])

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(base64.b64decode(output.getvalue()).decode()), {"access_token": "secret-access"})
            self.assertNotIn("secret-access", output.getvalue())


if __name__ == "__main__":
    unittest.main()
