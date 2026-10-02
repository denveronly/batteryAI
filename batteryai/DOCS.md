# BatteryAI

BatteryAI records your battery, solar, load, appliance, weather and outage data, and
asks Claude at fixed times each day (12:00 and 23:00 by default) to predict consumption
and plan the SOC and grid charge of the six Deye time-of-use programs. With AI
auto-control on, the plan is written to the inverter.

## Tabs

### Dashboard

- **Tiles** – battery SOC, load, PV power and PV surplus (positive = the battery is
  charging from the sun), consumption, solar forecasts, heat pump / boiler / EV power,
  outdoor temperature, tomorrow's weather, current tariff, outages.
- **Battery control** – also lists the six Deye programs with their time ranges; type a
  new SOC and press *Set*, or flip the *Grid charge* switch, to change the inverter
  directly.
  - *AI auto-control* – when on, every prediction writes Claude's SOC values and force
    charge settings to the Deye programs. When off, Claude only advises.
  - *Charge all to 98%* – sets every program to the “Charge all” SOC, turns on each
    program's grid-charge switch and turns AI auto-control off. Turning auto-control
    back on restores the grid-charge switches and applies the latest prediction.
- **Latest prediction** – consumption today/tomorrow, PV tomorrow, lowest SOC, outage
  risk, estimated grid cost, the effect of the weather, when the heat pump, boiler and
  EV will run, an hourly power forecast for tomorrow, the suggested programs, and
  *Apply to inverter* to write it once.
- **Predict now** – runs a prediction immediately.
- Charts: battery SOC with each Deye program's time range as a labelled band; power
  (load, PV, appliances, outdoor temperature and Claude's predicted hourly load); daily
  energy.
- **Prediction accuracy** – per day, predicted vs. actual consumption (accuracy %),
  solar forecast vs. actual PV, how much of the consumption PV covered and the grid
  share.
- **AI analysis log** – every run with its reasoning, the exact data sent to Claude and
  the changes written to the inverter.

### Economy

Per day and in total: what was paid for grid energy (peak / off-peak), what the same
consumption would have cost without PV and battery, and what was saved — split into
**saved by PV** (load covered directly by PV power) and **saved by battery & AI plan**
(battery energy and cheap off-peak charging replacing peak-time grid energy). The
*AI control* column shows how much of the day auto-control was on.
Requires the *Grid import today* and *PV power* sensors and the tariff settings.

### Logs

The add-on's recent log lines (filter by level, search, live update) and the storage
used: database size and rows, how far back the data goes, everything in `/data`, and
free disk space. *Compress old readings* and *Compact database* shrink the database.

### Settings

All settings are stored in `/data/settings.json` and apply immediately when saved.

- **Home Assistant connection** – test shows the version and time zone, or why the
  add-on cannot read data.
- **Prediction engine** – *Claude* (cloud), *Local fast* (statistics + rules, light
  CPU) or *Local slow* (Qwen2.5 3B LLM on the CPU). Also switchable from the header
  dropdown. See *Prediction engines* below.
- **Claude** – API key (never sent back to the browser), model (dropdown with the models
  your key can use; also in the header), effort, language.
- **Prediction schedule** – the times of the daily runs (default 12:00 and 23:00), or
  fill them evenly with *N per day from HH:MM*. Recording interval, history days,
  weekend days.
- **Prediction tuning**
  - *Safety margin* – Claude plans for the predicted consumption plus this %.
  - *Lowest / highest SOC* – Claude's SOC values are kept within this range.
  - *Apply threshold* – a program's SOC is only rewritten when the new value differs by
    at least this many %.
  - *Charge all SOC* – the value used by *Charge all* (98% by default).
- **Sensors** – battery SOC, solar forecast today/tomorrow, PV power (W), PV production
  today (kWh), load power (W, kW is converted), consumption today (kWh), grid import
  today (kWh), weather, probable outages.
- **Appliances** – your own list of devices with a power sensor (W or kW is converted):
  a name, the sensor, and *Depends on outdoor temperature* for anything that heats or
  cools (heat pump, AC). Add rows with **+ Add appliance**, remove them with **−**.
  Each appliance gets its own tile, chart line and daily bar, and the predictions
  forecast when it will run. Renaming keeps its history; a newly added appliance starts
  recording now (or use Settings → History → Import to fill it from Home Assistant).
- **Deye programs** – for each of the 6 programs: time entity, SOC entity (number,
  input_number or select) and an optional grid charge switch. *Program time
  marks* says how the inverter reads the times: **end** (default) – a program's time is
  the end of its range, which starts at the previous program's time (P1 05:00 after
  P6 23:15 covers 23:15 – 05:00); **start** – the range lasts until the next program's
  time. Two programs with the same time leave one of them unused. BatteryAI never
  changes program times, only SOC and grid charge.
- **Tariff** – currency, peak and off-peak price per kWh, off-peak windows such as
  `23:00-07:00` (every other time is peak).
- **Phone notifications** – one or more notify services (phones from the Home Assistant
  Companion app appear as `mobile_app_…`), with a test button. Notifications are normal
  priority, not critical alerts: every prediction, every SOC / grid-charge change, and
  failed predictions can each be switched on or off.
- **History** – rebuild the data from the Home Assistant recorder (10 days by default).
- **Extra instructions** for Claude.

Every entity has a **Test** button showing its current value or why it fails.

## Prediction engines

| Engine | Runs | Speed / load | Quality |
| --- | --- | --- | --- |
| Claude | Anthropic cloud, API key | ~1 min, nothing local | Best: reads all history, weather, outages, tariffs |
| Local fast | inside the add-on | instant, negligible CPU | Solid baseline: similar-day forecast + fixed rules |
| Local slow | inside the add-on (llama.cpp) | minutes, ~3 GB RAM, all cores | Plans and explains like an AI, from the local fast forecast |

**Local fast** averages the hourly load of the 5 most similar recorded days (same
weekday/weekend type, closest outdoor temperature, recent days preferred), scales heat
pump use with a temperature regression, and shapes PV from your recorded PV power. For
each off-peak program it keeps enough energy (plus the safety margin, using *Battery
capacity*) for the following peak hours that PV will not cover; peak programs let the
battery discharge; an expected outage keeps the battery full with grid charge on.

**Local slow** uses Qwen2.5-3B-Instruct (Q4_K_M, about 2 GB). llama.cpp is compiled into
the add-on when it is installed or updated (this takes 10–30 minutes on a Raspberry Pi;
if the build fails the add-on still works and this engine reports it is unavailable).
Download the model once in Settings → Prediction engine; it is stored in `/data/models`
and excluded from backups. The model is loaded in a separate process only while a
prediction runs, so the memory is free the rest of the time.

## Weather

Use a `weather.*` entity: BatteryAI reads the current temperature and asks Home Assistant
for the daily and hourly forecast (`weather.get_forecasts`). Claude relates heat pump
energy to the outdoor temperature in the history and uses tomorrow's temperatures to
predict it. A plain temperature sensor also works, without a forecast.

## Outages

When the outage sensor's state or attributes change, BatteryAI runs an extra
prediction (at most once every 30 minutes). Claude raises the SOC and turns on force
charge before an outage when the battery would not otherwise cover the load — unless
the outage falls in daylight and PV is expected to cover it.

## History and database size

Readings are kept in full detail (every few minutes) for *Keep full detail* days (30 by
default, Settings → History). Older readings are compressed to one row per hour: power,
SOC and temperature become hourly averages, energy counters keep their last value, so
daily totals and costs stay the same. Charts, the Economy tab and predictions use both.
A year of data takes a few MB. History sent to Claude can be up to 365 days.

On first start (less than a day of data) BatteryAI imports the configured entities from
the Home Assistant recorder, so the charts and Claude have data right away. New values
are then recorded at the recording interval. *Settings → History* re-imports on demand.

## Safety

Nothing is written to the inverter unless AI auto-control is on, *Apply to inverter* is
pressed, or *Charge all* is pressed. Every write respects the entity's own min/max/step
and the SOC range in *Prediction tuning*, and is listed in the analysis log and sent as a
notification.

## Costs

Each prediction is one Claude API request; token usage is shown in the log. Outage
changes can add extra runs (at most one per 30 minutes).
