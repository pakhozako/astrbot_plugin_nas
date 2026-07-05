"""Runtime-only state helpers."""

import math
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit_per_minute = int(limit_per_minute)
        self._hits: dict[str, deque[float]] = {}

    def wait_seconds(self, uid: str) -> int:
        if self.limit_per_minute <= 0:
            return 0
        now = time.time()
        window = 60.0
        hits = self._hits.setdefault(str(uid), deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= self.limit_per_minute:
            return max(1, math.ceil(window - (now - hits[0])))
        hits.append(now)
        return 0


class RebuildState:
    def __init__(self, timeout_seconds: int, logger):
        self.timeout_seconds = int(timeout_seconds)
        self.logger = logger
        self.active = False
        self.started_at = 0.0
        self.reason = ""
        self.token = 0

    def begin(self, reason: str, allow_stale: bool = False) -> int | None:
        now = time.time()
        if self.active:
            elapsed = self._elapsed(now)
            stale = self._is_stale(elapsed)
            if not allow_stale or not stale:
                return None
            self.logger.warning(f"[NAS] 索引任务状态超时，接管新任务: {self.reason} 已运行 {elapsed} 秒")
        self.token += 1
        self.active = True
        self.started_at = now
        self.reason = reason
        return self.token

    def finish(self, token: int | None):
        if token is not None and token == self.token:
            self.active = False
            self.started_at = 0.0
            self.reason = ""

    def busy_message(self) -> str | None:
        if not self.active:
            return None
        elapsed = self._elapsed()
        if self._is_stale(elapsed):
            self.logger.warning(f"[NAS] 索引任务状态超时，自动释放: {self.reason} 已运行 {elapsed} 秒")
            self.finish(self.token)
            return None
        reason = self.reason or "重建"
        return f"NAS索引{reason}中，已运行 {elapsed} 秒，请稍后再试"

    def status_text(self) -> str:
        if not self.active:
            return "正常"
        elapsed = self._elapsed()
        if self._is_stale(elapsed):
            self.logger.warning(f"[NAS] 索引任务状态超时，状态查询自动释放: {self.reason} 已运行 {elapsed} 秒")
            self.finish(self.token)
            return "正常"
        reason = self.reason or "重建"
        return f"{reason}中 ({elapsed}秒)"

    def _elapsed(self, now: float | None = None) -> int:
        if not self.started_at:
            return 0
        return int((now or time.time()) - self.started_at)

    def _is_stale(self, elapsed: int) -> bool:
        return elapsed >= self._timeout()

    def _timeout(self) -> int:
        return max(60, self.timeout_seconds)
