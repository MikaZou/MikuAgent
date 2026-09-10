"""带断点续传的下载器（curl 在本机 TLS 不可用，pip 不会续传大文件）。

用法:
    python tools/fetch_wheel.py <url> <目标文件>
中断后重新执行同一条命令即可从断点继续。
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

CHUNK = 1024 * 1024  # 1MB


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if have:
        req.add_header("Range", f"bytes={have}-")

    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) + have if total else None

        if have and resp.status != 206:
            print("[warn] 服务器不支持续传，重新开始下载")
            have = 0

        mode = "ab" if have else "wb"
        started = time.time()
        done = have
        last_print = 0.0
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_print >= 2.0:
                    last_print = now
                    speed = (done - have) / max(now - started, 1e-6)
                    if total:
                        pct = done / total * 100
                        eta = (total - done) / max(speed, 1e-6)
                        print(f"  {pct:5.1f}%  {human(done)}/{human(total)}  "
                              f"{human(speed)}/s  ETA {eta/60:.1f}min", flush=True)
                    else:
                        print(f"  {human(done)}  {human(speed)}/s", flush=True)

    size = dest.stat().st_size
    if total and size < total:
        print(f"[incomplete] {human(size)} / {human(total)} —— 重新运行本命令可续传")
        return 2
    print(f"[done] {dest}  {human(size)}")
    return 0


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    return download(sys.argv[1], Path(sys.argv[2]))


if __name__ == "__main__":
    raise SystemExit(main())
