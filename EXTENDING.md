# Extending the harness

The harness is three layers, and each one can be replaced without touching the others:

```
 fight state  ──►  decisions  ──►  output
 (Aim thread)      (Player + combos.json)   (backend)
 rek_mem by default                         Windows keyboard by default
 --state MODULE:NAME                        --backend MODULE:NAME
```

- **Fight state:** where the robots are, which way they face, what move each one is in, and the score.
- **Decisions:** which move to throw and when. This comes from `combos.json` and the measured tables.
- **Output:** how a chosen move reaches a robot.

You can swap the first and last layers from the command line. Both flags take `module:name`. The module is imported
from the repo folder, so the examples run as they are:

```
py rek_combo.py --state examples.replay_state:ReplayState --backend examples.move_backend:MoveBackend
```

To drive the decisions from your own program with no pad at all, see `examples/headless.py`.

## 1. Fight state

`Aim` (in `rek_combo.py`) polls a state source in its own thread and keeps the newest state. That is about 30 times a
second with the default source, `rek_mem.FightReader`, which reads REK's memory with read rights only.

A replacement is any object with a `read()` method. `--state` calls `NAME(game=GameData)` to build it. `read()`
returns the dict below, or raises `rek_combo.Unavailable` while there is no fight. `examples/replay_state.py` is a
complete source: it replays a logged session, so you can work with no REK running.

| Key | Value |
|---|---|
| `phase` | Fight phase. `1` means the round is live; the harness only acts then. |
| `me`, `them` | One dict per robot, see below. `me` is the robot this harness drives. |
| `distance` | Metres between the two robots. |
| `bearing` | World angle from `me` to `them`, in degrees: `atan2(dx, dz)`. |
| `round` | Round number. It changes when a new round starts. |
| `hits` | `[mine, theirs]` clean-hit points this round, or `None`. |
| `falls`, `health` | `[mine, theirs]`, or `None`. |
| `referee` | `{"seq", "call", "faller", "points"}`. `seq` changes on every referee call. |
| `local` | Which fighter slot is `me` (0 or 1). |
| `human` | `True` if the opponent is a person, `False` if it's REK's AI, `None` if unknown. |

Each robot's dict:

| Key | Value |
|---|---|
| `at` | When this sample was taken, in seconds on any steady clock. It must go up as new samples arrive. |
| `x`, `y`, `z` | Position in metres. `y` is up. |
| `heading` | Yaw in degrees, the same convention as `bearing`. |
| `tilt` | Degrees between the body's up and world up. Over about 55 means going over. |
| `move` | Current move id, `0` when idle. Ids are 1-based positions in the build's `set.json` move list. |
| `elapsed` | Seconds into the current move. |

The aim the harness uses is `bearing - heading - facing_offset_deg`, wrapped to ±180, with positive meaning turn
right. `facing_offset_deg` is set in `combos.json` (48 for the T800). The robot's front points at `heading + offset`,
not at `heading`.

## 2. Output backends

The default backend is `rek_combo.Keyboard`, which sends scan-code key events with `SendInput`. Every injected event
carries the tag `0x52454B43` ("REKC") in `dwExtraInfo`, so the game, or anyone else, can tell it from a person's
keys. Windows also marks these events as injected.

A replacement needs four members. `--backend` calls `NAME(game=GameData, dry_run=bool)` to build it.

| Member | Meaning |
|---|---|
| `down(name)`, `up(name)` | Press and release one key. Names are REK's key names, such as `LeftShift`, `Y`, `Digit6`, `W`. |
| `release_all()` | Let go of everything. Called on abort and on exit. |
| `held` | A list of the key names that are down now. The harness reads it to know whether it is walking. |
| `needs_focus` | Optional, default `True`. Set it to `False` if the backend doesn't type into REK's window. |

The harness thinks in chords because that is how REK's keyboard works. A move's chord comes from the build's
`set.json`, for example `LeftShift+Y` for Left Leg Kick. Walking uses `W`/`S`/`A`/`D` (forward, back, strafe), and
`Q`/`E` yaw.

`examples/move_backend.py` turns chords back into move names (`shanghai_leg2`, `big_kick`, `straighten`, ...) and
walking keys into directions. Override its `on_move(move)` and `on_walk(directions)` to send them anywhere: a game API,
a simulator, a network link. `py rek_combo.py --list` prints every move with its chord and in-game title.

**Timing the backend has to honour.** The harness checks its moves against the fight state. If the robot hasn't
started a move it pressed within 0.3 s (`Player.START_SEEN_S`), it counts the move as dropped. It also respects the
build's move-switch cooldown (`move_switch_cooldown_s` in `set.json`) and the uninterruptible kick clips. So the state
source has to show the robot doing what the backend asked, about as fast as REK does.

## 3. Decisions

### Adding or changing a combo

Edit `combos.json`; the harness reloads it on save. Each entry in `"combos"` has a trigger (`"pad"`, `"key"`, or
both) and either a fixed list of steps or an `"action"`.

A fixed sequence:

```json
{"name": "Jab then Left Leg Kick", "key": "6",
 "steps": [{"move": "LJab"},
           {"move": "LLK", "after_ms": 450}]}
```

- `after_ms` counts from the previous press. A step can give `min_ms`/`max_ms` instead, and then the harness picks
  the moment inside that range when the aim is right.
- Moves are named by their build name (`shanghai_leg2`, `big_kick`) or by a short name from `ALIASES` in
  `rek_combo.py` (`LJab`, `LLK`, `RLFK`, `RFK`, `RRFK`, `HRSK`).

An `"action"` of kind `"chain"` is the adaptive kind behind L1 and R1:

| Field | Meaning |
|---|---|
| `openers` | Moves it may open with. |
| `moves` | Moves it may continue with. |
| `move_aim`, `move_max_dist`, `move_min_dist` | Per-move aim window (degrees) and distance band (metres). |
| `wait_out_kicks`, `hold_into` | Wait out the opponent's kicks, except the ones listed in `hold_into`, which it kicks into. |
| `use_model`, `min_chance` | Throw only when `shot_model.json` gives at least this landing chance. |
| `position` | Walk in, hover or back off while waiting. |

The other kinds are:
- `face` and `turn`: turn the robot.
- `free`: cancel the current clip.
- `rescue`: run a list of get-up steps.
- `manualtap`: send one system chord, such as Passive Mode.

The file is checked when it loads (`Config` in `rek_combo.py`). If a saved file is wrong, the harness says why and
keeps the previous one. The README's tuning table covers the main L1 knobs.

### The measured tables

All of these are fitted from the session logs by the scripts named in the README:

- `turn_table.json`: how far each move turns the robot, over time.
- `land_table.json`: how often each move lands, by aim and distance.
- `shot_model.json`: a logistic model of the chance a kick lands. Its inputs are aim, distance, closing speed, the
  opponent's approach, their state (idle, kick live, kick spent, down), whether they are human, and how long we've been
  idle.

They are plain JSON, so other code can use them too. For example, an AI opponent could weight its move choice by
them.

## 4. Driving it from code

`examples/headless.py` is the minimal program. It plays one chain with no pilot and prints the moves:

```python
import rek_combo as rc
from examples.headless import NullSession
from examples.move_backend import MoveBackend

rc.use_game_dir()                                  # find REK's install, for set.json
game = rc.GameData()
config = rc.load_config(rc.HERE / "combos.json", game)
aim = rc.make_aim(config, game, reader=None)       # None reads REK; or pass your own state source
aim.start()
player = rc.Player(MoveBackend(game), NullSession(), require_focus=False, aim=aim)
l1 = next(c for c in config.combos if c.pad == "l1")
player.pad_now = l1.pad_mask                       # the trigger counts as held, so the chain stays armed
player.fire(l1, config, l1.pad)                    # plays in its own thread; player.busy, player.stop(reason)
```

The main loop in `rek_combo.main()` adds the other pieces you may want: auto rescue (Get Up spam when going over,
then Straighten and Get Up when stuck), hit and referee logging, and hot reload.

## Using it for an AI opponent

These are the pieces worth reusing, from most to least portable:

1. **The rules and the tables.** These are in `combos.json`, the three table files, and the rules in the README's
   "When Left Leg Kick is on" section:
   - Range 0.5-1.2 m, or 1.4 m walking in.
   - Lined up before kicking.
   - Never kick into their live kick; punish the retracting one.
   - Hold off while they're down.
   - Finish with three different hits for the combo bonus.

   These carry over to any engine.
2. **The decision code as it is.** Write a state source that reads the game's fight state directly, and a backend
   whose `on_move` issues the move. Then run `Player` as `headless.py` does.
3. **Everything.** Run the harness against REK's AI in Practice with `--backend` and `--state` left at their
   defaults.

Pilot assist is what the harness is built and tested for: a person holds the trigger and steers. A program that plays
on its own belongs against AI opponents and in practice, not in public matches against people.

## Tests

- `py selftest.py` checks chord timing, triggers, aim, chains and the rescue against a fake fight. It sends real key
  events, which its own hook swallows, so run it while REK isn't focused.
- `py examples\headless.py --replay logs\session-....jsonl` runs the decisions against a logged fight without REK.
