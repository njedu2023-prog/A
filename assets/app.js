"use strict";

const $ = (id) => document.getElementById(id);
const colors = {1: "#176b4d", 2: "#315f84", 3: "#a47a2a"};
let model = null;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function pct(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const number = Number(value) * 100;
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function probability(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function statusText(status) {
  const labels = {
    RANKED: "已完成排序", NO_CANDIDATE: "三表交集为0", INPUT_BLOCKED: "输入阻断",
    NOT_AVAILABLE: "无该名次", PENDING_BUY: "等待T日竞价", BUY_UNVERIFIABLE: "竞价不可验证",
    BUY_UNFILLED: "竞价未成交", OPEN: "持仓待退出", EXIT_UNVERIFIABLE: "退出不可验证",
    EXIT_DELAYED: "延迟退出", CLOSED: "已完成", CASH: "现金"
  };
  return labels[status] || status || "未知";
}

function actionText(action) {
  return action === "SHADOW" ? "影子验证" : action === "NO_TRADE" ? "不交易" : action || "—";
}

function reasonText(reason) {
  if (!reason) return "—";
  if (reason === "rank_outside_tracked_1_2_3") return "第4名以后仅保留候选，不进入固定前三名账本";
  const labels = {
    "policy_gate=NO_TRADE": "正式门槛：不交易",
    "policy_gate=TRADE": "正式门槛：可交易",
    p_fill_below_threshold: "竞价成交概率不足",
    expected_net_return_not_positive: "预期净收益不为正",
    risk_adjusted_utility_not_positive: "风险效用不为正",
    market_features_incomplete: "市场特征不完整",
    shadow_validation: "仅影子验证",
    not_a_broker_order: "不发送券商订单"
  };
  return reason.split(";").map((part) => labels[part] || part).join("；");
}

function tone(value) {
  if (value === null || value === undefined) return "";
  return Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
}

function statusBadge(status) {
  const good = ["CLOSED", "RANKED", "NO_CANDIDATE", "BUY_UNFILLED"];
  const bad = ["INPUT_BLOCKED", "EXIT_DELAYED"];
  return node("span", statusText(status), `badge ${good.includes(status) ? "good" : bad.includes(status) ? "bad" : "warn"}`);
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
      if (item.is_final && (item.daily_return === null || !Number.isFinite(Number(item.daily_return)))) throw new Error("最终收益必须是数值");
      if (!item.is_final && item.daily_return !== null) throw new Error("未最终收益必须为null");
    });
  });
}

function selectedRows() {
  const month = $("month").value;
  return model.rank_daily.filter((row) => !month || ["1", "2", "3"].some((rank) => row.ranks[rank].return_date.startsWith(month)));
}

function selectedDays() {
  const decisionDates = new Set(selectedRows().map((row) => row.decision_date));
  return model.days.filter((day) => decisionDates.has(day.decision_date));
}

function renderStatus() {
  const run = model.current_run || {};
  const container = $("status");
  container.replaceChildren();
  container.className = `status-card ${run.status === "INPUT_BLOCKED" ? "error" : "ok"}`;
  const copy = node("div");
  copy.append(node("p", "CURRENT RUN", "eyebrow"), node("h2", run.message || "暂无运行结论", "status-title"));
  const meta = node("div", null, "status-meta");
  meta.append(statusBadge(run.status), node("span", `实际交集 ${run.intersection_count ?? "—"} 支`, "pill"));
  const dates = run.source_dates || {};
  Object.entries(dates).forEach(([source, value]) => meta.append(node("span", `${source} D=${value.D || "—"}`, "pill")));
  container.append(copy, meta);
}

function periodStats(rank) {
  const rows = selectedRows();
  const month = $("month").value;
  let nav = 1;
  let observedDays = 0;
  let finalDays = 0;
  let pendingDays = 0;
  rows.forEach((row) => {
    const item = row.ranks[String(rank)];
    if (month && !item.return_date.startsWith(month)) return;
    observedDays += 1;
    if (item?.is_final === true && Number.isFinite(Number(item.daily_return))) {
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
  if (!stats.finalDays) return `待验证 · ${stats.pendingDays}日未完成`;
  if (stats.pendingDays) return `暂定 · ${stats.pendingDays}日未完成`;
  return "已最终";
}

function renderRankCards() {
  const container = $("rankCards");
  container.replaceChildren();
  [1, 2, 3].forEach((rank) => {
    const all = model.rank_metrics?.[String(rank)] || {};
    const month = periodStats(rank);
    const card = node("article", null, `rank-card rank-${rank}`);
    card.append(node("span", `固定第${rank}名`, "muted"), node("strong", pct(month.value), tone(month.value)));
    const one = node("div", null, "metric-row");
    one.append(node("span", "所选月"), node("span", verificationLabel(month)));
    const two = node("div", null, "metric-row");
    const allLabel = all.cumulative_return === null || all.cumulative_return === undefined
      ? "待验证"
      : all.is_provisional ? `${pct(all.cumulative_return)}（暂定）` : pct(all.cumulative_return);
    two.append(node("span", "成立以来"), node("span", allLabel));
    const three = node("div", null, "metric-row");
    three.append(node("span", "已平仓/最终/未完成"), node("span", `${all.closed_trades || 0}/${all.final_days || 0}/${all.pending_days || 0}`));
    card.append(one, two, three);
    container.append(card);
  });
}

function renderChart() {
  const rows = selectedRows();
  const container = $("chart");
  container.replaceChildren();
  if (!rows.length) { container.append(node("div", "所选月份暂无可绘制记录", "empty")); return; }
  const width = 1000, height = 250, left = 52, right = 16, top = 15, bottom = 30;
  const month = $("month").value;
  const axisByKey = new Map();
  const events = {1: new Map(), 2: new Map(), 3: new Map()};
  rows.forEach((row) => {
    [1, 2, 3].forEach((rank) => {
      const item = row.ranks[String(rank)];
      if (month && !item.return_date.startsWith(month)) return;
      const key = `${item.return_date}|${row.decision_date}`;
      axisByKey.set(key, {key, date: item.return_date, decisionDate: row.decision_date});
      events[rank].set(key, item);
    });
  });
  const axis = [...axisByKey.values()].sort((a, b) => a.date.localeCompare(b.date) || a.decisionDate.localeCompare(b.decisionDate));
  const series = {};
  let values = [];
  [1, 2, 3].forEach((rank) => {
    let nav = 1;
    let started = false;
    let gap = false;
    series[rank] = axis.map((point) => {
      const item = events[rank].get(point.key);
      if (!item) return started && !gap ? nav : null;
      started = true;
      if (item.is_final !== true || !Number.isFinite(Number(item.daily_return))) {
        gap = true;
        return null;
      }
      nav *= 1 + Number(item.daily_return);
      gap = false;
      values.push(nav);
      return nav;
    });
  });
  if (!values.length) { container.append(node("div", "所选月份只有待验证记录，暂不绘制收益曲线。", "empty")); return; }
  let min = Math.min(...values, 0.99), max = Math.max(...values, 1.01);
  if (max - min < .01) { min -= .005; max += .005; }
  const x = (index) => left + (axis.length === 1 ? 0 : index / (axis.length - 1)) * (width - left - right);
  const y = (value) => top + (max - value) / (max - min) * (height - top - bottom);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  [0, .5, 1].forEach((fraction) => {
    const value = min + (max - min) * fraction;
    const line = document.createElementNS(svg.namespaceURI, "line");
    line.setAttribute("x1", left); line.setAttribute("x2", width - right); line.setAttribute("y1", y(value)); line.setAttribute("y2", y(value)); line.setAttribute("class", "grid");
    const label = document.createElementNS(svg.namespaceURI, "text");
    label.setAttribute("x", 3); label.setAttribute("y", y(value) + 4); label.textContent = pct(value - 1);
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
        marker.setAttribute("cx", x(points[0].index)); marker.setAttribute("cy", y(points[0].value)); marker.setAttribute("r", "3.5"); marker.setAttribute("fill", colors[rank]);
        svg.append(marker);
        return;
      }
      const path = document.createElementNS(svg.namespaceURI, "polyline");
      path.setAttribute("fill", "none"); path.setAttribute("stroke", colors[rank]); path.setAttribute("stroke-width", "3"); path.setAttribute("stroke-linejoin", "round");
      path.setAttribute("points", points.map((point) => `${x(point.index)},${y(point.value)}`).join(" "));
      svg.append(path);
    });
  });
  const start = document.createElementNS(svg.namespaceURI, "text"); start.setAttribute("x", left); start.setAttribute("y", height - 7); start.textContent = axis[0].date;
  const end = document.createElementNS(svg.namespaceURI, "text"); end.setAttribute("x", width - right); end.setAttribute("y", height - 7); end.setAttribute("text-anchor", "end"); end.textContent = axis.at(-1).date;
  svg.append(start, end); container.append(svg);
}

function table(headers, rows) {
  const result = node("table");
  const headRow = node("tr");
  headers.forEach((header, index) => headRow.append(node("th", header, index < 3 ? "left" : "")));
  const thead = node("thead"); thead.append(headRow);
  const tbody = node("tbody");
  rows.forEach((cells) => {
    const row = node("tr");
    cells.forEach((cell, index) => {
      const td = node("td", cell.text, `${index < 3 ? "left " : ""}${cell.className || ""}`.trim());
      row.append(td);
    });
    tbody.append(row);
  });
  result.append(thead, tbody); return result;
}

function renderCandidates() {
  const container = $("candidates"); container.replaceChildren();
  const day = selectedDays().at(-1);
  if (!day || !day.candidates.length) {
    $("candidateCount").textContent = "0支";
    container.append(node("div", day ? "所选月最新一日三表有效交集为0，不补票。" : "所选月份尚无冻结信号。", "empty")); return;
  }
  $("candidateCount").textContent = `${day.intersection_count}支`;
  const rows = day.candidates.map((item) => [
    {text: String(item.rank)}, {text: item.symbol}, {text: item.name, className: "name"},
    {text: pct(item.model_score), className: tone(item.model_score)},
    {text: probability(item.metrics?.p_fill_0925)}, {text: pct(item.metrics?.expected_net_return), className: tone(item.metrics?.expected_net_return)},
    {text: actionText(item.action)}, {text: reasonText(item.action_reason)}
  ]);
  container.append(table(["新排名", "代码", "股票", "风险效用", "P_fill", "预期净收益", "模式", "说明"], rows));
}

function renderDaily() {
  const container = $("daily"); container.replaceChildren();
  const rows = selectedRows();
  if (!rows.length) { container.append(node("div", "所选月份暂无记录。", "empty")); return; }
  container.append(table(["D日", "第1名状态/归属日", "第1名收益", "第2名状态/归属日", "第2名收益", "第3名状态/归属日", "第3名收益"], rows.map((row) => [
    {text: row.decision_date},
    {text: `${statusText(row.ranks["1"].state)} · ${row.ranks["1"].return_date}`}, {text: pct(row.ranks["1"].daily_return), className: tone(row.ranks["1"].daily_return)},
    {text: `${statusText(row.ranks["2"].state)} · ${row.ranks["2"].return_date}`}, {text: pct(row.ranks["2"].daily_return), className: tone(row.ranks["2"].daily_return)},
    {text: `${statusText(row.ranks["3"].state)} · ${row.ranks["3"].return_date}`}, {text: pct(row.ranks["3"].daily_return), className: tone(row.ranks["3"].daily_return)}
  ])));
}

function renderIssues() {
  const container = $("issues"); container.replaceChildren();
  const issues = model.source_issues || [];
  if (!issues.length) { container.append(node("div", "本次未发现源契约问题。", "empty")); return; }
  issues.forEach((item) => {
    const box = node("article", null, `issue ${item.severity || "warning"}`);
    box.append(node("strong", `${item.code} · ${item.source_id}`), node("span", item.message));
    if (item.details && Object.keys(item.details).length) box.append(node("pre", JSON.stringify(item.details, null, 2), "muted"));
    container.append(box);
  });
}

function render() {
  renderStatus(); renderRankCards(); renderChart(); renderCandidates(); renderDaily(); renderIssues();
}

async function start() {
  try {
    const response = await fetch(`./data/dashboard.v1.json?t=${Date.now()}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`数据请求失败 HTTP ${response.status}`);
    model = await response.json(); validate(model);
    $("updated").textContent = `数据更新时间 ${model.generated_at} · ${model.timezone}`;
    const months = model.available_months || [];
    const select = $("month"); select.replaceChildren();
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
