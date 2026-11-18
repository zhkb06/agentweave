# 工业能耗优化 · 车间能耗与产量、环境因子归因

> 场景 ID：`energy-consumption` · 工作流 ID：`wf-energy-consumption` · 数据：`data.csv`（3000 行）

## 业务问题

分析车间单位能耗（energy_kwh）与产量、气温、夜班比例、设备役龄的关系，识别节能改造优先级。

**背景**：工厂要在年度预算里选择改造项目：换设备、调班次还是做保温？需要先量化每个因子的能耗贡献，避免拍脑袋。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `output` | 1.8 | 60.0 | 15.0 |
| `temp` | 1.1 | 18.0 | 8.0 |
| `night_shift` | 18.0 | 0.35 | 0.48 |
| `equipment_age` | 2.2 | 6.0 | 3.0 |
| `energy_kwh` | — | 因变量：120.0 + Σ 系数×因子 + N(0, 12.0) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario energy-consumption
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario energy-consumption --json
```

本次生成时的 OLS 恢复结果：R² = 0.8673，系数 {"output": 1.8224, "temp": 1.1167, "night_shift": 18.5287, "equipment_age": 2.0446}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
