"""
Service layer for iron-cdn - handles file processing and variant generation.
Uses pyvips for fast image processing with ladder downscaling optimization.
"""
from django.conf import settings
import os
import time
from datetime import datetime

import pyvips


class ImageProcessingService:
    """Service for processing and resizing images with watermarks using pyvips."""

    # Thumbnail sizes (no watermark, high priority) - sorted large to small for ladder
    THUMBNAIL_SIZES = [
        ('120', 120),
        ('64', 64),
        ('32', 32),
        ('16', 16),
    ]

    # Preview sizes - max 1080p, all WebP for best compression
    # Sorted large to small for ladder downscaling
    PREVIEW_SIZES = [
        ('1080', 1080),
        ('720', 720),
        ('480', 480),
        ('240', 240),
        ('160', 160),
    ]

    WEBP_QUALITY = 85
    JPEG_QUALITY = 85

    @classmethod
    def _resize(cls, img: pyvips.Image, target_height: int) -> pyvips.Image:
        """Resize image to target height. Returns same image if already smaller."""
        if img.height <= target_height:
            return img
        scale = target_height / img.height
        return img.resize(scale)

    @classmethod
    def _extract_embedded_thumbnail(cls, file_path: str) -> pyvips.Image | None:
        """
        Extract embedded thumbnail from image files.
        - HEIF/HEIC: ~512px embedded thumbnail
        - JPEG: EXIF thumbnail via shrink=8
        Returns None if extraction fails.
        """
        ext = os.path.splitext(file_path)[1].lower()

        # HEIF/HEIC - has larger embedded thumbnail (~512px)
        if ext in ('.heif', '.heic'):
            try:
                return pyvips.Image.heifload(file_path, thumbnail=True)
            except Exception:
                return None

        # JPEG - try shrink=8 for fast decode
        if ext in ('.jpg', '.jpeg'):
            try:
                return pyvips.Image.jpegload(file_path, shrink=8)
            except Exception:
                return None

        return None

    @classmethod
    def _add_watermark(cls, img: pyvips.Image, text: str = "Iron") -> pyvips.Image:
        """Add watermark text to image."""
        font_size = max(12, int(img.height * 0.05))

        markup = f'<span foreground="white" background="black">{text}</span>'
        text_img = pyvips.Image.text(
            markup,
            font=f"DejaVu Sans Bold {font_size}",
            dpi=72,
            rgba=True
        )

        padding = max(5, int(img.height * 0.02))
        x = max(padding, img.width - text_img.width - padding)
        y = max(padding, img.height - text_img.height - padding)

        if img.bands == 3:
            img = img.bandjoin(255)

        text_positioned = text_img.embed(x, y, img.width, img.height, extend='black')
        result = img.composite2(text_positioned, 'over')
        return result.flatten(background=[255, 255, 255])

    @classmethod
    def generate_thumbnails_only(cls, image_model) -> str:
        """
        Generate thumbnails using embedded thumbnail or shrink-on-load.
        Returns log string.
        """
        log_lines = []
        log_lines.append(f"[{datetime.now().isoformat()}] Starting thumbnail generation")

        file_path = image_model.original.path
        output_dir = os.path.join(settings.MEDIA_ROOT, image_model.type, image_model.file_hash)
        os.makedirs(output_dir, exist_ok=True)

        total_start = time.perf_counter()

        # Try embedded thumbnail first (HEIF ~512px, JPEG shrink=8)
        start = time.perf_counter()
        thumb = cls._extract_embedded_thumbnail(file_path)
        embed_time = int((time.perf_counter() - start) * 1000)

        if thumb and thumb.height >= 120:
            log_lines.append(f"  Embedded thumbnail: {thumb.height}px ({embed_time}ms)")
            current = cls._resize(thumb, 120).copy_memory()
        else:
            log_lines.append(f"  No embedded thumbnail, using shrink-on-load")
            start = time.perf_counter()
            current = pyvips.Image.thumbnail(file_path, 240, height=120, size='down')
            current = current.copy_memory()
            log_lines.append(f"  Load 120px: {int((time.perf_counter()-start)*1000)}ms")

        # Ladder: 120 -> 64 -> 32 -> 16
        sizes_generated = []
        for name, height in cls.THUMBNAIL_SIZES:
            start = time.perf_counter()
            current = cls._resize(current, height)
            current.webpsave(os.path.join(output_dir, f'{name}.webp'), Q=cls.WEBP_QUALITY)
            current = current.copy_memory()
            elapsed = int((time.perf_counter() - start) * 1000)
            sizes_generated.append(f"{name}={elapsed}ms")

        total_time = int((time.perf_counter() - total_start) * 1000)
        log_lines.append(f"  Thumbnails: {', '.join(sizes_generated)}")
        log_lines.append(f"  Total thumbnail time: {total_time}ms")

        return '\n'.join(log_lines)

    @classmethod
    def generate_previews_only(cls, image_model, apply_watermark: bool = True) -> str:
        """
        Generate previews using ladder downscaling. Max 1080p, all WebP.
        Creates all variant files even if image is smaller (without upscaling).
        Returns log string.
        """
        log_lines = []
        log_lines.append(f"[{datetime.now().isoformat()}] Starting preview generation")

        file_path = image_model.original.path
        output_dir = os.path.join(settings.MEDIA_ROOT, image_model.type, image_model.file_hash)
        os.makedirs(output_dir, exist_ok=True)

        total_start = time.perf_counter()

        # Load image - if > 1080p shrink, otherwise load as-is
        start = time.perf_counter()
        img_info = pyvips.Image.new_from_file(file_path, access='sequential')
        orig_height = img_info.height

        if orig_height > 1080:
            # Shrink to 1080p
            current = pyvips.Image.thumbnail(file_path, 2160, height=1080)
            log_lines.append(f"  Original: {img_info.width}x{orig_height}, shrunk to {current.width}x{current.height}")
        else:
            # Load as-is
            current = pyvips.Image.new_from_file(file_path)
            log_lines.append(f"  Loaded as-is: {current.width}x{current.height}")

        current = current.copy_memory()
        load_time = int((time.perf_counter() - start) * 1000)
        log_lines.append(f"  Load time: {load_time}ms")

        # Generate all preview sizes using ladder downscaling
        # For sizes >= loaded_height, save without resize (same quality, just different filename)
        sizes_generated = []
        for name, target_height in cls.PREVIEW_SIZES:
            start = time.perf_counter()

            if current.height > target_height:
                # Resize down
                current = cls._resize(current, target_height)
                resized = True
            else:
                # Image already smaller - use as-is
                resized = False

            output = cls._add_watermark(current) if apply_watermark else current
            output.webpsave(os.path.join(output_dir, f'{name}.webp'), Q=cls.WEBP_QUALITY)

            # Also save 720.jpg for legacy browser compatibility
            if name == '720':
                output.jpegsave(os.path.join(output_dir, '720.jpg'), Q=cls.JPEG_QUALITY)

            current = current.copy_memory()

            elapsed = int((time.perf_counter() - start) * 1000)
            resize_info = f"resize to {target_height}" if resized else f"as-is {current.height}px"
            sizes_generated.append(f"{name}({resize_info})={elapsed}ms")

        total_time = int((time.perf_counter() - total_start) * 1000)
        log_lines.append(f"  Previews: {', '.join(sizes_generated)}")
        log_lines.append(f"  Total preview time: {total_time}ms")

        return '\n'.join(log_lines)

    @classmethod
    def process_image(cls, image_model) -> str:
        """
        Process an image - extract metadata and generate all variants.
        Returns combined log string.
        """
        log_lines = []
        log_lines.append(f"=== Processing {image_model.file_hash[:8]} ===")

        file_path = image_model.original.path

        # Update dimensions if needed
        if image_model.original_width <= 1 or image_model.original_height <= 1:
            img = pyvips.Image.new_from_file(file_path, access='sequential')
            image_model.original_width = img.width
            image_model.original_height = img.height
            image_model.save(update_fields=['original_width', 'original_height'])
            log_lines.append(f"Updated dimensions: {img.width}x{img.height}")

        # Generate thumbnails
        thumb_log = cls.generate_thumbnails_only(image_model)
        log_lines.append(thumb_log)

        # Generate previews
        preview_log = cls.generate_previews_only(image_model)
        log_lines.append(preview_log)

        # Mark as processed and save log
        combined_log = '\n'.join(log_lines)
        image_model.is_processed = True
        image_model.processing_log = combined_log
        image_model.save(update_fields=['is_processed', 'processing_log'])

        return combined_log

    @classmethod
    def generate_image_variants(cls, image_model):
        """Alias for process_image for backwards compatibility."""
        return cls.process_image(image_model)


class VideoProcessingService:
    """Service for processing videos."""

    @classmethod
    def process_video(cls, video_model):
        """Process a video file - extract metadata and generate variants."""
        # TODO: Implement video processing with ffmpeg
        video_model.is_processed = True
        video_model.save()
        return video_model
