"""
Generates synthetic Flight-payload HTML pages of realistic sizes for
benchmarking, and a hidden "needle" chunk near the start (used to
benchmark early-exit search vs. full-page resolution) plus one near the
end (used to verify a worst-case search still completes reasonably).

Not a real page capture -- these are structurally representative
(numbered chunks, $-refs, nested objects, occasional text rows) but
synthetic, since redistributing a real site's payload isn't something
this project can do.
"""

import json
import random


def _row(i: int, rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.7:
        obj = {
            "id": i,
            "title": f"Item {i} - {'x' * rng.randint(5, 40)}",
            "price": rng.randint(100, 999999),
            "tags": [f"tag{i}-{j}" for j in range(rng.randint(0, 4))],
            "meta": {"views": rng.randint(0, 10000), "active": rng.random() > 0.5},
        }
        return f"{i}:{json.dumps(obj)}"
    elif kind < 0.85:
        # A $-ref to an earlier chunk, and a React-element-shaped array.
        ref_target = max(0, i - rng.randint(1, 5))
        return f'{i}:["$","div",null,{{"children":"${ref_target}"}}]'
    else:
        text = "Lorem ipsum " * rng.randint(3, 20)
        body = text.encode("utf-8")
        return f"{i}:T{len(body):x},{text}"


def generate_page(num_chunks: int, *, seed: int = 0, needle_keys=("__needle_price__", "__needle_title__")) -> str:
    """Build a synthetic page with `num_chunks` Flight rows, plus one
    "needle" chunk containing `needle_keys` inserted near the front
    (index ~5% in) so a `find_by_keys` search for it can complete after
    resolving only a small fraction of the page."""
    rng = random.Random(seed)
    rows = []
    needle_index = max(1, num_chunks // 20)
    for i in range(num_chunks):
        if i == needle_index:
            needle = {needle_keys[0]: 12345, needle_keys[1]: "The Needle Item"}
            rows.append(f"{i}:{json.dumps(needle)}")
        else:
            rows.append(_row(i, rng))
    payload = "\n".join(rows)
    # Split into a handful of push() calls, like a real streamed page,
    # rather than one giant call -- exercises the same
    # multi-push-call-reassembly path a real page would.
    n_calls = max(1, num_chunks // 200)
    chunk_size = (len(payload) // n_calls) + 1
    calls = []
    for start in range(0, len(payload), chunk_size):
        piece = payload[start:start + chunk_size]
        calls.append(f'<script>self.__next_f.push([1,{json.dumps(piece)}])</script>')
    return "".join(calls)


# Rough size targets used by benchmarks/run_benchmarks.py. Actual byte
# size depends on the random content generated, but these chunk counts
# land in roughly the right ballpark for "small/medium/large real page".
SIZE_PRESETS = {
    "small": 350,      # ~50KB
    "medium": 3500,    # ~500KB
    "large": 21000,    # ~3MB+
}
