"use strict";

const $ = (id) => document.getElementById(id);
const colors = {1: "#16865f", 2: "#4e7195", 3: "#a0844c"};
let model = null;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function finite(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function pct(value) {
  if (!finite(value)) return "—";
  const number = Number(value) * 100;
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function probability(value) {
  if (!finite(value)) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function price(value) {
  if (!finite(value)) return "—";
  return Number(value).toFixed(2);
}

function tone(value) {
  if (!finite(value)) return "";
  return Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
}

function statusText(status) {
  const labels = {
    RANKED: "已完成排序",
    NO_CANDIDATE: "三表交集为0",
    INPUT_BLOCKED: "输入阻断",
    NOT_AVAILABLE: "无该名次",
    PENDING_BUY: "待9:25竞价",
    BUY_UNVERIFIABLE: "竞价待核验",
    BUY_UNFILLED: "竞价未成交",
    OPEN: "持仓待退出",
    EXIT_UNVERIFIABLE: "退出待核验",
    EXIT_DELAYED: "延迟退出",
    CLOSED: "已验证",
    CASH: "现金",
    PENDING: "待验证",
    PROFIT: "盈利",
    LOSS: "未盈利",
    FLAT: "持平",
    WARNING: "提示"
  };
  return labels[status] || status || "待验证";
}

function resultText(result, isFinal) {
  if (isFinal !== true) return "待验证";
  return statusText(result);
}

function badge(status, title) {
  const good = ["CLOSED", "CASH", "BUY_UNFILLED", "PROFIT", "RANKED", "NO_CANDIDATE"];
  const bad = ["INPUT_BLOCKED", "EXIT_DELAYED", "LOSS"];
  const element = node("span", statusText(status), `badge ${good.includes(status) ? "good" : bad.includes(status) ? "bad" : "warn"}`);
  if (title) element.title = title;
  return element;
}

function reasonText(reason) {
  if (!reason) return "";
  const labels = {
    rank_outside_tracked_1_2_3: "固定前三名以外，仍进入全部候选影子账本",
    "policy_gate=NO_TRADE": "正式门槛：不交易",
    "policy_gate=TRADE": "正式门槛：可交易",
    p_fill_below_threshold: "竞价成交概率不足",
    expected_net_return_not_positive: "预期净收益不为正",
    risk_adjusted_utility_not_positive: "风险效用不为正",
    market_features_incomplete: "市场特征不完整",
    shadow_validation: "仅影子验证",
    shadow_validation_all_intersection_candidates: "全部交集候选仅做影子验证",
    not_a_broker_order: "不发送券商订单",
    previous_action_preserved_in_action_audit: "历史动作已留存审计"
  };
  return String(reason).split(";").map((part) => labels[part] || part).join("；");
}

function validate(data) {
  if (!data || data.schema_version !== "dashboard_v1") throw new Error("不支持的数据版本");
  if (!Array.isArray(data.days) || !Array.isArray(data.rank_daily)) throw new Error("看板数据结构不完整");
  if (JSON.stringify(data.policy?.tracked_ranks) !== JSON.stringify([1, 2, 3])) throw new Error("看板必须固定跟踪第1/2/3名");
  data.rank_daily.forEach((row) => {
    ["1", "2", "3"].forEach((rank) => {
      const item = row.ranks?.[rank];
      if (!item || typeof item.is_final !== "boolean") throw new Error(`收益行缺少第${rank}名状态`);
      if (typeof item.return_date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(item.return_date)) throw new Error("收益行缺少归属日期");
      if (item.is_final && !finite(item.daily_return)) throw new Error("最终收益必须是数值");
      if (!item.is_final && item.daily_return !== null) throw new Error("未最终收益必须为null");
    });
  });
}

function selectedMonth() {
  return $("month").value;
}

function selectedRows() {
  const month = selectedMonth();
  return model.rank_daily.filter((row) => !month || ["1", "2", "3"].some((rank) => row.ranks?.[rank]?.return_date?.startsWith(month)));
}

function selectedDays() {
  const month = selectedMonth();
  const decisionDates = new Set(selectedRows().map((row) => row.decision_date));
  return model.days.filter((day) => {
    if (!month) return true;
    if (decisionDates.has(day.decision_date)) return true;
    return [day.planned_exit_date, day.buy_date, day.decision_date].some((date) => String(date || "").startsWith(month));
  });
}

function fallbackPortfolioDaily() {
  return model.days.map((day) => {
    const candidates = (day.candidates || []).map((candidate) => {
      const slot = day.rank_slots?.[String(candidate.rank)] || {};
      const status = slot.status || candidate.ledger_status || (day.selection_status === "RANKED" ? "PENDING_BUY" : day.selection_status);
      const isFinal = ["CLOSED", "CASH", "BUY_UNFILLED", "NOT_AVAILABLE"].includes(status);
      const netReturn = slot.pnl?.net_return ?? slot.pnl?.return ?? null;
      return {
        candidate_id: candidate.candidate_id,
        rank: candidate.rank,
        symbol: candidate.symbol,
        name: candidate.name,
        status,
        is_final: isFinal,
        buy_price: slot.buy?.avg_price ?? null,
        exit_price: slot.exit?.avg_price ?? null,
        net_return: isFinal && finite(netReturn) ? Number(netReturn) : isFinal && ["CASH", "BUY_UNFILLED"].includes(status) ? 0 : null,
        result: isFinal && finite(netReturn) ? (Number(netReturn) > 0 ? "PROFIT" : Number(netReturn) < 0 ? "LOSS" : "FLAT") : "PENDING",
        return_date: slot.pnl?.return_date || day.planned_exit_date
      };
    });
    const finalCandidates = candidates.filter((item) => item.is_final);
    const allFinal = candidates.length > 0 && finalCandidates.length === candidates.length;
    const returns = finalCandidates.map((item) => item.net_return).filter(finite);
    const portfolioReturn = allFinal && returns.length === candidates.length
      ? returns.reduce((sum, value) => sum + Number(value), 0) / returns.length
      : null;
    return {
      decision_date: day.decision_date,
      buy_date: day.buy_date,
      planned_exit_date: day.planned_exit_date,
      return_date: allFinal ? day.planned_exit_date : null,
      candidate_count: candidates.length,
      final_count: finalCandidates.length,
      pending_count: candidates.length - finalCandidates.length,
      profitable_count: finalCandidates.filter((item) => Number(item.net_return) > 0).length,
      portfolio_return: portfolioReturn,
      result: !allFinal ? "PENDING" : Number(portfolioReturn) > 0 ? "PROFIT" : Number(portfolioReturn) < 0 ? "LOSS" : "FLAT",
      is_final: allFinal,
      is_provisional: finalCandidates.length > 0 && !allFinal,
      candidates
    };
  });
}

function portfolioDaily() {
  return Array.isArray(model.portfolio_daily) && model.portfolio_daily.length
    ? model.portfolio_daily
    : fallbackPortfolioDaily();
}

function selectedPortfolioDaily() {
  const month = selectedMonth();
  return portfolioDaily().filter((day) => {
    if (!month) return true;
    const attributionDate = day.return_date || day.planned_exit_date || day.buy_date || day.decision_date;
    return String(attributionDate || "").startsWith(month);
  });
}

function renderStatus() {
  const run = model.current_run || {};
  const container = $("status");
  container.replaceChildren();
  container.className = `status-card ${run.status === "INPUT_BLOCKED" ? "error" : "ok"}`;
  const copy = node("div");
  copy.append(node("h2", run.message || "暂无运行结论", "status-title"));
  const meta = node("div", null, "status-meta");
  meta.append(badge(run.status), node("span", `实际交集 ${run.intersection_count ?? "—"} 支`, "pill"));
  const dates = run.source_dates || {};
  Object.entries(dates).forEach(([source, value]) => meta.append(node("span", `${source} · D ${value.D || "—"}`, "pill")));
  container.append(copy, meta);
}

function periodStats(rank) {
  const rows = selectedRows();
  let nav = 1;
  let observedDays = 0;
  let finalDays = 0;
  let pendingDays = 0;
  rows.forEach((row) => {
    const item = row.ranks?.[String(rank)];
    if (!item) return;
    observedDays += 1;
    if (item.is_final === true && finite(item.daily_return)) {
      nav *= 1 + Number(item.daily_return);
      finalDays += 1;
    } else {
      pendingDays += 1;
    }
  });
  return {
    value: finalDays ? nav - 1 : null,
    finalDays,
    pendingDays,
    isProvisional: finalDays > 0 && pendingDays > 0,
    hasRows: observedDays > 0
  };
}

function verificationLabel(stats) {
  if (!stats.hasRows) return "暂无记录";
  if (!stats.finalDays) return `待验证 · ${stats.pendingDays}日`;
  if (stats.pendingDays) return `暂定 · ${stats.pendingDays}日待完成`;
  return "已最终";
}

function addMetricRow(card, label, value) {
  const row = node("div", null, "metric-row");
  row.append(node("span", label), node("span", value));
  card.append(row);
}

function portfolioPeriodStats() {
  const rows = selectedPortfolioDaily();
  let nav = 1;
  let finalDays = 0;
  let pendingDays = 0;
  rows.forEach((day) => {
    if (day.is_final === true && finite(day.portfolio_return)) {
      nav *= 1 + Number(day.portfolio_return);
      finalDays += 1;
    } else {
      pendingDays += 1;
    }
  });
  return {
    value: finalDays ? nav - 1 : null,
    finalDays,
    pendingDays,
    isProvisional: finalDays > 0 && pendingDays > 0,
    hasRows: rows.length > 0
  };
}

function renderRankCards() {
  const container = $("rankCards");
  container.replaceChildren();
  [1, 2, 3].forEach((rank) => {
    const all = model.rank_metrics?.[String(rank)] || {};
    const month = periodStats(rank);
    const card = node("article", null, `rank-card rank-${rank}`);
    card.append(node("span", `TOP${rank}`, "card-label top-rank-label"), node("strong", pct(month.value), tone(month.value)));
    addMetricRow(card, "月度状态", verificationLabel(month));
    const allLabel = !finite(all.cumulative_return)
      ? "待验证"
      : `${pct(all.cumulative_return)}${all.is_provisional ? " · 暂定" : ""}`;
    addMetricRow(card, "成立以来", allLabel);
    addMetricRow(card, "最终 / 未完成", `${all.final_days || 0} / ${all.pending_days || 0}`);
    container.append(card);
  });

  const metrics = model.portfolio_metrics || {};
  const history = {
    value: finite(metrics.cumulative_return) ? Number(metrics.cumulative_return) : null,
    finalDays: Number(metrics.final_days || 0),
    pendingDays: Number(metrics.pending_days || 0),
    hasRows: Array.isArray(metrics.history) ? metrics.history.length > 0 : portfolioDaily().length > 0,
    isProvisional: metrics.is_provisional === true
  };
  const historyCard = node("article", null, "rank-card portfolio-card");
  historyCard.append(node("span", "全部候选 · 历史累计总收益", "card-label"), node("strong", pct(history.value), tone(history.value)));
  addMetricRow(historyCard, "组合口径", "全部交集候选等额");
  addMetricRow(historyCard, "统计状态", verificationLabel(history));
  addMetricRow(historyCard, "最终 / 未完成", `${history.finalDays} / ${history.pendingDays}`);
  container.append(historyCard);

  const month = selectedMonth();
  const computed = portfolioPeriodStats();
  const monthMetric = metrics.by_month?.[month] || {};
  const monthValue = finite(monthMetric.cumulative_return) ? Number(monthMetric.cumulative_return) : computed.value;
  const monthFinalDays = Number(monthMetric.final_days ?? computed.finalDays);
  const monthCard = node("article", null, "rank-card portfolio-card month");
  monthCard.append(node("span", `全部候选 · ${month || "所选月"}累计总收益`, "card-label"), node("strong", pct(monthValue), tone(monthValue)));
  addMetricRow(monthCard, "组合口径", "每日等额 · 按退出日");
  addMetricRow(monthCard, "统计状态", verificationLabel({...computed, finalDays: monthFinalDays}));
  addMetricRow(monthCard, "最终 / 未完成", `${monthFinalDays} / ${computed.pendingDays}`);
  container.append(monthCard);
}

function renderChart() {
  const rows = selectedRows();
  const container = $("chart");
  container.replaceChildren();
  container.classList.remove("is-empty");
  if (!rows.length) {
    container.classList.add("is-empty");
    container.append(node("div", "所选月份暂无可绘制记录", "empty"));
    return;
  }
  const width = 1000;
  const height = 190;
  const left = 48;
  const right = 12;
  const top = 12;
  const bottom = 25;
  const axisByKey = new Map();
  const events = {1: new Map(), 2: new Map(), 3: new Map()};
  rows.forEach((row) => {
    [1, 2, 3].forEach((rank) => {
      const item = row.ranks?.[String(rank)];
      if (!item) return;
      const key = `${item.return_date}|${row.decision_date}`;
      axisByKey.set(key, {key, date: item.return_date, decisionDate: row.decision_date});
      events[rank].set(key, item);
    });
  });
  const axis = [...axisByKey.values()].sort((a, b) => a.date.localeCompare(b.date) || a.decisionDate.localeCompare(b.decisionDate));
  const series = {};
  const values = [];
  [1, 2, 3].forEach((rank) => {
    let nav = 1;
    let started = false;
    let gap = false;
    series[rank] = axis.map((point) => {
      const item = events[rank].get(point.key);
      if (!item) return started && !gap ? nav : null;
      started = true;
      if (item.is_final !== true || !finite(item.daily_return)) {
        gap = true;
        return null;
      }
      nav *= 1 + Number(item.daily_return);
      gap = false;
      values.push(nav);
      return nav;
    });
  });
  if (!values.length) {
    container.classList.add("is-empty");
    container.append(node("div", "所选月份只有待验证记录，收益曲线将在形成最终真值后显示。", "empty"));
    return;
  }
  let min = Math.min(...values, 0.99);
  let max = Math.max(...values, 1.01);
  if (max - min < .01) {
    min -= .005;
    max += .005;
  }
  const x = (index) => left + (axis.length === 1 ? 0 : index / (axis.length - 1)) * (width - left - right);
  const y = (value) => top + (max - value) / (max - min) * (height - top - bottom);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  [0, .5, 1].forEach((fraction) => {
    const value = min + (max - min) * fraction;
    const line = document.createElementNS(svg.namespaceURI, "line");
    line.setAttribute("x1", left);
    line.setAttribute("x2", width - right);
    line.setAttribute("y1", y(value));
    line.setAttribute("y2", y(value));
    line.setAttribute("class", "grid");
    const label = document.createElementNS(svg.namespaceURI, "text");
    label.setAttribute("x", 2);
    label.setAttribute("y", y(value) + 3);
    label.textContent = pct(value - 1);
    svg.append(line, label);
  });
  [1, 2, 3].forEach((rank) => {
    const segments = [];
    let segment = [];
    series[rank].forEach((value, index) => {
      if (value === null) {
        if (segment.length) segments.push(segment);
        segment = [];
      } else {
        segment.push({value, index});
      }
    });
    if (segment.length) segments.push(segment);
    segments.forEach((points) => {
      if (points.length === 1) {
        const marker = document.createElementNS(svg.namespaceURI, "circle");
        marker.setAttribute("cx", x(points[0].index));
        marker.setAttribute("cy", y(points[0].value));
        marker.setAttribute("r", "3");
        marker.setAttribute("fill", colors[rank]);
        svg.append(marker);
        return;
      }
      const path = document.createElementNS(svg.namespaceURI, "polyline");
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", colors[rank]);
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linejoin", "round");
      path.setAttribute("points", points.map((point) => `${x(point.index)},${y(point.value)}`).join(" "));
      svg.append(path);
    });
  });
  const start = document.createElementNS(svg.namespaceURI, "text");
  start.setAttribute("x", left);
  start.setAttribute("y", height - 5);
  start.textContent = axis[0].date;
  const end = document.createElementNS(svg.namespaceURI, "text");
  end.setAttribute("x", width - right);
  end.setAttribute("y", height - 5);
  end.setAttribute("text-anchor", "end");
  end.textContent = axis.at(-1).date;
  svg.append(start, end);
  container.append(svg);
}

function table(headers, rows, minWidth) {
  const result = node("table");
  if (minWidth) result.style.minWidth = minWidth;
  const headRow = node("tr");
  headers.forEach((header) => {
    const config = typeof header === "string" ? {text: header} : header;
    headRow.append(node("th", config.text, config.align === "left" ? "left" : ""));
  });
  const thead = node("thead");
  thead.append(headRow);
  const tbody = node("tbody");
  rows.forEach((cells) => {
    const row = node("tr");
    cells.forEach((cell) => {
      const td = node("td", null, `${cell.align === "left" ? "left " : ""}${cell.className || ""}`.trim());
      if (cell.title) td.title = cell.title;
      if (cell.content instanceof Node) td.append(cell.content);
      else td.textContent = cell.text ?? "—";
      row.append(td);
    });
    tbody.append(row);
  });
  result.append(thead, tbody);
  return result;
}

function latestPortfolioIndex() {
  const latestDay = selectedDays().at(-1);
  if (!latestDay) return new Map();
  const entry = portfolioDaily().find((day) => day.decision_date === latestDay.decision_date);
  return new Map((entry?.candidates || []).map((item) => [item.candidate_id || item.symbol, item]));
}

function renderCandidates() {
  const container = $("candidates");
  container.replaceChildren();
  const day = selectedDays().at(-1);
  if (!day || !day.candidates?.length) {
    $("candidateCount").textContent = "0支";
    container.append(node("div", day ? "所选月最新一日三表有效交集为0，不补票。" : "所选月份尚无冻结信号。", "empty"));
    return;
  }
  $("candidateCount").textContent = `${day.intersection_count}支`;
  const ledger = latestPortfolioIndex();
  const rows = day.candidates.map((item) => {
    const ledgerItem = ledger.get(item.candidate_id) || ledger.get(item.symbol);
    const ledgerStatus = ledgerItem?.status || "PENDING";
    const stateBadge = badge(ledgerStatus, reasonText(item.action_reason));
    stateBadge.classList.add("candidate-state");
    return [
      {text: String(item.rank), align: "left"},
      {text: item.symbol, align: "left"},
      {text: item.name, align: "left", className: "name"},
      {text: pct(item.model_score), className: tone(item.model_score)},
      {text: probability(item.metrics?.p_fill_0925)},
      {text: pct(item.metrics?.expected_net_return), className: tone(item.metrics?.expected_net_return)},
      {text: "影子账本"},
      {content: stateBadge, title: reasonText(item.action_reason)}
    ];
  });
  const candidateTable = table([
    {text: "排名", align: "left"},
    {text: "代码", align: "left"},
    {text: "股票", align: "left"},
    "风险效用",
    "P_fill",
    "预期净收益",
    "账本",
    "执行状态"
  ], rows, "760px");
  candidateTable.classList.add("candidate-table");
  container.append(candidateTable);
}

function ledgerFact(label, value, className) {
  const fact = node("span", null, "ledger-fact");
  fact.append(node("small", label), node("strong", value, className));
  return fact;
}

function stockCell(item) {
  const cell = node("div", null, "stock-cell");
  cell.append(node("strong", item.symbol || item.code || "—"), node("span", item.name || `第${item.rank || "—"}名`));
  return cell;
}

function renderDaily() {
  const container = $("daily");
  container.replaceChildren();
  const rows = selectedPortfolioDaily().slice().sort((a, b) => String(b.decision_date).localeCompare(String(a.decision_date)));
  if (!rows.length) {
    container.append(node("div", "所选月份暂无记录。", "empty"));
    return;
  }
  const firstFinalIndex = rows.findIndex((day) => day.is_final === true);
  rows.forEach((day, index) => {
    const details = node("details", null, "ledger-day");
    if (index === (firstFinalIndex >= 0 ? firstFinalIndex : 0)) details.open = true;
    const count = Number(day.candidate_count ?? day.candidates?.length ?? 0);
    const finalCount = Number(day.final_count ?? 0);
    const profitable = Number(day.profitable_count ?? 0);
    const summary = node("summary");
    const verifiedDate = day.return_date || day.planned_exit_date || "—";
    const returnValue = day.is_final === true && finite(day.portfolio_return) ? Number(day.portfolio_return) : null;
    summary.append(
      ledgerFact("信号日", day.decision_date || "—"),
      ledgerFact("9:25买入日", day.buy_date || "—"),
      ledgerFact("T+1验证日", verifiedDate),
      ledgerFact("股票", `${count} 支`),
      ledgerFact("已验证", `${finalCount} / ${count}`),
      ledgerFact("盈利票", day.is_final === true || finalCount > 0 ? `${profitable} / ${count}` : "—"),
      ledgerFact("当日组合净收益", pct(returnValue), tone(returnValue)),
      ledgerFact("当日结果", resultText(day.result, day.is_final), tone(returnValue)),
      node("span", null, "ledger-toggle")
    );
    details.append(summary);
    const detailWrap = node("div", null, "table-wrap");
    const candidates = (day.candidates || []).slice().sort((a, b) => Number(a.rank || 9999) - Number(b.rank || 9999));
    const childRows = candidates.map((item) => {
      const netReturn = item.is_final === true && finite(item.net_return) ? Number(item.net_return) : null;
      return [
        {content: stockCell(item), align: "left"},
        {text: price(item.buy_price)},
        {text: price(item.exit_price)},
        {text: pct(netReturn), className: tone(netReturn)},
        {text: resultText(item.result, item.is_final), className: tone(netReturn)},
        {content: badge(item.status)}
      ];
    });
    if (childRows.length) {
      detailWrap.append(table([
        {text: "股票", align: "left"},
        "9:25入场价",
        "T+1退出价",
        "净收益",
        "结果",
        "状态"
      ], childRows, "720px"));
    } else {
      detailWrap.append(node("div", "该批次暂无候选明细。", "empty"));
    }
    details.append(detailWrap);
    container.append(details);
  });
}

function render() {
  renderStatus();
  renderRankCards();
  renderChart();
  renderCandidates();
  renderDaily();
}

function availableMonths() {
  const values = new Set(model.available_months || []);
  Object.keys(model.portfolio_metrics?.by_month || {}).forEach((month) => values.add(month));
  portfolioDaily().forEach((day) => {
    const date = day.return_date || day.planned_exit_date || day.buy_date || day.decision_date;
    if (/^\d{4}-\d{2}/.test(String(date || ""))) values.add(String(date).slice(0, 7));
  });
  return [...values].sort().reverse();
}

function formatUpdatedAt(value) {
  const match = String(value || "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/);
  return match ? `${match[1]}：${match[2]}:${match[3]}` : String(value || "—");
}

async function start() {
  try {
    const response = await fetch(`./data/dashboard.v1.json?t=${Date.now()}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`数据请求失败 HTTP ${response.status}`);
    model = await response.json();
    validate(model);
    $("updated").textContent = `数据更新时间 ${formatUpdatedAt(model.generated_at)}`;
    const months = availableMonths();
    const select = $("month");
    select.replaceChildren();
    if (!months.length) select.append(new Option("暂无月份", ""));
    months.forEach((month) => select.append(new Option(month, month)));
    select.addEventListener("change", render);
    render();
  } catch (error) {
    $("updated").textContent = String(error.message || error);
    $("status").replaceChildren(node("div", "看板数据暂不可用，请检查最新工作流与 data/dashboard.v1.json。", "empty"));
  }
}

start();
