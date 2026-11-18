#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 8 段已成片片段拼出最终演示视频，并烧入与旁白对齐的字幕。

为什么需要它：剪映里时间线已经拼好（8 段 + 智能字幕），但本机剪映未登录，
导出按钮变成「开通会员并导出」（会员墙）。本脚本用完全相同的 8 段素材
（docs/视频素材/成片片段/scene_01..08.mp4）拼接，并按每段旁白的字数比例
生成同步字幕，产出与剪映导出等价、无水印的成片。

依赖：imageio-ffmpeg 自带的 ffmpeg（含 libass）。
用法：py -3.14 scripts/build_final_video.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "docs" / "视频素材" / "成片片段"
OUT = ROOT / "docs" / "AgentWeave_演示视频_织流AgentWeave.mp4"
WORK = ROOT / ".final_video"

sys.path.insert(0, str(ROOT / "scripts"))
import build_scenes as B  # noqa: E402  # 复用 SCENES 的旁白文本

FF = get_ffmpeg_exe()

# 字幕样式：白字 + 黑边，底部居中
STYLE = (
    "FontName=Microsoft YaHei,FontSize=11,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1.2,Shadow=0,"
    "MarginV=22,Alignment=2"
)

PUNCT = "，。；：、！？,.!?;:—"
TAIL_PAD = 0.9  # 每段末尾留白（与 build_scenes 一致）


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [FF, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise RuntimeError(f"无法读取时长：{path}")
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def split_lines(text: str, limit: int = 24) -> list[str]:
    """按标点切句，过长再按 22 字硬切，保证每行不超过 22 个汉字。"""
    units = re.findall(rf"[^{PUNCT}]+[{PUNCT}]?", text)
    lines: list[str] = []
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        while len(unit) > limit:
            cut = max(unit.rfind("，", 0, limit), unit.rfind("、", 0, limit), unit.rfind(" ", 0, limit))
            if cut <= 0:
                cut = limit
            else:
                cut += 1
            lines.append(unit[:cut])
            unit = unit[cut:]
        if unit:
            lines.append(unit)
    # 合并过短的相邻行，让字幕不那么碎
    merged: list[str] = []
    for line in lines:
        if merged and len(merged[-1]) + len(line) <= limit:
            merged[-1] += line
        else:
            merged.append(line)
    return merged


def fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt() -> Path:
    entries: list[str] = []
    offset = 0.0
    idx = 1
    for scene in B.SCENES:
        clip = CLIPS / f"scene_{scene['key']}.mp4"
        dur = probe_duration(clip)
        speech = max(dur - TAIL_PAD, 1.0)
        text = scene["narration"].strip()
        lines = split_lines(text)
        total_chars = sum(len(x) for x in lines) or 1
        t = 0.0
        for line in lines:
            share = speech * len(line) / total_chars
            start, end = offset + t, offset + t + share - 0.08
            if end - start >= 0.6:
                entries.append(f"{idx}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{line}\n")
                idx += 1
            t += share
        offset += dur
    srt = WORK / "subtitle.srt"
    srt.write_text("\n".join(entries), encoding="utf-8")
    print(f"字幕 {idx - 1} 条，总时长 {offset:.2f}s")
    return srt


def concat_clips() -> Path:
    lst = WORK / "concat.txt"
    lines = []
    for scene in B.SCENES:
        clip = CLIPS / f"scene_{scene['key']}.mp4"
        lines.append(f"file '{clip.as_posix()}'")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    base = WORK / "base.mp4"
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(base)],
        check=True,
    )
    print(f"拼接完成：{base.name} {probe_duration(base):.2f}s")
    return base


def burn(base: Path, srt: Path) -> None:
    rel = srt.relative_to(WORK).as_posix()
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(base),
         "-vf", f"subtitles={rel}:force_style='{STYLE}'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", "30",
         "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", str(OUT)],
        check=True, cwd=str(WORK),
    )
    print(f"成片：{OUT}  {OUT.stat().st_size / 1048576:.1f} MB  "
          f"{probe_duration(OUT):.2f}s")


def main() -> None:
    WORK.mkdir(exist_ok=True)
    srt = build_srt()
    base = concat_clips()
    burn(base, srt)


if __name__ == "__main__":
    main()
