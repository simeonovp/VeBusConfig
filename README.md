# VeBusConfig

Read, store and write configurations of Victron VE.Bus devices (MultiPlus,
MultiPlus-II, Quattro, …) through a GX device – **without VRM and without an
MK3-USB interface**.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![Status](https://img.shields.io/badge/status-experimental-orange)

## Idea

VE.Bus devices are configured with VEConfigure. Officially there are two ways to
get a configuration into the device: an MK3-USB interface connected to the unit,
or *Remote VEConfigure* through the VRM portal. With Remote VEConfigure the GX
reads the configuration, VRM passes it to the PC, and the modified file travels
the same way back.

Every GX ships the tool `mk2vsc`, which does exactly this reading and writing.
VeBusConfig calls it directly over SSH and shortens the path to
**PC ↔ GX ↔ VE.Bus**. The files are the same as with Remote VEConfigure (`.rvsc`)
and can be opened, edited and saved in VEConfigure offline.

## Features

- **Browse a store on the GX** (name, size, date)
- **Read from VE.Bus** – read the configuration from the device into a new file
  in the store, with a `.sha256` checksum
- **Write to VE.Bus** – write a file from the store to the device:
  1. automatic backup of the current state (`auto_before_<time>.rvsc`)
  2. write
  3. read back (`auto_after_<time>.rvsc`) and compare with the written file
- **Download / Upload** between the store on the GX and a folder on the PC
- **Protection:** files named `original_*` are never overwritten
- **Survives connection drops:** `mk2vsc` runs detached (`nohup`) on the GX; the
  tool keeps reconnecting for up to 10 minutes
- **VE.Bus service is detected** (`dbus -y`); with several systems you choose one
- **Log** of all actions in `VeBusConfig_log.md` (no addresses)

## Requirements

| What | Where |
|------|-------|
| Python 3 with `tkinter` and `paramiko` | PC (`pip install paramiko`) |
| VEConfigure | PC, to edit the files |
| SSH access as `root` | GX: Settings → General → Access Control (SSH on LAN, password) |
| `mk2vsc` | present on the GX (default `/opt/victronenergy/mk2vsc/mk2vsc`) |

Tested with a MultiPlus-II GX 48/3000/35-32 on Venus OS 3.66.

## Usage

```powershell
python VeBusConfig.py
```

```
Host [_________]  Password [*******]  [Connect]  GX: connected  com.victronenergy.vebus.ttyS3   [Refresh]
GX store [/data/vebusconfig_]          PC folder [configs__________] [Browse...]
┌ files on the GX ───────────┐         ┌ files on the PC ────────────┐
│ original_2026-10-04.rvsc   │         │ ...                         │
└────────────────────────────┘         └─────────────────────────────┘
[Read from VE.Bus...] [Write to VE.Bus...] [Download ->]   [<- Upload] [Open folder]
┌ progress ─────────────────────────────────────────────────────────────────┐
```

**Typical workflow:**

1. **Connect** – enter host and password. On first use compare the host key
   fingerprint with the GX (`ssh-keygen -lf /etc/ssh/ssh_host_<type>_key.pub`).
2. **Read from VE.Bus** – save the current state, e.g. as
   `original_<date>.rvsc`.
3. **Download** it to the PC, open it in **VEConfigure**, change it, save it
   under a new name.
4. **Upload** the changed file to the GX.
5. Select the file in the left list → **Write to VE.Bus**. This takes several
   minutes; progress is shown at the bottom of the window.

## Configuration – `VeBusConfig.json`

Lives next to the script and is updated with the last used values on
Connect/Refresh. The password is **never** stored.

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | – | address of the GX (last used) |
| `port` | `22` | SSH port |
| `user` | `root` | SSH user |
| `key_file` | – | optional private SSH key instead of a password (relative to the script) |
| `remote_dir` | `/data/vebusconfig` | store on the GX; below `/data` it survives firmware updates |
| `local_dir` | `configs` | folder on the PC (relative to the script or absolute) |
| `vebus_service` | – | fixed D-Bus service, e.g. `com.victronenergy.vebus.ttyS3`; empty = detect |
| `mk2_service` | – | service directory of `mk2-dbus`; empty = `/service/mk2-dbus.<port>` |
| `mk2vsc` | `/opt/victronenergy/mk2vsc/mk2vsc` | path of `mk2vsc` on the GX |

## Files

| File | Content |
|------|---------|
| `VeBusConfig.py` | the tool |
| `VeBusConfig.json` | settings (contains the address of the GX) |
| `VeBusConfig_known_hosts` | confirmed host keys (contains the address of the GX) |
| `VeBusConfig_log.md` | action log |

`VeBusConfig.json` and `VeBusConfig_known_hosts` contain network data. Do not
publish them.

## Before you write

- **The device may restart.** Victron: some changes, e.g. to Assistants, switch
  the inverter/charger off and on again; VEConfigure then shows "Changes require
  reset". AC-out has no power during that time. On a MultiPlus-II GX the built-in
  GX was unreachable for 1–2 minutes.
- **Limited write cycles.** Settings are stored in the device's EEPROM/flash –
  do not write them routinely.
- **Not supported by Victron** (Venus OS wiki: "There is no support on this type
  of using our products.").
- Always keep an unmodified backup first (`original_…`).

## Known quirks

- `mk2-dbus` restarts after **every** `mk2vsc` run; an immediately following run
  fails with `cannot open 'dbus://…/Interfaces/Mk2/Tunnel'`. The tool therefore
  waits until the service has been up for more than 60 s.
- Reading the same state twice does not give identical files: a counter (~1 per
  second) and a checksum at the end of the file change. The comparison after a
  write ignores the end of the file and reports remaining differences with their
  offsets; a single 2-byte difference is the counter.
- VEConfigure does not save an **unchanged** file.

## Troubleshooting

| Message | Cause / remedy |
|---------|----------------|
| `Connect failed: wrong password or user` | check password or `user` |
| `Connect failed: no VE.Bus service found` | the GX sees no VE.Bus device; check wiring and the Remote Console |
| `service directory … not found` | set `mk2_service` in `VeBusConfig.json` (`ls /service` on the GX) |
| `mk2vsc -r/-w failed (exit 1): … cannot open 'dbus://…'` | tunnel not ready; try again |
| many `Connection lost, reconnecting` while writing | expected when the device restarts; wait |
| VE.Bus Error 11 after writing a grid code | the relay test of the grid protection fails – check the installation (Victron: VE.Bus error codes) |

## Background

- Venus OS wiki, *commandline – operational*: `mk2vsc -r` / `-w` via the D-Bus tunnel
- Victron VRM manual, *Remote VEConfigure*
- Test notes and findings (German): `../../docu/veconfigure.md`
