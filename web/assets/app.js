/* 策略实验室 dashboard — dependency-light rendering on top of ECharts. */
(function () {
  "use strict";

  const COLORS = {
    up: "#d92b2b",
    down: "#0a9b5a",
    muted: "#76828f",
    line: "#e5e9ef",
    ink: "#18222d",
  };

  const $ = (sel) => document.querySelector(sel);
  const esc = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));

  const fmtMoney = (v) =>
    (v < 0 ? "-" : "") + "¥" + Math.abs(Number(v || 0)).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  const fmtSignedMoney = (v) => (v > 0 ? "+" : v < 0 ? "-" : "") + "¥" + Math.abs(Number(v || 0)).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  const fmtPct = (v, digits = 2) => (v > 0 ? "+" : "") + Number(v || 0).toFixed(digits) + "%";
  const signClass = (v) => (v > 0.0001 ? "up" : v < -0.0001 ? "down" : "flat");
  const shortDate = (d) => String(d || "").slice(5).replace("-", "/");
  const fmtWan = (v) => (Number(v) / 10000).toFixed(1) + "万";

  const state = {
    capital: 100000,
    mode: "ret",
    models: new Set(),
    selected: null,
    data: null,
    families: null,
    family: "llm",
    equityChart: null,
    candleChart: null,
    allocChart: null,
  };

  function sparkline(values, color, width, height) {
    if (!values || values.length < 2) return "";
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const step = width / (values.length - 1);
    const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`);
    return `<svg class="tick-spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><path d="M${pts.join(" L")}" fill="none" stroke="${color}" stroke-width="1.3" stroke-linejoin="round"/></svg>`;
  }

  function modelColor(id) {
    const m = state.data.models.find((x) => x.id === id);
    return m ? m.color : "#5b6774";
  }

  function portfolioById(id) {
    return state.data.portfolios.find((p) => p.id === id);
  }

  function familyKind() {
    return state.data.meta.kind || (state.data.family === "llm" ? "llm" : "quant");
  }

  function renderHeader() {
    const d = state.data;
    const p = d.meta.provenance || {};
    const quant = familyKind() !== "llm";
    $("#as-of").textContent = d.as_of || "--";
    $("#updated-at").textContent = (d.generated_at || "").slice(5, 16);
    $("#status-label").textContent = d.meta.status_label || (quant ? "自训模型 · 5日调仓" : "LLM 模型 · 日线回放");
    $("#footer-sub").textContent =
      `生成于 ${d.generated_at} · ${d.models.length} 个模型 · ${d.portfolios.length} 个组合 · ${d.trade_days} 个交易日 · 起始 ${d.meta.start}`;
    $("#family-note-footer").textContent =
      `${d.meta.method || ""} ｜ ${d.meta.fee_note || ""} ｜ ${p.leakage || ""}`;
    const note = quant
      ? `<b>${esc(d.meta.last_signal || d.meta.end)} 最后一次信号</b> · 训练 ${esc(p.train)} · 验证 ${esc(p.validation)} · 测试 ${esc(p.test)} ｜ ${esc(p.leakage)}`
      : `<b>${esc(d.meta.method || "")}</b> ｜ 回放 ${esc(p.test || "")} ｜ ${esc(p.leakage)}`;
    $("#family-note").innerHTML = note;
  }

  function renderTicker() {
    const items = state.data.market || [];
    $("#ticker").innerHTML = items.map((m) => {
      const cls = signClass(m.chg1);
      const color = m.chg1 >= 0 ? COLORS.up : COLORS.down;
      return `
        <div class="tick">
          <span class="tick-name">${esc(m.name)}</span>
          <span class="tick-val ${cls}">${Number(m.close).toFixed(2)}</span>
          <span class="tick-chg ${cls}">${fmtPct(m.chg1)}</span>
          ${sparkline(m.spark, color, 64, 24)}
        </div>`;
    }).join("");
  }

  function renderKpis() {
    const d = state.data;
    const quant = familyKind() !== "llm";
    const s = d.summary;
    const bench = (d.benchmark || []).filter((x) => x.ret != null).pop();
    const benchRet = bench ? bench.ret : 0;
    const best = [...d.portfolios].sort((a, b) => b.ret_pct - a.ret_pct)[0];
    const bestModel = d.models.find((m) => m.id === best.model_id);
    $("#kpis").innerHTML = `
      <div class="kpi">
        <div class="kpi-label">总资产 <span class="tag">${s.portfolio_count} 个组合</span></div>
        <div class="kpi-value">${fmtMoney(s.current)}</div>
        <div class="kpi-sub">初始 ${fmtMoney(s.initial)} · <b class="${signClass(s.ret_pct)}">${fmtPct(s.ret_pct)}</b></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">今日盈亏 <span class="tag">${esc(d.as_of)}</span></div>
        <div class="kpi-value ${signClass(s.today_pnl)}">${fmtSignedMoney(s.today_pnl)}</div>
        <div class="kpi-sub">组合平均 <b class="${signClass(s.today_pct)}">${fmtPct(s.today_pct)}</b> · 收盘结算</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">领先组合</div>
        <div class="kpi-value ${signClass(best.ret_pct)}">${fmtPct(best.ret_pct)}</div>
        <div class="kpi-sub"><b>${esc(bestModel ? bestModel.short : "")}</b> · ${esc(best.capital_label)}本金</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">${esc(d.meta.benchmark_label || "沪深300 同期")}</div>
        <div class="kpi-value ${signClass(benchRet)}">${fmtPct(benchRet)}</div>
        <div class="kpi-sub">组合相对基准 <b class="${signClass(s.ret_pct - benchRet)}">${fmtPct(s.ret_pct - benchRet)}</b></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">运行交易日</div>
        <div class="kpi-value">${d.trade_days}<small>天</small></div>
        <div class="kpi-sub">${d.models.length} 个模型 × 4 档本金 · ${esc(d.meta.kpi_unit || (quant ? "5 日调仓" : "日线回放"))}</div>
      </div>`;
  }

  function renderFamilyCompare() {
    const element = $("#family-compare");
    if (!element) return;
    const windowStart = "2026-09-01";
    const rows = [];
    for (const [key, family] of Object.entries(state.families || {})) {
      let best = null;
      for (const portfolio of family.portfolios || []) {
        if (Number(portfolio.capital) !== 100000) continue;
        const series = (portfolio.series || []).filter((point) => point.date >= windowStart);
        if (series.length < 2) continue;
        const ret = (series[series.length - 1].total / series[0].total - 1) * 100;
        if (!best || ret > best.ret) best = { portfolio, series, ret };
      }
      if (!best) continue;
      const totals = best.series.map((point) => point.total);
      let peak = totals[0];
      let maxDrawdown = 0;
      for (const value of totals) {
        peak = Math.max(peak, value);
        maxDrawdown = Math.min(maxDrawdown, value / peak - 1);
      }
      const daily = [];
      for (let index = 1; index < totals.length; index += 1) {
        if (totals[index - 1]) daily.push(totals[index] / totals[index - 1] - 1);
      }
      const mean = daily.reduce((sum, value) => sum + value, 0) / (daily.length || 1);
      const variance = daily.length > 1 ? daily.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (daily.length - 1) : 0;
      const sharpe = variance > 0 ? (mean / Math.sqrt(variance)) * Math.sqrt(252) : 0;
      rows.push({
        key,
        label: family.label || key,
        model: (family.models.find((item) => item.id === best.portfolio.model_id) || {}).short || best.portfolio.model_id,
        ret: best.ret,
        maxDrawdown: maxDrawdown * 100,
        sharpe,
        start: best.series[0].date,
        end: best.series[best.series.length - 1].date,
      });
    }
    if (!rows.length) {
      element.innerHTML = "";
      return;
    }
    element.innerHTML = `
      <div class="card-head">
        <div>
          <h2>家族对比 · ${windowStart} 起</h2>
          <p>统一 10 万本金与区间；各家族标的池、调仓频率与费用口径不同，收益不可直接比较</p>
        </div>
      </div>
      <table class="compare-table">
        <thead><tr><th>家族</th><th>代表模型</th><th class="num">区间收益</th><th class="num">最大回撤</th><th class="num">夏普</th><th class="num">区间</th></tr></thead>
        <tbody>${rows.map((row) => `
          <tr>
            <td><b>${esc(row.label)}</b></td>
            <td>${esc(row.model)}</td>
            <td class="num ${signClass(row.ret)}">${fmtPct(row.ret)}</td>
            <td class="num ${signClass(row.maxDrawdown)}">${fmtPct(row.maxDrawdown)}</td>
            <td class="num">${row.sharpe.toFixed(2)}</td>
            <td class="num" style="font-size:11px;color:#8c98a5">${esc(row.start.slice(5))} ~ ${esc(row.end.slice(5))}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  }

  function renderCapitalSegments() {
    const container = $("#capital-seg");
    container.innerHTML = Object.entries(state.data.meta.capital_labels)
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .map(([value, label]) => `<button type="button" data-capital="${value}" class="${Number(value) === state.capital ? "active" : ""}">${label}</button>`)
      .join("");
    container.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.capital = Number(btn.dataset.capital);
        if (state.selected) {
          const same = portfolioById(`${state.selected.model_id}__${state.capital}`);
          if (same) state.selected = same;
        }
        renderCapitalSegments();
        renderLegend();
        renderEquityChart();
        renderRanking();
        renderDetail();
      });
    });
  }

  function visibleModels() {
    return state.data.models.filter((m) => state.models.has(m.id));
  }

  function renderLegend() {
    const container = $("#legend");
    const items = state.data.models.map((m) => `
      <span class="legend-item ${state.models.has(m.id) ? "" : "off"}" data-model="${m.id}" role="button" tabindex="0">
        <i style="background:${m.color}"></i>${esc(m.name)}
      </span>`).join("");
    container.innerHTML = items + `<span class="legend-item benchmark"><i></i>${esc(state.data.meta.benchmark_label || "沪深300")}</span>`;
    container.querySelectorAll("[data-model]").forEach((el) => {
      const toggle = () => {
        const id = el.dataset.model;
        if (state.models.has(id)) state.models.delete(id);
        else state.models.add(id);
        renderLegend();
        renderEquityChart();
      };
      el.addEventListener("click", toggle);
      el.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
    });
    const cap = state.data.meta.capital_labels[String(state.capital)];
    const method = state.data.meta.chart_method || (familyKind() === "llm" ? "09/01 起回放" : "5 日调仓");
    $("#chart-sub").textContent = state.mode === "ret"
      ? `${state.data.label} · ${cap} 本金收益率对比 · ${method}（点击图例可隐藏）`
      : `${state.data.label} · ${cap} 本金账户净值对比 · ${method}`;
  }

  function renderModeSegments() {
    const container = $("#mode-seg");
    container.querySelectorAll("button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === state.mode);
      btn.onclick = () => {
        state.mode = btn.dataset.mode;
        renderModeSegments();
        renderLegend();
        renderEquityChart();
      };
    });
  }

  function renderEquityChart() {
    const chart = state.equityChart;
    const dates = state.data.dates;
    const models = visibleModels();
    const series = models.map((m) => {
      const portfolio = portfolioById(`${m.id}__${state.capital}`);
      if (!portfolio) return null;
      return {
        name: m.name,
        type: "line",
        data: portfolio.series.map((p) => (state.mode === "ret" ? p.ret : p.total)),
        showSymbol: false,
        symbol: "circle",
        symbolSize: 4,
        lineStyle: { width: 2, color: m.color },
        itemStyle: { color: m.color },
        emphasis: { focus: "series" },
        smooth: false,
      };
    }).filter(Boolean);

    if (state.mode === "ret") {
      series.push({
        name: state.data.meta.benchmark_label || "沪深300",
        type: "line",
        data: dates.map((day) => {
          const row = state.data.benchmark.find((b) => b.date === day);
          return row ? row.ret : null;
        }),
        showSymbol: false,
        lineStyle: { width: 1.4, type: "dashed", color: "#aab4bf" },
        itemStyle: { color: "#aab4bf" },
        emphasis: { focus: "series" },
        connectNulls: true,
      });
    }

    chart.setOption({
      animationDuration: 450,
      color: state.data.models.map((m) => m.color),
      grid: { left: 42, right: 16, top: 14, bottom: 26 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(22,40,63,.94)",
        borderWidth: 0,
        padding: [8, 10],
        confine: true,
        textStyle: { color: "#fff", fontSize: 11 },
        axisPointer: { type: "line", lineStyle: { color: "#c8d0d9" } },
        valueFormatter: (value) => (state.mode === "ret" ? fmtPct(value) : fmtMoney(value)),
      },
      xAxis: {
        type: "category",
        data: dates.map(shortDate),
        boundaryGap: false,
        axisLine: { lineStyle: { color: COLORS.line } },
        axisTick: { show: false },
        axisLabel: { color: "#8c98a5", fontSize: 10, interval: Math.max(0, Math.floor(dates.length / 8) - 1) },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: "#f0f3f6" } },
        axisLabel: {
          color: "#8c98a5", fontSize: 10,
          formatter: (value) => (state.mode === "ret" ? `${value > 0 ? "+" : ""}${Number(value).toFixed(1)}%` : fmtWan(value)),
        },
      },
      series,
    }, { notMerge: true });
    chart.resize();
  }

  function renderRanking() {
    const capex = state.data.portfolios;
    const sorted = [...capex].sort((a, b) => b.ret_pct - a.ret_pct);
    $("#rank-count").textContent = `${sorted.length} 个`;
    $("#rank-sub").textContent = "按累计收益率排序 · 点击查看持仓";
    $("#ranking").innerHTML = sorted.map((p, index) => {
      const m = state.data.models.find((x) => x.id === p.model_id);
      return `
        <div class="rank-row ${state.selected && state.selected.id === p.id ? "active" : ""}" data-id="${esc(p.id)}" tabindex="0" role="button" aria-label="${esc(p.model_name)} ${esc(p.capital_label)}本金">
          <span class="rank-no">${String(index + 1).padStart(2, "0")}</span>
          <span class="rank-name"><b><i class="model-dot" style="display:inline-block;margin-right:6px;background:${m ? m.color : "#999"}"></i>${esc(m ? m.short : p.model_name)}</b><span>${esc(p.capital_label)}本金</span></span>
          <span class="rank-ret ${signClass(p.ret_pct)}">${fmtPct(p.ret_pct)}</span>
          <span class="rank-today ${signClass(p.today_pct)}">${fmtSignedMoney(p.today_pnl)}</span>
        </div>`;
    }).join("");
    $("#ranking").querySelectorAll(".rank-row").forEach((row) => {
      const select = () => {
        const p = portfolioById(row.dataset.id);
        if (!p) return;
        state.selected = p;
        renderRanking();
        renderDetail();
        document.getElementById("detail").scrollIntoView({ behavior: "smooth", block: "start" });
      };
      row.addEventListener("click", select);
      row.addEventListener("keydown", (event) => { if (event.key === "Enter") select(); });
    });
  }

  function renderModelTable() {
    const models = state.data.models;
    const rows = models.map((m) => {
      const best = [...state.data.portfolios].filter((p) => p.model_id === m.id).sort((a, b) => b.ret_pct - a.ret_pct)[0];
      const totalTrades = state.data.portfolios.filter((p) => p.model_id === m.id).reduce((sum, p) => sum + p.metrics.trades, 0);
      const pct = m.avg_ret;
      const barWidth = Math.min(100, Math.max(4, 50 + pct * 1.6));
      const color = m.color;
      return `
        <div class="model-row">
          <div class="model-name"><i class="model-dot" style="background:${color}"></i><div><b>${esc(m.name)}</b><small>${esc(m.provider)} · ${esc(m.note)}</small></div></div>
          <div class="num ${signClass(pct)}"><b>${fmtPct(pct)}</b><div class="mini-bar"><i style="width:${barWidth}%;background:${color}"></i></div></div>
          <div class="num">${fmtPct(best ? best.ret_pct : 0)}<div style="font-size:10px;color:#8c98a5">${best ? esc(best.capital_label) : "-"}</div></div>
          <div class="num">${totalTrades}<span style="font-size:10px;color:#8c98a5"> 笔</span></div>
          <div class="num">${familyKind() === "llm" ? Math.round((m.confidence || 0) * 100) + "<span style=\"font-size:10px;color:#8c98a5\">%</span>" : Number(m.ic || 0).toFixed(3)}</div>
          <div class="num" style="font-size:10px;color:#8c98a5">${esc((m.decision_date || "").slice(5))}</div>
        </div>`;
    }).join("");
    const metricLabel = familyKind() === "llm" ? "置信度" : (state.data.meta.metric_label || "Rank IC");
    $("#model-table").innerHTML = `
      <div class="model-row head"><div>模型</div><div class="num">四档平均</div><div class="num">最佳本金</div><div class="num">交易</div><div class="num">${metricLabel}</div><div class="num">决策日</div></div>
      ${rows}`;
  }

  function renderDecisions() {
    const models = state.data.models;
    const local = familyKind() !== "llm";
    const latestDate = models.map((m) => m.decision_date).filter(Boolean).sort().pop() || "";
    $("#decision-sub").textContent = local
      ? `最后一次信号 ${state.data.meta.last_signal} · 权益估值至 ${state.data.as_of}`
      : `${latestDate} 收盘后生成 · 下一交易日开盘执行`;
    $("#decisions").innerHTML = models.map((m) => {
      const p = portfolioById(`${m.id}__100000`) || state.data.portfolios.find((x) => x.model_id === m.id);
      const decision = p ? p.decision : null;
      const targets = decision ? decision.targets.slice(0, 4) : [];
      const chips = targets.map((t) => `<span class="chip">${esc(t.name)} ${Number(t.weight).toFixed(0)}%</span>`).join("") +
        (decision && decision.targets.length > 4 ? `<span class="chip more">+${decision.targets.length - 4}</span>` : "");
      const view = decision && decision.market_view ? decision.market_view : (decision && decision.error ? "本次决策失败，组合保持持仓。" : "暂无决策记录");
      return `
        <div class="decision-card">
          <div class="decision-top">
            <i class="model-dot" style="background:${m.color}"></i><b>${esc(m.short)}</b>
            <span class="conf">${local ? "Rank IC " + Number(m.ic || 0).toFixed(3) : "置信度 " + Math.round((decision ? decision.confidence : 0) * 100) + "%"}</span>
          </div>
          <div class="decision-view">${esc(view)}</div>
          <div class="chips">${chips || '<span class="chip">空仓观望</span>'}</div>
        </div>`;
    }).join("");
  }

  function renderDetail() {
    const p = state.selected;
    if (!p) return;
    const m = state.data.models.find((x) => x.id === p.model_id);
    $("#detail-dot").style.background = m ? m.color : "#999";
    $("#detail-name").textContent = `${p.model_name} · ${p.capital_label}本金`;
    $("#detail-sub").textContent = `${esc(p.id)} · 截至 ${state.data.as_of} · 现金 ${fmtMoney(p.cash)}（${((p.cash / (p.total || 1)) * 100).toFixed(1)}%）`;
    const metrics = [
      ["总资产", fmtMoney(p.total), ""],
      ["累计收益", fmtPct(p.ret_pct), signClass(p.ret_pct)],
      ["今日盈亏", fmtSignedMoney(p.today_pnl), signClass(p.today_pnl)],
      ["最大回撤", fmtPct(p.metrics.max_drawdown), signClass(p.metrics.max_drawdown)],
      ["夏普比率", Number(p.metrics.sharpe).toFixed(2), ""],
      ["胜率", `${Number(p.metrics.win_rate).toFixed(0)}%`, ""],
    ];
    $("#detail-kpis").innerHTML = metrics.map(([k, v, cls]) => `<div class="dk"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`).join("");

    const holdings = p.positions;
    $("#holdings-note").textContent = `${holdings.length} 只持仓 · 交易 ${p.metrics.trades} 笔 · 换手 ${Number(p.metrics.turnover).toFixed(1)}x`;
    $("#holdings-table tbody").innerHTML = holdings.length ? holdings.map((h) => `
      <tr>
        <td class="name">${esc(h.name)}<span class="sym">${esc(h.symbol)}</span></td>
        <td class="num">${h.shares}</td>
        <td class="num">${Number(h.avg_cost).toFixed(2)}</td>
        <td class="num">${Number(h.price).toFixed(2)}</td>
        <td class="num">${fmtMoney(h.value)}</td>
        <td class="num ${signClass(h.pnl)}">${fmtSignedMoney(h.pnl)}<span style="font-size:10px;margin-left:3px">${fmtPct(h.pnl_pct)}</span></td>
        <td class="num">${Number(h.weight).toFixed(1)}%</td>
      </tr>`).join("") : `<tr><td colspan="7" class="empty">当前空仓，等待模型给出建仓信号</td></tr>`;

    $("#trades-table tbody").innerHTML = p.trades.length ? p.trades.map((t) => `
      <tr>
        <td>${esc(t.date.slice(5))}</td>
        <td class="name">${esc(t.name)}<span class="sym">${esc(t.symbol)}</span></td>
        <td class="${t.side === "buy" ? "side-buy" : "side-sell"}">${t.side === "buy" ? "买入" : "卖出"}</td>
        <td class="num">${t.shares}</td>
        <td class="num">${Number(t.price).toFixed(2)}</td>
        <td class="num">${fmtMoney(t.amount)}</td>
        <td class="reason">${esc(t.reason || "-")}</td>
      </tr>`).join("") : `<tr><td colspan="7" class="empty">暂无成交记录</td></tr>`;

    const decision = p.decision;
    if (!decision) {
      $("#detail-decision").innerHTML = `<div class="empty">暂无决策记录</div>`;
    } else {
      const targets = decision.targets.map((t) => `
        <div class="target"><b>${esc(t.name)}</b><em>${esc(t.reason || "")}</em><span>${Number(t.weight).toFixed(0)}%</span></div>`).join("");
      $("#detail-decision").innerHTML = `
        <div class="meta">${esc(decision.date)} ${familyKind() === "llm" ? "收盘" : (state.data.meta.decision_label || "最后一次信号")} · ${familyKind() === "llm" ? "置信度 " + Math.round((decision.confidence || 0) * 100) + "%" : "Rank IC " + Number((state.data.models.find((x) => x.id === p.model_id) || {}).ic || 0).toFixed(3)}</div>
        <div class="view">${esc(decision.market_view || "—")}</div>
        ${decision.error ? `<div class="view" style="color:#b08a00">校验提示：${esc(decision.error)}</div>` : ""}
        ${targets || '<div class="empty">空仓观望</div>'}`;
    }

    renderAlloc(p);
  }

  function renderAlloc(p) {
    const items = p.positions.map((h) => ({ name: h.name, value: h.value }));
    const cash = Math.max(0, p.cash);
    if (cash > 0) items.push({ name: "现金", value: cash });
    const colorsLen = items.length;
    const palette = state.data.models.map((m) => m.color);
    state.allocChart.setOption({
      animationDuration: 350,
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(22,40,63,.94)",
        borderWidth: 0,
        textStyle: { color: "#fff", fontSize: 11 },
        formatter: (params) => `${esc(params.name)}<br/>${fmtMoney(params.value)} · ${params.percent}%`,
      },
      series: [{
        type: "pie",
        radius: ["54%", "76%"],
        center: ["50%", "50%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 3 },
        label: { show: false },
        labelLine: { show: false },
        color: ["#d92b2b", "#1f6feb", "#0a9b5a", "#f08c00", "#7b2ff2", "#00a6a6", "#8b6b3f", "#5b6774", "#c3cbd4"],
        data: items.slice(0, 8),
      }],
      graphic: [{
        type: "text", left: "center", top: "45%",
        style: { text: "仓位", fill: "#8c98a5", fontSize: 10, textAlign: "center" },
      }, {
        type: "text", left: "center", top: "52%",
        style: { text: `${(100 - (cash / (p.total || 1)) * 100).toFixed(0)}%`, fill: COLORS.ink, fontSize: 18, fontWeight: 700, textAlign: "center" },
      }],
    }, { notMerge: true });
    state.allocChart.resize();
    if (colorsLen === 0) state.allocChart.clear();
  }

  function renderCandles() {
    const c = state.data.candles;
    if (!c || !c.dates.length) return;
    const dates = c.dates.map(shortDate);
    const values = c.values;
    const closes = values.map((v) => v[1]);
    const ma = (n) => closes.map((_, i) => {
      if (i < n - 1) return null;
      let sum = 0;
      for (let j = i - n + 1; j <= i; j += 1) sum += closes[j];
      return +(sum / n).toFixed(2);
    });
    state.candleChart.setOption({
      animationDuration: 300,
      grid: [
        { left: 46, right: 16, top: 12, height: "62%" },
        { left: 46, right: 16, top: "78%", height: "15%" },
      ],
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: "rgba(22,40,63,.94)",
        borderWidth: 0,
        textStyle: { color: "#fff", fontSize: 11 },
      },
      xAxis: [
        { type: "category", data: dates, boundaryGap: true, axisLine: { lineStyle: { color: COLORS.line } }, axisTick: { show: false }, axisLabel: { show: false } },
        { type: "category", gridIndex: 1, data: dates, axisLine: { lineStyle: { color: COLORS.line } }, axisTick: { show: false }, axisLabel: { color: "#8c98a5", fontSize: 9, interval: 9 } },
      ],
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: "#f0f3f6" } }, axisLabel: { color: "#8c98a5", fontSize: 9 }, axisLine: { show: false } },
        { gridIndex: 1, scale: true, splitNumber: 1, axisLabel: { show: false }, splitLine: { show: false }, axisLine: { show: false } },
      ],
      series: [
        {
          type: "candlestick", data: values,
          itemStyle: { color: COLORS.up, color0: COLORS.down, borderColor: COLORS.up, borderColor0: COLORS.down },
        },
        { name: "MA5", type: "line", data: ma(5), showSymbol: false, lineStyle: { width: 1, color: "#f08c00" } },
        { name: "MA20", type: "line", data: ma(20), showSymbol: false, lineStyle: { width: 1, color: "#1f6feb" } },
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: c.volumes, itemStyle: { color: "#c9d2dc" } },
      ],
    }, { notMerge: true });
    state.candleChart.resize();
  }

  function renderUniverse() {
    const local = familyKind() !== "llm";
    const items = local ? [...state.data.universe] : [...state.data.universe].sort((a, b) => b.chg1 - a.chg1);
    $("#universe-title").textContent = state.data.meta.universe_title || "候选池行情";
    $("#breadth").textContent = `涨 ${state.data.breadth.up} · 跌 ${state.data.breadth.down}`;
    $("#universe").innerHTML = items.map((u) => {
      const cls = signClass(u.chg1);
      const color = u.chg1 >= 0 ? COLORS.up : COLORS.down;
      return `
        <div class="uni" title="${esc(u.name)} · 5日 ${fmtPct(u.chg5)} · 20日 ${fmtPct(u.chg20)}${u.score != null ? " · 分数 " + Number(u.score).toFixed(2) : ""}">
          <div class="uni-name"><b>${esc(u.name)}</b><span>${esc(u.sector)}${u.score != null ? " · " + Number(u.score).toFixed(2) : ""}</span></div>
          ${sparkline(u.spark, color, 46, 18).replace("tick-spark", "uni-spark")}
          <span class="uni-chg ${cls}">${fmtPct(u.chg1, 1)}</span>
        </div>`;
    }).join("");
  }

  function bindResize() {
    let timer = null;
    window.addEventListener("resize", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.equityChart && state.equityChart.resize();
        state.candleChart && state.candleChart.resize();
        state.allocChart && state.allocChart.resize();
      }, 120);
    });
  }

  function initCharts() {
    state.equityChart = echarts.init($("#equity-chart"), null, { renderer: "canvas" });
    state.candleChart = echarts.init($("#candle-chart"), null, { renderer: "canvas" });
    state.allocChart = echarts.init($("#alloc-chart"), null, { renderer: "canvas" });
  }

  function renderFamilyTabs() {
    const keys = Object.keys(state.families);
    const order = ["local", "multifactor", "llm"].filter((key) => keys.includes(key));
    for (const key of keys) if (!order.includes(key)) order.push(key);
    $("#family-tabs").innerHTML = order.map((key) => {
      const family = state.families[key];
      return `<button type="button" class="family-tab ${key === state.family ? "active" : ""}" data-family="${key}" role="tab" aria-selected="${key === state.family}">${esc(family.label || key)}</button>`;
    }).join("");
    $("#family-tabs").querySelectorAll("[data-family]").forEach((btn) => {
      btn.onclick = () => switchFamily(btn.dataset.family);
    });
  }

  function choosePortfolio() {
    const best = [...state.data.portfolios].sort((a, b) => b.ret_pct - a.ret_pct)[0];
    state.selected = best || state.data.portfolios[0] || null;
  }

  function renderDashboard() {
    state.models = new Set(state.data.models.map((m) => m.id));
    choosePortfolio();
    renderHeader();
    renderTicker();
    renderKpis();
    renderFamilyCompare();
    renderCapitalSegments();
    renderModeSegments();
    renderLegend();
    renderRanking();
    renderModelTable();
    renderDecisions();
    renderUniverse();
    renderEquityChart();
    renderCandles();
    renderDetail();
  }

  function switchFamily(key) {
    if (!state.families[key] || key === state.family) return;
    state.family = key;
    state.data = state.families[key];
    state.capital = 100000;
    state.mode = "ret";
    renderFamilyTabs();
    renderDashboard();
    const url = new URL(window.location.href);
    url.searchParams.set("family", key);
    window.history.replaceState(null, "", url);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function boot() {
    try {
      const response = await fetch("./data.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      state.families = payload.families || { llm: payload };
      const requested = new URLSearchParams(window.location.search).get("family");
      state.family = requested && state.families[requested]
        ? requested
        : (payload.default_family && state.families[payload.default_family] ? payload.default_family : Object.keys(state.families)[0]);
      state.data = state.families[state.family];
    } catch (error) {
      $("#kpis").innerHTML = `<div class="card" style="grid-column:1/-1;padding:24px">数据加载失败：${esc(error.message)}。请先运行 <code>python3 -m paper_trader.runner export</code>。</div>`;
      return;
    }
    initCharts();
    renderFamilyTabs();
    renderDashboard();
    bindResize();
  }

  boot();
})();
