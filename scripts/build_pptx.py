#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 14 页演示 PPT（python-pptx）。

用法::

    <bundled python> scripts/build_pptx.py

输出：docs/织流AgentWeave-演示PPT.pptx
设计：深蓝底 #0B1B33 + 青色 #17C3B2 + 暖橙 #F29E4C，16:9。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SHOTS = ROOT / "screenshots"
OUT = DOCS / "织流AgentWeave-演示PPT.pptx"

# ---------- 视觉常量 ----------
NAVY = RGBColor(0x0B, 0x1B, 0x33)
NAVY_2 = RGBColor(0x10, 0x25, 0x44)
NAVY_3 = RGBColor(0x16, 0x2F, 0x52)
CYAN = RGBColor(0x17, 0xC3, 0xB2)
CYAN_DIM = RGBColor(0x0E, 0x7F, 0x76)
ORANGE = RGBColor(0xF2, 0x9E, 0x4C)
WHITE = RGBColor(0xF2, 0xF7, 0xFF)
MUTED = RGBColor(0x9F, 0xB3, 0xCC)
DIM = RGBColor(0x6B, 0x81, 0xA0)
FONT = "Microsoft YaHei"

W, H = 13.333, 7.5


def rgb(hex_str: str) -> RGBColor:
    hex_str = hex_str.lstrip("#")
    return RGBColor(int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))


def set_bg(slide, color: RGBColor = NAVY) -> None:
    bg = slide.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = color


def rect(slide, x, y, w, h, color=None, line=None, line_w=0.75, shape=MSO_SHAPE.RECTANGLE, radius=None):
    shp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if color is None:
        shp.fill.background()
    else:
        shp.fill.solid()
        shp.fill.fore_color.rgb = color
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(line_w)
    shp.shadow.inherit = False
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            shp.adjustments[0] = radius
        except Exception:
            pass
    return shp


def text(slide, x, y, w, h, content, size=16, color=WHITE, bold=False,
         align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, font=FONT, spacing=1.0,
         italic=False, margins=(0, 0, 0, 0)):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(margins[0])
    tf.margin_right = Inches(margins[1])
    tf.margin_top = Inches(margins[2])
    tf.margin_bottom = Inches(margins[3])
    tf.vertical_anchor = anchor
    lines = content.split("\n") if isinstance(content, str) else list(content)
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.name = font
        r.font.color.rgb = color
    return box


def bullet_list(slide, x, y, w, h, items, size=15, gap=10, marker_color=CYAN,
                color=WHITE, spacing=1.15):
    """每条前面一个青色小方块 + 加粗关键词（用 ** 包裹）。"""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = spacing
        p.space_after = Pt(gap)
        lead = p.add_run()
        lead.text = "▪ "
        lead.font.size = Pt(size)
        lead.font.color.rgb = marker_color
        lead.font.name = FONT
        chunks = item.split("**")
        for j, chunk in enumerate(chunks):
            if not chunk:
                continue
            r = p.add_run()
            r.text = chunk
            r.font.size = Pt(size + (1 if j % 2 == 1 else 0))
            r.font.bold = j % 2 == 1
            r.font.name = FONT
            r.font.color.rgb = color if j % 2 == 0 else WHITE
    return box


def header(slide, kicker: str, title: str, page_no: int) -> None:
    rect(slide, 0, 0, W, 0.06, CYAN)
    text(slide, 0.7, 0.42, 8.5, 0.3, kicker, size=11, color=CYAN, bold=True)
    text(slide, 0.7, 0.72, 11.9, 0.7, title, size=27, color=WHITE, bold=True)
    rect(slide, 0.72, 1.52, 0.9, 0.045, CYAN)
    footer(slide, page_no)


def footer(slide, page_no: int) -> None:
    rect(slide, 0, H - 0.42, W, 0.42, NAVY_2)
    text(slide, 0.7, H - 0.36, 7.2, 0.26, "织流 AgentWeave · 2026 第五届未来杯 · 赛题一",
         size=9.5, color=DIM)
    text(slide, W - 1.3, H - 0.36, 0.6, 0.26, f"{page_no:02d}", size=9.5,
         color=MUTED, align=PP_ALIGN.RIGHT)


def notes(slide, content: str) -> None:
    slide.notes_slide.notes_text_frame.text = content


def placeholder_chip(slide, x, y, w, label, color=ORANGE):
    rect(slide, x, y, w, 0.3, NAVY_3, color, 0.75, MSO_SHAPE.ROUNDED_RECTANGLE)
    text(slide, x, y + 0.02, w, 0.26, label, size=9.5, color=color, align=PP_ALIGN.CENTER)


def crop(src: Path, box: tuple[int, int, int, int], tag: str) -> Path:
    """裁剪截图并轻微提亮（深色 UI 投影更清晰），缓存到 screenshots/_crop_<tag>.png。"""
    from PIL import Image, ImageEnhance
    dst = SHOTS / f"_crop_{tag}.png"
    with Image.open(src) as im:
        out = im.crop(box)
        out = ImageEnhance.Brightness(out).enhance(1.22)
        out = ImageEnhance.Contrast(out).enhance(1.04)
        out.save(dst)
    return dst


def screenshot(slide, path: Path, x, y, w, h, caption=""):
    """等比放入指定区域，居中，加细边框。"""
    if not path.is_file():
        rect(slide, x, y, w, h, NAVY_2, ORANGE, 1)
        text(slide, x, y + h / 2 - 0.2, w, 0.4, f"缺少截图：{path.name}",
             size=12, color=ORANGE, align=PP_ALIGN.CENTER)
        return
    from PIL import Image
    with Image.open(path) as im:
        iw, ih = im.size
    scale = min(w / iw, h / ih)
    dw, dh = iw * scale, ih * scale
    dx, dy = x + (w - dw) / 2, y + (h - dh) / 2
    frame = rect(slide, dx - 0.035, dy - 0.035, dw + 0.07, dh + 0.07, None, CYAN_DIM, 1)
    frame.line.width = Pt(1.2)
    slide.shapes.add_picture(str(path), Inches(dx), Inches(dy), Inches(dw), Inches(dh))
    if caption:
        text(slide, x, y + h + 0.08, w, 0.26, caption, size=9.5, color=MUTED,
             align=PP_ALIGN.CENTER)


def load_bench() -> dict:
    path = ROOT / "data" / "benchmark.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------- 各页 ----------
def slide_cover(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    # 装饰：右侧节点网络
    import math
    cx, cy, R = 10.9, 3.55, 1.55
    pts = []
    for i in range(6):
        ang = -math.pi / 2 + i * math.pi / 3
        pts.append((cx + R * math.cos(ang), cy + R * math.sin(ang)))
    for (x1, y1), (x2, y2) in [(pts[i], pts[(i + 1) % 6]) for i in range(6)] + [(pts[i], pts[(i + 3) % 6]) for i in range(3)]:
        conn = s.shapes.add_connector(1, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
        conn.line.color.rgb = RGBColor(0x1E, 0x4D, 0x7A)
        conn.line.width = Pt(1.2)
    for i, (x, y) in enumerate(pts):
        c = rect(s, x - 0.11, y - 0.11, 0.22, 0.22, CYAN if i % 2 == 0 else ORANGE,
                 shape=MSO_SHAPE.OVAL)
        c.fill.solid()
    rect(s, cx - 0.62, cy - 0.62, 1.24, 1.24, NAVY_3, CYAN, 1.5, MSO_SHAPE.OVAL)
    text(s, cx - 0.62, cy - 0.26, 1.24, 0.5, "Agent\nWeave", size=13, color=WHITE,
         bold=True, align=PP_ALIGN.CENTER, spacing=0.95)

    rect(s, 0.9, 0.9, 0.55, 0.08, CYAN)
    text(s, 0.9, 1.08, 6.2, 0.3, "2026 第五届「未来杯」创新大赛 · 赛题一", size=12,
         color=CYAN, bold=True)
    text(s, 0.86, 2.02, 8.2, 1.0, "织流 AgentWeave", size=50, color=WHITE, bold=True)
    text(s, 0.9, 3.12, 7.4, 0.9,
         "让多智能体从「能演示」走向「可交付」\n多智能体编排、调度与可信验证平台",
         size=20, color=MUTED, spacing=1.25)
    chips = ["可编排", "可解释", "可验证", "零依赖 · 离线可跑"]
    cx0 = 0.9
    for chip in chips:
        w = 0.28 + len(chip) * 0.135
        rect(s, cx0, 4.62, w, 0.42, NAVY_3, CYAN_DIM, 0.9, MSO_SHAPE.ROUNDED_RECTANGLE, 0.35)
        text(s, cx0, 4.68, w, 0.3, chip, size=12.5, color=WHITE, align=PP_ALIGN.CENTER)
        cx0 += w + 0.22
    text(s, 0.9, 6.55, 8.4, 0.32,
         "织流 AgentWeave · 石家庄信息工程职业学院 · 张康博 · 指导教师：徐宇丰",
         size=11.5, color=DIM)
    footer(s, 1)
    notes(s, "开场不要念标题，直接抛问题：「如果把 7 个 Agent 交给一个团队，你怎么证明它做对了？」"
             "然后自报队伍。控制 20 秒内进入下一页。")


def slide_pain(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "01 / 痛点", "Demo 很惊艳，落地很狼狈：四道工程鸿沟", 2)
    quad = [
        ("流程硬编码", "换任务就要改代码，\n编排无法被非开发者复用", ORANGE),
        ("调度黑盒", "为什么选这个 Agent？\n没人说得清，出错只能猜", ORANGE),
        ("结果不可验证", "模型说「已完成」，\n没人知道数字对不对", ORANGE),
        ("没有反馈闭环", "失败就重来，重来几次、\n是否变好，全凭感觉", ORANGE),
    ]
    for i, (t, d, c) in enumerate(quad):
        col, row = i % 2, i // 2
        x, y = 0.9 + col * 5.95, 2.05 + row * 2.31
        rect(s, x, y, 5.55, 1.95, NAVY_2, RGBColor(0x2A, 0x3F, 0x5E), 0.9,
             MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
        rect(s, x, y, 0.07, 1.95, c)
        text(s, x + 0.32, y + 0.24, 4.9, 0.36, t, size=18, color=WHITE, bold=True)
        text(s, x + 0.32, y + 0.78, 4.9, 0.9, d, size=13.5, color=MUTED, spacing=1.2)
    rect(s, 3.55, 6.42, 6.3, 0.55, NAVY_3, ORANGE, 1, MSO_SHAPE.ROUNDED_RECTANGLE, 0.4)
    text(s, 3.55, 6.48, 6.3, 0.4, "科研场景代价更高：一次错误归因，可能误导后续全部实验",
         size=13, color=ORANGE, align=PP_ALIGN.CENTER)
    notes(s, "强调这不是「框架不够多」，而是「工程环节缺失」。落一句过渡："
             "「所以我们做的不是又一个 Agent 框架，而是补齐这四块。」")


def slide_overview(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "02 / 方案总览", "AgentWeave：把编排做成可执行、可解释、可验证的资产", 3)
    stages = [
        ("声明式\n工作流", "JSON DSL"),
        ("可解释\n调度", "score / reason"),
        ("执行与\n工具沙箱", "Agent Runtime"),
        ("五层\n验证", "证据优先"),
        ("PDCA\n自循环", "定向修复"),
        ("可验证\n报告", "Markdown + SVG"),
    ]
    x0, y0, bw, gap = 0.75, 2.35, 1.85, 0.24
    for i, (name, sub) in enumerate(stages):
        x = x0 + i * (bw + gap)
        rect(s, x, y0, bw, 1.5, NAVY_2, CYAN_DIM, 1.1, MSO_SHAPE.ROUNDED_RECTANGLE, 0.08)
        text(s, x, y0 + 0.16, bw, 0.66, name, size=13.5, color=WHITE, bold=True,
             align=PP_ALIGN.CENTER, spacing=0.95)
        text(s, x, y0 + 1.02, bw, 0.3, sub, size=9.5, color=CYAN, align=PP_ALIGN.CENTER)
        if i < len(stages) - 1:
            text(s, x + bw - 0.02, y0 + 0.52, gap + 0.04, 0.4, "›", size=22,
                 color=CYAN, align=PP_ALIGN.CENTER)
    # 回环曲线（用折线 + 箭头示意）
    loop_y = 4.55
    rect(s, x0 + 2 * (bw + gap) + 0.15, loop_y, 3 * (bw + gap) - 0.1, 0.045, ORANGE)
    rect(s, x0 + 2 * (bw + gap) + 0.15, loop_y - 0.62, 0.045, 0.62, ORANGE)
    rect(s, x0 + 2 * (bw + gap) + 0.15, loop_y - 0.62, 1.4 * (bw + gap), 0.045, ORANGE)
    text(s, x0 + 3.6, loop_y + 0.14, 5.5, 0.3, "验证失败 → 结构化反馈 → 定向重跑（≤3 轮，无改善即停）",
         size=11.5, color=ORANGE)
    bullet_list(s, 0.75, 5.35, 5.9, 1.5, [
        "五大模块：Agent 定义 · 工作流编排 · 调度执行 · 验证监控 · 反馈优化",
        "人只写「做什么」，系统负责「谁来做、做得对不对」",
        "交付形态：Web 控制台 + REST/SSE + 命令行，三种入口同一内核",
    ], size=12.5, gap=8)
    rect(s, 8.55, 5.2, 3.95, 1.55, NAVY_2, CYAN_DIM, 0.9, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 8.75, 5.34, 3.6, 0.3, "离线确定性演示", size=13, color=CYAN, bold=True)
    text(s, 8.75, 5.68, 3.6, 0.95, "默认 Mock LLM，固定种子；\n也支持一键接入 OpenAI 兼容模型。",
         size=11, color=MUTED, spacing=1.2)
    notes(s, "这一页只讲结构不讲细节，让评委建立「五个能力 + 一条闭环」的整体印象。"
             "提醒自己：闭环是全场主角。")


def slide_arch(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "03 / 总体架构", "六层架构：引擎不碰模型，模型不碰状态", 4)
    layers = [
        ("L6 展示层", "原生 JS/SVG 控制台", "无构建 · 无 CDN · SSE 实时", 0.55),
        ("L5 接口层", "REST + SSE", "28 个接口 · 断线续传", 0.75),
        ("L4 控制面", "引擎 / 调度 / 验证 / 自循环", "可单测 · 可替换", 0.75),
        ("L3 执行面", "Agent Runtime / 工具沙箱 / MCP", "权限白名单 · 子进程隔离", 0.75),
        ("L2 基础设施", "事件总线 · 表达式求值 · 状态", "线程安全 · 有界队列", 0.6),
        ("L1 资产", "类型契约 · 配置 · 数据集", "唯一事实来源", 0.6),
    ]
    y = 1.9
    for i, (name, mid, right, alpha) in enumerate(layers):
        c = rgb(["#123055", "#14365F", "#173D6B", "#1A4576", "#1D4D82", "#20558E"][i])
        rect(s, 0.75, y, 11.85, 0.72, c, RGBColor(0x2A, 0x54, 0x86), 0.9,
             MSO_SHAPE.ROUNDED_RECTANGLE, 0.12)
        text(s, 0.98, y + 0.16, 2.3, 0.4, name, size=13.5, color=CYAN, bold=True)
        text(s, 3.3, y + 0.17, 5.6, 0.4, mid, size=12.5, color=WHITE)
        text(s, 8.9, y + 0.19, 3.5, 0.36, right, size=9.5, color=MUTED, align=PP_ALIGN.RIGHT)
        y += 0.78
    # 依赖方向箭头
    arr = s.shapes.add_shape(MSO_SHAPE.DOWN_ARROW, Inches(12.72), Inches(2.05), Inches(0.28), Inches(3.35))
    arr.fill.solid()
    arr.fill.fore_color.rgb = CYAN_DIM
    arr.line.fill.background()
    arr.shadow.inherit = False
    text(s, 11.95, 1.62, 1.3, 0.26, "单向依赖", size=8.5, color=CYAN, align=PP_ALIGN.RIGHT)
    rect(s, 0.75, 6.55, 11.85, 0.5, NAVY_2, CYAN_DIM, 0.8, MSO_SHAPE.ROUNDED_RECTANGLE, 0.25)
    text(s, 0.95, 6.62, 11.5, 0.36,
         "铁律：引擎只经 RuntimeContext 调 Agent/工具，不直接依赖 LLM 与工具实现 —— 因此引擎可被单测，模型可被替换。",
         size=11, color=MUTED)
    notes(s, "重点讲「解耦带来可测试性」——引擎可以被单测，模型可以被替换。"
             "若评委追问，指向 RuntimeContext 这一层。")


def slide_agents(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "04 / Agent 团队", "不是七个聊天机器人，是一支有岗位说明书的团队", 5)
    agents = [
        ("planner", "规划者", "0.2", "拆任务 / 成功判据", "task.decompose"),
        ("researcher", "调研者", "0.3", "方法选型 / 文献", "http_get"),
        ("data_engineer", "数据工程师", "0.2", "清洗 / 特征工程", "dataset_stats"),
        ("statistician", "统计建模师", "0.1", "回归 / 代码 / 指标", "python_exec"),
        ("verifier", "验证者", "0.0", "独立复算 / 偏差", "python_exec"),
        ("critic", "评审者", "0.4", "风险 / 改进建议", "无写权限"),
        ("writer", "撰写者", "0.5", "结构化报告", "report_render"),
        ("contract", "协作契约", "—", "输入契约 / 输出 Schema", "工具白名单 · 成功判据"),
    ]
    x0, y0, cw, ch, gx, gy = 0.75, 1.9, 1.68, 1.95, 0.14, 0.18
    for i, (aid, name, temp, duty, tool) in enumerate(agents):
        col, row = i % 4, i // 4
        x, y = x0 + col * (cw + gx), y0 + row * (ch + gy)
        accent = CYAN if aid == "verifier" else NAVY_3
        rect(s, x, y, cw, ch, NAVY_2, RGBColor(0x2A, 0x3F, 0x5E), 0.8,
             MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
        rect(s, x, y, cw, 0.05, CYAN if aid == "verifier" else CYAN_DIM)
        text(s, x + 0.14, y + 0.16, cw - 0.28, 0.3, name, size=13, color=WHITE, bold=True)
        text(s, x + 0.14, y + 0.52, cw - 0.28, 0.24, aid, size=8.5, color=DIM)
        if temp != "—":
            rect(s, x + 0.14, y + 0.86, 0.72, 0.28, NAVY_3, CYAN_DIM, 0.6,
                 MSO_SHAPE.ROUNDED_RECTANGLE, 0.4)
            text(s, x + 0.14, y + 0.9, 0.72, 0.22, f"T {temp}", size=8.5, color=CYAN,
                 align=PP_ALIGN.CENTER)
        text(s, x + 0.14, y + 1.24, cw - 0.28, 0.26, duty, size=9, color=MUTED)
        text(s, x + 0.14, y + 1.58, cw - 0.28, 0.24, tool, size=8, color=DIM)
    text(s, 0.75, 6.18, 7.6, 0.3,
         "温度分层：越靠近「判断与事实」，温度越低；验证者 T=0.0 且无写权限，防「自己改题自己判卷」。",
         size=10.5, color=MUTED)
    rect(s, 8.6, 6.0, 3.95, 0.62, NAVY_2, ORANGE, 0.9, MSO_SHAPE.ROUNDED_RECTANGLE, 0.2)
    text(s, 8.75, 6.08, 3.7, 0.46, "冲突消解四级：证据 → 复算 → 评审 → 人工闸门",
         size=9.5, color=ORANGE, spacing=1.05)
    notes(s, "强调「验证者温度 0.0、无写权限」是设计而非巧合。这是最容易被记住的设计细节。")


def slide_orchestrator(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "05 / 流程编排中心", "流程编排中心：拖出流程，点开每个节点看契约", 6)
    src = SHOTS / "11-flow.png"
    if src.is_file():
        src = crop(src, (232, 150, 1420, 1000), "flow")
    screenshot(s, src, 0.75, 1.78, 8.6, 4.55,
               "演示工作流 wf-ai4s-air-quality：12 节点、2 条并行分支、1 个 PDCA 自循环")
    bullet_list(s, 9.65, 1.9, 3.0, 4.6, [
        "画布即工作流：节点、连线、并行、循环回边一目了然",
        "点节点即看配置：Agent、工具、输入输出映射、验证规约",
        "保存即 JSON DSL，可进 Git、可评审、可复用",
        "同一份定义，Web 与命令行行为完全一致",
    ], size=10.5, gap=12)
    rect(s, 9.65, 5.55, 2.95, 1.3, NAVY_2, CYAN_DIM, 0.8, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 9.82, 5.66, 2.6, 0.28, "DSL 是唯一事实来源", size=11, color=CYAN, bold=True)
    text(s, 9.82, 5.98, 2.6, 0.75, "界面只是它的视图；\n换 Agent/换模型无需改引擎。",
         size=9.5, color=MUTED, spacing=1.15)
    notes(s, "现场不要拖拽（易出错），直接用预置工作流讲解。强调「DSL 是唯一事实来源」，界面只是它的视图。")


def slide_scheduler(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "06 / 调度算法", "可解释调度：分数、理由、负载，一次决策三样都留下", 7)
    screenshot(s, SHOTS / "12-runtime.png", 0.75, 1.86, 7.55, 4.95,
               "运行与验证中心：节点状态 / 调度决策 / 事件流 / 五层验证")
    # 公式卡
    rect(s, 8.55, 1.86, 4.05, 1.42, NAVY_2, CYAN_DIM, 1.0, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 8.72, 1.96, 3.7, 0.28, "自适应调度打分", size=11.5, color=CYAN, bold=True)
    text(s, 8.72, 2.28, 3.72, 0.9,
         "score = 0.40·能力 + 0.30·空闲\n＋ 0.20·优先级 + 0.10·成功率",
         size=10.5, color=WHITE, spacing=1.35)
    bullet_list(s, 8.55, 3.5, 4.05, 3.2, [
        "五策略：FIFO · 轮询 · 能力匹配 · 负载感知 · 自适应",
        "决策输出 score / reason / agent_load，前端可回放候选集",
        "三层并发：节点 4 · Agent 2 · 重试 2 次后回退 Mock",
        "创新不在公式复杂度，而在「调度结果可审计」",
    ], size=10, gap=10)
    notes(s, "正面回答「凭什么是创新」——创新不在公式复杂度，而在「调度结果是可审计对象」。"
             "可现场问评委：「您希望系统告诉您为什么选它吗？」")


def slide_verify(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "07 / 五层验证", "五层验证漏斗：从「格式对」到「统计上可信」", 8)
    layers = [
        ("L1 结构与非空", "字段齐全、JSON 可解析、非空", "硬性"),
        ("L2 数值对齐真值", "相对误差 ≤ 15%（隐藏真值）", "硬性"),
        ("L3 代码重跑复现", "申报指标 vs 独立复算", "硬性"),
        ("L4 跨 Agent 独立复算", "验证者独立重算并比对", "硬性"),
        ("L5 统计与评审", "n/R² 阈值 + 评审置信度", "加权"),
    ]
    y = 1.92
    widths = [9.4, 8.3, 7.2, 6.1, 5.0]
    for i, ((name, desc, kind), w) in enumerate(zip(layers, widths)):
        x = 0.75 + (9.4 - w) / 2
        c = rgb(["#1B4E8A", "#1A5E96", "#17709E", "#1383A5", "#0F96AC"][i])
        rect(s, x, y, w, 0.72, c, RGBColor(0x2A, 0x6B, 0xA8), 0.9,
             MSO_SHAPE.ROUNDED_RECTANGLE, 0.14)
        text(s, x + 0.28, y + 0.16, 2.72, 0.4, name, size=12.5, color=WHITE, bold=True)
        text(s, x + 3.0, y + 0.2, max(w - 3.2, 1.6), 0.34, desc, size=9.5, color=rgb("#D6ECFF"))
        tag_c = ORANGE if kind == "硬性" else CYAN
        rect(s, 10.55, y + 0.17, 0.85, 0.3, NAVY, tag_c, 0.8, MSO_SHAPE.ROUNDED_RECTANGLE, 0.4)
        text(s, 10.55, y + 0.21, 0.85, 0.22, kind, size=8.5, color=tag_c, align=PP_ALIGN.CENTER)
        y += 0.8
    # 置信度卡
    rect(s, 11.62, 1.92, 1.0, 0.0, NAVY)
    rect(s, 0.75, 6.05, 11.9, 0.72, NAVY_2, ORANGE, 1.0, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 0.95, 6.14, 11.5, 0.55,
         "passed = 置信度 ≥ 阈值 且 无硬性失败 —— 不给「平均分掩盖致命错」留口子；跳过项不计入分母。",
         size=12, color=ORANGE, bold=True)
    notes(s, "重点讲 L4 交叉验证与「一票否决」，这是防幻觉的核心。别陷入权重细节，留到答辩。")


def slide_loop(prs, bench):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "08 / 自循环案例", "一次真实的失败修复：从 0.62 到通过，只用了两轮", 9)
    # 左：PDCA 小状态机
    steps = [("P", "计划", CYAN), ("D", "执行", CYAN), ("C", "验证", ORANGE), ("A", "修正", CYAN)]
    y = 2.0
    for i, (ch, label, color) in enumerate(steps):
        rect(s, 0.85, y, 0.62, 0.62, NAVY_3, color, 1.1, MSO_SHAPE.ROUNDED_RECTANGLE, 0.14)
        text(s, 0.85, y + 0.08, 0.62, 0.4, ch, size=18, color=color, bold=True, align=PP_ALIGN.CENTER)
        text(s, 1.62, y + 0.12, 0.9, 0.36, label, size=11, color=MUTED)
        if i < 3:
            text(s, 1.05, y + 0.6, 0.3, 0.3, "↓", size=13, color=DIM)
        y += 0.95
    text(s, 0.78, 5.9, 2.4, 0.6, "≤3 轮，\n无改善即停", size=10.5, color=ORANGE, spacing=1.2)

    # 中：失败/成功对比
    rect(s, 3.15, 1.9, 2.55, 2.25, NAVY_2, ORANGE, 1.2, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 3.35, 2.02, 2.2, 0.3, "第 1 轮 · 验证失败", size=12, color=ORANGE, bold=True)
    text(s, 3.35, 2.4, 2.2, 0.85, "42.3%", size=40, color=ORANGE, bold=True)
    text(s, 3.35, 3.34, 2.15, 0.7, "wind 偏差 34.4%\n其余 3 系数 26.5%–37.2%",
         size=9.5, color=MUTED, spacing=1.15)
    rect(s, 5.95, 1.9, 2.55, 2.25, NAVY_2, CYAN, 1.2, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 6.15, 2.02, 2.2, 0.3, "第 2 轮 · 通过", size=12, color=CYAN, bold=True)
    text(s, 6.15, 2.4, 2.2, 0.85, "100%", size=40, color=CYAN, bold=True)
    text(s, 6.15, 3.34, 2.15, 0.7, "最大系数误差 7.1%\n四层检查全部通过",
         size=9.5, color=MUTED, spacing=1.15)
    text(s, 3.15, 4.3, 5.4, 0.5, "反馈不是「再试一次」，而是「哪项失败、期望多少、实际多少、偏差多少」",
         size=10.5, color=WHITE, spacing=1.2)
    # 右：验证中心截图（裁剪主体，放大可读性）
    src = SHOTS / "12-runtime.png"
    screenshot(s, src, 8.55, 1.85, 4.4, 4.55)
    text(s, 8.55, 6.55, 4.4, 0.28, "运行与验证中心：两轮检查记录与失败明细",
         size=9.5, color=MUTED, align=PP_ALIGN.CENTER)
    text(s, 8.55, 6.86, 4.4, 0.2, "全程可回放：每轮事件 / 代码 / 指标 / 报告落盘 data/runs/<run_id>/",
         size=7.5, color=DIM, align=PP_ALIGN.CENTER)
    notes(s, "这是全场最有说服力的一页，讲慢一点。强调失败是「设计出来的」——演示数据首轮必然偏差，"
             "才能展示闭环。")


def slide_replay(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "09 / 演示回放", "从一句话任务到一份可验证报告", 10)
    screenshot(s, SHOTS / "10-dashboard.png", 0.75, 1.86, 8.05, 4.55)
    frames = [
        ("① 并行分支同时跑", "scan 与 research 互不阻塞"),
        ("② 验证失败 → 反馈", "红色失败项与偏差明细"),
        ("③ 修复通过 → 报告", "自动生成 Markdown + SVG"),
    ]
    y = 1.9
    for t, d in frames:
        rect(s, 9.05, y, 3.55, 1.02, NAVY_2, CYAN_DIM, 0.8, MSO_SHAPE.ROUNDED_RECTANGLE, 0.07)
        text(s, 9.22, y + 0.12, 3.2, 0.3, t, size=11.5, color=WHITE, bold=True)
        text(s, 9.22, y + 0.46, 3.25, 0.4, d, size=9.5, color=MUTED)
        y += 1.16
    text(s, 0.75, 6.62, 11.9, 0.3,
         "SSE 实时事件流 · 断线可续传 · 产物落盘 data/runs/<run_id>/（report.md / metrics.json / events.jsonl / workflow.json）",
         size=10, color=DIM)
    notes(s, "此页是视频的「截图版」，现场只点 3 个关键帧讲。若时间紧，可跳过直接进基准页。")


def slide_bench(prs, bench):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "10 / 基准数据", "并行换来速度，验证换来确定性", 11)
    modes = ["A 单 Agent 串行", "B 多 Agent 并行", "C 并行 + 验证自循环"]
    values = {
        "耗时 (s)": [0.157, 0.386, 1.031],
        "Token": [5845, 21309, 27191],
        "最终误差 (%)": [34.38, 34.38, 7.14],
        "验证通过率 (%)": [0, 0, 100],
        "置信度 (%)": [87.3, 89.0, 93.0],
    }
    actual = (bench.get("modes") or {}) if isinstance(bench, dict) else {}
    if actual:
        try:
            values = {
                "耗时 (s)": [actual["A"]["wall_s"]["mean"], actual["B"]["wall_s"]["mean"], actual["C"]["wall_s"]["mean"]],
                "Token": [actual["A"]["tokens"]["mean"], actual["B"]["tokens"]["mean"], actual["C"]["tokens"]["mean"]],
                "最终误差 (%)": [actual["A"]["coef_error_pct"]["mean"] * 100,
                             actual["B"]["coef_error_pct"]["mean"] * 100,
                             actual["C"]["coef_error_pct"]["mean"] * 100],
                "验证通过率 (%)": [actual["A"]["verified_rate"] * 100,
                              actual["B"]["verified_rate"] * 100,
                              actual["C"]["verified_rate"] * 100],
                "置信度 (%)": [actual["A"]["confidence"]["mean"] * 100,
                            actual["B"]["confidence"]["mean"] * 100,
                            actual["C"]["confidence"]["mean"] * 100],
            }
        except Exception:
            pass
    metric_names = list(values.keys())
    x0, y0, row_h = 1.05, 2.0, 0.68
    col_w = [2.75, 2.55, 2.55, 2.55]
    # 表头
    heads = ["指标", *modes]
    x = x0
    for i, head in enumerate(heads):
        rect(s, x, y0, col_w[i], 0.55, NAVY_3)
        text(s, x, y0 + 0.12, col_w[i], 0.34, head, size=11.5, color=CYAN, bold=True,
             align=PP_ALIGN.CENTER if i else PP_ALIGN.LEFT)
        x += col_w[i]
    for r, name in enumerate(metric_names):
        y = y0 + 0.55 + r * row_h
        vals = values[name]
        x = x0
        for c in range(4):
            bg = NAVY_2 if r % 2 == 0 else NAVY_3
            rect(s, x, y, col_w[c], row_h, bg)
            if c == 0:
                text(s, x + 0.14, y + 0.2, col_w[c] - 0.2, 0.3, name, size=10.5, color=MUTED)
            else:
                v = vals[c - 1]
                txt = f"{v:,.2f}" if name == "耗时 (s)" else (f"{v:,.0f}" if name == "Token" else f"{v:.1f}%")
                col = WHITE if c < 3 else CYAN
                if name in ("最终误差 (%)",) and c == 3:
                    col = CYAN
                if name in ("最终误差 (%)",) and c < 3:
                    col = ORANGE
                if name == "验证通过率 (%)" and c == 3:
                    col = CYAN
                text(s, x, y + 0.18, col_w[c], 0.3, txt, size=11, color=col,
                     bold=(c == 3), align=PP_ALIGN.CENTER)
            x += col_w[c]
    rect(s, 1.05, 6.0, 11.35, 0.62, NAVY_2, CYAN_DIM, 0.8, MSO_SHAPE.ROUNDED_RECTANGLE, 0.07)
    text(s, 1.22, 6.09, 11.2, 0.46,
         "结论：不宣称「全面更优」——用可接受的时间与 Token 成本，换取误差 34.4% → 7.1%、验证通过率 0% → 100%，"
         "数据来自 data/benchmark.json，一条命令可复现。",
         size=10.2, color=WHITE)
    notes(s, "务必诚实——不要宣称「全面更优」。落点是「用可接受成本换取可验证的正确性」，这句话评委会认可。")


def slide_innovation(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "11 / 创新点", "五个工程取舍，让多智能体「可交付」", 12)
    cards = [
        ("01", "可解释调度成一等对象", "score / reason / agent_load 随事件落盘，可回放、可审计"),
        ("02", "验证与执行同级", "验证不是事后脚本，而是工作流节点与一等数据结构"),
        ("03", "带预算与收敛判据的自循环", "PDCA + 签名去重 + 无改善即停，拒绝无限重试"),
        ("04", "事件总线驱动的可观测性", "每条调度、验证、循环动作都可回放，SSE 断线续传"),
        ("05", "零依赖离线可复现", "纯 Python 标准库，评委断网也能一键跑完整演示"),
    ]
    x0, y0, cw, ch, gx, gy = 0.75, 1.95, 3.85, 1.85, 0.25, 0.22
    for i, (num, title, desc) in enumerate(cards):
        col, row = i % 3, i // 3
        x, y = x0 + col * (cw + gx), y0 + row * (ch + gy)
        rect(s, x, y, cw, ch, NAVY_2, RGBColor(0x2A, 0x3F, 0x5E), 0.9,
             MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
        text(s, x + 0.22, y + 0.18, 0.7, 0.4, num, size=20, color=CYAN_DIM, bold=True)
        text(s, x + 0.22, y + 0.66, cw - 0.44, 0.4, title, size=12.5, color=WHITE, bold=True)
        text(s, x + 0.22, y + 1.14, cw - 0.44, 0.6, desc, size=9.5, color=MUTED, spacing=1.15)
    notes(s, "不要逐条念，挑「可解释调度」和「可验证性」两条展开，其余一句带过。"
             "留一句：「细节在方案书第十节。」")


def slide_roadmap(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    header(s, "12 / 路线图", "下一步：更强验证、可扩展执行、真实科研数据", 13)
    cols = [
        ("近期（1–2 个月）", CYAN, ["更多验证器：时序一致性、因果图、数据泄漏检测", "接入真实公开数据集", "验证器插件化与配置化"]),
        ("中期（3–6 个月）", rgb("#4FB3E8"), ["分布式执行与 worker 资源池", "修复案例库与公开评测基准", "更多 MCP 科学工具"]),
        ("长期（6–12 个月）", ORANGE, ["多人审阅与审批流", "实验版本管理与对比", "面向科研团队的协作部署"]),
    ]
    x0, y0, cw = 0.8, 1.95, 3.85
    for i, (title, color, items) in enumerate(cols):
        x = x0 + i * (cw + 0.25)
        rect(s, x, y0, cw, 3.55, NAVY_2, color, 1.0, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
        rect(s, x, y0, cw, 0.06, color)
        text(s, x + 0.24, y0 + 0.22, cw - 0.48, 0.36, title, size=13, color=color, bold=True)
        yy = y0 + 0.78
        for it in items:
            text(s, x + 0.24, yy, cw - 0.45, 0.62, "▪ " + it, size=10.2, color=MUTED, spacing=1.2)
            yy += 0.82
    rect(s, 0.8, 5.75, 11.95, 1.15, NAVY_2, ORANGE, 0.9, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 1.0, 5.86, 11.5, 0.3, "当前边界（主动说明，不夸大）", size=11.5, color=ORANGE, bold=True)
    text(s, 1.0, 6.2, 11.5, 0.6,
         "单机部署 · 合成数据集 · 默认 Mock LLM；真实模型需自备 API Key，验证器目前以内置五层为主。",
         size=10.5, color=MUTED)
    notes(s, "主动说局限反而加分。语气要稳：「我们知道现在到哪，也知道下一步去哪。」")


def slide_end(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(s)
    rect(s, 0, 0, W, 0.06, CYAN)
    text(s, 0.9, 1.2, 8.0, 0.32, "THANK YOU", size=13, color=CYAN, bold=True)
    text(s, 0.86, 1.75, 11.3, 1.2, "让每一个多智能体结果，\n都经得起复算", size=40, color=WHITE,
         bold=True, spacing=1.2)
    text(s, 0.9, 3.65, 8.0, 0.5, "织流 AgentWeave：可编排 · 可解释 · 可验证", size=18, color=MUTED)
    rect(s, 0.9, 4.5, 5.4, 1.35, NAVY_2, CYAN_DIM, 0.9, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 1.1, 4.62, 5.0, 0.3, "一键复现", size=11.5, color=CYAN, bold=True)
    text(s, 1.1, 4.98, 5.0, 0.8, "python server.py\n或 python scripts/run_demo.py", size=11,
         color=WHITE, spacing=1.3)
    rect(s, 6.6, 4.5, 5.85, 1.35, NAVY_2, ORANGE, 0.9, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
    text(s, 6.8, 4.62, 5.4, 0.3, "代码仓库 / 扫码复现", size=11.5, color=ORANGE, bold=True)
    text(s, 6.8, 4.98, 4.15, 0.8, "https://github.com/zhkb06/agentweave\nREADME 内含一键复现步骤",
         size=10.5, color=WHITE, spacing=1.25)
    qr_path = DOCS / "qrcode_repo.png"
    if qr_path.exists():
        s.shapes.add_picture(str(qr_path), Inches(11.2), Inches(4.62), Inches(1.02), Inches(1.02))
    text(s, 0.9, 6.35, 11.4, 0.32,
         "织流 AgentWeave · 石家庄信息工程职业学院 · 张康博 · 指导教师：徐宇丰",
         size=11, color=DIM)
    footer(s, 14)
    notes(s, "收尾不要拖，留足提问时间。最后一句固定为「我们的主张是：可验证，才是可交付。」")


def main() -> int:
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    bench = load_bench()
    slide_cover(prs)
    slide_pain(prs)
    slide_overview(prs)
    slide_arch(prs)
    slide_agents(prs)
    slide_orchestrator(prs)
    slide_scheduler(prs)
    slide_verify(prs)
    slide_loop(prs, bench)
    slide_replay(prs)
    slide_bench(prs, bench)
    slide_innovation(prs)
    slide_roadmap(prs)
    slide_end(prs)
    DOCS.mkdir(parents=True, exist_ok=True)
    prs.save(str(OUT))
    print(f"PPT 已生成：{OUT}（{len(prs.slides.__iter__.__self__._sldIdLst)} 页，{OUT.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
