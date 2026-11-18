"""线程安全事件总线。

执行引擎（async 事件循环线程）发布事件，HTTP/SSE 线程订阅，
保证前端实时看到 Agent 思考、工具调用、验证结果与调度决策。

实现要点
--------
* 每个订阅者拥有独立有界队列，慢消费者只会丢自己的事件，不会阻塞执行；
* 每个 run 分配单调递增 seq，SSE 断线重连可用 Last-Event-ID 续传；
* 历史轨迹有上限，避免长任务内存膨胀。
"""
from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict
from typing import Any, Iterable

from .types import Event

_MAX_HISTORY = 4000
_QUEUE_SIZE = 2000


class Subscription:
    """一次事件订阅；关闭后队列不再接收事件。"""

    def __init__(self, bus: "EventBus", run_id: str | None) -> None:
        self._bus = bus
        self.run_id = run_id
        self._queue: "queue.Queue[Event]" = queue.Queue(maxsize=_QUEUE_SIZE)
        self._closed = False
        self.dropped = 0

    def get(self, timeout: float | None = 30.0) -> Event | None:
        """阻塞取一条事件；超时返回 None（SSE 用于发送心跳）。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[Event]:
        items: list[Event] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

    def offer(self, event: Event) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self._closed = True
        self._bus.unsubscribe(self)

    @property
    def closed(self) -> bool:
        return self._closed


class EventBus:
    """进程内事件总线。"""

    def __init__(self, history_limit: int = _MAX_HISTORY) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[Subscription]] = defaultdict(list)
        self._global_subs: list[Subscription] = []
        self._history: dict[str, list[Event]] = defaultdict(list)
        self._seq: dict[str, int] = defaultdict(int)
        self._history_limit = history_limit
        self._counters: dict[str, int] = defaultdict(int)
        self._started_at = time.time()
        self._observed: list[Event] = []  # 最近事件（全局概览面板）

    # -- 发布 ------------------------------------------------------------
    def publish(self, event: Event) -> Event:
        with self._lock:
            seq = self._seq[event.run_id] + 1
            event.seq = seq
            self._seq[event.run_id] = seq
            self._counters[event.type] += 1
            hist = self._history[event.run_id]
            hist.append(event)
            if len(hist) > self._history_limit:
                del hist[: len(hist) - self._history_limit]
            self._observed.append(event)
            if len(self._observed) > 300:
                del self._observed[: len(self._observed) - 300]
            subs = list(self._subs.get(event.run_id, [])) + list(self._global_subs)
        for sub in subs:
            sub.offer(event)
        return event

    def emit(
        self,
        type_: str,
        run_id: str = "",
        node_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> Event:
        return self.publish(
            Event(
                type=type_,
                run_id=run_id,
                node_id=node_id,
                agent_id=agent_id,
                payload=payload or {},
                level=level,
            )
        )

    # -- 订阅 ------------------------------------------------------------
    def subscribe(self, run_id: str | None = None) -> Subscription:
        sub = Subscription(self, run_id)
        with self._lock:
            if run_id is None:
                self._global_subs.append(sub)
            else:
                self._subs[run_id].append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub.run_id is None:
                if sub in self._global_subs:
                    self._global_subs.remove(sub)
            elif sub in self._subs.get(sub.run_id, []):
                self._subs[sub.run_id].remove(sub)

    # -- 查询 ------------------------------------------------------------
    def history(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._history.get(run_id, []) if e.seq > after_seq]

    def last_seq(self, run_id: str) -> int:
        with self._lock:
            return self._seq.get(run_id, 0)

    def recent(self, limit: int = 50) -> list[Event]:
        with self._lock:
            return list(self._observed[-limit:])

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runs_tracked": len(self._seq),
                "events": sum(self._counters.values()),
                "subscribers": len(self._global_subs) + sum(len(v) for v in self._subs.values()),
                "by_type": dict(self._counters),
                "uptime_s": round(time.time() - self._started_at, 1),
            }

    def clear_run(self, run_id: str) -> None:
        with self._lock:
            self._history.pop(run_id, None)
            self._seq.pop(run_id, None)
            for sub in self._subs.pop(run_id, []):
                sub.close()
