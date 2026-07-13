# Netgear PoE Switch for Home Assistant

Control PoE power on Netgear Smart Managed Pro switches (e.g. GS728TPv2,
GS516TP) from Home Assistant, using the switch's web management API. Built
to power-cycle stubborn PoE devices (cameras, APs) from automations.

Note: these switches expose only read-only MIB-2 over SNMP, so PoE control
goes through the same web API the switch's UI uses. Two firmware
generations are supported and detected automatically:

- **JSON CGI** (GS728TPv2-class, firmware 6.x on Realtek RTL83xx):
  `/cgi/get.cgi`, `/cgi/set.cgi`. Protocol notes:
  <https://github.com/tai/gs310tp>.
- **Legacy XML "xui"** (GS516TP-class, Marvell firmware 6.0.x): the UI
  served under a per-device `/csbe<id>/` path prefix, with data over the
  `wcd` XML endpoint. These switches have no native PoE reset, so power
  cycling toggles PoE off and back on.

## Entities

For each PoE port the integration creates:

- **Switch** — enables/disables PoE power on the port, with
  `detection_status` (`delivering`, `searching`, `disabled`, `fault`, …) and
  `power_watts` attributes so automations can check whether the powered
  device is actually drawing power.
- **Button** — power cycles the port using the switch's native PoE reset.

With an SNMP community configured, you also get a per-port **link**
binary sensor (`connectivity`) from IF-MIB `ifOperStatus`, polled every
30 s. Enable **Listen for SNMP traps** and the integration also opens a
trap receiver on UDP 162 and registers this host as a trap destination on
the switch, so `linkUp`/`linkDown` events update the link sensors
instantly (the poll remains a backstop for dropped UDP traps).

Plus one **PoE power** sensor with the switch's total PoE draw in watts.

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

Only **Smart Managed Pro** switches (e.g. GS728TPv2, GS516TP) are offered,
since those are the models this integration's web APIs can control. The
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
