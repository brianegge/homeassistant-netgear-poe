# Netgear PoE Switch for Home Assistant

Control PoE power on Netgear Smart Managed Pro switches (e.g. GS728TPv2,
GS516TP, GS110TP, GS324TP) from Home Assistant, using the switch's web
management API. Built to power-cycle stubborn PoE devices (cameras, APs)
from automations.

Note: these switches expose only read-only MIB-2 over SNMP, so PoE control
goes through the same web API the switch's UI uses. Four firmware
generations are supported and detected automatically — the integration
probes the switch rather than trusting the model name, since revisions of
one model (a GS110TP vs a GS110TPv3) can speak different APIs:

- **JSON CGI** (GS728TPv2-class, firmware 6.x on Realtek RTL83xx):
  `/cgi/get.cgi`, `/cgi/set.cgi`. Protocol notes:
  <https://github.com/tai/gs310tp>.
- **Legacy XML "xui"** (GS516TP-class, Marvell firmware 6.0.x): the UI
  served under a per-device `/csb<hex>/` path prefix, with data over the
  `wcd` XML endpoint. These switches have no native PoE reset, so power
  cycling toggles PoE off and back on.
- **Classic HTML** (GS110TP-class, Broadcom firmware 5.4.x): the frames-based
  UI under `/base/`, driven by posting the same forms a browser would. These
  switches have no native PoE reset either, and don't support registering an
  SNMP trap destination (link state falls back to polling).
- **S350 EmWeb** (GS324TP-class, firmware 1.0.x): a hardened evolution of
  the classic UI with pages served as compiled-in routes at the site root.
  PoE on/off and power cycling work; setting port names (use SNMP `ifAlias`)
  and firmware install are not supported on this generation yet.

## Entities

For each PoE port the integration creates:

- **Switch** — enables/disables PoE power on the port, with
  `detection_status` (`delivering`, `searching`, `disabled`, `fault`, …) and
  `power_watts` attributes so automations can check whether the powered
  device is actually drawing power.
- **Button** — power cycles the port: JSON CGI models use the switch's
  native PoE reset; legacy xui and classic HTML models toggle PoE off and
  back on.

With an SNMP community configured, you also get a per-port **link**
binary sensor (`connectivity`) from IF-MIB `ifOperStatus`, polled every
30 s. Enable **Listen for SNMP traps** and the integration also opens a
trap receiver on UDP 162 and registers this host as a trap destination on
the switch, so `linkUp`/`linkDown` events update the link sensors
instantly (the poll remains a backstop for dropped UDP traps).

Plus one **PoE power** sensor with the switch's total PoE draw in watts,
and a **Firmware** update entity. The latest known firmware per model is
bundled with the integration (version, download link and release notes).
On classic HTML and legacy xui models the entity can also **install** the
update: it downloads the image from Netgear, flashes it to the switch's
inactive firmware slot (the running version stays in the other slot as a
rollback), and reboots. The reboot cuts PoE — every powered camera and AP —
and the switch's own uplink for a minute or more, so trigger it at a quiet
moment. JSON CGI models show the available version and release notes but
have no Install button.

An install takes 5–10 minutes, and the progress bar only moves when the
switch reports something. Legacy xui models report bytes transferred, so
the bar advances through the upload. Classic HTML models report nothing
while they write flash, so the bar sits at **20 % for several minutes**
there — the switch's own web UI just says to wait, and a stalled bar is
normal rather than a hang. Both then creep from 80 % to 95 % while the
switch reboots, and reach 100 % once the new version is confirmed running.

## Actions

`netgear_poe.set_port_name` sets a port's description on the switch,
targeting the port's PoE switch entity. On JSON CGI and classic HTML models
this is the port description (also visible as SNMP `ifAlias`); on legacy xui
models it is the PoE "powered device" field. Entity names include the port
description and are fixed at setup, so they pick up the new name after
the integration is reloaded.

```yaml
action: netgear_poe.set_port_name
target:
  entity_id: switch.boiler_switch_port_3_poe
data:
  name: driveway-cam
```

## Installation

Copy `custom_components/netgear_poe` into your Home Assistant `config`
directory (or add this repository to HACS as a custom repository), restart
Home Assistant, then add the **Netgear PoE Switch** integration with the
switch hostname and admin password.

## Discovery (NSDP)

The integration can auto-discover switches on the local subnet using
Netgear's own NSDP protocol (the same one the ProSAFE utility uses), so
they appear under **Settings → Devices & Services** as "Discovered → Add"
cards with the host pre-filled — you only enter the admin password.

Only **Smart Managed Pro** switches (e.g. GS728TPv2, GS516TP, GS110TP) are
offered, since those are the models this integration's web APIs can control. The
"Plus" line (GS10xPE, JGSxxPE) speaks NSDP too but uses a different web UI
and is deliberately skipped. NSDP is an L2 broadcast, so it only finds
switches on Home Assistant's own subnet, and switches answer
probabilistically — a newly powered switch may take up to a minute to
appear.

Discovery runs automatically once any switch is configured (to surface the
rest). To discover the **first** switch without adding one manually, add
this line to `configuration.yaml` and restart:

```yaml
netgear_poe:
```

## Caveats

- The switch allows a limited number of concurrent web sessions with a
  several-minute idle timeout. The integration holds one session and logs
  out when unloaded, but logging into the switch web UI may temporarily
  kick the integration (it re-authenticates automatically) and vice versa.
- Port settings (power limit, priority, …) are read and echoed back when
  toggling a port, so they are preserved.

## Example automation

```yaml
alias: Power cycle driveway camera when it stops responding
triggers:
  - trigger: state
    entity_id: binary_sensor.driveway_cam_online
    to: "off"
    for: "00:05:00"
actions:
  - action: button.press
    target:
      entity_id: button.boiler_switch_port_3_power_cycle
```
