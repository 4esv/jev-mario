# jev-mario

[Jev](https://typesafe.ai) is a text-only decision model. This harness has it play Super Mario Bros 1-1 by reading the emulator RAM, describing the situation in text, and asking for a joypad action every 6 frames.

![run](runs/jev-20260918-101109.gif)

## Results

World 1-1, one life. A run ends at death, at the flag (x ≈ 3160), or after 6 game-seconds without progress.

| player | runs | x reached | calls per run | cost per run |
|---|---|---|---|---|
| hold "run and jump" (scripted) | 1 | 724 | 0 | $0 |
| alternate jump / run (scripted) | 1 | 677 | 0 | $0 |
| Jev | 6 | 687, 687, 687, 759, 769, 839 | 22–42 | < $0.002 |

Median decision latency: 180 ms.

Jev selects the correct action for the described state, including a multi-step "running jump" macro at pipes. It does not carry a plan across calls; multi-step behaviour has to be packaged as a single action and executed by the harness.

Known issue: the macro's back-up phase can walk Mario into an enemy. Three of the six runs ended this way at x=687.

## State

Each call sends:

- a one-sentence summary, e.g. `A solid wall 4 tiles tall is 1 tile ahead. There are 4 tiles of clear ground behind Mario for a run-up. No enemy ahead. Mario is on the ground, standing still.`
- the same information as fields (`wall_ahead`, `gap_ahead`, `enemy_ahead`, `clear_behind`, `on_ground`, `speed`)
- a 13×16 tile grid around Mario
- the previous action

and asks one `choice` question with 8 options. About 850 input tokens per call.

The grid alone was not sufficient: with only the grid, Jev did not jump at the first pipe. The derived fields resolved this.

## Run

```bash
cp .env.example .env            # TYPESAFE_API_KEY=...
uv sync
uv run python play.py --bot jev
uv run python play.py --bot "run and jump right"   # scripted baseline
uv run python play.py --bot jev --dump              # print the state without calling the API
```

Each run writes a GIF and a per-decision log to `runs/` and appends a line to `runs/results.jsonl`.

Emulator: `gym-super-mario-bros` with `nes-py`, pinned to `numpy<2` and `gym==0.26`.
