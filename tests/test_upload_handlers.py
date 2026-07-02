"""
Tests for SpeedLimitUploadHandler (upload time and speed enforcement).
"""
import pytest
from django.core.files.uploadhandler import StopUpload
from stapel_cdn.upload_handlers import (
    SPEED_WINDOW,
    UPLOAD_MAX_TIME,
    SpeedLimitUploadHandler,
)


def make_handler():
    handler = SpeedLimitUploadHandler()
    handler.new_file('file', 'upload.bin', 'application/octet-stream', 1024)
    return handler


class TestSpeedLimitUploadHandler:
    def test_normal_chunk_passes_through(self):
        handler = make_handler()
        chunk = b'x' * 4096
        assert handler.receive_data_chunk(chunk, 0) == chunk
        assert handler.total_bytes == 4096
        assert len(handler.chunks) == 1

    def test_absolute_timeout_aborts(self):
        handler = make_handler()
        handler.upload_start -= UPLOAD_MAX_TIME + 1
        with pytest.raises(StopUpload):
            handler.receive_data_chunk(b'x' * 100, 0)

    def test_slow_upload_aborts(self):
        handler = make_handler()
        # Record a tiny chunk, then age both the upload start and that chunk
        # so the sliding window spans ~9s with only ~20 bytes -> < 2 KB/s.
        handler.receive_data_chunk(b'x' * 10, 0)
        handler.upload_start -= SPEED_WINDOW + 1
        ts, size = handler.chunks[0]
        handler.chunks[0] = (ts - (SPEED_WINDOW - 1), size)
        with pytest.raises(StopUpload):
            handler.receive_data_chunk(b'x' * 10, 0)

    def test_fast_upload_within_window_not_aborted(self):
        handler = make_handler()
        handler.upload_start -= SPEED_WINDOW + 1
        chunk = b'x' * (1024 * 1024)  # 1 MB in the current window
        assert handler.receive_data_chunk(chunk, 0) == chunk

    def test_old_chunks_evicted_from_window(self):
        handler = make_handler()
        chunk = b'x' * 1024
        handler.receive_data_chunk(chunk, 0)
        # Age the recorded chunk beyond the sliding window
        ts, size = handler.chunks[0]
        handler.chunks[0] = (ts - SPEED_WINDOW - 5, size)
        handler.receive_data_chunk(b'y' * (1024 * 1024), 0)
        assert len(handler.chunks) == 1
        assert handler.chunks[0][1] == 1024 * 1024

    def test_file_complete_returns_none(self):
        handler = make_handler()
        assert handler.file_complete(1234) is None
