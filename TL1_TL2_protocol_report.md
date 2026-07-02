# TL1/TL2 Temperature Lift Protocol Report

## 1. Purpose

TL1 and TL2 are left-poke based temperature-lift training tasks. The mouse can trigger a warming outcome during a fixed choice window. If the mouse does not poke, the task applies a cooling outcome during the feedback window. The protocol is designed to train an action-outcome contingency:

- Left nose poke leads to warming.
- No choice leads to cooling.
- Continuous temperature drift is disabled.

## 2. Trial Structure

TL1 and TL2 use the same event order but different window durations.

| Phase | Duration | Description |
|---|---:|---|
| Trial start | immediate | Target temperature is held. A 1.0 s solenoid-valve sound cue is generated through `reward.give(trial_start_reward)`. |
| Choice window | TL1: 20 s; TL2: 10 s | Left cue blinks. Left poke is the only valid choice. Other pokes are ignored. |
| Feedback window | TL1: 40 s; TL2: 20 s | Pokes are ignored. The selected outcome target is maintained. |
| Trial end | immediate | `FeedbackEnd` is logged and the next trial starts if session time remains. |

Default session duration is 60 min.

## 3. Temperature Rules

Common settings:

- Default start target temperature: 10.0 C
- If `maintemp.py` temperature set-on is used before task start, TL1/TL2 use that set-on target as the session start temperature.
- Temperature clamp: 10.0 to 40.0 C
- Continuous drift: off
- Choice window target: held constant unless a choice occurs

### TL1

If the mouse makes a valid left poke:

- Outcome delta is fixed at `+5.0` C.
- New target is `current_average_temp + outcome_delta`, clamped to 10 to 40 C.

If the mouse makes no choice:

- Outcome delta is fixed at `-5.0` C.
- New target is `current_average_temp + outcome_delta`, clamped to 10 to 40 C.

### TL2

If the mouse makes a valid left poke:

- Outcome delta is selected from `+3.0`, `+3.5`, `+4.0` C using 20-trial balanced random bag sampling.
- New target is `current_average_temp + outcome_delta`, clamped to 10 to 40 C.

If the mouse makes no choice:

- Outcome delta is selected from `-1.5`, `-2.0`, `-2.5` C using 20-trial balanced random bag sampling.

## 4. Balanced Random Jitter

TL1 does not use warming or cooling jitter. Its choice outcome is fixed at `+5.0` C, and its no-choice outcome is fixed at `-5.0` C.

TL2 choice warming jitter uses a 20-trial balanced random bag. Because three values do not divide evenly into 20, each block uses a 7/7/6 distribution. The value receiving 6 repeats rotates across blocks, preserving better long-run balance while keeping each block randomized.

TL2 warming bag values:

```text
[+3.0, +3.5, +4.0]
```

## 5. Balanced Random No-Choice Cooling

TL2 no-choice cooling uses the same 20-trial balanced random bag rule as choice warming. Because three values do not divide evenly into 20, each block uses a 7/7/6 distribution, and the value receiving 6 repeats rotates across blocks.

No-choice cooling bag values:

```text
[-1.5, -2.0, -2.5]
```

The bag is shuffled within each block, preserving trial-to-trial unpredictability while keeping the block-level distribution close to balanced.

## 6. Trial Data Export

Trial-wise data are written to:

```text
TD_{mouse_id}_{session}_{timestamp}_trial-wise.csv
```

Current columns:

```text
mouseID
Day
Task
Trial
Time
Event
Current_Temp
Target_Temp
Choice
Bump
RT
OutcomeDelta
OutcomeTarget_Temp
```

### Event Types

| Event | Meaning | Key fields |
|---|---|---|
| `SessionStart` | Session-level start marker | Current and target temperature at session start |
| `TrialStart` | Trial start and choice-window preparation | Current and target temperature at trial start |
| `LeftPoke` | Valid left poke occurred | `Choice=l`, `Bump=positive outcome`, `RT`, `OutcomeDelta`, `OutcomeTarget_Temp` |
| `NoChoice` | No valid left poke within the task-specific choice window | `Choice=n`, `Bump=negative outcome`, `OutcomeDelta`, `OutcomeTarget_Temp` |
| `FeedbackEnd` | End of the task-specific feedback window | Final current/target temperature plus repeated trial outcome fields |
| `SessionEnd` | Session end marker | Final current and target temperature |

### Bump and OutcomeDelta

`Bump` is retained for backward compatibility and records the selected trial outcome magnitude:

- `LeftPoke`: positive warming value
- `NoChoice`: negative cooling value

`OutcomeDelta` records the same signed outcome magnitude explicitly. `OutcomeTarget_Temp` records the target temperature commanded after applying that outcome. These fields are added to make downstream analysis unambiguous.

## Sound Cue

`reward.give()` is used as a solenoid-valve sound cue in TL1/TL2. The reward line is not connected to actual reward delivery in this setup. The function is called at two moments:

- Trial start sound cue
- Valid left nose-poke sound cue

Both calls use a 1.0 s duration.

## 7. Other Exported Data

### Temperature CSV

Written by `data_export.write_every_n_miliseconds()` every 500 ms:

```text
Temperature_{mouse_id}_{session}_{timestamp}.csv
```

Columns:

```text
time(s)
target_temp
sensor_temp1
sensor_temp2
average_temp
control_mode
control_rate
predicted_temp
delta_pwm
ref_pwm
```

For TL1/TL2, the most important fields are `time(s)`, `target_temp`, `sensor_temp1`, `sensor_temp2`, and `average_temp`.

### Sensor CSV

Written by `sensor_worker()` every 10 ms:

```text
SensorTime_{mouse_id}_{session}_{timestamp}.csv
```

Columns:

```text
time(s)
sensor_reward
sensor_left
sensor_center
sensor_right
```

### Video and Frame Time CSV

If camera initialization succeeds:

- Video is saved to `Video_{mouse_id}_{session}_{timestamp}.avi`.
- Frame timestamps are saved to `FrameTime_{mouse_id}_{session}_{timestamp}.csv`.
- Each frame has session time and current average temperature overlaid.

## 8. Analysis Notes

For behavior modeling, trial data should be merged with the 500 ms temperature CSV by session-relative time. The trial data identify task events and outcome magnitudes, while the temperature CSV provides the continuous temperature trajectory.

Recommended derived variables:

- `phase`: choice window vs feedback window
- `choice_available`: true only during the choice window before a poke
- `time_in_choice_window`
- `temperature_bin`
- `dTdt`
- `previous_choice`
- `previous_outcome_delta`
- `no_choice_streak`
- `time_since_last_poke`

The current protocol is more modeling-friendly than the earlier continuous-drift version because outcome changes occur at explicit trial events. This makes it easier to separate current temperature, outcome history, and no-choice cooling history.

## 9. Implementation Location

Core implementation:

```text
task_temp.py
Task._run_temperature_lift()
Task.TL1()
Task.TL2()
TL1_Task
TL2_Task
```

Runtime dispatch:

```text
maintemp.py
task == "TL1"
task == "TL2"
```
