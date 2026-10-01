# BatteryAI

BatteryAI records your battery, solar forecast, load and outage data into a local
database and asks Claude, a few times a day, to predict consumption and suggest SOC
capacities for the six Deye time-of-use programs.

## What it does

- **Records** every `record_interval_minutes` the configured sensors into SQLite
  (`/data/batteryai.db`, included in add-on backups): battery SOC, today/tomorrow solar
  forecast, today load, today consumption, probable outages (state and attributes), all
  six Deye program times and SOC capacities, plus the day of the week and whether it is a
  weekend.
- **Analyses** the data with Claude `analyses_per_day` times a day, starting at
  `first_analysis_time` and spread evenly over 24 hours (2 per day at 06:00 gives 06:00 and
  18:00). Each analysis gets the current values, per-day totals for the last
  `history_days`, hourly samples for the last 48 hours and its own previous predictions.
- **Shows** a panel in the Home Assistant sidebar with:
  - a battery chart (SOC vs. the SOC of the Deye program active at that moment),
  - a load chart (today load and today consumption counters),
  - a daily energy chart (load, consumption, solar forecast; weekends shaded),
  - the current Deye program table,
  - the AI analysis log: every run with its predictions, outage risk, suggested Deye
    programs, recommendations, reasoning and the exact data sent to Claude.

BatteryAI only reads Home Assistant entities. It does **not** change inverter settings;
the Deye program suggestions are advice you apply yourself.

## Configuration

| Option | Description |
| --- | --- |
| `claude_api_key` | Your Anthropic API key from <https://console.anthropic.com>. |
| `claude_model` | Default `claude-opus-5-5`. |
| `claude_effort` | `low` … `max`. Default `high`. |
| `response_language` | Language of the analysis text. |
| `analyses_per_day` | 1–24 analyses per day. |
| `first_analysis_time` | `HH:MM` of the first daily analysis, in the Home Assistant time zone. |
| `record_interval_minutes` | How often readings are stored. |
| `history_days` | Days of history included in each analysis. |
| `today_forecast_sensor` / `tomorrow_forecast_sensor` | Solar forecast sensors (kWh). |
| `battery_soc_sensor` | Battery SOC (%). |
| `outages_sensor` | Probable outages sensor (optional). |
| `today_load_sensor` / `today_consumption_sensor` | Daily energy counters (kWh) that reset at midnight. |
| `weekend_days` | Which days count as weekend. The weekday itself is detected automatically. |
| `deye_programs` | Six entries, each with the `time_entity` (program start time) and `soc_entity` (program SOC capacity). |
| `extra_instructions` | Optional notes for Claude: tariffs, backup preferences, etc. |

The defaults contain example entity IDs. Replace them with the IDs from your own
installation (Settings → Devices & services → Entities). Entities that cannot be read are
listed in a warning at the top of the panel.

Deye time entities may report `01:00`, `01:00:00` or `100`; all three are understood.

## Costs

Each analysis is a single Claude API request containing a few thousand tokens of data.
With 2 analyses a day the cost is small, but it scales with `analyses_per_day`,
`history_days` and `claude_effort`. Token usage for every run is shown in the log.
