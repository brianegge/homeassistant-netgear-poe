# AGENTS.md - Netgear PoE Switch Integration

## Project Overview

Custom Home Assistant integration (`netgear_poe`) focused on PoE control and
firmware management for Netgear smart switches: the Smart Managed Pro line
(GS7xx, GS5xx, GS3xx, GS1xxT/TP) and any Plus-line switch whose web UI
speaks one of the four supported API generations. PoE and firmware go
through the switch's web API (JSON CGI, legacy xui XML, classic /base/
HTML, or S350 EmWeb); link state, port names, and traps use SNMP.

## Directory Structure

```
homeassistant-netgear-poe/
├── custom_components/
│   └── netgear_poe/
│       ├── __init__.py          # Coordinator, setup, NSDP discovery, trap wiring
│       ├── api.py               # NetgearPoeApi: JSON CGI client (login, poe_port)
│       ├── api_json_v2.py       # NetgearJsonV2Api: redesigned "aj4" JSON CGI UI
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
│       ├── update.py            # Firmware update entity (and install, /base/ UI)
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

Five incompatible web UIs are in the wild. `async_detect_api` (in
`api_legacy.py`) probes `GET /` once and picks the client; all expose the
same interface, so everything above them is generation-agnostic.

| Client | Firmware / models | Detected by |
| --- | --- | --- |
| `NetgearPoeApi` | JSON CGI (GS728TPv2, GS3xx) | none of the below |
| `NetgearJsonV2Api` | redesigned "aj4" JSON CGI (GS728TPPv3, 6.2.x) | `login.html?aj4=` in body |
| `NetgearLegacyApi` | xui XML (GS516TP, 6.0.x) | 302 → `/csb<hex>/` |
| `NetgearBaseUiApi` | classic HTML (GS110TP, 5.4.x) | `/base/main_login.html` in body |
| `NetgearCheetahApi` | S350 EmWeb (GS324TP, 1.0.x) | `/base/cheetah_login.html` in body |

`NetgearJsonV2Api` subclasses `NetgearPoeApi` (same CGI endpoints, commands
and `bj4=md5(query)` URL hashes). What differs, reverse-engineered from the
switch's own login.html/home.html/js: the b64 session decodes with the
modulus running to the end of the blob (the older firmware drops a trailing
byte), `home_loginAuth` only signals a bad password with `status: "error"`,
and every `set.cgi` body carries a rotating `xsrf` token — seeded from
`home_home`'s `data.xsrf` after login and refreshed from any response
carrying an `xsrf` field (a stale token gets a logout/invalidCsrf answer).

`NetgearCheetahApi` subclasses `NetgearBaseUiApi` (shared FASTPATH login,
Referer-on-every-request). Its probe branch runs **before** the `/base/` one
since both live under `/base/`. Its pages are EmWeb routes at the site root
(`/poeInterfaceConfiguration.html`) whose cells are hidden inputs named
`1.<index>.<count>.v_..._<col>`; a PoE write echoes the whole table back with
one admin cell changed. PoE control works; the ifAlias write and firmware
install are not implemented for this generation yet. Its login locks out
after repeated failures, so a wrong password stays wrong for ~15 min.

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
- **Firmware install** — `LATEST_FIRMWARE` in `const.py` maps sysObjectID →
  `FirmwareRelease` (version + Netgear download URL + KB link). Both the
  classic and xui backends implement `async_install_firmware`
  (`supports_firmware_install`); the JSON CGI one does not. Either way the
  image goes to the **inactive** dual-image slot, so the running firmware
  stays flashed as a rollback, and the final reboot drops PoE and the
  switch's uplink for a minute or more.
  - *Classic /base/*: upload `.stk` to `system/http_file_download.html`
    (`localfilename` picks the slot), activate `system/dual_image_cfg.html`,
    reboot `system/sys_reset.html`. Never post `system/reset_cfg.html` —
    that near-identical form is "Factory Default" and wipes the config. The
    switch reports nothing while it writes flash, so progress sits at 20%.
  - *xui*: upload the `.ros` archive to `Maintenance/httpConfigProcess.htm`
    (`rlCopyDestinationFileType=8`); there is no slot field — the switch
    writes to the inactive slot itself. It answers **302 on success**, so
    the POST bypasses `_attempt_request` (which reads 302 as a dead
    session). `{LoadStatus}` reports `copyStatusType` (1/2 busy, 5 done,
    3/4 failed) plus `bytesTransfered`, polled alongside the upload exactly
    as the vendor UI's iframe does — the one real progress signal we get.
    Activate via `{ImageUnitList}` `nextBootImage`, reboot via `{Reload}`.
- **Link state / port names** — SNMP v2c: `ifOperStatus` (link) and `ifAlias`
  (names, same source as LibreNMS). Preferred over the web CGI for names.
- **Instant events** — SNMP trap receiver on UDP 162 (`linkUp`/`linkDown`).
- **Discovery** — NSDP L2 broadcast (63321/2 = Plus, 63323/4 = Pro). The
  scan binds a socket per enabled interface and targets each subnet's
  directed broadcast as well as 255.255.255.255 so multi-homed hosts scan
  every subnet. Pro-port switches are offered for setup outright; Plus-port
  switches are offered only after `async_probe_supported` confirms their web
  UI is a generation the integration drives (ProSAFE-Plus-only models fail
  the probe and are skipped). Models that never answer NSDP (GS728TPPv3)
  announce over SSDP/UPnP with modelDescription "NETGEAR Switch" and are
  matched via the manifest `ssdp` key; the MAC is the tail of the UPnP UDN
  and the host comes from presentationURL.

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
