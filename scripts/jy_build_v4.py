"""Build the AgentWeave demo video (v4) in JianYing via the jianying-editor skill.

v4 changes vs v3:
  * 9 scenes (~1450 chars of narration) so the cut lands in the 3:40-4:10 window
  * working 叠化 transitions (segment.add_transition takes the enum, not a Transition object)
  * no cloud BGM (the cloud URL returns 403; narration-only keeps the mix clean)
  * 2.0s tail hold per scene
"""

import os
import sys

SKILL = r"C:\Users\76540\.dsh\skills\jianying-editor"
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from jy_wrapper import JyProject  # noqa: E402
from pyJianYingDraft.metadata import TransitionType  # noqa: E402
from pyJianYingDraft import ClipSettings  # noqa: E402

ROOT = r"D:\76540\Desktop\2026第五届未来杯"
ASSETS = os.path.join(ROOT, "docs", "视频素材")
CARD = os.path.join(ASSETS, "卡片")
SLIDE = os.path.join(ASSETS, "幻灯片")
DRAFTS = r"D:\JianyingPro Drafts"
DRAFT_NAME = "AgentWeave-演示视频-v4"
SPEAKER = "zh_male_huoli"
TAIL_US = 2_000_000
TITLE_DUR_US = 3_000_000


def a(name):
    return os.path.join(CARD, name)


def s(num):
    return os.path.join(SLIDE, f"slide-{num:02d}.png")


def v(name):
    return os.path.join(ASSETS, name)


SCENES = [
    {
        "title": "01 · 一个问题",
        "narration": "多智能体不是把几个 Agent 串起来。真正的难题是，七个 Agent 分工协作，"
                     "最后交出一份报告，你凭什么相信它是对的？演示里看起来很聪明的系统，"
                     "一放到真实业务里，最先被问住的就是这句话。我们做了一个平台，"
                     "不是让 Agent 干更多事，而是让 Agent 干的事，可以被验收。",
        "visuals": [
            (0.38, a("钩子A-七个Agent.png"), 0, None),
            (0.34, a("钩子B-怎么证明算对了.png"), 0, None),
            (0.28, s(1), 0, None),
        ],
    },
    {
        "title": "02 · 四道工程鸿沟",
        "narration": "今天的 Agent 项目，落地时普遍卡在四道沟里。第一，流程硬编码，换个任务就要改代码，"
                     "复用率低。第二，调度是黑盒，没人说得清为什么选这个 Agent、为什么不用那个。"
                     "第三，结果不可验证，模型说已经完成，可数字对不对、结论站不站得住，没人知道。"
                     "第四，失败没有反馈闭环，错了就重来一次，下一次还是错。这四道沟，我们逐条去填。",
        "visuals": [
            (0.50, s(2), 0, None),
            (0.50, s(3), 0, None),
        ],
    },
    {
        "title": "03 · 把编排做成契约",
        "narration": "第一步，把编排变成一份可执行的契约。左边是七个 Agent，每个都有明确的岗位、"
                     "可用工具和输出字段。中间是 DAG 画布，连线就是数据依赖，谁先谁后一目了然。"
                     "点开任意一个节点，能看到它的 Agent、工具、输入输出映射和验证规约。"
                     "整套流程会被保存成一份 JSON，可以进版本库，可以代码评审，也可以一键复现。",
        "visuals": [
            (0.24, v("01-仪表盘.mp4"), 0.5, 6.0),
            (0.48, v("02-编排台与节点详情.mp4"), 3.0, 14.0),
            (0.28, v("04-资源中心.mp4"), 2.0, 8.0),
        ],
    },
    {
        "title": "04 · 调度可解释",
        "narration": "第二步，让调度可解释。运行起来，你看到的不只是一个转圈动画。"
                     "时间线上，每个节点谁在执行、什么时候开始、花了多久，都是实时推过来的。"
                     "调度器每一次决策都会留下分数：能力匹配、当前负载、历史成功率，三项加权。"
                     "所以为什么选它、为什么不选别人，可以查、可以复盘，而不是玄学。",
        "visuals": [
            (1.0, v("03-运行监控与实时事件.mp4"), 3.0, 24.0),
        ],
    },
    {
        "title": "05 · 验证与自循环",
        "narration": "第三步，也是最关键的一步，让结果可验证。同一个任务，第一轮失败，置信度只有百分之四十二点三。"
                     "系统没有简单重试，而是把失败定位到具体的检查项，调整结构之后重新执行。"
                     "第二轮，十三项检查全部通过，置信度百分之百。在基准测试里，系数误差从百分之三十四点四"
                     "降到百分之七点一，验证通过率从零变成百分之百。这就是自循环的价值。",
        "visuals": [
            (0.32, s(9), 0, None),
            (0.34, v("03-运行监控与实时事件.mp4"), 17.0, 10.0),
            (0.34, v("03-运行监控与实时事件.mp4"), 27.0, 8.0),
        ],
    },
    {
        "title": "06 · 六层架构",
        "narration": "支撑这些的，是一套六层架构。最底下是契约与数据，往上是事件总线与表达式求值，"
                     "再往上是 Agent 运行时与工具沙箱，然后是引擎、调度与验证自循环，"
                     "再往外是接口层，最上面是原生控制台。有一条铁律：工具只能通过运行时上下文访问，"
                     "模型可以换，状态不动，任务可以重跑。",
        "visuals": [
            (0.52, s(4), 0, None),
            (0.48, s(5), 0, None),
        ],
    },
    {
        "title": "07 · 工程可信度",
        "narration": "工程上，我们不靠演示碰运气。二百零六个自动化用例，十个行业场景的批量回归，"
                     "十三类验证检查器，全部可以一条命令跑完。契约先行，测试即验收，文档与实现同源，"
                     "换一台机器、换一个模型，结果可以复现。对我们来说，可复现本身就是一种能力。",
        "visuals": [
            (0.50, s(6), 0, None),
            (0.50, s(12), 0, None),
        ],
    },
    {
        "title": "08 · 五层验证漏斗",
        "narration": "验证不是打个分，而是一套漏斗。第一层结构合规，第二层数值真值比对，"
                     "第三层代码复算，第四层跨 Agent 交叉复核，最后一层统计稳健与 LLM 评审。"
                     "任何一层不通过，都会带着问题回到修复节点。所以报告里的每一个数字，"
                     "都能指回它的检查项和原始证据。",
        "visuals": [
            (0.50, s(7), 0, None),
            (0.50, s(8), 0, None),
        ],
    },
    {
        "title": "09 · 一句话到可验证报告",
        "narration": "从一句话任务，到一份带图表和指标的可验证报告，中间只隔着一个平台。"
                     "我们把它开源了，扫码就能复现，十分钟跑通一个完整闭环。"
                     "织流 AgentWeave，可编排，可解释，可验证。可验证，才是可交付。",
        "visuals": [
            (0.30, s(10), 0, None),
            (0.30, s(11), 0, None),
            (0.40, a("片尾卡-队伍与二维码.png"), 0, None),
        ],
    },
]


def sec(us):
    return f"{us / 1_000_000:.3f}s"


def plan_durations(span_us, visuals):
    weights = [w for w, *_ in visuals]
    total_w = sum(weights)
    caps = [float("inf") if cap is None else cap * 1_000_000 for _, _, _, cap in visuals]
    durations = [span_us * w / total_w for w in weights]
    overflow = 0.0
    for i, cap in enumerate(caps):
        if durations[i] > cap:
            overflow += durations[i] - cap
            durations[i] = cap
    if overflow > 0:
        flex = [i for i, c in enumerate(caps) if c == float("inf")]
        if flex:
            per = overflow / len(flex)
            for i in flex:
                durations[i] += per
        else:
            durations[-1] += overflow
    return [int(d) for d in durations]


def main():
    p = JyProject(DRAFT_NAME, width=1920, height=1080, drafts_root=DRAFTS)
    cursor = 0

    for scene in SCENES:
        start = cursor
        next_cursor = p.add_narrated_subtitles(
            scene["narration"], speaker=SPEAKER, start_time=start, track_name="Subtitles"
        )
        span = max(next_cursor - start, 1_000_000) + TAIL_US
        durations = plan_durations(span, scene["visuals"])

        segs, t = [], start
        for (weight, path, src_start, cap), dur in zip(scene["visuals"], durations):
            seg = p.add_media_safe(
                path, start_time=t, duration=sec(dur),
                track_name="VideoTrack", source_start=src_start,
            )
            segs.append(seg)
            if seg is not None:
                t = seg.target_timerange.start + seg.target_timerange.duration

        scene_end = start + span
        if scene_end - t > 100_000:
            _, last_path, _, last_cap = scene["visuals"][-1]
            if last_cap is None:
                filler = p.add_media_safe(
                    last_path, start_time=t, duration=sec(scene_end - t),
                    track_name="VideoTrack",
                )
                if filler is not None:
                    segs.append(filler)

        try:
            p.add_text_simple(
                scene["title"], start_time=start + 150_000, duration=sec(TITLE_DUR_US),
                track_name="Titles", font_size=5.0, color_rgb=(0.62, 0.85, 0.93),
                clip_settings=ClipSettings(transform_x=-0.78, transform_y=0.86), anim_in="渐显",
            )
        except Exception as exc:
            print(f"[warn] title failed: {exc}")

        trans_type = TransitionType.from_name("叠化")
        for seg in segs[:-1]:
            if seg is None:
                continue
            try:
                seg.add_transition(trans_type, duration=500_000)
            except Exception as exc:
                print(f"[warn] transition failed: {exc}")

        cursor = max(start + span, t)
        print(f"[scene] {scene['title']} {sec(start)} -> {sec(cursor)}")

    print(f"[total] {sec(cursor)}")

    p.save()
    print("DRAFT:", p.draft_dir)


if __name__ == "__main__":
    main()
