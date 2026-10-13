/* Portfolio Manager front end: renders the dashboard and position pages from the
   JSON API, and adds small conveniences to the transaction form. */
(() => {
  "use strict";

  const root = document.getElementById("app");
  const BASE = root?.dataset.base || "CAD";
  const REFRESH_MS = 60_000;
  const SECTOR_COLORS = [
    "#2457d6", "#e07a00", "#1a9e77", "#c2408c", "#7b5cd6", "#d9a400",
    "#2b9ec4", "#d6503e", "#5c8f2e", "#9c6b45", "#3f8f8f", "#4d5ccf",
  ];

  // ---- formatting --------------------------------------------------------------
  const numberFmt = new Intl.NumberFormat("en-CA", { maximumFractionDigits: 6 });
  const moneyFmts = {};
  function money(value, currency = BASE, { signed = false, compact = false } = {}) {
    if (value == null || Number.isNaN(value)) return "—";
    const key = `${currency}|${compact}`;
    if (!moneyFmts[key]) {
      try {
        moneyFmts[key] = new Intl.NumberFormat("en-CA", {
          style: "currency", currency, currencyDisplay: "narrowSymbol",
          maximumFractionDigits: compact ? 0 : 2, minimumFractionDigits: compact ? 0 : 2,
        });
      } catch {
        moneyFmts[key] = { format: (v) => `${v.toFixed(2)} ${currency}` };
      }
    }
    const text = moneyFmts[key].format(Math.abs(value));
    const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
    return sign + text + (currency !== BASE ? ` ${currency}` : "");
  }
  function pct(value, { signed = true } = {}) {
    if (value == null || Number.isNaN(value)) return "—";
    const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
    return `${sign}${Math.abs(value * 100).toFixed(2)}%`;
  }
  const qty = (v) => (v == null ? "—" : numberFmt.format(v));
  const tone = (v) => (v > 0 ? "gain" : v < 0 ? "loss" : "");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const $ = (id) => document.getElementById(id);

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("Signed out");
    }
    if (!res.ok) {
      let message = `Request failed (${res.status})`;
      try { message = (await res.json()).error || message; } catch { /* not JSON */ }
      throw new Error(message);
    }
    return res.json();
  }

  // Warnings can be closed for good: the server remembers the text and stops sending it.
  function notices(items) {
    $("notices").innerHTML = items
      .filter(Boolean)
      .map(([kind, text]) => kind === "warning"
        ? `<div class="alert alert-${kind} alert-dismissible"><span>${esc(text)}</span>
            <button type="button" class="alert-close" aria-label="Dismiss this message" title="Dismiss"
              data-dismiss="${esc(text)}">&times;</button></div>`
        : `<div class="alert alert-${kind}">${esc(text)}</div>`)
      .join("");
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".alert-close");
    if (!button) return;
    const alert = button.closest(".alert");
    alert.hidden = true;
    try {
      const res = await fetch("/api/warnings/dismiss", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "",
        },
        body: JSON.stringify({ message: button.dataset.dismiss }),
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      alert.remove();
    } catch {
      alert.hidden = false;  // not saved: keep it visible so it can be tried again
    }
  });

  function kpi(label, value, sub = "", valueTone = "") {
    return `<div class="kpi"><div class="kpi-label">${esc(label)}</div>
      <div class="kpi-value ${valueTone}" title="${esc(value)}">${esc(value)}</div>
      <div class="kpi-sub">${sub}</div></div>`;
  }

  function chartDefaults() {
    if (!window.Chart) return;
    Chart.defaults.color = cssVar("--muted");
    Chart.defaults.borderColor = cssVar("--border");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  }

  function dateTicks(labels) {
    return {
      maxTicksLimit: 7,
      autoSkip: true,
      maxRotation: 0,
      callback(value) {
        const d = new Date(`${labels[value]}T00:00:00`);
        return d.toLocaleDateString("en-CA", { month: "short", year: "2-digit" });
      },
    };
  }

  // ---- performance section (dashboard and Performance tab) -------------------
  // Growth / returns chart with time ranges, and the period returns table.
  // onHistory(history, error) runs once the history has loaded (or failed), for extra sections.
  function performanceSection(urls, benchmark, { onHistory } = {}) {
    let history = null;
    let mode = "growth";
    let range = "All";
    let perfChart = null;

    async function loadHistory() {
      try {
        history = await getJSON(urls.historyUrl);
        $("perf-status").hidden = true;
        renderPeriods();
        renderPerf();
      } catch (err) {
        $("perf-status").textContent = `Couldn't build history: ${err.message}`;
        onHistory?.(null, err);
        return;
      }
      onHistory?.(history, null);
    }

    function rangeStart(points) {
      if (range === "All" || !points.length) return 0;
      const end = new Date(`${points[points.length - 1].date}T00:00:00`);
      const start = new Date(end);
      if (range === "YTD") start.setMonth(0, 1);
      else start.setMonth(end.getMonth() - { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "5Y": 60 }[range]);
      const iso = start.toISOString().slice(0, 10);
      const i = points.findIndex((p) => p.date >= iso);
      return Math.max(i - 1, 0);
    }

    function renderPerf() {
      const all = history.points;
      if (!all.length) return;
      const pts = all.slice(rangeStart(all));
      const labels = pts.map((p) => p.date);
      const c1 = cssVar("--chart-1"), c2 = cssVar("--chart-2"), c3 = cssVar("--chart-3");
      let datasets, yFormat, note;
      if (mode === "growth") {
        datasets = [
          { label: "Portfolio value", data: pts.map((p) => p.value), borderColor: c1, backgroundColor: `${c1}22`, fill: true, borderWidth: 2 },
          { label: "Net contributions", data: pts.map((p) => p.contributions), borderColor: c3, borderDash: [4, 4], borderWidth: 1.5, stepped: true },
          { label: `Same contributions in ${benchmark}`, data: pts.map((p) => p.benchmark_value ?? null), borderColor: c2, borderWidth: 1.5 },
        ];
        yFormat = (v) => money(v, BASE, { compact: true });
        note = `Dashed line: money you put in. Orange: what the same deposits would be worth if each had bought ${benchmark}.`;
      } else if (mode === "benchmarks") {
        // Both lines start at the portfolio's value on the first day of the range. The benchmark line
        // invests that amount in the benchmark that day, then buys or sells it with the same deposits and
        // withdrawals as the portfolio. benchmark_twr tracks the benchmark's price, so 1 + it works as one.
        const start = pts[0];
        let units = start.benchmark_twr == null ? null : start.value / (1 + start.benchmark_twr);
        const benchLine = pts.map((p, i) => {
          if (units == null || p.benchmark_twr == null) return null;
          const index = 1 + p.benchmark_twr;
          if (i > 0) units += (p.contributions - pts[i - 1].contributions) / index;
          return units * index;
        });
        datasets = [
          { label: "Portfolio value", data: pts.map((p) => p.value), borderColor: c1, backgroundColor: `${c1}22`, fill: true, borderWidth: 2 },
          { label: "Starting value + net contributions", data: pts.map((p) => start.value + p.contributions - start.contributions),
            borderColor: c3, borderDash: [4, 4], borderWidth: 1.5, stepped: true },
          { label: `Same money in ${benchmark}`, data: benchLine, borderColor: c2, borderWidth: 1.5 },
        ];
        yFormat = (v) => money(v, BASE, { compact: true });
        note = `Both lines start at your portfolio's value on the first day shown. Orange: that amount invested in ${benchmark} that day, with the same deposits and withdrawals since.`;
      } else {
        const b = pts[0];
        const rebase = (end, start) => (end == null || start == null ? null : (1 + end) / (1 + start) - 1);
        datasets = [
          { label: "Portfolio (time-weighted)", data: pts.map((p) => rebase(p.twr, b.twr)), borderColor: c1, borderWidth: 2 },
          { label: benchmark, data: pts.map((p) => rebase(p.benchmark_twr, b.benchmark_twr)), borderColor: c2, borderWidth: 1.5 },
        ];
        yFormat = (v) => pct(v);
        note = "Time-weighted return removes the effect of deposits and withdrawals, so it compares directly with the index.";
      }
      $("perf-note").textContent = note;
      datasets.forEach((d) => Object.assign(d, { pointRadius: 0, pointHoverRadius: 3, tension: 0.15 }));
      const config = {
        type: "line",
        data: { labels, datasets },
        options: {
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { grid: { display: false }, ticks: dateTicks(labels) },
            y: { ticks: { callback: yFormat }, grace: "5%" },
          },
          plugins: {
            legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 2, usePointStyle: false } },
            tooltip: {
              callbacks: {
                title: (items) => new Date(`${items[0].label}T00:00:00`).toLocaleDateString("en-CA", { dateStyle: "medium" }),
                label: (c) => ` ${c.dataset.label}: ${c.parsed.y == null ? "—" : yFormat(c.parsed.y)}`,
              },
            },
          },
        },
      };
      if (perfChart) perfChart.destroy();
      perfChart = new Chart($("perf-chart"), config);
    }

    function renderPeriods() {
      const rows = history.periods;
      if (!rows.length) return;
      const cells = (fn) => rows.map((r) => `<td class="num">${fn(r)}</td>`).join("");
      const diff = (r) => (r.portfolio == null || r.benchmark == null ? null : r.portfolio - r.benchmark);
      const head = (r) => r.label === "All" ? "<th class=\"num\">Since inception</th>"
        : r.label === "Annualized" ? "<th class=\"num\" title=\"Compound yearly return since inception. Shown once there's at least a year of history.\">Annualized</th>"
        : `<th class="num">${esc(r.label)}</th>`;
      $("period-table").innerHTML = `
        <thead><tr><th></th>${rows.map(head).join("")}</tr></thead>
        <tbody>
          <tr><td>Portfolio</td>${cells((r) => `<span class="${tone(r.portfolio)}">${pct(r.portfolio)}</span>`)}</tr>
          <tr><td>${esc(benchmark)}</td>${cells((r) => `<span class="${tone(r.benchmark)}">${pct(r.benchmark)}</span>`)}</tr>
          <tr><td>Difference</td>${cells((r) => `<span class="${tone(diff(r))}">${pct(diff(r))}</span>`)}</tr>
        </tbody>`;
    }

    function bindSeg(id, attr, onChange) {
      $(id).addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        $(id).querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        onChange(btn.dataset[attr]);
        if (history) renderPerf();
      });
    }
    bindSeg("chart-mode", "mode", (v) => { mode = v; });
    bindSeg("chart-range", "range", (v) => { range = v; });

    loadHistory();
  }

  // ---- dashboard ---------------------------------------------------------------
  function dashboard() {
    const urls = root.dataset;
    const benchmark = urls.benchmark;
    const allocCharts = {};
    let holdings = [];
    let sort = { key: "market_value", dir: -1 };
    // Holdings and Upcoming earnings show their first PREVIEW_ROWS rows until "Show all" is clicked.
    const PREVIEW_ROWS = 5;
    let holdingsExpanded = false;
    let earningsExpanded = false;
    let earningsList = [];
    const moreButton = (count, expanded, controls, noun) => (count > PREVIEW_ROWS
      ? `<button type="button" class="btn btn-secondary btn-sm" aria-controls="${controls}" aria-expanded="${expanded}">
          ${expanded ? "Show fewer" : `Show all ${count} ${noun}`}</button>`
      : "");
    let lastSnapshot = null;

    chartDefaults();

    // Portfolio snapshot ------------------------------------------------------
    async function loadPortfolio() {
      try {
        const data = await getJSON(urls.portfolioUrl);
        lastSnapshot = data;
        renderSnapshot(data);
      } catch (err) {
        $("as-of").textContent = `Couldn't load prices: ${err.message}`;
      }
    }

    function renderSnapshot(p) {
      const t = p.totals;
      const updated = new Date(p.as_of).toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit" });
      $("as-of").textContent = `Live prices from Yahoo Finance · updated ${updated} · values in ${BASE}`;
      notices([
        p.holdings_only && ["info", "No deposits or withdrawals are recorded, so buys are treated as contributions and cash balances are not included in the portfolio value."],
        ...p.warnings.map((w) => ["warning", w]),
      ]);

      $("kpis").innerHTML = [
        kpi("Total value", money(t.total_value),
          p.holdings_only ? `${money(t.market_value)} in holdings` : `${money(t.market_value)} invested · ${money(t.cash)} cash`),
        kpi("Today", money(t.day_change, BASE, { signed: true }),
          `<span class="${tone(t.day_change)}">${pct(t.day_change_pct)}</span> daily P/L`, tone(t.day_change)),
        kpi("Total gain", money(t.total_gain, BASE, { signed: true }),
          `<span class="${tone(t.total_gain)}">${pct(t.total_gain_pct)}</span> on ${money(t.net_contributions)} contributed`, tone(t.total_gain)),
        kpi("Unrealized P/L", money(t.unrealized, BASE, { signed: true }),
          `<span class="${tone(t.unrealized)}">${pct(t.unrealized_pct)}</span> on ${money(t.book_cost)} book cost`, tone(t.unrealized)),
        kpi("Realized P/L", money(t.realized, BASE, { signed: true }), "From positions sold", tone(t.realized)),
        kpi("Dividends", money(t.dividends), "Received, all time"),
      ].join("");

      holdings = p.holdings;
      renderHoldings();
      renderEarnings(p.holdings);
      renderAlloc("sector", p.sectors.map((s) => ({ label: s.sector, value: s.value, change: s.day_change_pct })));
      renderAlloc("equity", [
        ...p.holdings.map((h) => ({ label: h.symbol, value: h.market_value, change: h.day_change_pct })),
        { label: "Cash", value: p.holdings_only ? 0 : t.cash, change: null },
      ]);
      renderCash(p.cash, p.holdings_only);
    }

    const HOLDING_COLUMNS = [
      { key: "symbol", label: "Symbol" },
      { key: "quantity", label: "Quantity", num: true },
      { key: "avg_cost", label: "Avg cost", num: true },
      { key: "price", label: "Price", num: true },
      { key: "day_change_pct", label: "Today", num: true },
      { key: "book_cost", label: "Book cost", num: true },
      { key: "unrealized", label: "Unrealized P/L", num: true },
      { key: "market_value", label: "Value", num: true },
      { key: "weight", label: "Weight", num: true },
    ];

    function renderHoldings() {
      const table = $("holdings");
      const rows = [...holdings].sort((a, b) => {
        const x = a[sort.key], y = b[sort.key];
        if (typeof x === "string") return x.localeCompare(y) * sort.dir;
        return ((x ?? -Infinity) - (y ?? -Infinity)) * sort.dir;
      });
      const head = HOLDING_COLUMNS.map((c) => {
        const state = sort.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : "none";
        return `<th class="${c.num ? "num" : ""}" aria-sort="${state}"><button type="button" data-sort="${c.key}">${esc(c.label)}</button></th>`;
      }).join("");
      const collapsed = !holdingsExpanded && rows.length > PREVIEW_ROWS;
      const body = (collapsed ? rows.slice(0, PREVIEW_ROWS) : rows).map((h) => {
        const href = urls.positionUrl.replace("__SYMBOL__", encodeURIComponent(h.symbol));
        const stale = h.live ? "" : '<span class="badge" title="No live quote; using last trade price">stale</span>';
        return `<tr>
          <td><a class="sym" href="${href}">${esc(h.symbol)}</a>${stale}<span class="sym-name">${esc(h.name)}</span></td>
          <td class="num">${qty(h.quantity)}</td>
          <td class="num nowrap">${money(h.avg_cost, h.cost_currency)}</td>
          <td class="num nowrap">${money(h.price, h.currency)}</td>
          <td class="num ${tone(h.day_change_pct)}">${pct(h.day_change_pct)}</td>
          <td class="num nowrap">${money(h.book_cost_native, h.cost_currency)}${h.cost_currency === BASE ? "" : `<br><small>${money(h.book_cost)} ${BASE}</small>`}</td>
          <td class="num nowrap ${tone(h.unrealized)}">${money(h.unrealized, BASE, { signed: true })}<br><small>${pct(h.unrealized_pct)}</small></td>
          <td class="num nowrap">${money(h.market_value_native, h.currency)}${h.currency === BASE ? "" : `<br><small>${money(h.market_value)} ${BASE}</small>`}</td>
          <td class="num">${pct(h.weight, { signed: false })}</td>
        </tr>`;
      }).join("");
      const t = lastSnapshot.totals;
      const totalLabel = collapsed ? `Total <small class="muted">all ${rows.length}</small>` : "Total";
      const foot = rows.length ? `<tfoot><tr><td>${totalLabel}</td><td></td><td></td><td></td>
          <td class="num ${tone(t.day_change)}">${money(t.day_change, BASE, { signed: true })}</td>
          <td class="num nowrap">${money(t.book_cost)}</td>
          <td class="num nowrap ${tone(t.unrealized)}">${money(t.unrealized, BASE, { signed: true })}</td>
          <td class="num nowrap">${money(t.market_value)}</td><td></td></tr></tfoot>` : "";
      table.innerHTML = rows.length
        ? `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}`
        : `<tbody><tr><td class="muted">No open positions.</td></tr></tbody>`;
      $("holdings-more").innerHTML = moreButton(rows.length, holdingsExpanded, "holdings", "holdings");
    }

    $("holdings-more").addEventListener("click", (e) => {
      if (!e.target.closest("button")) return;
      holdingsExpanded = !holdingsExpanded;
      renderHoldings();
    });

    // Upcoming earnings ---------------------------------------------------------
    // Dates are shown in New York time, the exchange's calendar day.
    const MARKET_TZ = "America/New_York";
    const marketDay = (ms) => new Date(`${new Date(ms).toLocaleDateString("en-CA", { timeZone: MARKET_TZ })}T00:00:00Z`);
    const earningsDate = (ts) => new Date(ts * 1000).toLocaleDateString("en-CA", {
      weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: MARKET_TZ,
    });

    function renderEarnings(list = earningsList) {
      earningsList = list;
      const today = marketDay(Date.now());
      const rows = list
        .filter((h) => h.earnings)
        .map((h) => ({ h, e: h.earnings, days: Math.round((marketDay(h.earnings.start * 1000) - today) / 86_400_000) }))
        .filter((r) => r.days >= 0)
        .sort((a, b) => a.e.start - b.e.start);
      const missing = list.filter((h) => h.quote_type === "EQUITY" && !rows.some((r) => r.h === h)).map((h) => h.symbol);
      const when = (d) => (d === 0 ? "Today" : d === 1 ? "Tomorrow" : `In ${d} days`);

      $("earnings").innerHTML = rows.length
        ? `<table class="table"><thead><tr><th>Symbol</th><th>Date</th><th class="num">When</th><th class="num">Value</th></tr></thead>
          <tbody>${(earningsExpanded ? rows : rows.slice(0, PREVIEW_ROWS)).map(({ h, e, days }) => {
            const href = urls.positionUrl.replace("__SYMBOL__", encodeURIComponent(h.symbol));
            const date = e.end ? `${earningsDate(e.start)} – ${earningsDate(e.end)}` : earningsDate(e.start);
            const badge = e.estimated ? '<span class="badge" title="Estimated by Yahoo; not yet confirmed by the company">estimated</span>' : "";
            return `<tr>
              <td><a class="sym" href="${href}">${esc(h.symbol)}</a><span class="sym-name">${esc(h.name)}</span></td>
              <td class="nowrap">${esc(date)}${badge}</td>
              <td class="num nowrap">${when(days)}</td>
              <td class="num nowrap">${money(h.market_value)}</td>
            </tr>`;
          }).join("")}</tbody></table>`
        : '<p class="muted">No upcoming earnings dates for your holdings.</p>';
      $("earnings-more").innerHTML = moreButton(rows.length, earningsExpanded, "earnings", "upcoming reports");
      $("earnings-note").textContent = missing.length
        ? `No date announced yet for ${missing.join(", ")}. ETFs and funds don't report earnings.`
        : "ETFs and funds don't report earnings.";
    }

    $("earnings-more").addEventListener("click", (e) => {
      if (!e.target.closest("button")) return;
      earningsExpanded = !earningsExpanded;
      renderEarnings();
    });

    $("holdings").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-sort]");
      if (!btn) return;
      const key = btn.dataset.sort;
      sort = { key, dir: sort.key === key ? -sort.dir : key === "symbol" ? 1 : -1 };
      renderHoldings();
    });

    // Doughnut + legend for "<name>-chart" / "<name>-legend". Items are {label, value}; slices
    // that aren't positive (e.g. an overdrawn cash balance) are left out, largest first.
    // Allocation cards (sector, equity): doughnut or heat map, picked in each card's dropdown and
    // remembered per card in this browser. Only the visible view is drawn, so the doughnut never
    // sizes itself while hidden. Items are {label, value, change} with change = today's change.
    const ALLOC_VIEWS = ["doughnut", "heatmap"];
    const allocView = {}, allocItems = {};
    for (const name of ["sector", "equity"]) {
      let stored = null;
      try { stored = localStorage.getItem(`${name}View`); } catch { /* storage blocked */ }
      allocView[name] = ALLOC_VIEWS.includes(stored) ? stored : "doughnut";
      $(`${name}-view`).value = allocView[name];
      $(`${name}-view`).addEventListener("change", (e) => {
        allocView[name] = e.target.value;
        try { localStorage.setItem(`${name}View`, allocView[name]); } catch { /* storage blocked */ }
        if (allocItems[name]) renderAlloc(name, allocItems[name]);
      });
    }

    function renderAlloc(name, items) {
      allocItems[name] = items;
      const heat = allocView[name] === "heatmap";
      $(`${name}-doughnut`).hidden = heat;
      $(`${name}-heatmap`).hidden = !heat;
      const hint = $(`${name}-hint`);
      hint.textContent = heat ? hint.dataset.heatmapHint : hint.dataset.doughnutHint;
      if (heat) renderHeatmap(name, items);
      else renderAllocation(name, items.map(({ label, value }) => ({ label, value })));
    }

    // Squarified treemap: rectangles whose areas match the values, kept close to square.
    // Returns items with x, y, w, h in the same units as width / height.
    function treemap(items, width, height) {
      const total = items.reduce((sum, i) => sum + i.value, 0);
      const nodes = items.map((i) => ({ ...i, area: (i.value / total) * width * height }));
      const out = [];
      let x = 0, y = 0, w = width, h = height, row = [];
      const worst = (r, side) => {
        const s = r.reduce((sum, n) => sum + n.area, 0);
        const areas = r.map((n) => n.area);
        return Math.max((side * side * Math.max(...areas)) / (s * s), (s * s) / (side * side * Math.min(...areas)));
      };
      const layRow = () => {
        const s = row.reduce((sum, n) => sum + n.area, 0);
        if (w >= h) {  // a column along the left edge
          const cw = s / h;
          let yy = y;
          row.forEach((n) => { out.push({ ...n, x, y: yy, w: cw, h: n.area / cw }); yy += n.area / cw; });
          x += cw; w -= cw;
        } else {  // a row along the top edge
          const rh = s / w;
          let xx = x;
          row.forEach((n) => { out.push({ ...n, x: xx, y, w: n.area / rh, h: rh }); xx += n.area / rh; });
          y += rh; h -= rh;
        }
        row = [];
      };
      for (let i = 0; i < nodes.length;) {
        const side = Math.min(w, h);
        if (!row.length || worst([...row, nodes[i]], side) <= worst(row, side)) row.push(nodes[i++]);
        else layRow();
      }
      if (row.length) layRow();
      return out;
    }

    // Grey at 0%, full gain / loss colour at ±3% or more.
    function heatColor(change) {
      const hex = (c) => {
        const m = /^#?([0-9a-f]{6})$/i.exec(c.trim());
        const n = m ? parseInt(m[1], 16) : 0x6b7280;
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
      };
      const neutral = [107, 114, 128];
      if (change == null) return `rgb(${neutral.join(",")})`;
      const target = hex(cssVar(change >= 0 ? "--gain" : "--loss") || (change >= 0 ? "#0e7a43" : "#c22d2d"));
      const t = Math.min(Math.abs(change) / 0.03, 1);
      return `rgb(${neutral.map((c, i) => Math.round(c + (target[i] - c) * t)).join(",")})`;
    }

    function renderHeatmap(name, all) {
      const el = $(`${name}-heatmap`);
      const items = all.filter((s) => s.value > 0).sort((a, b) => b.value - a.value);
      const total = items.reduce((sum, s) => sum + s.value, 0);
      if (!items.length) {
        el.innerHTML = '<p class="muted">Nothing to show yet.</p>';
        return;
      }
      const width = el.clientWidth || 600, height = el.clientHeight || 260;
      el.innerHTML = treemap(items, width, height).map((r) => {
        const change = r.change;
        const share = pct(r.value / total, { signed: false });
        const today = change == null ? "" : pct(change);
        const small = r.w < 90 || r.h < 48 ? " small" : "";
        const label = `${r.label}: ${share} of portfolio${today ? `, ${today} today` : ""}`;
        return `<div class="heat-tile${small}" role="listitem" title="${esc(label)}" aria-label="${esc(label)}"
          style="left:${(r.x / width) * 100}%;top:${(r.y / height) * 100}%;width:${(r.w / width) * 100}%;height:${(r.h / height) * 100}%;background:${heatColor(change)}">
          <strong>${esc(r.label)}</strong><span>${share}${today ? ` · ${today}` : ""}</span></div>`;
      }).join("");
    }

    function renderAllocation(name, items) {
      const shown = items.filter((s) => s.value > 0).sort((a, b) => b.value - a.value);
      const total = shown.reduce((sum, s) => sum + s.value, 0);
      shown.forEach((s) => { s.pct = s.value / total; });
      const colors = shown.map((s, i) => (s.label === "Cash" ? cssVar("--chart-3") : SECTOR_COLORS[i % SECTOR_COLORS.length]));
      $(`${name}-legend`).innerHTML = shown.length
        ? shown.map((s, i) => `<li><span class="swatch" style="background:${colors[i]}"></span>
            <span>${esc(s.label)}</span><span class="pct">${pct(s.pct, { signed: false })}</span></li>`).join("")
        : '<li class="muted">Nothing to show yet.</li>';
      const data = {
        labels: shown.map((s) => s.label),
        datasets: [{ data: shown.map((s) => s.value), backgroundColor: colors, borderColor: cssVar("--surface"), borderWidth: 2 }],
      };
      const chart = allocCharts[name];
      if (chart) {
        chart.$items = shown;
        chart.data = data;
        chart.update("none");
        return;
      }
      allocCharts[name] = new Chart($(`${name}-chart`), {
        type: "doughnut",
        data,
        options: {
          maintainAspectRatio: false,
          cutout: "62%",
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: (c) => ` ${c.label}: ${money(c.parsed)} (${pct(c.chart.$items[c.dataIndex]?.pct, { signed: false })})` } },
          },
        },
      });
      allocCharts[name].$items = shown;
    }

    function renderCash(cash, holdingsOnly) {
      if (!cash.length) {
        $("cash").innerHTML = '<p class="muted">No cash balances.</p>';
        return;
      }
      const total = cash.reduce((sum, c) => sum + c.amount_base, 0);
      $("cash").innerHTML = `<table class="table compact">
        <thead><tr><th>Account</th><th class="num">Balance</th><th class="num">In ${BASE}</th></tr></thead>
        <tbody>${cash.map((c) => `<tr><td>${esc(c.account)}</td>
          <td class="num nowrap ${c.amount < 0 ? "loss" : ""}">${money(c.amount, c.currency)}</td>
          <td class="num nowrap">${money(c.amount_base)}</td></tr>`).join("")}</tbody>
        <tfoot><tr><td>Total</td><td></td><td class="num nowrap">${money(total)}</td></tr></tfoot></table>
        ${holdingsOnly ? '<p class="hint">Record deposits and withdrawals to include cash in your portfolio value.</p>' : ""}`;
    }


    loadPortfolio();
    performanceSection(urls, benchmark);
    setInterval(() => { if (!document.hidden) loadPortfolio(); }, REFRESH_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadPortfolio(); });
  }

  // ---- position detail ---------------------------------------------------------
  function position() {
    chartDefaults();
    getJSON(root.dataset.positionApi).then(render).catch((err) => {
      $("pos-price").textContent = `Couldn't load this position: ${err.message}`;
      $("kpis").innerHTML = "";
    });

    function render(p) {
      const h = p.holding;
      $("pos-name").textContent = p.name !== p.symbol ? p.name : "";
      if (p.quote && p.quote.price != null) {
        const change = p.quote.prev_close ? p.quote.price - p.quote.prev_close : null;
        $("pos-price").innerHTML = `${money(p.quote.price, p.quote.currency)}
          <span class="${tone(change)}">${change == null ? "" : `${money(change, p.quote.currency, { signed: true })} (${pct(change / p.quote.prev_close)}) today`}</span>`;
      } else {
        $("pos-price").textContent = "No live quote available for this symbol.";
      }
      notices((p.warnings || []).map((w) => ["warning", w]));

      $("kpis").innerHTML = [
        kpi("Quantity", h ? qty(h.quantity) : "0", h ? `Avg cost ${money(h.avg_cost, h.cost_currency)}` : "Position closed"),
        kpi("Market value", money(h ? h.market_value : 0),
          h && h.currency !== BASE ? `${money(h.market_value_native, h.currency)} at ${money(h.price, h.currency)}` : h ? `at ${money(h.price)}` : ""),
        kpi("Unrealized P/L", money(p.unrealized, BASE, { signed: true }),
          h ? `<span class="${tone(h.unrealized)}">${pct(h.unrealized_pct)}</span> on ${money(h.book_cost)} book cost` : "", tone(p.unrealized)),
        kpi("Realized P/L", money(p.realized, BASE, { signed: true }), "From shares sold", tone(p.realized)),
        kpi("Dividends", money(p.dividends), "Received"),
        kpi("Total return", money(p.total_return, BASE, { signed: true }),
          `<span class="${tone(p.total_return)}">${pct(p.total_return_pct)}</span> on ${money(p.invested)} invested`, tone(p.total_return)),
      ].join("");

      renderPriceChart(p);

      $("pos-accounts").innerHTML = `<thead><tr><th>Account</th><th class="num">Quantity</th><th class="num">Avg cost</th>
          <th class="num">Book cost (${BASE})</th><th class="num">Realized</th><th class="num">Dividends</th></tr></thead>
        <tbody>${p.accounts.map((a) => `<tr><td>${esc(a.account)}</td><td class="num">${qty(a.quantity)}</td>
          <td class="num nowrap">${a.avg_cost == null ? "—" : money(a.avg_cost, a.currency)}</td>
          <td class="num nowrap">${money(a.book_cost)}</td>
          <td class="num nowrap ${tone(a.realized)}">${money(a.realized, BASE, { signed: true })}</td>
          <td class="num nowrap">${money(a.dividends)}</td></tr>`).join("")}</tbody>`;

      $("pos-txns").innerHTML = `<thead><tr><th>Date</th><th>Account</th><th>Type</th><th class="num">Quantity</th>
          <th class="num">Price</th><th class="num">Net amount</th><th></th></tr></thead>
        <tbody>${p.transactions.map((t) => `<tr><td class="nowrap">${esc(t.date)}</td><td>${esc(t.account)}</td>
          <td><span class="tag tag-${esc(t.kind.toLowerCase())}">${esc(t.kind)}</span></td>
          <td class="num">${t.quantity ? qty(t.quantity) : "—"}</td>
          <td class="num nowrap">${t.price ? money(t.price, t.currency) : "—"}</td>
          <td class="num nowrap ${tone(t.net_amount)}">${money(t.net_amount, t.currency, { signed: true })}</td>
          <td><div class="row-actions"><a class="btn btn-ghost btn-sm" href="${esc(t.edit_url)}">Edit</a></div></td></tr>`).join("")}</tbody>`;
    }

    function renderPriceChart(p) {
      const prices = p.prices;
      if (!prices.length) {
        document.querySelector("#price-chart").replaceWith(Object.assign(document.createElement("p"), {
          className: "muted", textContent: "No price history available for this symbol.",
        }));
        return;
      }
      $("price-currency").textContent = `Prices in ${p.currency}`;
      const labels = prices.map((d) => d.date);
      const indexOf = (date) => {
        const i = labels.findIndex((l) => l >= date);
        return i === -1 ? labels.length - 1 : i;
      };
      const buys = new Array(labels.length).fill(null);
      const sells = new Array(labels.length).fill(null);
      p.trades.forEach((t) => {
        const i = indexOf(t.date);
        (t.kind === "BUY" ? buys : sells)[i] = t.price || prices[i].close;
      });
      const c1 = cssVar("--chart-1");
      new Chart($("price-chart"), {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "Close", data: prices.map((d) => d.close), borderColor: c1, borderWidth: 2, pointRadius: 0, tension: 0.1 },
            { label: "Buy", data: buys, showLine: false, pointStyle: "triangle", pointRadius: 7, backgroundColor: cssVar("--gain"), borderColor: cssVar("--gain") },
            { label: "Sell", data: sells, showLine: false, pointStyle: "triangle", rotation: 180, pointRadius: 7, backgroundColor: cssVar("--loss"), borderColor: cssVar("--loss") },
          ],
        },
        options: {
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: { x: { grid: { display: false }, ticks: dateTicks(labels) }, y: { ticks: { callback: (v) => money(v, p.currency) } } },
          plugins: {
            legend: { display: false },
            tooltip: {
              filter: (item) => item.parsed.y != null,
              callbacks: {
                title: (items) => new Date(`${items[0].label}T00:00:00`).toLocaleDateString("en-CA", { dateStyle: "medium" }),
                label: (c) => ` ${c.dataset.label}: ${money(c.parsed.y, p.currency)}`,
              },
            },
          },
        },
      });
    }
  }

  // ---- transaction form ----------------------------------------------------------
  function txnForm() {
    const form = $("txn-form");
    const kind = $("kind");
    const fields = { quantity: $("quantity"), price: $("price"), costs: $("costs"), net: $("net_amount") };
    const hint = $("net-hint");
    const isTrade = () => kind.value === "BUY" || kind.value === "SELL";
    const needsSymbol = () => isTrade() || kind.value === "DIVIDEND" || kind.value === "TRANSFER" || kind.value === "OTHER";
    let netTouched = fields.net.value !== "";

    function update() {
      form.querySelectorAll('[data-field="trade"]').forEach((el) => { el.hidden = !isTrade() && kind.value !== "TRANSFER" && kind.value !== "OTHER"; });
      form.querySelectorAll('[data-field="symbol"]').forEach((el) => { el.hidden = !needsSymbol(); });
      hint.textContent = isTrade()
        ? "Calculated from quantity, price and costs. Edit it to match your broker statement exactly."
        : { DEPOSIT: "Amount deposited.", WITHDRAWAL: "Amount withdrawn.", DIVIDEND: "Dividend received (negative for withholding tax).",
            FEE: "Fee charged.", INTEREST: "Interest received." }[kind.value] || "Cash effect on the account: positive in, negative out.";
    }

    function recalc() {
      if (!isTrade() || netTouched) return;
      const q = parseFloat(fields.quantity.value) || 0;
      const p = parseFloat(fields.price.value) || 0;
      const c = parseFloat(fields.costs.value) || 0;
      if (!q || !p) return;
      const net = kind.value === "BUY" ? -(q * p + c) : q * p - c;
      fields.net.value = net.toFixed(2);
    }

    kind.addEventListener("change", () => { update(); recalc(); });
    fields.net.addEventListener("input", () => { netTouched = fields.net.value !== ""; });
    [fields.quantity, fields.price, fields.costs].forEach((el) => el.addEventListener("input", () => {
      if (netTouched && isTrade()) netTouched = false; // trade inputs changed: recompute the net amount
      recalc();
    }));
    update();
  }

  // Account / portfolio picker: options are "account:<id>" or "portfolio:<id>" ("" = all accounts).
  // Kept out of an inline onchange attribute, where `URL` would resolve to the document.URL string.
  $("account-select")?.addEventListener("change", (e) => {
    const url = new URL(location.href);
    ["account", "portfolio", "all", "page"].forEach((p) => url.searchParams.delete(p));
    const [key, id] = e.target.value.split(":");
    if (id) url.searchParams.set(key, id);
    else url.searchParams.set("all", "1");  // an explicit choice: don't fall back to a saved default
    location.href = url.href;
  });

  function performance() {
    const benchmark = root.dataset.benchmark;
    chartDefaults();
    performanceSection(root.dataset, benchmark, { onHistory: (history, err) => renderAnnual(history, err, benchmark) });
  }

  // ---- performance by year (Performance tab) -------------------------------------
  function renderAnnual(history, err, benchmark) {
    const status = $("annual-status");
    if (err) {
      status.textContent = `Couldn't build history: ${err.message}`;
      return;
    }
    const years = history.years || [];
    if (!years.length) {
      status.textContent = "Not enough history yet.";
      return;
    }
    status.hidden = true;
    const shortDate = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-CA", { month: "short", day: "numeric" });
    const label = (y) => (y.ytd ? `${y.year} YTD` : y.partial ? `${y.year} (from ${shortDate(y.start)})` : String(y.year));
    const diff = (y) => (y.portfolio == null || y.benchmark == null ? null : y.portfolio - y.benchmark);

    const c1 = cssVar("--chart-1"), c2 = cssVar("--chart-2");
    new Chart($("annual-chart"), {
      type: "bar",
      data: {
        labels: years.map((y) => (y.ytd ? `${y.year} YTD` : y.partial ? `${y.year}*` : String(y.year))),
        datasets: [
          { label: "Portfolio", data: years.map((y) => y.portfolio), backgroundColor: c1, borderRadius: 3 },
          { label: benchmark, data: years.map((y) => y.benchmark), backgroundColor: c2, borderRadius: 3 },
        ],
      },
      options: {
        maintainAspectRatio: false,
        animation: false,
        scales: { x: { grid: { display: false } }, y: { ticks: { callback: (v) => pct(v) }, grace: "5%" } },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12 } },
          tooltip: {
            callbacks: {
              title: (items) => label(years[items[0].dataIndex]),
              label: (c) => ` ${c.dataset.label}: ${pct(c.parsed.y)}`,
            },
          },
        },
      },
    });

    const cell = (v) => `<td class="num ${tone(v)}">${pct(v)}</td>`;
    $("annual-table").innerHTML = `
      <thead><tr><th>Year</th><th class="num">Portfolio</th><th class="num">${esc(benchmark)}</th><th class="num">Difference</th>
        <th class="num">Start value</th><th class="num">Net contributions</th><th class="num">End value</th><th class="num">Gain</th></tr></thead>
      <tbody>${[...years].reverse().map((y) => `<tr>
        <td class="nowrap">${esc(label(y))}</td>${cell(y.portfolio)}${cell(y.benchmark)}${cell(diff(y))}
        <td class="num nowrap">${money(y.start_value)}</td>
        <td class="num nowrap">${money(y.contributions, BASE, { signed: true })}</td>
        <td class="num nowrap">${money(y.end_value)}</td>
        <td class="num nowrap ${tone(y.gain)}">${money(y.gain, BASE, { signed: true })}</td>
      </tr>`).join("")}</tbody>`;
  }

  // ---- Markets tab -----------------------------------------------------------------
  // Pre-market futures and sectors (refreshed every minute), CNN's Fear & Greed Index and
  // the Treasury yield curve. Each section loads on its own, so one slow source can't hold up the rest.
  function markets() {
    const urls = root.dataset;
    chartDefaults();
    const num = (v, digits = 2) => (v == null ? "—"
      : v.toLocaleString("en-CA", { minimumFractionDigits: digits, maximumFractionDigits: digits }));
    const signed = (v, text) => (v < 0 ? "−" : v > 0 ? "+" : "") + text;
    const etTime = (unix) => (unix ? `${new Date(unix * 1000).toLocaleTimeString("en-CA",
      { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "");
    const longDate = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-CA",
      { month: "short", day: "numeric", year: "numeric" });

    // Pre-market ------------------------------------------------------------------
    const SESSIONS = {
      pre: "Pre-market", regular: "Market open · today's change",
      post: "After hours", closed: "Market closed · last session",
    };

    // Today's prices as a line, with the previous close dashed for reference.
    function sparkline(points, base) {
      if (points.length < 2) return "";
      const W = 160, H = 48;
      const values = points.map((p) => p[1]).concat(base ?? []);
      const lo = Math.min(...values), span = Math.max(...values) - lo || 1;
      const t0 = points[0][0], t1 = points[points.length - 1][0];
      const x = (t) => (((t - t0) / (t1 - t0 || 1)) * W).toFixed(1);
      const y = (v) => (H - 2 - ((v - lo) / span) * (H - 4)).toFixed(1);
      const path = points.map((p, i) => `${i ? "L" : "M"}${x(p[0])},${y(p[1])}`).join("");
      const ref = base == null ? "" : `<line class="spark-base" x1="0" x2="${W}" y1="${y(base)}" y2="${y(base)}"/>`;
      const last = points[points.length - 1][1];
      return `<svg class="spark ${base == null ? "" : tone(last - base)}" viewBox="0 0 ${W} ${H}"
        preserveAspectRatio="none" aria-hidden="true">${ref}<path d="${path}"/></svg>`;
    }

    function renderFutures(futures) {
      $("futures").innerHTML = futures.length
        ? futures.map((f) => `<div class="kpi future">
            <div class="future-text">
              <div class="kpi-label">${esc(f.label)}</div>
              <div class="kpi-value">${num(f.price)}</div>
              <div class="kpi-sub"><span class="${tone(f.change)}">${f.change == null ? "—" : signed(f.change, num(Math.abs(f.change)))}
                (${pct(f.change_pct)})</span></div>
              <div class="kpi-sub">${esc(f.name)}${f.time ? ` · ${etTime(f.time)}` : ""}</div>
            </div>
            ${sparkline(f.points, f.prev_close)}
          </div>`).join("")
        : '<p class="muted">Futures quotes are unavailable right now.</p>';
    }

    function renderSectors(data) {
      const rows = [...data.sectors].sort((a, b) => (b.change_pct ?? -Infinity) - (a.change_pct ?? -Infinity));
      const widest = Math.max(0.005, ...rows.map((r) => Math.abs(r.change_pct ?? 0)));
      $("sector-session").textContent = data.session
        ? `${SESSIONS[data.session]}${data.session_time ? ` · ${etTime(data.session_time)}` : ""}` : "";
      $("sectors").innerHTML = rows.length
        ? rows.map((r) => {
          const width = ((Math.abs(r.change_pct ?? 0) / widest) * 50).toFixed(1);
          const side = (r.change_pct ?? 0) < 0 ? "right" : "left";
          return `<div class="sbar" role="listitem" aria-label="${esc(`${r.sector} (${r.symbol}): ${pct(r.change_pct)}`)}">
            <span class="sbar-name">${esc(r.sector)} <span class="muted">${esc(r.symbol)}</span></span>
            <span class="sbar-track"><span class="sbar-fill ${tone(r.change_pct)}" style="${side}:50%;width:${width}%"></span></span>
            <span class="sbar-val ${tone(r.change_pct)}">${pct(r.change_pct)}</span>
          </div>`;
        }).join("")
        : '<p class="muted">Sector quotes are unavailable right now.</p>';
    }

    async function loadPremarket() {
      try {
        const data = await getJSON(urls.premarketUrl);
        renderFutures(data.futures);
        renderSectors(data);
        const updated = new Date().toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit" });
        $("as-of").textContent = `Futures and sector prices from Yahoo Finance · updated ${updated}`;
      } catch (err) {
        $("as-of").textContent = `Couldn't load prices: ${err.message}`;
        if ($("futures").querySelector(".skeleton")) {
          $("futures").innerHTML = '<p class="muted">Futures quotes are unavailable right now.</p>';
          $("sectors").innerHTML = '<p class="muted">Sector quotes are unavailable right now.</p>';
        }
      }
    }

    // Fear & Greed ----------------------------------------------------------------
    const FG_RATINGS = ["extreme fear", "fear", "neutral", "greed", "extreme greed"];
    const FG_BANDS = [[0, 25], [25, 45], [45, 55], [55, 75], [75, 100]];
    // CNN's bands, applied to the unrounded score as CNN does (44.9 shows as 45 but is "fear").
    const fgRating = (s) => FG_RATINGS[s < 25 ? 0 : s < 45 ? 1 : s <= 55 ? 2 : s <= 75 ? 3 : 4];
    const fgClass = (rating) => `fg-${Math.max(FG_RATINGS.indexOf(rating), 0)}`;
    const fgChip = (rating) => `<span class="fg-chip ${fgClass(rating)}">${esc(rating || "—")}</span>`;

    // Half-circle gauge: 0 on the left, 100 on the right, one coloured arc per band.
    function gauge(score) {
      const cx = 110, cy = 112, r = 90;
      const at = (s, radius = r) => {
        const a = Math.PI * (1 - s / 100);
        return [(cx + radius * Math.cos(a)).toFixed(2), (cy - radius * Math.sin(a)).toFixed(2)];
      };
      const arcs = FG_BANDS.map(([a, b], i) => {
        const [x1, y1] = at(a + (a ? 0.8 : 0)), [x2, y2] = at(b - (b < 100 ? 0.8 : 0));
        return `<path class="fg-arc fg-${i}" d="M${x1},${y1} A${r},${r} 0 0 1 ${x2},${y2}"/>`;
      }).join("");
      const [nx, ny] = at(score, r - 24);
      return `<svg class="gauge" viewBox="0 0 220 124" role="img" aria-label="Fear and Greed Index: ${Math.round(score)}">
        ${arcs}<line class="needle" x1="${cx}" y1="${cy}" x2="${nx}" y2="${ny}"/><circle class="needle-hub" cx="${cx}" cy="${cy}" r="6"/>
      </svg>`;
    }

    function renderFearGreed(fg) {
      const updated = fg.updated ? new Date(fg.updated).toLocaleString("en-CA",
        { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
      $("fear-greed").innerHTML = `<div class="fg">
        <div class="fg-now">
          ${gauge(fg.score)}
          <div class="fg-score">${Math.round(fg.score)}</div>
          <div class="fg-rating ${fgClass(fg.rating)}">${esc(fg.rating)}</div>
        </div>
        <ul class="fg-compare">${fg.compare.map((c) => `<li>
          <span class="muted">${esc(c.label)}</span>${fgChip(c.rating)}<strong>${Math.round(c.score)}</strong></li>`).join("")}
        </ul>
      </div>`;

      if (fg.history.length > 1) {
        $("fg-history-box").hidden = false;
        const labels = fg.history.map((p) => p[0]);
        new Chart($("fg-history"), {
          type: "line",
          data: { labels, datasets: [{ data: fg.history.map((p) => p[1]), borderColor: cssVar("--chart-1"), borderWidth: 2, pointRadius: 0, tension: 0.2 }] },
          options: {
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            scales: { x: { grid: { display: false }, ticks: dateTicks(labels) }, y: { min: 0, max: 100, ticks: { stepSize: 25 } } },
            plugins: {
              legend: { display: false },
              tooltip: { callbacks: {
                title: (items) => longDate(labels[items[0].dataIndex]),
                label: (c) => ` ${Math.round(c.parsed.y)} · ${fgRating(c.parsed.y)}`,
              } },
            },
          },
        });
      }

      $("fg-indicators").innerHTML = fg.indicators.length ? `
        <thead><tr><th>Indicator</th><th>Rating</th><th class="num">Score</th></tr></thead>
        <tbody>${fg.indicators.map((i) => `<tr>
          <td>${esc(i.name)}<span class="sym-name">${esc(i.detail)}</span></td>
          <td>${fgChip(i.rating)}</td><td class="num">${Math.round(i.score)}</td></tr>`).join("")}</tbody>` : "";
      $("fg-note").textContent = `From CNN Business${updated ? ` (updated ${updated})` : ""}. 0 is extreme fear and 100 is extreme greed; the index averages the seven indicators above.`;
    }

    // The browser reads CNN's feed directly: it allows cross-site reads, while hosts like
    // PythonAnywhere's free tier block the server from reaching CNN. Same shape as the server's
    // copy (macro.parse_fear_greed), which is the fallback.
    async function cnnFearGreed() {
      const res = await fetch(urls.fearGreedFeed, { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`CNN returned ${res.status}`);
      const d = await res.json();
      const fg = d.fear_and_greed || {};
      if (fg.score == null) throw new Error("No Fear & Greed score in CNN's response");
      const meta = JSON.parse(urls.fearGreedMeta);
      const byDay = new Map();
      for (const p of d.fear_and_greed_historical?.data || []) {
        if (p.x && p.y != null) byDay.set(new Date(p.x).toISOString().slice(0, 10), p.y);
      }
      return {
        score: fg.score,
        rating: fg.rating || fgRating(fg.score),
        updated: fg.timestamp,
        compare: meta.compare.filter(([key]) => fg[key] != null)
          .map(([key, label]) => ({ label, score: fg[key], rating: fgRating(fg[key]) })),
        indicators: meta.indicators.filter(([key]) => d[key]?.score != null)
          .map(([key, name, detail]) => ({ key, name, detail, score: d[key].score, rating: d[key].rating || fgRating(d[key].score) })),
        history: [...byDay].sort((a, b) => a[0].localeCompare(b[0])),
      };
    }

    async function loadSentiment() {
      let data;
      try {
        data = await cnnFearGreed();
      } catch {
        try {
          data = await getJSON(urls.sentimentUrl);
        } catch (err) {
          $("fear-greed").innerHTML = `<p class="muted">${esc(err.message)} Open it on CNN with the button above.</p>`;
          return;
        }
      }
      renderFearGreed(data);
    }

    // Treasury yields -------------------------------------------------------------
    const CURVES = {
      latest: { label: "Latest", color: "--chart-1", width: 2.5 },
      "1m": { label: "1 month ago", color: "--chart-2", width: 1.5 },
      "1y": { label: "1 year ago", color: "--chart-3", width: 1.5, dash: [5, 4] },
      prev: { label: "Previous close", color: "--chart-3", width: 1.5, dash: [5, 4] },
    };
    const COMPARE = { "1w": "1 wk", "1m": "1 mo", "1y": "1 yr", prev: "Prev. close" };
    const yieldText = (v) => (v == null ? "—" : `${v.toFixed(2)}%`);
    const bps = (a, b) => (a == null || b == null ? null : Math.round((a - b) * 100));
    const bpsText = (v) => (v == null ? "—" : signed(v, `${Math.abs(v)} bps`));

    function renderRates(data) {
      const { tenors, curves } = data;
      const latest = curves.find((c) => c.key === "latest");
      const at = (curve, tenor) => (curve && tenors.includes(tenor) ? curve.yields[tenors.indexOf(tenor)] : null);
      const comparisons = curves.filter((c) => c.key in COMPARE);
      const ref = curves.find((c) => c.key === "1m") || curves.find((c) => c.key === "prev");
      const refText = ref ? (ref.key === "prev" ? "vs previous close" : "vs 1 month ago") : "";

      const tiles = [];
      for (const [tenor, label] of [["2Y", "2-year"], ["10Y", "10-year"], ["30Y", "30-year"]]) {
        if (at(latest, tenor) == null) continue;
        const change = bps(at(latest, tenor), at(ref, tenor));
        tiles.push(kpi(`${label} yield`, yieldText(at(latest, tenor)), ref ? `${bpsText(change)} ${refText}` : ""));
      }
      for (const [long, short] of [["10Y", "2Y"], ["10Y", "3M"]]) {
        const spread = bps(at(latest, long), at(latest, short));
        if (spread == null) continue;
        const before = bps(at(ref, long), at(ref, short));
        const sub = `${spread < 0 ? '<span class="loss">Inverted</span>' : "Normal"}${before == null ? "" : ` · ${bpsText(spread - before)} ${refText}`}`;
        tiles.push(kpi(`${long} − ${short} spread`, bpsText(spread), sub));
      }
      $("rate-kpis").innerHTML = tiles.join("");

      $("yield-status").hidden = true;
      new Chart($("yield-chart"), {
        type: "line",
        data: {
          labels: tenors,
          datasets: curves.filter((c) => CURVES[c.key]).map((c) => {
            const style = CURVES[c.key];
            const color = cssVar(style.color);
            return {
              label: c.key === "latest" && c.date ? `${style.label} (${longDate(c.date)})` : style.label,
              data: c.yields, borderColor: color, backgroundColor: color, borderWidth: style.width,
              borderDash: style.dash || [], pointRadius: c.key === "latest" ? 3 : 0, spanGaps: true, tension: 0.25,
            };
          }),
        },
        options: {
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: { x: { grid: { display: false }, title: { display: true, text: "Maturity" } },
            y: { ticks: { callback: (v) => `${v.toFixed(2)}%` }, grace: "5%" } },
          plugins: {
            legend: { position: "bottom", labels: { boxWidth: 12 } },
            tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${yieldText(c.parsed.y)}` } },
          },
        },
      });

      $("yield-table").innerHTML = `
        <thead><tr><th>Maturity</th><th class="num">Yield</th>${comparisons.map((c) =>
          `<th class="num" title="${c.date ? esc(longDate(c.date)) : ""}">vs ${esc(COMPARE[c.key])}</th>`).join("")}</tr></thead>
        <tbody>${tenors.map((t) => `<tr><td>${esc(t)}</td><td class="num">${yieldText(at(latest, t))}</td>${comparisons.map((c) =>
          `<td class="num">${bpsText(bps(at(latest, t), at(c, t)))}</td>`).join("")}</tr>`).join("")}</tbody>`;

      $("yield-note").innerHTML = data.source === "treasury"
        ? `Daily par yield curve from the <a href="${esc(data.source_url)}" target="_blank" rel="noopener">U.S. Treasury</a>${latest.date ? `, as of ${esc(longDate(latest.date))}` : ""}. Published each business day after the close.`
        : `The U.S. Treasury site couldn't be reached, so this shows the four Treasury yields Yahoo Finance quotes (the 3-month is the 13-week bill).`;
    }

    async function loadRates() {
      try {
        renderRates(await getJSON(urls.ratesUrl));
      } catch (err) {
        $("yield-status").textContent = `Couldn't load yields: ${err.message}`;
        $("rate-kpis").innerHTML = "";
      }
    }

    loadPremarket();
    loadSentiment();
    loadRates();
    setInterval(() => { if (!document.hidden) loadPremarket(); }, REFRESH_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadPremarket(); });
  }

  const pages = { dashboard, performance, position, "txn-form": txnForm, markets };
  pages[document.body.dataset.page]?.();
})();
