#!/usr/bin/env python3
"""Classify local git branches, worktrees and repo upkeep by cleanup risk. Read-only and offline.

Writes <data>/git/summary.json and one TSV list per class (consumed by bin/git_cleanup.py),
and prints a human summary for the snapshot.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
OUT = DATA / "git"
HOME = str(Path.home())
PROJECT_DIRS = os.environ.get("MAC_OPTIMIZER_PROJECT_DIRS", f"{HOME}/Projects {HOME}/Desktop {HOME}/code").split()
STALE_DAYS = int(os.environ.get("MAC_OPTIMIZER_BRANCH_DAYS", "30"))
FETCH_STALE_DAYS = 14
MAX_DEPTH = 4
SKIP_DIRS = {"node_modules", "vendor", "venv", "dist", "build", "target", "Pods"}
DEFAULT_PROTECTED = ["main", "master", "develop", "development", "staging", "production", "release/*", "hotfix/*"]
# git status would otherwise refresh and rewrite the index of every worktree it inspects.
ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
# A repo unpacked from an archive brings its own .git/config and hooks, and the scheduled scan runs git on it unattended.
SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
RISKY_KEY = re.compile(r"core\.(sshcommand|gitproxy|askpass|pager|editor|alternaterefscommand)|sequence\.editor|ssh\.variant"
                       r"|credential\..*helper|remote\..*\.(uploadpack|receivepack)|protocol\.(.*\.)?allow|gpg\..*program"
                       r"|(filter|diff|merge)\..*\.(clean|smudge|process|command|textconv|driver)")
LFS_FILTER = re.compile(r"git-lfs (clean -- %f|smudge -- %f|filter-process)")


def risky_config(path):
    """Repo or worktree config keys that make git run a command, other than the ones git-lfs installs."""
    r = subprocess.run(["git", "-C", str(path), "config", "--list", "--show-scope", "--includes", "-z"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, env=ENV)
    fields = r.stdout.split("\0")
    entries = [(scope, *kv.partition("\n")[::2]) for scope, kv in zip(fields[::2], fields[1::2])]
    return sorted({k for scope, k, v in entries if scope in ("local", "worktree")
                   and RISKY_KEY.fullmatch(k) and not (k.startswith("filter.lfs.") and LFS_FILTER.fullmatch(v))})


def git(repo, *args, timeout=120):
    try:
        r = subprocess.run(["git", *SAFE_GIT, "-C", str(repo), *args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=ENV, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout if r.returncode == 0 else ""


def tilde(p):
    p = str(p)
    return "~" + p[len(HOME):] if p.startswith(HOME + "/") else p


def protected_patterns():
    path = DATA / "protected-branches.txt"
    if not path.exists():
        return DEFAULT_PROTECTED
    return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]


def find_repos():
    repos = set()
    for root in PROJECT_DIRS:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(base):
            if (Path(dirpath) / ".git").is_dir():
                repos.add(os.path.realpath(dirpath))
            depth = len(Path(dirpath).relative_to(base).parts)
            dirnames[:] = [] if depth >= MAX_DEPTH else [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
    return sorted(repos)


def reflog(path):
    """(unix time, message) entries of a reflog file."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        head, _, msg = line.partition("\t")
        parts = head.rsplit(" ", 2)
        if len(parts) == 3 and parts[1].isdigit():
            out.append((int(parts[1]), msg))
    return out


def last_checkouts(gitdir):
    """Last time each branch was checked out or left, in any worktree, plus how far back the main reflog goes."""
    seen, since = {}, None
    logs = [gitdir / "logs/HEAD", *sorted((gitdir / "worktrees").glob("*/logs/HEAD"))]
    for i, path in enumerate(logs):
        entries = reflog(path)
        if i == 0 and entries:
            since = entries[0][0]
        for ts, msg in entries:
            if msg.startswith("checkout: moving from "):
                for name in msg[len("checkout: moving from "):].rsplit(" to ", 1):
                    seen[name] = max(seen.get(name, 0), ts)
    return seen, since


def worktrees(repo, gitdir):
    admin = {}
    for d in (gitdir / "worktrees").glob("*"):
        try:
            admin[os.path.realpath(Path((d / "gitdir").read_text().strip()).parent)] = d
        except OSError:
            pass
    out, cur = [], None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": val, "branch": "", "locked": False, "prunable": False}
        elif cur is None:
            continue
        elif key == "branch":
            cur["branch"] = val[len("refs/heads/"):]
        elif key in ("locked", "prunable"):
            cur[key] = True
        elif not line:
            out.append(cur)
            cur = None
    for wt in out[1:]:
        d = admin.get(os.path.realpath(wt["path"]))
        stamps = [(d / f).stat().st_mtime for f in ("logs/HEAD", "index", "HEAD") if d and (d / f).exists()]
        wt["last"] = int(max(stamps)) if stamps else 0
    return out


def default_ref(repo):
    ref = git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").strip()
    if ref:
        return ref
    for r in ("origin/main", "origin/master", "main", "master"):
        if git(repo, "rev-parse", "-q", "--verify", f"refs/{'remotes' if '/' in r else 'heads'}/{r}").strip():
            return r
    return ""


def object_stats(repo):
    stats = {}
    for line in git(repo, "count-objects", "-v").splitlines():
        k, _, v = line.partition(": ")
        stats[k] = int(v) if v.isdigit() else 0
    return stats


def du_gb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout.split()
    return round(int(out[0]) / 1048576, 2) if out else 0.0


def classify(b, in_use, protected, default_branch, merged):
    name, track = b["name"], b["track"]
    if name in in_use:
        return "keep", "en uso (checkout o worktree)"
    if name == default_branch or any(fnmatch.fnmatchcase(name, p) for p in protected):
        return "keep", "protegida"
    if b["days"] < STALE_DAYS:
        return "recent", ""
    if name in merged:
        return "low", "mergeada en la principal"
    if "ahead" in track:
        return "keep", "commits sin pushear"
    if "gone" in track:
        return "gone", "borrada en origin"
    if b["upstream"]:
        return "low", "sin cambios respecto de origin"
    return "keep", "solo local, sin mergear"


def scan(repo, protected, now):
    gitdir = Path(repo) / ".git"
    default = default_ref(repo)
    default_branch = default.split("/", 1)[-1]
    merged = set(git(repo, "branch", "--merged", default, "--format=%(refname:short)").split()) if default else set()
    wts = worktrees(repo, gitdir)
    in_use = {w["branch"] for w in wts if w["branch"]}
    seen, since = last_checkouts(gitdir)

    branches, counts, keep_why = [], {"low": 0, "gone": 0, "keep": 0, "recent": 0}, {}
    fmt = "%(refname:short)%09%(objectname)%09%(committerdate:unix)%09%(upstream:short)%09%(upstream:track)"
    for line in git(repo, "for-each-ref", "refs/heads", f"--format={fmt}").splitlines():
        name, sha, cdate, upstream, track = line.split("\t")
        updated = reflog(gitdir / "logs/refs/heads" / name)
        last = max(int(cdate or 0), seen.get(name, 0), updated[-1][0] if updated else 0)
        b = {"name": name, "sha": sha, "upstream": upstream, "track": track, "days": (now - last) // 86400}
        b["class"], b["why"] = classify(b, in_use, protected, default_branch, merged)
        counts[b["class"]] += 1
        if b["class"] == "keep":
            keep_why[b["why"]] = keep_why.get(b["why"], 0) + 1
        branches.append(b)

    wt_rows = []
    for w in wts[1:]:
        days = (now - w["last"]) // 86400 if w["last"] else None
        if w["prunable"]:
            cls, why = "prunable", "la carpeta ya no existe"
        elif w["locked"]:
            cls, why = "keep", "bloqueado (git worktree lock)"
        elif risky_config(w["path"]):
            cls, why = "keep", "config que ejecuta comandos"
        elif git(repo, "-C", w["path"], "status", "--porcelain").strip():
            cls, why = "keep", "cambios sin commitear"
        elif days is not None and days < STALE_DAYS:
            cls, why = "recent", ""
        else:
            cls, why = "stale", f"sin uso hace {days} días" if days is not None else "sin fecha de uso"
        wt_rows.append({"path": w["path"], "branch": w["branch"], "days": days, "class": cls, "why": why,
                        "gb": du_gb(w["path"]) if cls == "stale" else 0})

    fetch_head = gitdir / "FETCH_HEAD"
    fetch_days = int((now - fetch_head.stat().st_mtime) // 86400) if fetch_head.exists() else None
    has_origin = "origin" in git(repo, "remote").split()
    tracked = sum(1 for b in branches if b["upstream"] and "gone" not in b["track"] and b["class"] != "keep")
    objs = object_stats(repo)
    return {
        "repo": repo, "default": default, "branches": branches, "counts": counts, "keep_why": keep_why,
        "worktrees": wt_rows, "fetch_days": fetch_days, "reflog_since": since,
        "remote_refs": len(git(repo, "for-each-ref", "refs/remotes", "--format=x").split()),
        "git_gb": round((objs.get("size-pack", 0) + objs.get("size", 0) + objs.get("size-garbage", 0)) / 1048576, 2),
        "loose_mb": round(objs.get("size", 0) / 1024),
        "packs": objs.get("packs", 0),
        "needs_gc": objs.get("size", 0) > 100 * 1024 or objs.get("packs", 0) >= 20 or objs.get("size-garbage", 0) > 50 * 1024,
        "needs_remote_prune": has_origin and tracked > 0 and (fetch_days is None or fetch_days > FETCH_STALE_DAYS),
    }


def write_tsv(name, rows):
    (OUT / name).write_text("".join("\t".join(map(str, r)) + "\n" for r in rows))


def main():
    now = int(time.time())
    protected = protected_patterns()
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda r: (r, risky_config(r)), find_repos()))
        repos = list(pool.map(lambda r: scan(r, protected, now), [r for r, risky in found if not risky]))
    skipped = [{"repo": tilde(r), "keys": risky} for r, risky in found if risky]
    OUT.mkdir(parents=True, exist_ok=True)

    write_tsv("branches-low-risk.tsv", [(r["repo"], b["name"], b["sha"], b["why"])
                                        for r in repos for b in r["branches"] if b["class"] == "low"])
    write_tsv("branches-gone.tsv", [(r["repo"], b["name"], b["sha"], b["why"])
                                    for r in repos for b in r["branches"] if b["class"] == "gone"])
    write_tsv("worktrees-prunable.tsv", [(r["repo"],) for r in repos if any(w["class"] == "prunable" for w in r["worktrees"])])
    write_tsv("worktrees-stale.tsv", [(r["repo"], w["path"], w["branch"])
                                      for r in repos for w in r["worktrees"] if w["class"] == "stale"])
    write_tsv("repos-gc.tsv", [(r["repo"],) for r in repos if r["needs_gc"]])
    write_tsv("repos-remote-prune.tsv", [(r["repo"],) for r in repos if r["needs_remote_prune"]])

    total = {k: sum(r["counts"][k] for r in repos) for k in ("low", "gone", "keep", "recent")}
    keep_why = {}
    for r in repos:
        for k, v in r["keep_why"].items():
            keep_why[k] = keep_why.get(k, 0) + v
    stale_wt = [dict(w, repo=tilde(r["repo"])) for r in repos for w in r["worktrees"] if w["class"] == "stale"]
    summary = {
        "stale_days": STALE_DAYS, "protected_patterns": protected,
        "repos": len(repos), "branches": sum(total.values()), "branches_by_class": total, "keep_reasons": keep_why,
        "worktrees": {
            "prunable": sum(w["class"] == "prunable" for r in repos for w in r["worktrees"]),
            "stale": {"count": len(stale_wt), "gb": round(sum(w["gb"] for w in stale_wt), 2), "items": stale_wt},
            "dirty": sum(w["why"] == "cambios sin commitear" for r in repos for w in r["worktrees"]),
        },
        "gc": [{"repo": tilde(r["repo"]), "git_gb": r["git_gb"], "loose_mb": r["loose_mb"], "packs": r["packs"]}
               for r in repos if r["needs_gc"]],
        "remote_prune": [{"repo": tilde(r["repo"]), "fetch_days": r["fetch_days"]} for r in repos if r["needs_remote_prune"]],
        "skipped_risky_config": skipped,
        "per_repo": sorted(({"repo": tilde(r["repo"]), "default": r["default"], "branches": len(r["branches"]),
                             **r["counts"], "keep_reasons": r["keep_why"], "remote_refs": r["remote_refs"],
                             "fetch_days": r["fetch_days"], "git_gb": r["git_gb"],
                             "reflog_since": time.strftime("%Y-%m-%d", time.localtime(r["reflog_since"])) if r["reflog_since"] else None}
                            for r in repos), key=lambda x: -(x["low"] + x["gone"])),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"Repos: {summary['repos']}  local branches: {summary['branches']}  (inactive = no commit, update or checkout in {STALE_DAYS}d)")
    print(f"Branches low risk {total['low']:5d}  gone in origin {total['gone']:5d}  recent {total['recent']:5d}  kept {total['keep']:5d}")
    print("Kept because:", ", ".join(f"{k}={v}" for k, v in sorted(keep_why.items(), key=lambda x: -x[1])) or "-")
    print("Top repos (low / gone / total, last fetch):")
    for r in summary["per_repo"][:12]:
        if r["low"] + r["gone"]:
            fetch = f"{r['fetch_days']}d" if r["fetch_days"] is not None else "never"
            print(f"  {r['low']:5d} {r['gone']:5d} {r['branches']:5d}  fetch {fetch:>6s}  {r['repo']}")
    wt = summary["worktrees"]
    print(f"Worktrees: prunable {wt['prunable']}, stale {wt['stale']['count']} ({wt['stale']['gb']:.1f} GB), dirty {wt['dirty']}")
    for w in sorted(wt["stale"]["items"], key=lambda w: -w["gb"])[:10]:
        print(f"  {w['gb']:5.1f} GB  {w['why']:>22s}  {tilde(w['path'])}")
    print(f"Repos needing gc: {len(summary['gc'])}  remote prune (fetch > {FETCH_STALE_DAYS}d): {len(summary['remote_prune'])}")
    if skipped:
        print(f"Skipped, local git config runs commands ({len(skipped)}):")
        for s in skipped:
            print(f"  {s['repo']}: {', '.join(s['keys'])}")
    print(f"Lists: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
