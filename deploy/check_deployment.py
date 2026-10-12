"""Bounded deployment checks with fixed diagnostics and no secret-bearing output."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


class CheckFailed(Exception):
    pass


def command(args, cwd=None):
    try:
        return subprocess.run(
            args, cwd=cwd, check=True, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckFailed("deployment command failed or timed out") from exc


def candidate(root):
    import tomllib

    revision = command(["git", "rev-parse", "HEAD"], cwd=root)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise CheckFailed("candidate revision is invalid")
    if command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root):
        raise CheckFailed("candidate checkout is dirty; commit changes before deployment")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise CheckFailed("candidate version is invalid")
    return version, revision


def image_identity(reference, version, revision):
    template = '{"id":"{{.Id}}","version":"{{index .Config.Labels "org.opencontainers.image.version"}}","revision":"{{index .Config.Labels "org.opencontainers.image.revision"}}"}'
    image = json.loads(command(["docker", "image", "inspect", reference, "--format", template]))
    if image.get("version") != version or image.get("revision") != revision:
        raise CheckFailed("candidate image version/revision labels do not match checkout")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("id", "")):
        raise CheckFailed("candidate image ID is invalid")
    return image["id"]


def service_health(expected_image):
    template = '{"status":"{{.State.Status}}","health":"{{if .State.Health}}{{.State.Health.Status}}{{end}}","image":"{{.Image}}"}'
    for service in ("db", "seaweed", "api", "caddy"):
        ids = command(["docker", "compose", "ps", "-q", service]).splitlines()
        if len(ids) != 1:
            raise CheckFailed(f"{service}: expected exactly one running container")
        state = json.loads(command(["docker", "inspect", ids[0], "--format", template]))
        if state.get("status") != "running":
            raise CheckFailed(f"{service}: container is not running")
        if service != "caddy" and state.get("health") != "healthy":
            raise CheckFailed(f"{service}: container is not healthy")
        if service == "api" and state.get("image") != expected_image:
            raise CheckFailed("api: running image differs from the checked candidate")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise CheckFailed("smoke endpoint unexpectedly redirected")


def validate_origin(base_url, *, public):
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != ("https" if public else "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CheckFailed("smoke base URL must be an origin with the required scheme")
    if not public and (parsed.hostname != "localhost" or parsed.port != 8000):
        raise CheckFailed("container smoke origin must be http://localhost:8000")


def http_checks(base_url, version, revision, *, public):
    validate_origin(base_url, public=public)
    opener = build_opener(NoRedirects())  # default HTTPS certificate verification

    def read_json(path):
        try:
            with opener.open(base_url.rstrip("/") + path, timeout=2) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise CheckFailed("smoke response exceeds the byte limit")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise CheckFailed("smoke response has an invalid shape")
            return value
        except CheckFailed:
            raise
        except Exception as exc:
            raise CheckFailed(f"{path}: request failed or returned invalid JSON") from exc

    ready = read_json("/ready")
    checks = ready.get("checks", {})
    if (
        ready.get("status") != "ready"
        or not isinstance(checks, dict)
        or not all(
            checks.get(name) is True for name in ("database", "storage", "worker", "probe_fresh")
        )
    ):
        raise CheckFailed("/ready: dependencies or worker are not ready")
    health = read_json("/health")
    if (
        health.get("status") != "ok"
        or health.get("version") != version
        or health.get("revision") != revision
    ):
        raise CheckFailed("/health: deployed version/revision differs from candidate")
    if public:
        try:
            with opener.open(base_url.rstrip("/") + "/ops", timeout=2):
                raise CheckFailed("private operations endpoint is publicly accessible")
        except HTTPError as exc:
            if exc.code != 404:
                raise CheckFailed("private operations endpoint did not return 404") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("candidate")
    origin = sub.add_parser("origin")
    origin.add_argument("base_url")
    image = sub.add_parser("image")
    image.add_argument("reference")
    services = sub.add_parser("services")
    services.add_argument("image_id")
    http = sub.add_parser("http")
    http.add_argument("base_url")
    http.add_argument("--public", action="store_true")
    for p in (image, http):
        p.add_argument("version")
        p.add_argument("revision")
    args = parser.parse_args()
    try:
        if args.mode == "candidate":
            print(*candidate(Path(__file__).resolve().parents[1]))
        elif args.mode == "origin":
            validate_origin(args.base_url, public=True)
        elif args.mode == "image":
            print(image_identity(args.reference, args.version, args.revision))
        elif args.mode == "services":
            service_health(args.image_id)
        elif args.mode == "http":
            http_checks(args.base_url, args.version, args.revision, public=args.public)
    except CheckFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - malformed command output must not expose data
        print("deployment check failed; inspect service state privately", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
