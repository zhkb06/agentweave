# 多应用场景演示

> 同一套 AgentWeave 引擎，换数据与工作流即可复用到不同行业。以下场景均可离线运行。

| # | 场景 | 目录 | 工作流 | 一键运行 |
|---|---|---|---|---|
| 1 | 零售销售归因 | `scenarios/finance-sales-attribution/` | `wf-finance-sales-attribution` | `py -3.14 scripts/run_scenario.py --scenario finance-sales-attribution` |
| 2 | 公共卫生风险归因 | `scenarios/public-health-risk/` | `wf-public-health-risk` | `py -3.14 scripts/run_scenario.py --scenario public-health-risk` |
| 3 | 电商转化归因 | `scenarios/ecommerce-conversion/` | `wf-ecommerce-conversion` | `py -3.14 scripts/run_scenario.py --scenario ecommerce-conversion` |
| 4 | 制造良品率归因 | `scenarios/manufacturing-yield/` | `wf-manufacturing-yield` | `py -3.14 scripts/run_scenario.py --scenario manufacturing-yield` |
| 5 | 工业能耗优化 | `scenarios/energy-consumption/` | `wf-energy-consumption` | `py -3.14 scripts/run_scenario.py --scenario energy-consumption` |
| 6 | 物流时效归因 | `scenarios/logistics-delivery/` | `wf-logistics-delivery` | `py -3.14 scripts/run_scenario.py --scenario logistics-delivery` |
| 7 | 教学成效归因 | `scenarios/education-performance/` | `wf-education-performance` | `py -3.14 scripts/run_scenario.py --scenario education-performance` |
| 8 | 用户流失归因 | `scenarios/marketing-churn/` | `wf-marketing-churn` | `py -3.14 scripts/run_scenario.py --scenario marketing-churn` |

## 共同点

- 每个场景都是 15 节点工作流：并行取数/调研 → 数据工程 → 分层画像 → 统计建模 → 五层验证 → PDCA 自循环 → 稳健性/敏感性 → 评审 → 报告；
- 每个场景的合成数据都内置真实系数与首轮偏差，因此离线可复现「首轮失败 → 反馈修复 → 第二轮通过」；
- 真值只在 `truth.json`，由平台注入验证器；分析 Agent 的上下文里没有真值；
- 换场景只换数据与工作流 JSON，引擎、调度、验证、Web 控制台均不改。

## Web 控制台

`py -3.14 server.py` 启动后，编排台下拉框会列出全部场景工作流，可直接选择运行。
