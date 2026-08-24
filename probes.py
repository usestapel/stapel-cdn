"""The deployment's time-based media tool — ffprobe/ffmpeg, behind one seam.

libvips answers everything about a still image (``decoders.py``). It answers
nothing about a video or a voice message: duration, a representative frame,
an amplitude envelope. That is ffmpeg's job, and ffmpeg is a **system
binary**, not a pip wheel — the same shape of optional dependency libvips
already is, and the same failure mode if it is treated casually: a
deployment without it produces a chat page of attachments with no duration,
no poster and no waveform, and nothing anywhere says why.

So every call into ffmpeg/ffprobe goes through this module, and every way it
can fail has a **name**:

    MediaToolUnavailable(reason=REASON_FFPROBE_MISSING)   — no binary
    MediaToolUnavailable(reason=REASON_PROBE_FAILED)      — binary said no
    MediaToolUnavailable(reason=REASON_RENDER_FAILED)     — no usable output
    MediaToolUnavailable(reason=REASON_TOOL_TIMEOUT)      — took too long

The name is what gets stored on the row (``meta_reason``) and reported in
the render-metadata snapshot, so "this voice message has no waveform" is
always accompanied by which of those four it was. ``checks.E002``/``E003``
catch the first one at boot for a deployment that enabled the submodule;
this module catches it per object for one that did not.

Nothing here ever raises out into a request or a task: callers translate a
:class:`MediaToolUnavailable` into a degraded snapshot. What this module
refuses to do is return *empty metadata that looks like measured metadata* —
a duration of 0 and a duration nobody could measure are not the same fact.

Subprocess posture
------------------
Fixed argument lists, never a shell; a timeout on every call
(``STAPEL_CDN["MEDIA_TOOL_TIMEOUT"]``); output size bounded by asking ffmpeg
for exactly one frame at a bounded size. The input path is a
content-addressed file this module already stored — not attacker-supplied
text — but the argv form keeps it that way even if a future caller passes
something else.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"

#: Named degradation reasons. These strings are part of the ``cdn.describe``
#: contract (``meta_reason``) — consumers may branch on them, so they are
#: constants here rather than message text.
REASON_FFPROBE_MISSING = "ffprobe_missing"
REASON_FFMPEG_MISSING = "ffmpeg_missing"
REASON_PROBE_FAILED = "probe_failed"
REASON_RENDER_FAILED = "render_failed"
REASON_TOOL_TIMEOUT = "tool_timeout"

TOOL_REASONS = (
    REASON_FFPROBE_MISSING,
    REASON_FFMPEG_MISSING,
    REASON_PROBE_FAILED,
    REASON_RENDER_FAILED,
    REASON_TOOL_TIMEOUT,
)


class MediaToolUnavailable(Exception):
    """A time-based media tool could not answer — with a named reason.

    ``reason`` is one of :data:`TOOL_REASONS` and is what ends up in the
    snapshot's ``meta_reason``; ``detail`` is for the log line only.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def ffprobe_available() -> bool:
    """Whether an ``ffprobe`` binary is on PATH."""
    return shutil.which(FFPROBE) is not None


def ffmpeg_available() -> bool:
    """Whether an ``ffmpeg`` binary is on PATH."""
    return shutil.which(FFMPEG) is not None


def _timeout() -> float:
    from .conf import cdn_settings

    try:
        value = float(cdn_settings.MEDIA_TOOL_TIMEOUT)
    except (TypeError, ValueError):
        return 30.0
    return value if value > 0 else 30.0


def _run(argv: list[str], missing_reason: str) -> subprocess.CompletedProcess:
    """Run *argv* with a timeout, mapping every failure to a named reason."""
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, never a shell
            argv,
            capture_output=True,
            timeout=_timeout(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaToolUnavailable(missing_reason, str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolUnavailable(REASON_TOOL_TIMEOUT, str(exc)) from exc
    except OSError as exc:  # permissions, exec format, ...
        raise MediaToolUnavailable(missing_reason, str(exc)) from exc


def probe_media(path: str) -> dict:
    """Measured facts about a time-based file: width, height, duration_ms.

    Returns ``{"width": int|None, "height": int|None, "duration_ms":
    int|None}``. A key is ``None`` when the container genuinely does not
    carry it (an audio file has no width) — *not* when nothing could be
    measured, which raises :class:`MediaToolUnavailable` instead.
    """
    if not ffprobe_available():
        raise MediaToolUnavailable(
            REASON_FFPROBE_MISSING,
            "no 'ffprobe' on PATH — install ffmpeg on this deployment",
        )
    proc = _run(
        [
            FFPROBE,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        REASON_FFPROBE_MISSING,
    )
    if proc.returncode != 0:
        raise MediaToolUnavailable(
            REASON_PROBE_FAILED,
            (proc.stderr or b"").decode("utf-8", "replace").strip()[:400],
        )
    try:
        payload = json.loads(proc.stdout or b"{}")
    except ValueError as exc:
        raise MediaToolUnavailable(REASON_PROBE_FAILED, str(exc)) from exc

    streams = payload.get("streams") or []
    width = height = None
    for stream in streams:
        if stream.get("codec_type") == "video":
            width = _as_int(stream.get("width"))
            height = _as_int(stream.get("height"))
            if width and height:
                break

    duration_ms = None
    for source in [payload.get("format") or {}] + list(streams):
        duration_ms = _as_ms(source.get("duration"))
        if duration_ms is not None:
            break

    if width is None and height is None and duration_ms is None:
        raise MediaToolUnavailable(
            REASON_PROBE_FAILED, "ffprobe reported neither dimensions nor duration"
        )
    return {"width": width, "height": height, "duration_ms": duration_ms}


def _as_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_ms(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return int(round(seconds * 1000))


def extract_poster_png(path: str, at_seconds: float = 0.0) -> bytes:
    """One frame of a video as PNG bytes, for the poster/blur pass.

    Seeks *before* the input (fast, keyframe-accurate enough for a poster)
    and asks for exactly one frame, so the output is bounded by one decoded
    picture no matter how long the video is.
    """
    if not ffmpeg_available():
        raise MediaToolUnavailable(
            REASON_FFMPEG_MISSING,
            "no 'ffmpeg' on PATH — install ffmpeg on this deployment",
        )
    argv = [FFMPEG, "-v", "error", "-nostdin"]
    if at_seconds and at_seconds > 0:
        argv += ["-ss", f"{at_seconds:.3f}"]
    argv += [
        "-i", path,
        "-frames:v", "1",
        "-f", "image2",
        "-c:v", "png",
        "-",
    ]
    proc = _run(argv, REASON_FFMPEG_MISSING)
    if proc.returncode != 0 or not proc.stdout:
        raise MediaToolUnavailable(
            REASON_RENDER_FAILED,
            (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
            or "ffmpeg produced no poster frame",
        )
    return proc.stdout


def render_waveform_png(
    path: str, width: int, height: int, color: str = "#3f7fbf"
) -> bytes:
    """An amplitude strip for an audio file as PNG bytes.

    ffmpeg's ``showwavespic`` filter, which is exactly the tool for this and
    is already the binary this module's video path requires — no second
    media stack, no numpy/matplotlib pulled in to draw a rectangle. The
    strip is rendered at the requested pixel size and re-encoded to WebP by
    the caller (``metadata.encode_preview``), which is also where the byte
    budget is enforced.
    """
    if not ffmpeg_available():
        raise MediaToolUnavailable(
            REASON_FFMPEG_MISSING,
            "no 'ffmpeg' on PATH — install ffmpeg on this deployment",
        )
    proc = _run(
        [
            FFMPEG, "-v", "error", "-nostdin",
            "-i", path,
            "-filter_complex",
            f"showwavespic=s={int(width)}x{int(height)}:colors={color}",
            "-frames:v", "1",
            "-f", "image2",
            "-c:v", "png",
            "-",
        ],
        REASON_FFMPEG_MISSING,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise MediaToolUnavailable(
            REASON_RENDER_FAILED,
            (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
            or "ffmpeg produced no waveform image",
        )
    return proc.stdout


__all__ = [
    "FFMPEG",
    "FFPROBE",
    "MediaToolUnavailable",
    "REASON_FFMPEG_MISSING",
    "REASON_FFPROBE_MISSING",
    "REASON_PROBE_FAILED",
    "REASON_RENDER_FAILED",
    "REASON_TOOL_TIMEOUT",
    "TOOL_REASONS",
    "extract_poster_png",
    "ffmpeg_available",
    "ffprobe_available",
    "probe_media",
    "render_waveform_png",
]
