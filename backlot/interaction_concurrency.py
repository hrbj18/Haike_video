"""有界并发内核：粗剪提速的唯一并发入口。

设计约束（见 `docs/team-runs/2026-09-12-cut-v2/02-ARCHITECTURE.md` 第 1 节）：

* 只用标准库 `concurrent.futures.ThreadPoolExecutor` + `threading.BoundedSemaphore`。
  调用图全同步（requests / subprocess / 云端 HTTP 都是阻塞调用），并发收益来自
  「等 I/O」而不是「算 CPU」，因此线程池即可，GIL 在阻塞期被释放。
* **绝不允许裸 `Thread()` fan-out**：所有并发都必须经过 :func:`run_bounded` 或
  :func:`run_batches`，由它们统一施加「线程数」与「全局在飞上限」两道闸门。
* 默认保守：视觉窗口并发默认 1（等于现状），ASR 分片默认 3。
* 一键回串行：环境变量 ``HAIKE_FORCE_SERIAL=1`` 让 :func:`resolve_limit` 一律返回 1，
  调用方据此把窗口策略强制回 ``serial_equivalent``。

本模块不含任何付费逻辑，并发旋钮**绝不**进入任何付费签名（`request_signature`）。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence


# 默认与上限：与架构文档 §7「并发上限的配置键名」逐字一致，严禁改名。
DEFAULT_ASR_CONCURRENCY = 3
DEFAULT_INTERACTION_CONCURRENCY = 1
MAX_CONCURRENCY = 4
MIN_CONCURRENCY = 1

# 进程级覆盖键名。
FORCE_SERIAL_ENV = "HAIKE_FORCE_SERIAL"
ASR_CONCURRENCY_ENV = "HAIKE_ASR_CONCURRENCY"
INTERACTION_CONCURRENCY_ENV = "HAIKE_INTERACTION_CONCURRENCY"

# 语义类型：asr 指腾讯 ASR 分片；interaction/vision 指视觉互动窗口（两者等价）。
KIND_ASR = "asr"
KIND_INTERACTION = "interaction"
KIND_VISION = "vision"
_KINDS = frozenset({KIND_ASR, KIND_INTERACTION, KIND_VISION})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class InteractionConcurrencyError(ValueError):
    """并发配置非法。所有文案中文且给出可执行补救。"""


@dataclass
class BoundedResult:
    """一个并发单元的结果；失败不外抛，就地隔离。"""

    index: int
    value: Any = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ConcurrencyStats:
    """计量：提交数、完成数、重试数与在飞峰值。

    ``in_flight_peak`` 是验收「在飞 ≤ 上限」的唯一依据，由信号量持有期间计数。
    """

    dispatched: int = 0
    completed: int = 0
    retries: int = 0
    in_flight_peak: int = 0
    _in_flight: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_dispatch(self, count: int = 1) -> None:
        with self._lock:
            self.dispatched += int(count)

    def record_start(self) -> None:
        with self._lock:
            self._in_flight += 1
            if self._in_flight > self.in_flight_peak:
                self.in_flight_peak = self._in_flight

    def record_end(self) -> None:
        with self._lock:
            self.completed += 1
            if self._in_flight > 0:
                self._in_flight -= 1

    def record_retry(self, count: int = 1) -> None:
        with self._lock:
            self.retries += int(count)

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "dispatched": self.dispatched,
                "completed": self.completed,
                "retries": self.retries,
                "in_flight_peak": self.in_flight_peak,
            }


_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}


def _shared_semaphore(limit: int) -> threading.BoundedSemaphore:
    """Return the process-wide in-flight cap for one limit value.

    The governor is shared across every call point so ASR and visual windows
    cannot each open their own ``limit`` and double the machine's real peak.
    """
    with _SEMAPHORE_LOCK:
        semaphore = _SEMAPHORES.get(limit)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit)
            _SEMAPHORES[limit] = semaphore
        return semaphore


def serial_kill_switch_active() -> bool:
    """True when the user asked for a global one-click return to serial."""
    return str(os.environ.get(FORCE_SERIAL_ENV, "")).strip().lower() in _TRUE_VALUES


def _normalise_limit(limit: Any) -> int:
    if limit is None:
        return MIN_CONCURRENCY
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise InteractionConcurrencyError(
            f"并发上限必须是 1 到 {MAX_CONCURRENCY} 之间的整数，请调整后重试。"
        ) from exc
    if value < MIN_CONCURRENCY:
        raise InteractionConcurrencyError("并发上限不能小于 1，请调整后重试。")
    return min(value, MAX_CONCURRENCY)


def _default_for(kind: str) -> int:
    return DEFAULT_ASR_CONCURRENCY if kind == KIND_ASR else DEFAULT_INTERACTION_CONCURRENCY


def _env_name_for(kind: str) -> str:
    return ASR_CONCURRENCY_ENV if kind == KIND_ASR else INTERACTION_CONCURRENCY_ENV


def resolve_limit(kind: str, requested: Any = None) -> int:
    """Resolve the effective concurrency for one stage.

    Precedence: 一键回串行 > 进程级环境变量 > 入口参数 > 默认值。
    结果永远落在 ``[1, MAX_CONCURRENCY]``。
    """
    if kind not in _KINDS:
        raise InteractionConcurrencyError(
            f"未知的并发阶段「{kind}」，请使用 asr 或 interaction。"
        )
    if serial_kill_switch_active():
        return MIN_CONCURRENCY
    value = requested if requested is not None else _default_for(kind)
    override = os.environ.get(_env_name_for(kind))
    if override is not None and str(override).strip() != "":
        value = override
    return _normalise_limit(value)


def run_bounded(
    items: Sequence[Any],
    worker: Callable[[Any, int], Any],
    *,
    limit: Any = None,
    stats: ConcurrencyStats | None = None,
) -> list[BoundedResult]:
    """Run ``worker(item, index)`` over ``items`` with a bounded in-flight cap.

    * 返回列表与输入**同序**（乱序完成也按 index 归位）。
    * ``worker`` 抛出的异常就地捕获进 :class:`BoundedResult`，不影响其他单元。
    * ``limit == 1`` 走内联串行路径，逐字节等价于现状，也避免线程开销。
    """
    materialized = list(items)
    stats = stats if stats is not None else ConcurrencyStats()
    effective = _normalise_limit(limit)
    stats.record_dispatch(len(materialized))
    if not materialized:
        return []

    def task(index: int, item: Any) -> BoundedResult:
        with _shared_semaphore(effective):
            stats.record_start()
            try:
                value = worker(item, index)
            except BaseException as exc:  # noqa: BLE001 - 失败必须隔离，逐个上报
                return BoundedResult(index=index, error=exc)
            finally:
                stats.record_end()
            return BoundedResult(index=index, value=value)

    results: list[BoundedResult | None] = [None] * len(materialized)
    if effective == 1 or len(materialized) == 1:
        for index, item in enumerate(materialized):
            results[index] = task(index, item)
        return [row for row in results if row is not None]

    with ThreadPoolExecutor(max_workers=effective) as executor:
        futures = {
            executor.submit(task, index, item): index
            for index, item in enumerate(materialized)
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
    return [row for row in results if row is not None]


def run_batches(
    items: Sequence[Any],
    worker: Callable[[Any, int, Any], Any],
    *,
    batch_size: Any = None,
    stats: ConcurrencyStats | None = None,
    initial_context: Any = None,
    on_batch: Callable[[Any, list[tuple[int, BoundedResult]]], Any] | None = None,
) -> tuple[list[BoundedResult], Any]:
    """批次屏障模型：批内并发、批间严格串行。

    ``worker(item, index, batch_context)`` 拿到的是**严格更早批次**已归并的上下文，
    批内单元互不可见 —— 这是让并发结果**可复现**的关键取舍（不做完全异步）。
    ``on_batch(context, completed)`` 在每批完成后按批内顺序推进上下文。
    """
    materialized = list(items)
    stats = stats if stats is not None else ConcurrencyStats()
    size = _normalise_limit(batch_size)
    results: list[BoundedResult | None] = [None] * len(materialized)
    context = initial_context
    for start in range(0, len(materialized), size):
        indices = list(range(start, min(start + size, len(materialized))))
        frozen_context = context

        def dispatch(item: Any, local_index: int, _ctx: Any = frozen_context,
                     _indices: list[int] = indices) -> Any:
            return worker(item, _indices[local_index], _ctx)

        outcomes = run_bounded(
            [materialized[index] for index in indices], dispatch, limit=size, stats=stats,
        )
        completed: list[tuple[int, BoundedResult]] = []
        for local_index, outcome in enumerate(outcomes):
            absolute_index = indices[local_index]
            outcome.index = absolute_index
            results[absolute_index] = outcome
            completed.append((absolute_index, outcome))
        if on_batch is not None:
            context = on_batch(context, completed)
    return [row for row in results if row is not None], context


@contextmanager
def bounded_executor(limit: Any = None, *, stats: ConcurrencyStats | None = None) -> Iterator[Callable[..., Any]]:
    """有界滚动预取执行器（路线甲「本地预取」用）。

    与 :func:`run_bounded` **复用同一个** :func:`_shared_semaphore`，因此两条并发路径共享
    同一个全局在飞上限，不会「各开一半」而让机器峰值翻倍（见 A3）。

    ``submit(worker, *args)`` 返回标准 ``Future``；调用方按窗口序 ``.result()`` 即可，
    乱序完成也不影响结果。退出 ``with`` 时等待在飞任务收尾。
    """
    effective = _normalise_limit(limit)
    stats = stats if stats is not None else ConcurrencyStats()
    semaphore = _shared_semaphore(effective)
    executor = ThreadPoolExecutor(max_workers=effective)

    def submit(worker: Callable[..., Any], *args: Any) -> Any:
        stats.record_dispatch()

        def guarded() -> Any:
            with semaphore:
                stats.record_start()
                try:
                    return worker(*args)
                finally:
                    stats.record_end()

        return executor.submit(guarded)

    try:
        yield submit
    finally:
        executor.shutdown(wait=True)


__all__ = [
    "ASR_CONCURRENCY_ENV",
    "BoundedResult",
    "ConcurrencyStats",
    "DEFAULT_ASR_CONCURRENCY",
    "DEFAULT_INTERACTION_CONCURRENCY",
    "FORCE_SERIAL_ENV",
    "INTERACTION_CONCURRENCY_ENV",
    "InteractionConcurrencyError",
    "MAX_CONCURRENCY",
    "MIN_CONCURRENCY",
    "bounded_executor",
    "resolve_limit",
    "run_batches",
    "run_bounded",
    "serial_kill_switch_active",
]
