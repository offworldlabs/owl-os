"""Local mock checks for the opt-in Pi 4 Compose selector; no Docker daemon."""
import json
import io
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROLE = Path(__file__).resolve().parents[1]
OS_SETUP = ROLE.parents[1]
PROJECT = ROLE.parents[4]


class SelectionTests(unittest.TestCase):
    def test_default_and_root_owned_opt_in(self):
        main = (OS_SETUP / "main.yml").read_text()
        tasks = (ROLE / "tasks/main.yml").read_text()
        pi4 = (PROJECT / "owl-os-pi4.yml").read_text()
        pi5 = (PROJECT / "owl-os-pi5.yml").read_text()
        self.assertIn("when: owl_pi4_gpu_stack | default(False) | bool", main)
        self.assertIn("owl_pi4_gpu_stack: False", pi4)
        self.assertNotIn("owl_pi4_gpu_stack", pi5)
        self.assertIn("path: /data/retina-node/compose", tasks)
        self.assertIn("owl_pi4_compose_override_src | default('') | length > 0", tasks)
        self.assertIn('src: "{{ owl_pi4_compose_override_src }}"', tasks)
        self.assertIn("dest: /data/retina-node/compose/pi4-gpu.override.yml", tasks)
        for path in ("/etc/owl/retina-compose.env", "/etc/systemd/system/retina-node.service.d/20-pi4-compose.conf",
                     "/etc/systemd/system/retina-gui.service.d/20-pi4-compose.conf"):
            self.assertIn("dest: " + path, tasks)
        self.assertGreaterEqual(tasks.count("owner: root"), 4)
        self.assertGreaterEqual(tasks.count("group: root"), 4)
        self.assertGreaterEqual(tasks.count("mode: '0644'"), 3)
        self.assertIn("COMPOSE_FILE={{ retina_node_manifests_path }}/docker-compose.yaml:/data/retina-node/compose/pi4-gpu.override.yml", tasks)
        self.assertIn("EnvironmentFile=/etc/owl/retina-compose.env", tasks)
        self.assertIn("path: /etc/mender/mender-docker-compose.conf", tasks)
        self.assertIn('DOCKER_COMPOSE_CMD="/usr/local/libexec/owl-mender-compose"', tasks)

    def test_preflight_and_manifest_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manifests = root / "current/manifests"
            manifests.mkdir(parents=True)
            base = manifests / "docker-compose.yaml"
            override = root / "persistent/pi4-gpu.override.yml"
            override.parent.mkdir()
            base.write_text("services:\n  blah2:\n    image: original\n")
            override.write_text("approved literal override")
            digest = "sha256:" + "a" * 64
            good = {"services": {
                "config-merger": {"image": "merge"},
                "blah2": {"image": digest, "labels": {"io.offworldlabs.pi4-support": "1"},
                          "pull_policy": "never", "container_name": "blah2",
                          "environment": {"OWL_GPU_FILTER_PERCENT": "50", "VK_ICD_FILENAMES": "/usr/share/vulkan/icd.d/broadcom_icd.json"},
                          "devices": [{"source": "/dev/dri/renderD129", "target": "/dev/dri/renderD128"}]},
                "blah2_api": {"image": digest, "labels": {"io.offworldlabs.pi4-support": "1"},
                              "pull_policy": "never", "container_name": "blah2-api"}}}
            base_only = {"services": {"config-merger": {}, "blah2": {"image": "mutable-default"}, "blah2_api": {"image": "mutable-default"}}}
            good_path = root / "good.json"; good_path.write_text(json.dumps(good))
            base_path = root / "base.json"; base_path.write_text(json.dumps(base_only))
            log = root / "calls.jsonl"
            docker = root / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                              "pathlib.Path(os.environ['MOCK_LOG']).open('a').write(json.dumps({'args':sys.argv[1:], 'cwd':os.getcwd(), 'files':os.environ['COMPOSE_FILE']})+'\\n')\n"
                              "override=pathlib.Path(os.environ['COMPOSE_FILE'].split(':')[1]).read_text()\n"
                              "if 'INVALID' in override: sys.exit(1)\n"
                              "print(pathlib.Path(os.environ['MOCK_GOOD'] if override else os.environ['MOCK_BASE']).read_text())\n")
            docker.chmod(0o755)
            check = root / "check"
            check.write_text((ROLE / "files/owl-retina-compose-check").read_text()
                             .replace("/usr/bin/docker", str(docker))
                             .replace("/usr/bin/python3 /usr/local/libexec/owl-retina-compose-verify",
                                      f"{sys.executable} {ROLE / 'files/owl-retina-compose-verify.py'}"))
            check.chmod(0o755)
            env = dict(os.environ, COMPOSE_FILE=f"{base}:{override}", MOCK_LOG=str(log),
                       MOCK_GOOD=str(good_path), MOCK_BASE=str(base_path))

            def run():
                return subprocess.run([str(check)], env=env, text=True, capture_output=True)

            self.assertEqual(run().returncode, 0)
            first = json.loads(log.read_text().splitlines()[-1])
            self.assertEqual(first, {"args": ["compose", "-p", "retina-node", "config", "--format", "json"],
                                     "cwd": str(manifests.resolve()), "files": f"{base}:{override}"})
            base.write_text("services:\n  blah2:\n    image: replacement\n")
            self.assertEqual(run().returncode, 0)
            self.assertEqual(len(log.read_text().splitlines()), 2)
            override.write_text("")
            self.assertIn("immutable image", run().stderr)
            override.write_text("approved literal override")
            good["services"]["blah2"]["environment"]["OWL_GPU_FILTER_PERCENT"] = "0"
            good_path.write_text(json.dumps(good))
            self.assertIn("GPU percentage", run().stderr)
            good["services"]["blah2"]["environment"]["OWL_GPU_FILTER_PERCENT"] = "50"
            good_path.write_text(json.dumps(good))
            good["services"]["blah2_api"]["labels"].clear()
            good_path.write_text(json.dumps(good))
            self.assertIn("opt-in label", run().stderr)
            good["services"]["blah2_api"]["labels"]["io.offworldlabs.pi4-support"] = "1"
            good["services"]["blah2"]["devices"] = []
            good_path.write_text(json.dumps(good))
            self.assertIn("render device", run().stderr)
            good["services"]["blah2"]["devices"] = [{"source": "/dev/dri/renderD129", "target": "/dev/dri/renderD128"}]
            good_path.write_text(json.dumps(good))
            override.unlink()
            self.assertIn("Required Compose file is missing", run().stderr)
            override.write_text("INVALID")
            self.assertNotEqual(run().returncode, 0)
            base.unlink()
            self.assertIn("Required Compose file is missing", run().stderr)
            env["COMPOSE_FILE"] = str(override)
            self.assertIn("requires base and override", run().stderr)
            env["COMPOSE_FILE"] = f"relative:{override}"
            self.assertIn("must be absolute", run().stderr)
            env["COMPOSE_FILE"] = f"{override}:{override}:{override}"
            self.assertIn("more than two files", run().stderr)

    def test_watchdog_default_and_opt_in_recovery(self):
        watchdog = (OS_SETUP / "roles/radar_packages/files/blah2_rspduo_restart.bash").read_text()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            base = root / "manifests/docker-compose.yaml"; base.parent.mkdir()
            base.write_text("services: {}\n")
            override = root / "pi4-gpu.override.yml"; override.write_text("services: {}\n")
            selector = root / "retina-compose.env"
            commands = root / "commands"; commands.mkdir()
            log = root / "events.jsonl"
            docker = commands / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport json, os, sys, pathlib\n"
                              "pathlib.Path(os.environ['MOCK_LOG']).open('a').write(json.dumps({'args':sys.argv[1:], 'cwd':os.getcwd(), 'files':os.environ.get('COMPOSE_FILE')})+'\\n')\n")
            docker.chmod(0o755)
            for name, body in {"curl": "", "flock": "exit 0", "pgrep": "exit 1", "systemctl": "exit 0"}.items():
                command = commands / name
                command.write_text("#!/bin/sh\n" + body + "\n")
                command.chmod(0o755)
            preflight = root / "preflight"
            preflight.write_text("#!/bin/sh\n[ -f \"${COMPOSE_FILE#*:}\" ]\n")
            preflight.chmod(0o755)
            script = root / "watchdog"
            script.write_text(watchdog.replace("#!/bin/bash\n", "#!/bin/bash\nkill() { :; }\n", 1)
                              .replace("/data/mender-docker-compose/current/manifests/docker-compose.yaml", str(base))
                              .replace("/etc/owl/retina-compose.env", str(selector))
                              .replace("/usr/local/libexec/owl-retina-compose-check", str(preflight))
                              .replace("/data/retina-gui/mode.txt", str(root / "mode"))
                              .replace("/data/retina-gui/calibrate.lock", str(root / "calibrate"))
                              .replace("/data/retina-gui/restart.lock", str(root / "restart"))
                              .replace("/run/blah2-rspduo-watchdog.count", str(root / "count"))
                              .replace("timeout 2 /usr/local/libexec/owl-docker-metadata", "echo '0 OLD'"))
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"], MOCK_LOG=str(log))

            def recovery():
                log.unlink(missing_ok=True)
                result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=5)
                events = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
                return result, events

            result, events = recovery()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([x["args"] for x in events],
                             [["compose", "-p", "retina-node", "-f", str(base), "down"],
                              ["compose", "-p", "retina-node", "-f", str(base), "up", "-d"]])
            selector.write_text(f"COMPOSE_FILE={base}:{override}\nCOMPOSE_PROFILES=\n")
            result, events = recovery()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([x["args"] for x in events],
                             [["compose", "-p", "retina-node", "down"],
                              ["compose", "-p", "retina-node", "up", "-d"]])
            self.assertTrue(all(x["files"] == f"{base}:{override}" and x["cwd"] == str(base.parent.resolve()) for x in events))
            override.unlink()
            result, events = recovery()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(events, [])

    def test_installed_mender_module_install_and_rollback(self):
        supplied = os.environ.get("OWL_MENDER_MODULE")
        vendor = Path(supplied) if supplied else Path("/usr/share/mender/modules/v3/docker-compose")
        if supplied:
            self.assertTrue(vendor.is_file(), f"OWL_MENDER_MODULE does not name a file: {vendor}")
        elif not vendor.is_file():
            self.skipTest("installed Mender Compose module absent; set OWL_MENDER_MODULE to an explicit captured 1.0.0 script")
        print(f"OWL_MENDER_MODULE_SHA256={hashlib.sha256(vendor.read_bytes()).hexdigest()}")
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            store = root / "data/mender-docker-compose"; store.mkdir(parents=True)
            current = store / "current"; (current / "manifests").mkdir(parents=True)
            (current / "manifests/docker-compose.yaml").write_text("services:\n  blah2:\n    image: old\n")
            (current / "project_name").write_text("retina-node\n")
            override = root / "data/retina-node/compose/pi4-gpu.override.yml"
            override.parent.mkdir(parents=True)
            selector = root / "retina-compose.env"
            selector.write_text(f"COMPOSE_FILE={current}/manifests/docker-compose.yaml:{override}\nCOMPOSE_PROFILES=\n")
            digest = "sha256:" + "b" * 64
            effective = {"services": {
                "config-merger": {},
                "blah2": {"image": digest, "labels": {"io.offworldlabs.pi4-support": "1"},
                          "pull_policy": "never", "container_name": "blah2",
                          "environment": {"OWL_GPU_FILTER_PERCENT": "50", "VK_ICD_FILENAMES": "/usr/share/vulkan/icd.d/broadcom_icd.json"},
                          "devices": [{"source": "/dev/dri/renderD128", "target": "/dev/dri/renderD128"}]},
                "blah2_api": {"image": digest, "labels": {"io.offworldlabs.pi4-support": "1"},
                              "pull_policy": "never", "container_name": "blah2-api"}}}
            override.write_text(json.dumps({"services": {name: effective["services"][name]
                                                          for name in ("blah2", "blah2_api")}}))
            effective_path = root / "effective.json"; effective_path.write_text(json.dumps(effective))
            events = root / "events.jsonl"
            docker = root / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                              "a=sys.argv[1:]\n"
                              "pathlib.Path(os.environ['MOCK_LOG']).open('a').write(json.dumps({'args':a,'cwd':os.getcwd(),'files':os.environ.get('COMPOSE_FILE')})+'\\n')\n"
                              "if a[:2]==['compose','--project-name'] and 'ps' in a: print('cid1')\n"
                              "elif a[:2]==['compose','-p'] and 'config' in a: print(pathlib.Path(os.environ['MOCK_EFFECTIVE']).read_text())\n"
                              "elif a and a[0]=='inspect': print('running:healthy')\n")
            docker.chmod(0o755)
            check = root / "check"
            check.write_text((ROLE / "files/owl-retina-compose-check").read_text()
                             .replace("/usr/bin/docker", str(docker))
                             .replace("/usr/bin/python3 /usr/local/libexec/owl-retina-compose-verify",
                                      f"{sys.executable} {ROLE / 'files/owl-retina-compose-verify.py'}"))
            check.chmod(0o755)
            wrapper = root / "wrapper"
            wrapper.write_text((ROLE / "files/owl-mender-compose").read_text()
                               .replace("/etc/owl/retina-compose.env", str(selector))
                               .replace("store=/data/mender-docker-compose", f"store={store}")
                               .replace("/usr/local/libexec/owl-retina-compose-check", str(check))
                               .replace("/usr/bin/python3 /usr/local/libexec/owl-retina-compose-verify",
                                        f"{sys.executable} {ROLE / 'files/owl-retina-compose-verify.py'}")
                               .replace("/usr/bin/docker", str(docker)))
            wrapper.chmod(0o755)
            module = root / "mender-docker-compose"
            module.write_text(vendor.read_text().replace('PERSISTENT_STORE="/data/mender-docker-compose"', f'PERSISTENT_STORE="{store}"'))
            module.chmod(0o755)
            config = root / "mender-docker-compose.conf"
            config.write_text(f'DOCKER_CMD="{docker}"\nDOCKER_COMPOSE_CMD="{wrapper}"\nWAIT_TIMEOUT=2\n')
            artifact = root / "artifact"; (artifact / "header").mkdir(parents=True)
            (artifact / "header/meta-data").write_text('{"project_name":"retina-node","version":"1"}')
            (artifact / "header/header-info").write_text('{"artifact_provides":{"artifact_name":"mock"}}')
            files = artifact / "files"; files.mkdir(); (artifact / "tmp").mkdir()
            with tarfile.open(files / "images.tar.gz", "w:gz") as archive:
                data = b"fake image"; info = tarfile.TarInfo("images/fake.tar"); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            with tarfile.open(files / "manifests.tar", "w") as archive:
                data = b"services:\n  blah2:\n    image: new\n"; info = tarfile.TarInfo("manifests/docker-compose.yaml"); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            env = dict(os.environ, MENDER_DOCKER_COMPOSE_CONFIG_FILE=str(config), MOCK_LOG=str(events), MOCK_EFFECTIVE=str(effective_path))
            for state in ("ArtifactInstall", "ArtifactRollback"):
                result = subprocess.run([str(module), state, str(artifact)], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, f"{state}: {result.stdout}\n{result.stderr}")
            records = [json.loads(x) for x in events.read_text().splitlines()]
            actions = [x for x in records if x["args"][:2] == ["compose", "--project-name"]
                       and x["args"][3] in ("up", "down", "ps")]
            self.assertEqual([x["args"][3] for x in actions], ["down", "up", "ps", "down", "up", "ps"])
            self.assertEqual([Path(x["files"].split(":")[0]).parents[1].name for x in actions],
                             ["previous", "new", "new", "new", "current", "current"])
            self.assertTrue(all(x["args"][2] == "retina-node" and x["files"].split(":")[1] == str(override) for x in actions))
            self.assertTrue((store / "current/manifests/docker-compose.yaml").is_file())
            override.write_text("{}")
            before = [x for x in events.read_text().splitlines() if json.loads(x)["args"][:1] == ["compose"]]
            failure = subprocess.run([str(module), "ArtifactInstall", str(artifact)], env=env, cwd=root,
                                     capture_output=True, text=True, timeout=15)
            self.assertNotEqual(failure.returncode, 0)
            self.assertTrue((store / "current/manifests/docker-compose.yaml").is_file())
            self.assertFalse((store / "previous").exists())
            self.assertEqual([x for x in events.read_text().splitlines() if json.loads(x)["args"][:1] == ["compose"]], before)
            override.unlink()
            before = len(events.read_text().splitlines())
            failure = subprocess.run([str(wrapper), "version"], env=env, capture_output=True, text=True)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("Missing Pi 4 Compose override", failure.stderr)
            self.assertEqual(len(events.read_text().splitlines()), before)
            failure = subprocess.run([str(module), "ArtifactInstall", str(artifact)], env=env, cwd=root,
                                     capture_output=True, text=True, timeout=15)
            self.assertNotEqual(failure.returncode, 0)
            self.assertTrue((store / "current/manifests/docker-compose.yaml").is_file())
            self.assertFalse((store / "previous").exists())


if __name__ == "__main__":
    unittest.main()
