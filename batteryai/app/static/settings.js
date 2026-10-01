"use strict";

// Settings tab: edit settings, test the Home Assistant / Claude connections and every entity.

const SENSOR_GROUPS = [
  {
    title: "Battery & solar",
    rows: [
      { key: "battery_soc_sensor", label: "Battery SOC", hint: "%", kind: "numeric" },
      { key: "today_forecast_sensor", label: "Today solar forecast", hint: "kWh, e.g. Solcast forecast today", kind: "numeric" },
      { key: "tomorrow_forecast_sensor", label: "Tomorrow solar forecast", hint: "kWh", kind: "numeric" },
      { key: "pv_power_sensor", label: "PV power", hint: "W, current production (shows if the battery charges)", kind: "numeric" },
      { key: "pv_energy_sensor", label: "PV production today", hint: "kWh counter (optional, for accuracy %)", kind: "numeric" },
    ],
  },
  {
    title: "Consumption",
    rows: [
      { key: "load_power_sensor", label: "Load power", hint: "W (kW is converted)", kind: "numeric" },
      { key: "today_consumption_sensor", label: "Today consumption", hint: "kWh counter, resets at midnight", kind: "numeric" },
      { key: "grid_import_sensor", label: "Grid import today", hint: "kWh counter (needed for the Economy tab)", kind: "numeric" },
    ],
  },
  {
    title: "Weather & outages",
    rows: [
      { key: "weather_entity", label: "Weather", hint: "weather.* entity (forecast) or a temperature sensor", kind: "weather" },
      { key: "outages_sensor", label: "Probable outages", hint: "optional, any state", kind: "text" },
    ],
  },
  {
    title: "Appliances (optional)",
    rows: [
      { key: "heat_pump_power_sensor", label: "Heat pump", hint: "power, W", kind: "numeric" },
      { key: "boiler_power_sensor", label: "Boiler", hint: "power, W", kind: "numeric" },
      { key: "ev_power_sensor", label: "EV charger", hint: "power, W", kind: "numeric" },
    ],
  },
];
const SENSORS = SENSOR_GROUPS.flatMap((g) => g.rows);
const NUMBER_FIELDS = ["record_interval_minutes", "history_days", "prediction_margin_percent", "min_soc_percent",
  "max_soc_percent", "apply_threshold_percent", "charge_all_soc_percent"];
const TEXT_FIELDS = ["claude_effort", "response_language", "extra_instructions", "tariff_currency", "program_time_marks"];
const PRICE_FIELDS = ["tariff_peak_price", "tariff_offpeak_price"];
const CHECKBOXES = ["notify_predictions", "notify_soc_changes", "notify_errors"];
const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
const PROGRAMS = 6;

const form = $("settingsForm");
const field = (name) => form.elements.namedItem(name);
let settingsLoaded = false;

// Rendering ----------------------------------------------------------------------

function entityRow({ name, label, hint, kind }) {
  const input = el("input", {
    type: "text",
    name,
    list: "entityList",
    placeholder: kind === "weather" ? "weather.home" : kind === "switch" ? "switch.example" : "sensor.example",
    autocomplete: "off",
    spellcheck: "false",
  });
  const result = el("div", { class: "result" });
  const button = el("button", { type: "button", class: "secondary" }, "Test");
  const row = el(
    "div",
    { class: "entity-row" },
    el("div", { class: "name" }, label, hint ? el("span", { class: "hint" }, hint) : null),
    input,
    button,
    result,
  );
  row.dataset.kind = kind;
  button.addEventListener("click", () => testEntity(row));
  input.addEventListener("change", () => testEntity(row));
  return row;
}

function buildForm() {
  $("sensorRows").replaceChildren(
    ...SENSOR_GROUPS.flatMap((group) => [
      el("h3", { class: "group-title" }, group.title),
      ...group.rows.map((s) => entityRow({ name: s.key, ...s })),
    ]),
  );
  const blocks = [];
  for (let slot = 1; slot <= PROGRAMS; slot++) {
    blocks.push(
      el(
        "div",
        { class: "program-block" },
        el("h3", {}, `Program ${slot}`),
        entityRow({ name: `deye_programs.${slot}.time_entity`, label: "Start time", hint: "time / select / sensor", kind: "time" }),
        entityRow({ name: `deye_programs.${slot}.soc_entity`, label: "SOC capacity", hint: "% (number or select)", kind: "numeric" }),
        entityRow({ name: `deye_programs.${slot}.charge_entity`, label: "Force charge", hint: "grid charge switch (optional)", kind: "switch" }),
      ),
    );
  }
  $("programRows").replaceChildren(...blocks);
  $("weekendDays").replaceChildren(
    ...DAYS.map((day) =>
      el("label", {}, el("input", { type: "checkbox", name: "weekend_days", value: day }), day[0].toUpperCase() + day.slice(1)),
    ),
  );
  field("analysis_times_list").addEventListener("input", schedulePreview);
}

function fillForm(s) {
  for (const key of [...TEXT_FIELDS, ...NUMBER_FIELDS, ...PRICE_FIELDS, ...SENSORS.map((x) => x.key)]) field(key).value = s[key] ?? "";
  field("tariff_offpeak_windows").value = (s.tariff_offpeak_windows || []).join(", ");
  field("analysis_times_list").value = (s.analysis_times_list || []).join(", ");
  field("notify_services").value = (s.notify_services || []).join(", ");
  for (const key of CHECKBOXES) field(key).checked = !!s[key];
  field("claude_api_key").value = "";
  loadModels().then((data) => {
    fillModelSelect(field("claude_model"), data.models, s.claude_model);
    $("modelHint").textContent = data.live ? "Models available to your API key." : data.error ? `Could not list models: ${data.error}` : "Save an API key to list the models it can use.";
  });
  $("apiKeyHint").textContent = s.claude_api_key_set ? "A key is saved. Leave empty to keep it." : "No key saved yet.";
  form.querySelectorAll('input[name="weekend_days"]').forEach((box) => {
    box.checked = s.weekend_days.includes(box.value);
  });
  for (let slot = 1; slot <= PROGRAMS; slot++) {
    const program = s.deye_programs[slot - 1] || {};
    for (const key of ["time_entity", "soc_entity", "charge_entity"]) field(`deye_programs.${slot}.${key}`).value = program[key] || "";
  }
  $("importDays").value = Math.min(s.history_days, 10);
  schedulePreview();
}

function readForm() {
  const value = (n) => field(n).value.trim();
  const data = {
    weekend_days: [...form.querySelectorAll('input[name="weekend_days"]:checked')].map((b) => b.value),
    analysis_times_list: value("analysis_times_list"),
    notify_services: value("notify_services"),
    tariff_offpeak_windows: value("tariff_offpeak_windows"),
    deye_programs: [],
  };
  for (const key of TEXT_FIELDS) data[key] = value(key);
  if (field("claude_model").value) data.claude_model = field("claude_model").value;
  for (const key of NUMBER_FIELDS) data[key] = Number(value(key));
  for (const key of PRICE_FIELDS) data[key] = value(key);
  for (const key of CHECKBOXES) data[key] = field(key).checked;
  if (value("claude_api_key")) data.claude_api_key = value("claude_api_key");
  for (const s of SENSORS) data[s.key] = value(s.key);
  for (let slot = 1; slot <= PROGRAMS; slot++) {
    data.deye_programs.push({
      time_entity: value(`deye_programs.${slot}.time_entity`),
      soc_entity: value(`deye_programs.${slot}.soc_entity`),
      charge_entity: value(`deye_programs.${slot}.charge_entity`),
    });
  }
  return data;
}

function schedulePreview() {
  const times = field("analysis_times_list").value.split(/[,;]/).map((t) => t.trim()).filter(Boolean);
  $("schedulePreview").textContent = times.length ? `${times.length} run(s) a day: ${times.join(", ")}` : "No runs scheduled";
}

$("spreadApply").addEventListener("click", () => {
  const count = Math.max(1, Math.min(24, Number($("spreadCount").value) || 1));
  const [h, m] = ($("spreadStart").value || "12:00").split(":").map(Number);
  const start = h * 60 + m;
  field("analysis_times_list").value = [...new Set(Array.from({ length: count }, (_, i) => (start + Math.round((i * 1440) / count)) % 1440))]
    .sort((a, b) => a - b)
    .map((t) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`)
    .join(", ");
  schedulePreview();
});

// Results ------------------------------------------------------------------------

function showResult(target, cls, ...parts) {
  target.className = `result ${cls}`;
  target.replaceChildren(...parts);
}

function ago(iso) {
  if (!iso) return "";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "updated just now";
  if (minutes < 120) return `updated ${minutes} min ago`;
  return `updated ${Math.round(minutes / 60)} h ago`;
}

async function testEntity(row) {
  const input = row.querySelector("input");
  const result = row.querySelector(".result");
  const entityId = input.value.trim();
  input.classList.remove("invalid");
  if (!entityId) {
    showResult(result, "", "Not configured");
    return;
  }
  showResult(result, "", "Testing…");
  try {
    const r = await api(`api/test/entity?entity_id=${encodeURIComponent(entityId)}&kind=${row.dataset.kind}`);
    const details = [r.detail, r.friendly_name, ago(r.last_updated)].filter(Boolean).join(" · ");
    if (r.error) {
      showResult(result, "error", `✕ ${r.error}`);
    } else {
      const value = el("span", { class: "value" }, `${r.state}${r.unit ? " " + r.unit : ""}`);
      showResult(result, r.ok ? "ok" : "warn", r.ok ? "✓ " : "⚠ ", value, details ? ` · ${details}` : "", r.warning ? ` — ${r.warning}` : "");
    }
  } catch (err) {
    showResult(result, "error", `✕ Could not run the test: ${err.message}`);
  }
}

function testAllEntities() {
  form.querySelectorAll(".entity-row").forEach((row) => testEntity(row));
}

async function testHa() {
  const result = $("haResult");
  showResult(result, "", "Testing…");
  try {
    const r = await api("api/test/ha");
    if (r.ok) {
      showResult(result, "ok", `✓ Connected to Home Assistant ${r.version || ""}`.trim(),
        r.location_name ? ` (${r.location_name})` : "", r.time_zone ? ` · time zone ${r.time_zone}` : "",
        ` · token from ${r.token_source}`);
    } else {
      showResult(result, "error", `✕ ${r.error}`, ` (API ${r.url}, token from ${r.token_source})`);
    }
  } catch (err) {
    showResult(result, "error", `✕ Could not run the test: ${err.message}`);
  }
}

async function postJson(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

async function testClaude() {
  const result = $("claudeResult");
  showResult(result, "", "Testing…");
  try {
    const r = await postJson("api/test/claude", { api_key: field("claude_api_key").value.trim(), model: field("claude_model").value.trim() });
    if (r.ok) showResult(result, "ok", "✓ API key works · ", el("span", { class: "value" }, r.display_name || r.model), ` (${r.model})`);
    else showResult(result, "error", `✕ ${r.error}`);
  } catch (err) {
    showResult(result, "error", `✕ Could not run the test: ${err.message}`);
  }
}

async function testNotify() {
  const result = $("notifyResult");
  const services = field("notify_services").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!services.length) {
    showResult(result, "error", "✕ Enter a notify service first.");
    return;
  }
  showResult(result, "", "Sending…");
  const outcomes = await Promise.all(services.map((service) => postJson("api/test/notify", { service }).catch((err) => ({ error: err.message }))));
  const failed = outcomes.map((r, i) => (r.ok ? null : `${services[i]}: ${r.error}`)).filter(Boolean);
  if (failed.length) showResult(result, "error", `✕ ${failed.join("; ")}`);
  else showResult(result, "ok", `✓ Sent to ${services.join(", ")}. Check your phone.`);
}

async function importHistory() {
  const result = $("importResult");
  const days = Number($("importDays").value) || 10;
  if (!confirm(`Replace BatteryAI's readings of the last ${days} days with Home Assistant history?`)) return;
  const r = await postJson("api/history/import", { days }).catch((err) => ({ error: err.message }));
  if (r.error) {
    showResult(result, "error", `✕ ${r.error}`);
    return;
  }
  showResult(result, "", "Importing…");
  const poll = async () => {
    const s = await api("api/status").catch(() => null);
    const imp = s?.history_import;
    if (imp?.running) {
      showResult(result, "", `Importing… ${imp.done ?? 0}/${imp.total ?? "?"} entities (${imp.current || ""})`);
      setTimeout(poll, 2000);
    } else if (imp?.error) {
      showResult(result, "error", `✕ ${imp.error}`);
    } else if (imp?.result) {
      const failed = Object.entries(imp.result.failed || {});
      showResult(result, failed.length ? "warn" : "ok",
        `✓ Imported ${imp.result.readings} readings (${imp.result.entities} entities)`,
        failed.length ? ` — failed: ${failed.map(([e, m]) => `${e} (${m})`).join(", ")}` : "");
    }
  };
  setTimeout(poll, 1000);
}

async function loadEntities() {
  try {
    const list = await api("api/entities");
    $("entityList").replaceChildren(
      ...list.map((e) => el("option", { value: e.entity_id }, `${e.name || e.entity_id} — ${e.state}${e.unit ? " " + e.unit : ""}`)),
    );
  } catch (err) {
    // The HA connection test above shows the reason.
  }
  try {
    const services = await api("api/notify_services");
    $("notifyList").replaceChildren(...services.map((s) => el("option", { value: s.service }, s.phone ? "📱 phone" : s.name || "")));
    const phones = services.filter((s) => s.phone).map((s) => s.service);
    $("notifyHint").textContent = phones.length
      ? `Phones found: ${phones.join(", ")}`
      : "No phones found. Install the Home Assistant Companion app on your phone; it then appears as mobile_app_…";
  } catch (err) {
    // ignore; the field still accepts typed names
  }
}

// Save ---------------------------------------------------------------------------

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("saveResult");
  form.querySelectorAll(".invalid").forEach((i) => i.classList.remove("invalid"));
  $("saveSettings").disabled = true;
  showResult(result, "", "Saving…");
  try {
    const resp = await fetch("api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readForm()),
    });
    const body = await resp.json();
    if (resp.status === 400) {
      const messages = Object.entries(body.errors).map(([key, msg]) => {
        field(key)?.classList.add("invalid");
        return `${key.replace(/_/g, " ")}: ${msg}`;
      });
      showResult(result, "error", `✕ ${messages.join("; ")}`);
      form.querySelector(".invalid")?.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (!resp.ok) {
      showResult(result, "error", `✕ HTTP ${resp.status}`);
    } else {
      await loadModels(true);
      fillForm(body);
      fillModelSelect($("modelSelect"), (await loadModels()).models, body.claude_model, true);
      showResult(result, "ok", "✓ Saved and applied");
    }
  } catch (err) {
    showResult(result, "error", `✕ ${err.message}`);
  } finally {
    $("saveSettings").disabled = false;
  }
});

$("testHa").addEventListener("click", testHa);
$("testClaude").addEventListener("click", testClaude);
$("testAll").addEventListener("click", testAllEntities);
$("testNotify").addEventListener("click", testNotify);
$("importHistory").addEventListener("click", importHistory);

async function openSettings() {
  if (settingsLoaded) return;
  settingsLoaded = true;
  buildForm();
  try {
    fillForm(await api("api/settings"));
  } catch (err) {
    showResult($("saveResult"), "error", `✕ Could not load settings: ${err.message}`);
    settingsLoaded = false;
    return;
  }
  testHa();
  loadEntities();
  testAllEntities();
}

window.addEventListener("batteryai:tab", (event) => {
  if (event.detail === "settings") openSettings();
});
if (location.hash === "#settings") openSettings();
