# REK combo macros (T800)

One button kicks when the kick will land. Press **L1** and the harness waits until you're in range and lined up, and
until their kick can't hit you first. Then it throws Left Leg Kick, and finishes the combo when one can follow. You keep
walking with the stick the whole time.

For [REK](https://store.steampowered.com/) on Windows, T800. Pure Python standard library: nothing to `pip install`.

## What the buttons do

| Trigger | What it plays |
|---|---|
| **L1** or key **2** | **Poke or combo.** Left Leg Kick when it's on, then Flying Side Kick, High Right Side Kick or Big Kick to finish a three-different-hits combo. Hold L1 to keep it armed. |
| **R1** or key **1** | The same, minus Flying Side Kick: finishes with High Right Side Kick or Big Kick. Pressing L1 cuts an R1 chain short. |
| key **4** | Left Jab just left of center, then one kick. |
| keys **5**-**0** | Face the opponent, free the robot (cancel the clip), turn 60/90/150/155. |
| **P** / **T** | Passive Mode (slip stop) / Kip-Up from prone. |
| **F10** / **F11** | Rescue tests. Use them in Practice only: the robot lies down, then gets up. |
| **F9** | Abort whatever is playing. |

When "Left Leg Kick is on":
- **Range:** about 0.5-1.2 m, or up to 1.4 m while walking in.
- **Aim:** within -10..+20 degrees, after turning the robot to line up.
- **Their kick:** it never kicks into their live Left Leg Kick, and it punishes the retracting one.
- **Down opponent:** it holds off while they're down.

**Auto rescue** is always on. When the robot starts going over, it spams Get Up. If the robot stays stuck, it runs
Straighten, then Get Up.

## Setup

1. Install Python 3 from [python.org](https://www.python.org/downloads/windows/). It was built and tested on 3.12.
2. Download this repo (**Code -> Download ZIP**) and unzip it anywhere.
3. In REK:
   - Leave the **keyboard** on its built-in layout. The macros send the default T800 keys, and the harness warns you
     if REK switches the keyboard to a custom layout.
   - In the default **gamepad** layout, L1 and R1 are punch modifiers. Either make a Custom gamepad layout with L1 and R1
     unbound, trigger with keys 1 and 2 instead, or change `"pad"` in `combos.json` to a free button.
4. Double-click `start.bat`, then click back into REK. Keys are only sent while REK has focus.

It finds REK by itself: first a running REK.exe, then every Steam library. If yours is somewhere else, point to the
folder that holds `REK.exe`:

```
py rek_combo.py --game-dir "D:\Games\REK Alpha Test"
```

or set the `REK_GAME_DIR` environment variable to it.

**Controller:** it reads the pad through the Windows joystick API, which was tested with a DualShock 4. Run
`py rek_combo.py --watch-pad` and press buttons to check that yours reports `l1` and `r1`.

To stop it, close its window, or create an empty file named `STOP` in this folder (it stops between chords, never mid-chord).

## Tuning

`combos.json` is reloaded every time you save it, so you can change it while you play. `py rek_combo.py --list` prints
the key table it read from your REK build and every combo.

The L1 entry is the one to look at. These are its main knobs:

| Key | Meaning |
|---|---|
| `llk_first.contact_m` | Distance band in which Left Leg Kick is thrown on sight. |
| `move_aim` | Aim window per move, in degrees (negative is left). |
| `move_max_dist` | Furthest start distance per move. |
| `wait_out_kicks`, `hold_into` | Wait out their kicks, except the ones it should kick into. |
| `hold_to_arm` | Keep re-arming while the button is held. |
| `settings.rescue` | Auto rescue timing. |

## How it works

- **Fight state:** `rek_mem.py` reads both robots' position, heading, current move, tilt and the score from REK's
  memory, about 30 times a second. It opens REK with read rights only: no writes, no injection. It finds the game's
  classes by name, so a patch that moves things around doesn't break it.
- **Moves:** the move table and strike windows come from your REK install's `set.json` at startup, so a rebind or
  retime in a patch is picked up.
- **Choosing a move:**
  - `turn_table.json`: how far each move turns the robot.
  - `land_table.json`: how often each move lands from each angle and distance.
  - `shot_model.json`: a small model of the chance each kick lands, given what the opponent is doing.
  - All three were measured from logged fights.
- **Input:** the harness sends keyboard chords with `SendInput`. A keyboard hook stops combo keys from reaching
  REK, where 1, 3, 4 and 5 are bound to other actions.
- **Logs:** everything goes to `logs/`. `console.log` mirrors the window, and `session-*.jsonl` records every press,
  move and state row.

## Rebuilding the tables from your own fights

The tables in the repo come from our fights. To fit them to yours, play with the harness running so `logs/` fills up,
then run:

```
py build_turn_table.py     # turn_table.json
py land_table.py           # land_table.json
py shot_data.py            # check the strike rows
py shot_model.py           # shot_model.json (this one needs numpy: py -m pip install numpy)
```

Analysis scripts:

| Script | Shows |
|---|---|
| `land_map.py` | Where each move lands. |
| `what_lands.py` | What separates a landed kick from a whiff. |
| `exchange_report.py` | Who hits whom, per situation. |
| `chain_mine.py` | Move pairs that chain. |
| `combo_rule.py`, `combo_chain_test.py` | What the game counts as a combo. |

## Extending it

The fight-state source, the decisions and the output are separate layers. You can swap the first and last from the
command line:

```
py rek_combo.py --state examples.replay_state:ReplayState --backend examples.move_backend:MoveBackend
```

- `examples/move_backend.py` turns the chosen chords into move names (`shanghai_leg2`, `big_kick`, ...) instead of
  typing them into REK.
- `examples/replay_state.py` replays a logged fight, so you can work without REK running.
- `examples/headless.py` plays a chain from code with no pad, which is the starting point for an AI opponent.

[EXTENDING.md](EXTENDING.md) covers the state format, the backend contract, adding combos, and using the decision
logic for a REK AI opponent.

## Tests

`py selftest.py` checks the engine: chord timing, triggers, aim, chains and rescue. It injects keys with its own tag
and swallows them in a keyboard hook, so run it while REK isn't focused.

## Files

| File | Purpose |
|---|---|
| `rek_combo.py` | The harness. |
| `rek_mem.py` | Read-only fight-state reader. |
| `rek_paths.py` | Finds the REK install and REK's user folder. |
| `combos.json` | Combos and settings, hot-reloaded. |
| `turn_table.json`, `land_table.json`, `shot_model.json` | Measured tables. |
| `start.bat` | Launcher. |
| `examples/` | A move-name backend, a replay state source and a headless driver. |
| `EXTENDING.md` | How to plug in other state sources, outputs and combos. |

## License

MIT. See [LICENSE](LICENSE).
