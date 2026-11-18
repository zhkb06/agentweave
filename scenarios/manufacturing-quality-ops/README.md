# 制造质量-能耗联合分析 · 产线良品率与单位能耗的多目标联合归因

> 场景 ID：`manufacturing-quality-ops` · 工作流 ID：`wf-manufacturing-quality-ops` · 数据：`data.csv`（3000 行）
> 多目标联合分析：目标数 **2**，目标列 `yield_pct、energy_kwh`

## 业务问题

同一批产线样本同时观测良品率（yield_pct）与单位能耗（energy_kwh），联合估计工艺参数对两个目标的影响，识别「提质量」与「降能耗」的共同抓手与冲突项。

**背景**：工艺部门要同时守住良品率与能耗指标，但直觉上「提压力、降线速」对质量有利，却可能推高能耗。需要联合分析来识别真正的双赢参数与冲突参数。

## 数据字典

| 字段 | 生成均值 | 生成标准差 |
|---|---|---|
| `temp` | 22.0 | 3.0 |\n| `pressure` | 3.0 | 0.4 |\n| `line_speed` | 12.0 | 2.0 |\n| `humidity` | 45.0 | 8.0 |\n| `output` | 100.0 | 15.0 |

| 目标列 | 截距 | 真值系数 | OLS 恢复 R² |
|---|---|---|---|
| `yield_pct` | 98.0 | temp=-0.45、pressure=2.2、line_speed=-1.1、humidity=-0.1、output=0.02 | 0.7218 |
| `energy_kwh` | 60.0 | temp=1.6、pressure=3.5、line_speed=2.2、humidity=0.4、output=0.9 | 0.6933 |

> 真值仅保存在 `truth.json`（扁平键 `<目标>.<因子>`），平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario manufacturing-quality-ops
```

预期：16 个节点执行完成；第 1 轮验证失败（各目标系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；联合对比节点给出共同抓手与冲突因子。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → joint（联合对比）→ critique → gate → report → end`

- 验证为五层：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`、`schema`；
- 多目标指标以 `<目标>.<因子>` 扁平键比对，目标数量不限；
- 首轮/修复轮偏差分别由 `scenario.json` 的 `first_bias` / `second_bias` 控制。

## 本次生成的自检结果

R²（各目标最小值）= 0.6933；
扁平系数键 10 个：
```json
{
  "yield_pct.temp": -0.4448,
  "yield_pct.pressure": 2.2624,
  "yield_pct.line_speed": -1.1159,
  "yield_pct.humidity": -0.1002,
  "yield_pct.output": 0.0222,
  "energy_kwh.temp": 1.5727,
  "energy_kwh.pressure": 3.2909,
  "energy_kwh.line_speed": 2.2987,
  "energy_kwh.humidity": 0.3987,
  "energy_kwh.output": 0.8854
}
```
