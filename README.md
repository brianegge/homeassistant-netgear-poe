# Netgear PoE Switch for Home Assistant

Control PoE power on Netgear Smart Managed Pro switches (e.g. GS728TPv2)
from Home Assistant, using the switch's web management API. Built to
power-cycle stubborn PoE devices (cameras, APs) from automations.

Note: these switches expose only read-only MIB-2 over SNMP, so PoE control
goes through the same JSON CGI API the web UI uses (`/cgi/get.cgi`,
`/cgi/set.cgi`). Protocol notes: <https://github.com/tai/gs310tp>.

## Entities

For each PoE port the integration creates:

- **Switch** — enables/disables PoE power on the port, with
  `detection_status` (`delivering`, `searching`, `disabled`, `fault`, …) and
  `power_watts` attributes so automations can check whether the powered
  device is actually drawing power.
- **Button** — power cycles the port using the switch's native PoE reset.

Plus one **PoE power** sensor with the switch's total PoE draw in watts.

## Installation

Copy `custom_components/netgear_poe` into your Home Assistant `config`
directory (or add this repository to HACS as a custom repository), restart
Home Assistant, then add the **Netgear PoE Switch** integration with the
switch hostname and admin password.

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
