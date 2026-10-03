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
];
const SENSORS = SENSOR_GROUPS.flatMap((g) => g.rows);
const NUMBER_FIELDS = ["record_interval_minutes", "history_days", "detail_days", "local_llm_threads", "prediction_margin_percent", "min_soc_percent",
  "max_soc_percent", "apply_threshold_percent", "charge_all_soc_percent"];
const TEXT_FIELDS = ["claude_effort", "response_language", "extra_instructions", "tariff_currency", "program_time_marks", "prediction_engine", "openai_effort"];
const PRICE_FIELDS = ["battery_capacity_kwh"];

const ENGINE_HELP = {
  claude: "Claude reads all recorded history, weather, tariffs and outages and writes the plan. Needs internet and an API key; each prediction costs API tokens.",
  openai: "ChatGPT gets the same data and instructions as Claude (history, weather, tariffs, outages) and writes the plan. Needs internet and an OpenAI API key; each prediction costs API tokens.",
  local_fast: "Runs inside the add-on with no internet and no AI: averages your most similar past days (weekday/weekend, temperature) and plans each program with fixed rules. Instant and light on the CPU.",
  local_llm: "Runs Qwen2.5 3B inside the add-on (no internet after the one-time model download): the local fast forecast is its input and the LLM writes the plan. Needs ~3 GB RAM and takes minutes per prediction on a small CPU.",
};

function updateEngineView() {
  const engine = field("prediction_engine").value;
  $("engineHelp").textContent = ENGINE_HELP[engine] || "";
  $("localLlmBox").hidden = engine !== "local_llm";
  $("claudeBox").hidden = engine !== "claude";
  $("openaiBox").hidden = engine !== "openai";
  if (engine === "local_llm") refreshLlmStatus();
}

let llmTimer = null;
async function refreshLlmStatus() {
  clearTimeout(llmTimer);
  let st;
  try {
    st = await api("api/local_llm");
  } catch (err) {
    showResult($("llmStatus"), "error", `✕ ${err.message}`);
    return;
  }
  const d = st.download;
  $("llmThreadsHint").textContent = `This machine has ${st.cpu_count} CPU cores.`;
  $("llmDownload").hidden = st.model || d.running;
  $("llmCancel").hidden = !d.running;
  $("llmDelete").hidden = !st.model;
  if (!st.runtime) {
    showResult($("llmStatus"), "error", "✕ llama.cpp is not built into this add-on image (check the add-on build log, then rebuild).");
  } else if (d.running) {
    const pct = d.total ? (d.done / d.total) * 100 : 0;
    showResult($("llmStatus"), "", `Downloading ${st.model_name}: ${fmtBytes(d.done)} of ${fmtBytes(d.total)}`,
      el("div", { class: "progress" }, el("div", { style: `width:${pct.toFixed(1)}%` })));
    llmTimer = setTimeout(refreshLlmStatus, 2000);
  } else if (st.model) {
    showResult($("llmStatus"), "ok", `✓ ${st.model_name} ready (${fmtBytes(st.model_bytes)}). It loads only while a prediction runs.`);
  } else {
    showResult($("llmStatus"), d.error ? "error" : "warn", d.error ? `✕ Download failed: ${d.error}` : `⚠ Model not downloaded yet (about 2 GB, stored in the add-on's /data, excluded from backups).`);
  }
}
const CHECKBOXES = ["notify_predictions", "notify_soc_changes", "notify_errors"];
const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
const PROGRAMS = 6;

const form = $("settingsForm");
const field = (name) => form.elements.namedItem(name);
let settingsLoaded = false;

// Appliances: a list the user builds with + / −. Each row keeps its id (database key)
// in data-id; new rows get an id from the server when saved.
const MAX_APPLIANCES = 12;

function applianceRow(appliance = {}) {
  const row = el("div", { class: "appliance-row" });
  row.dataset.id = appliance.id || "";
  const name = el("input", { type: "text", placeholder: "Name, e.g. Heat pump", maxlength: "40", "data-role": "name" });
  name.value = appliance.name || "";
  const entityInput = el("input", { type: "text", list: "entityList", placeholder: "sensor.example_power", autocomplete: "off", spellcheck: "false", "data-role": "entity" });
  entityInput.value = appliance.entity || "";
  const temp = el("input", { type: "checkbox", "data-role": "temp" });
  temp.checked = !!appliance.temperature_dependent;
  const result = el("div", { class: "result" });
  const test = el("button", { type: "button", class: "secondary" }, "Test");
  const remove = el("button", { type: "button", class: "secondary remove", title: "Remove this appliance", "aria-label": "Remove appliance" }, "−");
  const testRow = { querySelector: (sel) => (sel === "input" ? entityInput : result), dataset: { kind: "numeric" } };
  test.addEventListener("click", () => testEntity(testRow));
  entityInput.addEventListener("change", () => testEntity(testRow));
  remove.addEventListener("click", () => {
    row.remove();
    renumberAppliances();
  });
  row.append(
    el("div", { class: "appliance-fields" },
      el("label", {}, "Name", name),
      el("label", {}, "Power sensor", entityInput),
      el("label", { class: "inline-row temp-toggle", title: "Heats or cools the house, so its use follows the outdoor temperature" }, temp, "Depends on outdoor temperature"),
      el("span", { class: "appliance-buttons" }, test, remove)),
    result,
  );
  if (appliance.entity) testEntity(testRow);
  return row;
}

// Field names carry the row index so validation errors ("appliances.2.entity") find their input.
function renumberAppliances() {
  const rows = [...$("applianceRows").children];
  rows.forEach((row, i) => {
    row.querySelector('[data-role="name"]').name = `appliances.${i}.name`;
    row.querySelector('[data-role="entity"]').name = `appliances.${i}.entity`;
  });
  $("addAppliance").disabled = rows.length >= MAX_APPLIANCES;
  if (!rows.length) $("applianceRows").replaceChildren();
}

function fillAppliances(list) {
  $("applianceRows").replaceChildren(...list.map((a) => applianceRow(a)));
  renumberAppliances();
}

function readAppliances() {
  return [...$("applianceRows").children].map((row) => ({
    id: row.dataset.id,
    name: row.querySelector('[data-role="name"]').value.trim(),
    entity: row.querySelector('[data-role="entity"]').value.trim(),
    temperature_dependent: row.querySelector('[data-role="temp"]').checked,
  }));
}

$("addAppliance").addEventListener("click", () => {
  const row = applianceRow();
  $("applianceRows").append(row);
  renumberAppliances();
  row.querySelector('[data-role="name"]').focus();
});

// Tariffs: a list with + / −. Each has a name, a price and time windows; exactly one is
// "all other times" (radio button).
const MAX_TARIFFS = 8;

function tariffRow(tariff = {}) {
  const row = el("div", { class: "tariff-row" });
  const name = el("input", { type: "text", placeholder: "Name, e.g. Night", maxlength: "30", "data-role": "name" });
  name.value = tariff.name || "";
  const price = el("input", { type: "number", min: "0", step: "0.0001", placeholder: "0.00", "data-role": "price" });
  price.value = tariff.price ?? "";
  const windows = el("input", { type: "text", placeholder: "23:00-07:00", "data-role": "windows" });
  windows.value = (tariff.windows || []).join(", ");
  const isDefault = el("input", { type: "radio", name: "tariff_default", "data-role": "default" });
  isDefault.checked = !!tariff.default;
  const sync = () => {
    windows.disabled = isDefault.checked;
    if (isDefault.checked) windows.value = "";
    windows.placeholder = isDefault.checked ? "all other times" : "23:00-07:00";
  };
  isDefault.addEventListener("change", () => $("tariffRows").querySelectorAll('[data-role="default"]').forEach((r) => r.dispatchEvent(new Event("sync"))));
  isDefault.addEventListener("sync", sync);
  const remove = el("button", { type: "button", class: "secondary remove", title: "Remove this tariff", "aria-label": "Remove tariff" }, "−");
  remove.addEventListener("click", () => {
    row.remove();
    renumberTariffs();
  });
  row.append(
    el("label", {}, "Name", name),
    el("label", {}, "Price per kWh", price),
    el("label", { class: "tariff-windows" }, "Time windows", windows),
    el("label", { class: "inline-row temp-toggle" }, isDefault, el("span", { "data-role": "default-label" }, "All other times")),
    el("span", { class: "appliance-buttons" }, remove),
  );
  sync();
  return row;
}

function renumberTariffs() {
  const rows = [...$("tariffRows").children];
  rows.forEach((row, i) => {
    row.querySelector('[data-role="name"]').name = `tariffs.${i}.name`;
    row.querySelector('[data-role="price"]').name = `tariffs.${i}.price`;
    row.querySelector('[data-role="windows"]').name = `tariffs.${i}.windows`;
  });
  $("addTariff").disabled = rows.length >= MAX_TARIFFS;
  // A single tariff is one price at all times: it is the "all other times" tariff and
  // cannot be removed.
  const single = rows.length === 1;
  rows.forEach((row) => {
    const radio = row.querySelector('[data-role="default"]');
    if (single && !radio.checked) {
      radio.checked = true;
      radio.dispatchEvent(new Event("change"));
    }
    radio.disabled = single;
    row.querySelector('[data-role="windows"]').placeholder = radio.checked ? (single ? "all day" : "all other times") : "23:00-07:00";
    row.querySelector(".remove").disabled = single;
    row.querySelector('[data-role="default-label"]').textContent = single ? "All day" : "All other times";
  });
}

function fillTariffs(list) {
  $("tariffRows").replaceChildren(...list.map((t) => tariffRow(t)));
  renumberTariffs();
}

function readTariffs() {
  return [...$("tariffRows").children].map((row) => ({
    name: row.querySelector('[data-role="name"]').value.trim(),
    price: row.querySelector('[data-role="price"]').value.trim(),
    windows: row.querySelector('[data-role="windows"]').value.trim(),
    default: row.querySelector('[data-role="default"]').checked,
  }));
}

$("addTariff").addEventListener("click", () => {
  const row = tariffRow();
  $("tariffRows").append(row);
  renumberTariffs();
  row.querySelector('[data-role="name"]').focus();
});

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
        entityRow({ name: `deye_programs.${slot}.charge_entity`, label: "Grid charge", hint: "grid charge switch (optional)", kind: "switch" }),
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
  fillTariffs(s.tariffs || []);
  fillAppliances(s.appliances || []);
  field("analysis_times_list").value = (s.analysis_times_list || []).join(", ");
  field("notify_services").value = (s.notify_services || []).join(", ");
  for (const key of CHECKBOXES) field(key).checked = !!s[key];
  updateEngineView();
  field("claude_api_key").value = "";
  loadModels().then((data) => {
    fillModelSelect(field("claude_model"), data.models, s.claude_model);
    $("modelHint").textContent = data.live ? "Models available to your API key." : data.error ? `Could not list models: ${data.error}` : "Save an API key to list the models it can use.";
    fillModelSelect(field("openai_model"), data.openai_models || [], s.openai_model);
    $("openaiModelHint").textContent = data.openai_live ? "Models available to your API key." : data.openai_error ? `Could not list models: ${data.openai_error}` : "Save an API key to list the models it can use.";
  });
  field("openai_api_key").value = "";
  $("openaiKeyHint").textContent = s.openai_api_key_set ? "A key is saved. Leave empty to keep it." : "No key saved yet.";
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
    tariffs: readTariffs(),
    appliances: readAppliances(),
    deye_programs: [],
  };
  for (const key of TEXT_FIELDS) data[key] = value(key);
  if (field("claude_model").value) data.claude_model = field("claude_model").value;
  if (field("openai_model").value) data.openai_model = field("openai_model").value;
  for (const key of NUMBER_FIELDS) data[key] = Number(value(key));
  for (const key of PRICE_FIELDS) data[key] = value(key);
  for (const key of CHECKBOXES) data[key] = field(key).checked;
  if (value("claude_api_key")) data.claude_api_key = value("claude_api_key");
  if (value("openai_api_key")) data.openai_api_key = value("openai_api_key");
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

async function testOpenai() {
  const result = $("openaiResult");
  showResult(result, "", "Testing…");
  try {
    const r = await postJson("api/test/openai", { api_key: field("openai_api_key").value.trim(), model: field("openai_model").value.trim() });
    if (r.ok) showResult(result, "ok", "✓ API key works · ", el("span", { class: "value" }, r.model));
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
      initModelSelect(true);
      showResult(result, "ok", "✓ Saved and applied");
    }
  } catch (err) {
    showResult(result, "error", `✕ ${err.message}`);
  } finally {
    $("saveSettings").disabled = false;
  }
});

field("prediction_engine").addEventListener("change", updateEngineView);
$("llmDownload").addEventListener("click", async () => {
  await api("api/local_llm/download", { method: "POST" }).catch(() => null);
  refreshLlmStatus();
});
$("llmCancel").addEventListener("click", async () => {
  await api("api/local_llm/cancel", { method: "POST" }).catch(() => null);
  setTimeout(refreshLlmStatus, 500);
});
$("llmDelete").addEventListener("click", async () => {
  if (!confirm("Delete the downloaded model file?")) return;
  const r = await api("api/local_llm/model", { method: "DELETE" }).catch((err) => ({ error: err.message }));
  if (r.error) alert(r.error);
  refreshLlmStatus();
});
$("testHa").addEventListener("click", testHa);
$("testClaude").addEventListener("click", testClaude);
$("testOpenai").addEventListener("click", testOpenai);
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
