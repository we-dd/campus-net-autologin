"""跨运行的失败计数与防锁号状态。

学校认证系统连续失败会锁账号。宁可少试，也不要因为脚本死循环把账号试锁了——
锁号带来的麻烦远大于掉线。
所以失败计数跨运行累计，存 state.json。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)


@dataclass
class State:
    failures: list[float] = field(default_factory=list)
    stopped: bool = False


def load(path: str) -> State:
    if not os.path.exists(path):
        return State()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return State(
            failures=[float(x) for x in data.get("failures", [])],
            stopped=bool(data.get("stopped", False)),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("状态文件损坏，按空状态处理: %s", e)
        return State()


def save(state: State, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f)
    os.replace(tmp, path)


def prune(state: State, now: float, window_hours: int) -> None:
    cutoff = now - window_hours * 3600
    state.failures = [t for t in state.failures if t >= cutoff]
    if state.stopped and not state.failures:
        log.info("失败记录已全部过期，自动解除停止状态")
        state.stopped = False


def record_failure(state: State, now: float, window_hours: int, window_max_fail: int) -> None:
    state.failures.append(now)
    prune(state, now, window_hours)
    if len(state.failures) >= window_max_fail:
        log.error(
            "最近 %d 小时内已有 %d 次失败，停止自动登录，需人工登录一次才能恢复",
            window_hours, len(state.failures),
        )
        state.stopped = True


def is_stopped(state: State, now: float, window_hours: int) -> bool:
    prune(state, now, window_hours)
    return state.stopped


def clear(state: State) -> None:
    state.failures = []
    state.stopped = False
