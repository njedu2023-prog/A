"use strict";

const $ = (id) => document.getElementById(id);
const colors = {1: "#16865f", 2: "#4e7195", 3: "#a0844c"};
const MIN_TREND_POINTS = 5;
const DATA_FETCH_TIMEOUT_MS = 8000;
const DATA_ENDPOINTS = Object.freeze({
  pages: Object.freeze({
    dashboard: "./data/dashboard.v1.json",
    sourceIssues: "./data/source_issues.v1.json"
  }),
  main: Object.freeze({
    dashboard: "https://raw.githubusercontent.com/njedu2023-prog/A/main/data/dashboard.v1.json",
    sourceIssues: "https://raw.githubusercontent.com/njedu2023-prog/A/main/data/source_issues.v1.json"
  })
});
let model = null;
let sourceIssues = null;
let dataLoadState = null;

function timestampedUrl(url, requestTimestamp) {
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}t=${encodeURIComponent(String(requestTimestamp))}`;
}

function generatedAtMillis(payload) {
  const value = payload?.generated_at;
  if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new Error("数据缺少带时区的 generated_at");
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) throw new Error("generated_at 无效");
  return parsed;
}

function validateSourceIssues(data) {
  if (!data || data.schema_version !== "source_issues_v1") throw new Error("不支持的问题审计数据版本");
  if (!Array.isArray(data.issues)) throw new Error("问题审计数据结构不完整");
}

async function fetchSnapshot(source, kind, expectedSchema, validator, requestTimestamp) {
  const url = DATA_ENDPOINTS[source]?.[kind];
  if (!url) throw new Error(`未配置 ${source} ${kind} 数据源`);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), DATA_FETCH_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(timestampedUrl(url, requestTimestamp), {
      cache: "no-store",
      signal: controller.signal
    });
  } finally {
    clearTimeout(timeout);
  }
  if (!response.ok) throw new Error(`${source} ${kind} 请求失败 HTTP ${response.status}`);
  const payload = await response.json();
  if (!payload || payload.schema_version !== expectedSchema) throw new Error(`${source} ${kind} schema 不匹配`);
  validator(payload);
  return {source, payload, generatedAt: generatedAtMillis(payload)};
}

async function loadSourcePair(kind, expectedSchema, validator, requestTimestamp) {
  const sources = ["pages", "main"];
  const outcomes = await Promise.allSettled(
    sources.map((source) => fetchSnapshot(source, kind, expectedSchema, validator, requestTimestamp))
  );
  return Object.fromEntries(sources.map((source, index) => {
    const outcome = outcomes[index];
    return outcome.status === "fulfilled"
      ? [source, {ok: true, ...outcome.value}]
      : [source, {ok: false, source, error: outcome.reason}];
  }));
}

function chooseNewestSnapshot(candidates) {
  const valid = Object.values(candidates).filter((candidate) => candidate?.ok === true);
  if (!valid.length) throw new Error("Pages 与 main 数据均不可用");
  valid.sort((left, right) => {
    const freshness = right.generatedAt - left.generatedAt;
    if (freshness) return freshness;
    return left.source === "pages" ? -1 : 1;
  });
  return valid[0];
}

function chooseCompanionSnapshot(candidates, preferredSource) {
  if (candidates[preferredSource]?.ok === true) return candidates[preferredSource];
  const fallbackSource = preferredSource === "pages" ? "main" : "pages";
  return candidates[fallbackSource]?.ok === true ? candidates[fallbackSource] : null;
}

function dataSourceCopy(selected, candidates, selectedIssues) {
  const pages = candidates.pages;
  const main = candidates.main;
  let copy;
  if (selected.source === "main") {
    copy = pages?.ok === true ? "数据来源 main 兜底（Pages待同步）" : "数据来源 main 兜底（Pages不可用）";
  } else if (main?.ok !== true) {
    copy = "数据来源 Pages（main不可用）";
  } else if (pages.generatedAt === main.generatedAt) {
    copy = "数据来源 Pages（已与 main 同步）";
  } else {
    copy = "数据来源 Pages（main较旧）";
  }
  if (!selectedIssues) return `${copy} · 审计数据不可用`;
  if (selectedIssues.source !== selected.source) return `${copy} · 审计数据已回退`;
  return copy;
}

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

function predictionOf(item) {
  const raw = item?.prediction && typeof item.prediction === "object" ? item.prediction : {};
  const metrics = item?.metrics || {};
  return {
    ...raw,
    model_id: raw.model_id || item?.model?.selected_model_id || model?.engine?.selected_model_id || "透明基线",
    fill_probability: raw.fill_probability ?? raw.p_fill ?? metrics.p_fill_0925,
    conditional_net_return_mean: raw.conditional_net_return_mean ?? metrics.expected_net_return,
    conditional_net_return_p10: raw.conditional_net_return_p10 ?? raw.conditional_net_return_q10 ?? metrics.conditional_net_return_p10 ?? metrics.conditional_net_return_q10 ?? metrics.net_return_q10,
    conditional_net_return_p50: raw.conditional_net_return_p50 ?? raw.conditional_net_return_q50 ?? metrics.conditional_net_return_p50 ?? metrics.conditional_net_return_q50 ?? metrics.net_return_q50,
    conditional_net_return_p90: raw.conditional_net_return_p90 ?? raw.conditional_net_return_q90 ?? metrics.conditional_net_return_p90 ?? metrics.conditional_net_return_q90 ?? metrics.net_return_q90,
    exit_delay_probability: raw.exit_delay_probability ?? raw.p_exit_delay ?? metrics.p_exit_delay,
    expected_exit_delay_days: raw.expected_exit_delay_days ?? raw.expected_delay_days ?? metrics.expected_delay_days,
    promotion_probability: raw.promotion_probability ?? raw.p_promotion ?? metrics.p_promotion,
    risk_adjusted_utility: raw.risk_adjusted_utility ?? raw.utility ?? metrics.risk_adjusted_utility ?? metrics.utility_score,
    gate_decision: raw.gate_decision || metrics.gate_decision || (metrics.policy_trade_eligible === true ? "TRADE" : "NO_TRADE"),
    gate_reasons: Array.isArray(raw.gate_reasons) ? raw.gate_reasons : Array.isArray(metrics.gate_reasons) ? metrics.gate_reasons : []
  };
}

function returnForecast(item) {
  const prediction = predictionOf(item);
  const mean = prediction.conditional_net_return_mean;
  const low = prediction.conditional_net_return_p10;
  const high = prediction.conditional_net_return_p90;
  if (!finite(mean)) return "—";
  if (finite(low) && finite(high)) return `${pct(mean)} · [${pct(low)}, ${pct(high)}]`;
  return pct(mean);
}

function shortModel(value) {
  const text = String(value || "透明基线");
  if (text === "transparent_shadow_champion_v2") return "透明冠军 V2";
  if (text.startsWith("formal_quant_challenger_")) {
    const prefix = "formal_quant_challenger_";
    return `学习挑战者 ${text.slice(prefix.length, prefix.length + 8)}`;
  }
  return text
    .replace("transparent_shadow_baseline_", "基线 ")
    .replace("formal_transparent_champion_", "正式基线 ");
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
    PENDING_BUY: "待开盘",
    BUY_UNVERIFIABLE: "开盘价待补",
    BUY_UNFILLED: "开盘未成交",
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

function tDayValidation(item) {
  const validation = item?.t_day_validation;
  if (!validation || typeof validation !== "object") {
    return {status: "PENDING", t_return: null, is_limit_up: null, is_promoted: null};
  }
  return validation;
}

function tDayReturnText(item) {
  const validation = tDayValidation(item);
  if (validation.status === "PENDING") return "待验证";
  if (validation.status === "UNVERIFIABLE") return "待补证";
  return validation.status === "VERIFIED" && finite(validation.t_return)
    ? pct(validation.t_return)
    : "待补证";
}

function tDayOutcomeText(item, field) {
  const validation = tDayValidation(item);
  if (validation.status === "PENDING") return "待验证";
  if (validation.status === "UNVERIFIABLE") return "待补证";
  if (validation.status !== "VERIFIED" || typeof validation[field] !== "boolean") {
    return "待补证";
  }
  return validation[field] ? "是" : "否";
}

function verifiedTReturn(item) {
  const validation = tDayValidation(item);
  return validation.status === "VERIFIED" && finite(validation.t_return)
    ? Number(validation.t_return)
    : null;
}

function badge(status, title) {
  const good = ["CLOSED", "CASH", "BUY_UNFILLED", "PROFIT", "RANKED", "NO_CANDIDATE"];
  const bad = ["INPUT_BLOCKED", "EXIT_DELAYED", "LOSS"];
  const element = node("span", statusText(status), `badge ${good.includes(status) ? "good" : bad.includes(status) ? "bad" : "warn"}`);
  if (title) element.title = title;
  return element;
}

function outputAutomationCopy(run, fallbackCompletedAt) {
  const scheduled = run.scheduled_local_time || "21:30";
  const completedAt = run.last_completed_at || fallbackCompletedAt;
  if (!run.last_attempted_at && !completedAt) {
    return `名单 ${scheduled} · 待首次执行`;
  }
  const result = run.status === "COMPLETED"
    ? "已完成"
    : statusText(run.status || "INPUT_BLOCKED");
  const timestamp = completedAt || run.last_attempted_at;
  return `名单 ${scheduled} · ${result} · ${formatUpdatedAt(timestamp)}`;
}

function validationAutomationCopy(run) {
  const scheduled = run.scheduled_local_time || "19:00";
  const timestamp = run.last_completed_at || run.last_attempted_at;
  if (!timestamp) return `验证 ${scheduled} · 待首次执行`;
  const hasBatchCounts = ["due", "final", "pending_data", "delayed", "failed"]
    .every((field) => Number.isInteger(run[field]) && run[field] >= 0);
  if (!hasBatchCounts) {
    return `验证 ${scheduled} · 已执行 · ${formatUpdatedAt(timestamp)}`;
  }
  if (run.result_status === "SUCCESS_NO_DUE") {
    return `验证 ${scheduled} · 已执行 · 到期 0 · ${formatUpdatedAt(timestamp)}`;
  }
  return [
    `验证 ${scheduled}`,
    `到期 ${run.due}`,
    `完成 ${run.final}`,
    `待数据 ${run.pending_data}`,
    `延期 ${run.delayed}`,
    `失败 ${run.failed}`,
    formatUpdatedAt(timestamp)
  ].join(" · ");
}

function reasonText(reason) {
  if (!reason) return "";
  const labels = {
    rank_outside_tracked_1_2_3: "固定前三名以外，仍进入全部候选影子账本",
    "policy_gate=NO_TRADE": "正式门槛：不交易",
    "policy_gate=TRADE": "正式门槛：可交易",
    p_fill_below_threshold: "竞价成交概率不足",
    fill_probability_below_threshold: "竞价成交估计不足",
    expected_net_return_not_positive: "预期净收益不为正",
    conditional_net_return_not_positive: "条件净收益不为正",
    return_lower_bound_not_positive: "收益下界不为正",
    conditional_return_lcb_not_positive: "收益下界不为正",
    risk_adjusted_utility_not_positive: "风险效用不为正",
    exit_delay_probability_too_high: "延迟退出风险过高",
    exit_delay_risk_above_threshold: "延迟退出风险过高",
    prediction_uncertainty_too_high: "预测不确定性过高",
    market_features_incomplete: "市场特征不完整",
    insufficient_daily_bars: "D日前日线不足",
    stale_market_features: "D日行情不完整",
    invalid_candidate_facts: "候选冻结事实不完整",
    cohort_ranking_fallback_borda: "数据不完整，按三榜共识排序",
    shadow_validation: "仅影子验证",
    shadow_validation_all_intersection_candidates: "全部交集候选仅做影子验证",
    not_a_broker_order: "不发送券商订单",
    previous_action_preserved_in_action_audit: "历史动作已留存审计"
  };
  return String(reason).split(";").map((part) => labels[part] || part).join("；");
}

function gateText(item) {
  const prediction = predictionOf(item);
  if (prediction.gate_decision === "TRADE") return "通过";
  const reasons = Array.isArray(prediction.gate_reasons) ? prediction.gate_reasons : [];
  if (!reasons.length) return "未过";
  const labels = {
    p_fill_below_threshold: "成交率低",
    fill_probability_below_threshold: "成交率低",
    expected_net_return_not_positive: "收益≤0",
    conditional_net_return_not_positive: "收益≤0",
    return_lower_bound_not_positive: "下界≤0",
    conditional_return_lcb_not_positive: "下界≤0",
    risk_adjusted_utility_not_positive: "效用≤0",
    exit_delay_probability_too_high: "延迟风险高",
    exit_delay_risk_above_threshold: "延迟风险高",
    prediction_uncertainty_too_high: "不确定性高",
    market_features_incomplete: "数据不全",
    insufficient_daily_bars: "数据不全",
    stale_market_features: "数据不全",
    invalid_candidate_facts: "数据不全",
    cohort_ranking_fallback_borda: "共识排序"
  };
  const first = labels[reasons[0]] || reasonText(reasons[0]);
  return `${first}${reasons.length > 1 ? ` +${reasons.length - 1}` : ""}`;
}

function gateTitle(item) {
  const prediction = predictionOf(item);
  const reasons = Array.isArray(prediction.gate_reasons) ? prediction.gate_reasons : [];
  return reasons.length ? reasons.map(reasonText).join("；") : gateText(item);
}

function entryStateText(item) {
  const status = String(item?.status || item?.ledger_status || "PENDING_BUY");
  if (
    finite(item?.buy_price)
    || finite(item?.buy?.avg_price)
    || ["OPEN", "EXIT_UNVERIFIABLE", "EXIT_DELAYED", "CLOSED"].includes(status)
  ) {
    return "已成交";
  }
  if (status === "BUY_UNVERIFIABLE") return "待补价";
  if (status === "BUY_UNFILLED") return "未成交";
  return "待开盘";
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
  (data.days || []).forEach((day) => {
    (day.candidates || []).forEach((candidate) => {
      const prediction = predictionOf(candidate);
      ["fill_probability", "exit_delay_probability", "promotion_probability"].forEach((field) => {
        const value = prediction[field];
        if (value !== null && value !== undefined && (!finite(value) || Number(value) < 0 || Number(value) > 1)) {
          throw new Error(`模型字段 ${field} 超出范围`);
        }
      });
      const quantiles = [
        prediction.conditional_net_return_p10,
        prediction.conditional_net_return_p50,
        prediction.conditional_net_return_p90
      ];
      if (quantiles.every(finite) && !(Number(quantiles[0]) <= Number(quantiles[1]) && Number(quantiles[1]) <= Number(quantiles[2]))) {
        throw new Error("条件净收益预测区间顺序错误");
      }
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
        stage_transition: candidate.stage_transition,
        industry: candidate.industry,
        d_close: candidate.d_close,
        model: candidate.model || day.model || null,
        prediction: candidate.prediction || predictionOf(candidate),
        t_day_validation: candidate.t_day_validation || slot.t_day_validation || {
          status: "PENDING",
          t_return: null,
          is_limit_up: null,
          is_promoted: null
        },
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
      model: day.model || null,
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
  const copy = node("div", null, "status-copy");
  const latestDecisionDate = run.decision_date || model.days?.at(-1)?.decision_date;
  const title = run.completed === true && run.status === "NO_CANDIDATE" && latestDecisionDate
    ? `D日名单筛选已执行：${latestDecisionDate} · 严格交集0支`
    : run.completed === true && run.status === "RANKED" && latestDecisionDate
      ? `D日信号已冻结：${latestDecisionDate}`
      : run.message || "暂无运行结论";
  copy.append(node("h2", title, "status-title"));
  const meta = node("div", null, "status-meta");
  const summaryRow = node("div", null, "status-meta-row status-meta-summary");
  const automationRow = node("div", null, "status-meta-row status-meta-automation");
  const sourceRow = node("div", null, "status-meta-row status-meta-sources");
  summaryRow.append(badge(run.status), node("span", `实际交集 ${run.intersection_count ?? "—"} 支`, "pill"));
  if (run.completed_at) {
    summaryRow.append(node("span", `本次执行 ${formatUpdatedAt(run.completed_at)}`, "engine-meta-text"));
  }
  const engine = model.engine || {};
  summaryRow.append(
    node("span", engine.status_label || "基线运行 · 样本积累中", "engine-meta-text"),
    node("span", `模型 · ${shortModel(engine.selected_model_id)}`, "engine-meta-text")
  );
  const dates = run.source_dates || {};
  Object.entries(dates).forEach(([source, value]) => sourceRow.append(node("span", `${source} · D ${value.D || "—"}`, "pill")));
  const automation = model.automation_runs || {};
  const outputRun = automation.output || {};
  const validationRun = automation.validation || {};
  const outputAt = outputRun.last_completed_at || (run.completed === true ? run.completed_at : null);
  const validationAt = validationRun.last_completed_at;
  const outputAutomation = node(
    "span",
    outputAutomationCopy(outputRun, outputAt),
    "automation-meta-text automation-output"
  );
  const validationAutomation = node(
    "span",
    validationAutomationCopy(validationRun),
    `automation-meta-text automation-validation ${validationRun.result_status === "DEGRADED" ? "degraded" : ""}`
  );
  if (validationRun.batch_error) validationAutomation.title = validationRun.batch_error;
  automationRow.append(outputAutomation, validationAutomation);
  meta.append(summaryRow, automationRow, sourceRow);
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

function settlementState(stats) {
  if (!stats.hasRows) return {label: "暂无记录", className: "empty"};
  if (!stats.finalDays) return {label: "待结算", className: "pending"};
  if (stats.pendingDays) return {label: "暂定", className: "provisional"};
  return {label: "已结算", className: "final"};
}

function addMetricRow(card, label, value) {
  const row = node("div", null, "metric-row");
  row.append(node("span", label), node("span", value));
  card.append(row);
}

function addCardHeader(card, title, stats) {
  const state = settlementState(stats);
  const header = node("div", null, "metric-card-head");
  header.append(
    node("span", title, "card-label top-rank-label"),
    node("span", state.label, `settlement-badge ${state.className}`)
  );
  card.append(header);
}

function createMetricGroup(title, description, gridClass) {
  const group = node("section", null, "metric-group");
  const head = node("div", null, "metric-group-head");
  head.append(node("h3", title), node("span", description));
  const grid = node("div", null, `rank-card-grid ${gridClass}`);
  group.append(head, grid);
  return {group, grid};
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
  const monthLabel = selectedMonth() || "所选月";
  const scope = $("overviewScope");
  const settledDates = selectedRows().flatMap((row) => [1, 2, 3]
    .map((rank) => row.ranks?.[String(rank)])
    .filter((item) => item?.is_final === true && finite(item.daily_return))
    .map((item) => item.return_date));
  const settledThrough = settledDates.sort().at(-1) || "暂无已结算记录";
  scope.textContent = `统计月份 ${monthLabel} · 统计截至 ${settledThrough} · 累计收益仅包含已结算批次，待结算结果不计入当前收益`;

  const ranks = createMetricGroup(
    "固定名次策略",
    "每个D日的TOP1 / TOP2 / TOP3分别复利统计",
    "rank-strategy-grid"
  );
  [1, 2, 3].forEach((rank) => {
    const all = model.rank_metrics?.[String(rank)] || {};
    const month = periodStats(rank);
    const card = node("article", null, `rank-card rank-${rank}`);
    addCardHeader(card, `TOP${rank}`, month);
    card.append(
      node("span", `${monthLabel}累计收益`, "metric-name"),
      node("strong", pct(month.value), tone(month.value))
    );
    addMetricRow(card, "已结算 / 待结算", `${month.finalDays}日 / ${month.pendingDays}日`);
    const allLabel = !finite(all.cumulative_return)
      ? "待验证"
      : `${pct(all.cumulative_return)}${all.is_provisional ? "（暂定）" : ""}`;
    addMetricRow(card, "历史累计收益", allLabel);
    ranks.grid.append(card);
  });
  container.append(ranks.group);

  const metrics = model.portfolio_metrics || {};
  const history = {
    value: finite(metrics.cumulative_return) ? Number(metrics.cumulative_return) : null,
    finalDays: Number(metrics.final_days || 0),
    pendingDays: Number(metrics.pending_days || 0),
    hasRows: Array.isArray(metrics.history) ? metrics.history.length > 0 : portfolioDaily().length > 0,
    isProvisional: metrics.is_provisional === true
  };
  const portfolios = createMetricGroup(
    "全部候选等权组合",
    "每日实际交集候选等权统计",
    "portfolio-strategy-grid"
  );
  const historyCard = node("article", null, "rank-card portfolio-card");
  addCardHeader(historyCard, "全部候选 · 历史", history);
  historyCard.append(
    node("span", "历史累计收益", "metric-name"),
    node("strong", pct(history.value), tone(history.value))
  );
  addMetricRow(historyCard, "已结算 / 待结算", `${history.finalDays}日 / ${history.pendingDays}日`);
  const dataMonths = availableMonths();
  const historyRange = dataMonths.length === 1
    ? `当前仅含 ${dataMonths[0]}`
    : dataMonths.length > 1
      ? `${dataMonths.at(-1)} 至 ${dataMonths[0]}`
      : "暂无有效数据";
  addMetricRow(historyCard, "数据范围", historyRange);
  portfolios.grid.append(historyCard);

  const month = selectedMonth();
  const computed = portfolioPeriodStats();
  const monthMetric = metrics.by_month?.[month] || {};
  const monthValue = finite(monthMetric.cumulative_return) ? Number(monthMetric.cumulative_return) : computed.value;
  const monthFinalDays = Number(monthMetric.final_days ?? computed.finalDays);
  const monthStats = {...computed, finalDays: monthFinalDays};
  const monthCard = node("article", null, "rank-card portfolio-card month");
  addCardHeader(monthCard, `全部候选 · ${monthLabel}`, monthStats);
  monthCard.append(
    node("span", "月度累计收益", "metric-name"),
    node("strong", pct(monthValue), tone(monthValue))
  );
  addMetricRow(monthCard, "已结算 / 待结算", `${monthFinalDays}日 / ${computed.pendingDays}日`);
  addMetricRow(monthCard, "收益归属", "按实际退出日");
  portfolios.grid.append(monthCard);
  container.append(portfolios.group);
}

function renderComparisonChart(container, rankStats) {
  const values = rankStats.map(({stats}) => stats.value).filter(finite).map(Number);
  if (!values.length) {
    container.classList.add("is-empty");
    container.append(node("div", "所选月份只有待结算记录，收益对比将在形成最终结果后显示。", "empty"));
    return;
  }
  container.classList.add("comparison-chart");
  const maxAbs = Math.max(...values.map(Math.abs), 0.01);
  const rows = node("div", null, "comparison-rows");
  rankStats.forEach(({rank, stats}) => {
    const row = node("div", null, "comparison-row");
    const label = node("span", `TOP${rank}`, "comparison-label");
    const track = node("div", null, "comparison-track");
    if (finite(stats.value)) {
      const value = Number(stats.value);
      const bar = node("span", null, `comparison-bar ${value >= 0 ? "gain" : "loss"}`);
      bar.style.width = `${Math.max(1.5, Math.abs(value) / maxAbs * 48)}%`;
      bar.style.backgroundColor = colors[rank];
      track.append(bar);
    }
    const valueLabel = node("strong", pct(stats.value), tone(stats.value));
    row.append(label, track, valueLabel);
    rows.append(row);
  });
  const scale = node("div", null, "comparison-scale");
  scale.append(node("span", pct(-maxAbs)), node("span", "0%"), node("span", pct(maxAbs)));
  container.append(rows, scale);
  container.setAttribute(
    "aria-label",
    rankStats.map(({rank, stats}) => `TOP${rank}${pct(stats.value)}`).join("，")
  );
}

function renderChart() {
  const rows = selectedRows();
  const container = $("chart");
  const title = $("chartTitle");
  const note = $("chartNote");
  const legend = $("chartLegend");
  const monthLabel = selectedMonth() || "所选月";
  const rankStats = [1, 2, 3].map((rank) => ({rank, stats: periodStats(rank)}));
  const settledMinimum = Math.min(...rankStats.map(({stats}) => stats.finalDays));
  container.replaceChildren();
  container.className = "chart";
  if (!rows.length) {
    title.textContent = `${monthLabel}收益统计`;
    note.textContent = "";
    legend.hidden = true;
    container.classList.add("is-empty");
    container.append(node("div", "所选月份暂无可绘制记录", "empty"));
    return;
  }
  if (settledMinimum < MIN_TREND_POINTS) {
    title.textContent = `${monthLabel}累计收益对比`;
    note.textContent = `已结算样本少于${MIN_TREND_POINTS}期，暂不绘制趋势`;
    legend.hidden = true;
    renderComparisonChart(container, rankStats);
    return;
  }
  title.textContent = `${monthLabel}累计收益曲线（仅已结算）`;
  note.textContent = "";
  legend.hidden = false;
  container.setAttribute("aria-label", "TOP1、TOP2、TOP3的已结算累计收益曲线");
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
      if (!item || item.is_final !== true || !finite(item.daily_return)) return;
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
    series[rank] = axis.map((point) => {
      const item = events[rank].get(point.key);
      if (!item || item.is_final !== true || !finite(item.daily_return)) return null;
      nav *= 1 + Number(item.daily_return);
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
  if (min <= 1 && max >= 1) {
    const baseline = document.createElementNS(svg.namespaceURI, "line");
    baseline.setAttribute("x1", left);
    baseline.setAttribute("x2", width - right);
    baseline.setAttribute("y1", y(1));
    baseline.setAttribute("y2", y(1));
    baseline.setAttribute("class", "baseline");
    svg.append(baseline);
  }
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
      if (points.length > 1) {
        const path = document.createElementNS(svg.namespaceURI, "polyline");
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", colors[rank]);
        path.setAttribute("stroke-width", "2");
        path.setAttribute("stroke-linejoin", "round");
        path.setAttribute("points", points.map((point) => `${x(point.index)},${y(point.value)}`).join(" "));
        svg.append(path);
      }
      points.forEach((point) => {
        const marker = document.createElementNS(svg.namespaceURI, "circle");
        marker.setAttribute("cx", x(point.index));
        marker.setAttribute("cy", y(point.value));
        marker.setAttribute("r", "3");
        marker.setAttribute("fill", colors[rank]);
        svg.append(marker);
      });
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

function table(headers, rows, minWidth, ariaLabel) {
  const result = node("table");
  if (minWidth) result.style.minWidth = minWidth;
  if (ariaLabel) result.setAttribute("aria-label", ariaLabel);
  const headRow = node("tr");
  headers.forEach((header) => {
    const config = typeof header === "string" ? {text: header} : header;
    const className = `${config.align === "left" ? "left " : ""}${config.className || ""}`.trim();
    const heading = node("th", config.text, className);
    heading.scope = "col";
    headRow.append(heading);
  });
  const thead = node("thead");
  thead.append(headRow);
  const tbody = node("tbody");
  rows.forEach((cells) => {
    const row = node("tr");
    cells.forEach((cell, index) => {
      const td = node("td", null, `${cell.align === "left" ? "left " : ""}${cell.className || ""}`.trim());
      const header = headers[index];
      const label = typeof header === "string" ? header : header?.text;
      if (label) td.dataset.label = label;
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
    container.append(node(
      "div",
      day
        ? `D日 ${day.decision_date} 名单筛选已执行：三表严格交集0支，合法空选且不补票。`
        : "所选月份尚无冻结信号。",
      "empty"
    ));
    return;
  }
  const ledger = latestPortfolioIndex();
  const rows = day.candidates.map((item) => {
    const ledgerItem = ledger.get(item.candidate_id) || ledger.get(item.symbol);
    const ledgerStatus = ledgerItem?.status || "PENDING";
    const stateBadge = badge(ledgerStatus, reasonText(item.action_reason));
    stateBadge.classList.add("candidate-state");
    const validationItem = ledgerItem || item;
    const tReturn = verifiedTReturn(validationItem);
    const prediction = predictionOf(item);
    const gate = node(
      "span",
      gateText(item),
      `gate-text ${prediction.gate_decision === "TRADE" ? "trade" : "no-trade"}`
    );
    gate.setAttribute("aria-label", gateTitle(item));
    return [
      {text: String(item.rank), align: "left", className: "col-rank"},
      {text: item.symbol, align: "left", className: "code sticky-code col-code"},
      {text: item.name, align: "left", className: "name sticky-name col-name"},
      {text: returnForecast(item), className: `col-return-range ${tone(prediction.conditional_net_return_mean)}`},
      {text: item.stage_transition || "—", className: "col-stage"},
      {text: probability(prediction.promotion_probability), className: "col-promotion"},
      {text: item.industry || "—", align: "left", className: "col-industry"},
      {text: price(item.d_close), className: "col-d-close"},
      {text: tDayReturnText(validationItem), className: `col-t-return ${tone(tReturn)}`},
      {text: tDayOutcomeText(validationItem, "is_promoted"), className: "col-promoted"},
      {text: entryStateText(ledgerItem), className: "col-fill"},
      {text: probability(prediction.exit_delay_probability), className: "col-delay"},
      {text: pct(prediction.risk_adjusted_utility), className: `col-utility ${tone(prediction.risk_adjusted_utility)}`},
      {content: gate, className: "col-gate", title: gateTitle(item)},
      {content: stateBadge, className: "col-execution", title: reasonText(item.action_reason)}
    ];
  });
  const candidateTable = table([
    {text: "排名", align: "left", className: "col-rank"},
    {text: "代码", align: "left", className: "sticky-code col-code"},
    {text: "股票", align: "left", className: "sticky-name col-name"},
    {text: "条件净收益（10–90分位）", className: "col-return-range"},
    {text: "晋级目标", className: "col-stage"},
    {text: "晋级估计", className: "col-promotion"},
    {text: "行业板块", align: "left", className: "col-industry"},
    {text: "D收盘价", className: "col-d-close"},
    {text: "T日涨跌幅", className: "col-t-return"},
    {text: "是否晋级", className: "col-promoted"},
    {text: "开盘成交", className: "col-fill"},
    {text: "延迟风险", className: "col-delay"},
    {text: "风险调整值", className: "col-utility"},
    {text: "策略门槛", className: "col-gate"},
    {text: "执行状态", className: "col-execution"}
  ], rows, "1450px", "汇集排序候选表");
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
    const batchModel = day.model?.selected_model_id || predictionOf(day.candidates?.[0]).model_id;
    summary.append(
      ledgerFact("信号日", day.decision_date || "—"),
      ledgerFact("开盘买入日", day.buy_date || "—"),
      ledgerFact("T+1验证日", verifiedDate),
      ledgerFact("股票", `${count} 支`),
      ledgerFact("已验证", count ? `${finalCount} / ${count}` : "无候选"),
      ledgerFact("盈利票", count && (day.is_final === true || finalCount > 0) ? `${profitable} / ${count}` : "—"),
      ledgerFact("当日组合净收益", pct(returnValue), tone(returnValue)),
      ledgerFact("当日结果", count ? resultText(day.result, day.is_final) : "合法空选", tone(returnValue)),
      ledgerFact("模型", shortModel(batchModel)),
      node("span", null, "ledger-toggle")
    );
    details.append(summary);
    const detailWrap = node("div", null, "table-wrap");
    const candidates = (day.candidates || []).slice().sort((a, b) => Number(a.rank || 9999) - Number(b.rank || 9999));
    const childRows = candidates.map((item) => {
      const netReturn = item.is_final === true && finite(item.net_return) ? Number(item.net_return) : null;
      const tReturn = verifiedTReturn(item);
      const prediction = predictionOf(item);
      const gate = node(
        "span",
        gateText(item),
        `gate-text ${prediction.gate_decision === "TRADE" ? "trade" : "no-trade"}`
      );
      gate.setAttribute("aria-label", gateTitle(item));
      return [
        {content: stockCell(item), align: "left", className: "sticky-stock"},
        {text: returnForecast(item), className: `col-return-range ${tone(prediction.conditional_net_return_mean)}`},
        {text: entryStateText(item), className: "col-fill"},
        {text: probability(prediction.exit_delay_probability), className: "col-delay"},
        {text: pct(prediction.risk_adjusted_utility), className: `col-utility ${tone(prediction.risk_adjusted_utility)}`},
        {content: gate, className: "col-gate", title: gateTitle(item)},
        {text: item.stage_transition || "—"},
        {text: item.industry || "—", align: "left"},
        {text: price(item.d_close)},
        {text: tDayReturnText(item), className: tone(tReturn)},
        {text: tDayOutcomeText(item, "is_promoted")},
        {text: price(item.buy_price)},
        {text: price(item.exit_price)},
        {text: pct(netReturn), className: tone(netReturn)},
        {text: resultText(item.result, item.is_final), className: tone(netReturn)},
        {content: badge(item.status)}
      ];
    });
    if (childRows.length) {
      detailWrap.tabIndex = 0;
      detailWrap.setAttribute("role", "region");
      detailWrap.setAttribute("aria-label", `${day.decision_date || "该批次"}候选明细，可横向滑动查看全部字段`);
      const dailyTable = table([
        {text: "股票", align: "left", className: "sticky-stock"},
        {text: "条件净收益（10–90分位）", className: "col-return-range"},
        {text: "开盘成交", className: "col-fill"},
        {text: "延迟风险", className: "col-delay"},
        {text: "风险调整值", className: "col-utility"},
        {text: "策略门槛", className: "col-gate"},
        "晋级目标",
        {text: "行业板块", align: "left"},
        "D收盘价",
        "T日涨跌幅",
        "是否晋级",
        "开盘价",
        "T+1退出价",
        "净收益",
        "结果",
        "状态"
      ], childRows, "1760px", `${day.decision_date || "该批次"}候选验证明细`);
      dailyTable.classList.add("daily-detail-table");
      detailWrap.append(dailyTable);
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
    const requestTimestamp = Date.now();
    const [dashboardCandidates, sourceIssueCandidates] = await Promise.all([
      loadSourcePair("dashboard", "dashboard_v1", validate, requestTimestamp),
      loadSourcePair("sourceIssues", "source_issues_v1", validateSourceIssues, requestTimestamp)
    ]);
    const selectedDashboard = chooseNewestSnapshot(dashboardCandidates);
    const selectedIssues = chooseCompanionSnapshot(sourceIssueCandidates, selectedDashboard.source);
    model = selectedDashboard.payload;
    sourceIssues = selectedIssues?.payload || null;
    dataLoadState = {
      dashboard: selectedDashboard,
      sourceIssues: selectedIssues,
      copy: dataSourceCopy(selectedDashboard, dashboardCandidates, selectedIssues)
    };
    $("updated").textContent = `数据更新时间 ${formatUpdatedAt(model.generated_at)}`;
    $("dataSourceStatus").textContent = ` · ${dataLoadState.copy}`;
    const months = availableMonths();
    const select = $("month");
    select.replaceChildren();
    if (!months.length) select.append(new Option("暂无月份", ""));
    months.forEach((month) => select.append(new Option(month, month)));
    select.addEventListener("change", render);
    render();
  } catch (error) {
    $("updated").textContent = String(error.message || error);
    $("dataSourceStatus").textContent = " · Pages / main 双源不可用";
    $("status").replaceChildren(node("div", "看板数据暂不可用，请检查最新工作流与 data/dashboard.v1.json。", "empty"));
  }
}

start();
