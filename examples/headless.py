"""Plays one combo chain from code, with no pilot and no pad, and prints the moves it chooses.

This is the starting point for driving the harness's decisions from your own program, for example a REK AI opponent
that wants move names rather than key presses.

    py examples\\headless.py --replay logs\\session-YYYYMMDD-HHMMSS.jsonl --from 21:00:00 --seconds 60
    py examples\\headless.py --live --seconds 30     # reads REK's fight and prints what the chain would play

Nothing here sends input to REK. To make a robot act, give the Player a backend whose on_move() and on_walk() reach
it (examples/move_backend.py). With --replay the logged robot doesn't follow the chosen moves, so the chain sees its
moves "dropped" and retries; the output shows what it would choose, not a fight it is playing.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rek_combo as rc  # noqa: E402
from examples.move_backend import MoveBackend  # noqa: E402
from examples.replay_state import ReplayState  # noqa: E402


class NullSession:
    """The harness logs every press to a Session; this keeps headless runs out of logs/."""
    path = None

    def event(self, kind, **fields):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replay", type=Path, help="a logs/session-*.jsonl to replay")
    parser.add_argument("--from", dest="start", default="", help="replay from this time of day, HH:MM:SS")
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed")
    parser.add_argument("--live", action="store_true", help="read the fight from REK (read-only) instead")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--combo", default="l1", help="the pad trigger of the combo to play (default l1)")
    parser.add_argument("--config", type=Path, default=rc.HERE / "combos.json")
    parser.add_argument("--game-dir", type=Path)
    parser.add_argument("--quiet", action="store_true", help="print only the moves, not the harness's own lines")
    args = parser.parse_args()
    if bool(args.replay) == args.live:
        parser.error("give either --replay FILE or --live")
    if not rc.use_game_dir(args.game_dir):
        sys.exit(rc.rek_paths.NOT_FOUND)

    def printer():  # the harness reports through rc.say(); show it here instead of appending to logs/console.log
        while True:
            line = rc._console.get()
            if not args.quiet:
                print("   " + line, flush=True)

    threading.Thread(target=printer, daemon=True).start()
    rc.winmm.timeBeginPeriod(1)

    game = rc.GameData()
    config = rc.load_config(args.config, game)
    combo = next((c for c in config.combos if c.pad == args.combo), None)
    if combo is None:
        parser.error(f"no combo on pad {args.combo!r} in {args.config.name}")
    reader = ReplayState(path=args.replay, start=args.start, speed=args.speed) if args.replay else None
    aim = rc.make_aim(config, game, reader)
    aim.start()
    backend = MoveBackend(game)
    player = rc.Player(backend, NullSession(), require_focus=False, aim=aim)
    player.pad_now = combo.pad_mask  # the trigger counts as held, so a hold_to_arm chain stays armed
    print(f"playing {combo.name!r} for {args.seconds:g} s", flush=True)

    end = time.perf_counter() + args.seconds
    try:
        while time.perf_counter() < end:
            state = aim.state
            if state and state["phase"] == 1 and not player.busy:  # phase 1: the round is live
                player.fire(combo, config, combo.pad)
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        player.cancel.set()
        backend.release_all()
    print(f"moves chosen: {dict(player.sent) or 'none'}")


if __name__ == "__main__":
    main()
