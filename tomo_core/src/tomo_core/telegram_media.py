from __future__ import annotations

from typing import Any

from .models import MessageAttachment


def photo_attachment_from_message(message: dict[str, Any]) -> MessageAttachment | None:
    photos = message.get("photo")
    if not isinstance(photos, list):
        return None
    candidates = [photo for photo in photos if isinstance(photo, dict) and isinstance(photo.get("file_id"), str) and photo["file_id"].strip()]
    if not candidates:
        return None
    def area(photo: dict[str, Any]) -> int:
        width, height = photo.get("width"), photo.get("height")
        return width * height if isinstance(width, int) and not isinstance(width, bool) and isinstance(height, int) and not isinstance(height, bool) and width >= 0 and height >= 0 else 0
    photo = max(candidates, key=area)
    metadata = {key: photo[key] for key in ("width", "height", "file_size", "file_unique_id") if key in photo and isinstance(photo[key], (int, str)) and not isinstance(photo[key], bool)}
    return MessageAttachment(kind="image", file_id=photo["file_id"], mime_type="image/jpeg", metadata=metadata)
