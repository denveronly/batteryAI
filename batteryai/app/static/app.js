"use strict";

// Relative URLs keep working behind the Home Assistant ingress path prefix.
const api = (path, options) =>
  fetch(path, options).then((r) => {
    if (!r.ok && r.status !== 409) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  });

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = (id) => document.getElementById(id);
const charts = {};
let status = null;
let refreshTimer = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const fmt = (value, digits = 1) =>
  value === null || value === undefined ? "—" : Number(value).toFixed(digits).replace(/\.0+$/, "");
const fmtTime = (ts) =>
  new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
const fmtDateTime = (ts) =>
  new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const unit = (field, fallback) => (status?.units?.[field] || fallback);

// Charts ----------------------------------------------------------------------

function baseOptions(yTitle, extra = {}) {
  const grid = css("--grid");
  const text = css("--muted");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: text, boxWidth: 12 } },
      tooltip: {
        callbacks: { title: (items) => (items.length ? fmtTime(items[0].parsed.x / 1000) : "") },
      },
    },
    scales: {
      x: {
        type: "linear",
        grid: { color: grid },
        ticks: {
          color: text,
          maxTicksLimit: 8,
          callback: (v) => {
            const d = new Date(v);
            return d.getHours() === 0 && d.getMinutes() === 0
              ? d.toLocaleDateString([], { weekday: "short", day: "numeric" })
              : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          },
        },
      },
      y: { grid: { color: grid }, ticks: { color: text }, title: { display: true, text: yTitle, color: text }, ...extra },
    },
  };
}

function line(label, color, points, extra = {}) {
  return {
    label,
    data: points,
    borderColor: color,
    backgroundColor: color + "33",
    borderWidth: 2,
    pointRadius: 0,
    spanGaps: true,
    ...extra,
  };
}

function upsertChart(id, config) {
  if (charts[id]) {
    charts[id].data = config.data;
    charts[id].options = config.options;
    charts[id].update();
  } else {
    charts[id] = new Chart($(id), config);
  }
}

function renderBattery(rows) {
  const pts = (key) => rows.map((r) => ({ x: r.ts * 1000, y: r[key] }));
  upsertChart("batteryChart", {
    type: "line",
    data: {
      datasets: [
        line("Battery SOC", css("--series-soc"), pts("battery_soc"), { fill: "origin" }),
        line("Deye program SOC", css("--series-target"), pts("target_soc"), {
          stepped: true,
          borderDash: [6, 4],
          backgroundColor: "transparent",
        }),
      ],
    },
    options: baseOptions("%", { min: 0, max: 100 }),
  });
}

function renderLoad(rows) {
  const pts = (key) => rows.map((r) => ({ x: r.ts * 1000, y: r[key] }));
  upsertChart("loadChart", {
    type: "line",
    data: {
      datasets: [
        line("Today load", css("--series-load"), pts("today_load")),
        line("Today consumption", css("--series-consumption"), pts("today_consumption")),
      ],
    },
    options: baseOptions(unit("today_load", "kWh"), { beginAtZero: true }),
  });
}

const weekendShading = {
  id: "weekendShading",
  beforeDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    const days = opts.days || [];
    const width = (scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0)) || 0;
    ctx.save();
    ctx.fillStyle = css("--weekend");
    days.forEach((day, i) => {
      if (!day.is_weekend) return;
      const x = scales.x.getPixelForValue(i);
      ctx.fillRect(x - width / 2, chartArea.top, width, chartArea.bottom - chartArea.top);
    });
    ctx.restore();
  },
};

function renderDaily(days) {
  const text = css("--muted");
  const grid = css("--grid");
  const bar = (label, color, key) => ({
    label,
    data: days.map((d) => d[key]),
    backgroundColor: color,
    borderRadius: 4,
    maxBarThickness: 18,
  });
  upsertChart("dailyChart", {
    type: "bar",
    data: {
      labels: days.map((d) => {
        const date = new Date(d.date + "T12:00:00");
        return date.toLocaleDateString([], { weekday: "short", day: "numeric" });
      }),
      datasets: [
        bar("Load", css("--series-load"), "load_kwh"),
        bar("Consumption", css("--series-consumption"), "consumption_kwh"),
        bar("Solar forecast", css("--series-forecast"), "solar_forecast_kwh"),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { labels: { color: text, boxWidth: 12 } }, weekendShading: { days } },
      scales: {
        x: { grid: { display: false }, ticks: { color: text } },
        y: { beginAtZero: true, grid: { color: grid }, ticks: { color: text }, title: { display: true, text: "kWh", color: text } },
      },
    },
    plugins: [weekendShading],
  });
}

// Status, tiles, programs -----------------------------------------------------

function tile(label, value, suffix) {
  return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value, suffix ? el("small", {}, " ", suffix) : null));
}

function renderStatus() {
  const latest = status.latest || {};
  const next = status.next_analysis ? new Date(status.next_analysis) : null;
  $("schedule").textContent =
    `Analyses daily at ${status.analysis_times.join(", ")} · model ${status.model}` +
    (next ? ` · next ${next.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "");

  $("warnings").replaceChildren(...status.warnings.map((w) => el("div", { class: "banner" }, w)));

  const weekday = latest.weekday ? latest.weekday[0].toUpperCase() + latest.weekday.slice(1) : "—";
  $("tiles").replaceChildren(
    tile("Battery SOC", fmt(latest.battery_soc, 0), "%"),
    tile("Solar today", fmt(latest.today_forecast), unit("today_forecast", "kWh")),
    tile("Solar tomorrow", fmt(latest.tomorrow_forecast), unit("tomorrow_forecast", "kWh")),
    tile("Today load", fmt(latest.today_load), unit("today_load", "kWh")),
    tile("Today consumption", fmt(latest.today_consumption), unit("today_consumption", "kWh")),
    tile("Probable outages", latest.outages_state ?? "—"),
    tile("Day", weekday, latest.is_weekend ? "weekend" : latest.weekday ? "weekday" : ""),
    tile("Last reading", latest.ts ? fmtTime(latest.ts) : "—"),
  );

  const programs = latest.deye_programs || [];
  const table = $("programs");
  table.replaceChildren(
    el("tr", {}, el("th", {}, "Program"), el("th", {}, "Start time"), el("th", {}, "SOC capacity")),
    ...(programs.length
      ? programs.map((p) => el("tr", { class: p.slot === status.active_program_slot ? "active" : "" }, el("td", {}, `#${p.slot}`), el("td", {}, p.time ?? "—"), el("td", {}, p.soc === null ? "—" : `${fmt(p.soc, 0)} %`)))
      : [el("tr", {}, el("td", { colspan: "3", class: "empty" }, "No Deye program data yet."))]),
  );

  $("analyze").disabled = status.analysis_running;
  $("analyze").textContent = status.analysis_running ? "Analysing…" : "Run analysis now";
}

// Analysis log ----------------------------------------------------------------

function renderLog(analyses) {
  $("logInfo").textContent = analyses.length ? `${analyses.length} most recent runs` : "";
  if (!analyses.length) {
    $("log").replaceChildren(el("div", { class: "empty" }, "No analyses yet. They run on the schedule above, or press “Run analysis now”."));
    return;
  }
  $("log").replaceChildren(...analyses.map(renderEntry));
}

function renderEntry(a) {
  const r = a.result;
  const duration = a.finished_ts ? `${a.finished_ts - a.ts}s` : null;
  const tokens = a.input_tokens ? `${a.input_tokens.toLocaleString()} in / ${a.output_tokens.toLocaleString()} out tokens` : null;
  const head = el(
    "div",
    { class: "entry-head" },
    el("span", { class: "entry-time" }, fmtDateTime(a.ts)),
    el("span", { class: `badge ${a.status}` }, a.status),
    el("span", { class: "badge" }, a.trigger),
    el("span", { class: "muted" }, [a.model, duration, tokens].filter(Boolean).join(" · ")),
  );

  if (a.status === "running") return el("div", { class: "entry" }, head, el("div", { class: "muted" }, "Claude is analysing the data…"));
  if (a.status === "error") return el("div", { class: "entry error" }, head, el("div", { class: "error-text" }, a.error));

  const chips = el(
    "div",
    { class: "chips" },
    el("span", { class: "chip" }, "Rest of today ", el("b", {}, `${fmt(r.predicted_consumption_rest_of_today_kwh)} kWh`)),
    el("span", { class: "chip" }, "Tomorrow ", el("b", {}, `${fmt(r.predicted_consumption_tomorrow_kwh)} kWh`)),
    el("span", { class: "chip" }, "Min SOC ", el("b", {}, `${fmt(r.predicted_min_soc_percent, 0)} %`)),
    el("span", { class: "chip" }, "Outage risk ", el("b", {}, r.outage_risk)),
    el("span", { class: "chip" }, "Confidence ", el("b", {}, r.confidence)),
  );

  const programs = (r.deye_programs || []).length
    ? el(
        "table",
        { class: "programs" },
        el("tr", {}, el("th", {}, "Program"), el("th", {}, "Time"), el("th", {}, "SOC"), el("th", {}, "Why")),
        ...r.deye_programs.map((p) => el("tr", {}, el("td", {}, `#${p.slot}`), el("td", {}, p.time), el("td", {}, `${fmt(p.soc_percent, 0)} %`), el("td", {}, p.reason))),
      )
    : null;

  return el(
    "div",
    { class: "entry" },
    head,
    el("div", {}, r.summary),
    chips,
    r.recommendations?.length ? el("ul", {}, ...r.recommendations.map((t) => el("li", {}, t))) : null,
    programs ? el("details", {}, el("summary", {}, "Suggested Deye programs"), programs) : null,
    el("details", {}, el("summary", {}, "Reasoning"), el("pre", {}, r.reasoning)),
    inputDetails(a.id),
  );
}

function inputDetails(id) {
  const pre = el("pre", {}, "Loading…");
  const details = el("details", {}, el("summary", {}, "Data sent to Claude"), pre);
  details.addEventListener("toggle", () => {
    if (!details.open || pre.dataset.loaded) return;
    api(`api/analyses/${id}/input`)
      .then((data) => { pre.textContent = JSON.stringify(data, null, 2); pre.dataset.loaded = "1"; })
      .catch((err) => { pre.textContent = String(err); });
  });
  return details;
}

// Refresh loop ----------------------------------------------------------------

async function refresh() {
  clearTimeout(refreshTimer);
  const hours = $("range").value;
  try {
    const [s, rows, days, analyses] = await Promise.all([
      api("api/status"),
      api(`api/readings?hours=${hours}`),
      api("api/daily?days=14"),
      api("api/analyses?limit=30"),
    ]);
    status = s;
    renderStatus();
    renderBattery(rows);
    renderLoad(rows);
    renderDaily(days);
    renderLog(analyses);
  } catch (err) {
    $("warnings").replaceChildren(el("div", { class: "banner" }, `Could not load data: ${err.message}`));
  }
  // Poll quickly while an analysis is running so the log updates when it finishes.
  refreshTimer = setTimeout(refresh, status?.analysis_running ? 5000 : 60000);
}

$("range").addEventListener("change", refresh);
$("analyze").addEventListener("click", async () => {
  $("analyze").disabled = true;
  const res = await api("api/analyze", { method: "POST" }).catch((err) => ({ error: err.message }));
  if (res.error) alert(res.error);
  setTimeout(refresh, 500);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  Object.values(charts).forEach((c) => c.destroy());
  for (const key of Object.keys(charts)) delete charts[key];
  refresh();
});

refresh();
