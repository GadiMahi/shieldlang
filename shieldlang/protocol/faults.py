"""Named crash points for failure-injection tests.

A subprocess started with SHIELDLANG_CRASH_POINT=<name> terminates immediately (os._exit, no cleanup, portable to
Windows) when execution reaches that point. In normal operation the variable is unset and this is a no-op.
Points: after_reserve, after_send_intent, after_upstream_response, after_outcome_persisted.
"""
import os

CRASH_EXIT_CODE = 86
POINTS = ("after_reserve", "after_send_intent", "after_upstream_response", "after_outcome_persisted")


def crash_point(name: str) -> None:
    if os.environ.get("SHIELDLANG_CRASH_POINT") == name:
        os._exit(CRASH_EXIT_CODE)
