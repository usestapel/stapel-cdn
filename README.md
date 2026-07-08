# stapel-cdn

[![CI](https://github.com/usestapel/stapel-cdn/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-cdn/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-cdn/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-cdn)
[![PyPI](https://img.shields.io/pypi/v/stapel-cdn.svg)](https://pypi.org/project/stapel-cdn/)

> Media management — image/video/file upload, processing, CDN ref tracking

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

## Installation

```bash
pip install stapel-cdn
```

## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_cdn',
]
```

## Bus events

### Consumes
| `user.deleted` | [schema](schemas/consumes/user.deleted.json) |
| `user.deletion_initiated` | [schema](schemas/consumes/user.deletion_initiated.json) |

## License

MIT — see [LICENSE](LICENSE)
