# 用户流失归因 · 订阅用户流失与满意度、投诉因子归因

> 场景 ID：`marketing-churn` · 工作流 ID：`wf-marketing-churn` · 数据：`data.csv`（3000 行）

## 业务问题

分析月度流失率（churn_pct）与满意度、投诉量、在网时长、价格指数的关系，识别留存干预的抓手。

**背景**：留存团队面对预算约束：降价、补贴客服还是做满意度项目？需要先确认哪个因子对流失的边际贡献最大。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `satisfaction` | -0.25 | 70.0 | 12.0 |
| `complaints` | 0.08 | 30.0 | 15.0 |
| `tenure` | -0.15 | 24.0 | 10.0 |
| `price_index` | 0.12 | 100.0 | 10.0 |
| `churn_pct` | — | 因变量：35.0 + Σ 系数×因子 + N(0, 2.2) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario marketing-churn
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario marketing-churn --json
```

本次生成时的 OLS 恢复结果：R² = 0.7561，系数 {"satisfaction": -0.2551, "complaints": 0.0777, "tenure": -0.1504, "price_index": 0.125}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
