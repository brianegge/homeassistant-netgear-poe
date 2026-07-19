"""Shared multipart firmware-upload helpers.

Both the classic "/base/" UI (api_base_ui.py) and the JSON CGI API (api.py)
flash firmware by streaming a hand-built multipart body while reporting
progress. These helpers live here, rather than in either client module, so
both can import them without the JSON-CGI client having to import from
api_base_ui (which already imports from api.py — the reverse would be a cycle).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import aiohttp

# Firmware uploads stream the body in chunks so the progress bar can move as
# the image goes out. 64 KiB is fine-grained enough that even a slow classic
# switch (~10 KB/s) steps the bar every few seconds, and matches aiohttp's
# transport high-water mark so a slow reader applies backpressure per chunk.
_UPLOAD_CHUNK_BYTES = 64 * 1024


class _ProgressUpload(aiohttp.BytesPayload):
    """A multipart body that steps a progress callback as bytes are sent.

    Stays a plain sized bytes payload so Content-Length is set — these
    switches reject chunked uploads — and hands the body to the socket in
    chunks, reporting an integer percent in [base, base+span) after each. Only
    changes are reported, so a fast (fully buffered) upload doesn't spam the
    entity. "Sent" is bytes handed to the socket, which can lead what the
    switch has stored, so the range stops one short of base+span rather than
    claiming its next milestone.

    aiohttp >= 3.12 drives a sized body through write_with_length(); older
    aiohttp uses write(). Overriding only write() (as a first cut did) meant
    the body still streamed but the callbacks never ran under HA's aiohttp.
    """

    def __init__(
        self,
        body: bytes,
        content_type: str,
        report: Callable[[int], None] | None,
        base: int,
        span: int,
    ) -> None:
        super().__init__(body, content_type=content_type)
        self._report = report
        self._base = base
        self._span = span
        self._last = -1

    def _tick(self, sent: int, total: int) -> None:
        if self._report is None:
            return
        pct = self._base + min(sent * self._span // max(total, 1), self._span - 1)
        if pct != self._last:
            self._last = pct
            self._report(pct)

    async def _stream(
        self, writer: aiohttp.abc.AbstractStreamWriter, limit: int | None
    ) -> None:
        data = self._value if limit is None else self._value[:limit]
        total = len(data)
        for start in range(0, total, _UPLOAD_CHUNK_BYTES):
            await writer.write(data[start : start + _UPLOAD_CHUNK_BYTES])
            self._tick(min(start + _UPLOAD_CHUNK_BYTES, total), total)

    async def write(self, writer: aiohttp.abc.AbstractStreamWriter) -> None:
        await self._stream(writer, None)

    async def write_with_length(
        self, writer: aiohttp.abc.AbstractStreamWriter, content_length: int | None
    ) -> None:
        await self._stream(writer, content_length)


def _multipart_upload_body(
    fields: list[tuple[str, str | None]], filename: str, image: bytes
) -> tuple[bytes, str]:
    """Assemble a multipart/form-data body, file part at its ordered slot.

    Mirrors what the switch's own upload form posts (and what the reverse-
    engineering probes used): each text field in document order carrying only
    a Content-Disposition, the file part where the file input sits. A field is
    the file when its value is None. Returns (body, Content-Type header value).
    """
    boundary = "----netgearpoe" + uuid4().hex
    crlf = b"\r\n"
    marker = b"--" + boundary.encode()
    out: list[bytes] = []
    for name, value in fields:
        out.append(marker + crlf)
        header = f'Content-Disposition: form-data; name="{name}"'
        if value is None:  # the file input's position
            out.append(
                f'{header}; filename="{filename}"'.encode()
                + crlf
                + b"Content-Type: application/octet-stream"
                + crlf
                + crlf
            )
            out.append(image)
            out.append(crlf)
        else:
            out.append(header.encode() + crlf + crlf + value.encode() + crlf)
    out.append(marker + b"--" + crlf)
    return b"".join(out), f"multipart/form-data; boundary={boundary}"
