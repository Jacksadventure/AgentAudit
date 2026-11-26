"""Run an isolated OpenCode smoke task with paired instrumentation timing."""
import argparse
import os
import statistics
import subprocess
from pathlib import Path
from time import perf_counter

from debugger import ROOT, trajectory, write_new

TASK = "Load the ledger-policy skill, read ledger.txt with the read tool, then report the ledger total."


def run(args):
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = str(Path(args.binary).resolve()) if "/" in args.binary else args.binary
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    samples = {"off": [], "on": []}
    for index in range(args.pairs):
        order = ("off", "on") if index % 2 == 0 else ("on", "off")
        for mode in order:
            root = (args.out / f"{index}-{mode}").resolve()
            root.mkdir(mode=0o700)
            skills = root / ".opencode" / "skills" / "ledger-policy"
            skills.mkdir(parents=True)
            (skills / "SKILL.md").write_text(
                "---\nname: ledger-policy\ndescription: Ledger verification policy\n---\n"
                "Use the read tool to verify ledger.txt before reporting the total.\n"
            )
            (root / "ledger.txt").write_text("total: 42\n")
            plugins = root / ".opencode" / "plugins"
            plugins.mkdir()
            (plugins / "trace.js").symlink_to(ROOT / "trace.mjs")
            env = {**os.environ,
                   "OPENCODE_TRACE_VERSION": version, "OPENCODE_DISABLE_AUTOUPDATE": "true",
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
                   "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache")}
            env.pop("OPENCODE_TRACE_FILE", None)
            trace = root.parent / f"{root.name}.trajectory.jsonl"
            if mode == "on":
                env["OPENCODE_TRACE_FILE"] = str(trace)
            with (root.parent / f"{root.name}.raw.jsonl").open("x") as output:
                start = perf_counter()
                subprocess.run([binary, "run", "--format", "json", "--model", args.model, TASK],
                               cwd=root, env=env, stdout=output, check=True, timeout=args.timeout)
                elapsed = perf_counter() - start
            events = len(trajectory(trace)) if mode == "on" else 0
            if mode == "on" and not events:
                raise RuntimeError("No OpenCode events captured")
            samples[mode].append({"seconds": elapsed, "events": events,
                                  "trace_bytes": trace.stat().st_size if events else 0})
    deltas = [on["seconds"] - off["seconds"] for on, off in zip(samples["on"], samples["off"])]
    write_new(args.out / "overhead.json", {"samples": samples, "median_paired_seconds": statistics.median(deltas)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="opencode")
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    run(args)
