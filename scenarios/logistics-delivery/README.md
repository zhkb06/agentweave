# 物流时效归因 · 订单履约时长与距离、负载因子归因

> 场景 ID：`logistics-delivery` · 工作流 ID：`wf-logistics-delivery` · 数据：`data.csv`（3000 行）

## 业务问题

分析订单履约时长（delivery_min）与配送距离、订单量、恶劣天气、仓内负载的关系，支撑时效承诺与资源调度。

**背景**：平台承诺「次日达」，但履约时长波动大；运营需要知道哪些因子可控（仓内负载 vs 天气），以及距离的边际成本。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `distance_km` | 1.5 | 8.0 | 3.0 |
| `order_volume` | 0.08 | 120.0 | 40.0 |
| `bad_weather` | 9.0 | 0.2 | 0.4 |
| `warehouse_load` | 0.09 | 70.0 | 20.0 |
| `delivery_min` | — | 因变量：15.0 + Σ 系数×因子 + N(0, 4.0) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario logistics-delivery
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario logistics-delivery --json
```

本次生成时的 OLS 恢复结果：R² = 0.709，系数 {"distance_km": 1.5127, "order_volume": 0.0788, "bad_weather": 9.1105, "warehouse_load": 0.0834}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
