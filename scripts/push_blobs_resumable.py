#!/usr/bin/env python3
"""Resilient registry blob uploader -- workaround for non-resumable docker push.

Why this exists
---------------
``docker push`` uploads every layer blob as ONE monolithic ``PUT`` request that
cannot be resumed.  Pushing this image through a throttled/proxied link
(Docker Desktop -> Squid -> Clash -> overseas node, measured ~165 KB/s) makes a
58 MB blob take ~6 minutes, which exceeds the proxy's response-header timeout:
the request is reset and the blob restarts *from byte 0*, so the push never
finishes.  Splitting site-packages into several buckets (see
``scripts/split_site_packages.py``) shrinks each blob, but a 50 MB blob is still
slow enough to hit the same wall.

This script talks to the registry v2 HTTP API directly, from the HOST, and
uploads blobs as small resumable ``PATCH`` chunks (``Content-Range``): a dropped
connection only costs one chunk, and each chunk is retried / re-synced from the
registry's reported offset.  Run it on missing blobs, then finish with a normal
``docker push`` -- every blob already exists, so only the tiny manifest ``PUT``
remains and the push succeeds.

Usage
-----
    docker save <repo>:<tag> | tar -x -C /tmp/img          # OCI layout
    python3 scripts/push_blobs_resumable.py /tmp/img/blobs/sha256 \
        --repo bonnie333333333/crypto-churn-prediction [--dry-run]
    docker push <repo>:<tag>

Environment overrides: ``DH_PROXY`` (default http://127.0.0.1:7897),
``DH_CHUNK`` (bytes, default 2 MiB), ``DH_TIMEOUT`` (seconds),
``DH_CHUNK_RETRIES``.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = "registry-1.docker.io"
AUTH = "auth.docker.io"
DOCKER_HUB_SERVER = "https://index.docker.io/v1/"
PROXY = os.environ.get("DH_PROXY", "http://127.0.0.1:7897")
CHUNK = int(os.environ.get("DH_CHUNK", 2 * 1024 * 1024))
TIMEOUT = int(os.environ.get("DH_TIMEOUT", 90))
CHUNK_RETRIES = int(os.environ.get("DH_CHUNK_RETRIES", 6))

_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
)


def _lower(headers: dict | None) -> dict:
    """urllib header lookups are case-insensitive, a plain dict() is not."""
    return {k.lower(): v for k, v in (headers or {}).items()}


def request(method: str, url: str, headers: dict | None = None,
            data: bytes | None = None,
            expect: tuple = (200, 201, 202, 204, 404, 416)):
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, _lower(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.code in expect:
            return exc.code, _lower(exc.headers), body
        raise RuntimeError("%s %s -> HTTP %s: %s"
                           % (method, url, exc.code, body[:400])) from None


def credentials() -> tuple[str, str]:
    """Docker Hub (user, secret) via the configured credential helper."""
    path = os.path.expanduser("~/.docker/config.json")
    cfg = json.load(open(path)) if os.path.exists(path) else {}

    store = cfg.get("credsStore")
    if store:
        helper = "docker-credential-" + store
        out = subprocess.run([helper, "get"], input=DOCKER_HUB_SERVER,
                             capture_output=True, text=True, check=True).stdout
        creds = json.loads(out)
        return creds["Username"], creds["Secret"]

    auth = (cfg.get("auths", {}).get(DOCKER_HUB_SERVER) or {}).get("auth")
    if auth:
        user, _, secret = base64.b64decode(auth).decode().partition(":")
        return user, secret
    raise SystemExit("no Docker Hub credentials found -- run `docker login` first")


class Client:
    """Minimal registry v2 client with resumable chunked blob uploads."""

    def __init__(self, repo: str):
        self.repo = repo
        self.user, self.password = credentials()
        self.token: str | None = None
        self.token_at = 0.0

    # ---- auth ---------------------------------------------------------------
    def auth(self, force: bool = False) -> str:
        if not force and self.token and time.time() - self.token_at < 240:
            return self.token
        basic = base64.b64encode(
            ("%s:%s" % (self.user, self.password)).encode()).decode()
        scope = urllib.parse.quote("repository:%s:pull,push" % self.repo, safe=":")
        url = "https://%s/token?service=registry.docker.io&scope=%s" % (AUTH, scope)
        _, _, body = request("GET", url, {"Authorization": "Basic " + basic},
                             expect=(200,))
        self.token = json.loads(body)["token"]
        self.token_at = time.time()
        return self.token

    def h(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": "Bearer " + self.auth(),
                   "User-Agent": "push-blobs-resumable/1.0"}
        headers.update(extra or {})
        return headers

    # ---- registry primitives ------------------------------------------------
    def blob_exists(self, digest: str) -> bool:
        for force in (False, True):
            if force:
                self.auth(force=True)
            status, _, _ = request(
                "HEAD", "https://%s/v2/%s/blobs/%s" % (REGISTRY, self.repo, digest),
                self.h(), expect=(200, 401, 404))
            if status != 401:
                return status == 200
        return False

    @staticmethod
    def _location(loc: str | None) -> str:
        if not loc:
            raise RuntimeError("registry did not return a Location header")
        if loc.startswith("http"):
            return loc
        if loc.startswith("/"):
            return "https://%s%s" % (REGISTRY, loc)
        return "https://%s/%s" % (REGISTRY, loc)

    def start_upload(self) -> str:
        _, headers, _ = request(
            "POST", "https://%s/v2/%s/blobs/uploads/" % (REGISTRY, self.repo),
            self.h({"Content-Length": "0"}), data=b"", expect=(202,))
        return self._location(headers.get("location"))

    def offset(self, loc: str) -> int:
        """How many bytes does the registry already hold for this session?"""
        _, headers, _ = request("GET", loc, self.h(), expect=(204, 416))
        match = re.search(r"0-(\d+)", headers.get("range") or "")
        return int(match.group(1)) + 1 if match else 0

    def put_chunk(self, loc: str, start: int, end: int, blob: bytes) -> str | None:
        headers = self.h({
            "Content-Type": "application/octet-stream",
            "Content-Range": "%d-%d" % (start, end),
            "Content-Length": str(end - start + 1),
        })
        status, resp_headers, _ = request(
            "PATCH", loc, headers, data=blob[start:end + 1], expect=(202, 416))
        if status == 202:
            return self._location(resp_headers.get("location", loc))
        return None

    def finish(self, loc: str, digest: str) -> bool:
        sep = "&" if "?" in loc else "?"
        url = "%s%sdigest=%s" % (loc, sep, urllib.parse.quote(digest, safe=":"))
        status, _, _ = request("PUT", url, self.h({"Content-Length": "0"}),
                               b"", expect=(201, 202))
        return status in (201, 202)

    # ---- chunked upload -----------------------------------------------------
    def upload(self, path: str, digest: str) -> float:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            blob = handle.read()

        loc = self.start_upload()
        start = 0
        t0 = time.time()
        while start < size:
            end = min(start + CHUNK - 1, size - 1)
            for attempt in range(1, CHUNK_RETRIES + 1):
                try:
                    new_loc = self.put_chunk(loc, start, end, blob)
                    if new_loc:
                        loc = new_loc
                        start = end + 1
                        break
                    start = self.offset(loc)          # 416: re-sync with registry
                    print("      resync -> offset %d/%d" % (start, size))
                except Exception as exc:              # noqa: BLE001
                    if attempt == CHUNK_RETRIES:
                        raise
                    print("      chunk retry %d (%s)" % (attempt, exc))
                    time.sleep(2 * attempt)
                    try:
                        start = self.offset(loc)
                    except Exception:                 # noqa: BLE001
                        pass
            rate = start / max(time.time() - t0, 0.001) / 1024
            sys.stdout.write("\r    %5.1f%%  %6.2f/%6.2f MB  %6.0f KB/s"
                             % (100.0 * start / size, start / 1e6, size / 1e6, rate))
            sys.stdout.flush()
        print()
        if not self.finish(loc, digest):
            raise RuntimeError("PUT (commit) failed")
        return time.time() - t0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    blobdir = argv[1]
    repo = os.environ.get("DH_REPO",
                          "bonnie333333333/crypto-churn-prediction")
    if "--repo" in argv:
        repo = argv[argv.index("--repo") + 1]
    dry_run = "--dry-run" in argv

    blobs = []
    for name in sorted(os.listdir(blobdir)):
        path = os.path.join(blobdir, name)
        if os.path.isfile(path):
            blobs.append((os.path.getsize(path), "sha256:" + name, path))
    blobs.sort()

    client = Client(repo)
    print("repo=%s  proxy=%s  chunk=%dKB  blobs=%d"
          % (repo, PROXY, CHUNK // 1024, len(blobs)))

    missing = []
    for size, digest, path in blobs:
        try:
            exists = client.blob_exists(digest)
            tag = "OK  " if exists else "MISS"
        except Exception as exc:                      # noqa: BLE001
            exists, tag = False, "????"
            print("  %s %-19s (HEAD failed: %s)" % (tag, digest[:19], exc))
        print("  %s %-19s %7.2f MB" % (tag, digest[:19], size / 1e6))
        if not exists:
            missing.append((size, digest, path))

    print("\nmissing: %d blob(s), %.2f MB"
          % (len(missing), sum(s for s, _, _ in missing) / 1e6))
    if dry_run:
        return 0
    if not missing:
        print("nothing to upload -- now run: docker push %s:latest" % repo)
        return 0

    for size, digest, path in missing:
        print("\n-> uploading %s (%.2f MB)" % (digest[:19], size / 1e6))
        for attempt in range(1, 4):
            try:
                secs = client.upload(path, digest)
                print("   done in %.1fs (%.0f KB/s)"
                      % (secs, size / 1024 / max(secs, 0.001)))
                break
            except Exception as exc:                  # noqa: BLE001
                print("   attempt %d failed: %s" % (attempt, exc))
                if attempt == 3:
                    raise
                time.sleep(3)

    print("\nall blobs present -- now run: docker push %s:latest" % repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
