"""验证 TTS 合成服务的启动竞态已修复。

背景：``TextToSpeech.__init__`` 会起后台线程预热，而用户可能在预热完成前
就发了消息。两个线程都能看到「端口没开 + self._server is None」，
于是各启动一个服务进程 —— 两个进程各加载约 2.4GB 模型，显存翻倍直接 OOM。

本测试把 ``_spawn_server`` 换成计数器（不真的起进程），并发调用
``_ensure_server``，断言只会 spawn 一次。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import tts as tts_mod  # noqa: E402


class FakeProc:
    pid = 12345
    returncode = None

    def poll(self):
        return None


def run_once(n_threads: int = 16, delay: float = 0.02) -> int:
    """并发调用 _ensure_server，返回实际 spawn 次数。"""
    obj = tts_mod.TextToSpeech.__new__(tts_mod.TextToSpeech)
    obj.engine = "sovits"
    obj._server = None
    obj._server_log = None
    obj._server_lock = threading.Lock()

    counter = {"n": 0}
    barrier = threading.Barrier(n_threads)

    def fake_spawn(self):
        # 模拟真实 _spawn_server 的耗时（开日志文件 / 建 env / Popen）
        time.sleep(delay)
        counter["n"] += 1
        self._server = FakeProc()

    obj._spawn_server = fake_spawn.__get__(obj, type(obj))

    errors: list[str] = []

    def worker():
        try:
            barrier.wait()
            obj._ensure_server(wait=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        print(f"    线程异常: {errors[:3]}")
    return counter["n"]


def main() -> int:
    print("=" * 68)
    print("TTS 合成服务启动竞态测试")
    print("=" * 68)

    rounds = 8
    bad = 0
    for i in range(rounds):
        n = run_once()
        flag = "OK  " if n == 1 else "FAIL"
        if n != 1:
            bad += 1
        print(f"  第 {i+1} 轮：16 线程并发 → spawn {n} 次   {flag}")

    print()
    if bad == 0:
        print(f"  全部 {rounds} 轮都只 spawn 一次 [PASS] 竞态已修复")
        return 0
    print(f"  {bad}/{rounds} 轮出现重复 spawn [FAIL]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
