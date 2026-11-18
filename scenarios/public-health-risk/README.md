# 公共卫生风险归因 · 呼吸道发病率与环境、免疫因子归因

> 场景 ID：`public-health-risk` · 工作流 ID：`wf-public-health-risk` · 数据：`data.csv`（2000 行）

## 业务问题

分析地区呼吸道发病率（incidence）与气温、湿度、疫苗接种率、PM2.5 的关系，识别可干预因子并验证结论。

**背景**：疾控部门需要在季节性高发前判断：发病率上升主要是环境驱动还是免疫屏障不足，从而决定资源投向。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `temp` | -0.6 | 16.0 | 8.0 |
| `humidity` | 0.35 | 58.0 | 14.0 |
| `vaccination_rate` | -15.0 | 0.7 | 0.18 |
| `pm25` | 0.2 | 42.0 | 18.0 |
| `incidence` | — | 因变量：45.0 + Σ 系数×因子 + N(0, 3.1) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario public-health-risk
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario public-health-risk --json
```

本次生成时的 OLS 恢复结果：R² = 0.8702，系数 {"temp": -0.605, "humidity": 0.3574, "vaccination_rate": -14.818, "pm25": 0.2022}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
