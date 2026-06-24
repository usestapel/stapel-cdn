"""
Custom upload handler that enforces upload speed limits.

- Absolute timeout: 5 minutes per upload
- Minimum speed: 2 KB/s averaged over a 10-second sliding window
"""

import time
import logging
from collections import deque

from django.core.files.uploadhandler import FileUploadHandler, StopUpload

logger = logging.getLogger(__name__)

UPLOAD_MAX_TIME = 300  # 5 minutes
UPLOAD_MIN_SPEED = 2048  # 2 KB/s
SPEED_WINDOW = 10  # 10-second sliding window


class SpeedLimitUploadHandler(FileUploadHandler):
    """Terminate uploads that exceed time limit or drop below minimum speed."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.upload_start = time.monotonic()
        # deque of (timestamp, chunk_size) for sliding window
        self.chunks = deque()
        self.total_bytes = 0

    def receive_data_chunk(self, raw_data, start):
        now = time.monotonic()
        elapsed = now - self.upload_start
        chunk_size = len(raw_data)

        # Absolute timeout
        if elapsed > UPLOAD_MAX_TIME:
            logger.warning(
                "Upload aborted: exceeded %ds time limit (%.0fs elapsed, %d bytes)",
                UPLOAD_MAX_TIME, elapsed, self.total_bytes,
            )
            raise StopUpload(connection_reset=True)

        # Track chunk in sliding window
        self.chunks.append((now, chunk_size))
        self.total_bytes += chunk_size

        # Evict entries outside the window
        cutoff = now - SPEED_WINDOW
        while self.chunks and self.chunks[0][0] < cutoff:
            self.chunks.popleft()

        # Check speed only after the first window has passed
        if elapsed >= SPEED_WINDOW:
            window_bytes = sum(size for _, size in self.chunks)
            window_start = self.chunks[0][0] if self.chunks else cutoff
            window_duration = now - window_start
            if window_duration > 0:
                speed = window_bytes / window_duration
                if speed < UPLOAD_MIN_SPEED:
                    logger.warning(
                        "Upload aborted: speed %.0f B/s < %d B/s threshold "
                        "(%.0fs elapsed, %d bytes)",
                        speed, UPLOAD_MIN_SPEED, elapsed, self.total_bytes,
                    )
                    raise StopUpload(connection_reset=True)

        return raw_data

    def file_complete(self, file_size):
        return None
