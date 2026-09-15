"""并发内核契约测试：上限、保序、失败隔离、批次屏障、一键回串行。

全部离线、确定性、不依赖网络与 FFmpeg。
"""
from __future__ import annotations

import threading
import time

import pytest

from backlot import interaction_concurrency as ic


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (ic.FORCE_SERIAL_ENV, ic.ASR_CONCURRENCY_ENV, ic.INTERACTION_CONCURRENCY_ENV):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_conservative():
    assert ic.DEFAULT_ASR_CONCURRENCY == 3
    assert ic.DEFAULT_INTERACTION_CONCURRENCY == 1
    assert ic.MAX_CONCURRENCY == 4
    assert ic.resolve_limit("asr") == 3
    assert ic.resolve_limit("interaction") == 1
    assert ic.resolve_limit("vision") == 1


def test_explicit_request_and_clamping():
    assert ic.resolve_limit("interaction", 3) == 3
    # 超过硬上限被压回 4，低于 1 报中文错误。
    assert ic.resolve_limit("interaction", 9) == ic.MAX_CONCURRENCY
    with pytest.raises(ic.InteractionConcurrencyError, match="不能小于 1"):
        ic.resolve_limit("interaction", 0)
    with pytest.raises(ic.InteractionConcurrencyError, match="并发阶段"):
        ic.resolve_limit("planets", 1)


def test_process_env_overrides_the_request(monkeypatch):
    monkeypatch.setenv(ic.INTERACTION_CONCURRENCY_ENV, "2")
    assert ic.resolve_limit("interaction", 4) == 2
    monkeypatch.setenv(ic.ASR_CONCURRENCY_ENV, "4")
    assert ic.resolve_limit("asr") == 4


def test_force_serial_kill_switch_wins(monkeypatch):
    monkeypatch.setenv(ic.INTERACTION_CONCURRENCY_ENV, "4")
    assert ic.serial_kill_switch_active() is False
    monkeypatch.setenv(ic.FORCE_SERIAL_ENV, "1")
    assert ic.serial_kill_switch_active() is True
    assert ic.resolve_limit("interaction", 4) == 1
    assert ic.resolve_limit("asr") == 1


def test_run_bounded_is_ordered_even_when_completion_is_reversed():
    started: list[int] = []
    lock = threading.Lock()

    def worker(item, index):
        with lock:
            started.append(index)
        # Earlier items sleep longer, so completion order is the reverse of input.
        time.sleep(0.02 * (4 - index))
        return item * 10

    stats = ic.ConcurrencyStats()
    results = ic.run_bounded([0, 1, 2, 3], worker, limit=4, stats=stats)
    assert [row.value for row in results] == [0, 10, 20, 30]
    assert [row.index for row in results] == [0, 1, 2, 3]
    # Every unit must be dispatched exactly once even though completion order is
    # reversed.  (The previous form of this line ended in `or True` and therefore
    # asserted nothing at all — a silent no-op.)
    assert sorted(started) == [0, 1, 2, 3]
    assert stats.dispatched == 4
    assert stats.completed == 4
    assert stats.in_flight_peak <= 4


def test_run_bounded_never_exceeds_the_in_flight_cap():
    live = 0
    peak = 0
    lock = threading.Lock()

    def worker(item, index):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.01)
        with lock:
            live -= 1
        return item

    stats = ic.ConcurrencyStats()
    ic.run_bounded(list(range(20)), worker, limit=3, stats=stats)
    assert peak <= 3
    assert stats.in_flight_peak <= 3
    assert stats.dispatched == 20


def test_run_bounded_isolates_failures_without_losing_the_rest():
    def worker(item, index):
        if item == 2:
            raise RuntimeError("boom-2")
        return item

    outcomes = ic.run_bounded([0, 1, 2, 3], worker, limit=2)
    assert [row.ok for row in outcomes] == [True, True, False, True]
    assert isinstance(outcomes[2].error, RuntimeError)
    assert [row.value for row in outcomes if row.ok] == [0, 1, 3]


def test_run_bounded_limit_one_is_serial_and_inline():
    order: list[int] = []

    def worker(item, index):
        order.append(index)
        return item

    outcomes = ic.run_bounded([5, 6, 7], worker, limit=1)
    assert order == [0, 1, 2]
    assert [row.value for row in outcomes] == [5, 6, 7]
    assert ic.run_bounded([], worker, limit=3) == []


def test_run_batches_only_exposes_strictly_earlier_batches():
    seen_context: dict[int, list[int]] = {}

    def worker(item, index, context):
        # Each unit records what it could see from earlier batches only.
        seen_context[index] = list(context) if context is not None else []
        return index

    def on_batch(context, completed):
        merged = list(context) if context is not None else []
        for index, outcome in completed:
            merged.append(outcome.value)
        return merged

    outcomes, final = ic.run_batches(
        list(range(7)), worker, batch_size=3, initial_context=[], on_batch=on_batch,
    )
    # batch 0 = [0,1,2] sees nothing; batch 1 = [3,4,5] sees [0,1,2]; batch 2 = [6] sees [0..5].
    assert seen_context[0] == seen_context[1] == seen_context[2] == []
    assert seen_context[3] == seen_context[4] == seen_context[5] == [0, 1, 2]
    assert seen_context[6] == [0, 1, 2, 3, 4, 5]
    assert final == list(range(7))
    assert [row.index for row in outcomes] == list(range(7))


def test_run_batches_keeps_input_order_and_cap():
    stats = ic.ConcurrencyStats()

    def worker(item, index, context):
        time.sleep(0.005 * (index % 3))
        return item + 100

    outcomes, _ = ic.run_batches(list(range(10)), worker, batch_size=4, stats=stats)
    assert [row.value for row in outcomes] == [value + 100 for value in range(10)]
    assert stats.in_flight_peak <= 4


def test_stats_record_in_flight_peak_under_contention():
    stats = ic.ConcurrencyStats()

    def body():
        stats.record_start()
        time.sleep(0.01)
        stats.record_end()

    threads = [threading.Thread(target=body) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert stats.completed == 5
    assert stats.in_flight_peak >= 1
    assert stats.as_dict()["completed"] == 5


def test_bounded_executor_caps_in_flight_and_returns_in_order():
    live = 0
    peak = 0
    lock = threading.Lock()

    def work(value, tag):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.01 * (4 - tag))
        with lock:
            live -= 1
        return value

    stats = ic.ConcurrencyStats()
    with ic.bounded_executor(2, stats=stats) as submit:
        futures = [submit(work, tag, tag) for tag in range(4)]
    assert [future.result() for future in futures] == [0, 1, 2, 3]
    assert peak <= 2
    assert stats.in_flight_peak <= 2
    assert stats.dispatched == 4
    assert stats.completed == 4


def test_bounded_executor_propagates_worker_failure():
    def work():
        raise RuntimeError("局部抽帧失败")

    with ic.bounded_executor(2) as submit:
        future = submit(work)
    with pytest.raises(RuntimeError, match="局部抽帧失败"):
        future.result()

