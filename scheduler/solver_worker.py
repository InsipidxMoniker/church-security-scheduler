"""One stdin JSON request / one stdout JSON result; no DB, keys, web or logging."""
import json
import sys


def main():
    try:
        from scheduler.solver import solve
        request = json.load(sys.stdin)
        result = solve(request["snapshot"], request["budget"])
    except Exception:
        # Exception values can include input. Only this fixed diagnostic crosses
        # the worker boundary; the parent never logs stderr or a request body.
        result = {"assignments": None, "metadata": {"status": "WORKER_FAILURE"},
                  "problems": ["Solver worker failed; draft unchanged."]}
    json.dump(result, sys.stdout, sort_keys=True)


if __name__ == "__main__":
    main()
