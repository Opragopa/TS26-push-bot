#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small file-backed render queue shared by the Telegram bot and worker."""

import datetime as _dt
import fcntl
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path


QUEUE_VERSION = 1
DEFAULT_LEASE_SECONDS = 4 * 60 * 60
ACTIVE_STATUSES = frozenset({"queued", "preparing", "rendering"})
TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})
# A failed job must still dedupe by default: otherwise the sheet poller recreates
# the same broken render every minute. Callers can opt out with dedupe_statuses
# when they intentionally implement a retry button.
DEFAULT_DEDUPE_STATUSES = frozenset({"queued", "preparing", "rendering", "done", "error"})
# For enqueues that come from a deliberate human action (bot confirmation, HTTP
# trigger) rather than the periodic sheet poller. Re-submitting after a failure is
# the user asking for a retry, so "error" must not suppress the new job.
USER_RETRY_DEDUPE_STATUSES = frozenset({"queued", "preparing", "rendering", "done"})
# Keep the queue file bounded; without this it grows for the lifetime of the deploy
# and every enqueue/status call re-reads and re-writes the whole history.
MAX_TERMINAL_JOBS = 500


class RenderQueueError(Exception):
    pass


def default_queue_path():
    return Path(__file__).resolve().parent / "data" / "ae_render_queue.json"


def now_text():
    return _dt.datetime.now().isoformat(timespec="seconds")


def expires_at(seconds):
    return (_dt.datetime.now() + _dt.timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")


def is_expired(value):
    try:
        return _dt.datetime.fromisoformat(str(value or "")) <= _dt.datetime.now()
    except ValueError:
        return True


def load_queue_unlocked(queue_path):
    if not queue_path.exists():
        return {"version": QUEUE_VERSION, "jobs": []}
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RenderQueueError("Не удалось прочитать очередь {}: {}".format(queue_path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise RenderQueueError("Некорректный формат очереди {}".format(queue_path))
    return data


def save_queue_unlocked(queue_path, data):
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    previous_stat = queue_path.stat() if queue_path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ae_render_queue_", suffix=".json", dir=str(queue_path.parent))
    try:
        if previous_stat is not None:
            os.fchmod(descriptor, stat.S_IMODE(previous_stat.st_mode))
            try:
                os.fchown(descriptor, previous_stat.st_uid, previous_stat.st_gid)
            except PermissionError:
                pass
        else:
            os.fchmod(descriptor, 0o664)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, queue_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def locked_queue(queue_path):
    queue_path = Path(queue_path).expanduser()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = queue_path.with_suffix(queue_path.suffix + ".lock")
    lock_stream = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
    return queue_path, lock_stream


def prune_terminal_jobs(data, keep=MAX_TERMINAL_JOBS):
    """Drop the oldest finished jobs, keeping every active one."""
    jobs = data.get("jobs") or []
    terminal = [job for job in jobs if job.get("status") in TERMINAL_STATUSES]
    if len(terminal) <= keep:
        return 0
    terminal.sort(key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""))
    drop = {id(job) for job in terminal[: len(terminal) - keep]}
    data["jobs"] = [job for job in jobs if id(job) not in drop]
    return len(drop)


def enqueue(queue_path, kind, payload, source_key="", dedupe_statuses=None):
    queue_path, lock_stream = locked_queue(queue_path)
    try:
        data = load_queue_unlocked(queue_path)
        source_key = str(source_key or "").strip()
        if dedupe_statuses is None:
            dedupe_statuses = set(DEFAULT_DEDUPE_STATUSES)
        else:
            dedupe_statuses = set(dedupe_statuses)
        if source_key:
            for job in data["jobs"]:
                if job.get("source_key") == source_key and job.get("status") in dedupe_statuses:
                    return job, False
        job = {
            "id": uuid.uuid4().hex,
            "kind": str(kind),
            "payload": dict(payload or {}),
            "source_key": source_key,
            "status": "queued",
            "created_at": now_text(),
            "updated_at": now_text(),
        }
        data["jobs"].append(job)
        prune_terminal_jobs(data)
        save_queue_unlocked(queue_path, data)
        return job, True
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def claim_next(queue_path, lease_seconds=DEFAULT_LEASE_SECONDS):
    queue_path, lock_stream = locked_queue(queue_path)
    try:
        data = load_queue_unlocked(queue_path)
        for job in data["jobs"]:
            if job.get("status") == "queued":
                job["status"] = "preparing"
                job["attempt_count"] = int(job.get("attempt_count") or 0) + 1
                job["lease_expires_at"] = expires_at(lease_seconds)
                job["updated_at"] = now_text()
                save_queue_unlocked(queue_path, data)
                return dict(job)
        return None
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def recover_expired_jobs(queue_path):
    """Return jobs abandoned by a stopped worker to the queue."""
    queue_path, lock_stream = locked_queue(queue_path)
    try:
        data = load_queue_unlocked(queue_path)
        recovered = []
        for job in data["jobs"]:
            if job.get("status") not in {"preparing", "rendering"}:
                continue
            if not is_expired(job.get("lease_expires_at") or job.get("updated_at")):
                continue
            job["status"] = "queued"
            job["lease_expires_at"] = ""
            job["recovery_note"] = "Возвращено в очередь после истечения lease."
            job["updated_at"] = now_text()
            recovered.append(dict(job))
        if recovered:
            save_queue_unlocked(queue_path, data)
        return recovered
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def retry_failed_jobs(queue_path, limit=None, kind=None):
    """Put failed jobs back into the queue.

    Jobs created by the sheet poller dedupe on "error", so once a render fails it is
    never retried automatically — the plaque stays missing until somebody notices.
    This gives the operator an explicit way to say "try again", e.g. after opening
    the right project in After Effects.
    """
    queue_path, lock_stream = locked_queue(queue_path)
    try:
        data = load_queue_unlocked(queue_path)
        retried = []
        for job in data["jobs"]:
            if job.get("status") != "error":
                continue
            if kind and job.get("kind") != kind:
                continue
            if limit is not None and len(retried) >= limit:
                break
            job["status"] = "queued"
            job["error"] = ""
            job["lease_expires_at"] = ""
            job["retry_note"] = "Возвращено в очередь вручную."
            job["updated_at"] = now_text()
            retried.append(dict(job))
        if retried:
            save_queue_unlocked(queue_path, data)
        return retried
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def queue_counts(queue_path):
    """Status histogram plus the most recent errors, for operator-facing reports."""
    data = load_queue_unlocked(Path(queue_path).expanduser())
    counts = {}
    failures = []
    for job in data.get("jobs", []):
        status = job.get("status", "")
        counts[status] = counts.get(status, 0) + 1
        if status == "error":
            failures.append(job)
    failures.sort(key=lambda job: str(job.get("updated_at") or ""), reverse=True)
    return counts, failures


def update_job(queue_path, job_id, **changes):
    queue_path, lock_stream = locked_queue(queue_path)
    try:
        data = load_queue_unlocked(queue_path)
        for job in data["jobs"]:
            if job.get("id") == job_id:
                job.update(changes)
                job["updated_at"] = now_text()
                save_queue_unlocked(queue_path, data)
                return dict(job)
        raise RenderQueueError("Задание {} не найдено в очереди".format(job_id))
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()
