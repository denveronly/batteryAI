# BatteryAI

BatteryAI records your battery, solar forecast, load and outage data into a local
database and asks Claude, a few times a day, to predict consumption and suggest SOC
capacities for the six Deye time-of-use programs.

## What it does

- **Records** the configured sensors at a set interval into SQLite
  (`/data/batteryai.db`, included in add-on backups): battery SOC, today/tomorrow solar
  forecast, today load, today consumption, probable outages (state and attributes), all
  six Deye program times and SOC capacities, plus the day of the week and whether it is a
  weekend.
- **Analyses** the data with Claude a configurable number of times a day, starting at
  the first analysis time and spread evenly over 24 hours (2 per day at 06:00 gives 06:00 and
  18:00). Each analysis gets the current values, per-day totals for the last
  history days, hourly samples for the last 48 hours and its own previous predictions.
- **Shows** a panel in the Home Assistant sidebar with:
  - a battery chart (SOC vs. the SOC of the Deye program active at that moment),
  - a load chart (today load and today consumption counters),
  - a daily energy chart (load, consumption, solar forecast; weekends shaded),
  - the current Deye program table,
  - the AI analysis log: every run with its predictions, outage risk, suggested Deye
    programs, recommendations, reasoning and the exact data sent to Claude.

BatteryAI only reads Home Assistant entities. It does **not** change inverter settings;
the Deye program suggestions are advice you apply yourself.

## Settings

All settings are in the **Settings** tab of the BatteryAI panel (the add-on's
Configuration tab is empty). Changes apply immediately when you press **Save settings**;
no restart is needed. Settings are stored in `/data/settings.json`. If you used version
0.1.0, BatteryAI imports the old add-on options on first start if the Supervisor still
provides them; otherwise enter them again in the Settings tab.

- **Home Assistant connection** – *Test connection* shows the Home Assistant version and
  time zone, or the exact reason the add-on cannot read data (no token, token refused,
  Home Assistant unreachable).
- **Claude** – API key, model, analysis effort, response language. *Test Claude* checks
  the key and the model without spending tokens. The saved key is never sent back to the
  browser; leave the field empty to keep it.
- **Schedule** – analyses per day (1–24), first analysis time, recording interval, days of
  history sent to Claude, and which days count as weekend.
- **Sensors** – today/tomorrow solar forecast, battery SOC, probable outages (optional),
  today load and today consumption.
- **Deye programs** – for each of the 6 programs, the start-time entity and the SOC
  capacity entity.
- **Extra instructions** – free-text notes for Claude (tariffs, backup preferences…).

Every entity field has a **Test** button (and is tested automatically when the tab opens
or the field changes). It shows the current value and unit, or why it fails:

| Result | Meaning |
| --- | --- |
| ✓ value | The entity is read correctly. |
| ⚠ unavailable / unknown | The entity exists, but its integration is not delivering data. |
| ⚠ not numeric / not a time | The entity works but holds the wrong kind of value. |
| ✕ does not exist | No entity with that ID; check for typos (the field suggests IDs as you type). |
| ✕ token / connection error | The add-on cannot talk to Home Assistant at all; see the connection test. |

Deye time entities may report `01:00`, `01:00:00` or `100`; all three are understood.

## Theme

The ◐/☀/☾ button in the header switches between automatic (follows your system), light
and dark. The choice is remembered in your browser.

## Costs

Each analysis is a single Claude API request containing a few thousand tokens of data.
With 2 analyses a day the cost is small, but it scales with analyses per day, history
days and effort. Token usage for every run is shown in the log.
