"""Jev plays Super Mario Bros 1-1 from a text grid of the emulator RAM.

    uv run python play.py --bot jev          # Jev picks the action every HOLD frames
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
    "running jump at the obstacle ahead": -1,  # macro, executed by the harness
}
JUMPS = {2, 4, 5}
RELEASE = {2: 1, 4: 3, 5: 0}  # same action without A
ACTION_HELP = {
    "stand": "do nothing this step",
    "walk right": "move right slowly",
    "jump right": "jump while moving right; clears one enemy or a small gap",
    "run right": "move right fast; does not jump",
    "run and jump right": "long high jump to the right; clears wide gaps and tall pipes",
    "jump in place": "jump straight up",
    "walk left": "back off to the left",
    "running jump at the obstacle ahead": "back up, run at full speed, and jump when the obstacle is 3 tiles "
    "ahead; takes about a second; use for a wall 3 or more tiles tall or a gap 3 or more tiles wide",
}

GRID_LEGEND = (
    "Text grid, 13 rows x 16 columns, each cell one 16px tile. Row 7 is Mario's row. "
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
        for dx in range(-4, 12):
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


def features(g: str, v: int = 0) -> dict:
    """Describe the grid in fields: Mario is row 6, column 4; ground row is 7; right is +column."""
    rows = g.splitlines()
    look = range(1, 9)
    # NOTE: the grid is Mario-relative, so mid-jump his own row is air. Anchor on the ground under him.
    ground = next((r for r in range(7, 13) if rows[r][4] == "#"), 7)
    feet = ground - 1
    f = {"on_ground": ground == 7, "wall_ahead": None, "gap_ahead": None, "enemy_ahead": None}
    for dx in look:
        if rows[feet][4 + dx] == "#":
            height = sum(1 for r in range(feet, -1, -1) if rows[r][4 + dx] == "#")
            f["wall_ahead"] = {"tiles": dx, "height": height}
            break
    for dx in look:
        if rows[ground][4 + dx] == ".":
            width = 0
            while 4 + dx + width < 16 and rows[ground][4 + dx + width] == ".":
                width += 1
            f["gap_ahead"] = {"tiles": dx, "width": width}
            break
    for dx in look:
        if any(rows[r][4 + dx] == "E" for r in (feet - 1, feet, ground)):
            f["enemy_ahead"] = {"tiles": dx}
            break
    behind = 0
    while behind < 4 and rows[feet][3 - behind] == "." and rows[ground][3 - behind] == "#":
        behind += 1
    f["clear_behind"] = behind
    parts = []
    w, gp, e = f["wall_ahead"], f["gap_ahead"], f["enemy_ahead"]
    parts.append(f"A solid wall {w['height']} tiles tall is {w['tiles']} tile(s) ahead." if w else "No wall ahead.")
    parts.append(f"There are {behind} tiles of clear ground behind Mario for a run-up.")
    parts.append(f"A gap {gp['width']} tiles wide is {gp['tiles']} tile(s) ahead." if gp else "Solid ground ahead.")
    parts.append(f"An enemy is {e['tiles']} tile(s) ahead." if e else "No enemy ahead.")
    f["speed"] = speed_word(v)
    parts.append(("Mario is on the ground, " if f["on_ground"] else "Mario is in the air, ") + f["speed"] + ".")
    f["summary"] = " ".join(parts)
    return f


def ask_jev(client: httpx.Client, state: dict) -> tuple[str, dict, int, float]:
    body = {
        "state": state,
        "model": "jev-latest",
        "questions": {
            "action": {
                "type": "choice",
                "instructions": "You control Mario in Super Mario Bros. Pick the joypad action for the next "
                "0.1 seconds that moves right as far as possible without dying. Mario must jump to get over "
                "a wall, a gap, or an enemy, and must start the jump 1 to 3 tiles before reaching it. "
                "A wall 2 or more tiles tall or a gap 3 or more tiles wide needs a running jump. "
                "A wall 3 or more tiles tall or a gap 3 or more tiles wide cannot be cleared from a standstill; "
                "use 'running jump at the obstacle ahead' for those. "
                "The 'summary' field describes what is ahead; the grid is the same information drawn out. "
                + GRID_LEGEND,
                "criteria": ACTION_HELP,
            }
        },
    }
    t0 = time.perf_counter()
    r = client.post(JEV_URL, headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}, json=body)
    lat = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    a = d["answers"]["action"]
    return a["choice"], a["probabilities"], d["usage"]["input_tokens"], lat


def run(bot: str, dump: bool) -> dict:
    env = JoypadSpace(gym_super_mario_bros.make("SuperMarioBros-1-1-v0", apply_api_compatibility=True), SIMPLE_MOVEMENT)
    ram = nes(env).ram
    obs, _ = env.reset()
    # NOTE: nes_py reuses one screen buffer, so every stored frame must be a copy.
    frames, decisions, tokens, lats, best = [obs.copy()], 0, 0, [], 0
    action, info, frame = 0, {"x_pos": 0, "flag_get": False}, 0
    last_best, last_gain, prev = 0, 0, 0
    client = httpx.Client(timeout=30)
    log = []
    while frame < MAX_FRAMES:
        if frame % HOLD == 0:
            g, mx, my = grid(ram)
            v = int(ram[0x57])
            feats = features(g, v - 256 if v > 127 else v)
            state = {"summary": feats.pop("summary"), **feats, "grid": g, "action_before": action}
            if bot == "jev":
                if dump:
                    print(f"frame {frame} x={info['x_pos']}\n{g}\n")
                    action = 4 if (frame // 12) % 2 == 0 else 3
                else:
                    name, probs, tok, lat = ask_jev(client, state)
                    action, tokens = ACTIONS[name], tokens + tok
                    lats.append(lat)
                    log.append({"frame": frame, "x": int(info["x_pos"]), "choice": name, "p": round(probs[name], 2)})
            elif bot == "alternate":
                action = 4 if (frame // 12) % 2 == 0 else 3
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
            if action in JUMPS and prev in JUMPS:
                env.step(RELEASE[action])
                frame += 1
            prev = action
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
    gif = RUNS / f"{bot}-{stamp}.gif"
    if not dump:
        imageio.mimsave(gif, frames, duration=1 / 30, loop=0)
    result = {
        "bot": bot, "stamp": stamp, "best_x": best, "flag": bool(info["flag_get"]), "frames": frame,
        "decisions": decisions, "api_calls": len(lats), "input_tokens": tokens,
        "cost_usd": round(tokens * USD_PER_TOKEN, 5),
        "latency_p50": round(sorted(lats)[len(lats) // 2], 3) if lats else None,
        "gif": gif.name if not dump else None,
    }
    if not dump:
        with (RUNS / "results.jsonl").open("a") as f:
            f.write(json.dumps(result) + "\n")
        if log:
            (RUNS / f"{bot}-{stamp}.log.jsonl").write_text("\n".join(json.dumps(l) for l in log) + "\n")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="jev", choices=["jev", "alternate", *ACTIONS])
    ap.add_argument("--dump", action="store_true", help="print grids instead of calling Jev")
    a = ap.parse_args()
    load_env()
    print(json.dumps(run(a.bot, a.dump)))
