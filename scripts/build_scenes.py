"""按剪辑方案渲染 8 个场景片段（1920x1080/30fps，含 TTS 旁白音频），供剪映拼装。

用法：
    py -3.14 scripts/build_scenes.py            # 渲染全部
    py -3.14 scripts/build_scenes.py 3 5        # 只渲染第 3、5 段

产物：docs/视频素材/成片片段/scene_01.mp4 ... scene_08.mp4
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "视频素材"
OUT_DIR = ASSETS / "成片片段"
WORK = ROOT / ".scene_build"
W, H, FPS = 1920, 1080, 30
FFMPEG = r"D:\anaconda3\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v9.0.2.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

SCENES = [
    {
        "key": "01",
        "label": "钩子",
        "narration": "多智能体不是把几个 Agent 串起来。真正的难题是——七个 Agent 交出一份报告，你凭什么相信它是对的？我们做了一个平台，专门回答这个问题。",
        "segs": [
            {"src": "卡片/钩子A-七个Agent.png", "dur": 6.0},
            {"src": "卡片/钩子B-怎么证明算对了.png", "dur": 5.0},
            {"src": "幻灯片/slide-01.png", "dur": None},
        ],
    },
    {
        "key": "02",
        "label": "痛点与方案",
        "narration": "今天的大多数 Agent 作品，卡在四道鸿沟上：流程写死在代码里，换个任务就得改程序；调度是个黑盒，为什么选这个 Agent 没人说得清；结果说完成了，却无法验证数字对不对；失败之后只会重来，不知道有没有变好。我们把这些环节做成平台能力：声明式编排、可解释调度、五层验证，加上一个带预算的修复闭环。",
        "segs": [
            {"src": "幻灯片/slide-02.png", "dur": 15.0},
            {"src": "幻灯片/slide-03.png", "dur": None},
        ],
    },
    {
        "key": "03",
        "label": "启动与编排台",
        "narration": "一条命令启动，纯 Python 标准库，不装任何依赖，断网也能跑。这是编排台：左边是流程画布，节点、并行分支、循环回边一目了然；点开任意节点，能看到它的 Agent、工具、输入输出映射和验证规约。整套流程保存为一份 JSON，可以进版本库、可以被评审。",
        "segs": [
            {"src": "01-仪表盘.mp4", "in": 0.5, "out": 6.5},
            {"src": "04-资源中心.mp4", "in": 2.0, "out": 8.0},
            {"src": "02-编排台与节点详情.mp4", "in": 3.0, "out": None},
        ],
    },
    {
        "key": "04",
        "label": "运行与实时事件",
        "narration": "点击运行。所有节点状态通过 SSE 实时推送到前端。这里可以看到调度器的每一次决策：候选是谁、得了多少分、为什么选它。数据扫描和文献调研两个分支并行执行，互不阻塞。注意这一行——统计建模师的输出已经出来了，但系统没有直接采信，而是把它交给验证流水线。",
        "segs": [
            {"src": "03-运行监控与实时事件.mp4", "in": 3.0, "out": 33.0},
        ],
    },
    {
        "key": "05",
        "label": "验证失败与自循环",
        "narration": "第一轮验证失败：数值检查发现最大系数偏差 34.4%，超过 15% 的容差。关键在于，系统给出的不是一句「再试一次」，而是结构化反馈：哪一项失败、期望值多少、实际值多少、偏差多少。这份反馈被注入修复节点，定向重跑。第二轮，偏差进入容差，五层全部通过，置信度达标。整个修复过程有轮次上限和收敛判据，不会无限重试。",
        "segs": [
            {"src": "幻灯片/slide-09.png", "dur": 10.0},
            {"src": "03-运行监控与实时事件.mp4", "in": 16.5, "out": 27.0, "zoom": 1.10},
            {"src": "03-运行监控与实时事件.mp4", "in": 27.0, "out": 34.5},
        ],
    },
    {
        "key": "06",
        "label": "架构：控制面与执行面",
        "narration": "架构上，我们把控制面和执行面彻底分开。工作流引擎、调度器、验证流水线在控制面；Agent 运行时、工具沙箱、模型客户端在执行面。引擎只能通过一层上下文接口去调用它们，不直接依赖任何模型实现。好处很直接：引擎可以被单元测试，模型可以被整体替换，Mock 和真实大模型走同一条路径。",
        "segs": [
            {"src": "幻灯片/slide-04.png", "dur": None},
        ],
    },
    {
        "key": "07",
        "label": "算法：调度与五层验证",
        "narration": "调度不是一个黑盒。我们把五种策略统一到一套加权打分上，每次决策都留下分数、理由和负载，可以回放、可以调参、可以被度量。验证则是五层漏斗：结构对不对、数值对不对、代码能不能复现、另一个 Agent 独立复算是否一致、统计上是否显著。任何一项硬性检查失败，直接一票否决，不会被平均分掩盖。跳过项不计入分母，置信度不会虚高。",
        "segs": [
            {"src": "幻灯片/slide-07.png", "dur": 18.0},
            {"src": "幻灯片/slide-08.png", "dur": None},
        ],
    },
    {
        "key": "08",
        "label": "价值收尾",
        "narration": "最后，一份带图表和指标的 Markdown 报告自动生成，所有中间产物都落在 run 目录里，可以复查、可以对比。基准测试里，加了验证与自循环会多花时间和 Token，但误差显著下降、置信度可量化——这就是我们愿意付的代价。织流 AgentWeave：可编排，可解释，可验证。可验证，才是可交付。",
        "segs": [
            {"src": "幻灯片/slide-10.png", "dur": 8.0},
            {"src": "幻灯片/slide-11.png", "dur": 8.0},
            {"src": "卡片/片尾卡-队伍与二维码.png", "dur": None},
        ],
    },
]


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{cmd[-1]}\n{proc.stderr[-1500:]}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    import re
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", proc.stderr)
    if not m:
        raise RuntimeError(f"读不到时长：{path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def synth_tts(text: str, out_wav: Path) -> None:
    """Windows System.Speech 合成中文旁白（Huihui，Rate=0）。"""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    txt = out_wav.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    ps1 = out_wav.with_suffix(".ps1")
    ps = (
        "Add-Type -AssemblyName System.Speech;" 
        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.SelectVoice('Microsoft Huihui Desktop');"
        "$s.Rate = 1;"
        f"$s.SetOutputToWaveFile('{out_wav}');"
        f"$s.Speak([System.IO.File]::ReadAllText('{txt}', [System.Text.Encoding]::UTF8));"
        "$s.Dispose();"
    )
    ps1.write_bytes(b"\xef\xbb\xbf" + ps.replace("\n", "\r\n").encode("utf-8"))
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"TTS 失败：{proc.stderr[-800:]}")


def still_clip(img: Path, dur: float, out: Path) -> None:
    """静帧 + 缓慢推近（zoompan），30fps。"""
    frames = max(2, int(round(dur * FPS)))
    zoom = f"zoompan=z='min(1.0+0.0016*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}"
    run([FFMPEG, "-y", "-loglevel", "error", "-loop", "1", "-i", str(img), "-t", f"{dur:.3f}",
         "-vf", f"{zoom},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)])


def footage_clip(src: Path, start: float, dur: float, out: Path, zoom: float = 1.0) -> None:
    vf = f"fps={FPS},scale={int(W*zoom)}:-2,crop={W}:{H},format=yuv420p"
    run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", str(FPS), "-an", str(out)])


def build_scene(scene: dict, index: int) -> Path:
    key = scene["key"]
    work = WORK / key
    work.mkdir(parents=True, exist_ok=True)

    wav = work / f"tts_{key}.wav"
    if not wav.exists():
        print(f"  TTS {key} ...", flush=True)
        synth_tts(scene["narration"], wav)
    tts_dur = probe_duration(wav)
    total = round(tts_dur + 0.9, 2)          # 尾部留白，避免字幕被截断

    # 按 segs 分配时长：固定 dur 优先，其余平分剩余
    fixed = sum(s["dur"] for s in scene["segs"] if s.get("dur"))
    flex = [s for s in scene["segs"] if not s.get("dur")]
    remain = max(1.0, total - fixed)
    durations = []
    for s in scene["segs"]:
        if s.get("dur"):
            durations.append(float(s["dur"]))
        else:
            durations.append(remain / len(flex))
    # 末段吸收取整误差
    durations[-1] += total - sum(durations)

    parts: list[Path] = []
    for i, (seg, dur) in enumerate(zip(scene["segs"], durations)):
        dur = max(0.4, dur)
        out = work / f"seg_{i:02d}.mp4"
        src_path = ASSETS / seg["src"]
        if src_path.suffix.lower() == ".mp4":
            clip_dur = min(dur, max(0.4, probe_duration(src_path) - float(seg.get("in", 0))))
            if clip_dur < dur:                       # 素材不够长就定格补足
                base = work / f"seg_{i:02d}_base.mp4"
                footage_clip(src_path, float(seg.get("in", 0)), clip_dur, base, float(seg.get("zoom", 1.0)))
                last = work / f"seg_{i:02d}_last.png"
                run([FFMPEG, "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", str(base), "-frames:v", "1", str(last)])
                tail = work / f"seg_{i:02d}_tail.mp4"
                still_clip(last, dur - clip_dur, tail)
                lst = work / f"seg_{i:02d}.txt"
                lst.write_text(f"file '{base.as_posix()}'\nfile '{tail.as_posix()}'\n", encoding="utf-8")
                run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-c", "copy", str(out)])
            else:
                footage_clip(src_path, float(seg.get("in", 0)), dur, out, float(seg.get("zoom", 1.0)))
        else:
            still_clip(src_path, dur, out)
        parts.append(out)

    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    video = work / "video.mp4"
    run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(video)])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    final = OUT_DIR / f"scene_{key}.mp4"
    run([FFMPEG, "-y", "-loglevel", "error", "-i", str(video), "-i", str(wav),
         "-filter_complex", "[1:a]aresample=48000,apad[a]", "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000",
         "-t", f"{total:.3f}", "-movflags", "+faststart", str(final)])
    print(f"  scene_{key}: {total:.2f}s (tts {tts_dur:.2f}s) -> {final.name} {final.stat().st_size // 1024} KB", flush=True)
    return final


def main() -> None:
    picks = [int(a) for a in sys.argv[1:] if a.isdigit()]
    total = 0.0
    for i, scene in enumerate(SCENES, start=1):
        if picks and i not in picks:
            continue
        print(f"[{i}/8] {scene['label']}", flush=True)
        main_out = build_scene(scene, i)
    files = sorted(OUT_DIR.glob("scene_*.mp4"))
    for f in files:
        total += probe_duration(f)
    print(f"done: {len(files)} 段，合计 {total:.1f}s = {int(total//60)}:{total%60:04.1f}")
    (OUT_DIR / "时长.json").write_text(json.dumps({"total": round(total, 2)}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
