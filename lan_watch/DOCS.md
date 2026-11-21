# LAN Watch

Home Assistant add-on: ARP scanner of the local network (like WatchYourLAN) with

- device inventory grouped by type: network, servers/VM, cameras/NVR, smart home, media/TV,
  computers, phones/tablets, energy, climate/appliances, unknown;
- names filled from WatchYourLAN and the Home Assistant device registry (matched by MAC),
  area and model from HA, vendor from the OUI database;
- online/offline status, ARP RTT, 24h presence sparkline, event log;
- Telegram notifications via your own bot: new device in the network, watched device went offline / came back.


## Options

| option | meaning |
|---|---|
| `interface` | network interface, empty = default route interface |
| `scan_interval` | seconds between scans |
| `offline_after` | seconds without ARP reply before a device is offline |
| `telegram_token`, `telegram_chat_id` | bot token from @BotFather and chat id (comma-separated for several) |
| `notify_new` / `notify_watched` | which notifications to send |
| `wyl_url` | WatchYourLAN URL to import names from (button in the UI) |

Data lives in `/data/lan_watch.db` (SQLite). Initial names can be pushed with `POST /api/import` (JSON list of {mac, ip, name, category}) from the hassio network.
