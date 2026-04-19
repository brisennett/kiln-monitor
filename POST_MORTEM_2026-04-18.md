# Post-Mortem: First High-Temperature Validation Run

Date: `2026-04-18`

## Purpose

This was the first real high-temperature validation firing for the kiln monitor stack.

Goals for this run:

- validate long-duration live monitoring on the Raspberry Pi
- validate dashboard stability over several hours
- validate alert rules and email delivery during a live firing
- validate camera snapshots on alert triggers
- observe kiln behavior while approaching the cone 04 range
- learn where the sensor path or kiln hardware becomes unreliable at elevated temperature

This was an empty fire intended as a validation run, not a production firing.

## Summary

The software stack performed well overall:

- monitor ran continuously for hours
- dashboard stayed usable during the firing
- email alerts fired successfully every `100 F`
- alert-triggered camera snapshots worked
- trend logging remained useful even with intermittent faults
- watchdog and alerting infrastructure stayed functional

The main hardware issue during the run was sensor reliability at high temperature.

Observed behavior:

- sensor faults increased significantly above roughly `1700 F`
- at peak conditions, there were periods with more fault samples than good samples
- faults persisted during cooldown even after kiln power was turned off
- fault rate did not really settle until temperature dropped below roughly `1500 F`

The kiln itself also had a separate hardware problem:

- one element was confirmed not to be working
- the kiln plateaued around `1980 F`
- power was shut off once it was clear the kiln was not going higher without wasting energy

## What Worked Well

- `kiln-monitor.service` remained stable over a long real-world run
- the dashboard remained responsive and readable
- alert rules and email delivery worked in a live firing
- camera snapshots on alert triggers worked as expected
- the temperature trend line remained useful for operational judgment
- the project structure held up well under a real test instead of only bench testing

## What Did Not Go Well

- the MAX31855-based sensor path produced increasing fault density at elevated temperature
- faults remained common during hot cooldown, which weakens the “switching noise only” theory
- one kiln element was not working, which changed the thermal and electrical behavior of the run
- lack of event markers made it harder to correlate operational changes with fault clusters

## Key Observations

### Sensor Fault Pattern

- Faults became frequent above approximately `1700 F`
- Faults remained elevated until cooldown reached approximately `1500 F`
- Faults were still present after kiln power was turned off
- Reported faults included short conditions such as `short to VCC`

### Wiring / Installation Notes

- thermocouple is believed to be ungrounded
- extension/control wire was solid core and first-use
- extension/control wire remained cool to the touch near the connector entering the kiln
- mains wiring was physically separated from thermocouple/control wiring by roughly `2.5 ft`
- no stray strands were observed at the terminals

### Kiln / Process Notes

- one element was not functioning during the run
- this was an empty validation fire
- pyrometric bars and kiln sitter remain the true process/safety references
- target thinking for this run was “get into the cone 04 range / slow-fire behavior neighborhood,” not “trust software temperature alone”

## Lessons Learned

### 1. The software is already useful

Even with sensor faults at high temperature, the project has crossed an important threshold:

- it is no longer just a bench prototype
- it is operationally useful in a real firing
- alerts, dashboard, logging, and snapshots are all adding real value

### 2. The thermocouple signal path is the weak point right now

Based on this run, the problem is more likely:

- heat-related signal integrity
- hot-end / probe / extension path behavior
- terminal or leakage behavior when hot
- MAX31855 sensitivity

It is less likely to be only:

- low-temperature assembly quality
- simple room-temperature wiring mistakes
- purely a control-side or relay-side software issue

### 3. Event markers matter

During the run, it became clear that operational changes should be captured in the timeline, for example:

- turned second coil on
- turned third coil on
- attempted to use fourth element
- opened lid
- shut power off

These markers will make future diagnosis much easier.

### 4. Real kiln validation exposed the right next problems

This run did exactly what a validation firing should do:

- proved the monitoring system is useful
- exposed the sensor path as the next hardware bottleneck
- exposed the dead element as a kiln-side issue
- generated a clear roadmap for the next iteration

## Most Likely Root-Cause Areas

Current ranking after this run:

1. thermocouple / extension path issue that becomes marginal only at high temperature
2. connection or leakage behavior that changes when hot
3. MAX31855 sensitivity under this kiln environment
4. EMI/noise as a contributing factor, but probably not the whole story

## Next-Run Hardware Plan

### Highest Priority

- diagnose and repair the failed kiln element
- switch to the better industrial shielded thermocouple extension wire
- inspect and likely remake the thermocouple termination at the board
- try a small capacitor across the thermocouple input at the MAX31855 board

### Strongly Consider

- swap or test with a different thermocouple if available
- return to a MAX31856-based path later if robustness becomes the priority

Note:

- `MAX31865` is for RTD probes, not Type K thermocouples, so it is not the direct upgrade path for this build

## Software / UI To-Do List

### Reliability / Diagnostics

- investigate intermittent high-temperature sensor faults
- log and visualize recent fault counts more clearly
- add manual event markers to the timeline/log
- correlate event markers with sensor fault bursts

### Alerting

- optional time-based alerts, such as “2 hours into firing”
- duplicate/copy an alert rule
- improve alert wording where useful

### Camera / Media

- visible timestamp on snapshots
- attach snapshot image to alert emails
- browse archived snapshots from the UI

### Dashboard / Workflow

- continue polishing the new full-width dashboard layout
- keep alerting and profiles as full-width sections below the chart

## Immediate Follow-Up Checklist

After the kiln is cool and safe to work on:

1. inspect the dead element and document the failure
2. inspect thermocouple/probe/extension path again with high-temp behavior in mind
3. switch to the industrial shielded extension wire
4. add the capacitor mitigation
5. add manual event markers in software before the next serious run
6. run another monitored validation firing and compare:
   - fault onset temperature
   - fault density
   - effect of element state changes
   - effect of cooldown

## Overall Assessment

This run was a success.

Not because it was perfect, but because it proved the monitor is operationally useful and surfaced the next real problems to solve. The software stack is already delivering value. The next phase is improving hardware robustness and tightening the tooling around diagnostics and event capture.
