"""Input backend that turns the harness's key chords back into REK move names instead of typing them into REK.

Run the whole harness with it (your pad still triggers the combos, nothing reaches REK):

    py rek_combo.py --backend examples.move_backend:MoveBackend

To drive something else (a REK AI opponent, a simulator, a network link), subclass MoveBackend and override on_move()
and on_walk(), or pass on_move= / on_walk= when you build it yourself (see examples/headless.py).
"""
import time

MODIFIERS = ("LeftShift", "LeftCtrl")
# REK's default T800 locomotion keys (set.json bindings.keyboard.locomotion).
WALK = {"W": "forward", "S": "backward", "A": "strafe_left", "D": "strafe_right", "Q": "yaw_left", "E": "yaw_right"}


class MoveBackend:
    """The backend contract is down(name), up(name), release_all() and held; needs_focus is optional."""
    needs_focus = False  # nothing is typed into REK, so REK doesn't have to be the focused window

    def __init__(self, game, dry_run=False, on_move=None, on_walk=None):
        self.held = []
        self.dry_run = dry_run
        self._on_move, self._on_walk = on_move, on_walk
        self.started = time.perf_counter()
        # chord -> move name, from the installed build; the rescue's system chords (Passive Mode, Straighten) too
        self.moves = {}
        for move, chord in game.chords.items():
            self.moves.setdefault(frozenset(chord), move)
        for name, chord in game.rescue_chords.items():
            self.moves.setdefault(frozenset(chord), name)

    # ---- override these
    def on_move(self, move):
        """Once per chord, at the moment its last key goes down."""
        if self._on_move:
            self._on_move(move)
        else:
            print(f"{time.perf_counter() - self.started:8.3f}s  move  {move}", flush=True)

    def on_walk(self, directions):
        """Whenever the set of held locomotion directions changes, e.g. {"forward"} or set()."""
        if self._on_walk:
            self._on_walk(directions)
        else:
            print(f"{time.perf_counter() - self.started:8.3f}s  walk  {', '.join(sorted(directions)) or 'stop'}", flush=True)

    # ---- the backend contract
    def down(self, name):
        if name in self.held:
            return
        self.held.append(name)
        if name in WALK:
            self.on_walk(self._walking())
        elif name not in MODIFIERS:
            move = self.moves.get(frozenset([k for k in self.held if k in MODIFIERS] + [name]))
            if move:
                self.on_move(move)

    def up(self, name):
        if name in self.held:
            self.held.remove(name)
            if name in WALK:
                self.on_walk(self._walking())

    def release_all(self):
        walking = bool(self._walking())
        self.held.clear()
        if walking:
            self.on_walk(set())

    def _walking(self):
        return {WALK[k] for k in self.held if k in WALK}
