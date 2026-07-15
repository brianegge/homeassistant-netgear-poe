# AGENTS.md - Netgear PoE Switch Integration

## Project Overview

Custom Home Assistant integration (`netgear_poe`) that controls PoE ports on
Netgear Smart Managed Pro switches (GS7xx, e.g. GS728TPv2). PoE control uses
the switch's web JSON CGI API; link state, port names, and traps use SNMP.

## Directory Structure

```
homeassistant-netgear-poe/
├── custom_components/
│   └── netgear_poe/
│       ├── __init__.py          # Coordinator, setup, NSDP discovery, trap wiring
│       ├── api.py               # NetgearPoeApi: JSON CGI client (login, poe_port)
│       ├── api_legacy.py        # NetgearLegacyApi: xui XML + async_detect_api
│       ├── api_base_ui.py       # NetgearBaseUiApi: classic /base/ HTML UI
│       ├── snmp.py              # SnmpLinkMonitor: ifOperStatus + ifAlias walks
│       ├── trap_receiver.py     # SnmpTrapReceiver: UDP 162 linkUp/linkDown
│       ├── nsdp.py              # NSDP discovery scanner (async_discover)
│       ├── config_flow.py       # user, discovery, reauth, reconfigure steps
│       ├── const.py             # DOMAIN, intervals, CONF_* keys, PLATFORMS
│       ├── entity.py            # NetgearPoeEntity / NetgearPoePortEntity bases
│       ├── switch.py            # Per-port PoE on/off switches, set_port_name
│       ├── services.yaml        # set_port_name action description
│       ├── button.py            # Per-port power-cycle buttons
│       ├── sensor.py            # Total PoE power sensor
│       ├── binary_sensor.py     # Per-port link sensors (SNMP)
│       ├── update.py            # Firmware version / update-available entity
│       ├── diagnostics.py       # Config-entry diagnostics
│       ├── manifest.json        # Metadata (requirements: pysnmp)
│       ├── strings.json         # UI localization
│       ├── icons.json           # Entity icons
│       └── brand/               # icon.png / icon@2x.png (for brands submission)
├── tests/                       # pytest-homeassistant-custom-component suite
├── .github/workflows/           # validate (HACS), hassfest, lint, tests, release
├── hacs.json
└── README.md
```

## Firmware generations

Three incompatible web UIs are in the wild. `async_detect_api` (in
`api_legacy.py`) probes `GET /` once and picks the client; all three expose the
same interface, so everything above them is generation-agnostic.

| Client | Firmware / models | Detected by |
| --- | --- | --- |
| `NetgearPoeApi` | JSON CGI (GS728TPv2, GS3xx) | neither of the below |
| `NetgearLegacyApi` | xui XML (GS516TP, 6.0.x) | 302 → `/csbe<id>/` |
| `NetgearBaseUiApi` | classic HTML (GS110TP, 5.4.x) | `/base/main_login.html` in body |

Model names don't decide this — a GS110TPv3 is newer silicon and answers as the
JSON generation. Only the probe is authoritative.

## Transport summary

- **PoE control** — web CGI at `/cgi/get.cgi` and `/cgi/set.cgi`. Login posts
  an obfuscated password to `home_loginAuth`; writes carry an RSA-encrypted
  `X-CSRF-XSID` header. Commands: `poe_port`, `poe_portReset`, `port_port`
  (read names), `port_portEdit` (write names), `sys_info`,
  `snmp_trapConfgAdd`, `home_logout`. The switch limits concurrent web
  sessions with an idle timeout — always log out.
- **Legacy xui PoE control** — XML over `/<prefix>/wcd?{PoEPSEInterfaceList}`;
  see the module docstring in `api_legacy.py`.
- **Classic /base/ PoE control** — HTML form posts, scraped by column header;
  see the module docstring in `api_base_ui.py`. The switch answers `400` if a
  posted body carries a field that page's form doesn't define, so each form
  sends exactly its own field set.
- **Link state / port names** — SNMP v2c: `ifOperStatus` (link) and `ifAlias`
  (names, same source as LibreNMS). Preferred over the web CGI for names.
- **Instant events** — SNMP trap receiver on UDP 162 (`linkUp`/`linkDown`).
- **Discovery** — NSDP L2 broadcast (63321/2 = Plus, 63323/4 = Pro). Only
  Pro switches are controllable and offered for setup.

## Conventions

- Follow Home Assistant custom component conventions.
- Use `ConfigEntry.runtime_data` (typed `NetgearPoeRuntimeData`), not
  `hass.data[DOMAIN]`.
- `has_entity_name = True`; per-port entities are named from the switch's
  port description, e.g. `Port 4 (garage-left-cam) PoE`.
- All blocking work is async; `pysnmp` is imported lazily inside methods.
- SNMP and traps are best-effort — failures degrade features, never break
  PoE control.

## Testing

Run `pytest` from the repo root. Uses `pytest-homeassistant-custom-component`;
the web API, SNMP monitor, trap receiver, and NSDP scanner are mocked.
