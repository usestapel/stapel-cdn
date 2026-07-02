"""
Tests for ImageAdminForm (admin upload validation and dimension extraction).
"""
import pytest
from io import BytesIO
from unittest.mock import patch
from PIL import Image as PILImage
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from stapel_cdn.forms import ImageAdminForm
from stapel_cdn.models import Image


def make_jpeg(width=120, height=80):
    img = PILImage.new('RGB', (width, height), color='red')
    buffer = BytesIO()
    img.save(buffer, format='JPEG')
    return SimpleUploadedFile('valid.jpg', buffer.getvalue(), content_type='image/jpeg')


def _clean(uploaded):
    """Run clean_original on a bare form with prepared cleaned_data."""
    form = ImageAdminForm()
    form.cleaned_data = {'original': uploaded}
    return form, form.clean_original()


class TestCleanOriginal:
    def test_no_file_skips_validation(self):
        form, result = _clean(None)
        assert result is None

    def test_valid_image_extracts_dimensions(self):
        form, result = _clean(make_jpeg(120, 80))
        assert result is not None
        assert form._image_dimensions == (120, 80)
        assert form._is_heic is False

    def test_invalid_extension_rejected(self):
        bad = SimpleUploadedFile('a.exe', b'MZ', content_type='application/octet-stream')
        form = ImageAdminForm()
        form.cleaned_data = {'original': bad}
        with pytest.raises(ValidationError, match='Invalid file extension'):
            form.clean_original()

    def test_invalid_image_content_rejected(self):
        bad = SimpleUploadedFile('a.jpg', b'not an image', content_type='image/jpeg')
        form = ImageAdminForm()
        form.cleaned_data = {'original': bad}
        with pytest.raises(ValidationError, match='Invalid image file'):
            form.clean_original()

    def test_heic_empty_file_rejected(self):
        empty = SimpleUploadedFile('a.heic', b'', content_type='image/heic')
        form = ImageAdminForm()
        form.cleaned_data = {'original': empty}
        with pytest.raises(ValidationError, match='empty'):
            form.clean_original()

    def test_heic_gets_placeholder_dimensions(self):
        heic = SimpleUploadedFile('a.heic', b'fake heic bytes', content_type='image/heic')
        form, result = _clean(heic)
        assert result is not None
        assert form._image_dimensions == (1, 1)
        assert form._is_heic is True


@pytest.mark.django_db
class TestFormSave:
    @pytest.fixture
    def user(self, db):
        from stapel_core.django.users.models import User

        return User.objects.create_user(
            username='formuser', email='form@example.com', password='x'
        )

    def _form_data(self, user):
        return {
            'file_hash': 'ff' * 32,
            'original_filename': 'valid.jpg',
            'file_extension': '.jpg',
            'type': 'product',
            'original_width': 0,
            'original_height': 0,
            'original_size': 1234,
            'refs': '[]',
            'processing_log': '',
            'uploaded_by': user.pk,
        }

    def test_save_sets_extracted_dimensions(self, user):
        form = ImageAdminForm(data=self._form_data(user), files={'original': make_jpeg(120, 80)})
        assert form.is_valid(), form.errors
        with patch('stapel_cdn.tasks.process_image_async'):
            instance = form.save(commit=True)
        assert instance.original_width == 120
        assert instance.original_height == 80
        assert Image.objects.filter(pk=instance.pk).exists()

    def test_save_heic_marks_unprocessed(self, user):
        data = self._form_data(user)
        data['original_filename'] = 'a.heic'
        data['file_extension'] = '.heic'
        heic = SimpleUploadedFile('a.heic', b'fake heic bytes', content_type='image/heic')
        form = ImageAdminForm(data=data, files={'original': heic})
        assert form.is_valid(), form.errors
        instance = form.save(commit=False)
        assert instance.original_width == 1
        assert instance.original_height == 1
        assert instance.is_processed is False
