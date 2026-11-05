#!/usr/bin/env python3
"""Match worker-images user validation for initial runs and reruns."""

import json
import os
from pathlib import Path


def authorized(users, actor, triggering_actor):
    if not isinstance(users, list) or not users or not all(isinstance(u, str) and u for u in users):
        raise ValueError("RelOps allowlist must be a nonempty list of usernames")
    allowed = {user.casefold() for user in users}
    return bool(actor and actor.casefold() in allowed
                and (not triggering_actor or triggering_actor.casefold() in allowed))


if __name__ == "__main__":
    users = json.loads(Path(".github/relops.json").read_text())
    actor = os.environ.get("GITHUB_ACTOR", "")
    triggering = os.environ.get("GITHUB_TRIGGERING_ACTOR", "")
    if not authorized(users, actor, triggering):
        raise SystemExit(f"RelOps access denied for actor {actor!r} / triggering actor {triggering!r}")
    print(f"RelOps access verified for {actor} / {triggering or actor}")
