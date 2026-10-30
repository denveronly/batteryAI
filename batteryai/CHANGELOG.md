# Changelog

## 0.4.56

- Battery alarms as one **🚨 chip** in the header: green when all is well; red and flashing
  with the alarm(s) that occurred (e.g. "🚨 Cell under-voltage, Charge over-temperature").
  The alarm list (dropdown) shows active ones first.
- **Wire resistances** dropdown per battery: R01…R16 with the highest outlined red and in the
  title ("R05 max 0.244 mΩ"). A separate `…_wire_resistance_NN` sensor is shown too when the
  BMS has one; cell boxes now show the voltage only.

## 0.4.55

- Battery header, compact: a dot before the name (green online, red offline), no prefix;
  **CHG / DCHG** green when on, red when off; balancing a ♥ chip, green when on; heating 🔥 and
  precharging ⏳ as icons only, grey when off.
- Two batteries side by side on wide screens (Battery health and the Dashboard card).
- Short dropdown titles: "Cells (16) · Δ 12 mV · V11 min 3.312 V · V10 max 3.323 V",
  "Temperatures (6) · T2 max 25.1 °C".

## 0.4.54

- Economy *Cost by day* chart: Grid paid in red, like the grid figures in the tables.

## 0.4.53

- **Prediction strictness** slider (Settings → Prediction tuning), −10 relaxed … 0 … +10
  strict, applied to every engine and rule:
  - outage reserves × 0.5 (−10) … × 1.5 (+10): the outage cap, expected outages, local fast
    and the tariff-aware pre-outage charge;
  - *Leave room for PV* × 2 (−10) … × 0 (+10);
  - positive values: every program at least the minimum SOC + 3 % per step (+10: +30 %);
  - Claude, ChatGPT and the local LLM are told the strictness too.
  The note under the slider says what the current position does.

## 0.4.52

- **Battery line** on the Battery health tab and, new, on the **Dashboard** (Batteries card,
  every 15 s): name, Online, alarms, status chips with icons – ⚖️ Balancing, 🔌 Charging,
  🔋 Discharging, ⏳ Precharging, 🔥 Heating, green when on – SOC, voltage and a **power bar**
  with 0 in the middle: discharging to the left (amber, red when high), charging to the right
  (green).
- Battery health: cell voltages, temperatures, other sensors and the alarm list are dropdowns
  (they stay open across refreshes); active alarms and abnormal data stay visible on top.
- Status and temperature names no longer carry the bridge's device name ("Esp-Jk-Bridge-Rs485
  bms0 …"); the power tube *protection* settings exposed as sensors are no longer shown as
  temperatures.
- Economy: appliance emojis show without reloading the page after changing them.
- Settings: inputs sit right under their titles and line up across a row; number boxes are
  compact.

## 0.4.51

- Monthly bill tiles: PV before the Power bill.

## 0.4.50

- Battery health → **Control**: pick a battery from the list and see the BMS's own switches
  (`switch.<prefix>_*`: balancing, charging, discharging, float mode, …) and settings
  (`number.<prefix>_*`: cell voltages, balancing, currents, temperatures, delays), grouped,
  with the current value and the allowed range. Locked until *Allow changes* is ticked; every
  change asks for confirmation, must stay within the BMS's min/max, waits for the BMS to confirm
  and is written to the Battery log.
- Settings → Batteries: the first battery is **BMS0 / bms0**, the next ones BMS1 / bms1, BMS2 /
  bms2…; typing a name like "BMS3" fills in the prefix. With no batteries yet, BMS0 is added by
  itself when Home Assistant has `bms0` entities.

## 0.4.49

- New **Battery health** tab (after Economy), for 1–20 battery packs (Settings →
  **Batteries**: a name and the BMS entity prefix; *+ Add battery* continues the numbering,
  bms0 → bms1 → bms2). The entities are found by the prefix – no need to enter them one by one:
  - SOC (`_battery_capacity_state_of_charge`), voltage, current, power, cell delta, error
    bitmask, charging cycles, cell count (`_cell_count_real`), BMS active alarms;
  - every cell's voltage and resistance (`_cell_voltage_NN`, `_cell_resistance_NN`), with the
    lowest cell outlined blue and the highest red;
  - temperatures (`_temperature_powertube`, `_temperature_sensor_N`, …);
  - `binary_sensor.<prefix>_status_online` and other `_status_*`, and every
    `binary_sensor.<prefix>_alarm_*` with readable names (active ones on top).
  Refreshed every 10 s.
- **Battery log** on the tab: alarms on / cleared, BMS offline / back online, error bitmask,
  a changed cell count, and abnormal data – a cell below 2.90 V or above 3.62 V, cell delta
  above 50 mV, a cell's resistance above 2× the pack's median, temperatures above 50 °C (power
  tube 70 °C) or below 0 °C, BMS values unavailable – each when it starts and when it ends.
- **Phone notifications** for battery alarms (Settings → Phone notifications → *battery
  alarms*, on by default): the battery, the alarm and its code when an alarm turns on or
  clears, the error bitmask changes, the cell count changes or a BMS goes offline. Abnormal
  data is logged only.

## 0.4.48

- Monthly bill: the bill and the appliances tables share one layout – Month · Source ·
  Peak kWh · Peak cost · Off-peak kWh · Off-peak cost · kWh · Cost · Share of bill – with the
  same column widths, so they line up. The meter is the "⚡ Grid" row; each month's prices and
  *Use current prices* are under the month name.

## 0.4.47

- **Appliance emoji** (Settings → Appliances): pick one from the list or paste any; it is
  shown before the appliance's name in the tiles, charts, predictions and the bill.
- Monthly bill: the *Power bill* tile light red and the *PV* tile light yellow; in the bill
  and appliance tables Peak columns light red, Off-peak light purple and the grid cost in red
  – the same colours as the Economy tab.

## 0.4.46

- Monthly bill tiles are this month only: each tariff (Peak, Off-peak), the **Power bill**
  and **PV** (production × peak price), amount and kWh on one line. The year tiles are gone
  (the year total stays at the bottom of the table).

## 0.4.45

- Economy days table: tinted column blocks – Peak light red, Off-peak light purple, PV and
  battery light yellow – and Grid total in red.

## 0.4.44

- **Strict above** is a number again (1–24 hours).
- New sensor **Outage hours today** (Settings → Sensors, e.g.
  `sensor.dtek_..._outage_hours_today`): today's scheduled hours without power. Strict mode
  compares the limit with yesterday's recorded hours (Grid status sensor) and today's –
  recorded so far or scheduled, whichever is more. Works without the Grid status sensor too
  (scheduled hours only). The Grid card shows the scheduled hours.
- Economy: the battery discharge is the day's last reading of the counter, and values no
  battery can discharge in a day (> 1000 kWh, e.g. a lifetime total entered by mistake) are
  ignored – a wrong sensor no longer spoils the whole day.

## 0.4.43

- Fix: switching **Gen charge** on the dashboard failed with HTTP 500 after the inverter had
  already changed – the phone notification could not format a generator change. Notifications
  now show it ("P3 generator charge Both → Grid"), and an unexpected value can no longer
  break a request.

## 0.4.42

- **Economy tab, new layout** per day and in total for the period:
  - per grid tariff (Peak, then Off-peak): kWh · price paid · money; **Grid total**;
  - **PV**: kWh (PV production counter) · peak price · money;
  - **Battery discharged**: kWh · off-peak price · money;
  - **Inverter savings** = PV money + battery money.
  Tiles and the chart follow the same split.
- New optional sensor **Battery discharge today** (Settings → Sensors; kWh counter, e.g. the
  Deye daily battery discharge). It is recorded from now on; days before it show "—".

## 0.4.41

- Deye programs table: a program whose charge entity is a **switch** has a Grid charge
  toggle only. With a **charge-source select** (Disabled / Grid / Generator / Both) it gets
  **Grid** and **Gen** toggles; each changes only its own part (e.g. Generator + Grid on =
  Both, Both + Gen off = Grid). Predictions, the pre-outage charge and Cell Balance still only
  change the grid part – generator charging changes only from its toggle.

## 0.4.40

- Charge-source selects with **Disabled / Grid / Generator / Both** (Solarman "Program N
  Charging") are understood: *Both* counts as grid charge on, *Generator* as off. Switching
  grid charge keeps the generator part (Generator + on = Both, Both + off = Generator). One
  shared reading for the dashboard and the control logic (the dashboard showed *Both* as off).
- Deye programs table: a read-only **Gen charge** column for programs with a charge-source
  select. BatteryAI never changes generator charging – set it on the inverter or in Home
  Assistant.

## 0.4.39

- **Generator charging is kept**: with a Deye charge-source select (Disabled / Grid / Gen /
  Grid & Gen) as a program's grid-charge entity, BatteryAI now only changes the grid part:
  Gen + grid charge on = Grid & Gen, Grid & Gen + off = Gen. "Gen" counts as grid charge off.
  Use the select rather than an on/off switch when the generator charges the battery – a
  switch cannot tell Grid from Gen.

## 0.4.38

- **Manual program changes no longer flip back on the dashboard**: after a change in the
  Deye programs table the add-on waits (up to 20 s) until Home Assistant reports the new
  value – inverter integrations such as Solarman only update after their next poll – before
  redrawing the table. If it is still not confirmed, the result says so (⚠), so a change the
  inverter did not take is visible.

## 0.4.37

- **Pre-outage charge shown as such**: while *Charge before scheduled outages* (or the
  emergency charge) holds the programs, the badge says *Pre-outage charge* / *Emergency
  charge*, AI auto-control stays ticked when it will resume by itself, and the note says when
  and to what it hands back – it no longer looks like a manual *Charge all*.
- *Charge on emergency outages* unticked now also means the pre-outage charge stays
  tariff-aware during emergency outages (before, an emergency sensor that was on made it
  charge to the full pre-outage SOC at any price), and the dashboard no longer says
  "tariffs ignored" then.

## 0.4.36

- *Leave room for PV*: a program that ends before the sun (e.g. 03:00–08:00) now leaves room
  only for the first two hours of PV surplus, less what the load uses before the sun – more
  reserve overnight and in the morning, when outages hit. Programs the sun shines in still
  leave room for the whole sunny stretch (e.g. 100 / 92 / 80 / 85 / 100 / 100 instead of
  95 / 80 / 80 / 85 / 100 / 100).

## 0.4.35

- **Single price: the battery is kept full.** With one price all day, running the battery
  down saves nothing and only risks an outage with an empty battery. Every engine (Claude,
  ChatGPT, local LLM, local fast) now keeps every program at the maximum SOC with grid charge
  on, and *Leave room for PV* lowers only the programs before sunny hours by the surplus the
  sun will bring (e.g. 100 / 80 / 80 / 85 / 100 / 100). Before, they were told to keep the
  daytime SOC low with grid charge off.

## 0.4.34

- **Leave room for PV** (Settings → Prediction tuning, 20% by default, 0 = off): a program's
  SOC is also what the grid charges to, so 100% before sunny hours fills the battery from the
  grid and the sun is throttled (zero export). Every program is now lowered below the maximum
  SOC by the PV surplus still expected after it starts – the recorded PV shape scaled to
  today's / tomorrow's forecast, minus the margin, less what the load uses anyway before the
  sun (e.g. 100 → 95 / 80 / 85%). Never below an outage's need or the minimum SOC; a cloudy
  forecast changes nothing. For every engine; not in strict mode or during the emergency
  charge. Works with *Don't touch grid charge*, where the SOC is the only lever.
- **Local fast and outages**: a scheduled outage no longer keeps every program at 100% all
  day. Only the programs running during it or in the two hours before it hold the energy it
  needs (recorded load × margin) with grid charge on. Without a known start time, or with the
  emergency charge on, the battery is kept full as before.

## 0.4.33

- Settings → **Cell balance**: the check time (06:30) and the battery SOC it needs (above
  95%) for *Cell Balance to 100% in morning* can be changed.
- **Foldable Settings blocks**: click a block's title (Sensors, Deye programs, …) to fold or
  unfold it. Folded blocks are remembered in this browser; a block with a save error opens
  by itself.

## 0.4.32

- **Cell Balance to 100% in morning** (Battery control, a fixed rule without AI): at 06:30,
  if program 1 is running and the battery is above 95%, program 1 is set to 100% with grid
  charge on, so the battery tops up and the BMS can balance the cells. The 100% is held
  while program 1 runs (a prediction that lowers it is put back); when program 1 ends its
  previous SOC and grid charge come back. Bypasses the maximum SOC. Off by default; the
  dashboard shows the last check and a notification is sent when it fires.

## 0.4.31

- **Not strict: programs outside the cheap tariff are capped** at what the next outage needs
  (its expected load plus the margin), or at the minimum SOC when no outage comes before the
  next grid charge. A program's SOC is a floor: holding more only stops the battery and the
  grid covers the load at the higher price. Applies to every engine, so an AI that keeps
  everything high (e.g. 90/85/80/98) is corrected; each program's reason says why.
  Programs in the cheapest tariff are never lowered – that is where the battery charges.
- Not strict: a pricier-tariff night program no longer turns grid charge on because the
  battery may run low before the cheap tariff – that energy costs the same from the grid
  directly, without battery losses.
- Tip: set the *Minimum SOC* in Settings to the lowest level you want to keep (e.g. 40%);
  the cap never goes below it.

## 0.4.30

- **Less strict by default**: a scheduled outage, emergency outages or outages in the last
  days no longer keep the battery full. It is used in the day and evening and recharged at
  night in the cheap tariff; expected outage windows still get the SOC they need.
- **Strict above** (Settings → Prediction tuning) is now a choice of **6 or 8 hours** without
  grid yesterday or today (8 by default; 4 from 0.4.29 becomes 8). Only strict mode makes
  every program high, charges to the maximum before outages and keeps the night reserve.
- **Night reserve** only in strict mode or while emergency outages are on with *Charge on
  emergency outages* ticked, and only in programs that charge from the grid (the cheapest
  tariff). A pricier-tariff program with grid charge off is never held at a high SOC, since
  the grid would cover its load at the higher price.
- *Charge on emergency outages* off now also means the predictions do not plan for them.
- The AI prompt no longer asks to keep the battery full after any outage day.
- A prediction that returns a program twice (e.g. P5 twice, no P6) is fixed: the repeat
  takes the missing slot by its time, or is dropped; missing programs keep their values.

## 0.4.29

- Expected outages **may shift**: each window from yesterday is expected today and
  tomorrow give or take *Outages may shift by* (2 h by default). The battery is charged
  from the earliest possible start and the programs keep grid charge on until the latest
  possible end.
- **Strict mode** when the grid was out more than *Strict above* hours (4 by default)
  yesterday or today: the battery is charged to the maximum before expected outages and
  every program is held at least at the *Strict minimum SOC* (60% by default). The Grid card
  shows the hours without grid and 🔒 strict; the AI predictions are told to be strict.

## 0.4.28

- **Grid status** sensor (Settings → Sensors): a binary_sensor (on = power; device class
  *problem* is reversed), an On-Grid / Off-Grid status or a grid voltage (V).
  - It is checked every minute; every outage is stored with its start, end and duration
    (new database table). On first setup the last 14 days are imported from Home Assistant.
  - Dashboard → **Grid**: availability chart over the chart range, recent outages with their
    durations, the expected windows, and a *Grid* tile (⚠ Off during an outage).
  - **Yesterday's outages are expected again today and tomorrow** at the same times (today's
    so far also tomorrow). Before each expected window on the plan day, the program in
    effect just before it is raised to the SOC the window needs (recorded load × margin)
    with grid charge on.
  - The AI predictions get the last week's outages and the expected windows; real outages
    also count for the night reserve.

## 0.4.27

- **Charge on emergency outages** is enforced while emergency outages are on and the switch
  is ticked: changing the mode by hand (e.g. AI auto-control) starts the emergency charge
  again right away, and every minute the programs are checked – a lower SOC or grid charge
  turned off is put back. Untick the switch to stop it. Before, a mode change stopped it
  until the emergency ended, so predictions could write programs without grid charge.

## 0.4.26

- Night grid charge with **several tariffs** follows the tariffs:
  - programs in the cheapest tariff: grid charge on;
  - programs in a pricier tariff: grid charge only when the battery would not last until
    the cheapest tariff begins (battery now, sun until then and the recorded consumption
    and PV per hour, plus the safety margin), and then only to the SOC that needs;
  - in the cheap tariff now (e.g. the 23:00 run): when tomorrow's pricier hours would run
    short, tonight's cheap programs charge more instead of charging at the higher price.
- With a single tariff the night programs keep grid charge on, as before. The night reserve
  when outages are likely and *Don't touch grid charge* work as before.

## 0.4.25

- **Grid charge as a select**: Deye integrations that expose a program's grid charge as a
  select (Disabled / Grid / Gen / Grid & Gen) now work everywhere – Charge all, outage
  charging, predictions, the program table and restoring afterwards. Before, only
  switches worked, so Charge all could not turn grid charge on.
- **Don't touch grid charge** (Battery control): predictions only set the SOC; the
  grid-charge switches stay as you set them. Charge all and outage charging still turn
  them on.
- **Plan day**: a prediction run before 13:00 plans **today** (its programs are still
  ahead), later runs plan tomorrow. The prediction, the notification and the accuracy
  report say and use which day.
- **Night**: evening and night programs (hours without PV, from your recorded PV) always
  keep grid charge on, so the battery can recharge for an unplanned emergency outage.
  When outages are likely – scheduled, emergency, or recorded in the last 7 days – they are
  also kept at the **night reserve** SOC (Settings → Prediction tuning, 100% by default,
  0 = off).

## 0.4.24

- Fix: appliances with an energy sensor (kWh / Wh) were read as power (W), so the
  Monthly bill showed almost 0 kWh for them. Such a sensor is now used as a kWh counter.
- Appliances get an optional **Energy sensor (kWh)** (Settings → Appliances): a total,
  daily or monthly counter (resets are handled). With it, the Monthly bill uses its
  increase, split by tariff; without it, the power sensor is integrated. No separate peak /
  off-peak sensors are needed – BatteryAI splits by the tariff hours itself.

## 0.4.23

- Logs → Storage shows the **whole space the add-on uses**: *Add-on total*, its data and
  its program, and a breakdown (database, local LLM model, other data, BatteryAI code,
  Python packages, base system). Measured in the background every 15 minutes.

## 0.4.22

- The *Probable outages* setting is gone. Probable outages are now derived: **on** while
  *Minutes to outage* shows a scheduled outage, **off** when it is unknown or 9999 (none
  scheduled); emergency outages count too. A sensor entered there before that is an
  emergency indicator (`…emergency…`) moves to *Emergency outages* automatically.
- The AI predictions get the probable outages as on/off with the minutes to the outage,
  its start, its duration and whether emergency outages are on.
- 9999 minutes to outage counts as "no outage scheduled" everywhere.

## 0.4.21

- **Charge on emergency outages** (new switch in Battery control): while the *Emergency
  outages* sensor is on, every program is held at the pre-outage SOC with grid charge on
  right away; the outage schedule and AI predictions are ignored until it turns off, then
  the previous mode and SOC values come back. Stopping it by hand holds until the sensor
  turns off.
- The scheduled switch is now called *Charge before scheduled outages*.
- Dashboard: without a *Probable outages* sensor, the tile shows **Emergency outages**
  (On / Off) with the next scheduled outage.

## 0.4.20

- Monthly bill → **Appliances**: kWh and cost of each appliance per month (per tariff with
  several tariffs), its share of the month's bill, and the year's total. Recorded live
  from each appliance's power sensor from the moment it is added – an appliance added
  this month never shows up in earlier months. Costs use the month's price per tariff, so
  editing a month's price or *Use current prices* updates the appliances too.

## 0.4.19

- **Emergency outages** sensor (Settings → Sensors, optional): while it is on, the
  tariff-aware logic is skipped and the battery is charged to the pre-outage SOC before an
  outage, whatever the tariff. The Battery control card shows 🚨 when it applies.

## 0.4.18

- **Tariff-aware charge before outages** (switch under *Charge before outages*; only with
  several tariffs and an *Outage duration* sensor, e.g.
  `sensor.svitlo_kiivska_oblast_2_2_longest_continuous_outage`):
  - Outage in a pricier tariff (peak): charge only if the battery cannot carry the load
    from now until the outage ends, and then only what the outage needs; the rest waits
    for the cheap hours.
  - Outage in the cheapest tariff (off-peak): charge for the whole outage, and when it runs
    past the cheap hours, also until they return.
  - The load is the recorded average per hour of day (weekday / weekend) plus the safety
    margin; the pre-outage SOC is the upper limit. The dashboard shows the decision and why.
  - With a single tariff, or without the duration sensor, the battery is charged to the
    pre-outage SOC as before.

## 0.4.17

- **Notification start text** (Settings → Phone notifications): starts every notification
  title, e.g. "Garage · 🔋 BatteryAI: new plan", to tell several BatteryAI instances apart.
  Empty = the battery bank name. The test button uses the value in the form.

## 0.4.16

- The battery bank name sits on the title row next to the BatteryAI logo and version
  (click to edit), and can also be edited in Settings → Prediction tuning
  (*Battery bank name*).

## 0.4.15

- **Solar forecast correction** (Settings → Prediction tuning, 1–200%): today's and
  tomorrow's solar forecast are multiplied by it, for the dashboard, every prediction
  engine and the stored history. 110 = PV usually comes out 10% above the forecast. The
  Solar tiles show the sensor's forecast and the correction.
- Header: the running version as a chip next to the logo (read from the add-on's
  config.yaml when the build does not pass it), and the **battery bank name** on the right
  – click it, type, Enter. It is also the browser tab title.

## 0.4.14

- Fix: phone notifications failing with "HTTP 400: Bad Request".
  - A service entered without its prefix (e.g. `iphone_denis`) is sent to
    `notify.mobile_app_iphone_denis` when only that exists; an unknown service names the
    ones Home Assistant has.
  - If Home Assistant rejects the phone options (tap action, button, grouping), the
    notification is sent again without them instead of not at all.

## 0.4.13

- Monthly bill: months recorded with tariffs that are no longer in Settings (for example
  the default Peak / Off-peak before your own tariffs were set) are pointed out, and their
  **Use current prices** button is highlighted.
- Backup: the hint explains that the zip is compressed and much smaller than the database.

## 0.4.12

- Monthly bill: each month keeps its own price per kWh per tariff, shown in the table
  and editable there (the month's costs follow). A new month takes the prices from the
  tariffs in Settings. **Use current prices** sets a month to the Settings prices.

## 0.4.11

- **Charge before outages**: with a *Minutes to outage* sensor (e.g.
  `sensor.svitlo_kyiv_4_1_minutes_to_outage`), every program is set to 100% with grid
  charge on 90 minutes before an outage, then the previous mode and SOC values come back.
  Switch in the Battery control card; minutes and SOC in Settings → Prediction tuning. The Probable
  outages tile shows the time until the next outage.
- Logs → **Backup**: download a zip with the database, settings and control state, and
  restore it into a reinstalled add-on.
- Monthly bill: the most expensive tariff comes first (Peak before Off-peak); a single
  tariff shows its own name; **Recalculate** reprices a month with the current tariffs;
  energy at the start of a month no longer leaks into the previous month.
- Dashboard: PV surplus no longer goes negative at night ("no PV now", or how much of the
  load PV covers).

## 0.4.10

- Economy → **Monthly bill**: grid kWh and cost per month (current month first), per
  tariff (e.g. peak / off-peak kWh and cost) or just overall with a single price, a year
  total row, a year selector, and the days of a month on click.
  - New setting *Grid energy meter* (Settings → Sensors), e.g. a Shelly EM total energy
    sensor; without it the *Grid import today* sensor is used.
  - Energy and cost are stored in the database at the price in effect when recorded, so
    later tariff changes leave past months unchanged.
  - This month so far is read from the Home Assistant history when a meter is first set.

## 0.4.9

- New prediction engine: **ChatGPT** through the OpenAI API (Settings → Prediction
  engine, or the header dropdown). It gets the same data, instructions and answer format
  as Claude. Settings → ChatGPT: API key, model (listed from your key), reasoning effort
  and a test button.
- Phone notifications are easier to read, especially on iPhone:
  - One notification per prediction instead of two. Before, the "SOC updated" message
    replaced the prediction on the phone, because both used the same tag.
  - A short brief first (tomorrow's use, solar, lowest battery, whether it was written
    to the inverter). Pull the notification down to see the summary and every program as
    "P1 00:00–03:00 54% · grid charge (was 100%)".
  - Tap it, or its **Open BatteryAI** button, to open the BatteryAI panel.
  - A warning icon in the title when an outage is expected.

## 0.4.8

- Fix: Deye program time ranges. A program runs from its own time until the next
  program's time, and program 6 runs until program 1 (P1 00:00, P2 03:00 = 00:00 – 03:00).
  This is now the default, and existing settings are switched to it once. The dashboard,
  the battery chart bands, the active program, the stored program SOC history and every
  prediction engine use the corrected ranges.

## 0.4.7

- Single price: keep just one tariff (shown as "All day") when the grid price is the
  same all the time. Every prediction engine then keeps grid charge off (except before
  an outage) and uses the battery for PV.

## 0.4.6

- Tariffs are a list you manage in Settings → Tariffs: any number of named tariffs (up
  to 8) with their own price and time windows, one of them covering "all other times".
  Add rows with **+ Add tariff**, remove them with **−**. Existing peak / off-peak
  settings are carried over as *Off-peak* and *Peak*.
- The dashboard tile shows the current tariff's name and price; the Economy table has a
  grid energy column per tariff; Claude and the local engines get the price of every
  hour and charge in the cheapest tariff.
- Negative savings are shown in red.

## 0.4.5

- Seasons: a monthly summary over all stored history (consumption, PV, grid, appliances,
  temperature, weekday/weekend averages, cost and savings).
  - Economy tab → "By month" chart and table.
  - Claude receives the monthly history and the same weeks of last year.
  - Local fast searches all history for similar days and prefers the same time of year.

## 0.4.4

- New BatteryAI logo (battery with a green brain and the sun) in the panel header, the
  browser tab and the Home Assistant add-on store.

## 0.4.3

- The panel header shows the running add-on version.

## 0.4.2

- Fix: when Home Assistant was still starting, the add-on fell back to UTC, so the
  tariff (peak/off-peak), prediction times and active program were off by the UTC
  offset. It now retries Home Assistant, falls back to the container's time zone, keeps
  retrying in the background, and corrects the local date/time of stored readings. The
  header shows the time zone in use.
- Fix: browsers kept old copies of the panel after an update (missing Logs tab, old
  appliance settings). UI files are now versioned and always revalidated.

## 0.4.1

- Appliances are a list you manage in Settings → Appliances: custom name, power sensor
  (W or kW), "depends on outdoor temperature", with + / − to add and remove. They appear
  on the dashboard (tiles, power and daily charts, prediction card) and in every
  prediction engine. The former heat pump / boiler / EV settings and their recorded
  history are carried over; renaming an appliance keeps its history.

## 0.4.0

- Prediction engine selection (Settings → Prediction engine, and the header dropdown):
  - **Claude** (cloud).
  - **Local fast** – runs in the add-on without internet or AI: forecasts tomorrow from
    the most similar recorded days (day type, outdoor temperature; heat pump scaled by a
    temperature regression) and plans each program with tariff/PV/outage rules. Instant.
  - **Local slow** – Qwen2.5-3B-Instruct (Q4_K_M) running on the CPU inside the add-on
    via llama.cpp, which is compiled into the add-on image. The model (~2 GB) is
    downloaded once into /data/models from the Settings tab and excluded from backups.
    It runs in a separate process only during a prediction.
- Battery capacity setting (used by the local engines and given to Claude).

## 0.3.2

- Logs tab: live add-on log with level filter and search; storage overview (database
  size, rows, data range, add-on data and disk use) with "Compress old readings" and
  "Compact database".
- History up to 365 days. Readings older than "Keep full detail" (30 days by default)
  are compressed to hourly rows; outage attributes are stored only when they change.
  A year of data stays a few MB.
- Deye programs merged into the Battery control card: set each program's SOC and switch
  its grid charge directly from the dashboard.
- "Force charge" renamed to "Grid charge".

## 0.3.1

- Deye program ranges: a program's time is the END of its range, which starts at the
  previous program's time (P1 05:00 after P6 23:15 = 23:15 – 05:00). Configurable in
  Settings → Deye programs. Tables show the time range ("23:15 – 05:00", no seconds);
  Claude gets the computed ranges and no longer suggests moving program times.
- Battery chart shows each program's range as a labelled band; stored program SOC is
  recalculated for all readings.
- Power chart shows Claude's predicted hourly load (past days and tomorrow); time axis
  ticks on round hours.
- Claude requests are streamed with a 64k output limit, so long answers with thinking
  are no longer cut off; clearer API error messages.
- Model selection dropdown in the header and in Settings, listing the models available
  to your API key.

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
