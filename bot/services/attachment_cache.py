"""Keep a message's image bytes around so they survive the message being deleted.

Discord removes a message's attachments from the CDN the moment the message
goes away, so by the time ``on_message_delete`` fires the ``attachment.url`` is
already a dead link. To still be able to show the pictures in a log channel we
download the payload while the message is alive and hold it in a small bounded
cache keyed by message id.

The cache is intentionally in-memory and lossy: it is a best-effort buffer for
"deleted within the last few minutes", not an archive. Callers should treat
``pop_message_images`` returning nothing as normal.
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict

import discord

log = logging.getLogger(__name__)

MAX_MESSAGES = 250
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024

IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".heic", ".tiff",
)

_cache: OrderedDict[int, list[tuple[str, bytes]]] = OrderedDict()
_bytes_held = 0


def _is_image(attachment) -> bool:
    """True for attachments that look like a picture we can show in a log."""
    if (getattr(attachment, "content_type", None) or "").startswith("image/"):
        return True
    name = (getattr(attachment, "filename", "") or "").lower()
    return name.endswith(IMAGE_SUFFIXES)


async def _read(attachment) -> bytes | None:
    """Fetch the payload, skipping anything too big or unreadable.

    ``Attachment.to_bytes`` is the modern entry point but is not available on
    every supported discord.py release, hence the fallback.
    """
    size = getattr(attachment, "size", None)
    if isinstance(size, int) and size > MAX_IMAGE_BYTES:
        return None
    try:
        if hasattr(attachment, "to_bytes"):
            return await attachment.to_bytes()
        return await attachment.read()
    except Exception as exc:  # network hiccup, vanished file, 403 on a private one
        log.debug("attachment cache: could not read %r (%s)", attachment, exc)
        return None


def _evict() -> None:
    """Drop the oldest entries until both limits are satisfied again."""
    global _bytes_held
    while _cache and (len(_cache) > MAX_MESSAGES or _bytes_held > MAX_TOTAL_BYTES):
        _, dropped = _cache.popitem(last=False)
        _bytes_held -= sum(len(data) for _, data in dropped)


def _store(message_id: int, images: list[tuple[str, bytes]]) -> None:
    global _bytes_held
    discard_message_images(message_id)
    _cache[message_id] = images
    _bytes_held += sum(len(data) for _, data in images)
    _evict()


async def cache_message_images(message) -> int:
    """Download and remember the pictures attached to *message*.

    Returns how many images were stored (0 when the message has none worth
    keeping). Failures are swallowed: a missing picture must never interrupt
    message handling.
    """
    attachments = getattr(message, "attachments", None)
    if not attachments or getattr(message.author, "bot", False):
        return 0

    images: list[tuple[str, bytes]] = []
    for attachment in list(attachments)[:MAX_IMAGES_PER_MESSAGE]:
        if not _is_image(attachment):
            continue
        data = await _read(attachment)
        if data:
            images.append((getattr(attachment, "filename", None) or "image.png", data))

    if images:
        _store(message.id, images)
    return len(images)


def pop_message_images(message_id: int) -> list[tuple[str, bytes]]:
    """Take the cached images for a message and forget them."""
    global _bytes_held
    images = _cache.pop(message_id, [])
    if images:
        _bytes_held -= sum(len(data) for _, data in images)
    return images


def discard_message_images(message_id: int) -> None:
    """Forget a message's images without handing them back."""
    pop_message_images(message_id)


def has_message_images(message_id: int) -> bool:
    return message_id in _cache


def to_files(images: list[tuple[str, bytes]]) -> list[discord.File]:
    """Wrap cached images as fresh upload objects (a File can only be sent once)."""
    return [discord.File(io.BytesIO(data), filename=name) for name, data in images]


def clear() -> None:
    """Release everything — called on cog unload so the memory comes back."""
    global _bytes_held
    _cache.clear()
    _bytes_held = 0


def stats() -> dict:
    """Cache occupancy, for diagnostics and tests."""
    return {"messages": len(_cache), "bytes": _bytes_held}
