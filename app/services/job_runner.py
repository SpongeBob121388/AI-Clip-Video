"""后台任务执行器 —— 把 service 调用挂到调度器上跑。

统一负责：认领任务（含取消检查）、进度上报、WebSocket 广播、异常归一化。
业务端点因此只需要提交工作函数，不必各自复制一遍状态机操作。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from app.models.enums import JobStatus
from app.services.scheduler import (
    JobCancelledError,
    JobInfo,
    JobScheduler,
    JobStateError,
)
from app.utils.log_redaction import redact_value
from app.utils.logging import get_logger

logger = get_logger("services.job_runner")

WorkFn = Callable[["JobContext"], Awaitable[dict]]


class JobWasCancelledError(RuntimeError):
    """工作函数主动报告"用户已取消"，让任务落 CANCELLED 而不是 FAILED。"""


@dataclass
class JobContext:
    job_id: UUID
    attempt: int
    _scheduler: JobScheduler

    def progress(self, value: float, message: str = "") -> None:
        self._scheduler.update_progress(self.job_id, value, message, attempt=self.attempt)

    def should_stop(self) -> bool:
        return self._scheduler.should_stop(self.job_id)


@dataclass
class JobRunner:
    scheduler: JobScheduler = field(default_factory=JobScheduler)
    _tasks: dict[UUID, asyncio.Task] = field(default_factory=dict)
    on_progress: Callable[[str, float, str], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        # 进度经由订阅者扇出到 WebSocket；订阅者异常不会伤害状态机
        pass

    def start(
        self,
        project_id: str,
        stage: str,
        work: WorkFn,
        input_revision: str = "",
    ) -> JobInfo:
        job = self.scheduler.submit(project_id, stage, input_revision)
        self._subscribe(job.job_id)
        task = asyncio.get_running_loop().create_task(self._run(job, work))
        self._tasks[job.job_id] = task
        task.add_done_callback(lambda _t, jid=job.job_id: self._tasks.pop(jid, None))
        return job

    def _subscribe(self, job_id: UUID) -> None:
        if self.on_progress is None:
            return

        def _callback(info: JobInfo) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover - 只在无循环的调用路径发生
                return
            loop.create_task(
                self.on_progress(str(info.job_id), info.progress, info.message)
            )

        self.scheduler.subscribe(job_id, _callback)

    async def _run(self, job: JobInfo, work: WorkFn) -> None:
        try:
            claimed = self.scheduler.claim(job.job_id)
        except JobCancelledError:
            logger.info("job_cancelled_before_start", job_id=str(job.job_id))
            return
        except JobStateError as exc:
            logger.error("job_claim_failed", job_id=str(job.job_id), error=str(exc))
            return

        context = JobContext(claimed.job_id, claimed.attempt, self.scheduler)
        try:
            result = await work(context)
            self.scheduler.complete(claimed.job_id, result=result, attempt=claimed.attempt)
            logger.info("job_completed", job_id=str(claimed.job_id), stage=claimed.stage)
        except JobStateError as exc:
            # 过期 attempt 的写入被拒绝，说明任务已被重新认领；不要覆盖新 worker
            logger.warning("job_write_rejected", job_id=str(claimed.job_id), error=str(exc))
        except JobWasCancelledError:
            self._safe_transition(claimed, JobStatus.CANCELLED, "任务已取消")
            logger.info("job_cancelled_by_worker", job_id=str(claimed.job_id))
        except asyncio.CancelledError:
            self._safe_transition(claimed, JobStatus.CANCELLED, "任务已取消")
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务必须自己收尾
            logger.error(
                "job_failed",
                job_id=str(claimed.job_id),
                error_type=type(exc).__name__,
                error=redact_value(str(exc))[:300],
            )
            # 任务消息是给终端用户看的：异常类名留在日志里，不要出现在界面上
            self._safe_transition(claimed, JobStatus.FAILED, str(exc) or type(exc).__name__)

    def _safe_transition(self, job: JobInfo, status: JobStatus, message: str) -> None:
        try:
            if status is JobStatus.FAILED:
                self.scheduler.fail(job.job_id, message, attempt=job.attempt)
            else:
                self.scheduler.transition(job.job_id, status, attempt=job.attempt)
        except JobStateError as exc:
            logger.warning("job_finalize_skipped", job_id=str(job.job_id), error=str(exc))

    async def shutdown(self, cancel: bool = True) -> None:
        for task in list(self._tasks.values()):
            if cancel and not task.done():
                task.cancel()
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()


_runner: JobRunner | None = None


def get_job_runner() -> JobRunner:
    global _runner
    if _runner is None:
        from app.api.websocket import broadcast_progress

        async def _broadcast(job_id: str, progress: float, message: str) -> None:
            await broadcast_progress(job_id, progress, message)

        _runner = JobRunner(on_progress=_broadcast)
    return _runner


def reset_job_runner() -> None:
    global _runner
    _runner = None
