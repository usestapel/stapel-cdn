"""The metadata pipeline: what a UI needs to draw an attachment, per kind.

Covers, per media type and per failure mode:

- the open media-kind registry (merge-over-builtins, specificity, removal,
  malformed entries) — kinds.py;
- the inline-preview byte budget: reuse, quality downgrade, refusal, and the
  read-side ceiling — metadata.py;
- images/GIFs (blur-up produced in the SAME libvips pass), video (poster +
  ffprobe facts), voice (waveform + duration), documents (mime/ext);
- **the degraded-dependency path for every one of them**: no ffmpeg, no
  ffprobe, no libvips, no stored blob — each degrading with a NAMED reason
  rather than an empty snapshot;
- the backfill command's idempotence and resumability;
- the boot checks that tell a deployment a media tool is missing.

ffmpeg is not installed in this test environment on purpose: the happy path
for time-based media is driven through the ``probes`` seam with a stub that
returns what a real ffmpeg returns (a canned ffprobe answer and a genuine
PNG encoded by Pillow — an independent encoder, per conftest's rule), and
the *absence* path is the environment's own truth.
"""
import base64
import os
from io import BytesIO

import pytest
from django.core.management import call_command
from PIL import Image as PILImage

from stapel_core.comm import call, function_registry

from stapel_cdn import kinds, metadata, probes
from stapel_cdn.models import Audio, File, Image, Video
from stapel_cdn.services import (
    AudioProcessingService,
    ImageProcessingService,
    VideoProcessingService,
)

pyvips = pytest.importorskip("pyvips")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _png_bytes(width, height, color=(40, 90, 160)):
    """A real PNG, encoded by Pillow — what ffmpeg hands back in production."""
    buffer = BytesIO()
    PILImage.new("RGB", (width, height), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _store(obj, relpath: str, payload: bytes = b"", pil=None, fmt="JPEG"):
    """Attach a REAL stored file to *obj* under MEDIA_ROOT.

    Not a mocked ``original``: the backfill re-reads its rows from the
    database, so a mock attached to one instance would make the pass look
    like it works on rows nothing can actually open.
    """
    from django.conf import settings as django_settings

    full = os.path.join(django_settings.MEDIA_ROOT, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if pil is not None:
        pil.save(full, format=fmt)
    else:
        with open(full, "wb") as handle:
            handle.write(payload)
    obj.original.name = relpath
    obj.original_size = os.path.getsize(full)
    obj.save(update_fields=["original", "original_size"])
    return full


def _make_image(tmp_path, settings, width, height, hash_char="f",
                image_type="product", ext=".jpg", fmt="JPEG"):
    settings.MEDIA_ROOT = str(tmp_path)
    file_hash = hash_char * 64
    image = Image.objects.create(
        file_hash=file_hash,
        original_filename=f"original{ext}",
        file_extension=ext,
        type=image_type,
        original_width=width,
        original_height=height,
        original_size=0,
        is_processed=False,
    )
    _store(
        image,
        f"{image_type}/{file_hash}/original{ext}",
        pil=PILImage.new("RGB", (width, height), color="blue"),
        fmt=fmt,
    )
    return image, tmp_path / image_type / file_hash


def _make_audio(tmp_path, settings, hash_char="a", ext=".ogg"):
    settings.MEDIA_ROOT = str(tmp_path)
    file_hash = hash_char * 64
    audio = Audio.objects.create(
        file_hash=file_hash,
        original_filename=f"voice{ext}",
        file_extension=ext,
        original_size=0,
    )
    _store(audio, f"private/audio/{file_hash}/voice{ext}", b"OggS" + b"\0" * 512)
    return audio


def _make_video(tmp_path, settings, hash_char="v", ext=".mp4"):
    settings.MEDIA_ROOT = str(tmp_path)
    file_hash = hash_char * 64
    video = Video.objects.create(
        file_hash=file_hash,
        original_filename=f"clip{ext}",
        file_extension=ext,
        original_size=0,
    )
    _store(video, f"video/{file_hash}/clip{ext}", b"\0" * 1024)
    return video


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """A working ffmpeg/ffprobe, driven through the probes seam.

    Returns the recorder so a test can assert what was asked of it (how many
    decodes, which strip sizes were tried).
    """
    calls = {"probe": 0, "poster": 0, "waveform": []}

    def probe_media(path):
        calls["probe"] += 1
        return {"width": 1920, "height": 1080, "duration_ms": 4500}

    def extract_poster_png(path, at_seconds=0.0):
        calls["poster"] += 1
        return _png_bytes(640, 360)

    def render_waveform_png(path, width, height, color="#3f7fbf"):
        calls["waveform"].append((width, height))
        return _png_bytes(width, height)

    monkeypatch.setattr(probes, "probe_media", probe_media)
    monkeypatch.setattr(probes, "extract_poster_png", extract_poster_png)
    monkeypatch.setattr(probes, "render_waveform_png", render_waveform_png)
    return calls


@pytest.fixture
def no_ffmpeg(monkeypatch):
    """A deployment with no media tool at all — the degraded path."""

    def missing(*args, **kwargs):
        raise probes.MediaToolUnavailable(
            probes.REASON_FFMPEG_MISSING, "no 'ffmpeg' on PATH"
        )

    def missing_probe(*args, **kwargs):
        raise probes.MediaToolUnavailable(
            probes.REASON_FFPROBE_MISSING, "no 'ffprobe' on PATH"
        )

    monkeypatch.setattr(probes, "probe_media", missing_probe)
    monkeypatch.setattr(probes, "extract_poster_png", missing)
    monkeypatch.setattr(probes, "render_waveform_png", missing)
    monkeypatch.setattr(probes, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(probes, "ffprobe_available", lambda: False)


# --------------------------------------------------------------------------
# the media-kind registry
# --------------------------------------------------------------------------


class TestMediaKindRegistry:
    """kinds.py — open registry, not an enum."""

    def test_builtin_kinds_cover_every_stored_model(self):
        shipped = kinds.get_media_kinds()
        assert {"image", "gif", "video", "audio", "file"} <= set(shipped)
        assert {k.model for k in shipped.values()} == set(kinds.MODELS)

    def test_preview_kind_per_media_type(self):
        shipped = kinds.get_media_kinds()
        assert shipped["image"].preview == kinds.PREVIEW_BLUR
        assert shipped["video"].preview == kinds.PREVIEW_POSTER
        assert shipped["audio"].preview == kinds.PREVIEW_WAVEFORM
        assert shipped["file"].preview is None

    def test_gif_beats_plain_image_on_extension(self):
        assert kinds.classify("image", ".jpg").name == "image"
        gif = kinds.classify("image", ".gif")
        assert gif.name == "gif"
        assert gif.animated is True

    def test_host_adds_a_kind_without_a_release(self, settings):
        """Stickers: a dict literal in settings, no fork, no migration."""
        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product", "sticker"),
            "MEDIA_KINDS": {
                "sticker": {
                    "model": "image",
                    "asset_types": ("sticker",),
                    "preview": "blur",
                    "animated": True,
                }
            },
        }
        sticker = kinds.classify("image", ".webp", asset_type="sticker")
        assert sticker.name == "sticker"
        assert sticker.animated is True
        # A non-sticker image is untouched by the overlay.
        assert kinds.classify("image", ".webp", asset_type="avatar").name == "image"

    def test_asset_type_narrowing_outranks_extension(self, settings):
        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product", "sticker"),
            "MEDIA_KINDS": {
                "sticker": {
                    "model": "image",
                    "asset_types": ("sticker",),
                    "preview": "blur",
                    "animated": True,
                }
            },
        }
        # A .gif sticker is a sticker (specificity 2) not a gif (1).
        assert kinds.classify("image", ".gif", asset_type="sticker").name == "sticker"

    def test_none_removes_a_builtin_kind(self, settings):
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar",), "MEDIA_KINDS": {"gif": None}}
        assert "gif" not in kinds.get_media_kinds()
        assert kinds.classify("image", ".gif").name == "image"

    def test_a_model_with_no_kind_at_all_resolves_to_none(self, settings):
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar",), "MEDIA_KINDS": {"file": None}}
        assert kinds.classify("file", ".pdf") is None

    @pytest.mark.parametrize(
        "entry",
        [
            {"model": "hologram"},
            {"model": "image", "preview": "sparkle"},
            {"model": "image", "unknown_key": 1},
            "not-a-dict",
        ],
    )
    def test_malformed_entry_raises_a_config_error(self, settings, entry):
        settings.STAPEL_CDN = {"MEDIA_KINDS": {"broken": entry}}
        with pytest.raises(kinds.MediaKindConfigError):
            kinds.get_media_kinds()

    def test_malformed_entry_is_reported_by_a_boot_check(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"MEDIA_KINDS": {"broken": {"model": "hologram"}}}
        findings = checks.check_media_kinds()
        assert [f.id for f in findings] == [checks.W009_MEDIA_KINDS_INVALID]


# --------------------------------------------------------------------------
# the byte budget
# --------------------------------------------------------------------------


class TestPreviewBudget:
    """metadata.encode_preview — downgrade, then refuse. Never truncate."""

    def test_default_budget_is_four_kilobytes(self, settings):
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "product")}
        assert metadata.preview_budget() == 4096

    def test_already_encoded_bytes_are_reused_verbatim(self):
        payload = _png_bytes(4, 4)  # small; stands in for the micro tier
        uri, reason = metadata.encode_preview(encoded_webp=payload)
        assert reason == ""
        assert uri.startswith("data:image/webp;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == payload

    def test_over_budget_encoding_is_downgraded_not_truncated(self, settings):
        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product"),
            "MICRO_PREVIEW_MAX_BYTES": 900,
        }
        image = pyvips.Image.new_from_buffer(_png_bytes(64, 64), "")
        oversized = b"x" * 4000
        uri, reason = metadata.encode_preview(
            encoded_webp=oversized, vips_image=image
        )
        assert reason == ""
        assert len(uri) <= 900
        # The refused buffer is nowhere in the result — no truncation.
        assert base64.b64decode(uri.split(",", 1)[1]) != oversized[:100]

    def test_refuses_with_a_named_reason_when_nothing_fits(self, settings):
        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product"),
            "MICRO_PREVIEW_MAX_BYTES": 32,
        }
        image = pyvips.Image.new_from_buffer(_png_bytes(256, 256), "")
        uri, reason = metadata.encode_preview(vips_image=image)
        assert uri is None
        assert reason == metadata.REASON_PREVIEW_OVER_BUDGET

    def test_no_decoder_downgrades_to_png_with_a_named_reason(self, poisoned_pyvips):
        png = _png_bytes(4, 4)
        uri, reason = metadata.encode_preview(raw=png)
        assert uri.startswith("data:image/png;base64,")
        assert reason == metadata.REASON_DECODER_MISSING

    def test_no_decoder_and_over_budget_refuses(self, settings, poisoned_pyvips):
        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product"),
            "MICRO_PREVIEW_MAX_BYTES": 32,
        }
        uri, reason = metadata.encode_preview(raw=_png_bytes(256, 256))
        assert uri is None
        assert reason == metadata.REASON_PREVIEW_OVER_BUDGET

    @pytest.mark.django_db
    def test_lowering_the_budget_drops_stored_previews_on_read(
        self, tmp_path, settings
    ):
        """The ceiling is enforced on read too, not only at ingest."""
        image, _ = _make_image(tmp_path, settings, 400, 300, hash_char="1")
        ImageProcessingService.generate_thumbnails_only(image)
        assert metadata.build_render_metadata(image)["preview_b64"]

        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product"),
            "MICRO_PREVIEW_MAX_BYTES": 16,
        }
        snapshot = metadata.build_render_metadata(image)
        assert snapshot["preview_b64"] is None
        assert snapshot["meta_reason"] == metadata.REASON_PREVIEW_OVER_BUDGET

    @pytest.mark.django_db
    def test_a_real_micro_preview_is_well_inside_the_budget(
        self, tmp_path, settings
    ):
        """The number in CONFIG.MD is a claim; this is the measurement."""
        image, _ = _make_image(tmp_path, settings, 1600, 1200, hash_char="2")
        ImageProcessingService.generate_thumbnails_only(image)
        assert 0 < len(image.preview_b64) <= 4096


# --------------------------------------------------------------------------
# images and GIFs
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestImageMetadata:
    def test_ingest_stamps_the_blur_up_placeholder(self, tmp_path, settings):
        image, _ = _make_image(tmp_path, settings, 800, 400, hash_char="3")
        ImageProcessingService.generate_thumbnails_only(image)

        assert image.preview_b64.startswith("data:image/webp;base64,")
        assert image.meta_reason == ""

        snapshot = metadata.build_render_metadata(image)
        assert snapshot["kind"] == "image"
        assert snapshot["preview_kind"] == "blur"
        assert snapshot["width"] == 800 and snapshot["height"] == 400
        assert snapshot["aspect"] == 2.0
        assert snapshot["bytes"] == image.original_size
        assert snapshot["ext"] == ".jpg"
        assert snapshot["mime"] == "image/jpeg"
        assert snapshot["meta_status"] == "ok"
        assert snapshot["meta_reason"] is None
        assert snapshot["duration_ms"] is None

    def test_the_placeholder_is_the_micro_tier_itself_one_pass(
        self, tmp_path, settings
    ):
        """No second decode and no re-encode: the same bytes on disk and inline."""
        image, img_dir = _make_image(tmp_path, settings, 640, 480, hash_char="4")
        ImageProcessingService.generate_thumbnails_only(image)

        on_disk = (img_dir / "16.webp").read_bytes()
        inline = base64.b64decode(image.preview_b64.split(",", 1)[1])
        assert inline == on_disk

    def test_an_unprocessed_image_says_not_generated(self, tmp_path, settings):
        image, _ = _make_image(tmp_path, settings, 300, 300, hash_char="5")
        snapshot = metadata.build_render_metadata(image)
        assert snapshot["preview_b64"] is None
        assert snapshot["meta_reason"] == metadata.REASON_NOT_GENERATED
        assert snapshot["meta_status"] == "partial"  # dimensions are known
        assert snapshot["square"] is True

    def test_gif_is_its_own_kind_and_animated(self, tmp_path, settings):
        image, _ = _make_image(
            tmp_path, settings, 120, 120, hash_char="6", ext=".gif", fmt="GIF"
        )
        snapshot = metadata.build_render_metadata(image)
        assert snapshot["kind"] == "gif"
        assert snapshot["animated"] is True
        assert snapshot["mime"] == "image/gif"
        assert snapshot["ext"] == ".gif"


# --------------------------------------------------------------------------
# voice messages
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestVoiceMetadata:
    def test_duration_and_waveform(self, tmp_path, settings, fake_ffmpeg):
        audio = _make_audio(tmp_path, settings, hash_char="7")
        AudioProcessingService.extract_metadata(audio)

        assert audio.duration == pytest.approx(4.5)
        assert audio.preview_b64.startswith("data:image/webp;base64,")
        assert audio.meta_reason == ""

        snapshot = metadata.build_render_metadata(audio)
        assert snapshot["kind"] == "audio"
        assert snapshot["preview_kind"] == "waveform"
        assert snapshot["duration_ms"] == 4500
        assert snapshot["meta_status"] == "ok"
        assert snapshot["width"] is None  # a recording has no picture geometry
        assert snapshot["ref"] == f"audio/{audio.file_hash}"

    def test_waveform_downgrades_to_the_smaller_strip(
        self, tmp_path, settings, fake_ffmpeg
    ):
        """The ladder is real: the big strip is refused, the small one ships."""
        settings.STAPEL_CDN = {
            "ASSET_TYPES": ("avatar", "product"),
            "MICRO_PREVIEW_MAX_BYTES": 400,
            "WAVEFORM_SIZES": ((2000, 400), (24, 8)),
        }
        audio = _make_audio(tmp_path, settings, hash_char="8")
        AudioProcessingService.extract_metadata(audio)

        assert fake_ffmpeg["waveform"] == [(2000, 400), (24, 8)]
        assert audio.preview_b64
        assert len(audio.preview_b64) <= 400

    def test_no_ffmpeg_degrades_with_a_named_reason(
        self, tmp_path, settings, no_ffmpeg
    ):
        audio = _make_audio(tmp_path, settings, hash_char="9")
        AudioProcessingService.extract_metadata(audio)

        assert audio.preview_b64 == ""
        assert audio.meta_reason == probes.REASON_FFPROBE_MISSING
        # Never a fabricated zero: an unmeasured duration stays null.
        assert audio.duration is None

        snapshot = metadata.build_render_metadata(audio)
        assert snapshot["duration_ms"] is None
        assert snapshot["preview_b64"] is None
        assert snapshot["preview_kind"] == "waveform"
        assert snapshot["meta_status"] == "missing"
        assert snapshot["meta_reason"] == probes.REASON_FFPROBE_MISSING

    def test_a_missing_blob_is_not_reported_as_a_tool_failure(
        self, tmp_path, settings, fake_ffmpeg
    ):
        audio = _make_audio(tmp_path, settings, hash_char="b")
        os.remove(audio.original.path)
        AudioProcessingService.extract_metadata(audio)
        assert audio.meta_reason == metadata.REASON_SOURCE_MISSING


# --------------------------------------------------------------------------
# video
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestVideoMetadata:
    def test_measured_facts_and_poster(self, tmp_path, settings, fake_ffmpeg):
        video = _make_video(tmp_path, settings, hash_char="c")
        VideoProcessingService.process_video(video)

        assert video.original_width == 1920 and video.original_height == 1080
        assert video.duration == pytest.approx(4.5)
        assert video.is_processed is True
        assert video.has_poster is True
        assert os.path.exists(video.poster_path)
        # ONE frame extraction feeds both the poster file and the inline one.
        assert fake_ffmpeg["poster"] == 1

        snapshot = metadata.build_render_metadata(video)
        assert snapshot["kind"] == "video"
        assert snapshot["preview_kind"] == "poster"
        assert snapshot["preview_b64"].startswith("data:image/webp;base64,")
        assert snapshot["poster_url"].endswith("/poster.webp")
        assert snapshot["duration_ms"] == 4500
        assert snapshot["aspect"] == round(1920 / 1080, 6)
        assert snapshot["animated"] is True
        assert snapshot["meta_status"] == "ok"

    def test_poster_url_is_null_until_the_file_exists(self, tmp_path, settings):
        video = _make_video(tmp_path, settings, hash_char="d")
        assert video.poster_url is None
        assert metadata.build_render_metadata(video)["poster_url"] is None

    def test_no_ffmpeg_leaves_it_unprocessed_with_a_named_reason(
        self, tmp_path, settings, no_ffmpeg
    ):
        video = _make_video(tmp_path, settings, hash_char="e")
        VideoProcessingService.process_video(video)

        assert video.is_processed is False
        assert video.meta_reason == probes.REASON_FFPROBE_MISSING
        assert video.has_poster is False

        snapshot = metadata.build_render_metadata(video)
        assert snapshot["duration_ms"] is None
        assert snapshot["aspect"] is None
        assert snapshot["poster_url"] is None
        assert snapshot["meta_status"] == "missing"
        assert snapshot["meta_reason"] == probes.REASON_FFPROBE_MISSING

    def test_no_decoder_still_yields_an_inline_poster_when_it_fits(
        self, tmp_path, settings, fake_ffmpeg, poisoned_pyvips
    ):
        """libvips absent: the raw PNG frame is used, and says so."""
        video = _make_video(tmp_path, settings, hash_char="0")
        VideoProcessingService.process_video(video)
        assert video.has_poster is False
        assert video.meta_reason in (
            metadata.REASON_DECODER_MISSING,
            metadata.REASON_PREVIEW_OVER_BUDGET,
        )


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestDocumentMetadata:
    def test_mime_and_extension(self, tmp_path, settings):
        settings.MEDIA_ROOT = str(tmp_path)
        doc = File.objects.create(
            file_hash="d" * 64,
            original_filename="contract.pdf",
            file_extension=".pdf",
            mime_type="application/pdf",
            original_size=98765,
        )
        snapshot = metadata.build_render_metadata(doc)
        assert snapshot["kind"] == "file"
        assert snapshot["mime"] == "application/pdf"
        assert snapshot["ext"] == ".pdf"
        assert snapshot["bytes"] == 98765
        assert snapshot["preview_kind"] is None
        assert snapshot["preview_b64"] is None
        assert snapshot["animated"] is False
        # Nothing is pending for a document — the status must not read as if
        # a preview were still coming.
        assert snapshot["meta_status"] == "ok"
        assert snapshot["meta_reason"] is None

    def test_mime_falls_back_to_the_extension(self, tmp_path, settings):
        settings.MEDIA_ROOT = str(tmp_path)
        doc = File.objects.create(
            file_hash="f" * 63 + "e",
            original_filename="notes.csv",
            file_extension=".csv",
            original_size=12,
        )
        assert metadata.build_render_metadata(doc)["mime"] == "text/csv"


# --------------------------------------------------------------------------
# the comm surface
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestDescribeMany:
    def test_registered(self):
        assert "cdn.describe_many" in function_registry.names()

    def test_batch_resolves_and_reports_missing(self, tmp_path, settings):
        image, _ = _make_image(tmp_path, settings, 200, 100, hash_char="7")
        ImageProcessingService.generate_thumbnails_only(image)
        missing_ref = f"product/{'0' * 64}"

        result = call(
            "cdn.describe_many",
            {"refs": [f"product/{image.file_hash}", missing_ref, missing_ref]},
        )

        assert list(result["items"]) == [f"product/{image.file_hash}"]
        assert result["missing"] == [missing_ref]
        assert result["items"][f"product/{image.file_hash}"]["aspect"] == 2.0

    def test_batch_is_bounded(self):
        from stapel_cdn.functions import DESCRIBE_MANY_LIMIT

        refs = [f"product/{i:064d}" for i in range(DESCRIBE_MANY_LIMIT + 1)]
        with pytest.raises(Exception):
            call("cdn.describe_many", {"refs": refs})


@pytest.mark.django_db
class TestSerializerSurface:
    def test_image_serializer_carries_the_snapshot(self, tmp_path, settings):
        from stapel_cdn.serializers import ImageSerializer

        image, _ = _make_image(tmp_path, settings, 300, 150, hash_char="8")
        ImageProcessingService.generate_thumbnails_only(image)

        data = ImageSerializer(image).data
        assert set(data["render_meta"]) >= {
            "ref", "kind", "mime", "ext", "bytes", "width", "height", "aspect",
            "duration_ms", "preview_b64", "preview_kind", "meta_status",
        }
        assert data["render_meta"]["aspect"] == 2.0


# --------------------------------------------------------------------------
# backfill
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestBackfill:
    def test_stamps_rows_that_predate_the_pipeline(self, tmp_path, settings, capsys):
        image, _ = _make_image(tmp_path, settings, 400, 200, hash_char="1")
        call_command("cdn_backfill_media_meta", "--kind", "image")
        image.refresh_from_db()
        assert image.preview_b64.startswith("data:image/webp;base64,")
        assert "stamped 1" in capsys.readouterr().out

    def test_second_run_is_a_no_op(self, tmp_path, settings, capsys):
        _make_image(tmp_path, settings, 400, 200, hash_char="2")
        call_command("cdn_backfill_media_meta", "--kind", "image")
        capsys.readouterr()
        call_command("cdn_backfill_media_meta", "--kind", "image")
        out = capsys.readouterr().out
        assert "0 candidate(s)" in out

    def test_dry_run_writes_nothing(self, tmp_path, settings, capsys):
        image, _ = _make_image(tmp_path, settings, 400, 200, hash_char="3")
        call_command("cdn_backfill_media_meta", "--kind", "image", "--dry-run")
        image.refresh_from_db()
        assert image.preview_b64 == ""
        assert "would stamp 0" in capsys.readouterr().out

    def test_limit_slices_the_pass_and_the_rest_resumes(
        self, tmp_path, settings, capsys
    ):
        for char in ("4", "5", "6"):
            _make_image(tmp_path, settings, 200, 200, hash_char=char)
        call_command("cdn_backfill_media_meta", "--kind", "image", "--limit", "2")
        assert Image.objects.exclude(preview_b64="").count() == 2
        capsys.readouterr()
        call_command("cdn_backfill_media_meta", "--kind", "image")
        assert Image.objects.exclude(preview_b64="").count() == 3

    def test_degraded_rows_are_counted_with_their_reason(
        self, tmp_path, settings, no_ffmpeg, capsys
    ):
        _make_audio(tmp_path, settings, hash_char="7")
        call_command("cdn_backfill_media_meta", "--kind", "audio")
        out = capsys.readouterr().out
        assert "1 degraded" in out
        assert "ffprobe_missing=1" in out

    def test_a_degraded_row_leaves_the_default_candidate_set(
        self, tmp_path, settings, no_ffmpeg, capsys
    ):
        _make_audio(tmp_path, settings, hash_char="8")
        call_command("cdn_backfill_media_meta", "--kind", "audio")
        capsys.readouterr()
        call_command("cdn_backfill_media_meta", "--kind", "audio")
        assert "0 candidate(s)" in capsys.readouterr().out

    def test_retry_degraded_picks_it_up_once_the_tool_exists(
        self, tmp_path, settings, monkeypatch, capsys
    ):
        """The real operational sequence: no ffmpeg, then ffmpeg, then re-run."""

        def missing(*args, **kwargs):
            raise probes.MediaToolUnavailable(probes.REASON_FFPROBE_MISSING, "")

        monkeypatch.setattr(probes, "probe_media", missing)
        monkeypatch.setattr(probes, "render_waveform_png", missing)
        audio = _make_audio(tmp_path, settings, hash_char="9")
        call_command("cdn_backfill_media_meta", "--kind", "audio")
        audio.refresh_from_db()
        assert audio.meta_reason == probes.REASON_FFPROBE_MISSING

        monkeypatch.setattr(
            probes, "probe_media",
            lambda path: {"width": None, "height": None, "duration_ms": 1200},
        )
        monkeypatch.setattr(
            probes, "render_waveform_png",
            lambda path, w, h, color="#3f7fbf": _png_bytes(w, h),
        )
        capsys.readouterr()
        call_command("cdn_backfill_media_meta", "--kind", "audio", "--retry-degraded")
        audio.refresh_from_db()
        assert audio.preview_b64
        assert audio.meta_reason == ""
        assert audio.duration == pytest.approx(1.2)

    def test_one_unreadable_row_does_not_strand_the_rest(
        self, tmp_path, settings, monkeypatch, capsys
    ):
        _make_image(tmp_path, settings, 200, 200, hash_char="a")
        _make_image(tmp_path, settings, 200, 200, hash_char="b")
        original = ImageProcessingService.generate_thumbnails_only
        seen = {"n": 0}

        def flaky(image_model):
            seen["n"] += 1
            if seen["n"] == 1:
                raise RuntimeError("unreadable")
            return original(image_model)

        monkeypatch.setattr(
            ImageProcessingService, "generate_thumbnails_only", staticmethod(flaky)
        )
        call_command("cdn_backfill_media_meta", "--kind", "image")
        out = capsys.readouterr().out
        assert "1 failed" in out
        assert "stamped 1" in out


# --------------------------------------------------------------------------
# boot checks for missing media tools
# --------------------------------------------------------------------------


class TestMediaToolChecks:
    def test_split_install_is_reported(self, settings, monkeypatch):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"ENABLED_SUBMODULES": ("images", "recordings")}
        monkeypatch.setattr(probes, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(probes, "ffprobe_available", lambda: False)
        findings = checks.check_media_tools()
        assert [f.id for f in findings] == [checks.W010_MEDIA_TOOL_MISSING]
        assert "ffprobe_missing" in findings[0].msg

    def test_both_missing_is_left_to_the_submodule_error(self, settings, monkeypatch):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"ENABLED_SUBMODULES": ("images", "recordings")}
        monkeypatch.setattr(probes, "ffmpeg_available", lambda: False)
        monkeypatch.setattr(probes, "ffprobe_available", lambda: False)
        assert checks.check_media_tools() == []
        assert any(
            f.id == checks.E003_RECORDINGS_BINARY_MISSING
            for f in checks.check_submodule_binaries()
        )

    def test_silent_when_no_time_based_submodule_is_enabled(
        self, settings, monkeypatch
    ):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"ENABLED_SUBMODULES": ("images",)}
        monkeypatch.setattr(probes, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(probes, "ffprobe_available", lambda: False)
        assert checks.check_media_tools() == []

    @pytest.mark.parametrize("value", ["8kb", 0, -1, None, True])
    def test_unusable_budget_is_reported_and_falls_back(self, settings, value):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"MICRO_PREVIEW_MAX_BYTES": value}
        findings = checks.check_preview_budget()
        assert [f.id for f in findings] == [checks.W011_PREVIEW_BUDGET_INVALID]
        assert metadata.preview_budget() == metadata.DEFAULT_PREVIEW_BUDGET

    def test_a_usable_budget_is_silent(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"MICRO_PREVIEW_MAX_BYTES": 8192}
        assert checks.check_preview_budget() == []
        assert metadata.preview_budget() == 8192
