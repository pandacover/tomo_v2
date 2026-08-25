from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import MessageAttachment
from .vision import DownloadedAttachment


class AttachmentReadError(ValueError):
    """A safe failure to read a control-plane attachment."""


class AttachmentOpener(Protocol):
    def __call__(self, request: Request, timeout: float): ...


@dataclass
class ControlAttachmentReader:
    control_url: str
    capability: str
    owner_id: str
    generation_id: str
    opener: AttachmentOpener = urlopen

    @classmethod
    def from_env(cls) -> "ControlAttachmentReader":
        values = (
            os.getenv("TOMO_ATTACHMENT_CONTROL_URL"),
            os.getenv("TOMO_ATTACHMENT_CAPABILITY"),
            os.getenv("TOMO_ATTACHMENT_OWNER_ID"),
            os.getenv("TOMO_ATTACHMENT_GENERATION_ID"),
        )
        if not _valid_context(*values):
            raise AttachmentReadError("attachment_context_invalid")
        return cls(*values)

    def read(self, attachment: MessageAttachment) -> DownloadedAttachment:
        if not _valid_context(self.control_url, self.capability, self.owner_id, self.generation_id) or not isinstance(attachment.file_id, str) or not attachment.file_id:
            raise AttachmentReadError("attachment_invalid")
        request = Request(self.control_url.rstrip("/") + "/v1/attachments/resolve", data=json.dumps({"fileId": attachment.file_id}).encode("utf-8"), method="POST", headers={"Authorization": f"Bearer {self.capability}", "X-Tomo-Owner-Id": self.owner_id, "X-Tomo-Generation-Id": self.generation_id, "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=10) as response:
                mime_type = response.headers.get_content_type()
                if not mime_type.startswith("image/"):
                    raise AttachmentReadError("attachment_unavailable")
                data = response.read(10 * 1024 * 1024 + 1)
        except AttachmentReadError:
            raise
        except HTTPError as error:
            code = "attachment_auth_failed" if error.code == 401 else "attachment_source_failed" if error.code == 503 else "attachment_unavailable"
            raise AttachmentReadError(code) from None
        except (URLError, OSError, ValueError):
            raise AttachmentReadError("attachment_unavailable") from None
        if len(data) > 10 * 1024 * 1024:
            raise AttachmentReadError("attachment_too_large")
        return DownloadedAttachment(data, mime_type)


def _valid_context(control_url: object, capability: object, owner_id: object, generation_id: object) -> bool:
    parsed = urlparse(control_url) if isinstance(control_url, str) else None
    secure_control = bool(
        parsed
        and (
            (parsed.scheme == "https" and parsed.netloc)
            or (parsed.scheme == "http" and parsed.netloc == "tomo.control")
        )
        and not parsed.username
        and not parsed.password
    )
    return bool(secure_control and all(isinstance(value, str) and value for value in (capability, owner_id, generation_id)))
