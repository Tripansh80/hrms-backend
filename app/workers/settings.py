"""arq worker settings. Run with: arq app.workers.settings.WorkerSettings

Jobs are enqueued only AFTER the business transaction commits.
"""

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers.tasks.sweeps import mark_absent


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(get_settings().log_level)


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.core.redis import close_redis
    from app.db.session import dispose_engines

    await dispose_engines()
    await close_redis()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 3
    job_timeout = 15 * 60
    # TODO(M8): register run_import, send_notifications, report export
    functions: ClassVar[list[Any]] = []
    # TODO(M8): task timeout sweep (5 min), absent marking, yearly leave allocation,
    # document expiry, offer expiry, cleanup — see app/workers/tasks/sweeps.py
    cron_jobs: ClassVar[list[Any]] = [cron(mark_absent, hour=13, minute=0)]
