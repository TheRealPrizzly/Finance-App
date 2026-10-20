/* Portfolio Manager front end: renders the dashboard and position pages from the
   JSON API, and adds small conveniences to the transaction form. */
(() => {
  "use strict";

  const root = document.getElementById("app");
  const BASE = document.querySelector("[data-base]")?.dataset.base || "CAD";
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
  const withAlpha = (hex, a) => {
    const n = parseInt(hex.replace("#", ""), 16);
    return Number.isNaN(n) ? hex : `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  };

  // Notes under a chart or table: one per line in a <ul class="notes">, each after "- ".
  // Each note is text, or {html} for one with a link; empty ones are left out.
  function setNotes(list, notes) {
    list.replaceChildren(...notes.filter(Boolean).map((note) => {
      const li = document.createElement("li");
      if (typeof note === "string") li.textContent = note;
      else li.innerHTML = note.html;
      return li;
    }));
  }

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
        note = ["Dashed line: money you put in.", `Orange: what the same deposits would be worth if each had bought ${benchmark}.`];
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
        note = ["Both lines start at your portfolio's value on the first day shown.",
          `Orange: that amount invested in ${benchmark} that day, with the same deposits and withdrawals since.`];
      } else {
        const b = pts[0];
        const rebase = (end, start) => (end == null || start == null ? null : (1 + end) / (1 + start) - 1);
        datasets = [
          { label: "Portfolio (time-weighted)", data: pts.map((p) => rebase(p.twr, b.twr)), borderColor: c1, borderWidth: 2 },
          { label: benchmark, data: pts.map((p) => rebase(p.benchmark_twr, b.benchmark_twr)), borderColor: c2, borderWidth: 1.5 },
        ];
        yFormat = (v) => pct(v);
        note = ["Time-weighted return removes the effect of deposits and withdrawals, so it compares directly with the index."];
      }
      setNotes($("perf-note"), note);
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
      setNotes($("earnings-note"), [missing.length && `No date announced yet for ${missing.join(", ")}.`, "ETFs and funds don't report earnings."]);
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
      setNotes(hint, JSON.parse(heat ? hint.dataset.heatmapHint : hint.dataset.doughnutHint));
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
      // One row per account: CAD and USD columns always, any other currency only when held.
      const currencies = ["CAD", "USD", ...new Set(cash.map((c) => c.currency).filter((cur) => cur !== "CAD" && cur !== "USD"))];
      const accounts = new Map();
      for (const c of cash) {
        const row = accounts.get(c.account) || { amounts: {}, total: 0 };
        row.amounts[c.currency] = (row.amounts[c.currency] || 0) + c.amount;
        row.total += c.amount_base;
        accounts.set(c.account, row);
      }
      const amountCell = (value, currency) => value == null
        ? '<td class="num muted">—</td>'
        : `<td class="num nowrap ${value < 0 ? "loss" : ""}">${money(value, currency)}</td>`;
      const sumOf = (currency) => cash.some((c) => c.currency === currency)
        ? cash.filter((c) => c.currency === currency).reduce((sum, c) => sum + c.amount, 0) : null;
      const total = cash.reduce((sum, c) => sum + c.amount_base, 0);
      $("cash").innerHTML = `<table class="table compact">
        <thead><tr><th>Account</th>${currencies.map((cur) => `<th class="num">${cur}</th>`).join("")}<th class="num">Total (in ${BASE})</th></tr></thead>
        <tbody>${[...accounts].map(([account, row]) => `<tr><td>${esc(account)}</td>
          ${currencies.map((cur) => amountCell(row.amounts[cur], cur)).join("")}
          <td class="num nowrap ${row.total < 0 ? "loss" : ""}">${money(row.total)}</td></tr>`).join("")}</tbody>
        <tfoot><tr><td>Total</td>${currencies.map((cur) => amountCell(sumOf(cur), cur)).join("")}<td class="num nowrap">${money(total)}</td></tr></tfoot></table>
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
            { label: "Close", data: prices.map((d) => d.close), borderColor: c1, borderWidth: 2, pointRadius: 0, tension: 0.1, order: 0 },
            { label: "Buy", data: buys, showLine: false, pointStyle: "triangle", pointRadius: 7, backgroundColor: cssVar("--gain"), borderColor: cssVar("--gain"), order: -1 },
            { label: "Sell", data: sells, showLine: false, pointStyle: "triangle", rotation: 180, pointRadius: 7, backgroundColor: cssVar("--loss"), borderColor: cssVar("--loss"), order: -1 },
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
    performanceSection(root.dataset, benchmark, {
      onHistory: (history, err) => {
        renderAnnual(history, err, benchmark);
        projections(history, err, benchmark);
      },
    });
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

  // ---- projections (Performance tab) -----------------------------------------------
  // Today's value grown at an assumed yearly rate, compounded monthly, plus a fixed contribution
  // at the end of each month. The growth rate defaults to the portfolio's annualized return so far.
  // Inputs the user changes are remembered in this browser; clearing one brings back its default.
  const PROJECTION_STORAGE = "projectionInputs";
  function projections(history, err, benchmark) {
    const status = $("proj-status");
    if (err) {
      status.textContent = `Couldn't build history: ${err.message}`;
      return;
    }
    const points = history.points || [];
    if (!points.length) {
      status.textContent = "Not enough history yet.";
      return;
    }
    status.hidden = true;
    const now = points[points.length - 1];
    const inputs = { rate: $("proj-rate"), contribution: $("proj-contribution"), years: $("proj-years") };
    const annualized = (history.periods || []).find((p) => p.label === "Annualized");
    if (annualized?.portfolio != null) inputs.rate.value = (annualized.portfolio * 100).toFixed(2);
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(PROJECTION_STORAGE) || "{}") || {};
      for (const [key, el] of Object.entries(inputs)) if (Number.isFinite(saved[key])) el.value = saved[key];
    } catch { /* storage blocked or bad JSON */ }

    // What the past suggests, for reference: annualized returns and the last year's deposits.
    $("proj-rate-hint").textContent = annualized?.portfolio != null
      ? `Defaults to your portfolio's annualized return so far, ${pct(annualized.portfolio, { signed: false })}`
        + (annualized.benchmark != null ? ` (${benchmark}: ${pct(annualized.benchmark, { signed: false })}).` : ".")
      : "Defaults to 6% until there's a year of history to annualize.";
    const yearAgo = new Date(`${now.date}T00:00:00`);
    yearAgo.setFullYear(yearAgo.getFullYear() - 1);
    const start = points.find((p) => p.date >= yearAgo.toISOString().slice(0, 10)) || points[0];
    const months = Math.max((new Date(`${now.date}T00:00:00`) - new Date(`${start.date}T00:00:00`)) / (864e5 * 30.44), 1);
    $("proj-contribution-hint").textContent =
      `About ${money((now.contributions - start.contributions) / months, BASE, { compact: true })} a month over the last ${months >= 11.5 ? "year" : `${Math.round(months)} months`}. Negative for withdrawals.`;

    let chart = null;
    function render() {
      const num = (el, fallback) => (Number.isFinite(parseFloat(el.value)) ? parseFloat(el.value) : fallback);
      const rate = num(inputs.rate, 0) / 100;
      const contribution = num(inputs.contribution, 0);
      const years = Math.min(Math.max(Math.round(num(inputs.years, 20)), 1), 50);

      const monthly = Math.pow(1 + rate, 1 / 12) - 1;
      const baseYear = new Date(`${now.date}T00:00:00`).getFullYear();
      const rows = [{ year: 0, value: now.value, contributions: 0 }];
      let value = now.value, added = 0;
      for (let m = 1; m <= years * 12; m++) {
        value = value * (1 + monthly) + contribution;
        added += contribution;
        if (m % 12 === 0) rows.push({ year: m / 12, value, contributions: added });
      }
      const end = rows[rows.length - 1];
      const cents = (v) => Math.round(v * 100) / 100;  // so float noise doesn't show as "+$0.00"
      const growth = cents(end.value - now.value - end.contributions);
      $("proj-kpis").innerHTML = [
        kpi(`Value in ${years} year${years === 1 ? "" : "s"}`, money(end.value, BASE, { compact: true }), `In ${baseYear + years}`),
        kpi("Today's value", money(now.value, BASE, { compact: true }),
          new Date(`${now.date}T00:00:00`).toLocaleDateString("en-CA", { dateStyle: "medium" })),
        kpi("Contributions", money(end.contributions, BASE, { compact: true, signed: true }),
          `${money(contribution, BASE, { compact: true })} a month`),
        kpi("Investment growth", money(growth, BASE, { compact: true, signed: true }),
          `At ${pct(rate, { signed: false })} a year`, tone(growth)),
      ].join("");

      const c1 = cssVar("--chart-1"), c3 = cssVar("--chart-3");
      const fmt = (v) => money(v, BASE, { compact: true });
      const data = {
        labels: rows.map((r) => (r.year === 0 ? "Now" : String(baseYear + r.year))),
        datasets: [
          { label: "Projected value", data: rows.map((r) => r.value), borderColor: c1, backgroundColor: `${c1}22`, fill: true, borderWidth: 2 },
          { label: "Today's value + contributions", data: rows.map((r) => now.value + r.contributions),
            borderColor: c3, borderDash: [4, 4], borderWidth: 1.5 },
        ],
      };
      data.datasets.forEach((d) => Object.assign(d, { pointRadius: 0, pointHoverRadius: 3, tension: 0.15 }));
      if (chart) {
        chart.data = data;
        chart.update();
      } else {
        chart = new Chart($("proj-chart"), {
          type: "line",
          data,
          options: {
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 11, autoSkip: true, maxRotation: 0 } },
              y: { ticks: { callback: fmt }, grace: "5%" },
            },
            plugins: {
              legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 2 } },
              tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(c.parsed.y)}` } },
            },
          },
        });
      }

      // Every year up to 10, then every 5, always ending on the last year.
      const step = years <= 10 ? 1 : 5;
      const shown = rows.filter((r) => r.year > 0 && (r.year % step === 0 || r.year === years));
      $("proj-table").innerHTML = `
        <thead><tr><th>Year</th><th class="num">Projected value</th><th class="num">Contributions</th><th class="num">Investment growth</th></tr></thead>
        <tbody>${shown.map((r) => {
          const g = cents(r.value - now.value - r.contributions);
          return `<tr>
            <td class="nowrap">${baseYear + r.year} <span class="muted">(${r.year} yr${r.year === 1 ? "" : "s"})</span></td>
            <td class="num nowrap">${money(r.value)}</td>
            <td class="num nowrap">${money(r.contributions, BASE, { signed: true })}</td>
            <td class="num nowrap ${tone(g)}">${money(g, BASE, { signed: true })}</td>
          </tr>`;
        }).join("")}</tbody>`;
    }

    for (const [key, el] of Object.entries(inputs)) {
      el.addEventListener("input", () => {
        const value = parseFloat(el.value);
        if (Number.isFinite(value)) saved[key] = value;
        else delete saved[key];
        try { localStorage.setItem(PROJECTION_STORAGE, JSON.stringify(saved)); } catch { /* storage blocked */ }
        render();
      });
    }
    render();
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
      setNotes($("fg-note"), [`From CNN Business${updated ? ` (updated ${updated})` : ""}.`,
        "0 is extreme fear and 100 is extreme greed; the index averages the seven indicators above."]);
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

      $("yield-table").innerHTML = `
        <thead><tr><th>Maturity</th><th class="num">Yield</th>${comparisons.map((c) =>
          `<th class="num" title="${c.date ? esc(longDate(c.date)) : ""}">vs ${esc(COMPARE[c.key])}</th>`).join("")}</tr></thead>
        <tbody>${tenors.map((t) => `<tr><td>${esc(t)}</td><td class="num">${yieldText(at(latest, t))}</td>${comparisons.map((c) =>
          `<td class="num">${bpsText(bps(at(latest, t), at(c, t)))}</td>`).join("")}</tr>`).join("")}</tbody>`;

      // Spread views need the spread's history: the Yahoo fallback has no 2-year yield.
      for (const b of $("yield-mode").querySelectorAll("button")) {
        const missing = b.dataset.mode !== "curve" && !data.spreads?.[b.dataset.mode]?.length;
        b.disabled = missing;
        b.title = missing ? "Needs the U.S. Treasury's data, which couldn't be reached" : "";
      }
      if ($("yield-mode").querySelector(`[data-mode="${yieldView.mode}"]`).disabled) yieldView.mode = "curve";
      $("yield-status").hidden = true;
      ratesData = data;
      drawYield();
    }

    // Yield chart: the curve itself, or the daily history of one spread ----------------
    const SPREAD_LABELS = { "10Y-2Y": "10Y − 2Y", "10Y-3M": "10Y − 3M" };
    const RANGE_DAYS = { "1Y": 365, "2Y": 730, "5Y": 1826 };
    const yieldView = { mode: "curve", range: "5Y" };
    let ratesData = null, yieldChart = null;
    function drawYield() {
      const data = ratesData, mode = yieldView.mode, spread = mode !== "curve";
      for (const b of $("yield-mode").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
      for (const b of $("yield-range").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.range === yieldView.range));
      $("yield-range").hidden = !spread;
      $("yield-table").hidden = spread;
      $("yield-chart").setAttribute("aria-label", spread ? `${SPREAD_LABELS[mode]} Treasury yield spread over time` : "US Treasury yield curve");
      yieldChart?.destroy();
      yieldChart = spread ? spreadChart(data.spreads[mode], SPREAD_LABELS[mode]) : curveChart(data);

      const latest = data.curves.find((c) => c.key === "latest");
      const source = data.source === "treasury"
        ? [{ html: `Daily par yields from the <a href="${esc(data.source_url)}" target="_blank" rel="noopener">U.S. Treasury</a>${latest?.date ? `, as of ${esc(longDate(latest.date))}` : ""}.` },
          "Published each business day after the close."]
        : [spread
          ? "The U.S. Treasury site couldn't be reached, so this uses the 10-year and 13-week bill yields Yahoo Finance quotes."
          : "The U.S. Treasury site couldn't be reached, so this shows the four Treasury yields Yahoo Finance quotes (the 3-month is the 13-week bill)."];
      setNotes($("yield-note"), spread ? [...source, `The spread is the ${mode.split("-")[0]} yield minus the ${mode.split("-")[1]} yield.`,
        "Below zero (shaded red) the curve is inverted: short-term Treasuries pay more than long-term ones, which has often come before US recessions."] : source);
    }

    function spreadChart(points, label) {
      const end = new Date(`${points[points.length - 1][0]}T00:00:00`);
      end.setDate(end.getDate() - RANGE_DAYS[yieldView.range]);
      const from = end.toISOString().slice(0, 10);
      const shown = points.filter((p) => p[0] >= from);
      const labels = shown.map((p) => p[0]);
      return new Chart($("yield-chart"), {
        type: "line",
        data: {
          labels,
          datasets: [{
            label, data: shown.map((p) => Math.round(p[1] * 100)), borderColor: cssVar("--chart-1"), borderWidth: 1.5,
            pointRadius: 0, tension: 0,
            fill: { target: { value: 0 }, above: withAlpha(cssVar("--gain"), 0.12), below: withAlpha(cssVar("--loss"), 0.2) },
          }],
        },
        options: {
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { grid: { display: false }, ticks: dateTicks(labels) },
            y: { grace: "5%", ticks: { callback: (v) => `${v} bps` },
              grid: { color: (ctx) => (ctx.tick.value === 0 ? cssVar("--muted") : cssVar("--border")) } },
          },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: {
              title: (items) => longDate(labels[items[0].dataIndex]),
              label: (c) => ` ${label}: ${bpsText(c.parsed.y)}${c.parsed.y < 0 ? " (inverted)" : ""}`,
            } },
          },
        },
      });
    }

    function curveChart(data) {
      const { tenors, curves } = data;
      return new Chart($("yield-chart"), {
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
    }

    $("yield-mode").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-mode]");
      if (!b || b.disabled || !ratesData) return;
      yieldView.mode = b.dataset.mode;
      drawYield();
    });
    $("yield-range").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-range]");
      if (!b || !ratesData) return;
      yieldView.range = b.dataset.range;
      drawYield();
    });

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

  // ---- My Assets tab ---------------------------------------------------------------
  // Asset forms show only the fields for the kind of asset chosen (portfolio, accounts or a value).
  function assetForms() {
    document.querySelectorAll("[data-asset-kind]").forEach((group) => {
      const form = group.closest("form");
      const update = () => {
        const kind = form.querySelector('input[name="kind"]:checked')?.value;
        form.querySelectorAll("[data-kind]").forEach((el) => { el.hidden = el.dataset.kind !== kind; });
      };
      group.addEventListener("change", update);
      update();
    });
  }

  // The net worth tiles at the top of My Assets, also shown on the dashboard.
  function worthKpis(data) {
    const investments = data.assets.filter((a) => a.kind !== "manual").reduce((s, a) => s + a.value, 0);
    const count = data.mortgages.length;
    return [
      kpi("Net worth", money(data.net_worth), "Assets minus mortgages", data.net_worth < 0 ? "loss" : ""),
      kpi("Total assets", money(data.total_assets),
        `${money(investments)} investments · ${money(data.total_assets - investments)} other`),
      kpi("Mortgages owed", money(data.total_liabilities), count ? `${count} mortgage${count === 1 ? "" : "s"}` : "No mortgages"),
    ].join("");
  }

  // My Assets on the dashboard: just the net worth tiles; My Assets has the details and warnings.
  function dashboardWorth() {
    const box = $("worth-kpis");
    if (!box) return;
    async function load() {
      try {
        box.innerHTML = worthKpis(await getJSON(box.dataset.assetsUrl));
      } catch (err) {
        box.innerHTML = `<p class="muted">Couldn't load your assets: ${esc(err.message)}</p>`;
      }
    }
    load();
    setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  }

  function myAssets() {
    const urls = root.dataset;
    const monthName = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-CA", { month: "long", year: "numeric" });
    chartDefaults();
    assetForms();
    // A form that came back with errors is further down the page: bring it into view.
    document.querySelector("form[data-errors]")?.scrollIntoView({ block: "start" });

    // Net worth: portfolio and account values are live, so they come from the API.
    async function loadWorth() {
      let data;
      try {
        data = await getJSON(urls.assetsUrl);
      } catch (err) {
        $("as-of").textContent = `Couldn't load values: ${err.message}`;
        $("worth-kpis").innerHTML = "";
        return;
      }
      const updated = new Date(data.as_of).toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit" });
      const live = data.assets.some((a) => a.kind !== "manual");
      $("as-of").textContent = live
        ? `Portfolios and accounts at live prices from Yahoo Finance · updated ${updated} · values in ${BASE}`
        : `Values in ${BASE}`;
      notices(data.warnings.map((w) => ["warning", w]));

      $("worth-kpis").innerHTML = worthKpis(data);

      for (const a of data.assets) {
        const row = document.querySelector(`tr[data-asset="${a.id}"]`);
        if (!row) continue;
        row.querySelector("[data-value]").textContent = money(a.value);
        row.querySelector("[data-share]").textContent = data.total_assets > 0 ? pct(a.value / data.total_assets, { signed: false }) : "—";
      }
      if ($("total-assets")) $("total-assets").textContent = money(data.total_assets);
      if ($("net-worth-total")) {
        $("net-worth-total").textContent = money(data.net_worth);
        $("net-worth-total").className = `num nowrap ${data.net_worth < 0 ? "loss" : ""}`;
      }
    }

    // Mortgages: one payback chart that shows any of the mortgages, each on its own or combined into one.
    function mortgageChart(card, mortgages) {
      const canvas = card.querySelector("canvas");
      const status = card.querySelector(".chart-status");
      const state = { mode: "balance", combined: false, continued: false, shown: new Set(mortgages.map((m) => m.id)) };
      const note = card.querySelector("[data-continued-note]");
      const continuedBox = card.querySelector("[data-continued]");
      const pickText = status.textContent;
      const colorOf = (m) => {
        const i = mortgages.findIndex((x) => x.id === m.id);
        return i === 0 ? cssVar("--chart-1") : SECTOR_COLORS[i % SECTOR_COLORS.length];
      };
      card.querySelectorAll(".pick").forEach((b) => {
        const m = mortgages.find((x) => String(x.id) === b.dataset.id);
        if (mortgages.indexOf(m) > 0) b.style.setProperty("--pick", colorOf(m));
      });
      // Continued contributions depend on which mortgages are paid together, so the server works
      // them out for each set picked.
      const continuedCache = new Map();
      const continuedFor = (picked) => {
        const ids = picked.map((m) => m.id).sort((a, b) => a - b).join(",");
        if (!continuedCache.has(ids)) {
          continuedCache.set(ids, getJSON(`${card.dataset.continuedUrl}?ids=${ids}`).catch((err) => {
            continuedCache.delete(ids);
            throw err;
          }));
        }
        return continuedCache.get(ids);
      };
      const sum = (values) => {
        const known = values.filter((v) => v != null);
        return known.length ? Math.round(known.reduce((s, v) => s + v, 0) * 100) / 100 : null;
      };
      const options = (scales, plugins) => ({
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        scales: { ...scales, y: { ...scales.y, ticks: { callback: (v) => money(v, BASE, { compact: true }) } } },
        plugins: { ...plugins, legend: { position: "bottom", labels: { boxWidth: 12 }, ...plugins.legend } },
      });

      function balanceChart(picked, combined) {
        // Each mortgage's months start at its own start (or statement) date, so line them up.
        const labels = [...new Set(picked.flatMap((m) => m.dates))].sort();
        const now = labels.findIndex((iso) => iso >= urls.today);  // the month we are in
        const align = (m, values) => {
          const at = new Map(m.dates.map((d, i) => [d, values[i]]));
          return labels.map((d) => at.get(d) ?? null);
        };
        const cols = new Map(picked.map((m) => [m, {
          balance: align(m, m.balance), base: align(m, m.base_balance || m.balance) }]));
        const total = (key) => labels.map((_, i) => sum(picked.map((m) => cols.get(m)[key][i])));
        const surface = cssVar("--surface"), c3 = cssVar("--chart-3");
        const line = (label, data, color, fill) => ({
          label, data, borderColor: color, backgroundColor: `${color}22`, fill, borderWidth: 2,
          // Solid up to this month, dashed for the months still to come.
          segment: { borderDash: (ctx) => (now >= 0 && ctx.p0DataIndex >= now ? [6, 4] : undefined) },
          pointRadius: labels.map((_, i) => (i === now ? 4 : 0)), pointBackgroundColor: color,
          pointBorderColor: surface, pointBorderWidth: 2, pointHoverRadius: 4,
        });
        const base = (data) => ({ label: picked[0].continued ? "Without continued contributions" : "Without extra payments",
          data, borderColor: c3, borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 3 });

        let datasets;
        if (combined) {
          datasets = [line("Total balance", total("balance"), cssVar("--chart-1"), true)];
          if (picked.some((m) => m.base_balance)) datasets.push(base(total("base")));
        } else if (picked.length === 1) {
          const m = picked[0];
          datasets = [line("Balance", cols.get(m).balance, colorOf(m), true)];
          if (m.base_balance) datasets.push(base(cols.get(m).base));
        } else {
          datasets = picked.map((m) => line(m.name, cols.get(m).balance, colorOf(m), false));
        }
        return new Chart(canvas, {
          type: "line",
          data: { labels, datasets },
          options: options({
            x: { grid: { display: false },
              ticks: { maxTicksLimit: 8, autoSkip: true, maxRotation: 0,
                callback: (v) => new Date(`${labels[v]}T00:00:00`).getFullYear() } },
            y: { beginAtZero: true },
          }, {
            legend: { display: datasets.length > 1 },
            tooltip: {
              filter: (item) => item.parsed.y != null,
              callbacks: {
                title: (items) => `End of ${monthName(labels[items[0].dataIndex])}${items[0].dataIndex === now ? " (this month)" : ""}`,
                label: (c) => ` ${c.dataset.label}: ${money(c.parsed.y)}`,
                // The combined line also lists what each mortgage adds to it.
                afterBody: (items) => (combined ? picked
                  .map((m) => [m.name, cols.get(m).balance[items[0].dataIndex]])
                  .filter(([, v]) => v != null)
                  .map(([name, v]) => `   ${name}: ${money(v)}`) : []),
              },
            },
          }),
        });
      }

      function yearsChart(picked, combined) {
        const years = [...new Set(picked.flatMap((m) => m.years.map((y) => y.year)))].sort((a, b) => a - b);
        const byYear = (m, key) => {
          const at = new Map(m.years.map((y) => [y.year, y[key]]));
          return years.map((y) => at.get(y) ?? null);
        };
        const bar = { borderColor: cssVar("--surface"), borderWidth: { top: 2 }, borderSkipped: "bottom" };
        const top = { borderRadius: { topLeft: 4, topRight: 4 } };
        let datasets;
        if (combined || picked.length === 1) {
          const total = (key) => years.map((_, i) => sum(picked.map((m) => byYear(m, key)[i])));
          datasets = [
            { label: "Principal", data: total("principal"), backgroundColor: cssVar("--chart-1"), stack: "paid", ...bar },
            { label: "Interest", data: total("interest"), backgroundColor: cssVar("--chart-2"), stack: "paid", ...top, ...bar },
          ];
        } else {
          // A bar for each mortgage: principal in its colour, interest in a lighter shade of it.
          datasets = picked.flatMap((m) => {
            const color = colorOf(m), stack = `m${m.id}`;
            return [
              { label: `${m.name} · principal`, data: byYear(m, "principal"), backgroundColor: color, stack, ...bar },
              { label: `${m.name} · interest`, data: byYear(m, "interest"), backgroundColor: `${color}66`, stack, ...top, ...bar },
            ];
          });
        }
        return new Chart(canvas, {
          type: "bar",
          data: { labels: years, datasets },
          options: options({
            x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } },
            y: { stacked: true },
          }, {
            tooltip: {
              filter: (item) => item.parsed.y != null,
              callbacks: {
                label: (c) => ` ${c.dataset.label}: ${money(c.parsed.y)}`,
                footer: (items) => `Paid in ${items[0].label}: ${money(items.reduce((s, i) => s + i.parsed.y, 0))}`,
              },
            },
          }),
        });
      }

      let chart = null, drawn = 0;
      const clear = (message) => {
        chart?.destroy();
        chart = null;
        status.textContent = message;
        status.hidden = !message;
      };
      async function draw() {
        const run = ++drawn;
        let picked = mortgages.filter((m) => state.shown.has(m.id));
        const combined = state.combined && picked.length > 1;
        const continued = state.continued && picked.length > 1;
        card.querySelectorAll("button[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === state.mode)));
        card.querySelectorAll(".pick").forEach((b) => b.setAttribute("aria-pressed", String(state.shown.has(Number(b.dataset.id)))));
        card.querySelectorAll("button[data-combine]").forEach((b) => {
          b.setAttribute("aria-pressed", String(b.dataset.combine === String(state.combined)));
          b.disabled = picked.length < 2;
        });
        if (continuedBox) continuedBox.disabled = picked.length < 2;
        card.querySelector("[data-balance-only]").hidden = state.mode !== "balance";
        if (note) note.hidden = true;
        if (!picked.length) {
          clear(pickText);
          return;
        }

        if (continued) {
          let c;
          try {
            c = await continuedFor(picked);
          } catch (err) {
            if (run === drawn) clear(`Couldn't work out continued contributions: ${err.message}`);
            return;
          }
          if (run !== drawn) return;  // the picks changed while it loaded
          const byId = new Map(c.mortgages.map((x) => [x.id, x]));
          picked = picked.map((m) => ({ ...m, balance: byId.get(m.id).balance, years: byId.get(m.id).years,
            base_balance: m.balance, continued: true }));
          note.textContent = c.months_saved > 0 || c.interest_saved >= 0.01
            ? `With continued contributions, these mortgages would be paid off by ${monthName(c.payoff_date)}, `
              + `${c.sooner} sooner, with ${money(c.interest_saved)} less interest. `
              + "Once one is paid off, its payments go to the remaining mortgage with the highest rate."
            : "Continued contributions change nothing here: no mortgage is paid off while another still has a balance to pay.";
          note.hidden = false;
        }
        clear("");
        const names = picked.map((m) => m.name).join(", ");
        canvas.setAttribute("aria-label", `${state.mode === "years" ? "Principal and interest paid each year" : "Estimated balance until paid off"}`
          + ` for ${combined ? "the combined " : ""}${names} mortgage${picked.length === 1 ? "" : "s"}`);
        chart = state.mode === "years" ? yearsChart(picked, combined) : balanceChart(picked, combined);
      }
      card.addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (!b || b.disabled) return;
        if (b.dataset.mode) state.mode = b.dataset.mode;
        else if (b.dataset.combine) state.combined = b.dataset.combine === "true";
        else if (b.dataset.id) {
          const id = Number(b.dataset.id);
          if (state.shown.has(id)) state.shown.delete(id);
          else state.shown.add(id);
        } else return;
        draw();
      });
      card.addEventListener("change", (e) => {
        if (!e.target.matches("[data-continued]")) return;
        state.continued = e.target.checked;
        draw();
      });
      draw();
    }

    const mortgageData = $("mortgage-chart-data");
    if (mortgageData && window.Chart) mortgageChart($("mortgage-chart"), JSON.parse(mortgageData.textContent));

    loadWorth();
    setInterval(() => { if (!document.hidden) loadWorth(); }, REFRESH_MS);
  }

  // ---- Research tab ------------------------------------------------------------------
  // Indicator maths over a series of closes; each returns one value (or null) per close.
  function smaLine(values, n) {
    const out = new Array(values.length).fill(null);
    let sum = 0;
    values.forEach((v, i) => {
      sum += v;
      if (i >= n) sum -= values[i - n];
      if (i >= n - 1) out[i] = sum / n;
    });
    return out;
  }
  function emaLine(values, n) {  // seeded with the simple average of the first n closes
    const out = new Array(values.length).fill(null);
    const k = 2 / (n + 1);
    let ema = 0;
    values.forEach((v, i) => {
      if (i < n) ema += v / n;
      else ema = v * k + ema * (1 - k);
      if (i >= n - 1) out[i] = ema;
    });
    return out;
  }
  // Bollinger Bands: [upper, middle, lower] at each close, the n-bar SMA plus and minus k standard deviations.
  function bollingerLine(values, n = 20, k = 2) {
    return values.map((_, i) => {
      if (i < n - 1) return null;
      const last = values.slice(i - n + 1, i + 1);
      const mean = last.reduce((s, v) => s + v, 0) / n;
      const sd = Math.sqrt(last.reduce((s, v) => s + (v - mean) ** 2, 0) / n);
      return [mean + k * sd, mean, mean - k * sd];
    });
  }
  // MACD: [MACD, signal, histogram] at each close. MACD is the fast EMA minus the slow one, the
  // signal is an n-bar EMA of MACD, and the histogram is MACD minus the signal.
  function macdLine(values, fast = 12, slow = 26, n = 9) {
    const f = emaLine(values, fast), s = emaLine(values, slow);
    const start = slow - 1;  // the first bar with both EMAs
    const macd = values.map((_, i) => (i >= start ? f[i] - s[i] : null));
    const signal = new Array(start).fill(null).concat(emaLine(macd.slice(start), n));
    return macd.map((m, i) => (m == null ? null : [m, signal[i], signal[i] == null ? null : m - signal[i]]));
  }
  function rsiLine(values, n = 14) {  // Wilder's smoothing
    const out = new Array(values.length).fill(null);
    let gain = 0, loss = 0;
    for (let i = 1; i < values.length; i++) {
      const change = values[i] - values[i - 1];
      const g = Math.max(change, 0), l = Math.max(-change, 0);
      if (i <= n) { gain += g / n; loss += l / n; } else { gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n; }
      if (i >= n) out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    }
    return out;
  }
  const weekOf = (iso) => {  // the Monday starting an ISO date's week
    const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
    return d.toISOString().slice(0, 10);
  };
  // Average of the last n weekly closes, where the current week's close is the latest price.
  function weeklyMaLine(dates, values, n) {
    const out = new Array(values.length).fill(null);
    const weeks = dates.map(weekOf);
    const done = [];  // closes of finished weeks
    let sum = 0;  // of the last n - 1 of them
    values.forEach((v, i) => {
      if (i > 0 && weeks[i] !== weeks[i - 1]) {
        done.push(values[i - 1]);
        sum += values[i - 1];
        if (done.length > n - 1) sum -= done[done.length - n];
      }
      if (done.length >= n - 1) out[i] = (sum + v) / n;
    });
    return out;
  }

  const RESEARCH_INDICATORS = {
    sma10: { label: "10 SMA", line: (c) => smaLine(c, 10) },
    sma20: { label: "20 SMA", line: (c) => smaLine(c, 20) },
    sma50: { label: "50 SMA", line: (c) => smaLine(c, 50) },
    sma100: { label: "100 SMA", line: (c) => smaLine(c, 100), dashed: true },
    sma200: { label: "200-day SMA", line: (c) => smaLine(c, 200), dashed: true, daily: true },
    ma200w: { label: "200-week MA", line: (c, dates) => weeklyMaLine(dates, c, 200), dashed: true, daily: true },
    ema10: { label: "10 EMA", line: (c) => emaLine(c, 10) },
    ema20: { label: "20 EMA", line: (c) => emaLine(c, 20) },
    bb: { label: "BB (20, 2)", line: (c) => bollingerLine(c), bands: true },
    rsi: { label: "RSI (14)", line: (c) => rsiLine(c), pane: true },
    macd: { label: "MACD (12, 26, 9)", line: (c) => macdLine(c), pane: true },
  };
  const RESEARCH_RANGES = { "1M": [0, 1], "6M": [0, 6], "1Y": [1, 0], "3Y": [3, 0], "5Y": [5, 0], "10Y": [10, 0] };  // [years, months]
  const INTRADAY_RANGES = ["1D", "1W"];
  const CHART_STYLES = ["candles", "line", "colored", "baseline"];
  // Each mode's settings when nothing is saved for it, and after its Reset.
  const RESEARCH_DEFAULTS = {
    default: { style: "line", ind: [], trades: true, volume: true, events: true, range: "YTD", notes: false },
    ta: { style: "candles", ind: ["rsi", "bb", "sma10", "sma20", "sma50", "sma200"], trades: false, volume: true, events: true,
      range: "YTD", notes: false },
  };
  // Settings are saved per mode under this key. The older "research" key saved every setting of both
  // modes on any change, so it kept defaults from before these and is dropped.
  const RESEARCH_STORAGE = "research.v2";
  const presetRange = (r) => r in RESEARCH_RANGES || ["1D", "1W", "YTD", "Max"].includes(r);
  // A mode's settings: what was saved for it, where still valid, and its defaults for the rest.
  function modeSettings(mode, saved = {}) {
    const d = RESEARCH_DEFAULTS[mode];
    const flag = (k) => (typeof saved[k] === "boolean" ? saved[k] : d[k]);
    return {
      style: CHART_STYLES.includes(saved.style) ? saved.style : d.style,
      ind: new Set(Array.isArray(saved.ind) ? saved.ind.filter((k) => k in RESEARCH_INDICATORS) : d.ind),
      trades: flag("trades"), volume: flag("volume"), events: flag("events"), notes: flag("notes"),
      range: presetRange(saved.range) ? saved.range : d.range,
    };
  }
  const COMPARE_COLORS = ["--cmp-1", "--cmp-2", "--cmp-3", "--cmp-4", "--cmp-5"];  // CSS tokens, one per compared symbol
  const MAX_COMPARES = COMPARE_COLORS.length;

  function research() {
    chartDefaults();
    const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const dayText = (iso, opts = { dateStyle: "medium" }) => new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString("en-CA", opts);
    const menu = $("ta-menu");

    const saved = {};
    try {
      Object.assign(saved, JSON.parse(localStorage.getItem(RESEARCH_STORAGE) || "{}"));
      localStorage.removeItem("research");
    } catch { /* storage blocked */ }
    const state = {
      symbol: root.dataset.symbol,
      mode: saved.mode === "ta" ? "ta" : "default",
      // Each mode's chart type, indicators, toggles, time range and notes.
      modes: { default: modeSettings("default", saved.modes?.default), ta: modeSettings("ta", saved.modes?.ta) },
      range: "", from: "", to: "",  // the range shown: the mode's, or "custom" for picked dates
      main: null,
      compares: [],  // {symbol, color, data}
      zoom: null,  // {from, to}: the first and last bars zoomed to, or null for the whole range
    };
    const cur = () => state.modes[state.mode];
    state.range = cur().range;
    const save = () => {
      try {
        localStorage.setItem(RESEARCH_STORAGE, JSON.stringify({
          mode: state.mode,
          modes: Object.fromEntries(Object.entries(state.modes).map(([m, s]) => [m, { ...s, ind: [...s.ind] }])),
        }));
      } catch { /* storage blocked */ }
      syncReset();
    };

    // ---- data
    // The first day shown, or "" for the whole history.
    function visibleStart() {
      if (state.range === "custom") return state.from;
      if (state.range === "Max") return "";
      const d = new Date();
      if (state.range === "YTD") d.setMonth(0, 1);
      else if (INTRADAY_RANGES.includes(state.range)) d.setDate(d.getDate() - 10);
      else {
        const [years, months] = RESEARCH_RANGES[state.range];
        d.setFullYear(d.getFullYear() - years, d.getMonth() - months);
      }
      return isoDate(d);
    }

    // Responses are kept for a while; a daily history going back far enough serves later ranges too.
    const cache = new Map();  // query -> {at, promise}
    const earliest = {};  // symbol -> earliest daily start fetched ("" = all of it)
    function load(symbol) {
      let q;
      if (INTRADAY_RANGES.includes(state.range)) q = `span=${state.range}`;
      else {
        const start = visibleStart();
        q = `start=${(earliest[symbol] != null && earliest[symbol] <= start ? earliest[symbol] : start) || "max"}`;
      }
      const key = `${symbol}?${q}`;
      const hit = cache.get(key);
      if (hit && Date.now() - hit.at < (q.startsWith("span") ? 2 : 30) * 60_000) return hit.promise;
      const url = `${root.dataset.api.replace("SYMBOL", encodeURIComponent(symbol))}?${q}`;
      const promise = getJSON(url);
      cache.set(key, { at: Date.now(), promise });
      promise.then(() => {
        if (q.startsWith("span")) return;
        const start = q === "start=max" ? "" : q.slice(6);
        if (earliest[symbol] == null || start < earliest[symbol]) earliest[symbol] = start;
      }, () => cache.delete(key));
      return promise;
    }

    let seq = 0;
    async function refresh() {
      const mine = ++seq;
      status("Loading…");
      const [main, ...others] = await Promise.allSettled([load(state.symbol), ...state.compares.map((c) => load(c.symbol))]);
      if (mine !== seq) return;  // a newer request took over
      if (main.status === "rejected") {
        state.main = null;
        clearCharts();
        $("r-title").textContent = state.symbol;
        $("r-price").textContent = "";
        status(`Couldn't load ${state.symbol}: ${main.reason.message}`);
        return;
      }
      state.main = main.value;
      state.compares.forEach((c, i) => { c.data = others[i].status === "fulfilled" ? others[i].value : null; });
      draw();
    }

    // The bars shown, each indicator's value at each, and what one bar is: "bar" (intraday), "day", "week" or "month".
    function series(d) {
      const daily = d.daily.map(([t, o, h, l, c, v]) => ({ t, o, h, l, c, v: v || 0, from: t }));
      const dCloses = daily.map((r) => r.c), dDates = daily.map((r) => r.t);
      const dailyInd = Object.fromEntries(Object.entries(RESEARCH_INDICATORS).map(([k, def]) => [k, def.line(dCloses, dDates)]));

      if (INTRADAY_RANGES.includes(state.range)) {
        const bars = (d.bars || []).map(([t, o, h, l, c, v]) => ({ t, o, h, l, c, v: v || 0 }));
        const days = [...new Set(bars.map((b) => b.t.slice(0, 10)))].slice(state.range === "1D" ? -1 : -5);
        const first = bars.findIndex((b) => b.t >= days[0]);
        const bCloses = bars.map((b) => b.c), bDates = bars.map((b) => b.t);
        const ind = {};
        for (const [k, def] of Object.entries(RESEARCH_INDICATORS)) {
          if (def.daily) {  // the 200-day and 200-week averages stay daily: each bar gets its day's value
            const byDay = new Map(dDates.map((t, i) => [t, dailyInd[k][i]]));
            ind[k] = bars.map((b) => byDay.get(b.t.slice(0, 10)) ?? null).slice(first);
          } else {
            ind[k] = def.line(bCloses, bDates).slice(first);
          }
        }
        return { rows: first < 0 ? [] : bars.slice(first), ind, unit: "bar", startClose: bars[first - 1]?.c ?? bars[first]?.o };
      }

      const from = visibleStart();
      const to = state.range === "custom" && state.to ? state.to : "9999";
      let lo = daily.findIndex((r) => r.t >= from);
      if (lo < 0) lo = daily.length;
      let hi = daily.length;
      while (hi > lo && daily[hi - 1].t > to) hi--;
      // Long ranges group days into weeks or months, so candles stay wide enough to read.
      const n = hi - lo;
      const unit = state.mode === "ta" ? (n > 1600 ? "month" : n > 400 ? "week" : "day") : n > 3000 ? "week" : "day";
      const groups = [];
      for (let i = lo; i < hi; i++) {
        const key = unit === "day" ? i : unit === "week" ? weekOf(daily[i].t) : daily[i].t.slice(0, 7);
        const last = groups[groups.length - 1];
        if (last && last.key === key) last.end = i; else groups.push({ key, start: i, end: i });
      }
      const rows = groups.map(({ start, end }) => {
        const part = daily.slice(start, end + 1);
        return { t: daily[end].t, from: daily[start].t, o: daily[start].o, c: daily[end].c,
          h: Math.max(...part.map((r) => r.h)), l: Math.min(...part.map((r) => r.l)), v: part.reduce((sum, r) => sum + r.v, 0) };
      });
      const ind = Object.fromEntries(Object.entries(dailyInd).map(([k, vals]) => [k, groups.map((g) => vals[g.end])]));
      return { rows, ind, unit, startClose: daily[lo - 1]?.c ?? daily[lo]?.o };
    }

    // Buys and sells on the bars they fall in, for a line whose close at each bar is closes[i].
    // Trades have no time of day, so on intraday charts each goes on its day's bar closest to the trade price.
    function tradeMarks(trades, rows, unit, closes) {
      const buys = rows.map(() => null), sells = rows.map(() => null), info = rows.map(() => []);
      if (!rows.length) return { buys, sells, info, count: 0 };
      let count = 0;
      trades.forEach((t) => {
        let i = -1;
        if (unit === "bar") {
          rows.forEach((r, j) => {
            if (r.t.startsWith(t.date) && closes[j] != null
              && (i < 0 || Math.abs(closes[j] - t.price) < Math.abs(closes[i] - t.price))) i = j;
          });
        } else if (t.date >= rows[0].from) {
          i = rows.findIndex((r) => r.t >= t.date);
        }
        if (i < 0 || closes[i] == null) return;
        (t.kind === "BUY" ? buys : sells)[i] = t.price;
        info[i].push(t);
        count++;
      });
      return { buys, sells, info, count };
    }

    // Dividends (D), earnings reports (E) and splits (S) on the bars they fall in: the bar covering
    // the day, or on intraday charts its first bar; the next bar for a day without trading.
    // Returns each bar's events, its badges (the last event of each kind) and the kinds there are,
    // which get a row of badges each: E at the bottom, then D, then S.
    function eventMarks(events, rows, unit) {
      const marks = rows.map(() => []);
      let count = 0;
      if (events && rows.length) {
        const day = unit === "bar" ? (r) => r.t.slice(0, 10) : (r) => r.t;
        const first = (rows[0].from || rows[0].t).slice(0, 10), last = day(rows[rows.length - 1]);
        const place = (kind, e) => {
          if (e.date < first || e.date > last) return;
          marks[rows.findIndex((r) => day(r) >= e.date)].push({ kind, ...e });
          count++;
        };
        events.earnings.forEach((e) => place("E", e));
        events.dividends.forEach((e) => place("D", e));
        events.splits.forEach((e) => place("S", e));
      }
      const kinds = ["E", "D", "S"].filter((k) => marks.some((list) => list.some((e) => e.kind === k)));
      const badges = marks.map((list) => kinds.map((k) => list.filter((e) => e.kind === k).pop()).filter(Boolean));
      return { marks, badges, count, kinds, lanes: kinds.length };
    }

    // A compared symbol's closes on the main chart's bars (its latest close at or before each one).
    function compareCloses(c, rows, unit) {
      const src = unit === "bar" ? c.data.bars || [] : c.data.daily;
      const out = [];
      let j = -1;
      rows.forEach((r) => {
        while (j + 1 < src.length && src[j + 1][0] <= r.t) j++;
        out.push(j >= 0 ? src[j][4] : null);
      });
      return out;
    }

    // ---- drawing
    let chart = null, volumeChart = null, rsiChart = null, macdChart = null;
    const AXIS_WIDTH = 76;  // every pane's y axis, on the right, is this wide, so their bars line up
    const fixWidth = (scale) => { scale.width = AXIS_WIDTH; };
    function clearCharts() {
      chart?.destroy();
      volumeChart?.destroy();
      rsiChart?.destroy();
      macdChart?.destroy();
      chart = volumeChart = rsiChart = macdChart = null;
      $("volume-box").hidden = $("rsi-box").hidden = $("macd-box").hidden = true;
      win = null;
      syncZoom();
    }
    const translucent = (color, alpha) => {  // a #rrggbb colour at some opacity
      const m = /^#([0-9a-f]{6})$/i.exec(color);
      if (!m) return color;
      const n = parseInt(m[1], 16);
      return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
    };
    const compactNumber = new Intl.NumberFormat("en-CA", { notation: "compact", maximumFractionDigits: 1 });
    function status(text) {
      $("r-status").hidden = !text;
      $("r-status").textContent = text || "";
    }

    function priceFormat(currency) {
      let f;
      try { f = new Intl.NumberFormat("en-CA", { style: "currency", currency, currencyDisplay: "narrowSymbol" }); } catch { f = null; }
      return (v) => (v == null ? "—" : f ? f.format(v) : v.toFixed(2));
    }

    // Tick labels for bars lo to hi, the part of the chart in view.
    function xTicks(rows, unit, lo = 0, hi = rows.length - 1) {
      if (unit === "bar" && rows[lo].t.slice(0, 10) !== rows[hi].t.slice(0, 10)) {  // one tick per day, on its first bar
        return { maxRotation: 0, autoSkip: false,
          callback: (v) => (v === lo || rows[v].t.slice(0, 10) !== rows[v - 1].t.slice(0, 10)
            ? dayText(rows[v].t, { weekday: "short", day: "numeric" }) : null) };
      }
      const base = { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 };
      if (unit === "bar") return { ...base, callback: (v) => rows[v].t.slice(11) };
      const days = hi > lo ? (new Date(rows[hi].t) - new Date(rows[lo].t)) / 864e5 : 0;
      const opts = days > 8 * 365 ? { year: "numeric" } : days > 240 ? { month: "short", year: "2-digit" } : { month: "short", day: "numeric" };
      return { ...base, callback: (v) => dayText(rows[v].t, opts) };
    }

    function tipTitle(r, unit) {
      if (unit === "bar") return new Date(r.t).toLocaleString("en-CA", { dateStyle: "medium", timeStyle: "short" });
      if (unit === "week") return `Week of ${dayText(r.from)}`;
      if (unit === "month") return dayText(r.t, { month: "long", year: "numeric" });
      return dayText(r.t);
    }

    function rangeText() {
      return { custom: "over these dates", YTD: "this year", Max: "all time", "1D": "today" }[state.range] || `past ${state.range}`;
    }

    function draw() {
      const d = state.main;
      if (!d) return;
      const { rows, ind, unit, startClose } = series(d);
      $("r-title").textContent = d.name && d.name !== d.symbol ? `${d.name} (${d.symbol})` : d.symbol;
      document.title = `${d.symbol} · Research · Portfolio Manager`;

      const q = d.quote;
      let head = "";
      if (q && q.price != null) {
        const change = q.prev_close ? q.price - q.prev_close : null;
        head = `${esc(money(q.price, q.currency))} <span class="${tone(change)}">${change == null ? ""
          : `${esc(money(change, q.currency, { signed: true }))} (${pct(change / q.prev_close)}) today`}</span>`;
      }
      if (rows.length && startClose && state.range !== "1D") {  // from the last close before the first day shown
        const change = rows[rows.length - 1].c / startClose - 1;
        head += ` <span class="nowrap"><span class="muted">·</span> <span class="${tone(change)}">${pct(change)}</span> <span class="muted">${esc(rangeText())}</span></span>`;
      }
      $("r-price").innerHTML = head || "No live quote for this symbol.";

      if (state.range !== "custom" && rows.length) {
        $("r-from").value = rows[0].from || rows[0].t.slice(0, 10);
        $("r-to").value = rows[rows.length - 1].t.slice(0, 10);
      }
      clearCharts();
      if (!rows.length) {
        status(unit === "bar" ? "No intraday prices for this symbol." : "No prices for these dates.");
        return;
      }
      status(null);
      // The bars in view: all of them, or the ones last zoomed to (by date, so a change of mode keeps them).
      win = { rows, unit, lo: 0, hi: rows.length - 1 };
      if (state.zoom) {
        const lo = rows.findIndex((r) => r.t >= state.zoom.from);
        let hi = rows.length - 1;
        while (hi > 0 && rows[hi].t > state.zoom.to) hi--;
        if (lo >= 0 && hi - lo >= minSpan()) Object.assign(win, { lo, hi }); else state.zoom = null;
      }
      const xView = { min: win.lo, max: win.hi };
      syncZoom();

      const style = cur().style;
      const candle = style === "candles";
      const up = cssVar("--gain"), down = cssVar("--loss");
      const shown = cur().ind;
      const compares = state.compares.filter((c) => c.data);
      const relative = compares.length > 0;  // comparing: every line is its change since the first bar
      const tradesButton = $("r-trades");
      tradesButton.disabled = !d.trades.length && !compares.some((c) => c.data.trades?.length);
      tradesButton.title = tradesButton.disabled ? "You haven't bought or sold the symbols on this chart" : "Show your buys and sells";
      const hasVolume = rows.some((r) => r.v > 0);
      const volumeButton = $("r-volume");
      volumeButton.disabled = !hasVolume;
      volumeButton.title = hasVolume ? "Show trading volume below the chart" : `Yahoo Finance has no volume for ${d.symbol}`;
      const showVolume = hasVolume && cur().volume;
      const events = d.events || { dividends: [], earnings: [], splits: [] };
      const eventsButton = $("r-events");
      eventsButton.disabled = !events.dividends.length && !events.earnings.length && !events.splits.length;
      eventsButton.title = eventsButton.disabled ? `Yahoo Finance lists no dividends, earnings or splits for ${d.symbol}`
        : "Show dividends, earnings and splits";
      const ev = cur().events ? eventMarks(events, rows, unit) : eventMarks(null, rows, unit);
      const baseClose = rows[0].c;
      const f = relative ? (v) => (v == null ? null : v / baseClose - 1) : (v) => v;
      const price = priceFormat(d.currency);
      const fmt = relative ? (v) => pct(v) : price;
      const labels = rows.map((r) => r.t);
      const line = { pointRadius: 0, pointHoverRadius: 0, tension: 0.1 };
      const datasets = [];

      const base = f(startClose ?? rows[0].c);  // the baseline: the close before the first day shown
      if (candle) {
        // Candles are drawn by the plugin below; these invisible lines give the tooltip and y axis their values.
        const ghost = { ...line, borderColor: "transparent", backgroundColor: "transparent", borderWidth: 0 };
        datasets.push({ ...ghost, label: d.symbol, data: rows.map((r) => f(r.c)), candle: true, noLegend: true },
          { ...ghost, label: "High", data: rows.map((r) => f(r.h)), helper: true },
          { ...ghost, label: "Low", data: rows.map((r) => f(r.l)), helper: true });
      } else {
        const color = style === "line" ? cssVar("--chart-1") : up;  // the legend's colour for the coloured styles
        const ds = { ...line, label: d.symbol, data: rows.map((r) => f(r.c)), borderColor: color, backgroundColor: color,
          borderWidth: 2, pointHoverRadius: 3, order: 0 };
        if (style === "colored") {  // each step green when the close rose from the bar before, red when it fell
          ds.segment = { borderColor: (s) => (s.p1.parsed.y >= s.p0.parsed.y ? up : down) };
        } else if (style === "baseline") {  // green above the baseline, red below, shaded to it
          ds.segment = { borderColor: (s) => ((s.p0.parsed.y + s.p1.parsed.y) / 2 >= base ? up : down) };
          ds.fill = { target: { value: base }, above: translucent(up, 0.15), below: translucent(down, 0.15) };
          ds.baseline = true;
        }
        datasets.push(ds);
      }
      for (const [key, def] of Object.entries(RESEARCH_INDICATORS)) {
        if (def.pane || !shown.has(key)) continue;
        const color = cssVar(`--ind-${key}`);
        if (def.bands) {  // upper, middle and lower lines, shaded between the outer two; one legend entry
          const band = (j) => ind[key].map((b) => f(b?.[j] ?? null));
          const style = { ...line, borderColor: color, backgroundColor: color, borderWidth: 1.25, order: 1 };
          datasets.push({ ...style, label: def.label, tipLabel: `${def.label} upper`, data: band(0),
            fill: "+2", backgroundColor: translucent(color, 0.08) },
          { ...style, label: `${def.label} middle`, data: band(1), borderDash: [6, 4], noLegend: true },
          { ...style, label: `${def.label} lower`, data: band(2), noLegend: true });
          continue;
        }
        datasets.push({ ...line, label: def.label, data: ind[key].map(f), borderColor: color, backgroundColor: color,
          borderWidth: 1.5, borderDash: def.dashed ? [6, 4] : [], order: 1 });
      }
      // Buy and sell triangles on the main line and on each compared line. Those on
      // a compared line are outlined in its colour; only the first pair goes in the legend.
      const gain = cssVar("--gain"), loss = cssVar("--loss");
      let tradeCount = 0;
      const addMarks = (trades, closes, toY, { symbol, currency, outline } = {}) => {
        if (!cur().trades) return;
        const marks = tradeMarks(trades, rows, unit, closes);
        if (!marks.count) return;
        const legend = tradeCount === 0;
        tradeCount += marks.count;
        const mark = { showLine: false, pointStyle: "triangle", pointRadius: 7, pointHoverRadius: 8, order: -1,
          trades: marks.info, symbol, fmtPrice: priceFormat(currency), noLegend: !legend, borderWidth: outline ? 2 : 1 };
        datasets.push({ ...mark, label: "Buy", kind: "BUY", data: marks.buys.map(toY), backgroundColor: gain, borderColor: outline || gain },
          { ...mark, label: "Sell", kind: "SELL", data: marks.sells.map(toY), rotation: 180, backgroundColor: loss, borderColor: outline || loss });
      };
      addMarks(d.trades, rows.map((r) => r.c), f, { symbol: relative ? d.symbol : "", currency: d.currency });
      compares.forEach((c) => {
        const color = cssVar(c.color);
        const closes = compareCloses(c, rows, unit);
        const base = closes.find((v) => v != null);
        const toY = (v) => (v == null ? null : v / base - 1);
        datasets.push({ ...line, label: c.data.symbol, data: closes.map(toY), borderColor: color,
          backgroundColor: color, borderWidth: 1.75, pointHoverRadius: 3, order: 0 });
        addMarks(c.data.trades || [], closes, toY, { symbol: c.data.symbol, currency: c.data.currency, outline: color });
      });

      const candles = {
        id: "candles",
        beforeDatasetsDraw(c) {
          if (style === "baseline") {  // the dashed baseline, when it's in view
            const { ctx, chartArea: a, scales: { y } } = c;
            const py = y.getPixelForValue(base);
            if (py < a.top || py > a.bottom) return;
            ctx.save();
            ctx.strokeStyle = cssVar("--muted");
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(a.left, py);
            ctx.lineTo(a.right, py);
            ctx.stroke();
            ctx.restore();
          }
          if (!candle) return;
          const { ctx, chartArea: a, scales: { x, y } } = c;
          const width = Math.max(1, Math.min(16, ((a.right - a.left) / (x.max - x.min + 1)) * 0.7));
          ctx.save();
          ctx.beginPath();
          ctx.rect(a.left, a.top, a.right - a.left, a.bottom - a.top);
          ctx.clip();
          rows.forEach((r, i) => {
            const px = Math.round(x.getPixelForValue(i)) + 0.5;
            ctx.strokeStyle = ctx.fillStyle = r.c >= r.o ? up : down;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(px, y.getPixelForValue(f(r.h)));
            ctx.lineTo(px, y.getPixelForValue(f(r.l)));
            ctx.stroke();
            const top = y.getPixelForValue(f(Math.max(r.o, r.c))), bottom = y.getPixelForValue(f(Math.min(r.o, r.c)));
            ctx.fillRect(px - width / 2, top, width, Math.max(1, bottom - top));
          });
          ctx.restore();
        },
      };

      // Event badges stack up from the bottom of the price pane, which gets room for them below the prices.
      const BADGE = 8;  // the largest badge radius, in pixels
      const evText = cssVar("--ev-text"), evColor = { D: cssVar("--ev-div"), S: cssVar("--ev-split") };
      const badgeColor = (e) => evColor[e.kind]
        || (e.estimate == null ? cssVar("--muted") : e.actual > e.estimate ? up : down);  // E: beat the estimate or not
      const badgeRoom = ev.lanes ? ev.lanes * (2 * BADGE + 3) + 6 : 0;
      const roomForBadges = (s) => {
        s.lowest = s.min;  // no price labels in the room
        if (!badgeRoom) return;
        const h = s.chart.chartArea?.height || s.chart.height * 0.85 || 300;
        s.min -= ((s.max - s.min) || Math.abs(s.max) || 1) * badgeRoom / Math.max(h - badgeRoom, 60);
      };
      const eventsPlugin = {
        id: "events",
        afterDatasetsDraw(c) {
          if (!ev.count) return;
          const { ctx, chartArea: a, scales: { x } } = c;
          // Each row's badges shrink (and lose their letter) only when they're too close together to fit.
          const placed = Object.fromEntries(ev.kinds.map((k) => [k, []]));  // kind -> [[x pixel, event]]
          for (let i = Math.max(0, x.min); i <= Math.min(rows.length - 1, x.max); i++) {
            ev.badges[i].forEach((e) => placed[e.kind].push([x.getPixelForValue(i), e]));
          }
          ctx.save();
          ctx.beginPath();
          ctx.rect(a.left, a.top, a.right - a.left, a.bottom - a.top);
          ctx.clip();
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ev.kinds.forEach((kind, lane) => {
            const list = placed[kind];
            const gap = Math.min(Infinity, ...list.slice(1).map(([px], j) => px - list[j][0]));
            const r = Math.max(3, Math.min(BADGE, gap / 2 - 0.5));
            const py = a.bottom - 4 - BADGE - lane * (2 * BADGE + 3);
            ctx.font = `700 ${Math.round(r * 1.25)}px ${Chart.defaults.font.family}`;
            list.forEach(([px, e]) => {
              ctx.fillStyle = badgeColor(e);
              ctx.beginPath();
              ctx.arc(px, py, r, 0, Math.PI * 2);
              ctx.fill();
              if (r >= 6) {
                ctx.fillStyle = evText;
                ctx.fillText(kind, px, py + 0.5);
              }
            });
          });
          ctx.restore();
        },
      };
      let perShare;
      try {
        perShare = new Intl.NumberFormat("en-CA", { style: "currency", currency: d.currency, currencyDisplay: "narrowSymbol",
          minimumFractionDigits: 2, maximumFractionDigits: 4 }).format;
      } catch { perShare = (v) => v.toFixed(4); }
      const eps = new Intl.NumberFormat("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 3 }).format;
      const eventLine = (e) => {
        const when = unit === "day" ? "" : ` (${dayText(e.date, { month: "short", day: "numeric" })})`;
        if (e.kind === "S") return ` Split ${e.ratio}${when}`;
        if (e.kind === "D") {
          const adjusted = Math.abs(e.paid - e.amount) > 1e-9 ? `; ${perShare(e.amount)} adjusted for later splits` : "";
          return ` Dividend: ${perShare(e.paid)} per share, ex-date${when}${adjusted}`;
        }
        if (e.estimate == null) return ` Earnings: EPS ${eps(e.actual)}, no estimate${when}`;
        const verdict = e.actual > e.estimate ? "beat" : e.actual < e.estimate ? "missed" : "matched";
        return ` Earnings: EPS ${eps(e.actual)}, ${verdict} the ${eps(e.estimate)} estimate${when}`;
      };

      // Crosshair: a dashed line through every pane at the bar under the pointer, and one across the
      // pane under the pointer, with its value on the right.
      const paneFormat = { "r-chart": fmt, "volume-chart": (v) => compactNumber.format(v), "rsi-chart": (v) => v.toFixed(1) };
      const crossLine = cssVar("--muted"), labelFill = cssVar("--text"), labelText = cssVar("--surface");
      const crossLabel = (ctx, text, cx, cy, minLeft, maxRight) => {
        const w = ctx.measureText(text).width + 12, h = 18;
        const left = Math.max(minLeft, Math.min(maxRight - w, cx - w / 2));
        ctx.fillStyle = labelFill;
        ctx.beginPath();
        ctx.roundRect(left, cy - h / 2, w, h, 4);
        ctx.fill();
        ctx.fillStyle = labelText;
        ctx.fillText(text, left + w / 2, cy + 0.5);
      };
      const crosshair = {
        id: "crosshair",
        // Chart.js's own pointer position, so the crosshair lands on the bar the tooltip shows.
        afterEvent(c, { event: e }) {
          if (e.type === "mouseout") {
            if (hover?.canvas === c.canvas) hover = null;
          } else if (e.type === "mousemove" && win) {
            const i = Math.max(0, Math.min(lastBar(), Math.round(c.scales.x.getValueForPixel(e.x))));
            hover = { canvas: c.canvas, i, y: e.y };
          } else {
            return;
          }
          drawCrosshair();
        },
        afterDraw(c) {
          if (!hover || hover.i > rows.length - 1) return;
          const { ctx, chartArea: a, scales: { x, y } } = c;
          const px = x.getPixelForValue(hover.i);
          if (px < a.left - 0.5 || px > a.right + 0.5) return;
          const here = hover.canvas === c.canvas && hover.y >= a.top && hover.y <= a.bottom;
          ctx.save();
          ctx.strokeStyle = crossLine;
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 3]);
          ctx.beginPath();
          ctx.moveTo(Math.round(px) + 0.5, a.top);
          ctx.lineTo(Math.round(px) + 0.5, a.bottom);
          if (here) {
            ctx.moveTo(a.left, Math.round(hover.y) + 0.5);
            ctx.lineTo(a.right, Math.round(hover.y) + 0.5);
          }
          ctx.stroke();
          ctx.font = `600 11px ${Chart.defaults.font.family}`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          if (here) {
            const format = paneFormat[c.canvas.id] || ((v) => v.toFixed(2));
            crossLabel(ctx, format(y.getValueForPixel(hover.y)), a.right + 2 + 36, hover.y, a.right + 2, c.width);
          }
          ctx.restore();
        },
      };

      const tooltip = {
        filter: (item) => item.parsed.y != null && !item.dataset.helper,
        callbacks: {
          title: (items) => tipTitle(rows[items[0].dataIndex], unit),
          afterBody: (items) => (items.length ? ev.marks[items[0].dataIndex].map(eventLine) : []),
          label: (c) => {
            const ds = c.dataset;
            if (ds.trades) {
              return ds.trades[c.dataIndex].filter((t) => t.kind === ds.kind)
                .map((t) => ` ${ds.symbol ? `${ds.symbol} ` : ""}${ds.label} ${qty(t.quantity)} @ ${ds.fmtPrice(t.price)} · ${t.account}`);
            }
            if (ds.candle && !relative) {
              const r = rows[c.dataIndex];
              return ` O ${price(r.o)}  H ${price(r.h)}  L ${price(r.l)}  C ${price(r.c)}`;
            }
            if (ds.baseline) {
              const change = relative ? (1 + c.parsed.y) / (1 + base) - 1 : c.parsed.y / base - 1;
              return ` ${ds.label}: ${fmt(c.parsed.y)} (${pct(change)} from the baseline)`;
            }
            return ` ${ds.tipLabel || ds.label}: ${fmt(c.parsed.y)}`;
          },
        },
      };

      chart = new Chart($("r-chart"), {
        type: "line",
        data: { labels, datasets },
        plugins: [candles, eventsPlugin, crosshair],
        options: {
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { ...xView, offset: candle, grid: { display: false }, ticks: xTicks(rows, unit, win.lo, win.hi) },
            y: { position: "right", grace: "3%", afterDataLimits: roomForBadges, afterFit: fixWidth,
              ticks: { callback(v) { return v < this.lowest ? null : fmt(v); } } },
          },
          plugins: {
            legend: {
              position: "top",
              align: "end",
              onClick: () => {},
              labels: {
                // Buy and Sell show as their ▲ / ▼ markers; every other entry as a short line in its style.
                usePointStyle: true,
                boxWidth: 12,
                boxHeight: 8,
                filter: (item, data) => !data.datasets[item.datasetIndex].helper && !data.datasets[item.datasetIndex].noLegend,
                generateLabels: (c) => Chart.defaults.plugins.legend.labels.generateLabels(c).map((item) => {
                  const ds = c.data.datasets[item.datasetIndex];
                  return ds.trades
                    ? { ...item, pointStyle: "triangle", rotation: ds.rotation || 0, fillStyle: ds.backgroundColor, strokeStyle: ds.borderColor }
                    : { ...item, pointStyle: "line", rotation: 0, strokeStyle: ds.borderColor, lineWidth: 2, lineDash: ds.borderDash || [] };
                }),
              },
            },
            tooltip,
          },
        },
      });

      // Volume pane: each bar is green when its candle closed at or above its open, red when below.
      $("volume-box").hidden = !showVolume;
      if (showVolume) {
        volumeChart = new Chart($("volume-chart"), {
          type: "bar",
          plugins: [crosshair],
          data: { labels, datasets: [{ label: "Volume", data: rows.map((r) => r.v), barPercentage: 0.9, categoryPercentage: 1,
            backgroundColor: rows.map((r) => translucent(r.c >= r.o ? up : down, 0.55)) }] },
          options: {
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
              // Invisible copies of the price chart's dates leave the same room at the edges, so the panes line up.
              x: { ...xView, offset: candle, grid: { display: false }, ticks: { ...xTicks(rows, unit, win.lo, win.hi), color: "transparent" } },
              y: { position: "right", beginAtZero: true, afterFit: fixWidth, title: { display: true, text: "Volume", font: { size: 11 } },
                ticks: { maxTicksLimit: 3, callback: (v) => compactNumber.format(v) } },
            },
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: { title: (items) => tipTitle(rows[items[0].dataIndex], unit),
                  label: (c) => ` Volume: ${c.parsed.y.toLocaleString("en-CA")}` },
              },
            },
          },
        });
      }

      $("rsi-box").hidden = !shown.has("rsi");
      if (shown.has("rsi")) {
        const level = (value, color) => ({ ...line, label: String(value), data: labels.map(() => value), borderColor: color,
          borderWidth: 1, borderDash: [4, 4], helper: true });
        const rsiColor = cssVar("--ind-rsi");
        rsiChart = new Chart($("rsi-chart"), {
          type: "line",
          plugins: [crosshair],
          data: { labels, datasets: [
            { ...line, label: "RSI (14)", data: ind.rsi, borderColor: rsiColor, backgroundColor: rsiColor, borderWidth: 1.5 },
            level(70, down), level(30, up),
          ] },
          options: {
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
              // Invisible copies of the price chart's dates leave the same room at the edges, so the panes line up.
              x: { ...xView, offset: candle, grid: { display: false }, ticks: { ...xTicks(rows, unit, win.lo, win.hi), color: "transparent" } },
              y: { position: "right", min: 0, max: 100, afterFit: fixWidth, title: { display: true, text: "RSI (14)", font: { size: 11 } },
                afterBuildTicks: (s) => { s.ticks = [30, 50, 70].map((value) => ({ value })); } },
            },
            plugins: {
              legend: { display: false },
              tooltip: {
                filter: (item) => item.parsed.y != null && !item.dataset.helper,
                callbacks: { title: (items) => tipTitle(rows[items[0].dataIndex], unit), label: (c) => ` RSI (14): ${c.parsed.y.toFixed(1)}` },
              },
            },
          },
        });
      }

      // MACD pane: the MACD and signal lines over the histogram of their difference.
      $("macd-box").hidden = !shown.has("macd");
      if (shown.has("macd")) {
        const part = (j) => ind.macd.map((m) => m?.[j] ?? null);
        const macdColor = cssVar("--ind-macd"), signalColor = cssVar("--ind-macd-signal");
        const hist = part(2);
        const num = new Intl.NumberFormat("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format;
        paneFormat["macd-chart"] = num;
        macdChart = new Chart($("macd-chart"), {
          type: "bar",
          plugins: [crosshair],
          data: { labels, datasets: [
            { type: "line", ...line, label: "MACD", data: part(0), borderColor: macdColor, backgroundColor: macdColor, borderWidth: 1.5, order: 0 },
            { type: "line", ...line, label: "Signal", data: part(1), borderColor: signalColor, backgroundColor: signalColor, borderWidth: 1.5, order: 0 },
            { label: "Histogram", data: hist, barPercentage: 0.9, categoryPercentage: 1, order: 1,
              backgroundColor: hist.map((v) => translucent(v >= 0 ? up : down, 0.55)) },
          ] },
          options: {
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
              x: { ...xView, offset: candle, grid: { display: false }, ticks: { ...xTicks(rows, unit, win.lo, win.hi), color: "transparent" } },
              y: { position: "right", afterFit: fixWidth, title: { display: true, text: "MACD (12, 26, 9)", font: { size: 11 } },
                grid: { color: (c) => (c.tick.value === 0 ? cssVar("--muted") : Chart.defaults.borderColor) },
                ticks: { maxTicksLimit: 5, callback: (v) => num(v) } },
            },
            plugins: {
              legend: { display: false },
              tooltip: {
                filter: (item) => item.parsed.y != null,
                callbacks: { title: (items) => tipTitle(rows[items[0].dataIndex], unit), label: (c) => ` ${c.dataset.label}: ${num(c.parsed.y)}` },
              },
            },
          },
        });
      }

      const notes = [`Prices in ${d.currency}.`];
      if (relative) notes.push("While comparing, each line shows its change since the first day shown.");
      if (showVolume && relative) notes.push(`Volume is ${d.symbol}'s.`);
      if (ev.count) {
        notes.push(`Badges along the bottom mark ${relative ? `${d.symbol}'s ` : ""}earnings (E: green beat the estimate, red didn't),`
          + " dividends on their ex-dates (D) and splits (S); hover one for details.");
      }
      if (tradeCount) {
        notes.push(relative ? "Triangles mark your buys (▲) and sells (▼) on each line; those on a compared line are outlined in its colour."
          : "Triangles mark your buys (▲) and sells (▼).");
      }
      if (unit === "bar") {
        notes.push(`Each ${candle ? "candle" : "point"} is a ${parseInt(d.interval, 10)}-minute bar. The SMAs, EMAs, Bollinger Bands, RSI and MACD count bars; the 200-day and 200-week averages use daily closes.`);
      } else if (unit !== "day") {
        notes.push(`Each ${candle ? "candle" : "point"} is one ${unit}; the averages are as of its last day${showVolume ? " and the volume is its total" : ""}.`);
      }
      if (candle) notes.push("Green candles closed higher than they opened, red ones lower.");
      if (style === "colored") notes.push(`The ${d.symbol} line is green where it rose from the bar before and red where it fell.`);
      if (style === "baseline") {
        notes.push(`The ${d.symbol} line is green above the dashed baseline, its close before the first day shown, and red below it.`);
      }
      if (shown.has("bb")) notes.push("Bollinger Bands (20, 2) are the 20-bar SMA (dashed) with bands two standard deviations above and below it.");
      if (shown.has("macd")) {
        notes.push("MACD (12, 26, 9) is the 12-bar EMA minus the 26-bar EMA; the signal line is its 9-bar EMA, and the bars are MACD minus the signal.");
      }
      notes.push("Scroll or pinch over the chart to zoom, and drag to move along it.");
      setNotes($("r-note"), notes);
    }

    // ---- zoom
    // The panes show bars win.lo to win.hi of win.rows. They're fractional while zooming and
    // panning, so small wheel and trackpad steps add up, and rounded when drawn.
    const MIN_BARS = 10;
    let win = null, frame = 0;
    let hover = null, hoverFrame = 0;  // the crosshair: {canvas, i: the bar, y: the pointer's height on that canvas}
    const drawCrosshair = () => {
      hoverFrame ||= requestAnimationFrame(() => {
        hoverFrame = 0;
        [chart, volumeChart, rsiChart, macdChart].forEach((c) => c?.draw());
      });
    };
    const lastBar = () => win.rows.length - 1;
    const minSpan = () => Math.min(MIN_BARS - 1, lastBar());
    function syncZoom() {
      const span = win ? Math.round(win.hi) - Math.round(win.lo) : 0;
      const zoomed = !!win && span < lastBar();
      $("zoom-in").disabled = !win || span <= minSpan();
      $("zoom-out").disabled = $("zoom-reset").disabled = !zoomed;
      root.querySelector(".research").classList.toggle("zoomed", zoomed);
    }
    function applyView() {
      frame = 0;
      if (!win) return;
      const lo = Math.round(win.lo), hi = Math.round(win.hi);
      [chart, volumeChart, rsiChart, macdChart].forEach((c) => {
        if (!c) return;
        const ticks = xTicks(win.rows, win.unit, lo, hi);
        Object.assign(c.options.scales.x, { min: lo, max: hi, ticks: c === chart ? ticks : { ...ticks, color: "transparent" } });
        c.update("none");
      });
    }
    function setView(lo, span) {
      lo = Math.min(Math.max(lo, 0), lastBar() - span);
      Object.assign(win, { lo, hi: lo + span });
      const a = Math.round(win.lo), b = Math.round(win.hi);
      state.zoom = b - a < lastBar() ? { from: win.rows[a].t, to: win.rows[b].t } : null;
      syncZoom();
      frame ||= requestAnimationFrame(applyView);
    }
    function zoomBy(factor, at) {  // factor < 1 zooms in, keeping bar `at` where it is
      if (!win || lastBar() < 1) return;
      const span = win.hi - win.lo;
      const next = Math.min(lastBar(), Math.max(minSpan(), span * factor));
      setView(at - (at - win.lo) * (next / span), next);
    }
    function panBy(bars) {
      if (win) setView(win.lo + bars, win.hi - win.lo);
    }
    // The bar (fractional) at a screen x on a pane, and how many bars one pixel spans.
    function barAt(canvas, clientX) {
      const x = Chart.getChart(canvas)?.scales.x;
      if (!x) return null;
      const p0 = x.getPixelForValue(x.min), p1 = x.getPixelForValue(x.max);
      const perPx = (x.max - x.min) / (p1 - p0 || 1);
      return { at: x.min + (clientX - canvas.getBoundingClientRect().left - p0) * perPx, perPx };
    }

    const zoomStep = (factor) => {  // the buttons zoom around the middle, or keep the latest bar in view
      if (win) zoomBy(factor, Math.round(win.hi) >= lastBar() ? lastBar() : (win.lo + win.hi) / 2);
    };
    $("zoom-in").addEventListener("click", () => zoomStep(0.6));
    $("zoom-out").addEventListener("click", () => zoomStep(1 / 0.6));
    $("zoom-reset").addEventListener("click", () => win && setView(0, lastBar()));

    ["r-chart", "volume-chart", "rsi-chart", "macd-chart"].forEach((id) => {
      const canvas = $(id);
      // Wheel: up zooms in and down zooms out, around the pointer; sideways (or Shift+wheel) pans.
      // Trackpad pinches arrive as wheel events with ctrlKey set, in smaller steps.
      canvas.addEventListener("wheel", (e) => {
        const p = win && barAt(canvas, e.clientX);
        if (!p) return;
        const px = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;  // lines or pages to pixels
        const dx = (e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX) * px, dy = e.shiftKey ? 0 : e.deltaY * px;
        if (Math.abs(dx) > Math.abs(dy)) {
          e.preventDefault();
          panBy(dx * p.perPx);
        } else if (dy < 0 || Math.round(win.hi) - Math.round(win.lo) < lastBar()) {  // fully zoomed out, the page scrolls
          e.preventDefault();
          zoomBy(Math.exp(dy * (e.ctrlKey ? 0.01 : 0.002)), p.at);
        }
      }, { passive: false });

      // Drag with one pointer to pan; pinch with two fingers to zoom.
      const pointers = new Map();  // pointerId -> clientX
      canvas.addEventListener("pointerdown", (e) => {
        if (!win || (e.pointerType === "mouse" && e.button !== 0)) return;
        pointers.set(e.pointerId, e.clientX);
        canvas.setPointerCapture(e.pointerId);
      });
      canvas.addEventListener("pointermove", (e) => {
        if (!pointers.has(e.pointerId) || !win) return;
        const before = [...pointers.values()];
        pointers.set(e.pointerId, e.clientX);
        const after = [...pointers.values()];
        if (after.length === 1) {
          const p = barAt(canvas, e.clientX);
          if (!p || after[0] === before[0]) return;
          root.querySelector(".research").classList.add("dragging");
          panBy((before[0] - after[0]) * p.perPx);
        } else if (after.length === 2) {
          const p = barAt(canvas, (after[0] + after[1]) / 2);
          const gap = (xs) => Math.max(20, Math.abs(xs[0] - xs[1]));
          if (p) zoomBy(gap(before) / gap(after), p.at);
        }
      });
      const release = (e) => {
        pointers.delete(e.pointerId);
        if (!pointers.size) root.querySelector(".research").classList.remove("dragging");
      };
      canvas.addEventListener("pointerup", release);
      canvas.addEventListener("pointercancel", release);
      canvas.addEventListener("dblclick", () => win && setView(0, lastBar()));
    });

    // ---- controls
    const pressed = (groupId, attr, value) => {
      $(groupId).querySelectorAll(`button[data-${attr}]`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset[attr] === value)));
    };
    const styleMenu = $("style-menu");
    const syncMenu = () => {
      const s = cur();
      menu.querySelectorAll("[data-ind]").forEach((cb) => { cb.checked = s.ind.has(cb.dataset.ind); });
      styleMenu.querySelectorAll("input[name=r-style]").forEach((r) => { r.checked = r.value === s.style; });
      $("style-current").innerHTML = styleMenu.querySelector(`[data-style="${s.style}"]`).innerHTML;
      $("r-trades").setAttribute("aria-pressed", String(s.trades));
      $("r-volume").setAttribute("aria-pressed", String(s.volume));
      $("r-events").setAttribute("aria-pressed", String(s.events));
      $("r-note").hidden = !s.notes;
      $("r-notes").setAttribute("aria-expanded", String(s.notes));
    };
    // Reset is there while the mode's settings or the time shown differ from its defaults.
    const syncReset = () => {
      const s = cur(), d = modeSettings(state.mode);
      const same = state.range === d.range && ["style", "trades", "volume", "events", "notes"].every((k) => s[k] === d[k])
        && s.ind.size === d.ind.size && [...d.ind].every((k) => s.ind.has(k));
      $("r-reset").disabled = same;
      $("r-reset").title = `Put ${state.mode === "ta" ? "TA" : "PA"} mode back to its default settings`;
    };
    // Show the current mode's settings, and its time range if another one is showing.
    const showMode = () => {
      const newRange = state.range !== cur().range;
      if (newRange) Object.assign(state, { range: cur().range, from: "", to: "", zoom: null });
      pressed("r-mode", "mode", state.mode);
      pressed("r-range", "range", state.range);
      syncMenu();
      save();
      if (newRange) refresh(); else draw();
    };

    for (const key of ["trades", "volume", "events"]) {
      $(`r-${key}`).addEventListener("click", () => {
        cur()[key] = !cur()[key];
        syncMenu();
        save();
        draw();
      });
    }

    $("r-notes").addEventListener("click", () => {
      cur().notes = !cur().notes;
      syncMenu();
      save();
    });

    $("r-mode").addEventListener("click", (e) => {
      const b = e.target.closest("[data-mode]");
      if (!b || b.dataset.mode === state.mode) return;
      state.mode = b.dataset.mode;
      showMode();
    });

    $("r-reset").addEventListener("click", () => {
      state.modes[state.mode] = modeSettings(state.mode);
      state.zoom = null;
      state.range = "";  // so showMode puts the default range back
      showMode();
    });

    styleMenu.addEventListener("change", (e) => {
      if (e.target.name !== "r-style") return;
      cur().style = e.target.value;
      styleMenu.open = false;
      syncMenu();
      save();
      draw();
    });

    menu.addEventListener("change", (e) => {
      const key = e.target.dataset.ind;
      if (!key) return;
      if (e.target.checked) cur().ind.add(key); else cur().ind.delete(key);
      save();
      draw();
    });

    $("r-range").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      state.range = cur().range = b.dataset.range;
      state.from = state.to = "";
      state.zoom = null;
      pressed("r-range", "range", state.range);
      save();
      refresh();
    });

    const today = isoDate(new Date());
    $("r-from").max = $("r-to").max = today;
    ["r-from", "r-to"].forEach((id) => $(id).addEventListener("change", () => {
      let from = $("r-from").value, to = $("r-to").value;
      if (!from) return;
      if (to && to < from) [from, to] = [to, from];
      $("r-from").value = from;
      $("r-to").value = to;
      Object.assign(state, { range: "custom", from, to, zoom: null });
      pressed("r-range", "range", null);
      syncReset();
      refresh();
    }));

    $("symbol-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const symbol = $("symbol-input").value.trim().toUpperCase();
      if (!symbol || symbol === state.symbol) return;
      state.symbol = symbol;
      state.zoom = null;
      $("symbol-input").value = symbol;
      state.compares = state.compares.filter((c) => c.symbol !== symbol);
      renderChips();
      const url = new URL(location.href);
      url.searchParams.set("symbol", symbol);
      history.replaceState(null, "", url.href);
      refresh();
      loadStats();
      loadCompany();
    });

    // + Compare
    const compareForm = $("compare-form");
    const showCompare = (open) => {
      compareForm.hidden = !open;
      $("compare-add").setAttribute("aria-expanded", String(open));
      $("compare-msg").textContent = "";
      if (open) $("compare-input").focus();
    };
    $("compare-add").addEventListener("click", () => showCompare(compareForm.hidden));
    $("compare-cancel").addEventListener("click", () => showCompare(false));
    compareForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const symbol = $("compare-input").value.trim().toUpperCase();
      const msg = $("compare-msg");
      if (!symbol) return;
      if (symbol === state.symbol || state.compares.some((c) => c.symbol === symbol)) { msg.textContent = `${symbol} is already on the chart.`; return; }
      if (state.compares.length >= MAX_COMPARES) { msg.textContent = `You can compare up to ${MAX_COMPARES} symbols.`; return; }
      msg.textContent = "Loading…";
      try {
        const data = await load(symbol);
        const color = COMPARE_COLORS.find((c) => !state.compares.some((x) => x.color === c));
        state.compares.push({ symbol, color, data });
        $("compare-input").value = "";
        showCompare(false);
        renderChips();
        draw();
      } catch (err) {
        msg.textContent = err.message;
      }
    });
    function renderChips() {
      $("compare-chips").innerHTML = state.compares.map((c) => `<button type="button" class="pick" aria-pressed="true"
          data-remove="${esc(c.symbol)}" style="--pick: var(${c.color})" title="Stop comparing ${esc(c.symbol)}">
          <span class="dot"></span>${esc(c.data?.symbol || c.symbol)}<span class="x" aria-hidden="true">×</span></button>`).join("");
    }
    $("compare-chips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-remove]");
      if (!b) return;
      state.compares = state.compares.filter((c) => c.symbol !== b.dataset.remove);
      renderChips();
      draw();
    });

    // ---- key statistics, below the chart
    const bigMoney = (v, currency) => {  // $5.01T
      try {
        return new Intl.NumberFormat("en-CA", { style: "currency", currency, currencyDisplay: "narrowSymbol",
          notation: "compact", maximumFractionDigits: 2 }).format(v) + (currency !== BASE ? ` ${currency}` : "");
      } catch {
        return `${v.toExponential(2)} ${currency}`;
      }
    };
    // Earnings dates are shown on New York's calendar, as on the Dashboard.
    const nyDay = (ts, opts) => new Date(ts * 1000).toLocaleDateString("en-CA", { ...opts, timeZone: "America/New_York" });
    const daysUntil = (ts) => {
      const day = (ms) => Date.parse(`${new Date(ms).toLocaleDateString("en-CA", { timeZone: "America/New_York" })}T00:00:00Z`);
      return Math.round((day(ts * 1000) - day(Date.now())) / 864e5);
    };
    function earningsTile(e) {
      if (!e) return ["—", "No date announced"];
      const full = { month: "short", day: "numeric", year: "numeric" };
      const days = daysUntil(e.start);
      const when = days === 0 ? "Today" : days === 1 ? "Tomorrow" : days > 0 ? `In ${days} days` : days === -1 ? "Yesterday" : `${-days} days ago`;
      if (e.end) return [`${nyDay(e.start, { month: "short", day: "numeric" })} – ${nyDay(e.end, full)}`, `${when} · estimated window`];
      return [nyDay(e.start, full), `${nyDay(e.start, { weekday: "long" })}, ${when.toLowerCase()}${e.estimated ? " · estimated" : ""}`];
    }
    function renderStats(st) {
      const cur = st.currency;
      const num = (v) => (v == null ? "—" : v.toFixed(2));
      const upside = st.target != null && st.price ? st.target / st.price - 1 : null;
      const dividend = st.dividend != null
        ? `${money(st.dividend, cur)}${st.yield != null ? ` (${pct(st.yield, { signed: false })})` : ""}`
        : st.yield != null ? pct(st.yield, { signed: false }) : "—";
      const tiles = [  // [label, value, sub-line HTML, title]
        ["Market cap", st.market_cap ? bigMoney(st.market_cap, cur) : "—", "",
          st.market_cap ? money(st.market_cap, cur, { compact: true }) : ""],
        ["Beta (5Y monthly)", num(st.beta), ""],
        ["P/E ratio (TTM)", num(st.pe), ""],
        ["Earnings date", ...earningsTile(st.earnings).map((t, i) => (i ? esc(t) : t))],
        ["FWD Div & Yield", dividend, st.dividend == null && st.yield != null ? "Yield over the past 12 months"
          : st.dividend == null ? "No dividend" : ""],
        ["1Y target estimate", st.target == null ? "—" : money(st.target, cur), upside == null ? ""
          : `<span class="${tone(upside)}">${pct(upside)}</span> from ${esc(money(st.price, cur))}`],
      ];
      $("r-stats").innerHTML = tiles.map(([label, value, sub, title]) => `<div class="kpi"><div class="kpi-label">${esc(label)}</div>
        <div class="kpi-value" title="${esc(title || value)}">${esc(value)}</div><div class="kpi-sub">${sub}</div></div>`).join("");
    }
    // Company overview: name, sector and industry (a fund's family and category instead), with the
    // description and links behind "Show more".
    function renderOverview(p, symbol) {
      const fund = !p.sector && !p.industry && (p.family || p.category);
      const facts = [["Name", p.name || symbol], fund ? ["Fund family", p.family] : ["Sector", p.sector],
        fund ? ["Category", p.category] : ["Industry", p.industry]];
      $("r-overview").innerHTML = facts.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value || "—")}</dd></div>`).join("");
      $("r-summary").textContent = p.summary || "Yahoo Finance has no description for this symbol.";
      const link = (url, text) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)} ↗</a>`;
      const host = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return "Website"; } };
      const links = [p.website && link(p.website, host(p.website)),
        p.ir_website && link(p.ir_website, "Investor relations")].filter(Boolean);
      $("r-links").innerHTML = links.length ? links.join('<span class="muted" aria-hidden="true">·</span>')
        : '<span class="muted">No website listed.</span>';
    }
    $("r-overview-toggle").addEventListener("click", (e) => {
      const b = e.currentTarget, open = b.getAttribute("aria-expanded") !== "true";
      b.setAttribute("aria-expanded", String(open));
      b.lastElementChild.textContent = open ? "Show less" : "Show more";
      $("r-overview-more").hidden = !open;
    });

    let statsSeq = 0;
    async function loadStats() {
      const mine = ++statsSeq;
      $("r-stats").innerHTML = '<div class="kpi skeleton"></div>'.repeat(6);
      $("r-stats-note").textContent = "";
      $("r-overview").querySelectorAll("dd").forEach((dd) => { dd.textContent = "…"; });
      try {
        const st = await getJSON(root.dataset.statsApi.replace("SYMBOL", encodeURIComponent(state.symbol)));
        if (mine !== statsSeq) return;
        renderStats(st);
        renderOverview(st.profile || {}, st.symbol);
      } catch (err) {
        if (mine !== statsSeq) return;
        $("r-stats").innerHTML = "";
        $("r-stats-note").textContent = `Couldn't load the key statistics for ${state.symbol}: ${err.message}`;
        renderOverview({}, state.symbol);
      }
    }

    // ---- Company: financials, ownership and capital returns, below the key statistics
    const COMPANY_STORAGE = "research.company";
    const co = { data: null, tab: "financials", view: "income", freq: "annual", list: "institutions",
      sc: { mode: "count", basis: "diluted", freq: "annual", eps: false }, notes: false };
    try {
      const s = JSON.parse(localStorage.getItem(COMPANY_STORAGE) || "{}");
      const pick = (v, options, fallback) => (options.includes(v) ? v : fallback);
      co.tab = pick(s.tab, ["financials", "ownership", "capital"], co.tab);
      co.view = pick(s.view, ["income", "balance", "cash", "margins", "valuation"], co.view);
      co.freq = pick(s.freq, ["annual", "quarterly"], co.freq);
      co.list = pick(s.list, ["institutions", "funds"], co.list);
      co.sc = { mode: pick(s.sc?.mode, ["count", "change"], "count"), basis: pick(s.sc?.basis, ["basic", "diluted"], "diluted"),
        freq: pick(s.sc?.freq, ["annual", "quarterly"], "annual"), eps: s.sc?.eps === true };
      co.notes = s.notes === true;
    } catch { /* storage blocked */ }
    const saveCompany = () => {
      try {
        localStorage.setItem(COMPANY_STORAGE, JSON.stringify({ tab: co.tab, view: co.view, freq: co.freq, list: co.list, sc: co.sc, notes: co.notes }));
      } catch { /* storage blocked */ }
    };

    const compactFmt = new Intl.NumberFormat("en-CA", { notation: "compact", minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const compact = (v) => (v == null ? "—" : (v < 0 ? "−" : "") + compactFmt.format(Math.abs(v)));
    const axisFmt = new Intl.NumberFormat("en-CA", { notation: "compact", maximumFractionDigits: 3 });  // 1.405B, not 1.41B twice
    const axisCompact = (v) => (v < 0 ? "−" : "") + axisFmt.format(Math.abs(v));
    const signedFixed = (v, digits = 2) => (v < 0 ? "−" : "") + Math.abs(v).toFixed(digits);
    const times = (v) => (v == null ? "—" : `${signedFixed(v, v >= 100 ? 0 : 1)}×`);
    const share = (v) => (v == null ? "—" : v > 0 && v < 1e-5 ? "<0.001%" : v < 0.001 ? `${(v * 100).toFixed(3)}%` : pct(v, { signed: false }));
    function cellText(v, fmt) {
      if (v == null) return "—";
      if (fmt === "eps") return signedFixed(v);
      if (fmt === "pct") return pct(v, { signed: false });
      if (fmt === "x") return times(v);
      return compact(v);  // money, market caps and share counts
    }
    const monthYear = (iso) => dayText(iso, { month: "short", year: "numeric" });
    const periodName = (c) => (c.kind === "annual" ? `FY${c.date.slice(0, 4)}` : c.kind === "quarterly" ? monthYear(c.date)
      : c.kind === "ttm" ? "TTM" : "Current");
    function colHead(c, marked = false) {
      const sub = c.kind === "annual" ? dayText(c.date) : c.kind === "quarterly" ? "Quarter"
        : c.kind === "ttm" ? `to ${monthYear(c.date)}` : "Today";
      return `<th class="num" scope="col">${esc(periodName(c))}<small>${esc(sub)}${marked ? " · Yahoo" : ""}</small></th>`;
    }

    const coCharts = {};
    const coChart = (id, config) => {
      coCharts[id]?.destroy();
      coCharts[id] = config ? new Chart($(id), config) : undefined;
    };
    const dropCharts = () => Object.keys(coCharts).forEach((id) => coChart(id, null));
    // Line and column styling shared by the Company charts: 2px lines, 8px points ringed in the surface
    // colour, columns at most 24px wide with rounded ends.
    const lineSet = (label, data, color, extra = {}) => ({ label, data, borderColor: color, backgroundColor: color, borderWidth: 2,
      pointRadius: 4, pointHoverRadius: 6, pointBorderColor: cssVar("--surface"), pointBorderWidth: 2, spanGaps: true, ...extra });
    const barSet = (label, data, color) => ({ label, data, backgroundColor: color, maxBarThickness: 24, borderRadius: 4, borderSkipped: "start" });
    const zeroGrid = { color: (c) => (c.tick.value === 0 ? cssVar("--muted") : Chart.defaults.borderColor) };
    function coOptions({ y, title, legend = false, tooltip = {} }) {
      return {
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        scales: { x: { grid: { display: false } }, y: { grace: "8%", ...y, afterFit: (s) => { s.width = 72; } } },
        plugins: {
          title: title ? { display: true, text: title, align: "start", color: cssVar("--text"), font: { weight: 600, size: 13 }, padding: { bottom: 10 } } : { display: false },
          legend: legend ? { position: "bottom", labels: { boxWidth: 12 } } : { display: false },
          tooltip: { callbacks: tooltip },
        },
      };
    }

    // Financials: one statement, the margins or the valuation multiples at a time, annual or quarterly.
    const VIEW_NAMES = { income: "Income statement", balance: "Balance sheet", cash: "Cash flow", margins: "Margins", valuation: "Valuation" };
    // Only as many periods as fit are shown, beside the always-visible line item names; a slider
    // moves along the rest. Each statement and period type keeps its place, starting at the latest.
    const finBack = {};  // "view|freq" -> periods hidden after the last one shown
    function fitPeriods() {
      const width = $("fin-table").parentElement.clientWidth || 900;
      const narrow = width < 560;
      return Math.max(2, Math.floor((width - (narrow ? 140 : 210)) / (narrow ? 82 : 90)));
    }
    function renderFinancials() {
      const d = co.data, fin = d.financials;
      const tc = d.currency, fc = d.financial_currency || tc;
      $("fin-slider").hidden = true;
      $("fin-source").innerHTML = "";
      if (!fin) {
        $("fin-table").innerHTML = "";
        setNotes($("fin-note"), [`Yahoo Finance has no financial statements for ${d.symbol}.`, "Funds, indexes and crypto don't file them."]);
        return;
      }
      const t = fin[co.freq][co.view];
      const statement = ["income", "balance", "cash"].includes(co.view);
      const cur = t.currency || fc;
      // Where the figures come from, in plain sight: SEC filings, or Yahoo Finance and why.
      const periods = t.columns.filter((c) => c.kind === co.freq).length;
      const span = `${periods} ${co.freq === "annual" ? (periods === 1 ? "year" : "years") : (periods === 1 ? "quarter" : "quarters")}`;
      $("fin-source").innerHTML = !statement ? "<strong>Source:</strong> Yahoo Finance"
        : t.source === "sec"
          ? `<strong>Source:</strong> SEC filings (EDGAR) · ${span}${t.columns.some((c) => c.source === "yahoo") ? " · the newest from Yahoo Finance" : ""}`
          : `<strong>Source:</strong> Yahoo Finance · ${span}. ${esc(whyYahoo(d))}`;
      if (!t.rows.length) {
        $("fin-table").innerHTML = `<tbody><tr><td class="muted">Yahoo Finance has no ${esc(VIEW_NAMES[co.view].toLowerCase())} figures for ${esc(d.symbol)} by ${co.freq === "annual" ? "year" : "quarter"}.</td></tr></tbody>`;
      } else {
        // Start from an estimate of how many periods fit, then drop one at a time while the table is
        // still wider than its box (fonts differ between computers), so no period is ever cut off.
        const n = t.columns.length, key = `${co.view}|${co.freq}`, wrap = $("fin-table").parentElement;
        const draw = (fit) => {
          const back = Math.min(Math.max(finBack[key] || 0, 0), n - fit);
          const lo = n - fit - back, hi = lo + fit;
          $("fin-slider").hidden = fit >= n;
          if (fit < n) {
            const range = $("fin-range");
            range.max = String(n - fit);
            range.value = String(lo);
            range.dataset.key = key;
            range.setAttribute("aria-valuetext", `${periodName(t.columns[lo])} to ${periodName(t.columns[hi - 1])}`);
            $("fin-range-text").innerHTML = `Showing <strong>${esc(periodName(t.columns[lo]))} – ${esc(periodName(t.columns[hi - 1]))}</strong> of ${n} ${co.freq === "annual" ? "periods" : "quarters"}`;
            $("fin-prev").disabled = lo === 0;
            $("fin-next").disabled = hi === n;
          }
          $("fin-table").innerHTML = `<thead><tr><th scope="col">${statement ? `In ${esc(cur)}` : esc(VIEW_NAMES[co.view])}</th>${t.columns.slice(lo, hi).map((c) => colHead(c, t.source === "sec" && c.source === "yahoo")).join("")}</tr></thead>
            <tbody>${t.rows.map((r) => `<tr class="${r.style}"><th scope="row">${esc(r.label)}</th>${r.values.slice(lo, hi).map((v) =>
              `<td class="num${v == null ? " empty" : ""}">${esc(cellText(v, r.fmt))}</td>`).join("")}</tr>`).join("")}</tbody>`;
        };
        let fit = Math.min(n, fitPeriods());
        draw(fit);
        while (fit > 1 && wrap.clientWidth && $("fin-table").scrollWidth > wrap.clientWidth + 1) draw(--fit);
      }
      const filed = statement && t.source === "sec";
      const fromYahoo = filed && t.columns.some((c) => c.source === "yahoo")
        ? ["Periods marked Yahoo are newer than the company's latest SEC filing and come from Yahoo Finance.",
          "Lines on which the two sources disagree are left blank there."] : [];
      setNotes($("fin-note"), filed ? [
        `As filed with the US SEC in the company's reports (10-K, 10-Q, 20-F or 40-F), in ${cur}; restated figures replace the originals.`,
        ...{
          income: ["Fourth quarters are the year less the first three, so their per-share figures and share counts are left blank.",
            "EBIT is pretax income plus interest expense.", "EPS and share counts are adjusted for later splits.", "TTM is the last four quarters."],
          balance: ["Balances at each period's end.", "Total debt is current plus long-term debt; leases aren't included.",
            "Shares outstanding are adjusted for later splits."],
          cash: ["Fourth quarters are the year less the first three; reports give the rest of the year to date.",
            "Payments (capital expenditure, dividends, buybacks and debt repaid) are negative.",
            "Free cash flow is operating cash flow less capital expenditure.", "TTM is the last four quarters."],
        }[co.view],
        ...fromYahoo]
        : {
          income: [`From Yahoo Finance, in ${cur}, as standardized from the company's filings.`,
            "Fiscal years are named for the year they end in.", "TTM is the twelve months to the latest quarter."],
          balance: [`From Yahoo Finance, in ${cur}: balances at each period's end.`],
          cash: [`From Yahoo Finance, in ${cur}, as standardized from the company's filings.`, "Free cash flow is operating cash flow less capital expenditure."],
          margins: ["Each figure as a share of total revenue for the same period.", "FCF is free cash flow: operating cash flow less capital expenditure."],
          valuation: [`Market cap and enterprise value in ${tc}.`,
            `Past periods use the share price at the period's end and the twelve months to it${co.freq === "quarterly" ? " (four quarters added up, so the earliest quarters have none)" : ""}.`,
            "Current uses today's price and the TTM.",
            "Enterprise value is market cap plus debt and minority interest, less cash and short-term investments.",
            "A dash means a figure is missing or negative.",
            fc !== tc && `Figures reported in ${fc} are converted to ${tc} at the exchange rate of each period's end.`],
        }[co.view]);
    }
    // Why a statement shows Yahoo's few years rather than ten from SEC filings.
    function whyYahoo(d) {
      const f = d.filings || {};
      if (f.status === "off") return "Ten years come from SEC filings, which are switched off on this server: set SEC_CONTACT in its .env file and reload the app.";
      if (f.status === "not_filed") return `${d.symbol} doesn't file reports with the US SEC, so these are all the years available.`;
      if (f.status === "unavailable") return `The SEC's EDGAR service couldn't be reached for ten years of filings (${f.detail || "no answer"}); reload to try again.`;
      return f.status === "ok" ? "The company's SEC filings don't cover more periods than this." : "";
    }
    $("fin-range").addEventListener("input", (e) => {
      const range = e.currentTarget, t = co.data?.financials?.[co.freq]?.[co.view];
      if (!t) return;
      finBack[range.dataset.key] = Number(range.max) - Number(range.value);
      renderFinancials();
    });
    // The buttons move a whole screen of periods at a time.
    const finPage = (dir) => {
      const range = $("fin-range");
      const fit = co.data.financials[co.freq][co.view].columns.length - Number(range.max);
      const lo = Math.min(Math.max(Number(range.value) + dir * fit, 0), Number(range.max));
      finBack[range.dataset.key] = Number(range.max) - lo;
      renderFinancials();
    };
    $("fin-prev").addEventListener("click", () => finPage(-1));
    $("fin-next").addEventListener("click", () => finPage(1));
    let finResize = 0;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(finResize);
      finResize = requestAnimationFrame(() => { if (co.data && co.tab === "financials") renderFinancials(); });
    });

    // Ownership: by holder type, the largest institutions or funds, and insiders.
    function hbar(v, max) {
      const width = max > 0 && v > 0 ? Math.max(1, (100 * v) / max) : 0;
      return `<div class="hbar"><div class="hbar-track"><div class="hbar-fill" style="width: ${width.toFixed(1)}%"></div></div><strong>${share(v)}</strong></div>`;
    }
    function renderOwnership() {
      const d = co.data, o = d.ownership;
      const any = o && (o.insiders != null || o.institutions != null || o.top_institutions.length || o.top_funds.length || o.insider_holders.length);
      $("own-list").hidden = !any;
      if (!any) {
        $("own-types").innerHTML = `<p class="muted">Yahoo Finance has no ownership figures for ${esc(d.symbol)}.</p>`;
        ["own-holders", "own-insiders"].forEach((id) => { $(id).innerHTML = ""; });
        $("own-insider-lede").textContent = "";
        setNotes($("own-note"), []);
        return;
      }
      // Yahoo's institutional share can top 100% (lent shares count twice), so the bar is capped.
      const types = [["Institutions", o.institutions, "--co-1"], ["Insiders", o.insiders, "--co-2"], ["Public & other", o.public, "--chart-3"]]
        .filter(([, v]) => v != null);
      let room = 1;
      const widths = types.map(([, v]) => { const w = Math.max(0, Math.min(v, room)); room -= w; return w; });
      const label = types.map(([name, v]) => `${name} ${share(v)}`).join(", ");
      $("own-types").innerHTML = types.length ? `
        <div class="own-split" role="img" aria-label="${esc(label)}">${types.map(([name, , color], i) => (widths[i] > 0
          ? `<span style="--seg: var(${color}); flex: ${widths[i]} 1 0" title="${esc(`${name}: ${share(types[i][1])}`)}"></span>` : "")).join("")}</div>
        <ul class="own-legend">${types.map(([name, v, color]) => `<li><span class="swatch" style="--seg: var(${color})"></span>${esc(name)} <strong>${share(v)}</strong></li>`).join("")}</ul>
        ${o.institutions_count ? `<p class="co-lede muted">${o.institutions_count.toLocaleString("en-CA")} institutions hold shares${o.institutions_float != null ? `, ${share(o.institutions_float)} of the float` : ""}.</p>` : ""}`
        : '<p class="muted">Yahoo Finance has no breakdown by holder type.</p>';

      const funds = co.list === "funds", list = funds ? o.top_funds : o.top_institutions;
      const max = Math.max(0, ...list.map((h) => h.pct || 0));
      $("own-holders").innerHTML = list.length ? `<thead><tr><th scope="col">${funds ? "Fund" : "Institution"}</th><th scope="col">Share of stock</th>
          <th class="num" scope="col">Shares</th><th class="num" scope="col">Value</th>
          <th class="num" scope="col" title="Change in the position since the holder's previous report">Change</th><th class="num" scope="col">Reported</th></tr></thead>
        <tbody>${list.map((h) => `<tr><td class="who">${esc(h.name)}</td><td class="bar-cell">${hbar(h.pct, max)}</td>
          <td class="num">${compact(h.shares)}</td><td class="num nowrap">${h.value == null ? "—" : esc(bigMoney(h.value, d.currency))}</td>
          <td class="num ${tone(h.change)}">${pct(h.change)}</td><td class="num nowrap">${h.date ? esc(dayText(h.date)) : "—"}</td></tr>`).join("")}</tbody>`
        : `<tbody><tr><td class="muted">Yahoo Finance lists no ${funds ? "funds" : "institutions"} holding ${esc(d.symbol)}.</td></tr></tbody>`;

      const insiders = o.insider_holders;
      const imax = Math.max(0, ...insiders.map((h) => h.pct || 0));
      $("own-insider-lede").textContent = o.insiders != null
        ? `Insiders hold ${share(o.insiders)} of the shares.${insiders.length ? " The insiders Yahoo Finance lists, by shares held:" : ""}` : "";
      $("own-insiders").innerHTML = insiders.length ? `<thead><tr><th scope="col">Insider</th><th scope="col">Share of stock</th>
          <th class="num" scope="col">Shares</th><th class="num" scope="col" title="Held directly, and through trusts, family or companies">Direct / indirect</th>
          <th class="num" scope="col">As of</th></tr></thead>
        <tbody>${insiders.map((h) => `<tr><td class="who">${esc(h.name)}<span class="sym-name">${esc(h.relation || "")}</span></td>
          <td class="bar-cell">${hbar(h.pct, imax)}</td><td class="num">${compact(h.shares)}</td>
          <td class="num nowrap">${compact(h.direct)} / ${compact(h.indirect)}</td><td class="num nowrap">${h.date ? esc(dayText(h.date)) : "—"}</td></tr>`).join("")}</tbody>`
        : `<tbody><tr><td class="muted">Yahoo Finance lists no insider holdings for ${esc(d.symbol)}.</td></tr></tbody>`;
      setNotes($("own-note"), ["From Yahoo Finance: institutions and funds as of their latest 13F and fund reports, insiders as of their latest filing.",
        `Values are in ${d.currency}.`, "Share of stock is the share of the shares outstanding."]);
    }

    // Capital returns: share count, dividends, and returns on capital against the WACC.
    function renderShares() {
      const d = co.data, { mode, basis, freq, eps } = co.sc;
      const rows = (d.shares?.[freq] || []).filter((r) => r[basis] != null);
      $("sc-eps-box").hidden = !eps || !rows.length;
      if (!rows.length) {
        coChart("sc-chart", null);
        coChart("sc-eps-chart", null);
        $("sc-lede").textContent = `Yahoo Finance has no share counts for ${d.symbol}.`;
        $("sc-source").innerHTML = "";
        return;
      }
      // Filings give a fourth quarter's share count only within the full year's, so quarters can skip:
      // a change is only worked out on the period right before.
      const days = (a, b) => (new Date(b) - new Date(a)) / 864e5;
      const adjacent = rows.map((r, i) => i > 0 && days(rows[i - 1].date, r.date) <= (freq === "annual" ? 400 : 100));
      const span = `${rows.length} ${freq === "annual" ? (rows.length === 1 ? "year" : "years") : (rows.length === 1 ? "quarter" : "quarters")}`;
      $("sc-source").innerHTML = d.shares.source?.[freq] === "sec"
        ? `<strong>Source:</strong> SEC filings (EDGAR) · ${span}${rows.some((r) => r.source === "yahoo") ? " · the newest from Yahoo Finance" : ""}${
          freq === "quarterly" && adjacent.slice(1).some((a) => !a) ? " · fourth quarters only come in the full year's figures, so most are missing" : ""}`
        : `<strong>Source:</strong> Yahoo Finance · ${span}. ${esc(whyYahoo(d))}`;
      const labels = rows.map((r) => periodName({ kind: freq, date: r.date }));
      const changes = rows.map((r, i) => (adjacent[i] && rows[i - 1][basis] ? r[basis] / rows[i - 1][basis] - 1 : null));
      const name = `${basis === "basic" ? "Basic" : "Diluted"} average shares`;
      const first = rows[0][basis], last = rows[rows.length - 1][basis];
      const total = rows.length > 1 ? last / first - 1 : null;
      const yearly = freq === "annual" && rows.length > 1 ? (last / first) ** (1 / (rows.length - 1)) - 1 : null;
      $("sc-lede").innerHTML = total == null ? "" : `${esc(name)} ${total < 0 ? "fell" : "rose"} <strong class="${tone(-total)}">${pct(Math.abs(total), { signed: false })}</strong>
        from ${esc(labels[0])} to ${esc(labels[labels.length - 1])}${yearly != null ? ` (${pct(yearly)} a year)` : ""}${total < 0 ? ", so each share owns more of the company" : ""}.`;
      const c1 = cssVar("--co-1");
      const prior = freq === "annual" ? "the year before" : "the quarter before";
      coChart("sc-chart", mode === "count"
        ? { type: "line", data: { labels, datasets: [lineSet(name, rows.map((r) => r[basis]), c1)] },
            options: coOptions({ title: name, y: { ticks: { callback: (v) => axisCompact(v) } },
              tooltip: { label: (c) => ` ${compact(c.parsed.y)}${changes[c.dataIndex] != null ? ` (${pct(changes[c.dataIndex])} on ${prior})` : ""}` } }) }
        : { type: "bar", data: { labels, datasets: [barSet(name, changes, c1)] },
            options: coOptions({ title: `${name}, % change on ${prior}`, y: { ticks: { callback: (v) => pct(v) }, grid: zeroGrid },
              tooltip: { label: (c) => (c.parsed.y == null ? ` No figure for ${prior}` :` ${pct(c.parsed.y)} on ${prior}`) } }) });
      if (!eps) { coChart("sc-eps-chart", null); return; }
      const fc = d.financial_currency || d.currency;
      const epsName = `${basis === "basic" ? "Basic" : "Diluted"} EPS${freq === "quarterly" ? " for the quarter" : ""} (${fc})`;
      coChart("sc-eps-chart", { type: "line", data: { labels, datasets: [lineSet(epsName, rows.map((r) => r[`${basis}_eps`]), cssVar("--co-2"))] },
        options: coOptions({ title: epsName, y: { ticks: { callback: (v) => signedFixed(v) }, grid: zeroGrid },
          tooltip: { label: (c) => ` ${c.parsed.y == null ? "—" : signedFixed(c.parsed.y)} ${fc}` } }) });
    }

    function renderDividends() {
      const d = co.data, dv = d.dividends, cur = d.currency, fc = d.financial_currency || cur;
      const years = dv.years || [];
      if (!years.length && !dv.forward_rate && !dv.trailing_yield) {
        $("div-kpis").innerHTML = `<p class="muted co-lede">${esc(d.symbol)} doesn't pay a dividend.</p>`;
        $("div-body").hidden = true;
        coChart("div-chart", null);
        return;
      }
      const g = dv.growth || {};
      const lastFull = [...years].reverse().find((y) => !y.partial);
      const tiles = [  // [label, value, sub-line HTML]; tiles Yahoo has no figure for are left out
        ["Forward dividend & yield", dv.forward_rate != null ? `${money(dv.forward_rate, cur)}${dv.forward_yield != null ? ` (${pct(dv.forward_yield, { signed: false })})` : ""}` : "—",
          dv.ex_date ? `Ex-dividend ${esc(dayText(dv.ex_date))}` : "No regular dividend declared"],
        ["Trailing yield", pct(dv.trailing_yield, { signed: false }), dv.ttm_amount ? `${esc(money(dv.ttm_amount, cur))} over the past 12 months` : ""],
        ["5-year average yield", pct(dv.five_year_yield, { signed: false })],
        ["Payout ratio", pct(dv.payout_ratio, { signed: false }), dv.fcf_payout != null ? `${pct(dv.fcf_payout, { signed: false })} of free cash flow` : "Of earnings"],
        ["Dividend coverage", times(dv.coverage), dv.fcf_coverage != null ? `${times(dv.fcf_coverage)} by free cash flow` : "Earnings over dividends"],
        ["Dividend growth (5Y)", g["5Y"] != null ? `${pct(g["5Y"])} a year` : "—",
          ["1Y", "3Y", "10Y"].filter((k) => g[k] != null).map((k) => `${k} ${pct(g[k])}`).join(" · ")],
      ].filter(([, value], i) => i === 0 || value !== "—");
      if (d.financials) {  // a fund's distributions rise and fall with what its holdings pay
        tiles.push(["Years of increases", String(dv.streak), dv.streak && lastFull ? `In a row, through ${lastFull.year}` : "No raise last year"]);
      }
      $("div-kpis").innerHTML = tiles.map((t) => kpi(...t)).join("");
      $("div-body").hidden = !years.length;
      const shown = years.slice(-15), c1 = cssVar("--co-1");
      coChart("div-chart", years.length ? {
        type: "bar",
        data: { labels: shown.map((y) => (y.partial ? `${y.year}*` : String(y.year))),
          datasets: [{ ...barSet("Dividends per share", shown.map((y) => y.amount), c1), backgroundColor: shown.map((y) => (y.partial ? withAlpha(c1, 0.35) : c1)) }] },
        options: coOptions({ y: { beginAtZero: true, ticks: { callback: (v) => money(v, cur) } },
          tooltip: {
            title: (items) => { const y = shown[items[0].dataIndex]; return y.partial ? `${y.year} so far` : String(y.year); },
            label: (c) => { const y = shown[c.dataIndex]; return ` ${money(y.amount, cur)}${y.growth != null ? ` (${pct(y.growth)})` : ""}`; },
          } }),
      } : null);
      const hist = dv.history || [];
      $("div-table").innerHTML = hist.length ? `<thead><tr><th scope="col">Fiscal year</th><th class="num" scope="col">Dividends paid</th>
          <th class="num" scope="col">Net income</th><th class="num" scope="col">Payout ratio</th><th class="num" scope="col">Free cash flow</th>
          <th class="num" scope="col">FCF payout</th><th class="num" scope="col">Coverage</th></tr></thead>
        <tbody>${[...hist].reverse().map((h) => `<tr><td class="nowrap">${esc(periodName({ kind: "annual", date: h.date }))}</td>
          <td class="num">${compact(h.paid || null)}</td><td class="num">${compact(h.net_income)}</td><td class="num">${pct(h.payout, { signed: false })}</td>
          <td class="num">${compact(h.fcf)}</td><td class="num">${pct(h.fcf_payout, { signed: false })}</td>
          <td class="num">${times(h.payout ? 1 / h.payout : null)}</td></tr>`).join("")}</tbody>
` : "";
    }

    const RETURNS = [["roic", "ROIC", "--co-1"], ["roe", "ROE", "--co-2"], ["roce", "ROCE", "--co-3"]];
    function renderReturns() {
      const d = co.data, r = d.returns, w = r?.wacc;
      if (!r || !r.points.length) {
        $("ret-kpis").innerHTML = `<p class="muted co-lede">Yahoo Finance has no figures to work out returns on capital for ${esc(d.symbol)}.</p>`;
        $("ret-body").hidden = true;
        coChart("ret-chart", null);
        return [];
      }
      $("ret-body").hidden = false;
      const latest = (key) => [...r.points].reverse().find((p) => p[key] != null);
      const noEbit = !r.points.some((p) => p.roic != null);
      const tiles = RETURNS.map(([key, label]) => {
        const p = latest(key);
        return kpi(label, p ? pct(p[key], { signed: false }) : "—", p ? esc(periodName(p)) : "Not reported");
      });
      tiles.push(kpi("WACC (estimate)", w ? pct(w.wacc, { signed: false }) : "—",
        w ? `Equity ${pct(w.cost_of_equity, { signed: false })} · debt ${pct(w.cost_of_debt * (1 - w.tax_rate), { signed: false })} after tax`
          : noEbit ? "Not meaningful without ROIC" : "Needs a beta and market cap"));
      tiles.push(kpi("ROIC − WACC", r.spread == null ? "—" : pct(r.spread), r.spread == null ? ""
        : r.spread > 0 ? "Earns more than its capital costs" : "Earns less than its capital costs", r.spread == null ? "" : tone(r.spread)));
      $("ret-kpis").innerHTML = tiles.join("");
      const years = r.points.filter((p) => p.kind === "annual").length;
      const span = `${years} ${years === 1 ? "year" : "years"}${r.points.some((p) => p.kind === "ttm") ? " and the TTM" : ""}`;
      $("ret-source").innerHTML = r.source === "sec"
        ? `<strong>Source:</strong> SEC filings (EDGAR) · ${span}${r.points.some((p) => p.source === "yahoo") ? " · the newest from Yahoo Finance" : ""}`
        : `<strong>Source:</strong> Yahoo Finance · ${span}. ${esc(whyYahoo(d))}`;

      const labels = r.points.map(periodName);
      const series = RETURNS.filter(([key]) => r.points.some((p) => p[key] != null));
      const datasets = series.map(([key, label, color]) => lineSet(label, r.points.map((p) => p[key]), cssVar(color)));
      if (w) {
        datasets.push(lineSet("WACC (today)", labels.map(() => w.wacc), cssVar("--muted"),
          { borderWidth: 1.5, borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 0 }));
      }
      coChart("ret-chart", { type: "line", data: { labels, datasets },
        options: coOptions({ legend: true, y: { ticks: { callback: (v) => pct(v, { signed: false }) }, grid: zeroGrid },
          tooltip: { label: (c) => ` ${c.dataset.label}: ${c.parsed.y == null ? "—" : pct(c.parsed.y, { signed: false })}` } }) });
      $("ret-table").innerHTML = `<thead><tr><th scope="col">Return</th>${r.points.map((p) => colHead(p)).join("")}</tr></thead>
        <tbody>${series.map(([key, label]) => `<tr><td>${label}</td>${r.points.map((p) => `<td class="num">${pct(p[key], { signed: false })}</td>`).join("")}</tr>`).join("")}
        ${w ? `<tr><td>ROIC − WACC</td>${r.points.map((p) => `<td class="num ${tone(p.roic == null ? null : p.roic - w.wacc)}">${p.roic == null ? "—" : pct(p.roic - w.wacc)}</td>`).join("")}</tr>` : ""}</tbody>`;
      return [
        "ROIC is EBIT after tax over invested capital (equity plus debt).",
        "ROE is earnings to common shareholders over their equity.",
        "ROCE is EBIT over total assets less current liabilities.",
        "All three use capital at each period's end.",
        noEbit && "Yahoo Finance reports no EBIT for banks and insurers, so they have no ROIC or ROCE here, and no WACC: their debt includes customer deposits.",
        ...(w ? [
          `The WACC is an estimate weighted by today's market cap and debt (${pct(w.weight_equity, { signed: false })} equity).`,
          `Equity costs the 10-year US Treasury yield (${pct(w.risk_free, { signed: false })}) plus a beta of ${w.beta.toFixed(2)} times a ${pct(w.risk_premium, { signed: false })} equity risk premium.`,
          `Debt costs ${w.debt_cost_reported ? "its interest expense over total debt" : "the Treasury yield, as no interest expense is reported"}, less tax at ${pct(w.tax_rate, { signed: false })}.`,
          "ROIC − WACC compares every year with today's WACC.",
        ] : []),
      ];
    }

    function renderCapital() {
      renderShares();
      renderDividends();
      const returnsNotes = renderReturns();
      const fc = co.data.financial_currency || co.data.currency;
      setNotes($("cap-note"), ["Share counts are averages over each period, restated for splits.",
        "Dividends per share are by calendar year of the ex-dividend date, adjusted for splits; * marks this year so far.",
        `The dividend table's dividends paid are the cash paid to shareholders, in ${fc}, from the cash flow statement.`,
        ...returnsNotes]);
    }

    // Each tab's notes sit behind a Notes button, closed until opened (and then kept open).
    function syncNotes() {
      document.querySelectorAll("[data-notes]").forEach((b) => {
        const note = $(b.getAttribute("aria-controls"));
        b.hidden = !note.textContent.trim();
        b.setAttribute("aria-expanded", String(co.notes));
        note.hidden = !co.notes;
      });
    }
    document.querySelectorAll("[data-notes]").forEach((b) => b.addEventListener("click", () => {
      co.notes = !co.notes;
      saveCompany();
      syncNotes();
    }));

    function renderCompany() {
      if (!co.data) return;
      ({ financials: renderFinancials, ownership: renderOwnership, capital: renderCapital })[co.tab]();
      syncNotes();
    }
    const COMPANY_TABS = ["financials", "ownership", "capital"];
    function showTab(tab, focus = false) {
      co.tab = tab;
      $("co-tabs").querySelectorAll("[role=tab]").forEach((b) => {
        const on = b.dataset.tab === tab;
        b.setAttribute("aria-selected", String(on));
        b.tabIndex = on ? 0 : -1;
        if (on && focus) b.focus();
      });
      COMPANY_TABS.forEach((t) => { $(`co-${t}`).hidden = !co.data || t !== tab; });
      dropCharts();  // charts are drawn for the visible tab, so they get its size
      renderCompany();
    }
    $("co-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("[role=tab]");
      if (!b) return;
      showTab(b.dataset.tab);
      saveCompany();
    });
    $("co-tabs").addEventListener("keydown", (e) => {
      const i = COMPANY_TABS.indexOf(co.tab);
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: COMPANY_TABS.length - 1 }[e.key];
      if (next == null) return;
      e.preventDefault();
      showTab(COMPANY_TABS[(next + COMPANY_TABS.length) % COMPANY_TABS.length], true);
      saveCompany();
    });
    const companyPick = (groupId, attr, apply) => $(groupId).addEventListener("click", (e) => {
      const b = e.target.closest(`button[data-${attr}]`);
      if (!b) return;
      pressed(groupId, attr, b.dataset[attr]);
      apply(b.dataset[attr]);
      saveCompany();
      renderCompany();
    });
    companyPick("fin-view", "view", (v) => { co.view = v; });
    companyPick("fin-freq", "freq", (v) => { co.freq = v; });
    companyPick("own-list", "list", (v) => { co.list = v; });
    companyPick("sc-mode", "mode", (v) => { co.sc.mode = v; });
    companyPick("sc-basis", "basis", (v) => { co.sc.basis = v; });
    companyPick("sc-freq", "freq", (v) => { co.sc.freq = v; });
    $("sc-eps").addEventListener("click", () => {
      co.sc.eps = !co.sc.eps;
      $("sc-eps").setAttribute("aria-pressed", String(co.sc.eps));
      saveCompany();
      renderCompany();
    });
    pressed("fin-view", "view", co.view);
    pressed("fin-freq", "freq", co.freq);
    pressed("own-list", "list", co.list);
    pressed("sc-mode", "mode", co.sc.mode);
    pressed("sc-basis", "basis", co.sc.basis);
    pressed("sc-freq", "freq", co.sc.freq);
    $("sc-eps").setAttribute("aria-pressed", String(co.sc.eps));

    let companySeq = 0;
    async function loadCompany() {
      const mine = ++companySeq;
      co.data = null;
      dropCharts();
      $("co-status").hidden = false;
      $("co-status").textContent = "Loading…";
      showTab(co.tab);
      try {
        const d = await getJSON(root.dataset.companyApi.replace("SYMBOL", encodeURIComponent(state.symbol)));
        if (mine !== companySeq) return;
        co.data = d;
        $("co-status").hidden = true;
        showTab(co.tab);
      } catch (err) {
        if (mine !== companySeq) return;
        $("co-status").textContent = `Couldn't load the company figures for ${state.symbol}: ${err.message}`;
      }
    }

    pressed("r-mode", "mode", state.mode);
    pressed("r-range", "range", state.range);
    syncMenu();
    syncReset();
    refresh();
    loadStats();
    loadCompany();
  }

  const pages = { dashboard: () => { dashboardWorth(); if (root) dashboard(); }, performance, position, "txn-form": txnForm, markets, assets: myAssets, "asset-form": assetForms, research };
  pages[document.body.dataset.page]?.();
})();
