# BatteryAI – smart battery planning for Deye inverters in Home Assistant

**English** · [Українська](README.uk.md)

BatteryAI is a Home Assistant add-on for homes with solar panels, a battery and a **Deye
hybrid inverter**. It records data about your battery, solar, load, appliances, weather,
tariffs and outages. From that it predicts consumption and solar production, and plans the
**SOC and grid charge of the inverter's six time-of-use programs**. With AI auto-control
on, the plan is written to the inverter automatically: at the scheduled prediction times
(12:00 and 23:00 by default) and whenever an outage is coming.

![Dashboard](docs/screenshots/dashboard.png)

## What it does

- **Predicts** consumption, solar production and the lowest battery level. It uses all
  your recorded history, weekday/weekend patterns, the weather forecast and the season.
- **Plans the six Deye programs**: the SOC for each time range and whether grid charge is
  on. The battery charges in the cheapest tariff, PV is used first, and enough charge is
  kept for outages.
- **Controls the inverter** (optional): every prediction is written to the inverter. You
  can also set each program's SOC and grid charge by hand on the dashboard.
- **Outages**:
  - Charges the battery before scheduled outages, read from a "minutes to outage" sensor.
  - Charges straight away during emergency outages.
  - Learns from a grid status sensor when outages usually come.
- **Tariffs**: any number of named tariffs, each with its own price and time windows (for
  example off-peak 23:00–07:00 and peak), or a single price all day.
- **Economy**:
  - What you paid for grid energy.
  - What PV and the battery saved.
  - A monthly electricity bill split by tariff, and the cost of each appliance.
- **Phone notifications** through the Home Assistant Companion app. Each one starts with a
  short summary; pull it down to see every program. A button opens BatteryAI.
- **Four prediction engines**:
  - Claude and ChatGPT (cloud, your API key).
  - *Local fast*: statistics and rules, no internet.
  - *Local slow*: a small LLM that runs inside the add-on.

## Screenshots

| Battery control | Latest prediction |
| --- | --- |
| ![Battery control](docs/screenshots/battery-control.png) | ![Latest prediction](docs/screenshots/prediction.png) |

**Power chart** – load, PV, appliances, outdoor temperature and the predicted load:

![Power chart](docs/screenshots/chart-power.png)

**Economy** – what was saved by PV and by the battery & AI plan:

![Economy](docs/screenshots/economy.png)

**Monthly bill** – grid energy and cost per tariff:

![Monthly bill](docs/screenshots/bill.png)

| Tariffs | Phone |
| --- | --- |
| ![Tariffs](docs/screenshots/settings-tariffs.png) | ![Mobile](docs/screenshots/mobile.png) |

## Requirements

- **Home Assistant OS or Supervised.** Add-ons need one of these. Runs on aarch64 (e.g.
  Raspberry Pi 4/5) or amd64.
- **A Deye hybrid inverter in Home Assistant whose programs can be changed**, for example
  through the Solarman integration. Each of the 6 programs needs:
  - a time entity;
  - a SOC entity (number, input_number or select);
  - optionally, a grid-charge switch or charge-source select.
- **Sensors**: battery SOC, load power and PV. A solar forecast (e.g. Solcast or
  Forecast.Solar) and a `weather.*` entity are strongly recommended.
- **For Claude or ChatGPT**: an API key from console.anthropic.com or platform.openai.com,
  billed per request. The local engines need no key and no internet.

## Install

1. In Home Assistant open **Settings → Add-ons → Add-on store**.
2. Menu (⋮) → **Repositories** → add `https://github.com/denveronly/batteryai`.
3. Install **BatteryAI** and start it. Turn on **Show in sidebar**.
4. Open **BatteryAI** from the sidebar and go to **Settings**.

## First setup

1. **Sensors**: enter your entities. Every field has a **Test** button that shows the
   current value or why it cannot be read.
2. **Deye programs**: the time, SOC and grid-charge entity of each of the 6 programs.
   Each program runs from its own time until the next program's time; P6 runs until P1.
3. **Tariffs**: the currency, plus prices and time windows (**+ Add tariff**). Keep a
   single tariff for one price all day.
4. **Prediction engine**: choose Claude, ChatGPT, Local fast or Local slow. For the cloud
   engines, paste the API key and press **Test**.
5. **Phone notifications**: choose your phone (`mobile_app_…`) and send a test.
6. **Save settings**. History is imported from the Home Assistant recorder, so charts and
   predictions work right away.
7. On the **Dashboard**, press **Predict now** and check the plan. When you are happy with
   it, turn on **AI auto-control**.

## Prediction engines

| Engine | Where it runs | Speed | Notes |
| --- | --- | --- | --- |
| Claude | Anthropic cloud, API key | ~1 min | Reads all history, weather, outages and tariffs |
| ChatGPT | OpenAI cloud, API key | ~1 min | Same data, instructions and answer format as Claude |
| Local fast | inside the add-on | instant | Similar-day forecast and fixed rules; no internet |
| Local slow | inside the add-on (llama.cpp) | minutes, ~3 GB RAM | Qwen2.5 3B LLM; the model (~2 GB) is downloaded once |

## Safety

- BatteryAI writes to the inverter only in these cases:
  - **AI auto-control** is on;
  - **Apply to inverter** or **Charge all** is pressed;
  - you change a program by hand;
  - a protective rule you switched on in Battery control fires: *Charge on emergency
    outages*, *Charge before scheduled outages* or *Cell Balance to 100% in morning*.
- It changes only each program's **SOC and grid charge**, never the program times or
  generator charging.
- Every write respects the entity's min/max/step and the SOC range in *Prediction tuning*.
  It is listed in the analysis log and can be sent to your phone.
- If the add-on stops, the inverter keeps its last programs.

## Data and backup

All data stays in your Home Assistant, in an SQLite database under `/data`. Old readings
are compressed to hourly values, so a year of data takes only a few MB. **Logs → Backup**
downloads a zip with the database and settings, and can restore it into a new installation.
The zip includes your API keys, so keep it private.

## Documentation

- [batteryai/DOCS.md](batteryai/DOCS.md) describes every option and rule. It is also shown
  in the add-on's **Documentation** tab.
- [batteryai/CHANGELOG.md](batteryai/CHANGELOG.md) lists the changes in each version.

## Repository layout

```
repository.yaml            add-on repository metadata
docs/screenshots/          images used in this README
batteryai/
  config.yaml              add-on manifest (version, ingress, architectures)
  build.yaml, Dockerfile   container build (Python 3.12, llama.cpp for the local LLM)
  DOCS.md, CHANGELOG.md    documentation and release notes
  app/
    main.py                web server, recorder, prediction scheduler, HTTP API
    config.py              settings validation and storage (/data/settings.json)
    collector.py           reads the configured Home Assistant entities
    ha.py                  Home Assistant REST client (through the Supervisor)
    db.py                  SQLite storage, daily/monthly summaries, economy, accuracy
    history.py             imports past values from the Home Assistant recorder
    planning.py            planning rules shared by every engine
    analyzer.py            Claude input, instructions and answer schema
    openai_engine.py       ChatGPT engine (OpenAI Chat Completions)
    local_fast.py          local engine: similar-day forecast and rule planner
    local_llm.py           local engine: Qwen2.5 3B via llama.cpp
    llm_worker.py          runs one local LLM request in its own process
    control.py             writes SOC / grid charge to the Deye programs
    outage_plan.py         tariff-aware charging before outages
    grid.py                grid availability and outage history
    balance.py             morning cell balance to 100%
    bill.py                monthly bill from a grid energy meter
    backup.py              backup and restore of /data
    notify.py              phone notifications
    static/                panel UI (dashboard, economy, settings, logs; bundled Chart.js)
```

## Local development

```sh
cd batteryai/app
pip install -r ../requirements.txt
mkdir -p dev-data && export BATTERYAI_DATA=$PWD/dev-data
export HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived access token>
python main.py   # UI on http://localhost:8099
```
