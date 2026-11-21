## 1.0.0
- ARP scan (arp-scan) every N seconds, presence history 24h, events log
- Names/categories seeded from WatchYourLAN + Home Assistant device registry
- Telegram: new devices, watched devices offline/online

## 1.0.1
- HA sync: timeout, logging, never dies silently; ignore MAC/IP-like names from HA

## 1.0.2
- HA sync: read SUPERVISOR_TOKEN from s6 container environment, log auth errors
