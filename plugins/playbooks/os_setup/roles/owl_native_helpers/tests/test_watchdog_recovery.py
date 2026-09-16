#!/usr/bin/env python3
"""Mock-only regression test for the watchdog Docker metadata helper."""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "fixtures" / "watchdog_fixtures.py"
BASELINE = HERE / "fixtures" / "watchdog-baseline"
CANDIDATE = HERE.parents[2] / "roles" / "radar_packages" / "files" / "blah2_rspduo_restart.bash"
HELPER_NAME = "owl-docker-metadata"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference_values():
    def evaluate(node, names):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return names[node.id]
        if isinstance(node, ast.Dict):
            return {evaluate(key, names): evaluate(value, names) for key, value in zip(node.keys, node.values)}
        if isinstance(node, ast.List):
            return [evaluate(value, names) for value in node.elts]
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left, names), evaluate(node.right, names)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "dict":
                return {keyword.arg: evaluate(keyword.value, names) for keyword in node.keywords}
            if node.func.id == "str" and len(node.args) == 1:
                return str(evaluate(node.args[0], names))
        raise ValueError(f"unsupported fixture AST node: {ast.dump(node)}")

    values = {}
    names = {}
    for node in ast.parse(REFERENCE.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "now":
                    names["now"] = evaluate(node.value, names)
                elif isinstance(target, ast.Name) and target.id in {"base", "cases", "mock"}:
                    values[target.id] = evaluate(node.value, names)
    if set(values) != {"base", "cases", "mock"}:
        raise RuntimeError("could not AST-extract watchdog fixtures")
    return values["base"], values["cases"], values["mock"]


def fixture_mock():
    _, _, source = reference_values()
    source = source.replace("import json,os,sys,pathlib", "import json,os,sys,pathlib,signal")
    marker = "if name=='docker':"
    injected = """if name=='timeout':
 if len(a)>=2 and pathlib.Path(a[1]).name=='owl-docker-metadata':
  mode=os.environ['HELPER_MODE']
  if mode=='success':print(c['count'],'FIXED-START');sys.exit(0)
  if mode=='absent_2':sys.exit(2)
  if mode=='missing':sys.exit(127)
  if mode=='nonzero':sys.exit(1)
  if mode=='timeout_124':sys.exit(124)
  if mode=='signal_term':os.kill(os.getpid(),signal.SIGTERM)
  raise ValueError(mode)
 os.execvp(a[1],a[1:])
elif name=='docker':"""
    if marker not in source:
        raise RuntimeError("reference Docker mock shape changed")
    return source.replace(marker, injected, 1)


def run_variant(script_source, case, helper_mode):
    with tempfile.TemporaryDirectory(prefix="owl-watchdog-native-") as tmp:
        root = Path(tmp)
        commands = root / "commands"
        commands.mkdir()
        mock = commands / "mock"
        mock.write_text("#!" + sys.executable + "\n" + fixture_mock())
        mock.chmod(0o755)
        for name in ("docker", "curl", "date", "stat", "flock", "pgrep", "systemctl", "killmock", "timeout"):
            (commands / name).symlink_to(mock)

        (root / "case.json").write_text(json.dumps(case))
        (root / "mode").write_text(case["mode"])
        if case["previous"] is not None:
            (root / "count").write_text(case["previous"] + "\n")
        if case["calibrate"] is not None:
            (root / "calibrate").touch()
        helper = root / HELPER_NAME
        text = script_source.replace("/usr/local/libexec/owl-docker-metadata", str(helper))
        mapping = {
            "/data/retina-gui/mode.txt": root / "mode",
            "/run/blah2-rspduo-watchdog.count": root / "count",
            "/data/retina-gui/restart.lock": root / "restart",
            "/data/retina-gui/calibrate.lock": root / "calibrate",
        }
        for old, new in mapping.items():
            text = text.replace(old, str(new))
        text = text.replace("#!/bin/bash\n", "#!/bin/bash\nkill() { killmock \"$@\"; }\n", 1)
        script = root / "script"
        script.write_text(text)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        event_file = root / "events"
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                   CASE_FILE=str(root / "case.json"), EVENT_FILE=str(event_file), HELPER_MODE=helper_mode)
        result = subprocess.run(["/bin/bash", str(script)], env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        events = [json.loads(line) for line in event_file.read_text().splitlines()] if event_file.exists() else []
        actions = [event for event in events if event[0] in {"systemctl", "killmock"} or event[:2] == ["docker", "compose"]]
        helper_calls = [event for event in events if event[0] == "timeout" and len(event) >= 3 and
                        Path(event[2]).name == HELPER_NAME]
        return {
            "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "actions": actions,
            "count": (root / "count").read_text() if (root / "count").exists() else None,
            "docker_inspects": sum(event[:2] == ["docker", "inspect"] for event in events),
            "helper_calls": len(helper_calls), "events": events,
        }


def comparable(before, after):
    # Script-local temporary paths and shifted source lines can appear in Bash
    # arithmetic diagnostics for malformed fixture timestamps. They are not
    # observable watchdog behavior; compare exit, output, recovery actions and
    # persisted restart state.
    return {key: before[key] == after[key] for key in ("exit", "stdout", "actions", "count")}


def main():
    base, overrides, _ = reference_values()
    cases = {name: dict(base, **value) for name, value in overrides.items()}
    fixture_names = list(cases)
    now = base["started"] + 1000
    cases["restart_count_reset_4_to_0"] = dict(base, count=0, previous="4")
    cases["restart_count_increase_fresh_map"] = dict(
        base, count=5, previous="4", started=now - 20,
        map='{"timestamp":' + str(now * 1000) + ',"data":[]}'
    )
    baseline_source, candidate_source = BASELINE.read_text(), CANDIDATE.read_text()
    workers = int(os.environ.get("OWL_WATCHDOG_TEST_WORKERS", "4"))
    names = list(cases)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        baseline_values = list(pool.map(lambda name: run_variant(baseline_source, cases[name], "missing"), names))
    baseline = dict(zip(names, baseline_values))

    fallback = {}
    passed = True
    for mode in ("missing", "nonzero", "timeout_124", "signal_term"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            values = list(pool.map(lambda name: run_variant(candidate_source, cases[name], mode), fixture_names))
        failures = []
        for name, after in zip(fixture_names, values):
            dimensions = comparable(baseline[name], after)
            if not all(dimensions.values()):
                failures.append({"case": name, "dimensions": dimensions})
        fallback[mode] = {"cases": len(fixture_names), "failures": failures}
        passed &= not failures

    with ThreadPoolExecutor(max_workers=workers) as pool:
        native_values = list(pool.map(
            lambda name: run_variant(candidate_source, cases[name], "absent_2" if name == "container-missing" else "success"), names))
    native_failures = []
    native_rows = []
    for name, after in zip(names, native_values):
        dimensions = comparable(baseline[name], after)
        expected_helper_calls = 0 if name in {"spectrum", "sdrconnect", "calibrating"} else 1
        row_ok = all(dimensions.values()) and after["helper_calls"] == expected_helper_calls and after["docker_inspects"] == 0
        native_rows.append({"case": name, "pass": row_ok, "helper_mode": "absent_2" if name == "container-missing" else "success",
                            "expected_helper_calls": expected_helper_calls})
        if not row_ok:
            native_failures.append({"case": name, "dimensions": dimensions,
                                    "helper_calls": after["helper_calls"], "docker_inspects": after["docker_inspects"]})
    passed &= not native_failures
    healthy = native_values[names.index("healthy")]
    absent = native_values[names.index("container-missing")]

    bypass = {}
    for name in ("spectrum", "sdrconnect", "calibrating"):
        after = run_variant(candidate_source, cases[name], "success")
        dimensions = comparable(baseline[name], after)
        bypass[name] = {"helper_calls": after["helper_calls"], "full_behavior_equal_baseline": all(dimensions.values())}
        passed &= after["helper_calls"] == 0 and all(dimensions.values())

    report = {
        "pass": bool(passed), "fixture_case_count": len(fixture_names), "native_success_case_count": len(names), "workers": workers,
        "source_hashes": {"baseline_sha256": digest(BASELINE), "candidate_sha256": digest(CANDIDATE),
                          "fixture_source_sha256": digest(REFERENCE), "runner_sha256": digest(Path(__file__))},
        "fallback": fallback,
        "native_success": {"cases": len(names), "failures": native_failures, "rows": native_rows},
        "native_success_healthy": {"full_behavior_equal_baseline": all(comparable(baseline["healthy"], healthy).values()),
                                   "helper_calls": healthy["helper_calls"],
                                   "baseline_docker_inspects": baseline["healthy"]["docker_inspects"],
                                   "candidate_docker_inspects": healthy["docker_inspects"]},
        "native_absent_container_missing": {"full_behavior_equal_baseline": all(comparable(baseline["container-missing"], absent).values()),
                                             "helper_calls": absent["helper_calls"],
                                             "baseline_docker_inspects": baseline["container-missing"]["docker_inspects"],
                                             "candidate_docker_inspects": absent["docker_inspects"]},
        "bypass": bypass,
        "scope": "All Docker, curl, clock, lock, service, kill and timeout commands are mocked in isolated temporary directories. Success emits fixture RestartCount and FIXED-START; real helper timestamp validation is outside this mock test.",
    }
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
