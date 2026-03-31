# Kiln Monitor

Practical Raspberry Pi kiln monitoring for a MAX31856 thermocouple front end.

## Quick Start Docs

- Hardware manifest: [`HARDWARE.md`](/Users/briansennett/Documents/codex/kiln-monitor/HARDWARE.md)
- First boot checklist: [`FIRST_BOOT.md`](/Users/briansennett/Documents/codex/kiln-monitor/FIRST_BOOT.md)

## Why SQLite

SQLite is the better default for this project than CSV:

- Safer writes for a long-running process.
- Easier to query later for ramp rates, alerts, and event markers.
- Less fragile than appending plain text if power is lost during a write.

CSV is still useful for exports, but SQLite is a more reliable primary log format.

## Project Layout

```text
kiln-monitor/
  main.py
  config.py
  requirements.txt
  sensor/
  storage/
  utils/
  data/
  logs/
```

## Raspberry Pi Setup

1. Enable SPI.

   ```bash
   sudo raspi-config
   ```

   Then go to `Interface Options` -> `SPI` -> `Enable`.

2. Reboot the Pi.

   ```bash
   sudo reboot
   ```

3. Install system packages.

   ```bash
   sudo apt update
   sudo apt install -y python3-pip python3-venv
   ```

4. Create a virtual environment and install Python dependencies.

   ```bash
   cd /path/to/kiln-monitor
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

## Wiring Notes

- `VIN` -> `3.3V`
- `GND` -> `GND`
- `SCK` -> Pi `SCLK`
- `SDO` -> Pi `MISO`
- `SDI` -> Pi `MOSI`
- `CS` -> Pi `GPIO5` by default in this code

If you wire chip select to a different pin, set `KILN_MONITOR_MAX31856_CS_PIN` to a valid Blinka board pin name such as `D5`, `D6`, or `CE0`.

## Running

```bash
cd /path/to/kiln-monitor
source .venv/bin/activate
python main.py
```

Useful runtime overrides:

```bash
export KILN_MONITOR_READ_INTERVAL_SECONDS=2
export KILN_MONITOR_MAX31856_CS_PIN=D5
export KILN_MONITOR_THERMOCOUPLE_TYPE=K
export KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE=FULL
python main.py
```

Console output shows:

- UTC timestamp
- current temperature in C
- current temperature in F
- simple trend: `rising`, `falling`, or `steady`

Application logs are written to `logs/kiln_monitor.log`.
Temperature samples are stored in `data/kiln_monitor.db`.

## Running As A Service

Example `systemd` unit:

```ini
[Unit]
Description=Kiln Monitor
After=local-fs.target

[Service]
Type=simple
User=pi
WorkingDirectory=/path/to/kiln-monitor
Environment=PYTHONUNBUFFERED=1
Environment=KILN_MONITOR_READ_INTERVAL_SECONDS=2
Environment=KILN_MONITOR_MAX31856_CS_PIN=D5
Environment=KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE=FULL
ExecStart=/path/to/kiln-monitor/.venv/bin/python /path/to/kiln-monitor/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Install it as:

```bash
sudo cp kiln-monitor.service /etc/systemd/system/kiln-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable kiln-monitor
sudo systemctl start kiln-monitor
sudo systemctl status kiln-monitor
```

## Logging Interval Guidance

- Start with `2` seconds. That is a good balance for kiln thermal inertia and storage size.
- `1` second is reasonable if you want more detail during testing.
- For normal firings, `2` to `5` seconds is usually enough because kiln temperature changes slowly relative to that interval.

## Error Handling Strategy

- Sensor faults do not stop the process.
- Faults are logged into SQLite with `status=ERROR`.
- The monitor keeps retrying on the next interval.
- Unexpected exceptions are written to the app log and the loop continues.
- Hardware and storage settings can be changed through environment variables without editing the code on the Pi.

## Phase 2 Readiness

The current structure leaves room to add:

- event markers as a second SQLite table
- rolling ramp-rate calculations from recent samples
- threshold alerts
- button-triggered events
- camera snapshots linked to events
- a lightweight local display layer

## Notes For Real Hardware

- Keep thermocouple extension polarity correct all the way to the amplifier.
- Avoid running thermocouple wiring alongside mains wiring where possible.
- Mount the Pi and MAX31856 away from kiln body heat and strain on the probe cable.
- Test fault behavior by temporarily disconnecting the thermocouple before relying on the system.
