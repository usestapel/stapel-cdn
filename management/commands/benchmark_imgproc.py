"""
Benchmark image processing: Python (pyvips) vs C++ (imgproc)

Usage:
    python manage.py benchmark_imgproc [--image-id=ID] [--iterations=N]
"""
import os
import time
import tempfile
import subprocess
from django.core.management.base import BaseCommand
from django.conf import settings

from stapel_cdn.models import Image
from stapel_cdn.services import ImageProcessingService


class Command(BaseCommand):
    help = 'Benchmark Python vs C++ image processing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--image-id',
            type=int,
            help='Specific image ID to benchmark'
        )
        parser.add_argument(
            '--iterations',
            type=int,
            default=3,
            help='Number of iterations (default: 3)'
        )

    def handle(self, *args, **options):
        # Find image
        if options['image_id']:
            image = Image.objects.get(id=options['image_id'])
        else:
            image = Image.objects.filter(is_processed=True).first()
            if not image:
                image = Image.objects.first()

        if not image:
            self.stderr.write('No images found')
            return

        self.stdout.write(f'Image: {image.file_hash[:8]} ({image.original_width}x{image.original_height})')
        self.stdout.write(f'Size: {image.original_size / 1024 / 1024:.2f} MB')
        self.stdout.write('')

        iterations = options['iterations']

        # Python benchmark
        self.stdout.write('=== Python (pyvips) ===')
        python_times = []
        for i in range(iterations):
            with tempfile.TemporaryDirectory() as tmpdir:
                orig_media = settings.MEDIA_ROOT
                settings.MEDIA_ROOT = tmpdir

                start = time.perf_counter()
                ImageProcessingService.generate_thumbnails_only(image)
                ImageProcessingService.generate_previews_only(image, apply_watermark=True)
                elapsed = time.perf_counter() - start

                settings.MEDIA_ROOT = orig_media

            python_times.append(elapsed)
            self.stdout.write(f'  Run {i+1}: {elapsed*1000:.0f}ms')

        avg_py = sum(python_times) / len(python_times)
        self.stdout.write(f'  Average: {avg_py*1000:.0f}ms\n')

        # C++ benchmark
        imgproc_path = '/usr/local/bin/imgproc'
        if not os.path.exists(imgproc_path):
            self.stdout.write(self.style.WARNING(f'C++ binary not found at {imgproc_path}'))
            return

        self.stdout.write('=== C++ (imgproc) ===')
        cpp_times = []
        for i in range(iterations):
            with tempfile.TemporaryDirectory() as tmpdir:
                output_dir = os.path.join(tmpdir, 'out')

                start = time.perf_counter()
                result = subprocess.run(
                    [imgproc_path, image.original.path, output_dir],
                    capture_output=True, text=True
                )
                elapsed = time.perf_counter() - start

                if result.returncode != 0:
                    self.stderr.write(f'Error: {result.stderr}')
                    continue

                cpp_times.append(elapsed)
                self.stdout.write(f'  Run {i+1}: {result.stdout.strip()} (wall={elapsed*1000:.0f}ms)')

        if cpp_times:
            avg_cpp = sum(cpp_times) / len(cpp_times)
            self.stdout.write(f'  Average: {avg_cpp*1000:.0f}ms\n')

            self.stdout.write('=== Comparison ===')
            self.stdout.write(f'  Python: {avg_py*1000:.0f}ms')
            self.stdout.write(f'  C++:    {avg_cpp*1000:.0f}ms')
            self.stdout.write(f'  Speedup: {avg_py/avg_cpp:.2f}x')
