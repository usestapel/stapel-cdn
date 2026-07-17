"""
Tests for CDN services.
"""
import pytest
import os
from io import BytesIO
from unittest.mock import patch, MagicMock
from PIL import Image as PILImage
from stapel_cdn.models import Image, Video
from stapel_cdn.services import ImageProcessingService, VideoProcessingService
from stapel_cdn.watermarks import text_watermark
from stapel_core.django.users.models import User


@pytest.fixture
def user(db):
    """Create a test user."""
    return User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )


@pytest.fixture
def temp_image_file():
    """Create a temporary image file."""
    img = PILImage.new('RGB', (200, 150), color='red')
    buffer = BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return buffer


class TestImageProcessingService:
    """Tests for ImageProcessingService."""

    def test_thumbnail_sizes_ordered(self):
        """Test that thumbnail sizes are ordered from large to small."""
        sizes = [s[1] for s in ImageProcessingService.THUMBNAIL_SIZES]
        assert sizes == sorted(sizes, reverse=True)

    def test_preview_sizes_ordered(self):
        """Test that preview sizes are ordered from large to small."""
        sizes = [s[1] for s in ImageProcessingService.PREVIEW_SIZES]
        assert sizes == sorted(sizes, reverse=True)

    def test_webp_quality_reasonable(self):
        """Test that WebP quality is a reasonable value."""
        assert 1 <= ImageProcessingService.WEBP_QUALITY <= 100
        assert ImageProcessingService.WEBP_QUALITY >= 70  # Should be decent quality

    def test_jpeg_quality_reasonable(self):
        """Test that JPEG quality is a reasonable value."""
        assert 1 <= ImageProcessingService.JPEG_QUALITY <= 100
        assert ImageProcessingService.JPEG_QUALITY >= 70

    @patch('stapel_cdn.services.pyvips')
    def test_resize_smaller_image_unchanged(self, mock_pyvips):
        """Test that _resize returns same image if already smaller than target."""
        mock_img = MagicMock()
        mock_img.height = 50

        result = ImageProcessingService._resize(mock_img, 100)

        assert result == mock_img
        mock_img.resize.assert_not_called()

    @patch('stapel_cdn.services.pyvips')
    def test_resize_larger_image(self, mock_pyvips):
        """Test that _resize scales down larger images."""
        mock_img = MagicMock()
        mock_img.height = 200
        mock_resized = MagicMock()
        mock_img.resize.return_value = mock_resized

        result = ImageProcessingService._resize(mock_img, 100)

        assert result == mock_resized
        mock_img.resize.assert_called_once_with(0.5)  # 100/200

    @patch('stapel_cdn.services.pyvips')
    def test_extract_embedded_thumbnail_heic(self, mock_pyvips):
        """Test embedded thumbnail extraction for HEIC files."""
        mock_thumb = MagicMock()
        mock_pyvips.Image.heifload.return_value = mock_thumb

        result = ImageProcessingService._extract_embedded_thumbnail('/path/to/file.heic')

        mock_pyvips.Image.heifload.assert_called_once_with('/path/to/file.heic', thumbnail=True)
        assert result == mock_thumb

    @patch('stapel_cdn.services.pyvips')
    def test_extract_embedded_thumbnail_jpeg(self, mock_pyvips):
        """Test embedded thumbnail extraction for JPEG files."""
        mock_thumb = MagicMock()
        mock_pyvips.Image.jpegload.return_value = mock_thumb

        result = ImageProcessingService._extract_embedded_thumbnail('/path/to/file.jpg')

        mock_pyvips.Image.jpegload.assert_called_once_with('/path/to/file.jpg', shrink=8)
        assert result == mock_thumb

    @patch('stapel_cdn.services.pyvips')
    def test_extract_embedded_thumbnail_unsupported(self, mock_pyvips):
        """Test embedded thumbnail extraction for unsupported formats."""
        result = ImageProcessingService._extract_embedded_thumbnail('/path/to/file.png')

        assert result is None
        mock_pyvips.Image.heifload.assert_not_called()
        mock_pyvips.Image.jpegload.assert_not_called()

    @patch('stapel_cdn.services.pyvips')
    def test_extract_embedded_thumbnail_exception(self, mock_pyvips):
        """Test embedded thumbnail extraction handles exceptions."""
        mock_pyvips.Image.jpegload.side_effect = Exception("Failed to load")

        result = ImageProcessingService._extract_embedded_thumbnail('/path/to/file.jpeg')

        assert result is None

    def test_watermark_disabled_by_default(self):
        """No engine configured — image passes through unchanged."""
        mock_img = MagicMock()
        assert ImageProcessingService._add_watermark(mock_img) is mock_img

    def test_watermark_engine_from_settings(self, settings):
        """The STAPEL_CDN['WATERMARK'] callable is applied by the pipeline."""
        mock_img, marked = MagicMock(), MagicMock()
        settings.STAPEL_CDN = {"WATERMARK": lambda img: marked}
        assert ImageProcessingService._add_watermark(mock_img) is marked

    def test_watermark_engine_dotted_path_empty_text(self, settings):
        """A dotted-path engine resolves; the built-in text engine without
        WATERMARK_TEXT is a no-op."""
        mock_img = MagicMock()
        settings.STAPEL_CDN = {
            "WATERMARK": "stapel_cdn.watermarks.text_watermark",
        }
        assert ImageProcessingService._add_watermark(mock_img) is mock_img

    @patch('stapel_cdn.watermarks.pyvips')
    def test_add_watermark(self, mock_pyvips):
        """Test that watermark is added to image."""
        mock_img = MagicMock()
        mock_img.height = 1000
        mock_img.width = 1500
        mock_img.bands = 3

        mock_text = MagicMock()
        mock_text.width = 100
        mock_text.height = 50
        mock_pyvips.Image.text.return_value = mock_text

        mock_positioned = MagicMock()
        mock_text.embed.return_value = mock_positioned

        mock_alpha = MagicMock()
        mock_img.bandjoin.return_value = mock_alpha

        mock_composite = MagicMock()
        mock_alpha.composite2.return_value = mock_composite

        mock_flattened = MagicMock()
        mock_composite.flatten.return_value = mock_flattened

        result = text_watermark(mock_img, "Test")

        mock_pyvips.Image.text.assert_called_once()
        assert result == mock_flattened

    @patch('stapel_cdn.watermarks.pyvips')
    def test_add_watermark_with_alpha(self, mock_pyvips):
        """Test that watermark works with images that already have alpha."""
        mock_img = MagicMock()
        mock_img.height = 500
        mock_img.width = 800
        mock_img.bands = 4  # Already has alpha

        mock_text = MagicMock()
        mock_text.width = 80
        mock_text.height = 40
        mock_pyvips.Image.text.return_value = mock_text

        mock_positioned = MagicMock()
        mock_text.embed.return_value = mock_positioned

        mock_composite = MagicMock()
        mock_img.composite2.return_value = mock_composite

        mock_flattened = MagicMock()
        mock_composite.flatten.return_value = mock_flattened

        text_watermark(mock_img, "Test")

        # Should not call bandjoin since already has alpha
        mock_img.bandjoin.assert_not_called()


class TestVideoProcessingService:
    """Tests for VideoProcessingService."""

    @pytest.mark.django_db
    def test_process_video_sets_processed(self, user):
        """Test that process_video marks video as processed."""
        video = Video.objects.create(
            file_hash='z' * 64,
            original_filename='test.mp4',
            file_extension='.mp4',
            original_size=1000000,
            uploaded_by=user,
            is_processed=False,
        )

        result = VideoProcessingService.process_video(video)

        video.refresh_from_db()
        assert video.is_processed is True
        assert result == video

    @pytest.mark.django_db
    def test_process_video_returns_model(self, user):
        """Test that process_video returns the video model."""
        video = Video.objects.create(
            file_hash='y' * 64,
            original_filename='clip.mp4',
            file_extension='.mp4',
            original_size=500000,
            uploaded_by=user,
        )

        result = VideoProcessingService.process_video(video)

        assert isinstance(result, Video)
        assert result.id == video.id


@pytest.mark.django_db
class TestImageProcessingIntegration:
    """Integration tests for ImageProcessingService (requires pyvips)."""

    @pytest.fixture
    def image_with_file(self, user, tmp_path, settings):
        """Create an image instance with an actual file."""
        # Create a real image file
        img = PILImage.new('RGB', (800, 600), color='green')

        # Setup media root
        settings.MEDIA_ROOT = str(tmp_path)

        # Create directory structure
        hash_val = 'testimg1' + 'a' * 56
        img_dir = tmp_path / 'images' / hash_val
        img_dir.mkdir(parents=True, exist_ok=True)

        # Save image
        img_path = img_dir / 'original.jpg'
        img.save(str(img_path), format='JPEG')

        # Create model instance
        image = Image.objects.create(
            file_hash=hash_val,
            original_filename='original.jpg',
            file_extension='.jpg',
            original_width=800,
            original_height=600,
            original_size=os.path.getsize(str(img_path)),
            uploaded_by=user,
            is_processed=False,
        )

        # Mock the original field path
        image.original = MagicMock()
        image.original.path = str(img_path)

        return image

    def test_generate_thumbnails_only_returns_log(self, image_with_file, settings):
        """Test that generate_thumbnails_only returns a log string."""
        try:
            import pyvips
        except ImportError:
            pytest.skip("pyvips not installed")

        log = ImageProcessingService.generate_thumbnails_only(image_with_file)

        assert isinstance(log, str)
        assert 'Starting thumbnail generation' in log

    def test_generate_previews_only_returns_log(self, image_with_file, settings):
        """Test that generate_previews_only returns a log string."""
        try:
            import pyvips
        except ImportError:
            pytest.skip("pyvips not installed")

        log = ImageProcessingService.generate_previews_only(image_with_file, apply_watermark=False)

        assert isinstance(log, str)
        assert 'Starting preview generation' in log
