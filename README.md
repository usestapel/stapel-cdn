# stapel-cdn

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

## Contributing

The source for this package lives inside the [ironmemo-backend](https://github.com/UCSoftworks) monorepo as a git submodule.

## License

MIT — see [LICENSE](LICENSE)
