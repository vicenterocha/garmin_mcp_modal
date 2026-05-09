"""Pre-deploy guard for the Garmin MCP server.

Wraps `modal deploy main.py` with sanity checks so we don't ship code from a
dirty tree, the wrong branch, or a checkout that's behind origin/main. The
last one is what burned us: a stale worktree quietly redeployed pre-fix code.

Also blocks deploys when the local Garmin tokens are likely too old to survive
container startup. Garmin's OAuth2 token has a ~24h TTL, and refreshing it via
the exchange endpoint without recent SSO context gets 429'd by their anti-bot.
Deploying with stale tokens means the new container can't authenticate.

Usage:
    uv run python deploy.py            # enforce all checks
    uv run python deploy.py --force    # bypass all checks (emergencies)
"""

import argparse
import os
import subprocess
import sys
import time

TOKEN_PATH = os.path.expanduser("~/.garminconnect_base64")
TOKEN_MAX_AGE_HOURS = 20  # Garmin OAuth2 expires at ~24h; leave headroom


def _run(*cmd: str, check: bool = True) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, check=check
    ).stdout.strip()


def _check_clean() -> str | None:
    # `--quiet` returns 1 if there's any diff vs HEAD (tracked files only —
    # untracked artifacts like local debug scripts don't end up in the deploy).
    if subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode != 0:
        diff = subprocess.run(
            ["git", "diff", "--name-status", "HEAD"], capture_output=True, text=True
        ).stdout
        return f"working tree has uncommitted changes to tracked files:\n{diff}"
    return None


def _check_on_main() -> str | None:
    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        return f"not on 'main' (currently on '{branch}')"
    return None


def _check_up_to_date() -> str | None:
    subprocess.run(["git", "fetch", "origin", "main", "--quiet"], check=True)
    behind = int(_run("git", "rev-list", "--count", "HEAD..origin/main"))
    if behind > 0:
        return f"HEAD is {behind} commit(s) behind origin/main"
    return None


def _check_tokens_fresh() -> str | None:
    if not os.path.exists(TOKEN_PATH):
        return f"{TOKEN_PATH} does not exist; run `uv run python auth.py` first"
    age_hours = (time.time() - os.path.getmtime(TOKEN_PATH)) / 3600
    if age_hours > TOKEN_MAX_AGE_HOURS:
        return (
            f"{TOKEN_PATH} is {age_hours:.1f}h old (>{TOKEN_MAX_AGE_HOURS}h). "
            f"Run `uv run python auth.py` to refresh, then re-run deploy."
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="skip safety checks")
    args = parser.parse_args()

    if not args.force:
        checks = (_check_clean, _check_on_main, _check_up_to_date, _check_tokens_fresh)
        for check in checks:
            err = check()
            if err:
                print(f"✗ {err}", file=sys.stderr)
                print("Re-run with --force to override.", file=sys.stderr)
                return 1

    sha = _run("git", "rev-parse", "--short", "HEAD")
    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD")
    print(f"→ Deploying {sha} from {branch}")
    return subprocess.call(["uv", "run", "modal", "deploy", "main.py"])


if __name__ == "__main__":
    sys.exit(main())
