from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError as exc:  # pragma: no cover - 当前发行目标为 macOS/Linux
    raise RuntimeError("spec-review 当前只支持提供 fcntl 文件锁的 macOS 和 Linux") from exc


@contextmanager
def repository_lock(repo: Path, timeout: float = 10.0):
    """串行化一个仓库内的短生命周期运行时进程。

    锁文件会保留，但内核锁会在进程退出时自动释放，因此异常退出不会留下需要人工
    删除的“僵尸锁”。文件内容只用于在超时时给出可诊断的进程信息。
    """

    state_dir = repo / ".spec-review"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "runtime.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    owner = handle.read().strip() or "未知进程"
                    raise RuntimeError(
                        f"仓库中的 spec-review 正在执行，等待 {timeout:g} 秒后仍未取得锁。"
                        f"锁持有者：{owner}"
                    )
                time.sleep(0.1)

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_at={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        handle.flush()
        yield lock_path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
