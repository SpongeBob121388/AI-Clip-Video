"""任务调度器与状态机 — 统一任务调度、状态流转、取消/重试。

状态：pending → running → completed/failed/cancelled；failed/cancelled → pending（重试）。

三个原本缺失的关键性质：
1. 取消要真的生效。`transition(RUNNING)` 只改状态然后把 job 交回调用方继续跑，
   之后 RUNNING→COMPLETED 又因非法转移抛错。这里用 `claim()` 作为唯一入口，
   取消挂起的任务在认领时直接抛 JobCancelledError，worker 循环节点用 `should_stop()`。
2. 重试不能让新旧两个协程同时写同一个 job。每次 claim 递增 attempt，
   过期的 attempt 在写状态时被拒绝。
3. 多个订阅者。早期实现每个 job 只存一个回调，第二个 WebSocket 客户端会顶掉第一个。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.models.enums import JobStatus
from app.utils.logging import get_logger

logger = get_logger("services.scheduler")

_VALID_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.RUNNING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.PENDING},
    JobStatus.CANCELLED: {JobStatus.PENDING},
}

_TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
DEFAULT_MAX_RETAINED_JOBS = 200


class JobStateError(RuntimeError):
    pass


class JobCancelledError(JobStateError):
    """任务在认领前已被请求取消。worker 捕获后应把状态落为 CANCELLED 并停止。"""


class JobInfo(BaseModel):
    job_id: UUID = Field(default_factory=uuid4)
    project_id: str = Field(...)
    stage: str = Field(..., description="任务阶段：asr/detect/llm/render/export")
    status: JobStatus = Field(default=JobStatus.PENDING)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    message: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cancel_requested: bool = Field(default=False)
    input_revision: str = Field(default="")
    result: dict | None = Field(default=None)
    attempt: int = Field(default=0, ge=0, description="认领次数，用于拒绝过期 worker 的写入")


class JobScheduler:
    """单进程任务调度器。所有公开方法都是同步且线程安全的。"""

    def __init__(self, max_retained_jobs: int = DEFAULT_MAX_RETAINED_JOBS) -> None:
        self._jobs: dict[UUID, JobInfo] = {}
        self._subscribers: dict[UUID, list[Callable[[JobInfo], None]]] = {}
        self._lock = threading.RLock()
        self._max_retained_jobs = max_retained_jobs

    def submit(
        self,
        project_id: str,
        stage: str,
        input_revision: str = "",
    ) -> JobInfo:
        job = JobInfo(
            project_id=project_id,
            stage=stage,
            status=JobStatus.PENDING,
            input_revision=input_revision,
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._prune_locked()
        logger.info(
            "job_submitted",
            job_id=str(job.job_id),
            project_id=project_id,
            stage=stage,
        )
        return job

    def get_job(self, job_id: UUID) -> JobInfo | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, status: JobStatus | None = None) -> list[JobInfo]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return sorted(jobs, key=lambda j: j.created_at)

    def claim(self, job_id: UUID) -> JobInfo:
        """把任务从 PENDING 原子推进到 RUNNING，并返回带新 attempt 的 job。

        取消挂起时抛 JobCancelledError —— 这正是"请求取消后不该再开工"的执行点。
        """
        with self._lock:
            job = self._require(job_id)
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
                job.updated_at = datetime.now(UTC)
                self._notify_locked(job)
                raise JobCancelledError(f"任务已取消: {job_id}")
            if job.status is not JobStatus.PENDING:
                raise JobStateError(f"任务不可认领（当前 {job.status.value}）: {job_id}")
            job.status = JobStatus.RUNNING
            job.attempt += 1
            job.updated_at = datetime.now(UTC)
            self._notify_locked(job)
        logger.info("job_claimed", job_id=str(job_id), attempt=job.attempt)
        return job

    def should_stop(self, job_id: UUID) -> bool:
        """worker 的分块循环点：每完成一块就检查一次。"""
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and (job.cancel_requested or job.status is JobStatus.CANCELLED))

    def transition(
        self, job_id: UUID, new_status: JobStatus, attempt: int | None = None
    ) -> JobInfo:
        with self._lock:
            job = self._require(job_id)
            self._check_attempt(job, attempt)

            if new_status not in _VALID_TRANSITIONS.get(job.status, set()):
                raise JobStateError(
                    f"非法状态转移: {job.status.value} → {new_status.value}"
                )

            job.status = new_status

            job.updated_at = datetime.now(UTC)
            if job.status in _TERMINAL:
                self._subscribers.pop(job.job_id, None)
            self._prune_locked()
            self._notify_locked(job)

        logger.info(
            "job_transition",
            job_id=str(job_id),
            new_status=job.status.value,
        )
        return job

    def update_progress(
        self,
        job_id: UUID,
        progress: float,
        message: str = "",
        attempt: int | None = None,
    ) -> JobInfo:
        with self._lock:
            job = self._require(job_id)
            self._check_attempt(job, attempt)
            job.progress = max(0.0, min(1.0, progress))
            job.message = message[:500]
            job.updated_at = datetime.now(UTC)
            self._notify_locked(job)
            return job

    def complete(
        self, job_id: UUID, result: dict | None = None, attempt: int | None = None
    ) -> JobInfo:
        with self._lock:
            job = self._require(job_id)
            job.result = result
        return self.transition(job_id, JobStatus.COMPLETED, attempt=attempt)

    def fail(self, job_id: UUID, error: str, attempt: int | None = None) -> JobInfo:
        with self._lock:
            job = self._require(job_id)
            job.message = error[:500]
        return self.transition(job_id, JobStatus.FAILED, attempt=attempt)

    def request_cancel(self, job_id: UUID) -> JobInfo:
        """置取消标志；PENDING 任务直接落终态，RUNNING 任务由 worker 在检查点停止。"""
        with self._lock:
            job = self._require(job_id)
            job.cancel_requested = True
            job.updated_at = datetime.now(UTC)
            if job.status is JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                self._subscribers.pop(job.job_id, None)
                self._notify_locked(job)
            logger.info(
                "job_cancel_requested",
                job_id=str(job_id),
                status=job.status.value,
            )
            return job

    def retry(self, job_id: UUID) -> JobInfo:
        with self._lock:
            job = self._require(job_id)
            if job.status is JobStatus.RUNNING:
                raise JobStateError("任务仍在运行，不能重试（避免新旧协程同时写同一 job）")
            job.cancel_requested = False
            job.progress = 0.0
        return self.transition(job_id, JobStatus.PENDING)

    def subscribe(self, job_id: UUID, callback: Callable[[JobInfo], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(callback)

    def unsubscribe(self, job_id: UUID, callback: Callable[[JobInfo], None] | None = None) -> None:
        """不传 callback 时移除该任务的全部订阅（连接断开时的清理路径）。"""
        with self._lock:
            if callback is None:
                self._subscribers.pop(job_id, None)
                return
            callbacks = self._subscribers.get(job_id)
            if not callbacks:
                return
            if callback in callbacks:
                callbacks.remove(callback)
            if not callbacks:
                self._subscribers.pop(job_id, None)

    def _check_attempt(self, job: JobInfo, attempt: int | None) -> None:
        if attempt is not None and attempt != job.attempt:
            raise JobStateError(
                f"任务已被重新认领，拒绝过期写入: job={job.job_id} attempt={attempt} current={job.attempt}"
            )

    def _require(self, job_id: UUID) -> JobInfo:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobStateError(f"任务不存在: {job_id}")
        return job

    def _notify_locked(self, job: JobInfo) -> None:
        for callback in list(self._subscribers.get(job.job_id, ())):
            try:
                callback(job)
            except Exception as exc:  # noqa: BLE001 - 订阅者异常不能影响状态机
                logger.warning("job_subscriber_failed", job_id=str(job.job_id), error=str(exc))

    def _prune_locked(self) -> None:
        """终态任务数保持有界，长期运行的进程不会无限吃内存。"""
        if len(self._jobs) <= self._max_retained_jobs:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status in _TERMINAL),
            key=lambda j: j.updated_at,
        )
        excess = len(self._jobs) - self._max_retained_jobs
        for job in finished[:excess]:
            self._jobs.pop(job.job_id, None)
            self._subscribers.pop(job.job_id, None)

    @staticmethod
    def is_valid_transition(from_status: JobStatus, to_status: JobStatus) -> bool:
        return to_status in _VALID_TRANSITIONS.get(from_status, set())
