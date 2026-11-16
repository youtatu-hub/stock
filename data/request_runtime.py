"""数据请求的截止时间、有限并发、请求合并与阶段计时。"""
from __future__ import annotations

import concurrent.futures as futures
import contextvars
import functools
import inspect
import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("trend_data")
DATA_BUDGET_SECONDS = 8.0
OPTIONAL_BUDGET_SECONDS = 3.0
HTTP_ATTEMPT_SECONDS = 8.0

request_id = contextvars.ContextVar("request_id", default="background")
_deadline = contextvars.ContextVar("data_deadline", default=None)
_executor = futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="market-data")
_slots = threading.BoundedSemaphore(64)
_background_slots = threading.BoundedSemaphore(128)
_inflight = {}
_inflight_lock = threading.Lock()


@contextmanager
def deadline_scope(deadline: float):
    parent = _deadline.get()
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_seconds(limit: float = DATA_BUDGET_SECONDS) -> float:
    deadline = _deadline.get()
    remaining = min(limit, deadline - time.monotonic()) if deadline is not None else limit
    if remaining <= 0:
        raise TimeoutError("数据获取超过时间预算")
    return remaining


def data_stage(function):
    """相同参数的并发读取共享一次抓取；每个调用都输出可关联的耗时。"""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        key = (function.__name__, tuple(bound.arguments.items()))
        started = time.monotonic()
        status = "ok"
        with _inflight_lock:
            future = _inflight.get(key)
            owner = future is None
            if owner:
                future = futures.Future()
                _inflight[key] = future
        try:
            with deadline_scope(started + DATA_BUDGET_SECONDS):
                if not owner:
                    status = "shared"
                    return future.result(timeout=remaining_seconds())
                result = function(*args, **kwargs)
                status = "ok" if result else "empty"
                future.set_result(result)
                return result
        except Exception as exc:
            status = "error"
            if owner:
                future.set_exception(exc)
            raise
        finally:
            if owner:
                with _inflight_lock:
                    del _inflight[key]
            log.info("data_stage request_id=%s stage=%s status=%s elapsed_ms=%.1f",
                     request_id.get(), function.__name__, status,
                     (time.monotonic() - started) * 1000)
    return wrapped


def _run_stage(name, callback, deadline, submitted):
    with deadline_scope(deadline):
        remaining_seconds()
        try:
            value = callback()
            error = None if value else "数据源返回空结果"
        except (OSError, ValueError) as exc:
            # 网络/数据格式错误可降级；程序错误继续向上传播。
            log.warning("data_failure request_id=%s stage=%s error=%s", request_id.get(), name, exc)
            value, error = None, str(exc)
    return value, error, round((time.monotonic() - submitted) * 1000, 1)


def collect_stages(jobs: dict, required=(), *, budget=DATA_BUDGET_SECONDS,
                   optional_budget=OPTIONAL_BUDGET_SECONDS, executor=None) -> tuple:
    """并行获取数据。超时不等待线程退出，排队时间也计入预算。"""
    started = time.monotonic()
    deadline = started + remaining_seconds(budget)
    optional_deadline = min(deadline, started + optional_budget)
    pool = executor or _executor
    slots = _background_slots if executor is not None else _slots
    submitted = {}
    values, errors, timings = {}, {}, {}
    for name, callback in jobs.items():
        if not slots.acquire(blocking=False):
            errors[name] = "数据请求繁忙，请稍后重试"
            timings[name] = 0.0
            continue
        stage_deadline = deadline if name in required else optional_deadline
        context = contextvars.copy_context()
        try:
            future = pool.submit(context.run, _run_stage, name, callback, stage_deadline, started)
        except Exception:
            slots.release()
            raise
        future.add_done_callback(lambda _: slots.release())
        submitted[name] = future

    # 先收集非关键阶段，再等待仍在运行的必需阶段，共享同一个截止时间。
    pending = set(submitted.values())
    if pending:
        _, pending = futures.wait(pending, timeout=max(0, optional_deadline - time.monotonic()))
    optional_pending = {name for name, future in submitted.items()
                        if name not in required and future in pending}
    for name in optional_pending:
        submitted[name].cancel()
    required_pending = {future for name, future in submitted.items()
                        if name in required and future in pending}
    if required_pending:
        futures.wait(required_pending, timeout=max(0, deadline - time.monotonic()))

    for name, future in submitted.items():
        if name in optional_pending or not future.done():
            future.cancel()
            errors[name] = "数据获取超过时间预算"
            timings[name] = round(((optional_deadline if name in optional_pending else deadline) - started) * 1000, 1)
        else:
            try:
                value, error, timings[name] = future.result()
            except TimeoutError as exc:
                value, error = None, str(exc)
                timings[name] = round((time.monotonic() - started) * 1000, 1)
            if error:
                errors[name] = error
            else:
                values[name] = value
        log.info("fetch_stage request_id=%s stage=%s elapsed_ms=%.1f status=%s",
                 request_id.get(), name, timings[name], errors.get(name, "ok"))
    timings["fetch_total"] = round((time.monotonic() - started) * 1000, 1)
    return values, errors, timings


def fetch_required(name, callback):
    values, errors, _ = collect_stages({name: callback}, required=(name,))
    if name in errors:
        raise OSError(f"{name}: {errors[name]}")
    return values[name]
