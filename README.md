# Netgear PoE Switch for Home Assistant

PoE control and firmware management for Netgear smart switches from Home
Assistant, using the switch's web management API. Built to power-cycle
stubborn PoE devices (cameras, APs) from automations, and to keep a fleet
of switches on current firmware without visiting each web UI.

Supported hardware is the **Smart Managed Pro** line (e.g. GS728TPv2,
GS516TP, GS110TP, GS324TP) plus **some Plus-line switches** — any Plus
model whose web UI speaks one of the four API generations below works too;
ProSAFE-Plus-only models (configured solely via the ProSAFE utility, e.g.
GS105Ev2, GS108PEv3) do not.

Note: these switches expose only read-only MIB-2 over SNMP, so PoE control
goes through the same web API the switch's UI uses. Five firmware
generations are supported and detected automatically — the integration
probes the switch rather than trusting the model name, since revisions of
one model (a GS110TP vs a GS110TPv3) can speak different APIs:

- **JSON CGI** (GS728TPv2-class, firmware 6.x on Realtek RTL83xx):
  `/cgi/get.cgi`, `/cgi/set.cgi`. Protocol notes:
  <https://github.com/tai/gs310tp>.
- **JSON CGI, redesigned "aj4" UI** (GS728TPPv3-class, firmware 6.2.x): the
  same CGI endpoints behind a rebuilt Bootstrap/Backbone web UI. Requests
  are unchanged, but every write carries a rotating CSRF token and the
  session's RSA key is parsed slightly differently.
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
  PoE on/off, power cycling and firmware install all work; setting port
  names does not (use SNMP `ifAlias`). Firmware 1.0.0.44+ adds a per-request
  CSRF token — the integration reads it from each form and echoes it back, so
  newer firmware keeps working transparently.

Any of these can be reached over **HTTPS** as well as plain HTTP. A switch
whose web admin mode is set to HTTPS (and so redirects HTTP there, or refuses
it) is detected and driven over TLS, accepting the switch's self-signed
certificate — the same thing a browser does reaching that UI on the LAN. The
scheme is re-detected on every setup, so flipping a switch between HTTP and
HTTPS needs no reconfiguration.

## Entities

For each PoE port the integration creates:

- **Switch** — enables/disables PoE power on the port, with
  `detection_status` (`delivering`, `searching`, `disabled`, `fault`, …) and
  `power_watts` attributes so automations can check whether the powered
  device is actually drawing power.
- **Button** — power cycles the port: JSON CGI models use the switch's
  native PoE reset; legacy xui, classic HTML and S350 EmWeb models toggle PoE
  off and back on.

With an SNMP community configured, you also get a per-port **link**
binary sensor (`connectivity`) from IF-MIB `ifOperStatus`, polled every
30 s. Enable **Listen for SNMP traps** and the integration also opens a
trap receiver on UDP 162 and registers this host as a trap destination on
the switch, so `linkUp`/`linkDown` events update the link sensors
instantly (the poll remains a backstop for dropped UDP traps).

There is also a **PoE controller stalled** problem binary sensor. It turns
on when the switch keeps answering management reads but its PoE-status query
hangs for two consecutive polls — a wedged PoE controller (seen after a
firmware update on the xui switches) that a cold power-cycle clears. Power
delivery keeps working while this is on, so it means "a cold reboot is
needed", not "PoE is down"; point a notification automation at it to be told
when a switch needs power-cycling.

Plus one **PoE power** sensor with the switch's total PoE draw in watts,
and a **Firmware** update entity. The latest known firmware per model is
bundled with the integration (version, download link and release notes).
On every backend the entity can also **install** the update: it downloads the
image from Netgear, flashes it to the switch's inactive firmware slot (the
running version stays in the other slot as a rollback), and reboots. The reboot
cuts PoE — every powered camera and AP — and the switch's own uplink for a
minute or more, so trigger it at a quiet moment. On JSON CGI models the switch
writes to the inactive slot itself; before activating and rebooting, the
integration re-reads the slot status and refuses to proceed unless the new
image actually landed in the inactive slot, so the running firmware can't be
overwritten out from under you.

An install takes a few minutes to about ten, and the progress bar advances
through the upload on every backend. Legacy xui models report the switch's
own bytes-transferred counter; classic HTML and S350 models count the bytes
streamed to the switch — a slow classic switch (~10 KB/s) steps the bar up
through roughly 20 → 60 % over the upload. It then creeps 80 → 95 % during the
reboot and reaches 100 % once the new version is confirmed running. (S350
switches buffer the upload in seconds and then write flash for a few minutes,
reporting no byte count during the write, so the bar can hold in the 40–60 %
band there — that pause is normal, not a hang.)

## Actions

`netgear_poe.set_port_name` sets a port's description on the switch,
targeting the port's PoE switch entity. On JSON CGI and classic HTML models
this is the port description (also visible as SNMP `ifAlias`); on legacy xui
models it is the PoE "powered device" field. S350 EmWeb models don't support
setting names over the web UI yet — SNMP `ifAlias` supplies their port names.
Entity names include the port description and are fixed at setup, so they
pick up the new name after the integration is reloaded.

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

## Removal

To remove a switch, go to **Settings → Devices & Services**, open the
**Netgear PoE Switch** integration, and use the three-dot menu on the entry to
**Delete** it. That removes the entry and all of its entities, and the
integration logs out of the switch as it unloads so it does not hold a session
slot. If SNMP traps were enabled, the trap destination the integration
registered on the switch is left in place; remove it from the switch's own
Trap Configuration page if you no longer want it.

To remove the integration entirely: delete every entry, remove the
`netgear_poe:` line from `configuration.yaml` if you added it for discovery,
then delete the `custom_components/netgear_poe` directory (or remove the HACS
repository) and restart Home Assistant.

## Discovery (NSDP)

The integration can auto-discover switches on the local subnet using
Netgear's own NSDP protocol (the same one the ProSAFE utility uses), so
they appear under **Settings → Devices & Services** as "Discovered → Add"
cards with the host pre-filled — you only enter the admin password.

Switches that answer NSDP on the "Smart Managed Pro" port pair are offered
outright. Switches that answer only on the "Plus" port pair are a mix of
supported and unsupported web UIs, so discovery probes each one's web UI
and only offers those running a generation this integration can control;
ProSAFE-Plus-only models are skipped.

The scan binds a socket on every enabled interface and targets each
subnet's directed broadcast as well as 255.255.255.255, so a multi-homed
Home Assistant host (VLANs, a second NIC) finds switches on all of its
subnets, not just the default route's. NSDP is an L2 broadcast, so only
directly attached subnets are scanned, and switches answer
probabilistically — a newly powered switch may take up to a minute to
appear.

Some models never answer NSDP at all — the GS728TPPv3 for one — but
announce themselves over **SSDP/UPnP** as `NETGEAR Switch`. Those are
picked up through Home Assistant's built-in SSDP discovery and offered
the same way.

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
