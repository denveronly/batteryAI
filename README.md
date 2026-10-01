# BatteryAI – Home Assistant add-on

Records battery, solar forecast, load and outage data from Home Assistant and uses Claude
to predict consumption and plan the SOC of the six Deye time-of-use programs.

## Install

1. In Home Assistant open **Settings → Add-ons → Add-on store**.
2. Menu (⋮) → **Repositories** → add `https://github.com/denveronly/batteryai`.
3. Install **BatteryAI**, open the **Configuration** tab, set your Claude API key and
   entity IDs, then start it.
4. Open **BatteryAI** from the sidebar.

See [batteryai/DOCS.md](batteryai/DOCS.md) for all options.

## Layout

```
repository.yaml           add-on repository metadata
batteryai/
  config.yaml             add-on manifest and option schema
  build.yaml, Dockerfile  container build
  translations/en.yaml    option labels in the HA UI
  app/
    main.py               web server, recorder loop, analysis scheduler
    collector.py          reads the configured entities
    db.py                 SQLite storage (/data/batteryai.db)
    analyzer.py           Claude request and response schema
    ha.py                 Home Assistant REST client
    static/               panel UI (Chart.js)
```

## Local development

```sh
cd batteryai/app
pip install -r ../requirements.txt
mkdir -p dev-data && export BATTERYAI_DATA=$PWD/dev-data
export BATTERYAI_OPTIONS=/path/to/options.json   # same keys as config.yaml options
export HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token>
python main.py   # UI on http://localhost:8099
```

The UI expects Chart.js at `app/static/vendor/chart.umd.min.js`; the Docker build
downloads it.
