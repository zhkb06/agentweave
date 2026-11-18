# -*- coding: utf-8 -*-
"""录制真实界面素材（静音、1920x1080、可直接进剪映配音剪辑）。

原理：Edge 以 --remote-debugging-port 启动成 2560x1440 的应用窗口，
用 CDP 驱动真实鼠标事件（页面真实渲染），同时用 ffmpeg gdigrab 抓屏，
抓到的 2560x1440 画面缩放为 1920x1080 输出。

产物：docs/视频素材/
    01-仪表盘.mp4
    02-编排台与节点详情.mp4
    03-运行监控与实时事件.mp4
    04-五层验证与自循环.mp4
    05-资源中心.mp4

用法：py -3.14 scripts/record_demo.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "视频素材"
BASE = "http://127.0.0.1:8765"
DEBUG_PORT = 9222
PROFILE = ROOT / "_recprofile"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CAP_W, CAP_H = 2560, 1440      # 抓屏区域（= Edge 应用窗口物理尺寸）
OUT_W, OUT_H = 1920, 1080


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ------------------------------------------------------------------ CDP

class CDP:
    def __init__(self, port: int = DEBUG_PORT):
        self.port = port
        self.ws = None
        self.id = 0
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                page = next(t for t in targets if t.get("type") == "page"
                            and str(t.get("url", "")).startswith(BASE))
                self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20)
                self.send("Page.enable")
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("连不上 Edge 调试端口")

    def send(self, method: str, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        return r.get("result", {}).get("value")

    def wait_js(self, expr: str, timeout: float = 15.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.js(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def rect(self, sel: str):
        return self.js(
            "(function(){var e=document.querySelector(%s);if(!e)return null;var r=e.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};})()" % json.dumps(sel))

    def move(self, x: float, y: float):
        self.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")

    def glide(self, x: float, y: float, steps: int = 16, delay: float = 0.035):
        cur = self.js("({x: window.__mx||0, y: window.__my||0})") or {"x": 0, "y": 0}
        x0, y0 = cur["x"], cur["y"]
        for i in range(1, steps + 1):
            self.move(x0 + (x - x0) * i / steps, y0 + (y - y0) * i / steps)
            time.sleep(delay)
        self.js(f"window.__mx={x};window.__my={y};")

    def click(self, sel: str, settle: float = 0.8) -> bool:
        r = self.rect(sel)
        if not r:
            return False
        self.glide(r["x"], r["y"])
        time.sleep(0.15)
        for t in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"],
                      button="left", clickCount=1)
        time.sleep(settle)
        return True

    def scroll(self, sel: str | None, delta: int, steps: int = 6, pause: float = 0.12):
        r = self.rect(sel) if sel else {"x": 900, "y": 700}
        if not r:
            return
        self.glide(r["x"], r["y"], steps=8)
        for _ in range(steps):
            self.send("Input.dispatchMouseEvent", type="mouseWheel", x=r["x"], y=r["y"],
                      deltaX=0, deltaY=delta)
            time.sleep(pause)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 录屏

class Recorder:
    def __init__(self, name: str):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        self.path = OUT_DIR / f"{name}.mp4"
        self.proc = None

    def start(self):
        if self.path.exists():
            self.path.unlink()
        cmd = [ffmpeg(), "-y", "-loglevel", "error",
               "-f", "gdigrab", "-framerate", "30",
               "-offset_x", "0", "-offset_y", "22",
               "-video_size", f"{CAP_W}x{CAP_H}", "-i", "desktop",
               "-vf", f"scale={OUT_W}:{OUT_H}:flags=lanczos,fps=30",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
               "-pix_fmt", "yuv420p", str(self.path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def launch_edge() -> subprocess.Popen:
    for p in ROOT.glob("_recprofile*"):
        pass
    cmd = [EDGE, f"--user-data-dir={PROFILE}", "--no-first-run",
           f"--remote-debugging-port={DEBUG_PORT}",
           "--disable-sync", "--no-service-autorun", "--disable-background-networking",
           "--disable-features=msEdgeSyncConfirmationDialog,SyncPromoUI,"
           "msEdgeFirstRunExperience,msEdgeSyncPromo",
           f"--app={BASE}/#/dashboard",
           f"--window-size={CAP_W},{CAP_H + 22}", "--window-position=0,0"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    return proc


# ------------------------------------------------------------------ 分镜

def goto(cdp: CDP, page: str, settle: float = 1.2):
    cdp.click(f'.nav-item[data-page="{page}"]', settle=settle)


def seg_dashboard(cdp: CDP, rec: Recorder):
    rec.start()
    goto(cdp, "dashboard", 1.5)
    r = cdp.rect(".scene-card") or {"x": 700, "y": 780}
    cdp.glide(r["x"], r["y"], steps=20)
    time.sleep(2.0)
    cdp.glide(1200, 700, steps=14)
    time.sleep(1.2)
    cdp.scroll(None, 480, steps=3)
    time.sleep(1.5)
    cdp.scroll(None, -480, steps=2)
    time.sleep(4.0)
    rec.stop()


def seg_flow(cdp: CDP, rec: Recorder):
    goto(cdp, "flow", 1.6)
    cdp.js("(function(){var s=document.getElementById('wf-select');if(!s)return false;"
           "s.value='wf-ai4s-air-quality';s.dispatchEvent(new Event('change',{bubbles:true}));"
           "s.blur();return true;})()")
    time.sleep(1.8)
    rec.start()
    # 依次划过节点
    for sel in ['.dag-node[data-id*="plan"]', '.dag-node[data-id*="scan"]',
                '.dag-node[data-id*="analyze"]']:
        r = cdp.rect(sel)
        if r:
            cdp.glide(r["x"], r["y"], steps=14)
            time.sleep(1.4)
    # 打开「统计建模」节点详情
    cdp.click('.dag-node[data-id*="analyze"]', settle=1.4)
    time.sleep(2.2)
    cdp.scroll("#drawer-body", 240, steps=4)
    time.sleep(1.6)
    cdp.scroll("#drawer-body", 240, steps=3)
    time.sleep(2.0)
    # 切到「验证」节点
    cdp.click('.dag-node[data-id*="verify"]', settle=1.4)
    time.sleep(3.0)
    rec.stop()


def seg_run_verify(cdp: CDP, rec: Recorder):
    goto(cdp, "dashboard", 1.4)
    cdp.click("#btn-demo", settle=1.0)
    rec.start()
    time.sleep(3.0)
    goto(cdp, "runtime", 1.6)
    # 等待运行开始并产生事件
    cdp.wait_js("document.querySelectorAll('#monitor-events .event-row').length > 3", 25)
    r = cdp.rect("#monitor-events") or {"x": 900, "y": 800}
    cdp.glide(r["x"], r["y"], steps=14)
    time.sleep(1.5)
    cdp.scroll("#monitor-events", 260, steps=5)
    time.sleep(2.0)
    cdp.scroll("#monitor-events", 260, steps=4)
    time.sleep(2.5)
    # 调度决策卡片
    r = cdp.rect("#monitor-schedule .decision-card")
    if r:
        cdp.glide(r["x"], r["y"], steps=12)
        time.sleep(2.5)
    # 等运行结束
    cdp.wait_js("document.querySelectorAll('#verify-tbody tr').length > 2", 40)
    time.sleep(1.5)
    cdp.click("#btn-verify-refresh", settle=1.2)
    time.sleep(2.0)
    r = cdp.rect("#verify-tbody") or {"x": 900, "y": 700}
    cdp.glide(r["x"], r["y"], steps=12)
    time.sleep(1.5)
    cdp.scroll("#verify-tbody", 200, steps=5)
    time.sleep(2.0)
    cdp.scroll(None, 400, steps=4)
    time.sleep(2.5)
    cdp.scroll(None, -300, steps=3)
    time.sleep(3.0)
    rec.stop()


def seg_resources(cdp: CDP, rec: Recorder):
    goto(cdp, "resources", 1.6)
    rec.start()
    r = cdp.rect(".agent-card") or {"x": 700, "y": 500}
    cdp.glide(r["x"], r["y"], steps=18)
    time.sleep(2.0)
    cdp.scroll(None, 420, steps=4)
    time.sleep(2.0)
    cdp.scroll(None, -420, steps=3)
    time.sleep(3.0)
    rec.stop()


# ------------------------------------------------------------------ 主流程

SEGMENTS = [
    ("01-仪表盘", seg_dashboard, 14),
    ("02-编排台与节点详情", seg_flow, 30),
    ("03-运行监控与实时事件", seg_run_verify, 60),
    ("04-资源中心", seg_resources, 14),
]


def main() -> int:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as r:
            if r.status != 200:
                raise RuntimeError
    except Exception:
        print("服务未启动：先运行 py -3.14 server.py --port 8765 --no-browser --quiet")
        return 1

    edge = launch_edge()
    cdp = None
    results = []
    try:
        cdp = CDP()
        for name, fn, expect in SEGMENTS:
            print(f"[录制] {name} ...", flush=True)
            t0 = time.time()
            try:
                fn(cdp, Recorder(name))
            except Exception as exc:  # 单段失败不中断
                print(f"  失败：{type(exc).__name__}: {exc}", flush=True)
            dur = time.time() - t0
            path = OUT_DIR / f"{name}.mp4"
            size = path.stat().st_size if path.exists() else 0
            print(f"  {dur:.1f}s（预计 {expect}s）  {size / 1048576:.1f} MB", flush=True)
            if size > 50_000:
                results.append(name)
    finally:
        if cdp:
            cdp.close()
        try:
            edge.terminate()
        except Exception:
            pass

    print("\n完成：", ", ".join(results), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
