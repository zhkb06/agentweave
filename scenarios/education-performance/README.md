# 教学成效归因 · 学生成绩与学习行为因子归因

> 场景 ID：`education-performance` · 工作流 ID：`wf-education-performance` · 数据：`data.csv`（3000 行）

## 业务问题

分析期末成绩（final_score）与出勤率、作业完成度、自习时长、班级规模的关系，为教学干预提供证据。

**背景**：学校要设计下学期干预方案：抓出勤、抓作业还是增加自习？需要可解释、可复算的效应量，而不是经验判断。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `attendance` | 0.45 | 85.0 | 8.0 |
| `homework` | 0.3 | 75.0 | 12.0 |
| `study_hours` | 1.6 | 8.0 | 2.5 |
| `class_size` | -0.12 | 40.0 | 8.0 |
| `final_score` | — | 因变量：40.0 + Σ 系数×因子 + N(0, 5.0) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario education-performance
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario education-performance --json
```

本次生成时的 OLS 恢复结果：R² = 0.6443，系数 {"attendance": 0.4563, "homework": 0.3086, "study_hours": 1.5782, "class_size": -0.1239}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
