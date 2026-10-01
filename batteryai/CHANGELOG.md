# Changelog

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
