# Changelog

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
