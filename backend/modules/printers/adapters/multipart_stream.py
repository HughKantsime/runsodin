"""Bounded-memory, seekable multipart/form-data stream for device HTTP uploads.

Formats fields and one file part byte-identically to ``requests`` / ``urllib3``
without loading the uploaded file into memory. Implements ``__len__``, ``read``,
``tell`` and ``seek`` so ``requests`` sends an exact ``Content-Length`` header
and ``HTTPDigestAuth`` can rewind on a 401 challenge.
"""
from __future__ import annotations

import binascii
import io
import os
from typing import BinaryIO, Iterable


def _format_param(name: str, value: str) -> str:
    escaped = value.replace("\r", "%0D").replace("\n", "%0A").replace('"', "%22")
    return f'{name}="{escaped}"'


class MultipartFileStream:
    """Seekable file-like multipart/form-data body streaming one file from disk."""

    def __init__(
        self,
        *,
        fields: Iterable[tuple[str, str]] = (),
        file_field: str,
        filename: str,
        fileobj: BinaryIO,
        file_content_type: str = "application/octet-stream",
        boundary: str | None = None,
    ) -> None:
        self.boundary = boundary or binascii.hexlify(os.urandom(16)).decode("ascii")
        self.content_type = f"multipart/form-data; boundary={self.boundary}"
        self.fields = tuple((str(k), str(v)) for k, v in fields)
        self.file_field = file_field
        self.filename = filename
        self.file_content_type = file_content_type
        self._fileobj = fileobj
        self._file_start = fileobj.tell()
        fileobj.seek(0, io.SEEK_END)
        self._file_size = fileobj.tell() - self._file_start
        fileobj.seek(self._file_start, io.SEEK_SET)

        parts: list[bytes] = []
        for key, value in self.fields:
            parts.append(
                (
                    f"--{self.boundary}\r\n"
                    f"Content-Disposition: form-data; {_format_param('name', key)}\r\n"
                    f"\r\n"
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        parts.append(
            (
                f"--{self.boundary}\r\n"
                f"Content-Disposition: form-data; {_format_param('name', file_field)}; "
                f"{_format_param('filename', filename)}\r\n"
                f"Content-Type: {file_content_type}\r\n"
                f"\r\n"
            ).encode("utf-8")
        )
        self._prefix = b"".join(parts)
        self._suffix = f"\r\n--{self.boundary}--\r\n".encode("ascii")
        self._prefix_len = len(self._prefix)
        self._file_end = self._prefix_len + self._file_size
        self._total_len = self._file_end + len(self._suffix)
        self._pos = 0

    def __len__(self) -> int:
        return self._total_len

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._total_len + offset
        else:
            raise ValueError(f"Invalid whence: {whence}")
        if target < 0:
            raise ValueError("Negative seek position")
        self._pos = min(target, self._total_len)
        if self._pos <= self._prefix_len:
            self._fileobj.seek(self._file_start, io.SEEK_SET)
        elif self._pos < self._file_end:
            self._fileobj.seek(self._file_start + (self._pos - self._prefix_len), io.SEEK_SET)
        else:
            self._fileobj.seek(self._file_start + self._file_size, io.SEEK_SET)
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self._pos >= self._total_len or size == 0:
            return b""
        remaining = self._total_len - self._pos if size is None or size < 0 else min(size, self._total_len - self._pos)
        chunks: list[bytes] = []
        while remaining > 0 and self._pos < self._total_len:
            if self._pos < self._prefix_len:
                take = min(remaining, self._prefix_len - self._pos)
                chunks.append(self._prefix[self._pos : self._pos + take])
                self._pos += take
                remaining -= take
            elif self._pos < self._file_end:
                take = min(remaining, self._file_end - self._pos)
                data = self._fileobj.read(take)
                if not data:
                    raise OSError("Uploaded file shrank while streaming")
                chunks.append(data)
                self._pos += len(data)
                remaining -= len(data)
            else:
                offset = self._pos - self._file_end
                take = min(remaining, len(self._suffix) - offset)
                chunks.append(self._suffix[offset : offset + take])
                self._pos += take
                remaining -= take
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)
