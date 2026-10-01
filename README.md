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
    static/               panel UI: dashboard + settings (Chart.js)
```

## Local development

```sh
cd batteryai/app
pip install -r ../requirements.txt
mkdir -p dev-data && export BATTERYAI_DATA=$PWD/dev-data
export HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token>
python main.py   # UI on http://localhost:8099
```

The UI expects Chart.js at `app/static/vendor/chart.umd.min.js`; the Docker build
downloads it.
