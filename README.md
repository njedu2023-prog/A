# a_share_t1_engine

中国 A 股 T+1 连板概率模型工程化项目。

## 能力范围

- 读取每日 T+1 系统分析 PDF。
- 解析 `Top 10`、`Top-Decision`、`Premium` 三组固定名单。
- 自动输出连板概率最高的 4 只股票。
- 识别重叠股票，只标记来源，不重复计分、不额外加权。
- 解析封板质量、封单占成交、最高封单、涨停成交额、换手率、连板高度、题材、行业。
- 解析热门板块资金轮动，生成 `BRS`。
- 将个股题材映射到板块资金轮动，生成 `TSS`。
- 接入敏感舆情与公告标签。
- 支持外部搜索或 LLM 摘要后生成的舆情事件文件，作为模型输入合并。
- 固定模型版本：`CN-A-T1-ESE v1.1 / CP-TPS v1.1`。
- 使用 YAML 保存全部模型参数。
- 使用 pytest golden snapshot 保证同输入、同配置输出完全一致。

## 安装

```bash
python -m pip install -e ".[test]"
```

## 使用

```bash
a-share-t1-engine run path/to/report.pdf --output output.md
```

带外部舆情事件输入：

```bash
a-share-t1-engine run path/to/report.pdf --events events.yaml --output output.md
```

`events.yaml` 示例：

```yaml
600001:
  - order
600002:
  - share_reduction
```

也可以直接调用：

```bash
python -m a_share_t1_engine run path/to/report.pdf
```

## 测试

```bash
pytest
```

## 约束

`src/a_share_t1_engine/scoring.py` 不调用 LLM。LLM 只能出现在上游解析、摘要或舆情标签生成流程中，不能直接决定概率。
