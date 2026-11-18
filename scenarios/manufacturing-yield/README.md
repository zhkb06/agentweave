# 制造良品率归因 · 产线良品率与工艺参数归因

> 场景 ID：`manufacturing-yield` · 工作流 ID：`wf-manufacturing-yield` · 数据：`data.csv`（3000 行）

## 业务问题

分析产线良品率（yield_pct）与温度、压力、线速、湿度的关系，定位影响良品率的关键工艺窗口。

**背景**：质量部门连续三周看到良品率波动，但工艺参数每天都在小幅调整，无法判断是哪个参数在起作用，需要可验证的效应排序。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `temp` | -0.5 | 22.0 | 3.0 |
| `pressure` | 2.5 | 3.0 | 0.4 |
| `line_speed` | -1.2 | 12.0 | 2.0 |
| `humidity` | -0.12 | 45.0 | 8.0 |
| `yield_pct` | — | 因变量：98.0 + Σ 系数×因子 + N(0, 1.8) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario manufacturing-yield
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario manufacturing-yield --json
```

本次生成时的 OLS 恢复结果：R² = 0.7557，系数 {"temp": -0.5027, "pressure": 2.546, "line_speed": -1.1923, "humidity": -0.1242}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
