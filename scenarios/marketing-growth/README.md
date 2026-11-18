# 营销增长三目标联合分析 · 用户流失、生命周期价值与获客成本的多目标归因

> 场景 ID：`marketing-growth` · 工作流 ID：`wf-marketing-growth` · 数据：`data.csv`（3000 行）
> 多目标联合分析：目标数 **3**，目标列 `churn_pct、ltv、cac`

## 业务问题

同一批用户样本同时观测流失率（churn_pct）、生命周期价值（ltv）与获客成本（cac），联合估计满意度、投诉、在网时长、价格指数、广告投入对三个目标的影响，支持预算联合优化。

**背景**：增长团队只有一笔预算：降价、投广告、补客服还是做满意度项目？三个目标（留存、LTV、CAC）往往互相牵制，需要联合分析而不是逐个拍脑袋。

## 数据字典

| 字段 | 生成均值 | 生成标准差 |
|---|---|---|
| `satisfaction` | 70.0 | 12.0 |\n| `complaints` | 30.0 | 15.0 |\n| `tenure` | 24.0 | 10.0 |\n| `price_index` | 100.0 | 10.0 |\n| `ad_spend` | 25.0 | 6.0 |

| 目标列 | 截距 | 真值系数 | OLS 恢复 R² |
|---|---|---|---|
| `churn_pct` | 34.0 | satisfaction=-0.22、complaints=0.07、tenure=-0.14、price_index=0.1、ad_spend=0.02 | 0.6832 |
| `ltv` | 120.0 | satisfaction=0.8、complaints=-0.3、tenure=2.2、price_index=-0.1、ad_spend=0.15 | 0.8983 |
| `cac` | 50.0 | satisfaction=-0.4、complaints=0.05、tenure=-0.5、price_index=0.2、ad_spend=0.9 | 0.7556 |

> 真值仅保存在 `truth.json`（扁平键 `<目标>.<因子>`），平台只把它注入验证器；分析 Agent 看不到真值。

## 一键运行

```bash
py -3.14 scripts/run_scenario.py --scenario marketing-growth
```

预期：16 个节点执行完成；第 1 轮验证失败（各目标系数偏差约 30%，超出 15% 容差）；
PDCA 自循环注入反馈后第 2 轮通过（偏差 ≤5%）；联合对比节点给出共同抓手与冲突因子。

## 工作流结构

`start → plan → scan/research（并行）→ clean → segment → repair[analyze → verify]（PDCA 自循环）→ robustness ∥ sensitivity → joint（联合对比）→ critique → gate → report → end`

- 验证为五层：`code_rerun`、`numeric_ground_truth`、`statistical`、`cross_agent`、`schema`；
- 多目标指标以 `<目标>.<因子>` 扁平键比对，目标数量不限；
- 首轮/修复轮偏差分别由 `scenario.json` 的 `first_bias` / `second_bias` 控制。

## 本次生成的自检结果

R²（各目标最小值）= 0.6832；
扁平系数键 15 个：
```json
{
  "churn_pct.satisfaction": -0.2204,
  "churn_pct.complaints": 0.0696,
  "churn_pct.tenure": -0.1361,
  "churn_pct.price_index": 0.099,
  "churn_pct.ad_spend": 0.0245,
  "ltv.satisfaction": 0.8076,
  "ltv.complaints": -0.2938,
  "ltv.tenure": 2.193,
  "ltv.price_index": -0.1128,
  "ltv.ad_spend": 0.1333,
  "cac.satisfaction": -0.3991,
  "cac.complaints": 0.051,
  "cac.tenure": -0.4981,
  "cac.price_index": 0.205,
  "cac.ad_spend": 0.8605
}
```
