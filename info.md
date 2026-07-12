# Netgear PoE Switch

Control PoE ports on Netgear Smart Managed Pro switches (e.g. GS728TPv2) from
Home Assistant — power-cycle cameras, APs, and phones from automations.

## Features

- **Per-port PoE switch** — turn power on/off, named from the port's own
  description (e.g. `Port 4 (garage-left-cam) PoE`)
- **Per-port power-cycle button** — off, wait, on, using the switch's native
  PoE reset
- **Total PoE power sensor** — the switch's overall draw in watts
- **Per-port link sensors** — up/down via SNMP, with instant `linkUp`/
  `linkDown` events when the SNMP trap receiver is enabled
- **Auto-discovery** — Smart Managed Pro switches on your subnet appear as
  "Discovered" cards via Netgear's NSDP protocol

## Requirements

- A Netgear Smart Managed Pro switch reachable over HTTP
- The switch admin password
- Optional: SNMP v2c enabled (for link sensors, port names, and traps)
