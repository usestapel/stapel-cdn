from django.urls import path, include

urlpatterns = [
    path("cdn/api/", include("stapel_cdn.urls")),
]
