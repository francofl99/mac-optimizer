#!/usr/bin/env python3
"""Apply a cleanup list written by bin/git_repos.py. Meant for the dashboard's Run button.

Usage: git_cleanup.py branches|worktree-prune|worktree-remove|gc|remote-prune <list.tsv>
"""
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def git(repo, *args, timeout=600):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=ENV, timeout=timeout)


def rows(path):
    by_repo = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            repo, *rest = line.split("\t")
            by_repo.setdefault(repo, []).append(rest)
    return by_repo


def du_mb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout.split()
    return int(out[0]) // 1024 if out else 0


def checked_out(repo):
    return {l[len("branch refs/heads/"):] for l in git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
            if l.startswith("branch refs/heads/")}


def branches(by_repo):
    backup = DATA / "backup" / f"git-branches-{time.strftime('%Y%m%d-%H%M%S')}.sh"
    backup.parent.mkdir(parents=True, exist_ok=True)
    total, failed = 0, False
    with backup.open("a") as restore:
        for repo, items in by_repo.items():
            if not Path(repo, ".git").is_dir():
                print(f"✘ {repo}: ya no es un repo, se omite")
                continue
            busy, delete, skipped = checked_out(repo), [], 0
            for name, sha, *_ in items:
                current = git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{name}").stdout.strip()
                # A branch that moved or got checked out since the scan is in use again.
                if current != sha or name in busy:
                    skipped += 1
                    continue
                delete.append(name)
                restore.write(f"git -C {shlex.quote(repo)} branch {shlex.quote(name)} {sha}\n")
            restore.flush()
            for i in range(0, len(delete), 200):
                r = git(repo, "branch", "-D", *delete[i:i + 200])
                sys.stdout.write(r.stdout + r.stderr)
                failed |= r.returncode != 0
            total += len(delete)
            print(f"✔ {repo}: {len(delete)} borradas, {skipped} omitidas (cambiaron o están en uso)")
    print(f"Total: {total} branches borradas. Para restaurarlas: bash {backup}")
    return failed


def worktree_prune(by_repo):
    failed = False
    for repo in by_repo:
        r = git(repo, "worktree", "prune", "-v")
        print(f"{repo}:\n{r.stdout}{r.stderr}".rstrip())
        failed |= r.returncode != 0
    return failed


def worktree_remove(by_repo):
    failed, freed = False, 0
    for repo, items in by_repo.items():
        for path, *_ in items:
            size = du_mb(path)
            # Without --force git refuses worktrees with modified or untracked files.
            r = git(repo, "worktree", "remove", path)
            if r.returncode == 0:
                freed += size
                print(f"✔ {path} ({size} MB)")
            else:
                failed = True
                print(f"✘ {path}: {r.stderr.strip()}")
    print(f"Liberado: {freed / 1024:.2f} GB")
    return failed


def gc(by_repo):
    failed = False
    for repo in by_repo:
        before = du_mb(f"{repo}/.git")
        r = git(repo, "gc")
        failed |= r.returncode != 0
        print(f"{'✔' if r.returncode == 0 else '✘'} {repo}: .git {before} MB → {du_mb(f'{repo}/.git')} MB")
    return failed


def remote_prune(by_repo):
    def prune(repo):
        try:
            return repo, git(repo, "remote", "prune", "origin", timeout=90)
        except subprocess.TimeoutExpired:
            return repo, None

    failed = False
    with ThreadPoolExecutor(8) as pool:
        for repo, r in pool.map(prune, by_repo):
            if r is None or r.returncode != 0:
                failed = True
                print(f"✘ {repo}: {'sin respuesta de origin (90s)' if r is None else r.stderr.strip()}")
                continue
            pruned = [l for l in r.stdout.splitlines() if "[pruned]" in l]
            print(f"✔ {repo}: {len(pruned)} referencias remotas borradas")
            for l in pruned:
                print(f"   {l.strip()}")
    print("Vuelve a correr el repaso para ver las branches que quedaron sin origin.")
    return failed


ACTIONS = {"branches": branches, "worktree-prune": worktree_prune, "worktree-remove": worktree_remove,
           "gc": gc, "remote-prune": remote_prune}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ACTIONS:
        sys.exit(__doc__.strip())
    sys.exit(1 if ACTIONS[sys.argv[1]](rows(sys.argv[2])) else 0)
