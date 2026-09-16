#!/usr/bin/env python3
"""Mock-only regression coverage for the optional NM D-Bus health probe.

This imports fixture data from the earlier fast-path harness by parsing its AST;
the earlier runner is never imported or executed.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "fixtures" / "wifi_fastpath_fixtures.py"
BASELINE = HERE / "fixtures" / "wifi-reconnect-baseline"
CANDIDATE = HERE.parents[2] / "roles" / "wifi_reconnect" / "files" / "wifi-reconnect"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_values():
    tree = ast.parse(REFERENCE.read_text())
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {"mock", "cases"}:
                values[target.id] = ast.literal_eval(node.value)
    if set(values) != {"mock", "cases"}:
        raise RuntimeError("could not extract mock and cases from reference harness")
    return values["mock"], values["cases"]


def mock_source():
    source, _ = fixture_values()
    source = source.replace("import json,os,sys", "import json,os,sys,signal")
    old = "if name=='timeout':os.execvp(a[1],a[1:])"
    new = """if name=='timeout':
 if len(a)>=2 and Path(a[1]).name=='owl-nm-wifi-healthy':
  mode=os.environ['MOCK_HELPER_MODE']
  if mode=='ok':sys.exit(0)
  if mode=='exit_1':sys.exit(1)
  if mode=='timeout_124':sys.exit(124)
  if mode=='signal_term':os.kill(os.getpid(),signal.SIGTERM)
  raise RuntimeError('unknown helper mode '+mode)
 os.execvp(a[1],a[1:])"""
    if old not in source:
        raise RuntimeError("reference mock timeout implementation changed")
    return source.replace(old, new)


def materialize_case(root, case):
    run = root / "run"
    run.mkdir()
    (root / "log").mkdir()
    if case.get("started", True):
        (run / "started").touch()
    for name, contents in case.get("markers", {}).items():
        (run / name).write_text(contents + "\n")


def run_variant(script_source, case, helper_mode):
    with tempfile.TemporaryDirectory(prefix="owl-wifi-helper-") as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        mock = bin_dir / "mock"
        mock.write_text("#!" + sys.executable + "\n" + mock_source())
        mock.chmod(0o755)
        for name in ("nmcli", "systemctl", "iw", "timeout", "date", "stat", "sleep", "iwgetid", "ip"):
            (bin_dir / name).symlink_to(mock)

        helper = root / "owl-nm-wifi-healthy"
        if helper_mode != "missing":
            helper.write_text("#!/bin/sh\nexit 99\n")
            helper.chmod(0o755)

        script = root / "wifi-reconnect"
        test_source = script_source.replace("/usr/local/libexec/owl-nm-wifi-healthy", str(helper))
        test_source = test_source.replace("STATE_DIR=/run/wifi-reconnect", f"STATE_DIR={root / 'run'}")
        test_source = test_source.replace("LOG_DIR=/data/log", f"LOG_DIR={root / 'log'}")
        test_source = test_source.replace("/usr/sbin/iwgetid", "iwgetid")
        script.write_text(test_source)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        materialize_case(root, case)
        trace = root / "trace.jsonl"
        env = os.environ.copy()
        env.update({
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "MOCK_CASE": json.dumps(case),
            "MOCK_RUNTIME": str(root / "released"),
            "MOCK_TRACE": str(trace),
            "MOCK_HELPER_MODE": helper_mode,
            **case.get("env", {}),
        })
        proc = subprocess.run(["bash", str(script)], cwd=root, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        files = {}
        for directory in (root / "run", root / "log"):
            if directory.exists():
                for p in sorted(directory.rglob("*")):
                    if p.is_file():
                        files[str(p.relative_to(root))] = p.read_text()
        entries = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        # Read-only NM queries can be launched through process substitutions and
        # complete in a different order. Gate the externally mutating operations.
        actions = [entry for entry in entries if
                   (entry[0] == "nmcli" and "--wait" in entry) or
                   (entry[0] == "systemctl" and len(entry) > 1 and entry[1] == "stop")]
        return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
                "actions": actions, "files": files, "trace": entries}


def compare(baseline, candidate):
    return {key: baseline[key] == candidate[key]
            for key in ("rc", "stdout", "stderr", "actions", "files")}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def expected_sigterm_diagnostic(stderr):
    """Accept Bash's observed macOS and GNU/Linux rendering of SIGTERM only."""
    # GNU Bash in the ARM Bookworm container emits just "Terminated\n".
    # macOS Bash includes the location, PID, signal number, and command.
    return stderr == "Terminated\n" or bool(re.fullmatch(
        r".+: line \d+: +\d+ Terminated: 15[ \t]+timeout 3 .+\n", stderr))


def main():
    _, cases = fixture_values()
    case_list = list(cases.values())
    baseline_source = BASELINE.read_text()
    candidate_source = CANDIDATE.read_text()
    workers = int(os.environ.get("OWL_WIFI_TEST_WORKERS", "1"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        baseline_values = list(pool.map(
            lambda pair: run_variant(baseline_source, pair[1], "missing"), cases.items()))
    baseline_runs = dict(zip(cases, baseline_values))
    results = {"fallback": {}, "signal_term_diagnostics": [], "successful_probe": {}, "down_since_bypass": None,
               "firstboot_log_retained": None}
    all_ok = True
    for mode in ("missing", "exit_1", "timeout_124", "signal_term"):
        failed = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            after_values = list(pool.map(lambda case: run_variant(candidate_source, case, mode), case_list))
        for case_name, case, after in zip(cases, case_list, after_values):
            before = baseline_runs[case_name]
            dimensions = compare(before, after)
            if mode == "signal_term":
                # Bash reports its foreground command being terminated, while the
                # script continues through the ordinary fallback path.
                probe_called = any(entry[0] == "timeout" and len(entry) >= 3 and
                                   Path(entry[2]).name == "owl-nm-wifi-healthy"
                                   for entry in after["trace"])
                diagnostic_matches = expected_sigterm_diagnostic(after["stderr"])
                dimensions["stderr"] = (diagnostic_matches and not before["stderr"]) if probe_called else before["stderr"] == after["stderr"]
                results["signal_term_diagnostics"].append({
                    "case": case_name,
                    "probe_called": probe_called,
                    "baseline_stderr": before["stderr"],
                    "candidate_stderr": after["stderr"],
                    "candidate_matches_expected_platform_diagnostic": diagnostic_matches,
                })
            if not all(dimensions.values()):
                failed.append({"case": case_name, "dimensions": dimensions,
                               "baseline_stderr": before["stderr"], "candidate_stderr": after["stderr"]})
        results["fallback"][mode] = {"cases": len(case_list), "failures": failed}
        all_ok &= not failed

    by_name = cases
    for name in ("healthy_wifi", "healthy_clears_stale_markers"):
        before = baseline_runs[name]
        after = run_variant(candidate_source, by_name[name], "ok")
        no_network_commands = not any(entry[0] in {"nmcli", "systemctl"} for entry in after["trace"])
        dimensions = compare(before, after)
        # Only output and resulting state are required to agree: the probe intentionally
        # removes the normal nmcli action on this healthy short path.
        output_state_equal = all(dimensions[key] for key in ("rc", "stdout", "stderr", "files"))
        results["successful_probe"][name] = {
            "no_nmcli_or_systemctl": no_network_commands,
            "output_and_state_equal_baseline": output_state_equal,
            "trace": after["trace"],
        }
        all_ok &= no_network_commands and output_state_equal

    outage = run_variant(candidate_source, by_name["ethernet_during_outage"], "ok")
    bypassed = not any(entry[0] == "timeout" for entry in outage["trace"])
    same = compare(baseline_runs["ethernet_during_outage"], outage)
    results["down_since_bypass"] = {"case": "ethernet_during_outage", "no_probe_timeout": bypassed,
                                    "full_behavior_equal_baseline": all(same.values())}
    all_ok &= bypassed and all(same.values())

    firstboot = run_variant(candidate_source, by_name["first_boot"], "ok")
    firstboot_log = firstboot["files"].get("log/wifi-reconnect.log", "")
    retained = "EVENT=start" in firstboot_log
    firstboot_equal = compare(baseline_runs["first_boot"], firstboot)
    results["firstboot_log_retained"] = {
        "case": "first_boot", "success_probe_configured": True,
        "event_start_present": retained, "full_behavior_equal_baseline": all(firstboot_equal.values())
    }
    all_ok &= retained and all(firstboot_equal.values())

    report = {
        "pass": bool(all_ok),
        "reference_fixture": str(REFERENCE),
        "reference_fixture_sha256": sha256(REFERENCE),
        "baseline_sha256": sha256(BASELINE),
        "candidate_sha256": sha256(CANDIDATE),
        "case_count": len(case_list),
        "workers": workers,
        "results": results,
    }
    serialized = json.dumps(report, sort_keys=True, indent=2)
    report_path = os.environ.get("OWL_WIFI_TEST_REPORT")
    if report_path:
        Path(report_path).write_text(serialized + "\n")
    print(serialized)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
