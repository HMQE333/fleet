"""Resolve a merge conflict in an append-only file (the progress log): ours, then what the other side appended since the base.

    python scripts/merge_log.py AUTONOMOUS_PROGRESS.md     # inside a conflicted merge; stages the result
"""
import pathlib
import subprocess
import sys

path = sys.argv[1]


def stage(n: int) -> str:
    return subprocess.run(["git", "show", f":{n}:{path}"], capture_output=True, text=True, encoding="utf-8", check=True).stdout


base, ours, theirs = stage(1), stage(2), stage(3)
if not (ours.startswith(base) and theirs.startswith(base)):
    sys.exit(f"{path}: not append-only on both sides; resolve by hand")
merged = ours.rstrip("\n") + "\n" + theirs[len(base):]
pathlib.Path(path).write_text(merged, encoding="utf-8", newline="\n")
subprocess.run(["git", "add", path], check=True)
print(f"{path}: ours + {len(theirs) - len(base)} characters appended by the other side")
