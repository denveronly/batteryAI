# Changelog

## 0.4.12

- Monthly bill: each month keeps its own price per kWh per tariff, shown in the table
  and editable there (the month's costs follow). A new month takes the prices from the
  tariffs in Settings. **Use current prices** sets a month to the Settings prices.

## 0.4.11

- **Charge before outages**: with a *Minutes to outage* sensor (e.g.
  `sensor.svitlo_kyiv_4_1_minutes_to_outage`), every program is set to 100% with grid
  charge on 90 minutes before an outage, then the previous mode and SOC values come back.
  Switch in the Battery control card; minutes and SOC in Settings → Prediction tuning. The Probable
  outages tile shows the time until the next outage.
- Logs → **Backup**: download a zip with the database, settings and control state, and
  restore it into a reinstalled add-on.
- Monthly bill: the most expensive tariff comes first (Peak before Off-peak); a single
  tariff shows its own name; **Recalculate** reprices a month with the current tariffs;
  energy at the start of a month no longer leaks into the previous month.
- Dashboard: PV surplus no longer goes negative at night ("no PV now", or how much of the
  load PV covers).

## 0.4.10

- Economy → **Monthly bill**: grid kWh and cost per month (current month first), per
  tariff (e.g. peak / off-peak kWh and cost) or just overall with a single price, a year
  total row, a year selector, and the days of a month on click.
  - New setting *Grid energy meter* (Settings → Sensors), e.g. a Shelly EM total energy
    sensor; without it the *Grid import today* sensor is used.
  - Energy and cost are stored in the database at the price in effect when recorded, so
    later tariff changes leave past months unchanged.
  - This month so far is read from the Home Assistant history when a meter is first set.

## 0.4.9

- New prediction engine: **ChatGPT** through the OpenAI API (Settings → Prediction
  engine, or the header dropdown). It gets the same data, instructions and answer format
  as Claude. Settings → ChatGPT: API key, model (listed from your key), reasoning effort
  and a test button.
- Phone notifications are easier to read, especially on iPhone:
  - One notification per prediction instead of two. Before, the "SOC updated" message
    replaced the prediction on the phone, because both used the same tag.
  - A short brief first (tomorrow's use, solar, lowest battery, whether it was written
    to the inverter). Pull the notification down to see the summary and every program as
    "P1 00:00–03:00 54% · grid charge (was 100%)".
  - Tap it, or its **Open BatteryAI** button, to open the BatteryAI panel.
  - A warning icon in the title when an outage is expected.

## 0.4.8

- Fix: Deye program time ranges. A program runs from its own time until the next
  program's time, and program 6 runs until program 1 (P1 00:00, P2 03:00 = 00:00 – 03:00).
  This is now the default, and existing settings are switched to it once. The dashboard,
  the battery chart bands, the active program, the stored program SOC history and every
  prediction engine use the corrected ranges.

## 0.4.7

- Single price: keep just one tariff (shown as "All day") when the grid price is the
  same all the time. Every prediction engine then keeps grid charge off (except before
  an outage) and uses the battery for PV.

## 0.4.6

- Tariffs are a list you manage in Settings → Tariffs: any number of named tariffs (up
  to 8) with their own price and time windows, one of them covering "all other times".
  Add rows with **+ Add tariff**, remove them with **−**. Existing peak / off-peak
  settings are carried over as *Off-peak* and *Peak*.
- The dashboard tile shows the current tariff's name and price; the Economy table has a
  grid energy column per tariff; Claude and the local engines get the price of every
  hour and charge in the cheapest tariff.
- Negative savings are shown in red.

## 0.4.5

- Seasons: a monthly summary over all stored history (consumption, PV, grid, appliances,
  temperature, weekday/weekend averages, cost and savings).
  - Economy tab → "By month" chart and table.
  - Claude receives the monthly history and the same weeks of last year.
  - Local fast searches all history for similar days and prefers the same time of year.

## 0.4.4

- New BatteryAI logo (battery with a green brain and the sun) in the panel header, the
  browser tab and the Home Assistant add-on store.

## 0.4.3

- The panel header shows the running add-on version.

## 0.4.2

- Fix: when Home Assistant was still starting, the add-on fell back to UTC, so the
  tariff (peak/off-peak), prediction times and active program were off by the UTC
  offset. It now retries Home Assistant, falls back to the container's time zone, keeps
  retrying in the background, and corrects the local date/time of stored readings. The
  header shows the time zone in use.
- Fix: browsers kept old copies of the panel after an update (missing Logs tab, old
  appliance settings). UI files are now versioned and always revalidated.

## 0.4.1

- Appliances are a list you manage in Settings → Appliances: custom name, power sensor
  (W or kW), "depends on outdoor temperature", with + / − to add and remove. They appear
  on the dashboard (tiles, power and daily charts, prediction card) and in every
  prediction engine. The former heat pump / boiler / EV settings and their recorded
  history are carried over; renaming an appliance keeps its history.

## 0.4.0

- Prediction engine selection (Settings → Prediction engine, and the header dropdown):
  - **Claude** (cloud).
  - **Local fast** – runs in the add-on without internet or AI: forecasts tomorrow from
    the most similar recorded days (day type, outdoor temperature; heat pump scaled by a
    temperature regression) and plans each program with tariff/PV/outage rules. Instant.
  - **Local slow** – Qwen2.5-3B-Instruct (Q4_K_M) running on the CPU inside the add-on
    via llama.cpp, which is compiled into the add-on image. The model (~2 GB) is
    downloaded once into /data/models from the Settings tab and excluded from backups.
    It runs in a separate process only during a prediction.
- Battery capacity setting (used by the local engines and given to Claude).

## 0.3.2

- Logs tab: live add-on log with level filter and search; storage overview (database
  size, rows, data range, add-on data and disk use) with "Compress old readings" and
  "Compact database".
- History up to 365 days. Readings older than "Keep full detail" (30 days by default)
  are compressed to hourly rows; outage attributes are stored only when they change.
  A year of data stays a few MB.
- Deye programs merged into the Battery control card: set each program's SOC and switch
  its grid charge directly from the dashboard.
- "Force charge" renamed to "Grid charge".

## 0.3.1

- Deye program ranges: a program's time is the END of its range, which starts at the
  previous program's time (P1 05:00 after P6 23:15 = 23:15 – 05:00). Configurable in
  Settings → Deye programs. Tables show the time range ("23:15 – 05:00", no seconds);
  Claude gets the computed ranges and no longer suggests moving program times.
- Battery chart shows each program's range as a labelled band; stored program SOC is
  recalculated for all readings.
- Power chart shows Claude's predicted hourly load (past days and tomorrow); time axis
  ticks on round hours.
- Claude requests are streamed with a 64k output limit, so long answers with thinking
  are no longer cut off; clearer API error messages.
- Model selection dropdown in the header and in Settings, listing the models available
  to your API key.

## 0.3.0

- Charts fixed: history is imported from the Home Assistant recorder on first start (and
  on demand in Settings → History); Chart.js is bundled instead of downloaded at build
  time; empty charts explain why.
- Fixed prediction times (default 12:00 and 23:00) instead of "N per day".
- AI auto-control: predictions write SOC and force-charge settings to the Deye programs.
  "Charge all to 98%" button, "Apply to inverter" for a single prediction, "Predict now".
- Outage changes trigger an extra prediction; Claude decides force charge per program
  depending on usage, time of day and PV.
- Weather entity with tomorrow's forecast; heat pump, boiler and EV power sensors;
  hourly power forecast for tomorrow and appliance usage windows.
- Load is now a power sensor (W); PV power, PV production and grid import sensors.
- Prediction tuning: safety margin, SOC range, apply threshold, charge-all SOC.
- Prediction accuracy section (predicted vs. actual, solar forecast vs. PV).
- Tariff settings (peak/off-peak windows and prices, currency) and an Economy tab with
  savings by day from PV and from the battery / AI plan.
- Phone notifications through Home Assistant notify services.

## 0.2.0

- Settings moved from the add-on Configuration tab to a Settings tab in the panel; they
  apply without a restart. Existing options are imported on first start.
- Test buttons for the Home Assistant connection, the Claude API key/model and every
  entity, showing the current value or the exact reason it cannot be read.
- Entity ID suggestions from Home Assistant while typing.
- Fix: entities were reported as unavailable because the Supervisor token was not passed
  to the app under s6-overlay; the token is now also read from the s6 container
  environment, and connection errors are shown in the panel.
- Light / dark / automatic theme switch.

## 0.1.0

- First release: sensor recording to SQLite, scheduled Claude analyses, ingress panel with
  battery, load and daily energy charts and the AI analysis log.
