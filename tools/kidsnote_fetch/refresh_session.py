"""Keep KIDSNOTE_SESSION_COOKIE alive without a human in the loop.

The cookie Kidsnote hands out lives 30 days from the login that minted it and
does not slide when the mirror uses it, so it dies on a predictable monthly
cadence and every mirror run after that fails until someone re-extracts it by
hand. This script does that re-extraction: probe the stored cookie, and only
when it is actually dead trade the stored credentials for a fresh one.

It deliberately does NOT log in every run. Kidsnote tracks concurrent sessions
(`already_login`) and can mark an account `blocked`, so a daily login would be
both wasteful and risky. Steady state is a single cheap probe per day.

Exit codes:
    0  cookie is usable (either reused or refreshed)
    1  cookie is dead and could not be refreshed
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import (  # noqa: E402
    _baseline_session,
    _load_env_file,
    _login_with_password,
    _resolve_secret,
    _session_is_live,
)

_LOGGER = logging.getLogger("kidsnote_refresh")


def _emit(name: str, value: str) -> None:
    """Publish a step output when running under GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", type=Path,
                    default=Path(__file__).resolve().parents[2] / ".env")
    ap.add_argument("--session-out", type=Path, required=True,
                    help="Where to write a freshly minted sessionid. Only "
                         "written when the stored cookie had to be replaced.")
    ap.add_argument("--force", action="store_true",
                    help="Skip the probe and log in even if the stored cookie "
                         "still works. Used to prove the login path works "
                         "before the cookie actually expires; on success the "
                         "fresh cookie replaces the stored one, which also "
                         "restarts the 30-day clock.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    env = _load_env_file(args.env_file) if args.env_file.exists() else {}
    cookie = _resolve_secret(env, "KIDSNOTE_SESSION_COOKIE")

    if args.force:
        _LOGGER.warning("--force: skipping probe, logging in to mint a fresh cookie")
    elif cookie:
        sess = _baseline_session()
        sess.cookies.set("sessionid", cookie, domain="www.kidsnote.com", path="/")
        live = _session_is_live(sess)
        if live is True:
            _LOGGER.info("Stored sessionid still valid - nothing to do")
            _emit("refreshed", "false")
            return 0
        if live is None:
            # Kidsnote was unreachable or erroring. Leave the secret alone and
            # let tomorrow's run decide; spending a login here could cost us a
            # working session for nothing.
            _LOGGER.warning("Could not verify sessionid - leaving it untouched")
            _emit("refreshed", "false")
            return 0
        _LOGGER.warning("Stored sessionid is dead - refreshing")
    elif not cookie:
        _LOGGER.warning("No stored sessionid - minting one")

    username = _resolve_secret(env, "KIDSNOTE_USERNAME")
    password = _resolve_secret(env, "KIDSNOTE_PASSWORD")
    if not username or not password:
        _LOGGER.error(
            "Cookie needs replacing but KIDSNOTE_USERNAME / KIDSNOTE_PASSWORD "
            "are not set, so it cannot be refreshed automatically."
        )
        _emit("refreshed", "false")
        return 1

    try:
        sess = _login_with_password(username, password)
    except RuntimeError as exc:
        _LOGGER.error("Automatic refresh failed: %s", exc)
        _emit("refreshed", "false")
        return 1

    fresh = sess.cookies.get("sessionid", domain="www.kidsnote.com")
    args.session_out.parent.mkdir(parents=True, exist_ok=True)
    args.session_out.write_text(fresh, encoding="utf-8")
    args.session_out.chmod(0o600)
    _LOGGER.info("Fresh sessionid written to %s", args.session_out)
    _emit("refreshed", "true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
