"""Jev plays Super Mario Bros from a text description of the emulator RAM.

    uv run python play.py --bot jev [--level 2-1]   # Jev picks the action every HOLD frames
    uv run python play.py --bot alternate    # scripted baseline, no API calls
    uv run python play.py --bot jev --dump   # print the grid Jev sees, no API calls

Writes runs/<bot>-<timestamp>.gif and appends one JSON line per run to runs/results.jsonl.
"""

import argparse
import json
import os
import time
from pathlib import Path

import gym_super_mario_bros
import httpx
import imageio.v2 as imageio
from gym_super_mario_bros.actions import SIMPLE_MOVEMENT
from nes_py.wrappers import JoypadSpace

HOLD = 6  # frames per decision; 60 fps, so 10 decisions per game second
MAX_FRAMES = 6000
STALL_FRAMES = 360  # give up after 6 game-seconds without gaining distance
RUNS = Path(__file__).resolve().parent / "runs"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
USD_PER_TOKEN = 0.042 / 1e6

ACTIONS = {
    "stand": 0,
    "walk right": 1,
    "jump right": 2,
    "run right": 3,
    "run and jump right": 4,
    "jump in place": 5,
    "walk left": 6,
    "hop right": 2,  # same button as "jump right", released after HOP_FRAMES
    "back off for a run-up": -2,  # walk left until the obstacle ahead is 6 tiles away
}
HOP_FRAMES = 8
FULL_JUMP_FRAMES = 64
MACRO = "running jump at the obstacle ahead"  # harness-executed; offered only with --macro
JUMPS = {2, 4, 5}
RELEASE = {2: 1, 4: 3, 5: 0}  # same action without A
ACTION_HELP = {
    "stand": "wait in place; use when an enemy on a wall or pipe ahead has to move away first",
    "walk right": "move right slowly",
    "jump right": "full jump while moving right at walking speed; about 4 tiles high, 3 to 5 tiles far",
    "run right": "move right fast; does not jump; builds speed for a longer jump",
    "run and jump right": "full jump at running speed; up to 5 tiles high and 9 tiles far when already at full speed",
    "jump in place": "full jump straight up",
    "walk left": "back off to the left",
    "hop right": "short low jump to the right, about 2 tiles high; lands 1 to 4 tiles ahead depending on speed; "
    "use to land on top of an enemy 2 tiles ahead",
    "back off for a run-up": "walk left until the wall or gap ahead is 6 tiles away, so that 'run right' can "
    "build full speed before jumping; do not use with an enemy behind Mario",
}
MACRO_HELP = ("back up, run at full speed, and jump when the obstacle is 3 tiles ahead; takes about a second; "
              "use for a wall 3 or more tiles tall or a gap 3 or more tiles wide")

# Measured in this emulator: a full jump (A held until landing) at a given horizontal speed.
# (min speed, tiles high, tiles far)
JUMP_TABLE = [(30, 5, 9), (15, 4, 5), (0, 4, 3)]


def jump_reach(v: int) -> tuple[int, int]:
    for min_v, high, far in JUMP_TABLE:
        if abs(v) >= min_v:
            return high, far
    return JUMP_TABLE[-1][1:]

GRID_LEGEND = (
    "Text grid, 13 rows x 20 columns, each cell one 16px tile. Row 7 is Mario's row. "
    "M = Mario (column 5). # = solid ground, brick, block or pipe. E = enemy. . = empty air. "
    "Mario walks right (toward higher columns). Falling into a column with no # below Mario is death. "
    "Touching an E from the side is death; landing on it from above kills it."
)


def load_env() -> None:
    p = Path(__file__).resolve().parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def nes(env):
    """Walk the wrapper chain to the nes_py env that owns the RAM."""
    e = env
    while not hasattr(e, "ram"):
        e = e.env
    return e


def tile(ram, x: int, y: int) -> int:
    # NOTE: SMB keeps two 16x13 tile pages at 0x500, 208 bytes each; y offset 32 is the HUD.
    page = (x // 256) % 2
    sx, sy = (x % 256) // 16, (y - 32) // 16
    if sy < 0 or sy > 12:
        return 0
    return int(ram[0x500 + page * 208 + sy * 16 + sx])


def grid(ram) -> tuple[str, int, int]:
    mx = int(ram[0x6D]) * 256 + int(ram[0x86])
    my = int(ram[0x03B8]) + 16
    enemies = []
    for i in range(5):
        if ram[0x0F + i]:
            # NOTE: +8 puts a ground enemy in Mario's row; verified against the first goomba.
            enemies.append((int(ram[0x6E + i]) * 256 + int(ram[0x87 + i]), int(ram[0xCF + i]) + 8))
    rows = []
    for dy in range(-6, 7):
        row = []
        for dx in range(-4, 16):
            x, y = mx + dx * 16, my + dy * 16
            ch = "#" if tile(ram, x, y) else "."
            if any(abs(ex - x) <= 8 and abs(ey - y) <= 8 for ex, ey in enemies):
                ch = "E"
            if dx == 0 and dy == 0:
                ch = "M"
            row.append(ch)
        rows.append("".join(row))
    return "\n".join(rows), mx, my


def speed_word(v: int) -> str:
    # NOTE: SMB horizontal speed byte 0x57, signed. Walking tops out near 24, running near 40.
    if v >= 32:
        return "running at full speed"
    if v >= 8:
        return "moving right slowly"
    if v <= -8:
        return "moving left"
    return "standing still"


def features(g: str, v: int = 0, airborne: bool | None = None) -> dict:
    """Describe the grid in fields: Mario is row 6, column 4; ground row is 7; right is +column."""
    rows = g.splitlines()
    look = range(1, 9)
    # NOTE: the grid is Mario-relative, so mid-jump his own row is air. Anchor on the ground under him.
    ground = next((r for r in range(7, 13) if rows[r][4] == "#"), 7)
    feet = ground - 1
    on_ground = (not airborne) if airborne is not None else ground == 7
    f = {"on_ground": on_ground, "wall_ahead": None, "gap_ahead": None, "enemy_ahead": None}
    for dx in look:
        if rows[feet][4 + dx] == "#":
            height = sum(1 for r in range(feet, -1, -1) if rows[r][4 + dx] == "#")
            f["wall_ahead"] = {"tiles": dx, "height": height}
            break
    def bottomless(col: int) -> bool:
        # A column with no solid tile from the ground row to the bottom of the grid; a step down is not a gap.
        return all(rows[r][col] == "." for r in range(ground, 13))

    for dx in look:
        if bottomless(4 + dx):
            width = 0
            while 4 + dx + width < 20 and bottomless(4 + dx + width):
                width += 1
            f["gap_ahead"] = {"tiles": dx, "width": width}
            break
    # Enemies anywhere ahead within the grid, with height above Mario's feet (0 = same level).
    seen = []
    for dx in range(1, 16):
        for r in range(13):
            if rows[r][4 + dx] == "E":
                seen.append({"tiles": dx, "up": feet - r})
    f["enemies"] = seen
    enemies = [e["tiles"] for e in seen if -1 <= e["up"] <= 1]
    f["enemies_ahead"] = enemies
    f["enemy_ahead"] = {"tiles": enemies[0]} if enemies else None
    f["enemies_behind"] = [dx for dx in range(1, 5) if any(rows[r][4 - dx] == "E" for r in (feet - 1, feet, ground))]
    behind = 0
    while behind < 4 and rows[feet][3 - behind] == "." and rows[ground][3 - behind] == "#":
        behind += 1
    f["clear_behind"] = behind
    parts = []
    w, gp, e = f["wall_ahead"], f["gap_ahead"], f["enemy_ahead"]
    parts.append(f"A solid wall {w['height']} tiles tall is {w['tiles']} tile(s) ahead." if w else "No wall ahead.")
    parts.append(f"There are {behind} tiles of clear ground behind Mario for a run-up.")
    parts.append(f"A gap {gp['width']} tiles wide is {gp['tiles']} tile(s) ahead." if gp else "Solid ground ahead.")
    if seen:
        def where(en: dict) -> str:
            if en["up"] > 1:
                return f"{en['tiles']} tiles ahead and {en['up']} tiles up (on top of something)"
            if en["up"] < -1:
                return f"{en['tiles']} tiles ahead and {-en['up']} tiles below"
            return f"{en['tiles']} tiles ahead at ground level"
        parts.append("Enemies: " + "; ".join(where(en) for en in seen[:4]) + ".")
    else:
        parts.append("No enemy ahead.")
    if f["enemies_behind"]:
        parts.append(f"An enemy is {f['enemies_behind'][0]} tile(s) behind Mario; walking left into it is death.")
    f["speed"] = speed_word(v)
    high, far = jump_reach(v)
    f["jump_reach_now"] = {"tiles_high": high, "tiles_far": far}
    f["jump_reach_at_full_speed"] = {"tiles_high": JUMP_TABLE[0][1], "tiles_far": JUMP_TABLE[0][2]}
    parts.append(("Mario is on the ground, " if f["on_ground"] else "Mario is in the air, ") + f["speed"] + ".")
    parts.append(f"A jump from this speed clears {high} tiles high and {far} tiles far; "
                 f"at full running speed it clears {JUMP_TABLE[0][1]} high and {JUMP_TABLE[0][2]} far.")
    if w:
        # The arc peaks halfway, so a wall must be about half the far reach away when the jump starts.
        if high >= w["height"]:  # verified by replay: a 4-tile standing jump lands on a 4-tall ledge
            f["jump_now_clears_wall"] = w["tiles"] <= max(1, far // 2) + 1 and w["tiles"] >= max(1, far // 2) - 1
            parts.append(f"Start the jump when the wall is about {max(1, far // 2)} tiles ahead.")
        else:
            f["jump_now_clears_wall"] = False
            parts.append("A jump from this speed is not high enough for this wall; more speed is needed.")
    # Headroom: blocks above the arc cut a jump short. Clearance in tiles above the feet over the arc.
    headroom = 13
    for dx in range(0, min(far, 15) + 1):
        for r in range(feet - 1, -1, -1):
            if rows[r][4 + dx] == "#":
                headroom = min(headroom, feet - r - 1)
                break
    if headroom < high:
        eff_far = max(1, round(far * headroom / high))
        f["headroom"] = headroom
        parts.append(f"Blocks overhead {headroom + 1} tiles up cap the jump: it would land about {eff_far} tiles ahead instead of {far}.")
    else:
        eff_far = far
    # Landing zone: enemies walk toward Mario about 2 tiles during a one-second jump.
    land = [en for en in seen if abs(en["up"]) <= 1 and eff_far - 4 <= en["tiles"] <= eff_far + 1]
    f["enemies_near_landing_spot"] = [en["tiles"] for en in land]
    parts.append(f"A jump right now would land about {eff_far} tiles ahead"
                 + (", where an enemy will be by then." if land else ", on clear ground."))
    on_wall = [en for en in seen if w and en["tiles"] in (w["tiles"], w["tiles"] + 1) and en["up"] >= 1]
    if on_wall:
        clear_over = JUMP_TABLE[0][1] >= w["height"] + 2
        parts.append("An enemy is on top of the wall ahead: jumping onto it is death. "
                     + ("A full-speed jump started 4 tiles before the wall clears the wall and the enemy together."
                        if clear_over else "Back off, wait for it to move, then jump when the top is clear."))
    if enemies and enemies[0] <= 1:
        parts.append("An enemy is right in front of Mario: moving toward it is death. Jump in place if standing still; "
                     "momentum carries into a jump, so at speed a full jump forward is the only way over it.")
    if f["enemies_behind"] and f["enemies_behind"][0] <= 2:
        parts.append("The enemy behind reaches Mario in about a second: jump over it or away from it, but not into another enemy.")
    f["summary"] = " ".join(parts)
    return f


def ask_jev(client: httpx.Client, state: dict, macro: bool) -> tuple[str, dict, int, float]:
    criteria = dict(ACTION_HELP)
    rules = (
        "You control Mario in Super Mario Bros. Pick the joypad action that moves right as far as possible "
        "without dying. A jump action is held until Mario lands, so one decision is one full jump. "
        "Mario must jump over walls, gaps and enemies. A jump clears a wall only if its reach in tiles high "
        "is at least the wall height, and a gap only if its reach in tiles far exceeds the gap width plus 1. "
        "Reach grows with speed: 'run right' reaches full speed after about 3 decisions on clear ground. "
        "When the current reach is not enough and the obstacle is closer than 6 tiles, choose "
        "'back off for a run-up', then 'run right' until full speed, then 'run and jump right' 2 to 3 tiles out. "
        "A jump arc peaks halfway, so start a jump over a wall when the wall is about half the far reach "
        "ahead (4 tiles at full speed, 2 at walking speed); jumping from closer hits the wall and drops. "
        "Start a jump over a gap 1 tile before its edge. Against an enemy ahead, jump when it is 2 to 3 tiles "
        "away. An enemy 1 or 2 tiles behind Mario will hit him within a second: jump immediately. "
        "The 'summary' field describes what is ahead; the grid is the same information drawn out. "
    )
    if macro:
        criteria[MACRO] = MACRO_HELP
        rules += f"'{MACRO}' handles the run-up for you. "
    body = {
        "state": state,
        "model": "jev-latest",
        "questions": {"action": {"type": "choice", "instructions": rules + GRID_LEGEND, "criteria": criteria}},
    }
    t0 = time.perf_counter()
    r = client.post(JEV_URL, headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}, json=body)
    lat = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    a = d["answers"]["action"]
    return a["choice"], a["probabilities"], d["usage"]["input_tokens"], lat


def run(bot: str, dump: bool, level: str = "1-1", macro: bool = False) -> dict:
    env = JoypadSpace(gym_super_mario_bros.make(f"SuperMarioBros-{level}-v0", apply_api_compatibility=True), SIMPLE_MOVEMENT)
    ram = nes(env).ram
    obs, _ = env.reset()
    # NOTE: nes_py reuses one screen buffer, so every stored frame must be a copy.
    frames, decisions, tokens, lats, best = [obs.copy()], 0, 0, [], 0
    action, info, frame, hold_cap = 0, {"x_pos": 0, "flag_get": False}, 0, FULL_JUMP_FRAMES
    last_best, last_gain, prev = 0, 0, 0
    term = trunc = False
    replay_script, replay_tail, replay_i = [], [], 0
    if bot.startswith("replay:"):
        _, path, tail = bot.split(":", 2)
        replay_script = [json.loads(l)["choice"] for l in Path(path).read_text().splitlines()]
        replay_tail = tail.split(",")
    client = httpx.Client(timeout=30)
    log = []
    while frame < MAX_FRAMES:
        if frame % HOLD == 0 and (bot == "jev" or bot.startswith("replay:")) and not dump:
            # Decide only on the ground: keep the current direction (without A) while airborne.
            fall = 0
            while ram[0x1D] and fall < 120:
                obs, _, term, trunc, info = env.step(RELEASE.get(action, action))
                frame += 1
                fall += 1
                if fall % 2 == 0:
                    frames.append(obs.copy())
                if term or trunc:
                    break
            if term or trunc:
                break
            frame += (-frame) % HOLD
        if frame % HOLD == 0:
            g, mx, my = grid(ram)
            v = int(ram[0x57])
            feats = features(g, v - 256 if v > 127 else v, airborne=bool(ram[0x1D]))
            state = {"summary": feats.pop("summary"), **feats, "grid": g, "action_before": action}
            if bot == "jev":
                if dump:
                    print(f"frame {frame} x={info['x_pos']}\n{g}\n")
                    action = 4 if (frame // 12) % 2 == 0 else 3
                else:
                    name, probs, tok, lat = ask_jev(client, state, macro)
                    action, tokens = (-1 if name == MACRO else ACTIONS[name]), tokens + tok
                    hold_cap = HOP_FRAMES if name == "hop right" else FULL_JUMP_FRAMES
                    lats.append(lat)
                    log.append({"frame": frame, "x": int(info["x_pos"]), "choice": name, "p": round(probs[name], 2),
                                "probs": {k: round(v, 2) for k, v in probs.items()}, "summary": state["summary"],
                                "grid": g})
            elif bot == "alternate":
                action = 4 if (frame // 12) % 2 == 0 else 3
            elif bot.startswith("replay:"):
                # replay:<log.jsonl>:<action>[,<action>...] — replay logged choices, then the given tail.
                name = replay_script.pop(0) if replay_script else replay_tail[min(replay_i, len(replay_tail) - 1)]
                if not replay_script:
                    replay_i += 1
                action = -2 if name == "back off for a run-up" else ACTIONS[name]
                hold_cap = HOP_FRAMES if name == "hop right" else FULL_JUMP_FRAMES
                log.append({"frame": frame, "x": int(info["x_pos"]), "choice": name, "summary": state["summary"], "grid": g})
            else:
                action = ACTIONS[bot]
            decisions += 1
            # NOTE: the NES only jumps on an A press, not a hold. Release A for one frame between decisions.
            if action == -1:
                # Macro, tuned by sweep at the 4-tall pipe: back up until the obstacle is 6 tiles
                # ahead, run until it is 3 ahead, jump and hold A for 30 frames.
                def near() -> int:
                    fe = features(grid(ram)[0], 48)
                    return (fe["wall_ahead"] or fe["gap_ahead"] or {}).get("tiles", 99)

                def enemy_close() -> bool:
                    e = features(grid(ram)[0], 48)["enemy_ahead"]
                    return bool(e) and e["tiles"] <= 2

                # An enemy inside 2 tiles during the run-up aborts the plan with an immediate jump.
                plan = [(6, lambda: near() >= 6, 120), (3, lambda: near() <= 3 or enemy_close(), 120), (4, lambda: False, 30)]
                for act, done_when, limit in plan:
                    for i in range(limit):
                        obs, _, term, trunc, info = env.step(act)
                        frame += 1
                        if i % 2 == 0:
                            frames.append(obs.copy())
                        if done_when() or term or trunc:
                            break
                    if term or trunc:
                        break
                frame += (-frame) % HOLD  # realign so the next decision comes on the next boundary
                action, prev = 3, 0
                if term or trunc:
                    break
            if action == -2:
                # Atomic back-off: walk left until the obstacle ahead is 6 tiles away, at most 60 frames.
                for i in range(60):
                    obs, _, term, trunc, info = env.step(6)
                    frame += 1
                    if i % 2 == 0:
                        frames.append(obs.copy())
                    fe = features(grid(ram)[0], 0)
                    near = (fe["wall_ahead"] or fe["gap_ahead"] or {}).get("tiles", 99)
                    if term or trunc or near >= 6:
                        break
                if term or trunc:
                    break
                frame += (-frame) % HOLD
                action = 0
            if action in JUMPS and prev in JUMPS:
                obs, _, term, trunc, info = env.step(RELEASE[action])
                frame += 1
                if term or trunc:
                    break
            prev = action
            if action in JUMPS and bot != "alternate" and not dump:
                # One decision is one jump: hold A until Mario lands, or hold_cap frames for a hop.
                # A full-speed jump lasts about 50 frames, so the cap must exceed that.
                for i in range(FULL_JUMP_FRAMES):
                    obs, _, term, trunc, info = env.step(action)
                    frame += 1
                    if i % 2 == 0:
                        frames.append(obs.copy())
                    if term or trunc or i >= hold_cap or (i > 4 and ram[0x1D] == 0):
                        break
                best = max(best, int(info["x_pos"]))
                if term or trunc:
                    break
                frame += (-frame) % HOLD
                action = RELEASE[action]
        obs, _, term, trunc, info = env.step(action)
        frame += 1
        best = max(best, int(info["x_pos"]))
        if frame % 2 == 0:
            frames.append(obs.copy())
        if best > last_best:
            last_best, last_gain = best, frame
        if term or trunc or info["flag_get"] or frame - last_gain > STALL_FRAMES:
            break
    env.close()
    RUNS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = f"{level}-{'replay' if bot.startswith('replay:') else bot.replace(' ', '-')}"
    gif = RUNS / f"{tag}-{stamp}.gif"
    if not dump:
        imageio.mimsave(gif, frames, duration=1 / 30, loop=0)
    result = {
        "level": level, "bot": bot, "macro": macro, "stamp": stamp, "best_x": best, "flag": bool(info["flag_get"]), "frames": frame,
        "decisions": decisions, "api_calls": len(lats), "input_tokens": tokens,
        "cost_usd": round(tokens * USD_PER_TOKEN, 5),
        "latency_p50": round(sorted(lats)[len(lats) // 2], 3) if lats else None,
        "gif": gif.name if not dump else None,
    }
    if not dump:
        with (RUNS / "results.jsonl").open("a") as f:
            f.write(json.dumps(result) + "\n")
        if log:
            (RUNS / f"{tag}-{stamp}.log.jsonl").write_text("\n".join(json.dumps(l) for l in log) + "\n")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="jev", help="jev | alternate | <action name> | replay:<log.jsonl>:<action,...>")
    ap.add_argument("--dump", action="store_true", help="print grids instead of calling Jev")
    ap.add_argument("--level", default="1-1", help="world-stage, e.g. 2-1")
    ap.add_argument("--macro", action="store_true", help="also offer the harness-executed running-jump macro")
    a = ap.parse_args()
    load_env()
    print(json.dumps(run(a.bot, a.dump, a.level, a.macro)))
