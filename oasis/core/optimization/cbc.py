"""CBC execution with a wall-clock budget and cooperative cancellation."""
from __future__ import annotations

import subprocess
from shutil import which
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import pulp


def solve_bounded_cbc(problem, deadline: float, *, cancel_check=None, heartbeat=None) -> str:
    """Include model export and CBC preprocessing in the caller's deadline.

    CBC's own time limit is not a wall-clock watchdog. Poll the child so a
    cancelled or expired search cannot remain blocked inside PuLP's wait().
    A killed solve supplies no certified incumbent; retain earlier completed
    probes instead of presenting an incomplete solution as optimal.
    """
    def check():
        if cancel_check:
            cancel_check()

    check()
    if monotonic() >= deadline:
        return "Not Solved"
    # CBC's serial mode is 0. Even 1 enables its parallel worker machinery,
    # which can stall on Windows without observing the solver's own time limit.
    system_cbc = which("cbc")
    solver = (pulp.COIN_CMD(path=system_cbc, msg=False, threads=0) if system_cbc
              else pulp.PULP_CBC_CMD(msg=False, threads=0))
    with TemporaryDirectory(prefix="oasis_cbc_") as directory:
        model = str(Path(directory) / "model.mps")
        result = str(Path(directory) / "result.sol")
        variables, variable_names, constraint_names, _ = problem.writeMPS(model, rename=1)
        check()
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "Not Solved"
        args = [solver.path, model]
        if problem.sense == pulp.LpMaximize:
            args.append("-max")
        args += ["-sec", str(remaining), "-threads", "0", "-timeMode", "elapsed",
                 "-solve", "-printingOptions", "all", "-solution", result]
        with subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as process:
            last_heartbeat = monotonic()
            try:
                while process.poll() is None:
                    check()
                    now = monotonic()
                    if now >= deadline:
                        return "Not Solved"
                    if heartbeat and now - last_heartbeat >= 1:
                        heartbeat()
                        last_heartbeat = now
                    try:
                        process.wait(timeout=min(0.1, deadline - now))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            if process.returncode:
                raise pulp.PulpSolverError(f"CBC exited with code {process.returncode}")
        check()
        status, values, reduced, prices, slacks, solution_status = solver.readsol_MPS(
            result, problem, variables, variable_names, constraint_names,
        )
        problem.assignVarsVals(values)
        problem.assignVarsDj(reduced)
        problem.assignConsPi(prices)
        problem.assignConsSlack(slacks, activity=True)
        problem.assignStatus(status, solution_status)
        # PuLP may label a time-limited integer incumbent 'Optimal'.
        if solution_status == pulp.LpSolutionIntegerFeasible:
            return "Feasible"
        return pulp.LpStatus[status]
