"""任务调度器与状态机测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.enums import JobStatus
from app.services.scheduler import (
    JobCancelledError,
    JobInfo,
    JobScheduler,
    JobStateError,
)


@pytest.mark.unit
class TestJobStateMachine:
    def test_valid_transitions(self) -> None:
        assert JobScheduler.is_valid_transition(JobStatus.PENDING, JobStatus.RUNNING)
        assert JobScheduler.is_valid_transition(JobStatus.RUNNING, JobStatus.COMPLETED)
        assert JobScheduler.is_valid_transition(JobStatus.RUNNING, JobStatus.FAILED)
        assert JobScheduler.is_valid_transition(JobStatus.FAILED, JobStatus.PENDING)

    def test_invalid_transitions(self) -> None:
        assert not JobScheduler.is_valid_transition(JobStatus.PENDING, JobStatus.COMPLETED)
        assert not JobScheduler.is_valid_transition(JobStatus.COMPLETED, JobStatus.RUNNING)

    def test_transition_success(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("proj1", "asr")
        updated = scheduler.transition(job.job_id, JobStatus.RUNNING)
        assert updated.status == JobStatus.RUNNING

    def test_transition_invalid_raises(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("proj1", "asr")
        with pytest.raises(JobStateError, match="非法状态转移"):
            scheduler.transition(job.job_id, JobStatus.COMPLETED)


@pytest.mark.unit
class TestJobScheduler:
    def test_submit_and_get(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("proj1", "asr", "rev1")
        assert job.project_id == "proj1"
        assert job.stage == "asr"
        assert job.status == JobStatus.PENDING
        assert job.input_revision == "rev1"
        retrieved = scheduler.get_job(job.job_id)
        assert retrieved is not None

    def test_list_jobs_filter_by_status(self) -> None:
        scheduler = JobScheduler()
        j1 = scheduler.submit("p1", "asr")
        j2 = scheduler.submit("p1", "render")
        scheduler.transition(j1.job_id, JobStatus.RUNNING)
        pending = scheduler.list_jobs(JobStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].job_id == j2.job_id

    def test_update_progress(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        scheduler.update_progress(job.job_id, 0.5, "识别中")
        updated = scheduler.get_job(job.job_id)
        assert updated.progress == 0.5
        assert updated.message == "识别中"

    def test_progress_clamped(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        scheduler.update_progress(job.job_id, 1.5, "")
        assert scheduler.get_job(job.job_id).progress == 1.0
        scheduler.update_progress(job.job_id, -0.5, "")
        assert scheduler.get_job(job.job_id).progress == 0.0

    def test_cancel_request(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        scheduler.request_cancel(job.job_id)
        assert scheduler.get_job(job.job_id).cancel_requested is True

    def test_cancel_prevents_running(self) -> None:
        """PENDING 被取消即落终态；随后试图进入 RUNNING 必须被拒绝。"""
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        cancelled = scheduler.request_cancel(job.job_id)
        assert cancelled.status == JobStatus.CANCELLED

        with pytest.raises(JobStateError, match="非法状态转移"):
            scheduler.transition(job.job_id, JobStatus.RUNNING)

        with pytest.raises(JobCancelledError):
            scheduler.claim(job.job_id)

    def test_claim_advances_and_blocks_double_claim(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        claimed = scheduler.claim(job.job_id)
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempt == 1
        # 已被认领的任务不能被第二个 worker 再次认领
        with pytest.raises(JobStateError, match="不可认领"):
            scheduler.claim(job.job_id)

    def test_stale_attempt_writes_are_rejected(self) -> None:
        """重试后旧协程不能再写状态，否则会出现两个 worker 交替改写同一 job。"""
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        first_attempt = scheduler.claim(job.job_id).attempt
        scheduler.fail(job.job_id, "boom", attempt=first_attempt)
        scheduler.retry(job.job_id)

        second_attempt = scheduler.claim(job.job_id).attempt
        assert second_attempt != first_attempt
        with pytest.raises(JobStateError, match="过期写入"):
            scheduler.update_progress(job.job_id, 0.9, "旧协程", attempt=first_attempt)
        # 当前 attempt 仍能正常写入
        assert scheduler.update_progress(job.job_id, 0.9, "新协程", attempt=second_attempt).progress == 0.9

    def test_stale_attempt_cannot_complete_job(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        stale = scheduler.claim(job.job_id).attempt
        scheduler.fail(job.job_id, "boom", attempt=stale)
        scheduler.retry(job.job_id)
        fresh = scheduler.claim(job.job_id).attempt

        with pytest.raises(JobStateError, match="过期写入"):
            scheduler.complete(job.job_id, {"ok": 1}, attempt=stale)
        assert scheduler.get_job(job.job_id).status == JobStatus.RUNNING
        assert scheduler.complete(job.job_id, {"ok": 1}, attempt=fresh).status == JobStatus.COMPLETED

    def test_should_stop_reflects_cancel(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "render")
        scheduler.claim(job.job_id)
        assert scheduler.should_stop(job.job_id) is False
        scheduler.request_cancel(job.job_id)
        assert scheduler.should_stop(job.job_id) is True

    def test_multiple_subscribers_all_notified(self) -> None:
        """旧实现每个 job 只存一个回调，第二个 WebSocket 客户端会顶掉第一个。"""
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        seen_a: list[JobStatus] = []
        seen_b: list[JobStatus] = []
        scheduler.subscribe(job.job_id, lambda j: seen_a.append(j.status))
        scheduler.subscribe(job.job_id, lambda j: seen_b.append(j.status))
        scheduler.claim(job.job_id)
        assert seen_a == [JobStatus.RUNNING]
        assert seen_b == [JobStatus.RUNNING]

    def test_subscriber_error_does_not_break_state_machine(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        scheduler.subscribe(job.job_id, lambda j: (_ for _ in ()).throw(RuntimeError("订阅者炸了")))
        ok: list[JobStatus] = []
        scheduler.subscribe(job.job_id, lambda j: ok.append(j.status))
        updated = scheduler.claim(job.job_id)
        assert updated.status == JobStatus.RUNNING
        assert ok == [JobStatus.RUNNING]

    def test_terminal_jobs_are_pruned(self) -> None:
        scheduler = JobScheduler(max_retained_jobs=5)
        ids = []
        for _ in range(12):
            job = scheduler.submit("p1", "asr")
            ids.append(job.job_id)
            scheduler.claim(job.job_id)
            scheduler.complete(job.job_id)
        assert len(scheduler.list_jobs()) <= 5
        assert scheduler.get_job(ids[-1]) is not None

    def test_unsubscribe_specific_callback(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        seen: list[JobStatus] = []
        cb = lambda j: seen.append(j.status)  # noqa: E731
        scheduler.subscribe(job.job_id, cb)
        scheduler.unsubscribe(job.job_id, cb)
        scheduler.claim(job.job_id)
        assert seen == []

    def test_retry_running_job_rejected(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        scheduler.claim(job.job_id)
        with pytest.raises(JobStateError, match="仍在运行"):
            scheduler.retry(job.job_id)

    def test_retry_failed_job(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        scheduler.transition(job.job_id, JobStatus.RUNNING)
        scheduler.transition(job.job_id, JobStatus.FAILED)
        retried = scheduler.retry(job.job_id)
        assert retried.status == JobStatus.PENDING

    def test_subscribe_callback(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        notifications: list[JobInfo] = []
        scheduler.subscribe(job.job_id, lambda j: notifications.append(j))
        scheduler.transition(job.job_id, JobStatus.RUNNING)
        assert len(notifications) == 1
        assert notifications[0].status == JobStatus.RUNNING

    def test_unsubscribe(self) -> None:
        scheduler = JobScheduler()
        job = scheduler.submit("p1", "asr")
        notifications: list[JobInfo] = []
        scheduler.subscribe(job.job_id, lambda j: notifications.append(j))
        scheduler.unsubscribe(job.job_id)
        scheduler.transition(job.job_id, JobStatus.RUNNING)
        assert len(notifications) == 0

    def test_nonexistent_job_raises(self) -> None:
        scheduler = JobScheduler()
        with pytest.raises(JobStateError, match="任务不存在"):
            scheduler.transition(uuid4(), JobStatus.RUNNING)
