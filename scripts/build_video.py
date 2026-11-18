# -*- coding: utf-8 -*-
"""按 docs/视频脚本.md 生成 4 分钟演示视频（静态视觉 + TTS 旁白 + 硬字幕）。

用法：
    py -3.14 scripts/build_video.py

产物：media/AgentWeave_演示视频_织流AgentWeave.mp4（1920x1080 / 25fps / H.264 + AAC）

设计说明：
- 不依赖实时录屏（此前 Edge/CDP 录屏不稳定），全部由已知素材合成：
  PPT 导出的 1920x1080 幻灯片、真实界面截图、PIL 渲染的钩子文字卡。
- 旁白用 Windows System.Speech（Huihui，zh-CN）；每段旁白独立 wav，按场景起点延迟混音。
- 字幕为硬字幕：底部长条、白字，长句按标点切成 <=22 字的块，每块出现时间按字数比例分配。
- 底部有进度条与场景号，画面不是纯静帧堆叠。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "media"
TTS = MEDIA / "tts"
FRAMES = MEDIA / "frames"
CLIPS = MEDIA / "clips"
SLIDES = MEDIA / "slides"
OUT = MEDIA / "AgentWeave_演示视频_织流AgentWeave.mp4"

W, H, FPS = 1920, 1080, 25
FONT_CN = "C:/Windows/Fonts/msyh.ttc"
FONT_CN_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

NAVY = (10, 18, 32)
NAVY_2 = (16, 28, 48)
INK = (232, 240, 250)
GREY = (150, 168, 190)
ACCENT = (78, 190, 200)
RED = (232, 96, 96)

# ---------------------------------------------------------------- 场景脚本

SCENES = [
    {
        "key": "01_hook",
        "label": "钩子",
        "visuals": ["card_hook_1", "card_hook_2", "slide-01"],
        "narration": (
            "多智能体不是把几个 Agent 串起来。真正的难题是——七个 Agent 交出一份报告，"
            "你凭什么相信它是对的？我们做了一个平台，专门回答这个问题。"
        ),
    },
    {
        "key": "02_pain",
        "label": "痛点与方案",
        "visuals": ["slide-02", "slide-03"],
        "narration": (
            "今天的大多数 Agent 作品，卡在四道鸿沟上：流程写死在代码里，换个任务就得改程序；"
            "调度是个黑盒，为什么选这个 Agent 没人说得清；结果说完成了，却无法验证数字对不对；"
            "失败之后只会重来，不知道有没有变好。我们把这些环节做成平台能力：声明式编排、"
            "可解释调度、五层验证，加上一个带预算的修复闭环。"
        ),
    },
    {
        "key": "03_build",
        "label": "启动与编排台",
        "visuals": ["slide-06", "shot_11-flow"],
        "narration": (
            "一条命令启动，纯 Python 标准库，不装任何依赖，断网也能跑。这是编排台："
            "左边是流程画布，节点、并行分支、循环回边一目了然；点开任意节点，"
            "能看到它的 Agent、工具、输入输出映射和验证规约。整套流程保存为一份 JSON，"
            "可以进版本库、可以被评审。"
        ),
    },
    {
        "key": "04_run",
        "label": "运行与实时事件",
        "visuals": ["shot_12-runtime", "slide-06"],
        "narration": (
            "点击运行。所有节点状态通过 SSE 实时推送到前端。这里可以看到调度器的每一次决策："
            "候选是谁、得了多少分、为什么选它。数据扫描和文献调研两个分支并行执行，互不阻塞。"
            "注意这一行——统计建模师的输出已经出来了，但系统没有直接采信，而是把它交给验证流水线。"
        ),
    },
    {
        "key": "05_verify",
        "label": "验证失败与自循环",
        "visuals": ["slide-09", "shot_04-verify"],
        "narration": (
            "第一轮验证失败：数值检查发现最大系数偏差 34.4%，超过 15% 的容差。"
            "关键在于，系统给出的不是一句「再试一次」，而是结构化反馈：哪一项失败、期望值多少、"
            "实际值多少、偏差多少。这份反馈被注入修复节点，定向重跑。第二轮，偏差进入容差，"
            "五层全部通过，置信度达标。整个修复过程有轮次上限和收敛判据，不会无限重试。"
        ),
    },
    {
        "key": "06_arch",
        "label": "架构：控制面与执行面",
        "visuals": ["slide-04"],
        "narration": (
            "架构上，我们把控制面和执行面彻底分开。工作流引擎、调度器、验证流水线在控制面；"
            "Agent 运行时、工具沙箱、模型客户端在执行面。引擎只能通过一层上下文接口去调用它们，"
            "不直接依赖任何模型实现。好处很直接：引擎可以被单元测试，模型可以被整体替换，"
            "Mock 和真实大模型走同一条路径。"
        ),
    },
    {
        "key": "07_algo",
        "label": "可解释调度与五层验证",
        "visuals": ["slide-07", "slide-08"],
        "narration": (
            "调度不是一个黑盒。我们把五种策略统一到一套加权打分上，每次决策都留下分数、理由和负载，"
            "可以回放、可以调参、可以被度量。验证则是五层漏斗：结构对不对、数值对不对、"
            "代码能不能复现、另一个 Agent 独立复算是否一致、统计上是否显著。"
            "任何一项硬性检查失败，直接一票否决，不会被平均分掩盖。跳过项不计入分母，置信度不会虚高。"
        ),
    },
    {
        "key": "08_outro",
        "label": "可验证才是可交付",
        "tail": 2.8,
        "visuals": ["slide-10", "slide-11", "card_team"],
        "narration": (
            "最后，一份带图表和指标的 Markdown 报告自动生成，所有中间产物都落在运行目录里，"
            "可以复查、可以对比。基准测试里，加了验证与自循环会多花时间和 Token，"
            "但误差显著下降、置信度可量化——这就是我们愿意付的代价。"
            "织流 AgentWeave：可编排，可解释，可验证。可验证，才是可交付。"
        ),
    },
]


# ---------------------------------------------------------------- 工具函数

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_CN_BOLD if bold else FONT_CN, size)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as f:
        return f.getnframes() / float(f.getframerate())


def tts_scene(scene: dict) -> Path:
    out = TTS / f"{scene['key']}.wav"
    if out.exists() and out.stat().st_size > 2000:
        return out
    txt = TTS / f"{scene['key']}.txt"
    txt.write_text(scene["narration"], encoding="utf-8")
    ps = MEDIA / "_tts.ps1"
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps),
         "-TextFile", str(txt), "-OutFile", str(out), "-Rate", "1"])
    if not out.exists():
        raise RuntimeError(f"TTS 失败：{scene['key']}")
    return out


PUNCT = "。！？；：，、"


def split_subtitles(text: str, limit: int = 22) -> list[str]:
    """按标点切块，每块 <= limit 字；过长的句子再按长度硬切。"""
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in PUNCT and len(buf) >= 6:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    chunks: list[str] = []
    for p in parts:
        if len(p) <= limit:
            chunks.append(p)
            continue
        # 在逗号/顿号处再切
        while len(p) > limit:
            cut = max(p.rfind(c, 0, limit) for c in "，、；：") + 1
            if cut <= 1:
                cut = limit
            chunks.append(p[:cut])
            p = p[cut:]
        if p:
            chunks.append(p)
    return chunks


# ---------------------------------------------------------------- 视觉素材


def ensure_slides() -> None:
    """PPT 幻灯片导出为 1920x1080 PNG（首次运行需要在装 PowerPoint 的机器上导出一次）。"""
    if SLIDES.exists() and len(list(SLIDES.glob("slide-*.png"))) >= 10:
        return
    SLIDES.mkdir(parents=True, exist_ok=True)
    ps = MEDIA / "_export.ps1"
    ps.write_text(
        "$ErrorActionPreference = 'Stop'\r\n"
        "$app = New-Object -ComObject PowerPoint.Application\r\n"
        "$pres = $app.Presentations.Open('%s', $true, $false, $false)\r\n"
        "for ($i = 1; $i -le $pres.Slides.Count; $i++) {\r\n"
        "  $pres.Slides.Item($i).Export('%s\\slide-' + $i.ToString('00') + '.png', 'PNG', 1920, 1080)\r\n"
        "}\r\n$pres.Close(); $app.Quit()\r\n" % (ROOT / "docs" / "织流AgentWeave-演示PPT.pptx",
                                                     SLIDES.as_posix()),
        encoding="utf-8-sig")
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)])


def build_card(name: str, lines: list[tuple[str, int, tuple[int, int, int]]],
               subtitle: str = "") -> Path:
    """钩子/团队文字卡：黑底、逐行大字。"""
    path = FRAMES / f"{name}.png"
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    total = sum(size + 34 for _, size, _ in lines)
    y = (H - 190 - total) // 2
    for text, size, color in lines:
        f = font(size, bold=True)
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, font=f, fill=color)
        y += size + 34
    if subtitle:
        draw_subtitle(d, subtitle)
    img.save(path)
    return path


def draw_subtitle(d: ImageDraw.ImageDraw, text: str) -> None:
    if not text.strip():
        return
    f = font(42)
    lines = [text]
    if len(text) > 26:
        mid = len(text) // 2
        best = min(range(8, len(text) - 8), key=lambda i: abs(d.textlength(text[:i], font=f) - W * 0.5))
        lines = [text[:best], text[best:]]
    bw = max(d.textlength(t, font=f) for t in lines) + 90
    bh = 54 * len(lines) + 44
    x0 = (W - bw) / 2
    y0 = H - 150 - (bh - 90)
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=14, fill=(0, 0, 0))
    for i, t in enumerate(lines):
        tw = d.textlength(t, font=f)
        d.text(((W - tw) / 2, y0 + 22 + i * 54), t, font=f, fill=(255, 255, 255))


def load_visual(name: str) -> Image.Image:
    if name.startswith("card_"):
        p = FRAMES / f"{name}.png"
        if not p.exists():
            raise FileNotFoundError(p)
        return Image.open(p).convert("RGB")
    if name.startswith("slide-"):
        return Image.open(SLIDES / f"{name}.png").convert("RGB")
    return Image.open(ROOT / "screenshots" / f"{name.replace('shot_', '')}.png").convert("RGB")


def compose_frame(visual: Image.Image | None, subtitle: str, scene_no: int,
                  scene_label: str, progress: float, *, full_bleed: bool = False) -> Image.Image:
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)

    if visual is not None and full_bleed:
        img.paste(visual.resize((W, H)))
    elif visual is not None:
        # 背景：铺满 + 高斯模糊 + 压暗，避免两侧大片纯色空白
        bg = visual.resize((W, H), Image.LANCZOS)
        from PIL import ImageFilter
        bg = bg.filter(ImageFilter.GaussianBlur(26))
        bg = Image.eval(bg, lambda v: int(v * 0.32))
        img.paste(bg, (0, 0))
        d = ImageDraw.Draw(img)
        # 视觉区：1680x840 内等比缩放
        vw, vh = 1680, 820
        ratio = min(vw / visual.width, vh / visual.height)
        nw, nh = int(visual.width * ratio), int(visual.height * ratio)
        vis = visual.resize((nw, nh), Image.LANCZOS)
        x = (W - nw) // 2
        y = 96 + (820 - nh) // 2
        img.paste(vis, (x, y))
        d.rectangle([x - 1, y - 1, x + nw, y + nh], outline=(40, 66, 100))

    # 顶部品牌与场景号
    d.text((60, 34), "织流 AgentWeave · 多智能体编排调度与可信验证平台", font=font(26, True), fill=GREY)
    tag = f"{scene_no:02d} / 08 · {scene_label}"
    d.text((W - 60 - d.textlength(tag, font=font(26)), 34), tag, font=font(26), fill=ACCENT)

    # 进度条
    d.rectangle([0, H - 8, W, H], fill=(22, 34, 54))
    d.rectangle([0, H - 8, int(W * progress), H], fill=ACCENT)

    draw_subtitle(d, subtitle)
    return img


# ---------------------------------------------------------------- 主流程

def main() -> int:
    for p in (TTS, FRAMES, CLIPS):
        p.mkdir(parents=True, exist_ok=True)
    ensure_slides()

    # 0) CT 素材：钩子卡与团队卡（含字幕的版本在渲染时叠加）
    build_card("card_hook_1", [("7 个 Agent 协作", 96, INK), ("→ 交出一份报告", 96, INK)])
    build_card("card_hook_2", [("但你怎么证明它算对了？", 88, RED)])
    team_card = build_card("card_team", [
        ("可编排 · 可解释 · 可验证", 82, INK),
        ("可验证，才是可交付", 82, ACCENT),
        ("织流 AgentWeave · 石家庄信息工程职业学院", 34, GREY),
        ("张康博 · 指导教师：徐宇丰", 34, GREY),
    ])
    qr = ROOT / "docs" / "qrcode_repo.png"
    if qr.exists():
        img = Image.open(team_card).convert("RGB")
        q = Image.open(qr).convert("RGB").resize((228, 228), Image.LANCZOS)
        img.paste(q, (W - 228 - 120, H - 228 - 210))
        ImageDraw.Draw(img).text((W - 228 - 120, H - 210 + 8),
                                 "github.com/zhkb06/agentweave",
                                 font=font(22), fill=GREY)
        img.save(team_card)

    # 1) 旁白
    print("[1/4] 生成旁白 ...", flush=True)
    durations = {}
    for sc in SCENES:
        wav = tts_scene(sc)
        durations[sc["key"]] = wav_duration(wav)
        print(f"  {sc['key']}: {durations[sc['key']]:.2f}s", flush=True)

    # 2) 字幕分块与时间轴
    print("[2/4] 排时间轴与渲染帧 ...", flush=True)
    total_tts = sum(durations.values())
    tail = 1.1
    scene_meta = []
    timeline_total = total_tts + sum(sc.get("tail", tail) for sc in SCENES)
    t0 = 0.0
    for idx, sc in enumerate(SCENES, start=1):
        chunks = split_subtitles(sc["narration"])
        weights = [len(c) for c in chunks]
        wsum = sum(weights)
        dur = durations[sc["key"]] + sc.get("tail", tail)
        segs = []
        acc = 0.0
        for i, (chunk, wgt) in enumerate(zip(chunks, weights)):
            seg_dur = dur * wgt / wsum
            if i == len(chunks) - 1:
                seg_dur = dur - acc
            segs.append({"text": chunk, "start": acc, "dur": seg_dur})
            acc += seg_dur
        # 最后一段旁白结束后留一小段无字幕收尾（只给带 tail 的场景）
        if sc.get("tail", tail) > 1.6:
            hold = max(0.8, sc["tail"] - 0.6)
            segs.append({"text": "", "start": acc, "dur": hold})
            acc += hold
            dur += hold
        scene_meta.append({"scene": sc, "index": idx, "start": t0, "dur": dur, "segs": segs})
        t0 += dur
    print(f"  总时长 {t0:.2f}s / {int(t0 // 60)}:{t0 % 60:04.1f}", flush=True)

    # 3) 渲染帧，并按时间轴写 concat 清单（用 duration 精确控制每段时长）
    print("[3/4] 渲染帧与时间轴 ...", flush=True)
    concat_txt = MEDIA / "concat.txt"
    entries: list[str] = []
    last_frame: Path | None = None
    for meta in scene_meta:
        sc = meta["scene"]
        visuals = sc["visuals"]
        n = len(meta["segs"])
        for j, seg in enumerate(meta["segs"]):
            vname = visuals[min(len(visuals) - 1, j * len(visuals) // max(1, n))]
            visual = load_visual(vname)
            full_bleed = vname.startswith("card_")
            progress = (meta["start"] + seg["start"]) / timeline_total
            frame = compose_frame(visual, seg["text"], meta["index"], sc["label"],
                                  progress, full_bleed=full_bleed)
            fpath = FRAMES / f"{sc['key']}_{j:03d}.png"
            frame.save(fpath)
            entries.append(f"file '{fpath.as_posix()}'\nduration {seg['dur']:.3f}\n")
            last_frame = fpath
        print(f"  {sc['key']}: {n} 段", flush=True)
    if last_frame is not None:
        entries.append(f"file '{last_frame.as_posix()}'\n")
    concat_txt.write_text("".join(entries), encoding="utf-8")

    # 4) 拼接视频 + 混音
    print("[4/4] 编码与混音 ...", flush=True)
    video_only = MEDIA / "video_only.mp4"
    run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat_txt), "-vf", "fps=25,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "25",
         str(video_only)])

    inputs: list[str] = []
    filt: list[str] = []
    mix_in = []
    for k, meta in enumerate(scene_meta):
        wav = TTS / f"{meta['scene']['key']}.wav"
        inputs += ["-i", str(wav)]
        delay = int(round((meta["start"] + 0.25) * 1000))
        filt.append(f"[{k + 1}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"adelay={delay}|{delay}[a{k}]")
        mix_in.append(f"[a{k}]")
    filt.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=longest[out]")
    run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(video_only)]
        + inputs + ["-filter_complex", ";".join(filt), "-map", "0:v", "-map", "[out]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-t", f"{timeline_total:.3f}",
                    "-movflags", "+faststart", str(OUT)])

    dur = subprocess.run([ffmpeg(), "-i", str(OUT)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", dur)
    total = float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3)) if m else 0
    size_mb = OUT.stat().st_size / 1048576
    print(f"\n完成：{OUT}  {total:.1f}s / {size_mb:.1f} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
