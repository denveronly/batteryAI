# BatteryAI – Home Assistant add-on

Records battery, solar forecast, load and outage data from Home Assistant and uses Claude
to predict consumption and plan the SOC of the six Deye time-of-use programs.

## Install

1. In Home Assistant open **Settings → Add-ons → Add-on store**.
2. Menu (⋮) → **Repositories** → add `https://github.com/denveronly/batteryai`.
3. Install and start **BatteryAI**.
4. Open **BatteryAI** from the sidebar, go to **Settings**, enter your Claude API key and
   entity IDs, use the Test buttons to check them, and save.

See [batteryai/DOCS.md](batteryai/DOCS.md) for all options.

## Layout

```
repository.yaml           add-on repository metadata
batteryai/
  config.yaml             add-on manifest and option schema
  build.yaml, Dockerfile  container build
  app/
    main.py               web server, recorder loop, analysis scheduler
    collector.py          reads the configured entities
    db.py                 SQLite storage (/data/batteryai.db)
    analyzer.py           Claude request and response schema
    ha.py                 Home Assistant REST client
    config.py             settings validation and storage (/data/settings.json)
    control.py            writes SOC / grid charge to the Deye programs
    history.py            imports past values from the HA recorder
    notify.py             phone notifications
    local_fast.py         local engine: similar-day forecast + rule planner
    local_llm.py          local engine: Qwen2.5 3B via llama.cpp (model download, worker)
    llm_worker.py         runs one local LLM request in its own process
    static/               panel UI: dashboard, economy, settings (bundled Chart.js)
```

## Local development

```sh
cd batteryai/app
pip install -r ../requirements.txt
mkdir -p dev-data && export BATTERYAI_DATA=$PWD/dev-data
export HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token>
python main.py   # UI on http://localhost:8099
```


## Standalone container (Docker, Umbrel, Portainer)

Without the Home Assistant add-on store (for example Home Assistant running as an app on
Umbrel), run BatteryAI as a container. GitHub builds the image on every push to `main`:

`ghcr.io/denveronly/batteryai:latest` (amd64 and arm64; also tagged with the version)

1. Use [`deploy/docker-compose.yml`](deploy/docker-compose.yml) – in Portainer (*Stacks →
   Add stack*), or over SSH with `docker compose up -d`.
2. Open `http://<host>:8099`.
3. Settings → **Home Assistant connection**: the Home Assistant URL (on Umbrel use the
   Umbrel's IP address, e.g. `http://192.168.1.50:8123` – `umbrel.local` often does not
   resolve inside containers) and a **long-lived access token** (Home Assistant → your
   profile → Security → Long-lived access tokens). Or set `HA_URL` / `HA_TOKEN` in the
   compose file instead.
4. Everything else is configured in the panel as with the add-on. Data and settings live in
   the `/data` volume (`./batteryai-data`); backups from the Logs tab work the same and can
   be restored into either version.

Differences to the add-on: no Home Assistant sidebar panel (open the port directly),
notifications have no "Open BatteryAI" link, and the "Local slow" LLM engine is not in the
image (Claude, ChatGPT and Local fast work).
