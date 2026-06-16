"""
Continuous Data Collection Orchestrator
=======================================
Launches and supervises all three data collectors simultaneously:
  1. Paper traders (WIF, PEPE, CRV) — live signal + trade PnL data
  2. Fill-rate trackers (same pairs) — passive execution quality data
  3. Stat-arb collector — broad microstructure data (order book, funding, OI)

Each subprocess auto-restarts on crash. The orchestrator:
  - Prevents Windows sleep
  - Logs all subprocess restarts with timestamps
  - Prints unified status every 5 minutes
  - Gracefully shuts down on Ctrl+C (saves all state)

Usage:
    python experiments/collect_all.py                    # default 5-day run
    python experiments/collect_all.py --hours 120        # 5 days
    python experiments/collect_all.py --hours 24 --no-ws # 1 day, REST only
    python experiments/collect_all.py --skip-statarb     # paper + fill only
    python experiments/collect_all.py --skip-fill-rate   # paper + statarb only

This is the "leave it running for 5 days" script that builds the full dataset
needed for credible resume claims:
  - 50+ live trades per asset for Sharpe ratio
  - Fill-rate and adverse selection measurements
  - Multi-day microstructure history for regime analysis
"""

import subprocess
import sys
import os
import time
import signal
import ctypes
import json
from datetime import datetime, timezone
from pathlib import Path
import argparse


# ---------------------------------------------------------------------------
# Windows sleep prevention
# ---------------------------------------------------------------------------

def prevent_sleep():
    if sys.platform == "win32":
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        print("  [POWER] Windows sleep inhibited.")


def allow_sleep():
    if sys.platform == "win32":
        ES_CONTINUOUS = 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


# ---------------------------------------------------------------------------
# Subprocess manager
# ---------------------------------------------------------------------------

class ManagedProcess:
    """A subprocess that auto-restarts on crash."""

    def __init__(self, name: str, cmd=None, cwd: str = None, cmd_factory=None):
        """
        cmd:         a static command list, OR
        cmd_factory: a zero-arg callable returning the command list. Re-evaluated
                     on every (re)start so the command can change between runs —
                     used by the stat-arb collector to switch to ``--resume`` once
                     a checkpoint exists, so restarts continue the same run dir.
        """
        if cmd is None and cmd_factory is None:
            raise ValueError("ManagedProcess needs either cmd or cmd_factory")
        self.name = name
        self.cmd_factory = cmd_factory
        self.cmd = cmd if cmd is not None else cmd_factory()
        self.cwd = cwd
        # Each child writes to its own log file rather than a PIPE. Over a
        # multi-day run an undrained PIPE buffer fills and blocks the child;
        # a file sink avoids that entirely and leaves a persistent per-process
        # log to inspect later.
        log_root = Path(cwd) / "data" / "logs" if cwd else Path("data") / "logs"
        self.log_path = log_root / f"{name}.log"
        self._log_fh = None
        self.process: subprocess.Popen | None = None
        self.restart_count = 0
        self.start_time = 0.0
        self.total_uptime = 0.0
        self.last_crash_time = 0.0
        self.backoff = 1.0

    def _tail_log(self, n: int = 5) -> list[str]:
        """Return the last n non-empty lines of this process's log file."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                data = f.read().decode("utf-8", errors="replace")
            lines = [ln.rstrip() for ln in data.splitlines() if ln.strip()]
            return lines[-n:]
        except Exception:
            return []

    def start(self):
        """Start or restart the subprocess."""
        if self.cmd_factory is not None:
            self.cmd = self.cmd_factory()
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if self.restart_count == 0:
            print(f"  [{ts}] LAUNCH {self.name} (log: {self.log_path})")
        else:
            print(f"  [{ts}] RESTART {self.name} (attempt #{self.restart_count})")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_path, "ab")
        self._log_fh.write(
            f"\n===== {self.name} start @ {datetime.now(timezone.utc).isoformat()} "
            f"(restart #{self.restart_count}) =====\n".encode("utf-8")
        )
        self._log_fh.flush()

        self.process = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        self.start_time = time.time()

    def check(self) -> bool:
        """Check if process is alive. Returns True if alive, False if exited."""
        if self.process is None:
            return False
        ret = self.process.poll()
        if ret is None:
            return True

        # Process exited
        uptime = time.time() - self.start_time
        self.total_uptime += uptime
        self.last_crash_time = time.time()

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] CRASHED {self.name} | exit={ret} | uptime={uptime/60:.1f}m | "
              f"restarts={self.restart_count}")

        # Close the log handle and surface the last few lines for context.
        if self._log_fh is not None:
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        for line in self._tail_log():
            print(f"    [{self.name}] {line}")

        self.process = None
        return False

    def maybe_restart(self):
        """Restart with exponential backoff if crashed."""
        if self.process is not None:
            return  # still running

        # Backoff: don't restart too fast
        if time.time() - self.last_crash_time < self.backoff:
            return

        self.restart_count += 1
        self.backoff = min(self.backoff * 2, 120.0)  # max 2 min backoff
        self.start()

    def stop(self):
        """Gracefully stop the subprocess."""
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        except Exception:
            pass
        self.total_uptime += time.time() - self.start_time
        self.process = None
        if self._log_fh is not None:
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


# ---------------------------------------------------------------------------
# Process definitions
# ---------------------------------------------------------------------------

# Optimal pairs from optimization results
PAPER_TRADER_CONFIGS = [
    {
        "name": "PT-WIF",
        "asset": "WIF",
        "exchanges": "binance,cryptocom",
        "strategy": "zscore",
        "entry_z": "1.5",
        "exit_z": "0.3",
        "warmup": "20",
        "vol_filter": "0.8",
    },
    {
        "name": "PT-PEPE",
        "asset": "PEPE",
        "exchanges": "binance,cryptocom",
        "strategy": "ou",
        "entry_z": "1.5",
        "exit_z": "0.3",
        "warmup": "20",
        "vol_filter": "0.8",
    },
    {
        "name": "PT-CRV",
        "asset": "CRV",
        "exchanges": "cryptocom,mexc",
        "strategy": "ou",
        "entry_z": "1.5",
        "exit_z": "0.3",
        "warmup": "20",
        "vol_filter": "0.8",
    },
]

FILL_RATE_CONFIGS = [
    {"name": "FR-WIF", "asset": "WIF", "exchanges": "binance,cryptocom"},
    {"name": "FR-PEPE", "asset": "PEPE", "exchanges": "binance,cryptocom"},
    {"name": "FR-CRV", "asset": "CRV", "exchanges": "cryptocom,mexc"},
]


def _resolve_statarb_run(repo_root: str, hours: float) -> tuple[Path, bool]:
    """
    Decide which stat-arb run directory this orchestrator session should use.

    To make the collector survive both internal restarts and orchestrator-level
    relaunches (run_all.bat) as ONE continuous dataset, we pin a single run dir
    per session and record it in ``data/statarb/.orchestrator_active``.

    Returns (run_dir, is_resume). We reuse the recorded active run only if it has
    a checkpoint AND is still within its original time budget — this prevents
    resuming a long-finished run (which would exit instantly and restart-loop).
    """
    base = Path(repo_root) / "data" / "statarb"
    base.mkdir(parents=True, exist_ok=True)
    marker = base / ".orchestrator_active"

    if marker.exists():
        try:
            active = Path(marker.read_text(encoding="utf-8").strip())
            state_file = active / "_state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                start = datetime.fromisoformat(state["start_ts"])
                cfg_hours = state.get("config", {}).get("hours", hours)
                elapsed_h = (datetime.now(timezone.utc) - start).total_seconds() / 3600
                if elapsed_h < cfg_hours:
                    return active, True
        except Exception:
            pass  # fall through to a fresh run

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    active = base / run_id
    marker.write_text(str(active), encoding="utf-8")
    return active, False


def build_processes(args) -> list[ManagedProcess]:
    """Build the list of managed subprocesses based on CLI flags."""
    repo_root = str(Path(__file__).parent.parent.resolve())
    python = sys.executable
    procs = []

    ws_flag = ["--no-ws"] if args.no_ws else []

    # 1. Paper traders
    if not args.skip_paper:
        for cfg in PAPER_TRADER_CONFIGS:
            cmd = [
                python, "experiments/paper_trader.py",
                "--asset", cfg["asset"],
                "--exchanges", cfg["exchanges"],
                "--strategy", cfg["strategy"],
                "--entry-z", cfg["entry_z"],
                "--exit-z", cfg["exit_z"],
                "--warmup", cfg["warmup"],
                "--vol-filter", cfg["vol_filter"],
            ] + ws_flag
            procs.append(ManagedProcess(cfg["name"], cmd, repo_root))

    # 2. Fill-rate trackers
    if not args.skip_fill_rate:
        duration = str(int(args.hours * 3600))
        for cfg in FILL_RATE_CONFIGS:
            cmd = [
                python, "experiments/fill_rate_tracker.py",
                "--asset", cfg["asset"],
                "--exchanges", cfg["exchanges"],
                "--duration", duration,
            ] + ws_flag
            procs.append(ManagedProcess(cfg["name"], cmd, repo_root))

    # 3. Stat-arb broad collector (REST). Uses the tuned long-run settings so it
    #    stays within rate limits for a multi-day run. A cmd_factory lets every
    #    (re)start resume the same run dir once a checkpoint exists, so the
    #    dataset stays continuous across crashes and orchestrator relaunches.
    if not args.skip_statarb:
        statarb_dir, _ = _resolve_statarb_run(repo_root, args.hours)

        def _statarb_cmd(statarb_dir=statarb_dir):
            base_cmd = [python, "-m", "experiments.collect_statarb_data"]
            if (statarb_dir / "_state.json").exists():
                return base_cmd + ["--resume", str(statarb_dir)]
            return base_cmd + [
                "--hours", str(args.hours),
                "--interval", str(args.interval),
                "--slow-every", str(args.slow_every),
                "--assets", "volatile",
                # 1-min OHLCV is backfillable (issue #33); drop it live to keep
                # the snapshot cadence near --interval. Collection is now
                # per-exchange concurrent, so the remaining signals stay fast.
                "--skip-ohlcv",
                "--output-dir", str(statarb_dir),
            ]

        procs.append(ManagedProcess("STATARB", cmd_factory=_statarb_cmd, cwd=repo_root))

    return procs


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Continuous data collection orchestrator — runs paper traders, "
                    "fill-rate trackers, and stat-arb collector simultaneously"
    )
    parser.add_argument("--hours", type=float, default=168,
                        help="Total hours to run (default: 168 = 7 days)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between stat-arb REST snapshots (default: 60). "
                             "Higher = lighter rate-limit load over a long run.")
    parser.add_argument("--slow-every", type=int, default=10,
                        help="Collect slow/heavy stat-arb signals (funding, OI, "
                             "withdrawal/exchange status) every Nth snapshot "
                             "(default: 10).")
    parser.add_argument("--no-ws", action="store_true",
                        help="Force REST polling for all feeds. NOT recommended for "
                             "long runs: it pushes paper traders onto REST, competing "
                             "with the collector's rate-limit budget.")
    parser.add_argument("--skip-paper", action="store_true",
                        help="Skip paper trader processes")
    parser.add_argument("--skip-fill-rate", action="store_true",
                        help="Skip fill-rate tracker processes")
    parser.add_argument("--skip-statarb", action="store_true",
                        help="Skip broad stat-arb collector")
    args = parser.parse_args()

    total_sec = args.hours * 3600
    end_time = time.time() + total_sec

    print(f"\n{'='*60}")
    print(f"  CONTINUOUS DATA COLLECTION ORCHESTRATOR")
    print(f"  Started: {datetime.now(timezone.utc).isoformat()}")
    print(f"  Duration: {args.hours:.0f} hours ({args.hours/24:.1f} days)")
    print(f"  Feed mode: {'REST polling' if args.no_ws else 'WebSocket (REST fallback)'}")
    if not args.skip_statarb:
        print(f"  Stat-arb collector: interval={args.interval}s, slow-every={args.slow_every}")
    print(f"{'='*60}")

    # Build subprocess list
    procs = build_processes(args)
    if not procs:
        print("ERROR: All collectors disabled. Nothing to run.")
        sys.exit(1)

    print(f"\n  Launching {len(procs)} processes:")
    for p in procs:
        print(f"    - {p.name}: {' '.join(p.cmd[1:3])}")
    print()

    # Prevent Windows sleep
    prevent_sleep()

    # Start all
    for p in procs:
        p.start()
        time.sleep(0.5)  # stagger launches slightly

    # Status tracking
    last_status = time.time()
    status_interval = 300  # every 5 min
    log_path = Path(procs[0].cwd) / "data" / "orchestrator_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Signal handler for graceful shutdown
    shutdown = False

    def handle_signal(sig, frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while time.time() < end_time and not shutdown:
            time.sleep(5)

            # Check all processes and restart crashed ones
            for p in procs:
                if not p.check():
                    p.maybe_restart()

            # Periodic status
            if time.time() - last_status > status_interval:
                elapsed = time.time() - (end_time - total_sec)
                remaining = end_time - time.time()
                alive = sum(1 for p in procs if p.process is not None)
                total_restarts = sum(p.restart_count for p in procs)

                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\n  [{ts}] ORCHESTRATOR STATUS")
                print(f"    Elapsed: {elapsed/3600:.1f}h | Remaining: {remaining/3600:.1f}h")
                print(f"    Alive: {alive}/{len(procs)} | Total restarts: {total_restarts}")
                for p in procs:
                    status = "RUNNING" if p.process else "RESTARTING"
                    uptime = (time.time() - p.start_time) if p.process else 0
                    print(f"    {p.name:12s} {status:10s} uptime={uptime/60:.0f}m restarts={p.restart_count}")
                print()

                # Log to file
                log_entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "elapsed_h": round(elapsed / 3600, 2),
                    "alive": alive,
                    "total": len(procs),
                    "restarts": total_restarts,
                    "processes": {
                        p.name: {
                            "alive": p.process is not None,
                            "restarts": p.restart_count,
                            "uptime_min": round((time.time() - p.start_time) / 60, 1) if p.process else 0,
                        }
                        for p in procs
                    },
                }
                with open(log_path, "a") as f:
                    f.write(json.dumps(log_entry) + "\n")

                last_status = time.time()

    finally:
        print(f"\n  Shutting down {len(procs)} processes...")
        for p in procs:
            p.stop()

        allow_sleep()

        # Final summary
        total_uptime = sum(p.total_uptime for p in procs)
        total_restarts = sum(p.restart_count for p in procs)
        elapsed = time.time() - (end_time - total_sec)

        print(f"\n{'='*60}")
        print(f"  ORCHESTRATOR SHUTDOWN")
        print(f"  Total runtime: {elapsed/3600:.1f} hours")
        print(f"  Total restarts: {total_restarts}")
        for p in procs:
            print(f"    {p.name:12s} uptime={p.total_uptime/3600:.1f}h restarts={p.restart_count}")
        print(f"  Log: {log_path}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
