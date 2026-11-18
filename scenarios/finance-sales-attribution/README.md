# 零售销售归因 · 月度销售额与营销因子归因

> 场景 ID：`finance-sales-attribution` · 工作流 ID：`wf-finance-sales-attribution` · 数据：`data.csv`（2000 行）

## 业务问题

分析月度销售额（revenue）与广告投放、折扣力度、季节性指数之间的关系，估计因子效应并验证结论。

**背景**：运营团队认为「加大广告投放一定带来同比例销售增长」，需要用数据验证真实弹性，并避免把季节性误判为广告效果。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `ad_spend` | 2.4 | 25.0 | 6.0 |
| `discount` | 0.8 | 15.0 | 5.0 |
| `season` | 9.0 | 0.5 | 0.3 |
| `revenue` | — | 因变量：120.0 + Σ 系数×因子 + N(0, 6.0) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario finance-sales-attribution
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario finance-sales-attribution --json
```

本次生成时的 OLS 恢复结果：R² = 0.8606，系数 {"ad_spend": 2.4048, "discount": 0.7944, "season": 9.7035}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
