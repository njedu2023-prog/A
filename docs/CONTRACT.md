# 输入与执行契约

## 三表成员关系

`a_top10` 与 `premium_top10` 读取各自正式Top10文件；Decision读取 `decision.html` 主表实际按 `report_index.json.reports[0].action_url` 渲染的 `action_plan.stage_watchlist`。Decision 的页面排名必须使用 `stage_watch_rank`；同一行的 `rank` 只是全量 `candidates` 池排名，不得用于三表成员关系。每张表最多取实际展示的前10名，不足10名时按实际数量，绝不从全量候选池补票。求交集前统一证券代码、要求显式正整数连续页面排名，并检查重复代码与重复排名。Premium的 `WATCH/EXCLUDED` 和Decision的 `REJECT/SHADOW_ONLY/BUY` 只作为特征，不改变“是否出现在主表前10名”。

每次读取先分别解析 `a-top10/main` 与 `top10-decision/main` 的完整远端 commit SHA，再用该 SHA 读取同一仓库内的指针、日期文件和索引。禁止在同一次快照中直接读取可变的 `raw/.../main/...`。a-top10 的指针 `run_id/commit_sha` 必须与日期CSV一致；Premium指针必须明确 `ok=True`；Decision索引的 `latest_report_date`、`reports[0].report_date` 与行动文件 `report_date` 必须一致。

Premium CSV 若出现重复表头，仅在每行重复值一致（数值等价也视为一致）或仅一侧非空时安全合并，并记录源质量警告；任一行存在冲突值则阻断。Decision 的 `stage_watch_display_limit` 必须为10；`stage_watch_count` 必须等于 `stage_watchlist` 行数，并等于 `min(stage_watch_eligible_count, 10)`。每个主表成员还必须能按代码及原始 `rank` 唯一映射回 `candidates`，且 `observation_rank` 与 `stage_watch_rank` 一致；任何不一致均阻断输入，绝不回退全量池。`decision_p_fill/decision_e_ret/decision_ev/decision_cost/decision_risk_penalty` 是辅助数值特征：缺失时记录覆盖率警告但保留该实际展示成员；非空值仍必须是有限数，且概率、成本和风险惩罚必须满足合法范围。

严格交集：

```text
intersection = codes(a_top10) ∩ codes(premium_top10) ∩ codes(decision_table)
```

交集为零是合法结果。交集有几支就保留几支，每一支都进入独立影子账本并接受同一套9:25买入与T+1退出验证。固定第1/2/3名仍作为三个长期独立序列分别统计，不会限制第4名及以后进入影子验证，也不会发生名次补位。

## 日期链

```text
a.trade_date = premium.trade_date = premium.base_date = decision.signal_date = D
a.verify_date = premium.buy_date = decision.exec_date = T
premium.target_date = decision.exit_date = T+1
```

三个上游产物先交叉验证，再由版本化的 `data/trading_calendar_2026.json` 验证严格的交易日后继关系。该日历按上海证券交易所《关于上海证券交易所2026年部分节假日休市安排的通知》固化：周六、周日及通知列明的休市日均不可作为 D/T/T+1。日历缺失、超出2026年、日期非法或并非严格下一交易日时，一律 `INPUT_BLOCKED`。

同一D日首次冻结不得早于北京时间20:00；20:00整允许冻结。已有冻结信号不受该时间门禁影响，仍可结算并只读比较最新源表。冻结后若源表内容哈希或固定仓库commit发生变化，只报告 `SOURCE_REVISION_AFTER_FREEZE`，不得重写原排名。

官方日历依据：[上交所2026年休市安排](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)。

## 9:25买入真值

集合竞价申报存在价格与时间队列。`data/execution_truth.v1.json` 的每个键为 `YYYYMMDD:000000.SH`，每条值必须是 `auction_execution_v2`，至少包含：

```json
{
  "schema_version": "auction_execution_v2",
  "trade_date": "20260805",
  "ts_code": "000000.SZ",
  "event_at": "2026-08-05T09:25:00+08:00",
  "phase": "OPENING_CALL_AUCTION",
  "source": "BROKER_EXECUTION",
  "data_tier": "BROKER_LOG",
  "label_quality": "ACTUAL",
  "quantity_unit": "SHARES",
  "submitted_qty": 1000,
  "filled_qty": 1000,
  "limit_price": 10.00,
  "price": 10.00,
  "auction_matched_qty": 200000,
  "price_limit_source": "BROKER_ORDER_RECORD"
}
```

`ACTUAL` 只接受券商成交日志。`REPLAY` 和 `CONSERVATIVE` 若声称正成交，必须使用 `ORDERBOOK` 证据并额外提供 `queue_ahead_qty` 与 `executable_qty_at_order`，证明竞价同价队列确实轮到该申报。参与率、整手、资金上限、最小价位、限价和成交数量关系均会拒绝式校验。真实券商回报即使超出研究参与率也保留事实，但会记录越限诊断；非真实回报则直接拒绝。

`filled_qty=0` 可以不含 `price`，但必须给出停牌、无集合竞价撮合、队列未轮到等配置允许的可靠原因。

日线 `open` 可能来自9:30后的首笔连续竞价，所以只保存为 `daily_open_proxy` 诊断，`counts_as_fill=false`。缺失真值时状态为 `BUY_UNVERIFIABLE`，收益保持 `null`，不会显示为0%。

## T日收盘验证

每支实际交集候选都保存冻结的 `晋级目标`（目前只接受 `2→3` 或 `3→4`）、D收盘价、涨停机制百分比与预计涨停价。Decision 的 `d_close` 是主值，Premium 的 `close_T` 是独立交叉校验值；两者价差超过0.005、非有限正数，或预计涨停价与D收盘价及涨停机制不一致时，输入契约阻断。

页面显示的 `T日涨跌幅 / 是否晋级` 与9:25是否成交相互独立：即使候选最终未成交，也必须验证股票在T日的真实表现。北京时间T日15:10之前不得请求或写入收盘真值，页面统一显示“待验证”；15:10之后及后续日期可重试，证据不足时显示“待补证”，不能写成“否”。

验证只接受日期严格等于T且 `price_adjustment=NONE` 的无复权日线。`T日涨跌幅` 优先使用供应商正式涨跌幅并与 `T收盘价 / 冻结D收盘价 - 1` 交叉校验；供应商未提供时使用后者。账本内部以T日收盘价在半个最小价位内等于冻结涨停价判定收盘涨停，盘中触及后开板不会被视为晋级；页面不再重复展示“是否涨停”。`是否晋级` 只有在合法晋级目标存在且T日收盘涨停时为“是”。最终真值一经写入即保持幂等，不因后续供应商数据修订而静默改写。

## T+1退出

退出基准使用 `[11:00,11:01)` 至 `[11:04,11:05)` 五个一分钟桶。供应商的分钟结束标签必须先归一化为区间左边界；系统因此不会把10:59–11:04误当目标窗口。执行价固定使用不复权行情，每分钟价格必须由成交额和明确单位的成交量还原，并落在该分钟高低价和合法最小价位范围内；缺一根或数据不自洽即 `EXIT_UNVERIFIABLE`。

每分钟目标为剩余持仓的五分之一，并受保守的5%成交量参与率上限约束。缺少精确跌停价时，一字分钟线按无法成交处理；模拟成交价会限制在该分钟合法价格区间内。11:05仍有余额则 `EXIT_DELAYED`，已卖数量、每笔最低佣金、费用、剩余数量与已处理窗口全部持久化。系统仅在官方交易日历的后续交易日重试，不重放旧窗口；完全退出后按最后一笔真实归属日记账，绝不回填计划T+1。

## 状态语义

- `NOT_AVAILABLE`：该日不存在这个固定名次。
- `PENDING_BUY`：T尚未到达或真值尚未入库。
- `BUY_UNVERIFIABLE`：只有代理价，不能判成交。
- `BUY_UNFILLED`：精确真值确认未成交，现金收益为0。
- `OPEN`：已成交，等待T+1。
- `EXIT_UNVERIFIABLE`：退出数据不足，收益未知。
- `EXIT_DELAYED`：按规则仅部分退出，持仓仍存在。
- `CLOSED`：全部退出且费用后收益已结算。

`null` 表示未知或不适用，绝不等同于0。

## 全部候选组合收益

同一D日的全部实际交集候选构成一个等额影子组合，每支使用相同配置资金。`CLOSED` 使用费用后的 `net_return_on_allocated`，精确确认未成交的 `BUY_UNFILLED` 记为最终现金0；任何候选仍处于非最终状态时，当日组合收益保持 `null`。

当且仅当该日全部候选最终结算，组合收益才取所有候选最终收益的算术平均。历史累计与月度累计均对最终日组合收益复利，而不是直接相加单票百分比。存在最终日与未完成日并存时，累计值标记为暂定；只有未完成日时显示待验证。若候选因延迟退出跨越多个交易日，组合归属日取该组最后一个最终 `return_date`，不得回填计划T+1。
