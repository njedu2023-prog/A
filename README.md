# a_share_t1_engine

中国 A 股 T+1 Maya 系统。本机运行，PDF 为主数据源，同花顺截图/导出数据只作为数据增强层补充。

## 当前形态

Maya v1.0 包含三套并行系统：

1. `连板概率`：从 PDF 三组名单中计算固定名单连板概率 Top4。
2. `隔夜单 / EV单`：评估买贵风险、隔夜适配度和 EV 排序。
3. `最终承接`：评估 T 买入到 T+1 卖出的可兑现承接排序。

系统输出为本机 HTML 报告包：`首页`、`综合看板`、`连板概率`、`隔夜单 / EV单`、`最终承接`、`验证复盘`。

## 数据源原则

- PDF 是主数据源：负责确定股票池、三组名单、日期线、题材和基础模型输入。
- 同花顺数据是补充源：只补齐截图或导出里能明确读到的字段，不新增候选股票池，不替代 PDF。
- 验证数据是独立真值：T+1 卖出/验证日收盘后，再用真实行情回填命中率和收益统计。

## 安装

```bash
python -m pip install -e ".[test]"
```

## 运行

生成三系统报告包：

```bash
a-share-t1-engine path/to/report.pdf --format pack
```

带同花顺补充数据：

```bash
a-share-t1-engine path/to/report.pdf --ths-data data/ths_supplement.yaml --format pack
```

启动本机网页和上传入口：

```bash
a-share-t1-web --open-browser
```

默认访问：

```text
http://127.0.0.1:8765/
```

macOS 本机启动器：

```text
launchers/A股T1预测引擎.app
launchers/A股T1预测引擎.command
```

## 输出

- `outputs/html_reports/latest.html`：最新报告首页。
- `outputs/html_reports/latest_dashboard.html`：综合看板。
- `outputs/html_reports/latest_limit.html`：连板概率。
- `outputs/html_reports/latest_overnight.html`：隔夜单 / EV单。
- `outputs/html_reports/latest_continuation.html`：最终承接。
- `outputs/html_reports/latest_validation.html`：验证复盘。
- `outputs/predictions/latest.json`：最新机器可读预测快照。

## 同花顺补充数据

支持 CSV、YAML、JSON。可补充字段包括：

- 首次涨停时间、最终涨停时间。
- 封板质量、封成比、涨停封单额、换手率。
- 涨停原因、所属行业、主力净流入。
- 竞价涨幅、竞价成交额、竞价封单、开盘 5 分钟成交额。

示例：

```yaml
stocks:
  - code: "600228"
    name: "返利科技"
    turnover_rate_pct: 0.14
    max_seal_order_yi: 1.73
    order_to_turnover_pct: 27.02
    first_limit_up_time: "09:30:00"
    final_limit_up_time: "09:30:00"
    theme: "控制权变更+复牌+摘帽+在线导购"
    industry: "文化传媒"
```

## 验证复盘

预测快照生成后，到 T+1 验证日再回填真值：

```bash
a-share-t1-validate --prediction outputs/predictions/latest.json --actuals actuals.yaml
```

`actuals.yaml` 示例：

```yaml
600228: true
603698: false
```

验证输出：

- `outputs/validation/latest.html`
- `outputs/validation/history.csv`

## 基础概率校准

当前基础概率是冷启动先验，不是行业权威共识。样本累计后可校准：

```bash
a-share-t1-calibrate --history outputs/validation/history.csv --output outputs/calibration/base_probabilities.yaml
```

## 测试

```bash
pytest
```

## 约束

`src/a_share_t1_engine/scoring.py` 不调用 LLM。LLM 只能用于解析、摘要、舆情标签生成，不能直接决定概率。

重要提示：本系统输出内容仅为量化研究与信息整理结果，不构成投资建议或任何形式的买卖依据。模型概率、排序、评分及结论均不代表未来收益保证，任何基于本系统内容作出的投资决策，均由使用者独立判断并自行承担风险。
