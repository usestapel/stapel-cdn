"""Celery is optional for stapel-cdn.

With celery installed every processing job is a celery task under the name it
always had (beat schedules and ``task_routes`` reference those names). Without
it the jobs are core's :class:`stapel_core.background.InlineTask`: ``delay``
and ``apply_async`` run them in the calling process.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

from stapel_cdn import tasks

REPO = Path(__file__).resolve().parent.parent

NAMES = {
    "generate_thumbnails": "stapel_cdn.tasks.generate_thumbnails",
    "generate_previews": "stapel_cdn.tasks.generate_previews",
    "process_image_async": "stapel_cdn.tasks.process_image_async",
    "process_video_async": "stapel_cdn.tasks.process_video_async",
    "process_audio_async": "stapel_cdn.tasks.process_audio_async",
    "retry_unprocessed": "stapel_cdn.tasks.retry_unprocessed",
    "sweep_unclaimed": "stapel_cdn.tasks.sweep_unclaimed",
}

_WITHOUT_CELERY = (
    "import sys; sys.modules['celery'] = None; "
    "from stapel_cdn._codegen import _configure; _configure(); "
    "from stapel_cdn import tasks; "
    f"names = {NAMES!r}; "
    "jobs = [getattr(tasks, attr) for attr in names]; "
    "assert all(isinstance(job, tasks.InlineTask) for job in jobs), jobs; "
    "assert {attr: getattr(tasks, attr).name for attr in names} == names; "
    "print('inline')"
)


def test_celery_is_not_a_declared_dependency():
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    assert not [d for d in deps if d.split(">")[0].split("=")[0].strip() == "celery"]


def test_without_celery_every_job_is_inline_under_its_name(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _WITHOUT_CELERY],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "inline"


def test_with_celery_every_job_is_a_task_under_its_name():
    for attr, name in NAMES.items():
        job = getattr(tasks, attr)
        assert not isinstance(job, tasks.InlineTask), attr
        assert job.name == name

