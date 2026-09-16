#!/usr/bin/env python3
"""Fixture tests for the fixed Unix-socket Docker metadata request."""
import json
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT.parent / "files" / "docker_metadata.c"
OVERRIDES = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_API_VERSION",
             "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
CHECKS = 0


class Server:
    def __init__(self, body=b"{}", status=200, delay=0, raw=None):
        self.body, self.status, self.delay, self.raw = body, status, delay, raw
        self.requests = 0
        self.request = b""

    def start(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(path))
        self.sock.listen(1)

        def serve():
            try:
                client, _ = self.sock.accept()
                self.requests += 1
                self.request = client.recv(4096)
                time.sleep(self.delay)
                response = self.raw
                if response is None:
                    response = (b"HTTP/1.1 %d Fixture\r\nContent-Length: %d\r\n\r\n" %
                                (self.status, len(self.body))) + self.body
                client.sendall(response)
                client.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        threading.Thread(target=serve, daemon=True).start()

    def close(self):
        self.sock.close()


def compile_helper(executable, socket_path):
    command = ["cc", "-std=c17", "-Wall", "-Wextra", "-Werror"]
    command += shlex.split(os.environ.get("CPPFLAGS", ""))
    command += shlex.split(os.environ.get("CFLAGS", ""))
    command += [f'-DOWL_DOCKER_SOCKET="{socket_path}"', str(SRC)]
    command += shlex.split(os.environ.get("LDFLAGS", ""))
    command += shlex.split(os.environ.get("LIBS", "")) or ["-lcurl", "-ljson-c"]
    command += ["-o", str(executable)]
    subprocess.run(command, check=True)


def clean_env(home):
    environment = os.environ.copy()
    for key in OVERRIDES:
        environment.pop(key, None)
    environment["HOME"] = str(home)
    return environment


def run(executable, server, socket_path, environment):
    server.start(socket_path)
    result = subprocess.run([executable], env=environment, capture_output=True,
                            text=True, timeout=3)
    server.close()
    socket_path.unlink(missing_ok=True)
    return result


def assert_case(name, executable, socket_path, home, body, *, status=200, delay=0,
                raw=None, code=1, output=""):
    global CHECKS
    result = run(executable, Server(body, status, delay, raw), socket_path, clean_env(home))
    CHECKS += 1
    assert result.returncode == code and result.stdout == output, (
        name, result.returncode, result.stdout, result.stderr)


def main():
    global CHECKS
    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        socket_path = temporary / "docker.sock"
        executable = temporary / "helper"
        home = temporary / "home"
        home.mkdir()
        compile_helper(executable, socket_path)
        good = b'{"RestartCount":7,"State":{"StartedAt":"2026-01-01T00:00:00Z"}}'
        assert_case("valid", executable, socket_path, home, good, code=0,
                    output="7 2026-01-01T00:00:00Z\n")
        assert_case("maximum count", executable, socket_path, home,
                    good.replace(b'"RestartCount":7', b'"RestartCount":9223372036854775807'),
                    code=0, output="9223372036854775807 2026-01-01T00:00:00Z\n")
        server = Server(good)
        result = run(executable, server, socket_path, clean_env(home))
        CHECKS += 1
        assert result.returncode == 0 and b"GET /v1.44/containers/blah2/json HTTP/1.1" in server.request
        invalid = [
            ("missing", b'{"RestartCount":7,"State":{}}'),
            ("wrong type", b'{"RestartCount":"7","State":{"StartedAt":"2026-01-01T00:00:00Z"}}'),
            ("fraction", b'{"RestartCount":1.5,"State":{"StartedAt":"2026-01-01T00:00:00Z"}}'),
            ("negative", b'{"RestartCount":-1,"State":{"StartedAt":"2026-01-01T00:00:00Z"}}'),
            ("overflow", b'{"RestartCount":9223372036854775808,"State":{"StartedAt":"2026-01-01T00:00:00Z"}}'),
            ("null fields", b'{"RestartCount":null,"State":{"StartedAt":null}}'),
            ("nondigit timestamp", b'{"RestartCount":1,"State":{"StartedAt":"2026-01-01Taa:00:00Z"}}'),
            ("timestamp whitespace", b'{"RestartCount":1,"State":{"StartedAt":" 2026-01-01T00:00:00Z"}}'),
            ("timestamp NUL", b'{"RestartCount":1,"State":{"StartedAt":"2026-01-01T00:00:00\\u0000Z"}}'),
            ("timestamp control", b'{"RestartCount":1,"State":{"StartedAt":"2026-01-01T00:00:00Z\\u0001"}}'),
            ("malformed", b"{"),
            ("trailing JSON", good + b"x"),
            ("huge", b"x" * 65537),
            ("depth", b'{"RestartCount":1,"State":' + b'{"x":' * 70 + b'0' + b'}' * 70 + b'}'),
        ]
        for name, body in invalid:
            assert_case(name, executable, socket_path, home, body)
        assert_case("non-200", executable, socket_path, home, good, status=500)
        assert_case("timeout", executable, socket_path, home, good, delay=1)
        chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                   b"10\r\n{\"RestartCount\":\r\n"
                   b"2f\r\n7,\"State\":{\"StartedAt\":\"2026-01-01T00:00:00Z\"}}\r\n0\r\n\r\n")
        assert_case("chunked", executable, socket_path, home, good, raw=chunked,
                    code=0, output="7 2026-01-01T00:00:00Z\n")
        absent = b'{"message":"No such container: blah2"}'
        assert_case("absent 404", executable, socket_path, home, absent, status=404, code=2)
        assert_case("NUL 404 message", executable, socket_path, home,
                    b'{"message":"No such container: blah2\\u0000x"}', status=404)
        assert_case("malformed 404", executable, socket_path, home, b'{"message":', status=404)
        assert_case("unsupported API 404", executable, socket_path, home,
                    b'{"message":"client version 1.44 is too old"}', status=404)

        for key in OVERRIDES:
            server = Server(good)
            environment = clean_env(home)
            environment[key] = "x"
            result = run(executable, server, socket_path, environment)
            CHECKS += 1
            assert result.returncode == 1 and result.stdout == "" and server.requests == 0, key

        for label, home_value in [("unset HOME", None), ("empty HOME", "")]:
            environment = clean_env(home)
            if home_value is None:
                environment.pop("HOME", None)
            else:
                environment["HOME"] = home_value
            server = Server(good)
            result = run(executable, server, socket_path, environment)
            CHECKS += 1
            assert result.returncode == 1 and result.stdout == "" and server.requests == 0, label

        docker = home / ".docker"
        docker.mkdir()
        config = docker / "config.json"
        for name, contents, okay in [
            ("empty object", b"{}", True),
            ("default", b'{"currentContext":"default"}', True),
            ("NUL default", b'{"currentContext":"default\\u0000remote"}', False),
            ("other", b'{"currentContext":"remote"}', False),
            ("null", b'{"currentContext":null}', False),
            ("malformed", b"{", False),
            ("oversize", b" " * 65537, False),
        ]:
            config.write_bytes(contents)
            server = Server(good)
            result = run(executable, server, socket_path, clean_env(home))
            CHECKS += 1
            assert (result.returncode == 0) == okay and (server.requests == 1) == okay, (
                name, result.returncode, server.requests)
        config.unlink()
        os.mkfifo(config)
        server = Server(good)
        result = run(executable, server, socket_path, clean_env(home))
        CHECKS += 1
        assert result.returncode == 1 and result.stdout == "" and server.requests == 0, "nonregular"
        config.unlink()
        config.write_text("{}")
        config.chmod(0)
        server = Server(good)
        result = run(executable, server, socket_path, clean_env(home))
        if os.geteuid() != 0:
            CHECKS += 1
            assert result.returncode == 1 and result.stdout == "" and server.requests == 0, "unreadable"
        config.chmod(0o600)
    print(json.dumps({"status": "pass", "checks": CHECKS,
                      "unreadable_checked": os.geteuid() != 0}, sort_keys=True))


if __name__ == "__main__":
    main()
