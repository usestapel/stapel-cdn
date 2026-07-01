"""Custom error keys for the CDN service."""

from stapel_core.django.api.errors import ErrorKeysView, register_service_errors

ERR_400_NO_FILE = 'error.400.no_file'
ERR_400_INVALID_FORMAT = 'error.400.invalid_format'
ERR_413_FILE_TOO_LARGE = 'error.413.file_too_large'
ERR_400_INVALID_HASH = 'error.400.invalid_hash'
ERR_400_INVALID_IMAGE_TYPE = 'error.400.invalid_image_type'
ERR_404_NO_IMAGES = 'error.404.no_images'
ERR_400_FILE_HASH_REQUIRED = 'error.400.file_hash_required'
ERR_400_MISSING_FIELDS = 'error.400.missing_fields'
ERR_400_FILE_TYPE_NOT_ALLOWED = 'error.400.file_type_not_allowed'

CDN_ERRORS = {
    ERR_400_NO_FILE: 'No file provided',
    ERR_400_INVALID_FORMAT: 'Unsupported file format',
    ERR_413_FILE_TOO_LARGE: 'File is too large',
    ERR_400_INVALID_HASH: 'Invalid file hash',
    ERR_400_INVALID_IMAGE_TYPE: 'Invalid image type',
    ERR_404_NO_IMAGES: 'No processed images found',
    ERR_400_FILE_HASH_REQUIRED: 'file_hash parameter is required',
    ERR_400_MISSING_FIELDS: 'Required fields are missing',
    ERR_400_FILE_TYPE_NOT_ALLOWED: 'File type not allowed',
}

register_service_errors(CDN_ERRORS)


class CdnErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return CDN_ERRORS
