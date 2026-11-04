# Security policy

**English** · [Español](SECURITY.es.md)

## Supported versions

Only the latest commit on `main` is supported. Update with `git pull && ./install.sh`.

## Reporting a vulnerability

Report it privately through [GitHub security advisories](https://github.com/francofl99/mac-optimizer/security/advisories/new). Please don't open a public issue.

Include:

- what an attacker can do and from where (another site in the browser, another local user, a crafted report or config file),
- steps to reproduce, and the macOS and Python versions,
- the commit you tested.

## Scope

In scope:

- The dashboard server (`app/server.py`): bypassing the localhost-only binding, the `Host` check or the session token, running commands that don't come from a report on disk, or switching launch items outside the allowed set.
- Files under `~/.mac-optimizer/` created with permissions readable by other users.
- Shell or command injection in `install.sh`, `bin/` or the scheduled analysis.

Out of scope:

- Commands suggested by the analysis that you chose to run: the dashboard shows each command and asks for confirmation.
- Findings the security checks miss or misreport. Those are bugs; open a regular issue.
- Anything that requires an attacker already running code as your user.
