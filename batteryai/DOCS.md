# BatteryAI

BatteryAI records your battery, solar, load, appliance, weather and outage data, and
asks an AI (Claude, ChatGPT or a local engine) at fixed times each day (12:00 and 23:00 by default) to predict consumption
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

Per day and in total: grid energy per tariff and what was paid for it, what the same
consumption would have cost without PV and battery, and what was saved — split into
**saved by PV** (load covered directly by PV power) and **saved by battery & AI plan**
(battery energy and charging in the cheapest tariff replacing pricier grid energy). The
*AI control* column shows how much of the day auto-control was on.
Requires the *Grid import today* and *PV power* sensors and the tariff settings.

**Monthly bill** (sub-tab of Economy): grid energy and cost per month of a year, read
from the *Grid energy meter* sensor (for example a Shelly EM total energy, kWh; Wh is
converted) or, if that is empty, from *Grid import today*. With a peak / off-peak tariff
each tariff has its own kWh and cost columns (the most expensive first) plus the overall
kWh and grid cost; with a single tariff its name labels the kWh and cost columns. The last row is the year's total; click a month to see its days.
The meter is read every recording interval and the energy is stored per day and tariff.
Each month keeps its own **price per kWh** for every tariff: it is taken from the tariffs in
Settings when the month's first energy is recorded, so changing the tariffs later does not
change months already started. Edit a month's price right in the table and that month's
costs follow; **Use current prices** sets a month to the tariffs in Settings (with a single
price the month becomes one total under that tariff). When a meter is set for the first
time, this month so far is taken from the Home Assistant history. A lifetime counter
(Shelly) and a counter that resets daily both work.

### Logs

**Backup**: *Download backup* saves a zip with the database (readings, predictions,
monthly bill), the settings (sensors, tariffs, programs, notifications, API keys) and the
control state. *Restore from backup…* uploads such a zip into a new or reinstalled add-on
and replaces all of its data and settings; the database it replaces is kept once as
`batteryai.db.before-restore`. The local LLM model is not included. The file contains your
API keys, so keep it private.

The add-on's recent log lines (filter by level, search, live update) and the storage
used: database size and rows, how far back the data goes, everything in `/data`, and
free disk space. *Compress old readings* and *Compact database* shrink the database.

### Settings

All settings are stored in `/data/settings.json` and apply immediately when saved.

- **Home Assistant connection** – test shows the version and time zone, or why the
  add-on cannot read data.
- **Prediction engine** – *Claude* (cloud), *ChatGPT* (OpenAI cloud), *Local fast*
  (statistics + rules, light CPU) or *Local slow* (Qwen2.5 3B LLM on the CPU). Also switchable from the header
  dropdown. See *Prediction engines* below.
- **Claude** – API key (never sent back to the browser), model (dropdown with the models
  your key can use; also in the header), effort, language.
- **ChatGPT (OpenAI)** – OpenAI API key (from platform.openai.com; never sent back to the
  browser), model (dropdown with the chat models your key can use; also in the header)
  and reasoning effort for reasoning models (GPT-5, o-series). ChatGPT gets exactly the
  same data, instructions and answer format as Claude. A ChatGPT Plus subscription does
  not include API access – the API is billed separately per token.
- **Prediction schedule** – the times of the daily runs (default 12:00 and 23:00), or
  fill them evenly with *N per day from HH:MM*. Recording interval, history days,
  weekend days.
- **Prediction tuning**
  - *Safety margin* – Claude plans for the predicted consumption plus this %.
  - *Lowest / highest SOC* – Claude's SOC values are kept within this range.
  - *Apply threshold* – a program's SOC is only rewritten when the new value differs by
    at least this many %.
  - *Charge all SOC* – the value used by *Charge all* (98% by default).
  - *Solar forecast correction* – 1–200%; today's and tomorrow's solar forecast are
    multiplied by it before anything uses them (110 = PV comes out 10% above the forecast
    in your home). Readings already stored keep the value they were recorded with.
- **Sensors** – battery SOC, solar forecast today/tomorrow, PV power (W), PV production
  today (kWh), load power (W, kW is converted), consumption today (kWh), grid import
  today (kWh), grid energy meter for the monthly bill (kWh, e.g. Shelly EM), weather,
  probable outages.
- **Appliances** – your own list of devices with a power sensor (W or kW is converted):
  a name, the sensor, and *Depends on outdoor temperature* for anything that heats or
  cools (heat pump, AC). Add rows with **+ Add appliance**, remove them with **−**.
  Each appliance gets its own tile, chart line and daily bar, and the predictions
  forecast when it will run. Renaming keeps its history; a newly added appliance starts
  recording now (or use Settings → History → Import to fill it from Home Assistant).
- **Deye programs** – for each of the 6 programs: time entity, SOC entity (number,
  input_number or select) and an optional grid charge switch. *Program time
  marks* says how the inverter reads the times: **start** (default, as Deye does it) – a
  program runs from its own time until the next program's time, and P6 runs until P1
  (P1 00:00, P2 03:00 = 00:00 – 03:00; P6 21:00 = 21:00 – 00:00); **end** – a program's
  time is the end of its range, which starts at the previous program's time. Two
  programs with the same time leave one of them unused. BatteryAI never
  changes program times, only SOC and grid charge.
- **Tariffs** – the currency and your own list of tariffs (up to 8): a name, the price
  per kWh and the time windows, such as `23:00-07:00` or several separated by commas
  (`07:00-08:00, 11:00-17:00`). Exactly one tariff is marked **All other times** and
  needs no windows. Add rows with **+ Add tariff**, remove them with **−**. Keep just
  one tariff for a **single price all day**: charging from the grid then saves nothing,
  so the battery is only used for PV and kept for outages, never charged from the grid
  for price reasons. The
  dashboard shows the current tariff, the Economy tab splits grid energy per tariff, and
  every prediction engine charges from the grid in the cheapest tariff. Settings from
  older versions become *Off-peak* (your off-peak windows and price) and *Peak* (all
  other times).
- **Phone notifications** – one or more notify services (phones from the Home Assistant
  Companion app appear as `mobile_app_…`), with a test button. Notifications are normal
  priority, not critical alerts: every prediction, every SOC / grid-charge change, and
  failed predictions can each be switched on or off. Each prediction sends **one**
  notification: the first lines are a brief (tomorrow's use, solar and lowest battery,
  and whether the plan was written to the inverter); pull it down (or long-press it on an
  iPhone) to see the summary and every program with its time range, SOC, grid charge and
  previous value. Tap it, or its **Open BatteryAI** button, to open the panel. A new plan
  replaces the previous plan notification; changes and errors stay as separate
  notifications.
- **History** – rebuild the data from the Home Assistant recorder (10 days by default).
- **Extra instructions** for the AI.

Every entity has a **Test** button showing its current value or why it fails.

## Prediction engines

| Engine | Runs | Speed / load | Quality |
| --- | --- | --- | --- |
| Claude | Anthropic cloud, API key | ~1 min, nothing local | Best: reads all history, weather, outages, tariffs |
| ChatGPT | OpenAI cloud, API key | ~1 min, nothing local | Same data and instructions as Claude |
| Local fast | inside the add-on | instant, negligible CPU | Solid baseline: similar-day forecast + fixed rules |
| Local slow | inside the add-on (llama.cpp) | minutes, ~3 GB RAM, all cores | Plans and explains like an AI, from the local fast forecast |

**Local fast** averages the hourly load of the 5 most similar recorded days (same
weekday/weekend type, closest outdoor temperature, recent days preferred), scales heat
pump use with a temperature regression, and shapes PV from your recorded PV power. For
each program in the cheapest tariff it keeps enough energy (plus the safety margin, using
*Battery capacity*) for the following pricier hours that PV will not cover; other programs let the
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

## Charge before an outage

Set *Minutes to outage* (Settings → Sensors; the minutes and SOC are in Prediction tuning), for example
`sensor.svitlo_kyiv_4_1_minutes_to_outage`) and turn on **Charge before outages** in the
Battery control card. When the next outage is *Charge before an outage* minutes away (90
by default), every program is set to the *Pre-outage SOC* (100% by default) with grid
charge on, and you get a notification. When the sensor points to a later outage again,
the previous mode comes back: AI auto-control re-applies the latest prediction, and in
advice-only mode the SOC values from before are restored. Turning AI auto-control on or
pressing *Charge all* ends it early; it then does not start again for the same outage.
The Probable outages tile shows how long until the next outage.

**Tariff-aware** (switch under *Charge before outages*, with several tariffs and an
*Outage duration* sensor such as `sensor.svitlo_kiivska_oblast_2_2_longest_continuous_outage`;
a number in hours or minutes by its unit, or H:MM): instead of always charging to the
pre-outage SOC, BatteryAI works out the energy needed from the recorded average load per
hour (plus the safety margin):
- outage in a pricier tariff: it charges only if the battery cannot last from now until
  the outage ends, and only what the outage itself needs – topping up waits for the cheap
  hours;
- outage in the cheapest tariff: it charges for the whole outage, and if the outage runs
  past the cheap hours, also for the time until they return.
The Battery control card shows the decision and the reason before the outage. While the optional
*Emergency outages* sensor is on (on / true / active, or a number above 0), tariffs are
ignored and the battery is charged to the pre-outage SOC.

## History and database size

All history is kept – nothing is deleted. *History sent to Claude* only limits the daily
detail in each prediction; every prediction also gets a summary of every recorded month,
and (after a year) the same weeks of last year, because PV and usage change with the
season. The Economy tab shows the same monthly summary under *By month*.

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
