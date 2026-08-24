"""
v1 URL set for the CDN app (api-versioning.md §2).

Paths inside are unchanged from the pre-v1 layout; the root ``urls.py``
mounts this module under the mandatory ``v1/`` sub-prefix, producing the
canonical ``/cdn/api/v1/...`` surface.
"""
from django.urls import path
from .errors import CdnErrorKeysView
from .views import (
    ImageUploadView,
    AvatarUploadView,
    VideoUploadView,
    DescribeMediaView,
    FileExistsView,
    RandomImageView,
    TypedImageUploadView,
    RefSyncView,
    GenericFileUploadView,
)

urlpatterns = [
    path('upload/image/', ImageUploadView.as_view(), name='upload-image'),
    path('upload/avatar/', AvatarUploadView.as_view(), name='upload-avatar'),
    path('upload/video/', VideoUploadView.as_view(), name='upload-video'),
    path('upload/file/', GenericFileUploadView.as_view(), name='upload-file'),
    path('images/<str:image_type>/random/', RandomImageView.as_view(), name='random-image'),
    path('images/<str:image_type>/upload/', TypedImageUploadView.as_view(), name='typed-image-upload'),
    path('file/exists/', FileExistsView.as_view(), name='file-exists'),

    # The browser's half of cdn.describe_many — render metadata for refs the
    # caller holds but did not necessarily upload (a chat attachment).
    path('describe/', DescribeMediaView.as_view(), name='describe-media'),

    path('refs/sync/', RefSyncView.as_view(), name='refs-sync'),

    # Error-key registry for the stapel-translate collector (service/staff only).
    path('error-keys/', CdnErrorKeysView.as_view(), name='error-keys'),
]
