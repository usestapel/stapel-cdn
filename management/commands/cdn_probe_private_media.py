"""cdn_probe_private_media — measure whether the public media route serves the
private and protected trees.

The library never hands out a public URL for a private or protected object
(``protected.stored_url``), but whether the web server in front of
``MEDIA_ROOT`` *serves* those paths to whoever guesses them is deployment
configuration this process cannot read. So it is measured: a random canary is
written under each tree, fetched through the public media URL exactly as a
stranger would, and removed again.

    manage.py cdn_probe_private_media --base-url https://media.example.com

Verdicts, one line per tree: ``denied (<status>)`` when the canary did not
come back, ``EXPOSED`` when its exact bytes did, ``UNMEASURED`` when the fetch
itself failed. Exit status is non-zero unless every tree was measured and
denied — an unreachable route is not a pass.
"""
from __future__ import annotations

import os
import secrets
import urllib.error
import urllib.request
from urllib.parse import urljoin

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _fetch(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "stapel-cdn-probe"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(4096)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


class Command(BaseCommand):
    help = (
        "Fetch a canary under the private and protected media prefixes through "
        "the public media URL; fail unless both are denied."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            default="",
            help=(
                "Scheme and host the public media route is served on, e.g. "
                "https://media.example.com. Not needed when MEDIA_URL is absolute."
            ),
        )
        parser.add_argument("--timeout", type=float, default=10.0)

    def handle(self, *args, **options):
        from stapel_cdn.protected import private_prefix, protected_prefix

        media_url = str(settings.MEDIA_URL or "/")
        base = options["base_url"]
        if not media_url.startswith(("http://", "https://")):
            if not base:
                raise CommandError(
                    "MEDIA_URL is relative; pass --base-url with the scheme and "
                    "host the public media route is served on."
                )
            media_url = urljoin(base.rstrip("/") + "/", media_url.lstrip("/"))
        if not media_url.endswith("/"):
            media_url += "/"

        trees = [("protected", protected_prefix())]
        if private_prefix():
            trees.insert(0, ("private", private_prefix()))
        else:
            self.stderr.write(
                "PRIVATE_MEDIA_PREFIX is empty: private objects share the public "
                "tree and cannot be denied by prefix (checks.W016)."
            )

        failed = not private_prefix()
        for label, prefix in trees:
            token = secrets.token_hex(16)
            rel = f"{prefix}.stapel-probe-{token}.txt"
            path = os.path.join(settings.MEDIA_ROOT, rel)
            body = f"stapel-cdn probe {token}".encode()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                with open(path, "wb") as handle:
                    handle.write(body)
                url = media_url + rel
                try:
                    status, got = _fetch(url, options["timeout"])
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    failed = True
                    self.stdout.write(f"{label}/: UNMEASURED ({exc}) — {url}")
                    continue
                if status == 200 and got.startswith(body):
                    failed = True
                    self.stdout.write(f"{label}/: EXPOSED — {url} served the canary")
                else:
                    self.stdout.write(f"{label}/: denied ({status})")
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

        self.stdout.write(f"checked {len(trees)} prefix(es) under {media_url}")
        if failed:
            raise CommandError(
                "private media is not proven denied on the public media route"
            )
