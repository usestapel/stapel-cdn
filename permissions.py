"""Permission classes stapel-cdn names from settings.

A settings seam takes DOTTED PATHS, and DRF's list form means "all of these
must pass" — so an OR of two classes has no spelling as a list. It has one as
a name: the composed permission below is a single importable object, which is
what makes ``STAPEL_CDN["DESCRIBE_PERMISSIONS"]`` able to carry the module's
default posture instead of a weaker one that happens to be expressible.
"""

from rest_framework.permissions import IsAuthenticated
from stapel_core.django.api.permissions import IsServiceRequest

#: The read-endpoint seam: a signed-in caller (guest sessions included —
#: ``AUTH_ANONYMOUS`` makes those ``is_authenticated``, and a guest reading a
#: chat has attachments to draw) **or** another service on the internal
#: channel. Identical to what ``FileExistsView`` pins inline; named here so
#: the describe endpoint can take it from settings and a host can swap it for
#: something tighter (``IsServiceRequest`` alone) or looser
#: (``AllowAny`` — see CONFIG.MD) without subclassing a view.
IsAuthenticatedOrService = IsAuthenticated | IsServiceRequest

__all__ = ["IsAuthenticatedOrService"]
