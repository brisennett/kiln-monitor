# First Boot Checklist

Use this checklist for the first real bring-up on the Raspberry Pi after the MAX31856 breakout is assembled.

## Before Powering Up

1. Inspect the MAX31856 board for cold joints, solder bridges, and lifted pads.
2. Confirm the thermocouple is Type K and the extension wire is also Type K.
3. Confirm thermocouple polarity all the way from probe to amplifier terminals.
4. Verify the MAX31855 wiring:
   - `VIN` -> `3.3V`
   - `GND` -> `GND`
   - `CLK` / `SCK` -> Pi `SCLK` / `GPIO11` / physical pin `23`
   - `SO` / `SDO` -> Pi `MISO` / `GPIO9` / physical pin `21`
   - `CS` -> Pi `GPIO5` / physical pin `29` unless you intentionally chose another pin
   - Do not connect Pi `MOSI` for MAX31855
5. Keep thermocouple and logic wiring physically away from mains wiring.
6. Do the first test on the bench, not connected to kiln power switching hardware.

## Pi Setup Check

1. Confirm SPI is enabled:

   ```bash
   ls /dev/spidev*
   ```

2. Go to the project:

   ```bash
   cd /Users/briansennett/Documents/codex/kiln-monitor
   ```

3. Create and activate a virtual environment if needed:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

4. Install dependencies:

   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

## First Run

1. Export the expected runtime settings:

   ```bash
   export KILN_MONITOR_READ_INTERVAL_SECONDS=2
   export KILN_MONITOR_SENSOR_MODEL=MAX31855
   export KILN_MONITOR_SPI_CS_PIN=D5
   export KILN_MONITOR_THERMOCOUPLE_TYPE=K
   export KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE=FULL
   ```

2. Run the one-shot diagnostic first:

   ```bash
   python main.py --diagnostic --diagnostic-samples 10 --diagnostic-delay-seconds 1
   ```

3. If the diagnostic looks good, start the monitor:

   ```bash
   python main.py
   ```

## What Good Looks Like

- The program starts without crashing
- You see timestamped temperature output in the terminal
- Room temperature looks plausible
- Temperature changes gradually, not in large random jumps
- A warm fingertip or warm air near the probe raises the reading

Expected room temperature ballpark:

- About `18-27 C`
- About `64-80 F`

## Fault Test

1. With the program running, briefly disconnect the thermocouple.
2. Confirm the process keeps running.
3. Confirm you see `ERROR` output instead of a crash.
4. Reconnect the thermocouple.
5. Confirm normal readings resume on their own.

## Files To Check

Application log:

- [`logs/kiln_monitor.log`](/Users/briansennett/Documents/codex/kiln-monitor/logs/kiln_monitor.log)

SQLite database:

- [`data/kiln_monitor.db`](/Users/briansennett/Documents/codex/kiln-monitor/data/kiln_monitor.db)

If you want to inspect recent rows with SQLite:

```bash
sqlite3 data/kiln_monitor.db "select timestamp_utc, temp_c, temp_f, status, detail from temperature_log order by id desc limit 10;"
```

## If Something Looks Wrong

If the program crashes immediately:

- Recheck SPI enablement
- Recheck Python dependencies
- Recheck chip-select pin setting
- Recheck solder joints on the MAX31856 board

If the reading is obviously wrong:

- Recheck thermocouple polarity
- Confirm Type K wiring all the way through
- Look for loose screw terminals or weak solder joints
- Move the thermocouple leads farther from noisy mains wiring

If you get only fault messages:

- Confirm the thermocouple is fully connected
- Confirm the breakout terminals are tight
- Inspect the thermocouple and extension wire for breaks

## After Bench Validation

1. Install the `systemd` service.
2. Reboot the Pi.
3. Confirm the service restarts automatically.
4. Confirm new rows continue appearing in SQLite after reboot.
5. Only then move on to enclosure mounting and field wiring near the kiln.
