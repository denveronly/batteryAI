"use strict";

// Settings tab: edit settings, test the Home Assistant / Claude connections and every entity.

const SENSORS = [
  { key: "today_forecast_sensor", label: "Today solar forecast", hint: "kWh, e.g. Solcast forecast today", kind: "numeric" },
  { key: "tomorrow_forecast_sensor", label: "Tomorrow solar forecast", hint: "kWh", kind: "numeric" },
  { key: "battery_soc_sensor", label: "Battery SOC", hint: "%", kind: "numeric" },
  { key: "outages_sensor", label: "Probable outages", hint: "optional, any state", kind: "text" },
  { key: "today_load_sensor", label: "Today load", hint: "kWh counter, resets at midnight", kind: "numeric" },
  { key: "today_consumption_sensor", label: "Today consumption", hint: "kWh counter, resets at midnight", kind: "numeric" },
];
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
    placeholder: "sensor.example",
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
  $("sensorRows").replaceChildren(...SENSORS.map((s) => entityRow({ name: s.key, ...s })));
  const blocks = [];
  for (let slot = 1; slot <= PROGRAMS; slot++) {
    blocks.push(
      el(
        "div",
        { class: "program-block" },
        el("h3", {}, `Program ${slot}`),
        entityRow({ name: `deye_programs.${slot}.time_entity`, label: "Start time", hint: "time / select / sensor", kind: "time" }),
        entityRow({ name: `deye_programs.${slot}.soc_entity`, label: "SOC capacity", hint: "%", kind: "numeric" }),
      ),
    );
  }
  $("programRows").replaceChildren(...blocks);
  $("weekendDays").replaceChildren(
    ...DAYS.map((day) =>
      el("label", {}, el("input", { type: "checkbox", name: "weekend_days", value: day }), day[0].toUpperCase() + day.slice(1)),
    ),
  );
  ["analyses_per_day", "first_analysis_time"].forEach((n) => field(n).addEventListener("input", schedulePreview));
}

function fillForm(s) {
  for (const key of ["claude_model", "claude_effort", "response_language", "analyses_per_day",
    "first_analysis_time", "record_interval_minutes", "history_days", "extra_instructions",
    ...SENSORS.map((x) => x.key)]) {
    field(key).value = s[key] ?? "";
  }
  field("claude_api_key").value = "";
  $("apiKeyHint").textContent = s.claude_api_key_set
    ? "A key is saved. Leave empty to keep it."
    : "No key saved yet.";
  form.querySelectorAll('input[name="weekend_days"]').forEach((box) => {
    box.checked = s.weekend_days.includes(box.value);
  });
  for (let slot = 1; slot <= PROGRAMS; slot++) {
    const program = s.deye_programs[slot - 1] || {};
    field(`deye_programs.${slot}.time_entity`).value = program.time_entity || "";
    field(`deye_programs.${slot}.soc_entity`).value = program.soc_entity || "";
  }
  schedulePreview();
}

function readForm() {
  const value = (n) => field(n).value.trim();
  const data = {
    claude_model: value("claude_model"),
    claude_effort: value("claude_effort"),
    response_language: value("response_language"),
    analyses_per_day: Number(value("analyses_per_day")),
    first_analysis_time: value("first_analysis_time").slice(0, 5),
    record_interval_minutes: Number(value("record_interval_minutes")),
    history_days: Number(value("history_days")),
    extra_instructions: value("extra_instructions"),
    weekend_days: [...form.querySelectorAll('input[name="weekend_days"]:checked')].map((b) => b.value),
    deye_programs: [],
  };
  if (value("claude_api_key")) data.claude_api_key = value("claude_api_key");
  for (const s of SENSORS) data[s.key] = value(s.key);
  for (let slot = 1; slot <= PROGRAMS; slot++) {
    data.deye_programs.push({
      time_entity: value(`deye_programs.${slot}.time_entity`),
      soc_entity: value(`deye_programs.${slot}.soc_entity`),
    });
  }
  return data;
}

function schedulePreview() {
  const count = Math.max(1, Math.min(24, Number(field("analyses_per_day").value) || 1));
  const [h, m] = (field("first_analysis_time").value || "06:00").split(":").map(Number);
  const start = h * 60 + m;
  const times = [...new Set(Array.from({ length: count }, (_, i) => (start + Math.round((i * 1440) / count)) % 1440))]
    .sort((a, b) => a - b)
    .map((t) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`);
  $("schedulePreview").textContent = `Runs daily at ${times.join(", ")}`;
}

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
    const details = [r.friendly_name, ago(r.last_updated)].filter(Boolean).join(" · ");
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

async function testClaude() {
  const result = $("claudeResult");
  showResult(result, "", "Testing…");
  try {
    const r = await api("api/test/claude", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: field("claude_api_key").value.trim(), model: field("claude_model").value.trim() }),
    });
    if (r.ok) showResult(result, "ok", "✓ API key works · ", el("span", { class: "value" }, r.display_name || r.model), ` (${r.model})`);
    else showResult(result, "error", `✕ ${r.error}`);
  } catch (err) {
    showResult(result, "error", `✕ Could not run the test: ${err.message}`);
  }
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
      fillForm(body);
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
