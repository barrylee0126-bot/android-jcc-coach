"""Bounded incremental rows. An incomplete row never preserves a ready hint."""

import os
from pathlib import Path
import stat

from jcc_timed_overlay.source import decode_event

MAX_LINE = 4096
MAX_CHUNK = 32768
MAX_FILE = 8 * 1024 * 1024


class JsonlDecoder:
    def __init__(self, bridge):
        self.bridge = bridge
        self.buffer = b""
        self.closed = False
        self.lines = 0

    def feed(self, data):
        if self.closed:
            return
        if type(data) is not bytes or len(data) > MAX_CHUNK:
            self.fail("transport_chunk")
            return
        self.buffer += data
        while b"\n" in self.buffer and not self.closed:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.lines += 1
            if not line or len(line) > MAX_LINE:
                self.fail("transport_line_size")
                break
            try:
                raw = decode_event(line)
            except (ValueError, UnicodeError, RecursionError):
                self.fail("transport_json")
                break
            self.bridge.ingest(raw)
            if self.bridge.faulted:
                self.closed = True
        if len(self.buffer) > MAX_LINE:
            self.fail("transport_line_size")
        elif self.buffer and not self.closed:
            self.bridge.partial_line()

    def fail(self, reason):
        self.closed, self.buffer = True, b""
        self.bridge.fault(reason)

    def eof(self):
        self.fail("transport_partial_eof" if self.buffer else "transport_eof")


class AppendOnlyTail:
    """Controlled empty file only. No history, symlink, FIFO, or reattachment.

    Compare all already-read bytes too: size alone misses truncate-and-refill.
    An unobserved rewrite to an identical prefix cannot replay bytes because the
    offset never rewinds; event/frame watermarks and original deadlines remain.
    """

    def __init__(self, path, decoder):
        self.path, self.decoder = Path(path), decoder
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != 0:
                raise ValueError("controlled_empty_file_required")
        except Exception:
            os.close(self.fd)
            raise
        self.identity = info.st_dev, info.st_ino
        self.prefix = b""
        self.failed = False
        self.closed = False

    def poll(self):
        if self.failed or self.closed:
            return
        try:
            info = self.path.lstat()
            offset = len(self.prefix)
            if ((info.st_dev, info.st_ino) != self.identity or not stat.S_ISREG(info.st_mode)
                    or info.st_size < offset or info.st_size > MAX_FILE
                    or os.pread(self.fd, offset, 0) != self.prefix):
                raise OSError("file_changed")
            data = os.pread(self.fd, MAX_CHUNK, offset)
            self.prefix += data
            if os.pread(self.fd, len(self.prefix), 0) != self.prefix:
                raise OSError("file_changed_during_read")
            if data:
                self.decoder.feed(data)
        except OSError:
            self.failed = True
            self.decoder.fail("transport_file_changed")

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.fd)
            self.decoder.eof()
