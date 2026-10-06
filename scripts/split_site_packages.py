"""Build-time helper: split an installed Python prefix into balanced buckets.

Used by the Dockerfile's builder stage so the final image is composed of
several medium layers instead of one ~1 GB layer.  A single giant layer is
fragile to push to a registry through a proxied / rate-limited link -- the
connection is reset mid-upload and the blob restarts from byte 0 forever.
Several ~100-200 MB layers can be retried independently, so a flaky link only
has to re-send the small blob that failed.

Layout produced (all under ``<dest-root>``):

* ``sp0`` .. ``sp<N-1>`` -- balanced chunks of ``site-packages`` contents
* ``rest``               -- everything else in the prefix (e.g. ``bin/``)

The runtime stage copies each bucket to ``/usr/local/lib/pythonX.Y/site-packages``
and ``rest`` to ``/usr/local`` so the merged filesystem is identical to a plain
``COPY --from=builder /install /usr/local``.

Usage:
    python split_site_packages.py <prefix-dir> <dest-root> <num-buckets>
"""

from __future__ import annotations

import glob
import os
import shutil
import sys


def _size(path: str) -> int:
    """Return the on-disk size of *path* without following symlinks."""
    if os.path.islink(path):
        return 1
    if os.path.isdir(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _find_site_packages(prefix: str) -> str:
    for candidate in sorted(glob.glob(os.path.join(prefix, "lib", "python*"))):
        sp = os.path.join(candidate, "site-packages")
        if os.path.isdir(sp):
            return sp
    raise SystemExit(f"no site-packages directory found under {prefix!r}")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit(__doc__)

    prefix, dest_root, n_buckets = argv[1], argv[2], int(argv[3])
    site_packages = _find_site_packages(prefix)

    buckets = [os.path.join(dest_root, f"sp{i}") for i in range(n_buckets)]
    for bucket in buckets:
        os.makedirs(bucket, exist_ok=True)

    # Greedy largest-first bin packing -> roughly equal bucket sizes.
    entries = sorted(
        ((_size(os.path.join(site_packages, name)), name)
         for name in os.listdir(site_packages)),
        reverse=True,
    )
    load = [0] * n_buckets
    for size, name in entries:
        idx = load.index(min(load))
        load[idx] += size
        shutil.move(os.path.join(site_packages, name), os.path.join(buckets[idx], name))

    # Move everything left in the prefix (bin/, the now-empty site-packages, ...).
    rest = os.path.join(dest_root, "rest")
    os.makedirs(rest, exist_ok=True)
    for name in os.listdir(prefix):
        shutil.move(os.path.join(prefix, name), os.path.join(rest, name))

    for idx, size in enumerate(load):
        print(f"bucket sp{idx}: {size / 1e6:.1f} MB")
    print(f"site-packages split: {n_buckets} buckets from {site_packages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
