"""
URL configuration for CDN app.
"""
from django.urls import path
from .views import (
    ImageUploadView,
    AvatarUploadView,
    VideoUploadView,
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
    path('refs/sync/', RefSyncView.as_view(), name='refs-sync'),
]
