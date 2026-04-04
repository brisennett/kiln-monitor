# Kiln Monitor

Practical Raspberry Pi kiln monitoring for MAX31855 and MAX31856 thermocouple front ends.

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
  status.py
  dashboard.py
  config.py
  requirements.txt
  kiln-monitor.service
  kiln-dashboard.service
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

### MAX31855 Board

Use this wiring for the MAX31855 K-type thermocouple board:

| MAX31855 pin | Raspberry Pi signal | BCM GPIO | Physical pin |
| --- | --- | --- | --- |
| `VIN` | `3.3V` | n/a | `1` |
| `GND` | `GND` | n/a | `6` |
| `CLK` / `SCK` | `SCLK` | `GPIO11` | `23` |
| `SO` / `SDO` | `MISO` | `GPIO9` | `21` |
| `CS` | chip select | `GPIO5` | `29` |

Do not connect Pi `MOSI` for MAX31855. That board is read-only over SPI.

Set:

```bash
export KILN_MONITOR_SENSOR_MODEL=MAX31855
export KILN_MONITOR_SPI_CS_PIN=D5
export KILN_MONITOR_THERMOCOUPLE_TYPE=K
```

### MAX31856 Board

- `VIN` -> `3.3V`
- `GND` -> `GND`
- `SCK` -> Pi `SCLK`
- `SDO` -> Pi `MISO`
- `SDI` -> Pi `MOSI`
- `CS` -> Pi `GPIO5` by default in this code

If you wire chip select to a different pin, set `KILN_MONITOR_SPI_CS_PIN` to a valid Blinka board pin name such as `D5`, `D6`, or `CE0`.

## Running

```bash
cd /path/to/kiln-monitor
source .venv/bin/activate
python main.py
```

Useful runtime overrides:

```bash
export KILN_MONITOR_READ_INTERVAL_SECONDS=2
export KILN_MONITOR_SENSOR_MODEL=MAX31855
export KILN_MONITOR_SPI_CS_PIN=D5
export KILN_MONITOR_THERMOCOUPLE_TYPE=K
export KILN_MONITOR_MAX_SAMPLE_JUMP_C=50
export KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE=FULL
python main.py
```

## Diagnostic Mode

Use diagnostic mode for a quick one-shot hardware check before running the full logger:

```bash
python main.py --diagnostic
```

For repeated samples:

```bash
python main.py --diagnostic --diagnostic-samples 10 --diagnostic-delay-seconds 1
```

This mode:

- verifies the configured sensor model, chip-select pin, and thermocouple type
- initializes the MAX31855 or MAX31856
- reads several samples in a row
- prints temperature, sample-to-sample delta, or a fault reason
- exits immediately with a success or failure code

Console output shows:

- UTC timestamp
- current temperature in C
- current temperature in F
- simple trend: `rising`, `falling`, or `steady`

Application logs are written to `logs/kiln_monitor.log`.
Temperature samples are stored in `data/kiln_monitor.db`.

## Status Command

Print the latest sample, sample age, last fault, and total row count from SQLite:

```bash
python status.py
```

## Local Dashboard

Serve a read-only LAN dashboard with a live status summary and temperature trend graph:

```bash
python dashboard.py --host 0.0.0.0 --port 8080
```

Then open `http://<pi-hostname-or-ip>:8080/` from another device on the same network.

The page auto-refreshes every 5 seconds and includes `1h`, `24h`, and `7d` trend views.

## Running As A Service

Verified on Pi host `kiln-spy` with the MAX31855 board: the service starts at boot, logs live samples, and survives a reboot.

### Logger Service

Example `systemd` unit:

```ini
[Unit]
Description=Kiln Monitor
After=local-fs.target

[Service]
Type=simple
User=brisennett
WorkingDirectory=/home/brisennett/kiln-monitor
Environment=PYTHONUNBUFFERED=1
Environment=KILN_MONITOR_READ_INTERVAL_SECONDS=2
Environment=KILN_MONITOR_SENSOR_MODEL=MAX31855
Environment=KILN_MONITOR_SPI_CS_PIN=D5
Environment=KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE=FULL
ExecStart=/home/brisennett/kiln-monitor/.venv/bin/python /home/brisennett/kiln-monitor/main.py
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

### Dashboard Service

Example `systemd` unit:

```ini
[Unit]
Description=Kiln Monitor Dashboard
After=network-online.target kiln-monitor.service
Wants=network-online.target

[Service]
Type=simple
User=brisennett
WorkingDirectory=/home/brisennett/kiln-monitor
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/brisennett/kiln-monitor/.venv/bin/python /home/brisennett/kiln-monitor/dashboard.py --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Install it as:

```bash
sudo cp kiln-dashboard.service /etc/systemd/system/kiln-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable kiln-dashboard
sudo systemctl start kiln-dashboard
sudo systemctl status kiln-dashboard --no-pager
```

Then open `http://<pi-hostname-or-ip>:8080/` from another device on the LAN.

## Logging Interval Guidance

- Start with `2` seconds. That is a good balance for kiln thermal inertia and storage size.
- `1` second is reasonable if you want more detail during testing.
- For normal firings, `2` to `5` seconds is usually enough because kiln temperature changes slowly relative to that interval.

## Error Handling Strategy

- Sensor faults do not stop the process.
- Faults are logged into SQLite with `status=ERROR`.
- The monitor keeps retrying on the next interval.
- Single-sample jumps larger than `KILN_MONITOR_MAX_SAMPLE_JUMP_C` are rejected and logged as `ERROR`.
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
- Mount the Pi and thermocouple amplifier board away from kiln body heat and strain on the probe cable.
- Test fault behavior by temporarily disconnecting the thermocouple before relying on the system.
