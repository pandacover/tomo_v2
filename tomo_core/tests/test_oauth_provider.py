import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from tomo_core.models import MessageAttachment
from tomo_core.oauth import OAuthManager, OAuthProviderConfig
from tomo_core.providers import OAuthBackedSuperGrokProvider, ProviderSetupRequired, ProviderStreamCompleted, ProviderTextDelta
from tomo_core.vision import DownloadedAttachment, ProviderVisionInterpreter


class OAuthBackedProviderTests(unittest.TestCase):
    def test_connected_actor_token_is_reused_for_local_vision_without_a_second_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(data_dir=tmp, providers={"supergrok": OAuthProviderConfig("supergrok", "client", "https://auth.example/authorize", "https://auth.example/token", "http://127.0.0.1:56120/callback", ("openid",))})
            (Path(tmp) / "oauth" / "token_supergrok_telegram-actor.json").write_text(json.dumps({"access_token": "actor-access"}), encoding="utf-8")
            image = BytesIO(); Image.new("RGB", (10, 10), "red").save(image, format="PNG")

            class Reader:
                def read(self, attachment): return DownloadedAttachment(image.getvalue(), "image/png")

            class Provider:
                name = "oauth"; supports_images_in = True; supports_images_out = False; supports_tool_calls = False
                def stream(self, messages, *, tools=(), actor_id=None):
                    return OAuthBackedSuperGrokProvider(oauth=oauth).stream(messages, tools=tools, actor_id=actor_id)

            class FakeResponse:
                def __enter__(self): return self
                def __exit__(self, *args): return None
                def raise_for_status(self): return None
                def iter_raw(self): yield b'data: {"choices":[{"delta":{"content":"{\\"summary\\":\\"red image\\",\\"visible_text\\":[],\\"relevant_details\\":[],\\"uncertainties\\":[]}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

            with patch("tomo_core.providers.httpx.stream", return_value=FakeResponse()) as stream:
                observation = ProviderVisionInterpreter(Provider(), Reader()).observe(MessageAttachment("image", "photo"), "what is shown?", message_id="message", attachment_index=0, actor_id="telegram-actor")

        self.assertEqual(observation.summary, "red image")
        self.assertEqual(stream.call_args.kwargs["headers"]["Authorization"], "Bearer actor-access")

    def test_connected_supergrok_token_is_used_for_actor_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(
                data_dir=tmp,
                providers={
                    "supergrok": OAuthProviderConfig(
                        provider="supergrok",
                        client_id="supergrok-client",
                        auth_url="https://auth.example/authorize",
                        token_url="https://auth.example/token",
                        redirect_uri="http://127.0.0.1:56120/callback",
                        scopes=("openid",),
                    )
                },
            )
            token_path = Path(tmp) / "oauth" / "token_supergrok_99.json"
            token_path.write_text(json.dumps({"access_token": "actor-access"}), encoding="utf-8")

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def raise_for_status(self):
                    return None

                def iter_raw(self):
                    yield b'data: {"choices":[{"delta":{"content":"real supergrok reply"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

            with patch("tomo_core.providers.httpx.stream", return_value=FakeResponse()) as stream:
                reply = OAuthBackedSuperGrokProvider(oauth=oauth).complete([{"role": "user", "content": "hi"}], actor_id="99")

            self.assertEqual(reply, "real supergrok reply")
            self.assertEqual(stream.call_args.kwargs["headers"]["Authorization"], "Bearer actor-access")
            request_body = stream.call_args.kwargs["json"]
            self.assertEqual(request_body["reasoning_effort"], "high")
            self.assertNotIn("reasoning", request_body)

    def test_missing_supergrok_token_returns_connect_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(data_dir=tmp, providers={})

            with self.assertRaises(ProviderSetupRequired) as raised:
                OAuthBackedSuperGrokProvider(oauth=oauth).complete([{"role": "user", "content": "hi"}], actor_id="99")

            self.assertIn("/connect", raised.exception.user_message)
            self.assertIn("supergrok", raised.exception.user_message)


if __name__ == "__main__":
    unittest.main()
