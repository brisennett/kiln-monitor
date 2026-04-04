# Hardware Manifest

This project is a Raspberry Pi based kiln temperature monitor for an electric kiln.

## Core Components

- Raspberry Pi 4
- MAX31855 K-type thermocouple amplifier breakout for current testing
- Adafruit MAX31856 thermocouple amplifier breakout as the original target board
- Type K kiln thermocouple
- Fiberglass-insulated Type K extension wire
- Kingston A400 SATA SSD with USB-to-SATA adapter
- Small SPI or I2C display HAT with buttons
- 5MP camera module for possible future use
- Vented ABS enclosure

## Current Phase 1 Scope

Phase 1 is monitoring only. There are no relays, contactors, or kiln control outputs in this phase.

Implemented scope:

- Read kiln temperature through MAX31855 or MAX31856 over SPI
- Convert and store temperatures in Celsius and Fahrenheit
- Detect and log thermocouple and cold-junction faults
- Log samples to SQLite on the Pi
- Provide simple local console status output
- Run continuously as a long-lived service

Not yet in scope:

- Kiln power switching
- Relay or SSR outputs
- Automatic shutoff
- Closed-loop firing control
- Camera capture
- Display HAT integration

## Wiring Summary

## MAX31855 Wiring

| MAX31855 pin | Raspberry Pi signal | BCM GPIO | Physical pin |
| --- | --- | --- | --- |
| `VIN` | `3.3V` | n/a | `1` |
| `GND` | `GND` | n/a | `6` |
| `CLK` / `SCK` | `SCLK` | `GPIO11` | `23` |
| `SO` / `SDO` | `MISO` | `GPIO9` | `21` |
| `CS` | chip select | `GPIO5` | `29` |

MAX31855 does not use Pi `MOSI`.

## MAX31856 Wiring

MAX31856 breakout to Raspberry Pi:

- `VIN` -> `3.3V`
- `GND` -> `GND`
- `SCK` -> `SCLK`
- `SDO` -> `MISO`
- `SDI` -> `MOSI`
- `CS` -> `GPIO5` by default

Thermocouple side:

- Use Type K thermocouple wire and matching Type K extension wire
- Maintain correct polarity from the probe all the way to the amplifier
- Avoid mixed metals or improvised splices near the measurement path

## Physical Installation Notes

- Keep the electronics outside the kiln heat zone
- Route thermocouple wiring away from mains wiring where possible
- Add strain relief for probe and extension wire
- Keep the enclosure vented but protected from dust and accidental contact
- Mount the SSD and Pi so USB and power connections cannot loosen during a firing

## Recommended Bench Test Order

1. Assemble and inspect the MAX31856 breakout.
2. Verify SPI is enabled on the Pi.
3. Wire the MAX31856 to the Pi on the bench.
4. Connect the thermocouple and confirm a plausible room-temperature reading.
5. Warm the probe tip slightly and confirm the reading rises.
6. Disconnect the thermocouple briefly and confirm the monitor logs faults without crashing.
7. Reconnect the probe and confirm readings recover automatically.

## Bill Of Materials Notes

Items likely still needed depending on assembly details:

- Soldering iron and solder
- Female-female or female-male jumper wires, depending on the breakout and Pi header arrangement
- Standoffs or mounting hardware for the enclosure
- Cable glands or strain relief fittings for thermocouple and power leads
- Labels for probe polarity and field wiring
