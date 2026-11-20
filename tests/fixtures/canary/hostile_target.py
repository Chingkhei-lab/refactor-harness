"""A target that misbehaves in the three ways a sandbox must contain.

Every attempt is recorded as a *value*, so the harness observation itself says
whether the container held: no exception escapes the case, and the test asserts on
what the target managed to do, not on how the harness felt about it.
"""

import os
import socket
import time


def escape(_arg):
    outcome = {"workdir": os.getcwd()}
    # Reported as evidence: the count proves pids_limit is what stopped the storm,
    # rather than the loop having finished early.
    outcome["forks_created"] = 0

    try:
        with open("/etc/harness-escape", "w", encoding="utf-8") as handle:
            handle.write("escaped")
        outcome["write"] = "wrote"
    except OSError as exc:
        outcome["write"] = type(exc).__name__

    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            outcome["net"] = "connected"
    except OSError as exc:
        outcome["net"] = type(exc).__name__

    # Children sleep, so they accumulate: a fork storm that exits immediately is
    # bounded by nothing and would "succeed" even with a tight pids_limit.
    children = []
    try:
        for _ in range(200):
            pid = os.fork()
            if pid == 0:
                time.sleep(5)
                os._exit(0)
            children.append(pid)
            outcome["forks_created"] = len(children)
        outcome["fork"] = "forked"
    except OSError as exc:
        outcome["fork"] = type(exc).__name__
        outcome["forks_created"] = len(children)
    for pid in children:
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
    return outcome
