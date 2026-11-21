"""LAN Watch — ARP scanner with device inventory, HA enrichment and Telegram alerts."""
import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import ipaddress

import aiohttp
from aiohttp import web

log = logging.getLogger("lan_watch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA = Path(os.environ.get("DATA_DIR", "/data"))
APP = Path(__file__).parent
OPTS_FILE = DATA / "options.json"
DB_FILE = DATA / "lan_watch.db"
BUCKET = 900  # presence bucket: 15 min -> 96 per day

CATEGORIES = ["network", "server", "camera", "iot", "media", "computer", "mobile", "energy", "appliance", "unknown"]
CAT_RULES = [
    ("camera", r"\bcam\b|reolink|dahua|hikvision|laview|lt security|\bnvr\b|doorbell|bilian|camera"),
    ("network", r"mikrotik|routerboard|\beero\b|ubiquiti|slzb|smlight|zigbee|tp-link|keenetic|unifi"),
    ("server", r"proxmox|qemu|unraid|synology|umbrel|openmediavault|trilium|casaos|jeedom|home assistant\b|kasm|ubuntu|raspberry|\bvm\b"),
    ("energy", r"deye|inverter|solar|\bjk|bms|3em|charger|smart-maic|kraftwerk|ecoflow|victron"),
    ("appliance", r"miele|gree|\bac\b|roborock|vacuum|mxchip|fryer|withings|chamberlain|myq|rachio|petcare|thermostat|terneo|nest labs|ratgdo"),
    ("computer", r"\bpc\b|desktop|laptop|macbook|toughpad|printer|xerox|laser|lenovo|asustek|dell|hewlett|intel corporate|micro-star|gigabyte"),
    ("iot", r"espressif|esphome|tuya|sonoff|shelly|kincony|ajax|m5stack|atom echo|nabu casa|hue|philips lighting|curtain|\bir\b|co2|relay|xiaomi|yeelight|aqara"),
    ("media", r"shield|nvidia|\btv\b|tpvision|google|nest audio|playstation|sony|sofabaton|vizio|liteon|chromecast|amazon|roku|samsung electronics|lg electronics"),
    ("mobile", r"iphone|ipad|apple|redmi|moto|galaxy|pixel|oneplus|huawei|honor|oppo|vivo|watch"),
]

DEFAULT_OPTS = {
    "interface": "",
    "scan_interval": 60,
    "offline_after": 300,
    "telegram_token": "",
    "telegram_chat_id": "",
    "notify_new": True,
    "notify_watched": True,
    "wyl_url": "",
}


def load_opts():
    o = dict(DEFAULT_OPTS)
    if OPTS_FILE.exists():
        o.update({k: v for k, v in json.loads(OPTS_FILE.read_text()).items() if v is not None})
    return o


OPTS = load_opts()


def categorize(*parts):
    s = " ".join(p or "" for p in parts).lower()
    for cat, rx in CAT_RULES:
        if re.search(rx, s):
            return cat
    return "unknown"


def is_random_mac(mac):
    try:
        return bool(int(mac.split(":")[0], 16) & 2)
    except ValueError:
        return False


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS hosts(
          mac TEXT PRIMARY KEY, ip TEXT, name TEXT DEFAULT '', category TEXT DEFAULT 'unknown',
          vendor TEXT DEFAULT '', hostname TEXT DEFAULT '', ha_name TEXT DEFAULT '', ha_model TEXT DEFAULT '',
          ha_area TEXT DEFAULT '', note TEXT DEFAULT '', known INTEGER DEFAULT 0, watch INTEGER DEFAULT 0,
          cat_manual INTEGER DEFAULT 0, first_seen REAL, last_seen REAL, online INTEGER DEFAULT 0, rtt REAL);
        CREATE TABLE IF NOT EXISTS presence(mac TEXT, b INTEGER, PRIMARY KEY(mac,b)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mac TEXT, kind TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
        """
    )
    if con.execute("SELECT count(*) FROM hosts").fetchone()[0] == 0:
        seed = APP / "seed.json"
        if seed.exists():
            rows = json.loads(seed.read_text())
            for r in rows:
                fs = time.mktime(time.strptime(r["first_seen"], "%Y-%m-%d %H:%M:%S")) if r.get("first_seen") else time.time()
                watch = 1 if r["category"] in ("network", "server", "camera") else 0
                con.execute(
                    "INSERT OR IGNORE INTO hosts(mac,ip,name,category,vendor,known,watch,cat_manual,first_seen,last_seen,online)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                    (r["mac"], r["ip"], r["name"], r["category"], r["vendor"], r["known"], watch,
                     1 if r["name"] else 0, fs, fs),
                )
            log.info("seeded %d hosts", len(rows))
    con.commit()
    con.close()


def add_event(con, mac, kind, detail=""):
    con.execute("INSERT INTO events(ts,mac,kind,detail) VALUES(?,?,?,?)", (time.time(), mac, kind, detail))


# ---------------------------------------------------------------- telegram
async def tg_send(text):
    tok, chat = OPTS.get("telegram_token"), str(OPTS.get("telegram_chat_id") or "")
    if not tok or not chat:
        return False, "telegram not configured"
    ok_all, err = True, ""
    async with aiohttp.ClientSession() as s:
        for cid in [c.strip() for c in chat.split(",") if c.strip()]:
            try:
                async with s.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                  json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                                        "disable_web_page_preview": True}, timeout=15) as r:
                    if r.status != 200:
                        ok_all, err = False, await r.text()
                        log.warning("telegram %s: %s", r.status, err[:200])
            except Exception as e:  # noqa: BLE001
                ok_all, err = False, str(e)
                log.warning("telegram error: %s", e)
    return ok_all, err


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def label(h):
    return h["name"] or h["ha_name"] or h["hostname"] or h["vendor"] or h["mac"]


# ---------------------------------------------------------------- scanning
def default_iface():
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000":
                return f[0]
    except OSError:
        pass
    return "eth0"


def arp_scan(iface):
    cmd = ["arp-scan", "--localnet", "-I", iface, "--retry=2", "--plain",
           "--format=${ip}\t${mac}\t${rtt}\t${vendor}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        log.error("arp-scan failed: %s", e)
        return {}
    found = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 2 or not re.match(r"^\d+\.\d+\.\d+\.\d+$", p[0]):
            continue
        mac = p[1].lower()
        rtt = None
        if len(p) > 2:
            try:
                rtt = float(p[2])
            except ValueError:
                rtt = None
        vendor = p[3].strip() if len(p) > 3 else ""
        if vendor.startswith("(Unknown"):
            vendor = ""
        found.setdefault(mac, (p[0], rtt, vendor))
    return found


def rdns(ip):
    import socket
    try:
        n = socket.gethostbyaddr(ip)[0]
        return "" if n == ip else n.split(".")[0]
    except OSError:
        return ""


STATE = {"last_scan": 0, "scan_ms": 0, "iface": "", "scanning": False, "ha_sync": 0}


async def scan_once():
    if STATE["scanning"]:
        return
    STATE["scanning"] = True
    try:
        iface = OPTS.get("interface") or default_iface()
        STATE["iface"] = iface
        t0 = time.time()
        found = await asyncio.to_thread(arp_scan, iface)
        STATE["scan_ms"] = int((time.time() - t0) * 1000)
        now = time.time()
        notes = []
        first = STATE["last_scan"] == 0  # after start: don't report everything as "back online"
        con = db()
        for mac, (ip, rtt, vendor) in found.items():
            h = con.execute("SELECT * FROM hosts WHERE mac=?", (mac,)).fetchone()
            if h is None:
                hn = await asyncio.to_thread(rdns, ip)
                cat = categorize(hn, vendor)
                con.execute(
                    "INSERT INTO hosts(mac,ip,vendor,hostname,category,first_seen,last_seen,online,rtt,known)"
                    " VALUES(?,?,?,?,?,?,?,1,?,0)", (mac, ip, vendor, hn, cat, now, now, rtt))
                add_event(con, mac, "new", ip)
                if OPTS.get("notify_new"):
                    rnd = " · random MAC" if is_random_mac(mac) else ""
                    notes.append(f"🆕 <b>Новое устройство в сети</b>\n{esc(hn or vendor or 'Unknown')}\n"
                                 f"<code>{ip}</code> · <code>{mac}</code>{rnd}")
            else:
                if h["ip"] != ip:
                    add_event(con, mac, "ip", f"{h['ip']} → {ip}")
                if not h["online"]:
                    add_event(con, mac, "online", ip)
                    if h["watch"] and OPTS.get("notify_watched") and not first:
                        notes.append(f"🟢 <b>{esc(label(h))}</b> снова в сети · <code>{ip}</code>")
                con.execute("UPDATE hosts SET ip=?, last_seen=?, online=1, rtt=?, vendor=CASE WHEN vendor='' THEN ? ELSE vendor END"
                            " WHERE mac=?", (ip, now, rtt, vendor, mac))
            con.execute("INSERT OR IGNORE INTO presence(mac,b) VALUES(?,?)", (mac, int(now // BUCKET)))
        # offline transitions
        cutoff = now - float(OPTS.get("offline_after", 300))
        for h in con.execute("SELECT * FROM hosts WHERE online=1 AND last_seen<?", (cutoff,)).fetchall():
            con.execute("UPDATE hosts SET online=0 WHERE mac=?", (h["mac"],))
            add_event(con, h["mac"], "offline", h["ip"])
            if h["watch"] and OPTS.get("notify_watched"):
                notes.append(f"🔴 <b>{esc(label(h))}</b> пропал из сети · <code>{h['ip']}</code>")
        con.execute("DELETE FROM presence WHERE b<?", (int(now // BUCKET) - 96 * 7,))
        con.execute("DELETE FROM events WHERE ts<?", (now - 86400 * 30,))
        con.commit()
        con.close()
        STATE["last_scan"] = now
        if notes:
            # batch to avoid flooding
            for i in range(0, len(notes), 15):
                await tg_send("\n\n".join(notes[i:i + 15]))
    finally:
        STATE["scanning"] = False


async def scan_loop():
    while True:
        try:
            await scan_once()
        except Exception:  # noqa: BLE001
            log.exception("scan error")
        await asyncio.sleep(max(15, int(OPTS.get("scan_interval", 60))))


# ---------------------------------------------------------------- Home Assistant enrichment
GENERIC = re.compile(r"([0-9a-f]{2}[:_-]){5}[0-9a-f]{2}|^([0-9a-f]{12})$|^\d+\.\d+\.\d+\.\d+$|^(wlan0|eth0|espressif|eero|iphone|ipad|mac|samsung|reolink|mxchip|"
                     r"esp_[0-9a-f]{6}|nvr|google-home-mini|nest-audio|android-.*|localhost)$", re.I)


async def ha_ws(commands):
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        try:  # s6-overlay keeps the container env here when the process env was cleared
            token = Path("/run/s6/container_environment/SUPERVISOR_TOKEN").read_text().strip()
        except OSError:
            token = ""
    if not token:
        log.warning("HA sync: SUPERVISOR_TOKEN not available")
        return None
    res = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://supervisor/core/websocket", max_msg_size=0, timeout=30) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                log.warning("HA sync: websocket auth: %s", auth)
                return None
            for i, c in enumerate(commands, 1):
                await ws.send_json({"id": i, **c})
                while True:
                    m = await ws.receive_json()
                    if m.get("id") == i and m.get("type") == "result":
                        if not m.get("success"):
                            log.warning("HA %s: %s", c["type"], m.get("error"))
                        res.append(m.get("result"))
                        break
    return res


async def ha_sync():
    log.info("HA sync: start")
    try:
        r = await asyncio.wait_for(ha_ws([{"type": "config/device_registry/list"}, {"type": "config/area_registry/list"},
                         {"type": "get_states"}]), 120)
    except Exception as e:  # noqa: BLE001
        log.warning("HA sync failed: %r", e)
        return
    if not r:
        log.warning("HA sync: no data (auth failed or no SUPERVISOR_TOKEN)")
        return
    devs, areas, states = r
    areas = {a["area_id"]: a["name"] for a in areas or []}
    info = {}

    def rec(m):
        return info.setdefault(m, {"name": "", "model": "", "area": "", "host": ""})

    for d in devs or []:
        name = d.get("name_by_user") or d.get("name") or ""
        if GENERIC.search(name or "x"):
            name = ""
        model = " ".join(x for x in (d.get("manufacturer"), d.get("model")) if x)
        for c in d.get("connections", []):
            if c[0] in ("mac", "network_mac"):
                r = rec(c[1].lower())
                if name and not r["name"]:
                    r["name"] = name
                r["model"] = r["model"] or model
                r["area"] = r["area"] or areas.get(d.get("area_id"), "")
    for s in states or []:
        if not s["entity_id"].startswith("device_tracker."):
            continue
        a = s["attributes"]
        m = (a.get("mac") or "").lower()
        if not m:
            continue
        r = rec(m)
        fn = a.get("friendly_name") or ""
        if fn and not GENERIC.search(fn) and not r["name"]:
            r["name"] = fn
        hn = a.get("host_name") or a.get("hostname") or ""
        if hn and not GENERIC.search(hn):
            r["host"] = r["host"] or hn
    con = db()
    for m, r in info.items():
        name, model, area, hn = r["name"], r["model"], r["area"], r["host"]
        h = con.execute("SELECT * FROM hosts WHERE mac=?", (m,)).fetchone()
        if not h:
            continue
        con.execute("UPDATE hosts SET ha_name=?, ha_model=?, ha_area=? WHERE mac=?", (name, model, area, m))
        if hn and not h["hostname"]:
            con.execute("UPDATE hosts SET hostname=? WHERE mac=?", (hn, m))
        if not h["cat_manual"]:
            cat = categorize(h["name"] or name, model, h["vendor"], hn)
            if cat != "unknown":
                con.execute("UPDATE hosts SET category=? WHERE mac=?", (cat, m))
    con.commit()
    con.close()
    STATE["ha_sync"] = time.time()
    log.info("HA sync: %d devices with MAC", len(info))


async def ha_loop():
    await asyncio.sleep(5)
    while True:
        try:
            await ha_sync()
        except Exception:  # noqa: BLE001
            log.exception("HA sync crashed")
        await asyncio.sleep(600)


async def wyl_import():
    url = (OPTS.get("wyl_url") or "").rstrip("/")
    if not url:
        return 0
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/api/all", timeout=15) as r:
            rows = await r.json(content_type=None)
    con = db()
    n = 0
    for x in rows:
        mac = x["Mac"].lower().split(" ")[0]
        if not x.get("Name"):
            continue
        h = con.execute("SELECT name FROM hosts WHERE mac=?", (mac,)).fetchone()
        if h is None:
            con.execute("INSERT INTO hosts(mac,ip,name,category,known,first_seen,last_seen) VALUES(?,?,?,?,1,?,?)",
                        (mac, x["IP"], x["Name"], categorize(x["Name"], x.get("Hw")), time.time(), 0))
            n += 1
        elif not h["name"]:
            con.execute("UPDATE hosts SET name=?, known=1 WHERE mac=?", (x["Name"], mac))
            n += 1
    con.commit()
    con.close()
    return n


# ---------------------------------------------------------------- HTTP API
routes = web.RouteTableDef()


def host_dict(h, spark):
    d = dict(h)
    d["random_mac"] = is_random_mac(d["mac"])
    d["label"] = label(h)
    d["spark"] = spark.get(d["mac"], [])
    return d


@routes.get("/")
async def index(_):
    return web.FileResponse(APP / "static" / "index.html")


@routes.get("/api/hosts")
async def api_hosts(_):
    con = db()
    rows = con.execute("SELECT * FROM hosts").fetchall()
    nowb = int(time.time() // BUCKET)
    spark = {}
    for r in con.execute("SELECT mac,b FROM presence WHERE b>?", (nowb - 96,)):
        spark.setdefault(r["mac"], []).append(r["b"] - (nowb - 95))
    con.close()
    return web.json_response({"hosts": [host_dict(h, spark) for h in rows], "state": STATE,
                              "categories": CATEGORIES, "now": time.time(),
                              "telegram": bool(OPTS.get("telegram_token") and OPTS.get("telegram_chat_id"))})


@routes.post("/api/hosts/{mac}")
async def api_update(req):
    mac = req.match_info["mac"].lower()
    body = await req.json()
    allowed = {"name", "category", "note", "known", "watch"}
    sets = {k: v for k, v in body.items() if k in allowed}
    if "category" in sets:
        if sets["category"] not in CATEGORIES:
            raise web.HTTPBadRequest(text="bad category")
        sets["cat_manual"] = 1
    if not sets:
        raise web.HTTPBadRequest(text="nothing to update")
    con = db()
    con.execute(f"UPDATE hosts SET {', '.join(k + '=?' for k in sets)} WHERE mac=?", (*sets.values(), mac))
    con.commit()
    con.close()
    return web.json_response({"ok": True})


@routes.delete("/api/hosts/{mac}")
async def api_delete(req):
    mac = req.match_info["mac"].lower()
    con = db()
    con.execute("DELETE FROM hosts WHERE mac=?", (mac,))
    con.execute("DELETE FROM presence WHERE mac=?", (mac,))
    con.commit()
    con.close()
    return web.json_response({"ok": True})


@routes.post("/api/known-all")
async def api_known_all(_):
    con = db()
    con.execute("UPDATE hosts SET known=1")
    con.commit()
    con.close()
    return web.json_response({"ok": True})


@routes.post("/api/scan")
async def api_scan(_):
    await scan_once()
    return web.json_response({"ok": True, "state": STATE})


@routes.post("/api/ha-sync")
async def api_ha_sync(_):
    await ha_sync()
    return web.json_response({"ok": True})


@routes.post("/api/wyl-import")
async def api_wyl(_):
    try:
        n = await wyl_import()
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)})
    return web.json_response({"ok": True, "imported": n})


@routes.post("/api/test-telegram")
async def api_tg(_):
    ok, err = await tg_send("✅ LAN Watch: тестовое уведомление")
    return web.json_response({"ok": ok, "error": err})


@routes.get("/api/events")
async def api_events(req):
    limit = min(int(req.query.get("limit", 100)), 1000)
    con = db()
    rows = con.execute(
        "SELECT e.*, COALESCE(NULLIF(h.name,''), NULLIF(h.ha_name,''), NULLIF(h.hostname,''), e.mac) AS label "
        "FROM events e LEFT JOIN hosts h ON h.mac=e.mac ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return web.json_response([dict(r) for r in rows])


ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("172.30.32.0/23", "127.0.0.0/8", "::1/128")]


@web.middleware
async def access_guard(req, handler):
    """host_network exposes the port on the LAN: only HA ingress / hassio network may talk to us."""
    try:
        ip = ipaddress.ip_address(req.remote or "")
    except ValueError:
        raise web.HTTPForbidden()
    if not any(ip in n for n in ALLOWED_NETS):
        raise web.HTTPForbidden(text="use the Home Assistant panel")
    return await handler(req)


@routes.post("/api/import")
async def api_import(req):
    """Bulk upsert of names/categories (seed kept out of the public repo)."""
    rows = await req.json()
    con = db()
    n = 0
    for r in rows:
        mac = (r.get("mac") or "").lower()
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
            continue
        cat = r.get("category") if r.get("category") in CATEGORIES else "unknown"
        fs = time.mktime(time.strptime(r["first_seen"], "%Y-%m-%d %H:%M:%S")) if r.get("first_seen") else time.time()
        h = con.execute("SELECT * FROM hosts WHERE mac=?", (mac,)).fetchone()
        if h is None:
            con.execute("INSERT INTO hosts(mac,ip,name,category,vendor,known,watch,cat_manual,first_seen,last_seen,online)"
                        " VALUES(?,?,?,?,?,1,?,?,?,?,0)",
                        (mac, r.get("ip", ""), r.get("name", ""), cat, r.get("vendor", ""),
                         1 if cat in ("network", "server", "camera") else 0, 1 if r.get("name") else 0, fs, fs))
        else:
            con.execute("UPDATE hosts SET name=CASE WHEN name='' THEN ? ELSE name END, known=1,"
                        " category=CASE WHEN cat_manual=0 AND ?<>'' THEN ? ELSE category END,"
                        " cat_manual=CASE WHEN ?<>'' THEN 1 ELSE cat_manual END,"
                        " watch=CASE WHEN ? IN ('network','server','camera') THEN 1 ELSE watch END WHERE mac=?",
                        (r.get("name", ""), r.get("name", ""), cat, r.get("name", ""), cat, mac))
        n += 1
    con.commit()
    con.close()
    return web.json_response({"ok": True, "imported": n})


async def on_start(app):
    app["tasks"] = [asyncio.create_task(scan_loop()), asyncio.create_task(ha_loop())]


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    init_db()
    app = web.Application(middlewares=[access_guard], client_max_size=8 * 1024 * 1024)
    app.add_routes(routes)
    app.on_startup.append(on_start)
    port = int(os.environ.get("PORT", 8099))
    log.info("LAN Watch on :%d, iface=%s", port, OPTS.get("interface") or default_iface())
    web.run_app(app, port=port, access_log=None)


if __name__ == "__main__":
    main()
