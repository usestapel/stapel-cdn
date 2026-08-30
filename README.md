# stapel-cdn

[![CI](https://img.shields.io/github/actions/workflow/status/usestapel/stapel-cdn/ci.yml?branch=main&logo=github&label=CI)](https://github.com/usestapel/stapel-cdn/actions/workflows/ci.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/codecov/c/github/usestapel/stapel-cdn?branch=main&logo=codecov&label=coverage)](https://app.codecov.io/gh/usestapel/stapel-cdn)
[![pypi](https://img.shields.io/pypi/v/stapel-cdn?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/stapel-cdn/)
[![downloads](https://static.pepy.tech/badge/stapel-cdn/month)](https://pepy.tech/project/stapel-cdn)
[![python](https://img.shields.io/pypi/pyversions/stapel-cdn?logo=python&logoColor=white)](https://pypi.org/project/stapel-cdn/)
[![license](https://img.shields.io/github/license/usestapel/stapel-cdn)](https://github.com/usestapel/stapel-cdn/blob/main/LICENSE)
[![llms.txt](https://img.shields.io/badge/llms.txt-blue)](https://github.com/usestapel/stapel-cdn/blob/main/docs/llms.txt)

> Media management — image/video/file/audio upload, processing, CDN ref tracking

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

## Installation

```bash
pip install stapel-cdn
# + media submodules, as needed:
pip install 'stapel-cdn[images]'      # libvips-backed image processing (system: apt libvips-dev)
pip install 'stapel-cdn[video]'       # video submodule marker — ffmpeg is a system binary, VPS/prod only
pip install 'stapel-cdn[recordings]'  # recordings (audio) submodule marker — storage always works;
                                       # ffmpeg-audio compression is a separate, not-yet-implemented opt-in
```

## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_cdn',
]

STAPEL_CDN = {
    "ASSET_TYPES": ("avatar",),          # zero-infra default; add your own types here
    "ENABLED_SUBMODULES": ("images",),   # add "video" / "recordings" once you actually use them
}
```

`manage.py check` (tag `stapel_cdn`) fails loudly if an enabled submodule's system
binary/library is missing — see [MODULE.md](MODULE.md) and [CONFIG.MD](CONFIG.MD) for
the full submodule table and settings registry.

## Bus events

### Consumes
| `user.deleted` | [schema](schemas/consumes/user.deleted.json) |
| `user.deletion_initiated` | [schema](schemas/consumes/user.deletion_initiated.json) |
| `user.merged` | [schema](schemas/consumes/user.merged.json) |

## License

MIT — see [LICENSE](LICENSE)
