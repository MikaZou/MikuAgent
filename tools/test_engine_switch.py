"""验证「运行中切换 TTS/STT 引擎」是真的生效，而不只是改了 .env。

重点验证三件事：
  1. 云端 → 本地：GPT-SoVITS 进程真的拉起来了、显存真的涨了、能合成
  2. 本地 → 云端：进程真的死了、端口真的释放了、显存真的还回去了
  3. STT 同理：Whisper 加载/卸载，内存真的增减

只改 .env 不做 reconfigure 的话，第 2 条会失败（进程和显存一直挂着），
这正是这个测试要卡住的地方。

用法：python tools/test_engine_switch.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

import config  # noqa: E402
import stt as S  # noqa: E402
import tts as T  # noqa: E402


def vram_used() -> int:
    """当前显存占用（MiB）。取不到返回 -1。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:  # noqa: BLE001
        return -1


def rss_mb() -> int:
    """本进程常驻内存（MB）。"""
    try:
        import psutil  # type: ignore

        return int(psutil.Process().memory_info().rss / 1024 / 1024)
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
        )
        return int(pmc.WorkingSetSize / 1024 / 1024)
    except Exception:  # noqa: BLE001
        return -1


def line(tag: str, extra: str = "") -> None:
    print(f"  [{tag:22s}] 显存 {vram_used():5d} MiB | 本进程内存 {rss_mb():5d} MB {extra}")


def wait_port(tts: T.TextToSpeech, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tts._port_open():
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    failures: list[str] = []

    print("=" * 74)
    print("阶段 0：以 .env 当前配置启动（应为云端 minimax）")
    print("=" * 74)
    tt = T.TextToSpeech()
    ss = S.SpeechToText()
    print(f"  TTS engine = {tt.engine} | STT transcriber = {ss.transcriber}")
    line("基线")

    base_vram = vram_used()

    # ---------------------------------------------------------------- 云端→本地
    print()
    print("=" * 74)
    print("阶段 1：云端 → 本地（拉起 GPT-SoVITS）")
    print("=" * 74)
    config.TTS_ENGINE = "sovits"
    t0 = time.time()
    tt.reconfigure()
    print(f"  reconfigure 返回耗时 {time.time() - t0:.2f}s（不该阻塞界面）")
    print(f"  status = {tt.status}（应为 loading）")
    if tt.status != "loading":
        failures.append(f"切到 sovits 后 status 应为 loading，实际 {tt.status}")

    ok = wait_port(tt, 120)
    print(f"  服务端口就绪: {ok}（耗时 {time.time() - t0:.1f}s）")
    if not ok:
        failures.append("GPT-SoVITS 服务 120s 内没起来")
    else:
        line("加载 sovits 后")
        after_vram = vram_used()
        if base_vram >= 0 and after_vram - base_vram < 400:
            failures.append(
                f"加载 sovits 后显存只涨了 {after_vram - base_vram} MiB，疑似没真正加载"
            )
        # 真的能合成吗
        r = tt.synthesize("切换测试，能听到吗？", "NORMAL")
        print(f"  合成结果: {r[0].name if r else None} 时长 {r[1]:.2f}s" if r else "  合成失败！")
        if not r:
            failures.append("切到 sovits 后合成失败")

    # ---------------------------------------------------------------- 本地→云端
    print()
    print("=" * 74)
    print("阶段 2：本地 → 云端（必须停掉进程并释放显存）")
    print("=" * 74)
    proc = tt._server
    pid = proc.pid if proc else None
    print(f"  切换前 sovits 子进程 pid = {pid}")
    config.TTS_ENGINE = "minimax"
    t0 = time.time()
    tt.reconfigure()
    dt = time.time() - t0
    print(f"  reconfigure 耗时 {dt:.2f}s（内含等待子进程退出的时间）")

    time.sleep(1.0)
    if pid is not None:
        poll = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        ).stdout
        alive = str(pid) in poll
        print(f"  子进程 {pid} 是否仍存活: {alive}（应为 False）")
        if alive:
            failures.append(f"切走后 sovits 子进程 {pid} 仍在运行，显存没释放")
    print(f"  端口是否仍被占用: {tt._port_open()}（应为 False）")
    if tt._port_open():
        failures.append("切走后合成服务端口仍被占用")

    line("释放后")
    back_vram = vram_used()
    if base_vram >= 0 and back_vram - base_vram > 600:
        failures.append(f"切回云端后显存仍比基线高 {back_vram - base_vram} MiB，没有真正释放")
    else:
        print(f"  显存相对基线差 {back_vram - base_vram:+d} MiB（越接近 0 越好）")

    r = tt.synthesize("云端恢复测试。", "NORMAL")
    print(f"  云端合成: {'OK ' + str(r[1]) + 's' if r else '失败！'}")
    if not r:
        failures.append("切回 minimax 后合成失败")

    # ---------------------------------------------------------------- STT 切换
    print()
    print("=" * 74)
    print("阶段 3：STT 云端 → 本地 Whisper → 云端")
    print("=" * 74)
    config.STT_TRANSCRIBER = "local-whisper"
    t0 = time.time()
    ss.reconfigure()
    print(f"  status = {ss.status}（应为 loading）")
    while ss.status == "loading" and time.time() - t0 < 180:
        time.sleep(1.0)
    print(f"  Whisper 加载完成 status = {ss.status}（耗时 {time.time() - t0:.1f}s）")
    if ss.status != "ready":
        failures.append(f"Whisper 加载后 status={ss.status}，预期 ready")
    line("加载 Whisper 后")
    loaded_rss = rss_mb()

    config.STT_TRANSCRIBER = "minimax"
    ss.reconfigure()
    time.sleep(1.0)
    line("卸载 Whisper 后")
    freed_rss = rss_mb()
    print(f"  内存变化 {freed_rss - loaded_rss:+d} MB（切回云端应释放约 1GB，"
          f"但 Python 未必立刻归还 OS，关键看 _model is None: {ss._model is None}）")
    if ss._model is not None:
        failures.append("切回 minimax 后 Whisper 模型没被卸载")
    if ss.status != "ready":
        failures.append(f"切回 minimax 后 status={ss.status}，预期 ready")

    # ------------------------------------------------- 竞态：预热还没完就退出
    print()
    print("=" * 74)
    print("阶段 4：切到本地后**立刻**退出（预热线程还没 spawn 完）")
    print("=" * 74)
    print("  回归测试：预热线程是异步的，用户可能在它 spawn 之前就切走/退出。")
    print("  没有旗标的话它会在这之后才拉起进程，留下占 2.2GB 显存的野进程。")
    config.TTS_ENGINE = "sovits"
    tt.reconfigure()
    time.sleep(0.3)          # 预热线程刚起来，还没走到 spawn
    tt.shutdown()            # 立刻退出
    print("  已调用 shutdown()，现在盯 45 秒看有没有野进程把端口占起来...")
    leaked = False
    for i in range(45):
        if tt._port_open():
            print(f"  [X] 第 {i + 1}s 端口被占用了 —— 野进程冒出来了！")
            leaked = True
            break
        time.sleep(1.0)
    if leaked:
        failures.append("shutdown 之后预热线程仍拉起了合成服务（野进程，显存泄漏）")
    else:
        print("  [OK] 45s 内端口始终没被占用，没有野进程")
    if tt._server is not None:
        failures.append(f"shutdown 后 _server 不为 None：{tt._server}")
    else:
        print("  [OK] _server 已置空")

    # 收尾
    tt.shutdown()

    print()
    print("=" * 74)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(f"  [X] {f}")
        return 1
    print("全部通过：切换真实生效，且旧引擎资源被释放 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
