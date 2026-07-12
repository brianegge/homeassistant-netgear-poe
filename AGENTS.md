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
│       ├── api.py               # NetgearPoeApi: web CGI client (login, poe_port)
│       ├── snmp.py              # SnmpLinkMonitor: ifOperStatus + ifAlias walks
│       ├── trap_receiver.py     # SnmpTrapReceiver: UDP 162 linkUp/linkDown
│       ├── nsdp.py              # NSDP discovery scanner (async_discover)
│       ├── config_flow.py       # user, discovery, reauth, reconfigure steps
│       ├── const.py             # DOMAIN, intervals, CONF_* keys, PLATFORMS
│       ├── entity.py            # NetgearPoeEntity / NetgearPoePortEntity bases
│       ├── switch.py            # Per-port PoE on/off switches
│       ├── button.py            # Per-port power-cycle buttons
│       ├── sensor.py            # Total PoE power sensor
│       ├── binary_sensor.py     # Per-port link sensors (SNMP)
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

## Transport summary

- **PoE control** — web CGI at `/cgi/get.cgi` and `/cgi/set.cgi`. Login posts
  an obfuscated password to `home_loginAuth`; writes carry an RSA-encrypted
  `X-CSRF-XSID` header. Commands: `poe_port`, `poe_portReset`, `port_port`
  (names), `sys_info`, `snmp_trapConfgAdd`, `home_logout`. The switch limits
  concurrent web sessions with an idle timeout — always log out.
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
