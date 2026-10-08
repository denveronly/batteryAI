"use strict";

// Relative URLs keep working behind the Home Assistant ingress path prefix.
const api = (path, options) =>
  fetch(path, options).then(async (r) => {
    const body = await r.json().catch(() => ({}));
    if (!r.ok && !body.error) throw new Error(`${path}: HTTP ${r.status}`);
    return body;
  });

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = (id) => document.getElementById(id);
const charts = {};
let status = null;
let latestPrediction = null;
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
const pct = (v) => (v === null || v === undefined ? "—" : `${fmt(v, 0)} %`);
const unit = (field, fallback) => status?.units?.[field] || fallback;
const shortDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString([], { weekday: "short", day: "numeric" });

// Deye program time ranges ----------------------------------------------------

const pad2 = (n) => String(n).padStart(2, "0");
const fmtMin = (m) => `${pad2(Math.floor(m / 60))}:${pad2(m % 60)}`;

function toMinutes(value) {
  const text = String(value ?? "").trim();
  if (!text || ["unknown", "unavailable", "none"].includes(text.toLowerCase())) return null;
  let h, m;
  if (text.includes(":")) [h, m] = text.split(":").map((x) => parseInt(x, 10));
  else {
    const n = parseInt(text, 10);
    if (Number.isNaN(n)) return null;
    [h, m] = [Math.floor(n / 100), n % 100];
  }
  return h >= 0 && h < 24 && m >= 0 && m < 60 ? h * 60 + m : null;
}

// slot -> [start, end] minutes. With "start" (default) a program runs from its own time
// until the next program's time, and the last one until P1: P1 00:00, P2 03:00 = 00:00–03:00.
// With "end" a program's time is the END of its range, which starts at the previous one.
function programSpans(programs) {
  const timeIsEnd = (status?.program_time_marks || "start") === "end";
  const timed = programs
    .map((p) => [toMinutes(p.time), p.slot])
    .filter(([m]) => m !== null)
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const spans = {};
  timed.forEach(([minute, slot], i) => {
    spans[slot] = timeIsEnd
      ? [timed[(i - 1 + timed.length) % timed.length][0], minute]
      : [minute, timed[(i + 1) % timed.length][0]];
  });
  return spans;
}

function rangeText(span) {
  if (!span) return "—";
  return span[0] === span[1] ? `${fmtMin(span[0])} (unused)` : `${fmtMin(span[0])} – ${fmtMin(span[1])}`;
}

// Charts ----------------------------------------------------------------------

// Ticks on round local hours (3 h, 6 h, a day…) instead of wherever the data starts.
function alignedTicks(scale) {
  const span = (scale.max - scale.min) / 3_600_000;
  const step = [1, 2, 3, 6, 12, 24, 48, 72, 168].find((h) => span / h <= 9) || 336;
  const start = new Date(scale.min);
  start.setHours(0, 0, 0, 0);
  const ticks = [];
  for (let t = start.getTime(); t <= scale.max; ) {
    if (t >= scale.min) ticks.push({ value: t });
    const d = new Date(t);
    d.setHours(d.getHours() + step);
    t = d.getTime();
  }
  scale.ticks = ticks;
}

// Shaded band per Deye program range, labelled with its SOC, repeated for every day shown.
const programBands = {
  id: "programBands",
  beforeDatasetsDraw(chart, _args, opts) {
    const programs = opts.programs || [];
    if (!programs.length) return;
    const spans = programSpans(programs);
    const { ctx, chartArea, scales } = chart;
    const x = scales.x;
    const color = css("--series-target");
    const first = new Date(x.min);
    first.setHours(0, 0, 0, 0);
    first.setDate(first.getDate() - 1);
    ctx.save();
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "top";
    for (const day = new Date(first); day.getTime() <= x.max; day.setDate(day.getDate() + 1)) {
      for (const p of programs) {
        const span = spans[p.slot];
        if (!span || span[0] === span[1]) continue;
        const startDate = new Date(day);
        startDate.setMinutes(span[0]);
        const endDate = new Date(day);
        endDate.setMinutes(span[1] + (span[1] <= span[0] ? 1440 : 0));
        const left = Math.max(chartArea.left, x.getPixelForValue(startDate.getTime()));
        const right = Math.min(chartArea.right, x.getPixelForValue(endDate.getTime()));
        if (right <= left) continue;
        ctx.fillStyle = color + (p.slot % 2 ? "1f" : "0d");
        ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
        ctx.strokeStyle = color + "40";
        ctx.beginPath();
        ctx.moveTo(left, chartArea.top);
        ctx.lineTo(left, chartArea.bottom);
        ctx.stroke();
        const label = `P${p.slot}${p.soc !== null && p.soc !== undefined ? ` ${fmt(p.soc, 0)}%` : ""}`;
        if (right - left > ctx.measureText(label).width + 6) {
          ctx.fillStyle = css("--muted");
          ctx.fillText(label, left + 3, chartArea.top + 3);
        }
      }
    }
    ctx.restore();
  },
};

function timeOptions(yTitle, y = {}, y2 = null) {
  const grid = css("--grid");
  const text = css("--muted");
  const scales = {
    x: {
      type: "linear",
      grid: { color: grid },
      afterBuildTicks: alignedTicks,
      ticks: {
        color: text,
        autoSkip: false,
        callback: (v) => {
          const d = new Date(v);
          return d.getHours() === 0 && d.getMinutes() === 0
            ? d.toLocaleDateString([], { weekday: "short", day: "numeric" })
            : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        },
      },
    },
    y: { grid: { color: grid }, ticks: { color: text }, title: { display: true, text: yTitle, color: text }, ...y },
  };
  if (y2) scales.y2 = { position: "right", grid: { display: false }, ticks: { color: text }, title: { display: true, text: y2, color: text } };
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: text, boxWidth: 12 } },
      tooltip: { callbacks: { title: (items) => (items.length ? fmtTime(items[0].parsed.x / 1000) : "") } },
    },
    scales,
  };
}

function line(label, color, points, extra = {}) {
  return { label, data: points, borderColor: color, backgroundColor: color + "33", borderWidth: 2, pointRadius: 0, spanGaps: true, ...extra };
}

function hasPoints(datasets) {
  return datasets.some((d) => d.data.some((p) => (typeof p === "object" && p !== null ? p.y : p) !== null && (typeof p === "object" && p !== null ? p.y : p) !== undefined));
}

function upsertChart(id, config, emptyText) {
  const empty = $(id).parentElement.querySelector(".chart-empty");
  const show = hasPoints(config.data.datasets);
  if (empty) {
    empty.hidden = show;
    empty.textContent = emptyText || "No data yet.";
  }
  if (typeof Chart === "undefined") {
    if (empty) { empty.hidden = false; empty.textContent = "Chart library failed to load."; }
    return;
  }
  if (charts[id]) {
    charts[id].data = config.data;
    charts[id].options = config.options;
    charts[id].update();
  } else {
    charts[id] = new Chart($(id), config);
  }
}

const NO_READINGS =
  "No readings yet. History is imported from Home Assistant on first start (Settings → History), and new values are recorded every few minutes.";

function renderBattery(rows) {
  const pts = (key) => rows.map((r) => ({ x: r.ts * 1000, y: r[key] }));
  upsertChart("batteryChart", {
    type: "line",
    data: {
      datasets: [
        line("Battery SOC", css("--series-soc"), pts("battery_soc"), { fill: "origin" }),
        line("Program SOC (recorded)", css("--series-target"), pts("target_soc"), { stepped: true, borderDash: [6, 4], backgroundColor: "transparent" }),
      ],
    },
    options: (() => {
      const o = timeOptions("%", { min: 0, max: 100 });
      o.plugins.programBands = { programs: status?.latest?.deye_programs || [] };
      return o;
    })(),
    plugins: [programBands],
  }, NO_READINGS);
}

function renderPower(rows, predicted = []) {
  const pts = (key) => rows.map((r) => ({ x: r.ts * 1000, y: r[key] }));
  const datasets = [line("Load", css("--series-load"), pts("load_power"), { fill: "origin", backgroundColor: css("--series-load") + "22" })];
  if (predicted.length) {
    datasets.push(line("Predicted load", css("--series-target"), predicted.map((p) => ({ x: p.ts * 1000, y: p.load_w })), {
      stepped: "before", borderDash: [6, 4], borderWidth: 2, backgroundColor: "transparent",
    }));
  }
  const pv = pts("pv_power");
  if (pv.some((p) => p.y !== null)) datasets.push(line("PV", css("--series-pv"), pv, { fill: "origin", backgroundColor: css("--series-pv") + "22" }));
  appliancesList().forEach((a, i) =>
    datasets.push(line(a.name, applianceColor(i), rows.map((r) => ({ x: r.ts * 1000, y: r.appliances?.[a.id] ?? null })))));
  const temps = pts("outdoor_temp");
  const hasTemp = temps.some((p) => p.y !== null);
  if (hasTemp) datasets.push(line("Outdoor temp", css("--series-temp"), temps, { yAxisID: "y2", borderDash: [4, 4], borderWidth: 1.5, backgroundColor: "transparent" }));
  upsertChart("loadChart", { type: "line", data: { datasets }, options: timeOptions("W", { beginAtZero: true }, hasTemp ? "°" : null) }, NO_READINGS);
}

const weekendShading = {
  id: "weekendShading",
  beforeDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    const days = opts.days || [];
    const width = scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0) || 0;
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

function categoryOptions(days, yTitle, y2Title) {
  const text = css("--muted");
  const grid = css("--grid");
  const scales = {
    x: { grid: { display: false }, ticks: { color: text } },
    y: { beginAtZero: true, grid: { color: grid }, ticks: { color: text }, title: { display: true, text: yTitle, color: text } },
  };
  if (y2Title) scales.y2 = { position: "right", grid: { display: false }, ticks: { color: text }, title: { display: true, text: y2Title, color: text } };
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: text, boxWidth: 12 } }, weekendShading: { days } },
    scales,
  };
}

function bar(label, color, data, extra = {}) {
  return { type: "bar", label, data, backgroundColor: color, borderRadius: 4, maxBarThickness: 16, ...extra };
}

function renderDaily(days) {
  const col = (key) => days.map((d) => d[key]);
  const datasets = [bar("Consumption", css("--series-consumption"), col("consumption_kwh"))];
  appliancesList().forEach((a, i) =>
    datasets.push(bar(a.name, applianceColor(i), days.map((d) => d.appliances?.[a.id]?.kwh ?? null))));
  if (col("pv_kwh").some((v) => v !== null)) datasets.push(bar("PV", css("--series-pv"), col("pv_kwh")));
  datasets.push({ type: "line", label: "Solar forecast", data: col("solar_forecast_kwh"), borderColor: css("--series-forecast"), backgroundColor: css("--series-forecast"), borderDash: [5, 4], pointRadius: 3, borderWidth: 1.5 });
  const hasTemp = col("temp_avg").some((v) => v !== null);
  if (hasTemp) datasets.push({ type: "line", label: "Avg temp", data: col("temp_avg"), yAxisID: "y2", borderColor: css("--series-temp"), backgroundColor: css("--series-temp"), pointRadius: 2, borderWidth: 1.5 });
  upsertChart("dailyChart", {
    data: { labels: days.map((d) => shortDate(d.date)), datasets },
    options: categoryOptions(days, "kWh", hasTemp ? "°" : null),
    plugins: [weekendShading],
  }, NO_READINGS);
}

function renderAccuracy(report) {
  const days = report.days;
  const col = (key) => days.map((d) => d[key]);
  const avg = report.average;
  $("accuracySummary").replaceChildren(
    el("span", { class: "chip" }, "Prediction accuracy ", el("b", {}, pct(avg.prediction_accuracy))),
    el("span", { class: "chip" }, "Solar forecast accuracy ", el("b", {}, pct(avg.solar_accuracy))),
    el("span", { class: "chip" }, "PV covered ", el("b", {}, pct(avg.pv_coverage)), " of consumption"),
    el("span", { class: "chip" }, "Grid share ", el("b", {}, pct(avg.grid_share))),
  );
  upsertChart("accuracyChart", {
    data: {
      labels: days.map((d) => shortDate(d.date)),
      datasets: [
        bar("Predicted", css("--series-target"), col("predicted_kwh")),
        bar("Actual", css("--series-consumption"), col("actual_kwh")),
        bar("PV", css("--series-pv"), col("pv_kwh")),
        { type: "line", label: "Prediction accuracy %", data: col("prediction_accuracy"), yAxisID: "y2", borderColor: css("--series-soc"), backgroundColor: css("--series-soc"), pointRadius: 3, borderWidth: 2 },
      ],
    },
    options: (() => {
      const o = categoryOptions(days, "kWh", "%");
      o.scales.y2.min = 0;
      o.scales.y2.max = 100;
      return o;
    })(),
    plugins: [weekendShading],
  }, "No completed days with a prediction yet. Accuracy appears the day after the first prediction.");

  const header = ["Day", "Predicted", "Actual", "Accuracy", "Solar fcst", "PV", "Solar acc.", "PV covered", "Grid", "Temp"];
  $("accuracyTable").replaceChildren(
    el("tr", {}, ...header.map((h) => el("th", {}, h))),
    ...days.slice().reverse().map((d) =>
      el("tr", {},
        el("td", {}, shortDate(d.date)),
        el("td", {}, d.predicted_kwh === null ? "—" : `${fmt(d.predicted_kwh)} kWh`),
        el("td", {}, d.actual_kwh === null ? "—" : `${fmt(d.actual_kwh)} kWh`),
        el("td", {}, pct(d.prediction_accuracy)),
        el("td", {}, d.solar_forecast_kwh === null ? "—" : `${fmt(d.solar_forecast_kwh)} kWh`),
        el("td", {}, d.pv_kwh === null ? "—" : `${fmt(d.pv_kwh)} kWh`),
        el("td", {}, pct(d.solar_accuracy)),
        el("td", {}, pct(d.pv_coverage)),
        el("td", {}, pct(d.grid_share)),
        el("td", {}, d.temp_avg === null ? "—" : `${fmt(d.temp_avg)}°`),
      )),
  );
}

// Grid availability -----------------------------------------------------------

function renderGrid(g, hours) {
  $("gridCard").hidden = !g.entity;
  if (!g.entity) return;
  const now = Date.now();
  const from = now - hours * 3600 * 1000;
  const at = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const dur = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${pad2(m % 60)} min` : `${m} min`);
  // Stepped availability: 1 = grid on, 0 = outage.
  const points = [{ x: from, y: 1 }];
  for (const o of g.outages) {
    const start = Math.max(o.start * 1000, from);
    const end = o.end ? o.end * 1000 : now;
    points.push({ x: start, y: 1 }, { x: start, y: 0 }, { x: end, y: 0 });
    if (o.end) points.push({ x: end, y: 1 });
  }
  if (g.up !== false) points.push({ x: now, y: 1 });
  const options = timeOptions("Grid", { min: -0.1, max: 1.1, ticks: { color: css("--muted"), stepSize: 1, callback: (v) => (v === 1 ? "On" : v === 0 ? "Off" : "") } });
  options.scales.x.min = from;
  options.scales.x.max = now;
  options.plugins.legend = { display: false };
  upsertChart("gridChart", {
    type: "line",
    data: { datasets: [line("Grid", css("--series-pv"), points, { stepped: true, fill: "origin", backgroundColor: css("--series-pv") + "22" })] },
    options,
  }, "No grid data yet.");
  const h = g.hours_without_grid || {};
  $("gridInfo").textContent = `${g.up === false ? "⚠ Outage now" : g.up ? "Grid on" : "Unknown"} · without grid: yesterday ${h.yesterday ?? 0} h, today ${h.today ?? 0} h · last 7 days: ${g.week.count} outages, ${dur(g.week.minutes)}`
    + (g.strict ? ` · 🔒 strict: ${g.strict}` : "") + (g.backfilling ? " · reading history…" : "");
  const recent = g.outages.slice().reverse().slice(0, 10);
  $("gridOutages").replaceChildren(
    el("tr", {}, el("th", {}, "Outage"), el("th", {}, "Until"), el("th", { class: "num" }, "Duration")),
    ...(recent.length ? recent.map((o) => el("tr", {}, el("td", {}, at(o.start)), el("td", {}, o.end ? at(o.end) : "now"), el("td", { class: "num" }, dur(o.minutes))))
      : [el("tr", {}, el("td", { class: "empty", colspan: 3 }, "No outages in this period."))]),
  );
  const expectedRows = [["Today", g.expected.today], ["Tomorrow", g.expected.tomorrow]].flatMap(([day, list]) =>
    list.map((w) => el("tr", {}, el("td", {}, day), el("td", {}, `${w.from}–${w.to}`, w.shift_hours ? el("span", { class: "hint" }, ` ±${w.shift_hours} h`) : ""), el("td", { class: "num" }, dur(w.minutes)))));
  $("gridExpected").replaceChildren(
    el("tr", {}, el("th", {}, "Expected"), el("th", {}, "Window"), el("th", { class: "num" }, "Length")),
    ...(expectedRows.length ? expectedRows : [el("tr", {}, el("td", { class: "empty", colspan: 3 }, "No outages expected (none yesterday)."))]),
  );
}

// Status, tiles, programs, control ---------------------------------------------

function tile(label, value, suffix) {
  return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value, suffix ? el("small", {}, " ", suffix) : null));
}

// "kWh · forecast 80.8 × 110%" when the solar forecast correction is not 100%.
function solarSuffix(key) {
  const kwh = unit(key, "kWh");
  const pct = status?.solar_forecast_percent ?? 100;
  const raw = status?.forecast_raw?.[key];
  if (pct === 100) return kwh;
  return raw === null || raw === undefined ? `${kwh} · ${pct}% of forecast` : `${kwh} · forecast ${fmt(raw)} × ${pct}%`;
}

function pvTiles(latest) {
  if (latest.pv_power === null || latest.pv_power === undefined) return [];
  const tiles = [tile("PV now", fmt(latest.pv_power, 0), "W")];
  if (latest.load_power !== null && latest.load_power !== undefined) {
    const surplus = latest.pv_power - latest.load_power;
    // No negative surplus: when PV doesn't cover the load the rest comes from the battery or grid.
    const covered = latest.load_power > 0 ? Math.min(100, (latest.pv_power / latest.load_power) * 100) : 100;
    tiles.push(
      surplus > 50
        ? tile("PV surplus", `+${fmt(surplus, 0)}`, "W · battery charging")
        : tile("PV surplus", "0", latest.pv_power < 20 ? "W · no PV now" : `W · PV covers ${fmt(covered, 0)} % of the load`),
    );
  }
  return tiles;
}

function weatherText(w) {
  if (!w || !w.tomorrow) return null;
  const t = w.tomorrow;
  return `${fmt(t.templow)}…${fmt(t.temperature)}${w.unit || "°"}`;
}

function renderStatus() {
  const latest = status.latest || {};
  $("version").textContent = status.version ? `v${status.version}` : "";
  renderBankName();
  const next = status.next_analysis ? new Date(status.next_analysis) : null;
  $("schedule").textContent =
    `Predictions daily at ${status.analysis_times.join(", ")} (${status.time_zone}) · model ${status.model}` +
    (next ? ` · next ${next.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "");

  const warnings = [...status.warnings];
  const imp = status.history_import;
  if (imp?.running) warnings.unshift(`Importing history from Home Assistant… (${imp.done ?? 0}/${imp.total ?? "?"} entities)`);
  $("warnings").replaceChildren(...warnings.map((w) => el("div", { class: "banner" }, w)));

  const weekday = latest.weekday ? latest.weekday[0].toUpperCase() + latest.weekday.slice(1) : "—";
  const tiles = [
    tile("Battery SOC", fmt(latest.battery_soc, 0), "%"),
    tile("Load", fmt(latest.load_power, 0), "W"),
    ...pvTiles(latest),
    tile("Consumption today", fmt(latest.today_consumption), unit("today_consumption", "kWh")),
    tile("Solar today", fmt(latest.today_forecast), solarSuffix("today_forecast")),
    tile("Solar tomorrow", fmt(latest.tomorrow_forecast), solarSuffix("tomorrow_forecast")),
  ];
  if (latest.pv_today !== null && latest.pv_today !== undefined) tiles.push(tile("PV today", fmt(latest.pv_today), "kWh"));
  for (const a of appliancesList()) tiles.push(tile(a.name, fmt(latest.appliances?.[a.id], 0), "W"));
  if (latest.outdoor_temp !== null && latest.outdoor_temp !== undefined) tiles.push(tile("Outside", fmt(latest.outdoor_temp), status.weather?.unit || "°"));
  const tomorrowWeather = weatherText(status.weather);
  if (tomorrowWeather) tiles.push(tile("Tomorrow", tomorrowWeather, status.weather.tomorrow.condition || ""));
  if (status.tariff) {
    const t = status.tariff;
    tiles.push(tile("Tariff now", t.now || "—", `${t.now_price} ${t.currency}/kWh`));
  }
  tiles.push(
    ...outageTiles(latest, status.outage_minutes || {}),
    ...(status.grid?.entity ? [tile("Grid", status.grid.up === false ? "⚠ Off" : status.grid.up ? "On" : "—", status.grid.up === false ? "outage now" : "")] : []),
    tile("Day", weekday, latest.is_weekend ? "weekend" : latest.weekday ? "weekday" : ""),
    tile("Last reading", latest.ts ? fmtTime(latest.ts) : "—"),
  );
  $("tiles").replaceChildren(...tiles);

  renderPrograms(latest.deye_programs || []);

  $("analyze").disabled = status.analysis_running;
  $("analyze").textContent = status.analysis_running ? "Predicting…" : "Predict now";
  renderControl();
}

// "Probable outages" when that sensor is set; "Emergency outages" (on/off) when only the
// emergency sensor is; the next scheduled outage under either.
function outageTiles(latest, o) {
  const tiles = [];
  // Probable outages: on while Minutes to outage shows a scheduled outage (below 9999).
  if (o.entity) tiles.push(tile("Probable outages", o.minutes !== null && o.minutes !== undefined ? "On" : "Off", outageInText(o)));
  if (o.emergency_entity) tiles.push(tile("Emergency outages", o.emergency ? "🚨 On" : "Off"));
  if (!tiles.length) tiles.push(tile("Probable outages", "—", "set the outage sensors in Settings"));
  return tiles;
}

function fmtDuration(minutes) {
  const m = Math.round(minutes);
  return m >= 60 ? `${Math.floor(m / 60)} h ${pad2(m % 60)} min` : `${m} min`;
}

// "next in 1 h 25 min (14:30)" from the minutes-to-outage sensor.
function outageInText(o) {
  if (!o?.entity) return "";
  if (o.minutes === null || o.minutes === undefined) return "none scheduled";
  if (o.minutes <= 0) return "outage now";
  const at = new Date((o.ts + o.minutes * 60) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `next in ${fmtDuration(o.minutes)} (${at})`;
}

// Battery bank name in the header: click, type, Enter (Esc cancels).
function renderBankName() {
  if (!$("bankNameInput").hidden) return; // being edited
  const name = status?.battery_name || "";
  $("bankNameText").textContent = name || "Name your battery";
  $("bankNameText").classList.toggle("placeholder", !name);
  document.title = name ? `BatteryAI · ${name}` : "BatteryAI";
}
function editBankName() {
  if (!$("bankNameInput").hidden) return;
  $("bankNameInput").value = status?.battery_name || "";
  $("bankNameText").hidden = true;
  $("bankNameInput").hidden = false;
  $("bankNameInput").focus();
  $("bankNameInput").select();
}
async function saveBankName(save) {
  const input = $("bankNameInput");
  if (input.hidden) return;
  input.hidden = true;
  $("bankNameText").hidden = false;
  if (save && status && input.value.trim() !== (status.battery_name || "")) {
    const res = await api("api/battery_name", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: input.value }) })
      .catch((err) => ({ error: err.message }));
    if (res.error) alert(res.error);
    else {
      status.battery_name = res.name;
      // Keep an open Settings form in step, so saving it does not bring the old name back.
      const field = document.querySelector('#settingsForm [name="battery_name"]');
      if (field) field.value = res.name;
    }
  }
  renderBankName();
}
$("bankName").addEventListener("click", editBankName);
$("bankNameInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveBankName(true);
  if (e.key === "Escape") saveBankName(false);
});
$("bankNameInput").addEventListener("blur", () => saveBankName(true));

const MODE_TEXT = {
  off: "Advice only",
  auto: "AI auto-control",
  charge_all: "Charging all",
};

function renderControl() {
  const c = status.control;
  $("modeBadge").textContent = MODE_TEXT[c.mode];
  $("modeBadge").className = `badge ${c.mode}`;
  $("autoToggle").checked = c.mode === "auto";
  $("autoToggle").disabled = !c.can_write;
  $("chargeAll").textContent = c.mode === "charge_all" ? `Charging all to ${c.charge_all_soc}% — set again` : `Charge all to ${c.charge_all_soc}%`;
  $("chargeAll").disabled = !c.can_write;
  const o = status.outage_minutes || {};
  $("prechargeToggle").checked = c.precharge_enabled;
  $("prechargeToggle").disabled = !c.can_write || !o.entity;
  $("prechargeLabel").replaceChildren(
    "Charge before scheduled outages ",
    el("span", { class: "hint" }, `— set every program to ${c.precharge_smart && c.precharge_smart_applies ? "up to " : ""}${c.precharge_soc}% with grid charge on ${fmtDuration(c.precharge_minutes)} before an outage`),
  );
  $("keepGridCharge").checked = c.keep_grid_charge;
  $("keepGridCharge").disabled = !c.can_write;
  $("emergencyToggle").checked = c.emergency_enabled;
  $("emergencyToggle").disabled = !c.can_write || !o.emergency_entity;
  $("emergencyLabel").replaceChildren(
    "Charge on emergency outages ",
    el("span", { class: "hint" }, o.emergency_entity
      ? `— while ${o.emergency_entity} is on, every program is held at ${c.precharge_soc}% with grid charge on; the outage schedule and AI predictions are ignored until it turns off`
      : "— set the “Emergency outages” sensor in Settings"),
  );
  $("prechargeSmart").checked = c.precharge_smart;
  $("prechargeSmart").disabled = !c.precharge_enabled || !c.can_write || !o.entity;
  $("prechargeSmart").closest("label").hidden = !c.precharge_smart_applies;
  const at = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("prechargeInfo").textContent = c.precharge?.emergency
    ? `🚨 Emergency outages: charging to ${c.precharge.soc}%; the outage schedule and AI predictions are ignored until they end, then back to ${MODE_TEXT[c.precharge.previous_mode] || c.precharge.previous_mode}.`
    : o.emergency && c.emergency_enabled && c.mode === "charge_all"
      ? "🚨 Emergency outages are on (Charge all is already active)."
      : o.emergency && c.emergency_enabled
        ? "🚨 Emergency outages are on: the emergency charge starts within a minute."
      : !o.entity
    ? "Set the “Minutes to outage” sensor in Settings to charge before outages."
    : c.precharge
      ? `⚡ Charging to ${c.precharge.soc}% for the outage at ${at(c.precharge.outage_at)}; afterwards back to ${MODE_TEXT[c.precharge.previous_mode] || c.precharge.previous_mode}.`
      : o.minutes === null || o.minutes === undefined
        ? "No outage scheduled."
        : o.minutes <= 0 ? "Outage now." : `Next outage ${outageInText(o).replace(/^next /, "")}${o.duration ? ` for ${fmtDuration(o.duration)}` : ""}.`
          + (o.emergency && c.precharge_smart
            ? ` 🚨 Emergency outages: tariffs ignored, charging to ${c.precharge_soc}% before the outage.`
            : c.precharge_smart && c.outage_plan ? ` ${c.outage_plan.charge ? "⚡ Will charge: " : "✓ "}${c.outage_plan.reason}` : "");
  const since = c.since ? ` since ${fmtTime(c.since)}` : "";
  $("controlInfo").textContent = !c.can_write
    ? "Configure the Deye program SOC entities in Settings to control the inverter."
    : c.mode === "auto"
      ? `Every prediction's SOC and grid charge are written to the inverter${since}.`
      : c.mode === "charge_all"
        ? `All programs are held at ${c.charge_all_soc}%${since}. Turn on AI auto-control to hand control back to the predictions.`
        : "Predictions only advise; nothing is written to the inverter. Use Apply on a prediction to write it once.";
}

// Deye programs in the Battery control card: SOC and grid charge can be changed here.
function renderPrograms(programs) {
  const table = $("programs");
  // Don't throw away a value the user is typing.
  if (table.querySelector("input.dirty, input:focus")) return;
  const spans = programSpans(programs);
  const configured = status.programs_configured || {};
  const hasCharge = Object.values(configured).some((p) => p.charge) || programs.some((p) => p.grid_charge !== undefined);
  if (!programs.length) {
    table.replaceChildren(el("tr", {}, el("td", { class: "empty" }, "No Deye program data yet. Configure the programs in Settings.")));
    return;
  }
  table.replaceChildren(
    el("tr", {}, el("th", {}, "Program"), el("th", {}, "Time range"), el("th", {}, "SOC"), hasCharge ? el("th", {}, "Grid charge") : null),
    ...programs.map((p) => {
      const conf = configured[p.slot] || {};
      const input = el("input", { type: "number", min: "0", max: "100", step: "1", "aria-label": `Program ${p.slot} SOC` });
      input.value = p.soc === null || p.soc === undefined ? "" : String(Math.round(p.soc));
      const setButton = el("button", { type: "button", class: "secondary" }, "Set");
      setButton.disabled = true;
      input.disabled = !conf.soc;
      input.addEventListener("input", () => {
        const changed = input.value !== "" && Number(input.value) !== Math.round(p.soc ?? -1);
        input.classList.toggle("dirty", changed);
        setButton.disabled = !changed;
      });
      const save = () => {
        if (setButton.disabled) return;
        input.classList.remove("dirty");
        setProgram(p.slot, { soc: Number(input.value) }, [input, setButton]);
      };
      setButton.addEventListener("click", save);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });

      let chargeCell = null;
      if (hasCharge) {
        if (conf.charge) {
          const box = el("input", { type: "checkbox", "aria-label": `Program ${p.slot} grid charge` });
          box.checked = p.grid_charge === "on";
          box.addEventListener("change", () => setProgram(p.slot, { grid_charge: box.checked }, [box]));
          chargeCell = el("td", {}, el("label", { class: "toggle", title: "Grid charge" }, box, el("span")));
        } else {
          chargeCell = el("td", { class: "muted" }, "—");
        }
      }
      return el("tr", { class: p.slot === status.active_program_slot ? "active" : "" },
        el("td", {}, `#${p.slot}`),
        el("td", { class: "nowrap" }, rangeText(spans[p.slot])),
        el("td", {}, el("span", { class: "soc-cell" }, input, "%", setButton)),
        chargeCell);
    }),
  );
}

async function setProgram(slot, body, controls) {
  controls.forEach((c) => (c.disabled = true));
  await controlAction(`api/programs/${slot}`, body);
}

function actionsList(actions) {
  if (!actions?.length) return null;
  return el("ul", { class: "actions-list" }, ...actions.map((a) =>
    el("li", { class: a.status === "error" ? "error" : "" },
      a.slot ? `Program ${a.slot}: ` : "",
      a.kind === "grid_charge" ? "grid charge " : "",
      a.status === "set" ? `${a.entity_id} ${a.from ?? ""} → ${a.to}` :
        a.status === "unchanged" ? (a.kind === "grid_charge" ? `${a.entity_id} already ${a.value}` : `${a.entity_id} kept at ${fmt(a.value, 0)} (suggested ${fmt(a.suggested, 0)}, below threshold)`) :
          `${a.entity_id}: ${a.error}`)));
}

async function controlAction(path, body) {
  const result = $("controlResult");
  result.className = "result";
  result.textContent = "Working…";
  const res = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .catch((err) => ({ error: err.message }));
  if (res.error) {
    result.className = "result error";
    result.textContent = `✕ ${res.error}`;
  } else {
    const failed = (res.actions || []).filter((a) => a.status === "error").length;
    result.className = `result ${failed ? "warn" : "ok"}`;
    result.replaceChildren(failed ? `⚠ ${failed} change(s) failed` : "✓ Done", actionsList(res.actions) || "");
  }
  refresh();
}

$("autoToggle").addEventListener("change", (e) => controlAction("api/control", { mode: e.target.checked ? "auto" : "off" }));
$("keepGridCharge").addEventListener("change", (e) => controlAction("api/control/keep_grid_charge", { keep: e.target.checked }));
$("emergencyToggle").addEventListener("change", (e) => controlAction("api/control/precharge", { emergency: e.target.checked }));
$("prechargeSmart").addEventListener("change", (e) => controlAction("api/control/precharge", { smart: e.target.checked }));
$("prechargeToggle").addEventListener("change", (e) => controlAction("api/control/precharge", { enabled: e.target.checked }));
$("chargeAll").addEventListener("click", () => {
  const soc = status?.control?.charge_all_soc ?? 98;
  if (confirm(`Set all Deye programs to ${soc}% and turn off AI auto-control?`)) controlAction("api/control/charge_all");
});

// Latest prediction -------------------------------------------------------------

// Appliances come from Settings (custom names); predictions refer to them by id.
const LEGACY_APPLIANCE_NAMES = { heat_pump: "Heat pump", boiler: "Boiler", ev: "EV charger" };
const APPLIANCE_COLORS = ["--series-heatpump", "--series-boiler", "--series-ev", "--series-app4", "--series-app5", "--series-app6"];
const appliancesList = () => (Array.isArray(status?.appliances) ? status.appliances : []);
const applianceColor = (i) => css(APPLIANCE_COLORS[i % APPLIANCE_COLORS.length]);
const applianceName = (id) => appliancesList().find((a) => a.id === id)?.name || LEGACY_APPLIANCE_NAMES[id] || id;

function predictionChips(r) {
  return el(
    "div",
    { class: "chips" },
    el("span", { class: "chip" }, "Rest of today ", el("b", {}, `${fmt(r.predicted_consumption_rest_of_today_kwh)} kWh`)),
    el("span", { class: "chip" }, `${planDay(r, true)} `, el("b", {}, `${fmt(r.predicted_consumption_tomorrow_kwh)} kWh`)),
    r.predicted_pv_tomorrow_kwh !== undefined ? el("span", { class: "chip" }, `PV ${planDay(r)} `, el("b", {}, `${fmt(r.predicted_pv_tomorrow_kwh)} kWh`)) : null,
    el("span", { class: "chip" }, "Min SOC ", el("b", {}, `${fmt(r.predicted_min_soc_percent, 0)} %`)),
    r.estimated_grid_cost_tomorrow !== undefined
      ? el("span", { class: "chip" }, `Grid cost ${planDay(r)} `, el("b", {}, `${fmt(r.estimated_grid_cost_tomorrow, 2)} ${status?.tariff?.currency || ""}`))
      : null,
    el("span", { class: "chip" }, "Outage risk ", el("b", {}, r.outage_risk)),
    el("span", { class: "chip" }, "Confidence ", el("b", {}, r.confidence)),
  );
}

function renderPrediction(analysis) {
  latestPrediction = analysis;
  const box = $("prediction");
  if (!analysis) {
    $("predictionTime").textContent = "";
    box.replaceChildren(el("div", { class: "empty" }, "No prediction yet. Press “Predict now”."));
    return;
  }
  const r = analysis.result;
  $("predictionTime").textContent = fmtDateTime(analysis.ts);
  const applyButton = el("button", { type: "button", class: "secondary" }, "Apply to inverter");
  applyButton.disabled = !status?.control?.can_write;
  applyButton.addEventListener("click", () => {
    if (confirm("Write this prediction's SOC values to the Deye programs now?")) controlAction(`api/analyses/${analysis.id}/apply`);
  });
  box.replaceChildren(
    el("div", {}, r.summary),
    predictionChips(r),
    r.weather_impact ? el("p", { class: "muted prediction-meta" }, "Weather: ", r.weather_impact) : null,
    r.appliance_forecast?.length
      ? el("ul", { class: "appliances" }, ...r.appliance_forecast.map((a) =>
        el("li", {}, el("b", {}, applianceName(a.appliance)), ` ${fmt(a.expected_kwh_tomorrow)} kWh ${planDay(r)} · ${a.expected_usage_windows}`)))
      : null,
    r.hourly_forecast_tomorrow?.length ? el("div", { class: "chart small" }, el("canvas", { id: "forecastChart" })) : null,
    programTable(r) ? el("details", {}, el("summary", {}, "Suggested Deye programs"), programTable(r)) : null,
    el("div", { class: "control-buttons" }, applyButton),
  );
  if (r.hourly_forecast_tomorrow?.length) {
    delete charts.forecastChart;
    const hours = r.hourly_forecast_tomorrow.slice().sort((a, b) => a.hour - b.hour);
    const series = [["Load", "load_w", "--series-load"]];
    appliancesList().forEach((a, i) => {
      if (hours.some((h) => h[`${a.id}_w`] !== undefined)) series.push([a.name, `${a.id}_w`, APPLIANCE_COLORS[i % APPLIANCE_COLORS.length]]);
    });
    const text = css("--muted");
    charts.forecastChart = typeof Chart === "undefined" ? undefined : new Chart($("forecastChart"), {
      type: "line",
      data: {
        labels: hours.map((h) => `${String(h.hour).padStart(2, "0")}:00`),
        datasets: series.map(([label, key, color], i) => ({
          label, data: hours.map((h) => h[key]), borderColor: css(color), backgroundColor: css(color) + "22",
          borderWidth: 2, pointRadius: 0, fill: i === 0 ? "origin" : false,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { color: text, boxWidth: 10 } }, title: { display: true, text: `Predicted power ${planDay(r)} (W)`, color: text } },
        scales: { x: { ticks: { color: text, maxTicksLimit: 8 }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: text }, grid: { color: css("--grid") } } },
      },
    });
  }
}

// The day a prediction is for: "today" (morning run) or "tomorrow".
const planDay = (r, capital = false) => {
  const day = r?.plan_day || "tomorrow";
  return capital ? day[0].toUpperCase() + day.slice(1) : day;
};

function programTable(r) {
  if (!(r.deye_programs || []).length) return null;
  const charge = (v) => (v === true ? "⚡ on" : v === false ? "off" : "—");
  const spans = programSpans(r.deye_programs);
  return el(
    "div",
    { class: "table-scroll" },
    el(
      "table",
      { class: "programs" },
      el("tr", {}, el("th", {}, "Program"), el("th", {}, "Time range"), el("th", {}, "SOC"), el("th", {}, "Grid charge"), el("th", {}, "Why")),
      ...r.deye_programs.map((p) => el("tr", {}, el("td", { class: "nowrap" }, `#${p.slot}`), el("td", { class: "nowrap" }, rangeText(spans[p.slot])), el("td", { class: "nowrap" }, `${fmt(p.soc_percent, 0)} %`), el("td", { class: "nowrap" }, charge(p.grid_charge)), el("td", {}, p.reason))),
    ),
  );
}

// Economy tab -------------------------------------------------------------------

async function refreshEconomy() {
  const days = $("economyRange").value;
  let report;
  try {
    [report, status] = await Promise.all([api(`api/economy?days=${days}`), status ? Promise.resolve(status) : api("api/status")]);
  } catch (err) {
    $("economyNotes").replaceChildren(el("div", { class: "banner" }, `Could not load data: ${err.message}`));
    return;
  }
  const cur = report.currency;
  const money = (v) => (v === null || v === undefined ? "—" : `${fmt(v, 2)} ${cur}`);
  const t = report.totals;
  const notes = [];
  if (!report.has_grid_sensor) notes.push("Set a “Grid import today” sensor in Settings to calculate what was paid and the savings.");
  if (!report.has_pv_power_sensor) notes.push("Set a “PV power” sensor in Settings to split savings into PV and battery.");
  $("economyNotes").replaceChildren(...notes.map((n) => el("div", { class: "banner" }, n)));
  $("economyInfo").textContent = status?.tariff
    ? status.tariff.tariffs.length === 1
      ? `${status.tariff.tariffs[0].name} ${status.tariff.tariffs[0].price_per_kwh} ${cur}/kWh (all day)`
      : status.tariff.tariffs.map((x) => `${x.name} ${x.price_per_kwh} ${cur}/kWh (${Array.isArray(x.windows) ? x.windows.join(", ") : x.windows})`).join(" · ")
    : "";
  const tariffNames = (status?.tariff?.tariffs || []).map((x) => x.name);
  const cheapest = status?.tariff?.cheapest;
  $("economyTiles").replaceChildren(
    tile("Saved", money(t.total_saved), t.saved_percent !== null ? `${fmt(t.saved_percent, 0)} %` : ""),
    tile("Saved by PV", money(t.pv_saved)),
    tile("Saved by battery & AI plan", money(t.battery_saved)),
    tile("Paid for grid", money(t.paid)),
    tile("Without PV & battery", money(t.without_system)),
    tile("Grid energy", t.grid_kwh === null ? "—" : fmt(t.grid_kwh),
      t.grid_kwh === null ? "" : status?.tariff?.single_price ? "kWh" : `kWh (${fmt(t.grid_by_tariff?.[cheapest] ?? 0)} at ${cheapest})`),
    tile("Consumption", fmt(t.load_kwh), "kWh"),
  );

  const rows = report.days;
  const col = (key) => rows.map((d) => d[key]);
  const options = categoryOptions(rows, cur, null);
  options.scales.x.stacked = true;
  options.scales.y.stacked = true;
  upsertChart("economyChart", {
    data: {
      labels: rows.map((d) => shortDate(d.date)),
      datasets: [
        bar("Paid", css("--series-load"), col("paid"), { stack: "cost" }),
        bar("Saved by PV", css("--series-pv"), col("pv_saved"), { stack: "cost" }),
        bar("Saved by battery & AI", css("--series-soc"), rows.map((d) => (d.battery_saved === null ? null : Math.max(0, d.battery_saved))), { stack: "cost" }),
      ],
    },
    options,
    plugins: [weekendShading],
  }, NO_READINGS);

  const header = ["Day", "Consumption", ...tariffNames.map((n) => (tariffNames.length === 1 ? "Grid" : `Grid ${n}`)), "Paid", "Without system", "PV saved", "Battery/AI saved", "Saved", "AI control"];
  $("economyTable").replaceChildren(
    el("tr", {}, ...header.map((h, i) => el("th", { class: i ? "num" : "" }, h))),
    ...rows.slice().reverse().map((d) =>
      el("tr", {},
        el("td", {}, shortDate(d.date)),
        el("td", { class: "num" }, `${fmt(d.load_kwh)} kWh`),
        ...tariffNames.map((n) => el("td", { class: "num" }, d.grid_kwh === null ? "—" : `${fmt(d.grid_by_tariff?.[n] ?? 0)} kWh`)),
        el("td", { class: "num" }, money(d.paid)),
        el("td", { class: "num" }, money(d.without_system)),
        el("td", { class: "num" }, money(d.pv_saved)),
        el("td", { class: "num" }, money(d.battery_saved)),
        el("td", { class: d.total_saved < 0 ? "num saved loss" : "num saved" }, d.total_saved === null ? "—" : `${money(d.total_saved)} (${fmt(d.saved_percent, 0)} %)`),
        el("td", { class: "num" }, `${d.ai_control_share} %`),
      )),
  );
}
$("economyRange").addEventListener("change", refreshEconomy);

async function refreshMonths() {
  let data;
  try {
    data = await api("api/monthly");
  } catch (err) {
    return;
  }
  const months = data.months;
  const cur = data.currency;
  const money = (v) => (v === null || v === undefined ? "—" : `${fmt(v, 0)} ${cur}`);
  const label = (m) => new Date(`${m}-15T12:00:00`).toLocaleDateString([], { month: "short", year: "numeric" });
  const text = css("--muted");
  const grid = css("--grid");
  upsertChart("monthChart", {
    data: {
      labels: months.map((m) => label(m.month)),
      datasets: [
        bar("Consumption / day", css("--series-consumption"), months.map((m) => m.avg_daily_kwh)),
        bar("PV / day", css("--series-pv"), months.map((m) => m.avg_daily_pv_kwh)),
        { type: "line", label: "Avg temp", data: months.map((m) => m.temp_avg), yAxisID: "y2", borderColor: css("--series-temp"), backgroundColor: css("--series-temp"), pointRadius: 3, borderWidth: 1.5 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: text, boxWidth: 12 } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: text } },
        y: { beginAtZero: true, grid: { color: grid }, ticks: { color: text }, title: { display: true, text: "kWh per day", color: text } },
        y2: { position: "right", grid: { display: false }, ticks: { color: text }, title: { display: true, text: "°", color: text } },
      },
    },
  }, "No data yet.");
  const header = ["Month", "Days", "Use / day", "Weekday", "Weekend", "PV / day", "Grid", "Temp", "Paid", "Saved"];
  $("monthTable").replaceChildren(
    el("tr", {}, ...header.map((h, i) => el("th", { class: i ? "num" : "" }, h))),
    ...months.slice().reverse().map((m) =>
      el("tr", {},
        el("td", { class: "nowrap" }, label(m.month)),
        el("td", { class: "num" }, m.days),
        el("td", { class: "num" }, m.avg_daily_kwh === null ? "—" : `${fmt(m.avg_daily_kwh)} kWh`),
        el("td", { class: "num" }, m.avg_weekday_kwh === null ? "—" : `${fmt(m.avg_weekday_kwh)} kWh`),
        el("td", { class: "num" }, m.avg_weekend_kwh === null ? "—" : `${fmt(m.avg_weekend_kwh)} kWh`),
        el("td", { class: "num" }, m.avg_daily_pv_kwh === null ? "—" : `${fmt(m.avg_daily_pv_kwh)} kWh`),
        el("td", { class: "num" }, `${fmt(m.grid_import_kwh, 0)} kWh`),
        el("td", { class: "num" }, m.temp_avg === null ? "—" : `${fmt(m.temp_avg)}°`),
        el("td", { class: "num" }, money(m.paid)),
        el("td", { class: m.total_saved < 0 ? "num saved loss" : "num saved" }, m.total_saved === null ? "—" : `${money(m.total_saved)} (${fmt(m.saved_percent, 0)} %)`),
      )),
  );
}

// Monthly bill (Economy sub-tab) ------------------------------------------------

const openBillMonths = new Set();

async function refreshBill() {
  const year = $("billYear").value;
  let data;
  try {
    data = await api(`api/bill${year ? `?year=${year}` : ""}`);
  } catch (err) {
    $("billNotes").replaceChildren(el("div", { class: "banner" }, `Could not load data: ${err.message}`));
    return;
  }
  const cur = data.currency;
  const money = (v) => (v === null || v === undefined ? "—" : `${fmt(v, 2)} ${cur}`);
  const kwh = (v) => (v === null || v === undefined ? "—" : `${fmt(v, 1)} kWh`);
  const monthLabel = (m) => new Date(`${m}-15T12:00:00`).toLocaleDateString([], { month: "long", year: "numeric" });

  $("billYear").replaceChildren(...data.years.map((y) => el("option", y === data.year ? { value: y, selected: "" } : { value: y }, y)));
  const notes = [];
  if (!data.sensor) notes.push("Set a “Grid energy meter” (e.g. the Shelly EM total energy) or “Grid import today” sensor in Settings to record the monthly bill.");
  else if (data.backfilling) notes.push("Reading this month's history of the meter from Home Assistant…");
  else if (!data.meter) notes.push(`Waiting for the first value of ${data.sensor}.`);
  // Months recorded under tariffs that are no longer in Settings (e.g. the defaults before
  // the tariffs were set): point at "Use current prices".
  const currentTariffs = new Set(data.current_tariffs || []);
  const outdated = (m) => Object.keys(m.by_tariff).filter((n) => !currentTariffs.has(n));
  const stale = data.months.filter((m) => outdated(m).length);
  if (stale.length) {
    const names = [...new Set(stale.flatMap(outdated))].join(", ");
    notes.push(`${stale.map((m) => monthLabel(m.key)).join(", ")}: recorded with tariffs that are no longer in Settings (${names}). Press “Use current prices” on a month to switch it to ${[...currentTariffs].join(", ")}.`);
  }
  $("billNotes").replaceChildren(...notes.map((n) => el("div", { class: "banner" }, n)));
  $("billInfo").textContent = data.sensor ? `Meter: ${data.sensor}` : "";

  // Per-tariff columns plus the overall total when energy was recorded under more than one
  // tariff (peak / off-peak); with a single tariff its name labels the total columns.
  const names = data.tariffs.length > 1 ? data.tariffs : [];
  const single = data.tariffs.length === 1 ? data.tariffs[0] : null;
  const now = new Date();
  const current = data.months.find((m) => m.key === `${now.getFullYear()}-${pad2(now.getMonth() + 1)}`);
  $("billTiles").replaceChildren(
    tile("This month", money(current?.cost ?? (data.year === now.getFullYear() ? 0 : null)), current ? `${fmt(current.kwh, 1)} kWh` : ""),
    tile(`Year ${data.year}`, money(data.total.cost), `${fmt(data.total.kwh, 1)} kWh`),
    ...names.map((n) => tile(`${n} ${data.year}`, money(data.total.by_tariff[n]?.cost ?? 0), `${fmt(data.total.by_tariff[n]?.kwh ?? 0, 1)} kWh`)),
  );

  // Each tariff: kWh, the month's price (editable on month rows) and cost; with several
  // tariffs also the overall kWh and cost.
  const priced = single ? [single] : names;
  const priceInput = (m, n) => {
    const input = el("input", { type: "number", min: "0", step: "0.0001", class: "price-input", "aria-label": `${n} price ${m.key}`, title: `${n} price per kWh in ${monthLabel(m.key)}` });
    input.value = m.prices?.[n] ?? "";
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("change", async () => {
      const res = await api("api/bill/price", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ month: m.key, tariff: n, price: input.value }) })
        .catch((err) => ({ error: err.message }));
      if (res.error) alert(res.error);
      refreshBill();
    });
    return input;
  };
  const cells = (item, month = null) => [
    ...priced.flatMap((n) => {
      const part = item.by_tariff[n];
      return [
        el("td", { class: "num" }, part ? kwh(part.kwh) : "—"),
        el("td", { class: "num" }, month && part ? priceInput(month, n) : ""),
        el("td", { class: "num" }, part ? money(part.cost) : "—"),
      ];
    }),
    ...(single ? [] : [el("td", { class: "num" }, kwh(item.kwh)), el("td", { class: "num" }, money(item.cost))]),
  ];
  const header = ["Month", ...priced.flatMap((n) => [`${n} kWh`, `${n} price, ${cur}/kWh`, `${n} cost`]), ...(single ? [] : ["Overall kWh", "Overall grid cost"]), ""];
  const rows = [];
  for (const m of data.months) {
    const open = openBillMonths.has(m.key);
    const reprice = el("button", { type: "button", class: outdated(m).length ? "small" : "secondary small", title: "Set this month's prices to the current tariffs in Settings" }, "Use current prices");
    reprice.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Set the prices of ${monthLabel(m.key)} to the current tariffs in Settings and recalculate its costs?`)) return;
      const res = await api("api/bill/reprice", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ month: m.key }) })
        .catch((err) => ({ error: err.message }));
      if (res.error) alert(res.error);
      refreshBill();
    });
    const row = el("tr", { class: open ? "month open" : "month", title: "Show the days" }, el("td", { class: "nowrap" }, monthLabel(m.key)), ...cells(m, m), el("td", { class: "num" }, reprice));
    row.addEventListener("click", () => {
      if (openBillMonths.has(m.key)) openBillMonths.delete(m.key);
      else openBillMonths.add(m.key);
      refreshBill();
    });
    rows.push(row);
    if (open) rows.push(...m.days.map((d) => el("tr", { class: "day" }, el("td", { class: "nowrap" }, shortDate(d.key)), ...cells(d), el("td"))));
  }
  $("billTable").replaceChildren(
    el("tr", {}, ...header.map((h, i) => el("th", { class: i ? "num" : "" }, h))),
    ...(rows.length ? rows : [el("tr", {}, el("td", { class: "empty", colspan: header.length }, "Nothing recorded for this year yet."))]),
    el("tr", { class: "total" }, el("td", {}, `Year ${data.year} total`), ...cells(data.total), el("td")),
  );
  renderBillAppliances(data, money, kwh, monthLabel);
}

// Appliances in the Monthly bill: one row per appliance and month, with the year's total.
function renderBillAppliances(data, money, kwh, monthLabel) {
  let tariffs;
  const months = data.appliance_months || [];
  $("billAppliancesBox").hidden = !months.length;
  if (!months.length) return;
  // Only the tariffs the appliances used, in the bill's order; one tariff = just the totals.
  const used = new Set(months.flatMap((m) => m.appliances.flatMap((a) => Object.keys(a.by_tariff))));
  tariffs = data.tariffs.filter((t) => used.has(t));
  if (tariffs.length < 2) tariffs = [];
  const monthCost = Object.fromEntries(data.months.map((m) => [m.key, m.cost]));
  const share = (cost, total) => (total ? `${fmt((cost / total) * 100, 0)} %` : "—");
  const row = (label, a, total, cls = "") =>
    el("tr", cls ? { class: cls } : {},
      el("td", { class: "nowrap" }, label),
      el("td", {}, a.name, a.removed ? el("span", { class: "hint" }, " (removed)") : ""),
      ...tariffs.flatMap((t) => {
        const part = a.by_tariff[t];
        return [el("td", { class: "num" }, part ? kwh(part.kwh) : "—"), el("td", { class: "num" }, part ? money(part.cost) : "—")];
      }),
      el("td", { class: "num" }, kwh(a.kwh)),
      el("td", { class: "num" }, money(a.cost)),
      el("td", { class: "num" }, share(a.cost, total)),
    );
  const header = ["Month", "Appliance", ...tariffs.flatMap((t) => [`${t} kWh`, `${t} cost`]), "kWh", "Cost", "Share of bill"];
  const rows = months.flatMap((m) => m.appliances.map((a, i) => row(i ? "" : monthLabel(m.key), a, monthCost[m.key], i ? "" : "group-start")));
  const totals = (data.appliance_total || []).map((a, i) => row(i ? "" : `Year ${data.year}`, a, data.total.cost, i ? "total-more" : "total"));
  $("billAppliances").replaceChildren(
    el("tr", {}, ...header.map((h, i) => el("th", { class: i > 1 ? "num" : "" }, h))),
    ...rows,
    ...totals,
  );
}
$("billYear").addEventListener("change", refreshBill);

// Logs tab ----------------------------------------------------------------------

let logTimer = null;
let logLines = [];
let lastLogId = 0;

const fmtBytes = (n) => {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
};

async function refreshStorage() {
  let s;
  try {
    s = await api("api/storage");
  } catch (err) {
    $("storageInfo").textContent = `Could not load storage info: ${err.message}`;
    return;
  }
  const db = s.database;
  $("storageInfo").textContent = s.data_dir;
  $("storageTiles").replaceChildren(
    tile("Database", fmtBytes(db.file_bytes), db.free_bytes ? `${fmtBytes(db.free_bytes)} reusable` : ""),
    tile("Readings", db.rows.readings.toLocaleString(), ""),
    tile("Predictions", db.rows.analyses.toLocaleString(), ""),
    tile("Data from", db.first_reading ? new Date(db.first_reading * 1000).toLocaleDateString([], { day: "numeric", month: "short" }) : "—",
      db.detail_since ? `detail since ${new Date(db.detail_since * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}` : ""),
    tile("Add-on total", s.addon ? fmtBytes(s.addon.total) : "…", s.addon ? "data + program" : "measuring"),
    tile("Add-on data", fmtBytes(s.data_bytes), `${s.data_files} files`),
    tile("Add-on program", s.addon ? fmtBytes(s.addon.image) : "…", "code, Python, system"),
    tile("Disk free", fmtBytes(s.disk.free), `of ${fmtBytes(s.disk.total)}`),
  );
  $("dbTable").replaceChildren(
    el("tr", {}, el("th", {}, "Database"), el("th", { class: "num" }, "")),
    el("tr", {}, el("td", {}, "Readings (recorded live)"), el("td", { class: "num" }, (db.reading_sources.live || 0).toLocaleString())),
    el("tr", {}, el("td", {}, "Readings (imported from Home Assistant)"), el("td", { class: "num" }, (db.reading_sources.history || 0).toLocaleString())),
    el("tr", {}, el("td", {}, "Readings (compressed to hourly)"), el("td", { class: "num" }, (db.reading_sources.hourly || 0).toLocaleString())),
    el("tr", {}, el("td", {}, "Predictions"), el("td", { class: "num" }, db.rows.analyses.toLocaleString())),
    el("tr", {}, el("td", {}, "First / last reading"), el("td", { class: "num" }, db.first_reading ? `${fmtDateTime(db.first_reading)} – ${fmtDateTime(db.last_reading)}` : "—")),
    ...Object.entries(db.files).map(([suffix, bytes]) => el("tr", {}, el("td", {}, `batteryai.db${suffix}`), el("td", { class: "num" }, fmtBytes(bytes)))),
  );
  if (!s.addon) {
    $("usageTable").replaceChildren(el("tr", {}, el("td", { class: "empty" }, "Measuring the add-on's disk space… (refresh in a moment)")));
    setTimeout(() => { if (location.hash === "#logs") refreshStorage(); }, 5000);
  }
  const u = s.addon?.parts;
  const usageRow = (label, bytes, cls = "") => el("tr", cls ? { class: cls } : {}, el("td", {}, label), el("td", { class: "num" }, fmtBytes(bytes)));
  if (u) $("usageTable").replaceChildren(
    el("tr", {}, el("th", {}, "Space used by the add-on"), el("th", { class: "num" }, "Size")),
    usageRow("Database (readings, predictions, bill)", u.database),
    usageRow("Local LLM model", u.model),
    usageRow("Other data (settings, backups in progress, …)", u.other_data),
    usageRow("BatteryAI code", u.app),
    usageRow("Python packages (incl. the local LLM engine)", u.python),
    usageRow("System (base image)", u.system),
    usageRow("Total", s.addon.total, "total"),
  );
  $("filesTable").replaceChildren(
    el("tr", {}, el("th", {}, "File in /data"), el("th", { class: "num" }, "Size")),
    ...s.files.map((f) => el("tr", {}, el("td", {}, f.name), el("td", { class: "num" }, fmtBytes(f.bytes)))),
  );
}

function renderLogLines() {
  const query = $("logSearch").value.trim().toLowerCase();
  const view = $("logView");
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
  const shown = query ? logLines.filter((l) => `${l.logger} ${l.message}`.toLowerCase().includes(query)) : logLines;
  $("logCount").textContent = `${shown.length} lines`;
  view.replaceChildren(
    ...(shown.length
      ? shown.map((l) =>
        el("div", { class: "log-line" },
          el("span", { class: "time" }, new Date(l.ts * 1000).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })),
          el("span", { class: `lvl-${l.level}` }, l.level),
          el("span", { class: "msg" }, el("span", { class: "src" }, `${l.logger}: `), l.message)))
      : [el("div", { class: "empty" }, "No log lines.")]),
  );
  if (atBottom) view.scrollTop = view.scrollHeight;
}

async function refreshLogs(reset = false) {
  clearTimeout(logTimer);
  if (reset) { logLines = []; lastLogId = 0; }
  try {
    const fresh = await api(`api/logs?level=${$("logLevel").value}&after=${lastLogId}`);
    if (fresh.length) {
      lastLogId = fresh[fresh.length - 1].id;
      logLines = logLines.concat(fresh).slice(-1000);
    }
    renderLogLines();
  } catch (err) {
    $("logCount").textContent = `Could not load logs: ${err.message}`;
  }
  if ($("logFollow").checked && location.hash === "#logs") logTimer = setTimeout(() => refreshLogs(), 5000);
}

$("logLevel").addEventListener("change", () => refreshLogs(true));
$("logSearch").addEventListener("input", renderLogLines);
$("logFollow").addEventListener("change", () => refreshLogs());
$("compressDb").addEventListener("click", async () => {
  $("compressDb").disabled = true;
  $("vacuumResult").className = "result";
  $("vacuumResult").textContent = "Compressing…";
  const r = await api("api/storage/compress", { method: "POST" }).catch((err) => ({ error: err.message }));
  $("compressDb").disabled = false;
  $("vacuumResult").className = `result ${r.error ? "error" : "ok"}`;
  $("vacuumResult").textContent = r.error ? `✕ ${r.error}` : r.removed ? `✓ ${r.removed.toLocaleString()} readings → ${r.added.toLocaleString()} hourly rows. Use “Compact database” to give the space back to the disk.` : "✓ Nothing older than the detail period.";
  refreshStorage();
});
$("downloadBackup").addEventListener("click", () => {
  $("backupResult").className = "result";
  $("backupResult").textContent = "Preparing the backup… the download starts when it is ready.";
});
$("restoreFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!confirm(`Replace ALL data and settings with the backup “${file.name}”? This cannot be undone from the panel.`)) return;
  const result = $("backupResult");
  result.className = "result";
  result.textContent = `Uploading ${file.name} (${fmtBytes(file.size)})…`;
  const res = await api("api/backup/restore", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file })
    .catch((err) => ({ error: err.message }));
  if (res.error) {
    result.className = "result error";
    result.textContent = `✕ ${res.error}`;
    return;
  }
  const m = res.manifest;
  result.className = "result ok";
  result.textContent = `✓ Restored the backup from ${m.created} (BatteryAI ${m.version}, ${Number(m.readings).toLocaleString()} readings). Reloading…`;
  setTimeout(() => location.reload(), 2000);
});
$("vacuumDb").addEventListener("click", async () => {
  $("vacuumDb").disabled = true;
  $("vacuumResult").className = "result";
  $("vacuumResult").textContent = "Compacting…";
  const r = await api("api/storage/vacuum", { method: "POST" }).catch((err) => ({ error: err.message }));
  $("vacuumDb").disabled = false;
  $("vacuumResult").className = `result ${r.error ? "error" : "ok"}`;
  $("vacuumResult").textContent = r.error ? `✕ ${r.error}` : `✓ ${fmtBytes(r.before)} → ${fmtBytes(r.after)}`;
  refreshStorage();
});

// Analysis log ----------------------------------------------------------------

function renderLog(analyses) {
  $("logInfo").textContent = analyses.length ? `${analyses.length} most recent runs` : "";
  if (!analyses.length) {
    $("log").replaceChildren(el("div", { class: "empty" }, "No predictions yet. They run on the schedule above, or press “Predict now”."));
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
    a.actions?.some((x) => x.status === "set") ? el("span", { class: "badge auto" }, "applied") : null,
    el("span", { class: "muted" }, [a.model, duration, tokens].filter(Boolean).join(" · ")),
  );

  if (a.status === "running") return el("div", { class: "entry" }, head, el("div", { class: "muted" }, "Predicting…"));
  if (a.status === "error") return el("div", { class: "entry error" }, head, el("div", { class: "error-text" }, a.error));

  const programs = programTable(r);

  return el(
    "div",
    { class: "entry" },
    head,
    el("div", {}, r.summary),
    predictionChips(r),
    r.recommendations?.length ? el("ul", {}, ...r.recommendations.map((t) => el("li", {}, t))) : null,
    programs ? el("details", {}, el("summary", {}, "Suggested Deye programs"), programs) : null,
    a.actions?.length ? el("details", {}, el("summary", {}, "Changes written to the inverter"), actionsList(a.actions)) : null,
    el("details", {}, el("summary", {}, "Reasoning"), el("pre", {}, r.reasoning)),
    inputDetails(a.id),
  );
}

function inputDetails(id) {
  const pre = el("pre", {}, "Loading…");
  const details = el("details", {}, el("summary", {}, "Data sent to the AI"), pre);
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
    const [s, rows, days, analyses, accuracy, predicted] = await Promise.all([
      api("api/status"),
      api(`api/readings?hours=${hours}`),
      api("api/daily?days=14"),
      api("api/analyses?limit=30"),
      api("api/accuracy?days=14"),
      api(`api/predicted_load?hours=${hours}`),
    ]);
    api(`api/grid?hours=${hours}`).then((g) => renderGrid(g, Number(hours))).catch(() => {});
    status = s;
    renderStatus();
    renderBattery(rows);
    renderPower(rows, predicted);
    renderDaily(days);
    renderAccuracy(accuracy);
    const latestOk = analyses.find((a) => a.status === "ok" && a.result);
    if (latestOk?.id !== latestPrediction?.id || latestOk?.actions?.length !== latestPrediction?.actions?.length) renderPrediction(latestOk);
    renderLog(analyses);
  } catch (err) {
    $("warnings").replaceChildren(el("div", { class: "banner" }, `Could not load data: ${err.message}`));
  }
  // Poll quickly while a prediction or history import is running.
  const busy = status?.analysis_running || status?.history_import?.running;
  refreshTimer = setTimeout(refresh, busy ? 5000 : 60000);
}

$("range").addEventListener("change", refresh);
$("analyze").addEventListener("click", async () => {
  $("analyze").disabled = true;
  $("analyze").textContent = "Predicting…";
  const res = await api("api/analyze", { method: "POST" }).catch((err) => ({ error: err.message }));
  if (res.error) alert(res.error);
  setTimeout(refresh, 500);
});

// Charts read their colours from CSS variables, so rebuild them when the theme changes.
function rebuildCharts() {
  Object.values(charts).forEach((c) => c?.destroy());
  for (const key of Object.keys(charts)) delete charts[key];
  latestPrediction = null;
  refresh();
  if (location.hash === "#economy") refreshEconomy();
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rebuildCharts);

// Model selection (header and Settings) ------------------------------------------

let modelsPromise = null;
function loadModels(force = false) {
  if (!modelsPromise || force) modelsPromise = api("api/models").catch(() => ({ models: [], current: null }));
  return modelsPromise;
}

function fillModelSelect(select, models, current, short = false) {
  const label = (m) => (!m.display_name || m.display_name === m.id ? m.id : short ? m.display_name : `${m.display_name} (${m.id})`);
  select.replaceChildren(...models.map((m) => el("option", { value: m.id, title: m.id }, label(m))));
  if (current) select.value = current;
}

// Header: one dropdown for the engine and, for Claude and ChatGPT, the model.
function fillEngineSelect(data) {
  const select = $("modelSelect");
  const claude = el("optgroup", { label: "Claude (cloud)" }, ...data.models.map((m) => el("option", { value: `claude:${m.id}`, title: m.id }, m.display_name || m.id)));
  const local = el("optgroup", { label: "Local (inside the add-on)" },
    el("option", { value: "local_fast" }, "Local fast (light CPU)"),
    el("option", { value: "local_llm" }, "Local LLM (heavy CPU)"));
  const openai = el("optgroup", { label: "ChatGPT (OpenAI cloud)" }, ...(data.openai_models || []).map((m) => el("option", { value: `openai:${m.id}`, title: m.id }, m.id)));
  select.replaceChildren(claude, openai, local);
  select.value = data.engine === "claude" ? `claude:${data.current}` : data.engine === "openai" ? `openai:${data.openai_current}` : data.engine;
}

async function initModelSelect(force = false) {
  fillEngineSelect(await loadModels(force));
}

$("modelSelect").addEventListener("change", async (e) => {
  const select = e.target;
  select.disabled = true;
  const resp = await fetch("api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(select.value.startsWith("claude:")
      ? { prediction_engine: "claude", claude_model: select.value.slice(7) }
      : select.value.startsWith("openai:")
        ? { prediction_engine: "openai", openai_model: select.value.slice(7) }
        : { prediction_engine: select.value }),
  }).catch(() => null);
  select.disabled = false;
  if (!resp?.ok) alert("Could not change the prediction engine.");
  const saved = resp?.ok ? await resp.json() : null;
  if (saved) {
    const settingsEngine = document.querySelector('#settingsForm select[name="prediction_engine"]');
    const settingsModel = document.querySelector('#settingsForm select[name="claude_model"]');
    if (settingsEngine) { settingsEngine.value = saved.prediction_engine; settingsEngine.dispatchEvent(new Event("change")); }
    if (settingsModel) settingsModel.value = saved.claude_model;
    const settingsOpenai = document.querySelector('#settingsForm select[name="openai_model"]');
    if (settingsOpenai) settingsOpenai.value = saved.openai_model;
  }
  initModelSelect(true);
  refresh();
});
initModelSelect();

// Theme switch: auto (follow the system) -> light -> dark ------------------------

const THEMES = ["auto", "light", "dark"];
const THEME_LABELS = { auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark" };

function currentTheme() {
  try {
    const saved = localStorage.getItem("batteryai-theme");
    return THEMES.includes(saved) ? saved : "auto";
  } catch (e) {
    return "auto";
  }
}

function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $("themeToggle").textContent = THEME_LABELS[theme];
  $("themeToggle").title = `Theme: ${theme}. Click to switch.`;
}

$("themeToggle").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  try { localStorage.setItem("batteryai-theme", next); } catch (e) {}
  applyTheme(next);
  rebuildCharts();
});
applyTheme(currentTheme());

// Tabs ---------------------------------------------------------------------------

const OTHER_TABS = ["#settings", "#economy", "#economy-bill", "#logs"];

function showTab() {
  const tab = OTHER_TABS.includes(location.hash) ? location.hash.slice(1).split("-")[0] : "dashboard";
  const sub = location.hash === "#economy-bill" ? "bill" : "overview";
  $("view-dashboard").hidden = tab !== "dashboard";
  $("view-economy").hidden = tab !== "economy";
  $("view-settings").hidden = tab !== "settings";
  $("view-logs").hidden = tab !== "logs";
  $("dashActions").hidden = tab !== "dashboard";
  document.querySelectorAll(".tabs a[data-tab]").forEach((a) => {
    a.classList.toggle("active", a.dataset.tab === tab);
    a.setAttribute("aria-selected", a.dataset.tab === tab);
  });
  // Charts created while the dashboard was hidden have no size yet.
  if (tab === "dashboard" || tab === "economy") requestAnimationFrame(() => Object.values(charts).forEach((c) => c?.resize()));
  $("economyOverview").hidden = sub !== "overview";
  $("economyBill").hidden = sub !== "bill";
  document.querySelectorAll(".subtabs a").forEach((a) => {
    a.classList.toggle("active", a.dataset.sub === sub);
    a.setAttribute("aria-selected", a.dataset.sub === sub);
  });
  if (tab === "economy" && sub === "bill") refreshBill();
  else if (tab === "economy") {
    refreshEconomy();
    refreshMonths();
  }
  if (tab === "logs") {
    refreshStorage();
    refreshLogs(true);
  }
  window.dispatchEvent(new CustomEvent("batteryai:tab", { detail: tab }));
}
window.addEventListener("hashchange", () => {
  showTab();
  if (!OTHER_TABS.includes(location.hash)) refresh();
});

showTab();
refresh();
