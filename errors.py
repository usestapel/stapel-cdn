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
#: The file is fine; this DEPLOYMENT cannot read that format. Configuration,
#: not bad input: STAPEL_CDN['ALLOWED_IMAGE_EXTENSIONS'] advertises an
#: extension no libvips loader in this build can decode. Kept apart from
#: ERR_400_INVALID_FORMAT on purpose — collapsing the two is the defect this
#: key was minted for. A HEIC avatar that libvips reads without complaint was
#: refused as "Invalid image file", so the uploader was told to fix a file that
#: had nothing wrong with it while the only party who could act — an operator —
#: heard nothing. Same split as stapel-workspaces'
#: error.503.profiles_not_configured (env-address-class v2 §2: a configuration
#: error degrades loudly instead of posing as the caller's mistake), and joined
#: by the same kind of startup check (checks.E004) so the deployment hears
#: about it before a user does. 503, not 4xx: from the caller's side this
#: endpoint genuinely cannot be served here.
ERR_503_IMAGE_DECODER_UNAVAILABLE = 'error.503.image_decoder_unavailable'

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
    ERR_503_IMAGE_DECODER_UNAVAILABLE:
        'This server cannot process {extension} images right now',
}

# Remediation (frontend-core-architecture §2.5). Only the key whose hint the
# status+name heuristic would get wrong is declared: 503 image_decoder_
# unavailable is NOT retryable and NOT fixable by the uploader. Nothing about
# the request changes the answer — a missing libvips loader is deterministic
# until an operator installs it or narrows the setting — so the honest hint is
# contact_support, exactly as stapel-workspaces declares for
# profiles_not_configured. `fix_input` would send the user back to re-export a
# file that was never the problem.
CDN_REMEDIATION = {
    ERR_503_IMAGE_DECODER_UNAVAILABLE: 'contact_support',
}

register_service_errors(CDN_ERRORS, remediation=CDN_REMEDIATION)


class CdnErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return CDN_ERRORS
