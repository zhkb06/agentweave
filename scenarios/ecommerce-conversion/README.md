# 电商转化归因 · 页面转化率与体验因子归因

> 场景 ID：`ecommerce-conversion` · 工作流 ID：`wf-ecommerce-conversion` · 数据：`data.csv`（2000 行）

## 业务问题

分析页面转化率（conversion）与页面加载速度、个性化推荐强度、购物车步骤数的关系，量化体验改版收益。

**背景**：增长团队要决定下一季度把预算投在「提速」「个性化」还是「简化购物车流程」，需要一个可验证的效应排序。

## 数据字典

| 字段 | 真值系数 | 生成均值 | 生成标准差 |
|---|---|---|---|
| `page_speed` | 0.06 | 72.0 | 12.0 |
| `personalization` | 0.9 | 0.55 | 0.22 |
| `cart_steps` | -0.4 | 3.2 | 0.9 |
| `conversion` | — | 因变量：1.8 + Σ 系数×因子 + N(0, 0.55) | — |

> 真值仅保存在 `truth.json`，平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario ecommerce-conversion
```

预期：15 个节点执行完成；第 1 轮验证失败（系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；报告写入 `data/runs/<run_id>/report.md`。

## 离线自检

```bash
py -3.14 scripts/run_scenario.py --scenario ecommerce-conversion --json
```

本次生成时的 OLS 恢复结果：R² = 0.7008，系数 {"page_speed": 0.0603, "personalization": 0.9545, "cart_steps": -0.4247}。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → critique → gate → report → end`

- `verify` 五层检查：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`；
- 容差 15%，置信度阈值 0.7，`n ≥ 1000` 且 `R² ≥ 0.55`；
- 首轮偏差由 `scenario.json` 的 `first_bias` 控制，修复轮偏差由 `second_bias` 控制。
