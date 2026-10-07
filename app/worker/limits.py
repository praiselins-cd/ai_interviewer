import logging
import sys

logger = logging.getLogger(__name__)

# Job Object handles MUST stay referenced for the worker process's entire
# lifetime: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE means the job (and everything
# assigned to it) is killed the moment its last handle is closed. Letting
# `job` fall out of scope and get garbage-collected right after this
# function returns would kill the worker instantly instead of limiting it.
_job_handles: dict[int, object] = {}


def release_process_limits(pid: int) -> None:
    """Call once the process has actually exited (supervisor reap), to stop
    holding its Job Object handle open forever."""
    _job_handles.pop(pid, None)


def apply_process_limits(pid: int, max_memory_mb: int = 1536) -> None:
    """Best-effort CPU/memory limiting for a worker process.

    Windows: assigns the process to a Job Object with a memory cap and
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, which guarantees the whole Chromium
    process tree dies with it (not just the Python parent) if the job
    handle is closed or the limit is hit.

    Linux/Docker (future phase): resource limiting is handled by the
    container's cgroup (`docker run --memory --cpus`) instead, so this is a
    no-op there for now — the same worker entrypoint just needs to be
    launched inside such a container unchanged.
    """
    if sys.platform != "win32":
        logger.info("apply_process_limits: no-op on this platform; use container cgroup limits instead.")
        return

    try:
        import win32job
        import win32api
        import win32con

        job = win32job.CreateJobObject(None, "")
        extended_info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        extended_info["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        )
        extended_info["ProcessMemoryLimit"] = max_memory_mb * 1024 * 1024
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, extended_info)

        handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
        win32job.AssignProcessToJobObject(job, handle)
        _job_handles[pid] = job  # keep alive — see module-level note above
        logger.info(f"Applied Job Object memory limit ({max_memory_mb}MB) to pid {pid}.")
    except Exception as e:
        logger.warning(f"Could not apply process limits to pid {pid}: {e}")
