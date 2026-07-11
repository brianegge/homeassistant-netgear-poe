# Netgear PoE Switch for Home Assistant

Control PoE power on Netgear smart managed switches (e.g. GS728TPv2) from
Home Assistant over SNMP. Built to power-cycle stubborn PoE devices (cameras,
APs) from automations.

## Entities

For each PoE port the integration creates:

- **Switch** — enables/disables PoE power on the port
  (`pethPsePortAdminEnable`). Port aliases configured on the switch are used
  in entity names, e.g. `Port 3 (driveway cam) PoE`.
- **Button** — power cycles the port: off, wait 5 seconds, on.

Plus one **PoE power** sensor with the switch's total PoE draw in watts, if
the switch reports it.

Each switch entity exposes a `detection_status` attribute
(`delivering_power`, `searching`, `disabled`, `fault`, …) so automations can
check whether a powered device is actually drawing power.

## Requirements

- SNMP v2c enabled on the switch, with a community that has **write** access
  (Netgear web UI: *System → SNMP → Community Configuration*, set the
  community to *Read/Write*).
- The integration uses the standard POWER-ETHERNET-MIB (RFC 3621), so any
  switch implementing it should work.

## Installation

Copy `custom_components/netgear_poe` into your Home Assistant `config`
directory (or add this repository to HACS as a custom repository), restart
Home Assistant, then add the **Netgear PoE Switch** integration and enter the
switch hostname and SNMP community strings.

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
      entity_id: button.boiler_switch_port_3_driveway_cam_power_cycle
```
