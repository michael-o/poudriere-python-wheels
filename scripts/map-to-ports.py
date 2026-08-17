#!/usr/bin/env python3
"""
Best-effort map PyPI project names (e.g. from find-native-deps.py) to
FreeBSD port origins, using the upstream ports INDEX rather than scanning
a local ports tree. Name-only: this makes no attempt to reconcile a
port's current version against any lockfile pin -- poudriere only ever
builds whatever version is currently in the tree. As of this writing
INDEX-14/15/16 are identical for every py-* port, so --freebsd-version
currently has no effect on results (only its major-version component is
even used), but the flag exists -- consistent with find-native-deps.py's
flag of the same name -- so a future divergence does not silently go
unnoticed.

Usage: map-to-ports.py [-V] [--freebsd-version VERSION] [--pretty] [name ...]
       map-to-ports.py [-V] [--freebsd-version VERSION] [--pretty] < names.txt
       (one name per line, optionally "name scope type parent version
       freebsd-version" as emitted by find-native-deps.py)

-V/--show-versions additionally prints the requested version (from stdin,
if given) next to the port's current version, e.g. for a quick sanity
check of how stale a port is -- it does not affect matching. A requested
version newer than the port's is flagged "(port update needed)": the
project has moved past what poudriere would build. A requested version
older than the port's is not flagged -- the project simply pinned an
older release, which is fine.

-V also prints an INDEX column: the highest version already published
as a FreeBSD wheel to the queried package index, taken from a 6th field
on stdin (as emitted by find-native-deps.py: "name scope type parent
version freebsd-version-or-hyphen"). "-" means the index has no FreeBSD
wheel for this package yet; "?" means this information was not supplied
at all (e.g. names given as CLI args, or piped from something other than
find-native-deps.py). The 2nd (scope), 3rd (type), and 4th (parent)
fields are ignored here -- parent is always present in find-native-
deps.py's plain output regardless of its --scope, as a "-" placeholder
when not applicable, so field position here does not shift.

The INDEX file is cached in ~/.cache/freebsd-ports/ (%LOCALAPPDATA%
\freebsd-ports on Windows) and revalidated against the server's ETag on
every run, so it is only re-downloaded when it has actually changed
upstream.

--pretty replaces the machine-readable stdout format with a formatted
table for human consumption.
"""
import argparse
import lzma
import os
import re
import sys
from pathlib import Path

import requests
import truststore
from packaging.version import InvalidVersion, Version
from tabulate import tabulate

truststore.inject_into_ssl()

# %LOCALAPPDATA% (AppData\Local) is the native per-user cache location on
# Windows; ~/.cache is the XDG convention everywhere else, including the
# FreeBSD/poudriere hosts this project actually targets. Fall back to
# ~/.cache even on Windows if LOCALAPPDATA is somehow unset, rather than
# crashing outright.
local_appdata = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
if local_appdata:
    CACHE_DIR = Path(local_appdata) / "freebsd-ports"
else:
    CACHE_DIR = Path.home() / ".cache" / "freebsd-ports"
DEFAULT_FREEBSD_VERSION = "14.4-RELEASE"
INDEX_URL_TEMPLATE = "https://download.freebsd.org/ports/index/INDEX-{major}.xz"

# INDEX pkgname fields look like "py312-cyclonedx-python-lib-11.11.0":
# an optional python-flavor prefix, the port name (which may itself
# contain hyphens), then a trailing version. pkg version strings never
# contain "-", so the rightmost "-<digit...>" split is unambiguous.
PKGNAME_RE = re.compile(r"^(.+)-([0-9][^-]*)$")
FLAVOR_PREFIX_RE = re.compile(r"^py\d+-")


def normalize(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def bare_version(pkgversion: str) -> str:
    """Strip PKGVERSION's _PORTREVISION and ,PORTEPOCH suffixes."""
    return pkgversion.split(",")[0].split("_")[0]


def fetch_index(cache_dir: Path, freebsd_version: str) -> Path:
    major = freebsd_version.split(".", 1)[0]
    index_url = INDEX_URL_TEMPLATE.format(major=major)

    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path = cache_dir / f"INDEX-{major}"
    etag_path = cache_dir / f"{index_path.name}.etag"

    headers = {}
    if index_path.exists() and etag_path.exists():
        headers["If-None-Match"] = etag_path.read_text().strip()

    try:
        resp = requests.get(index_url, headers=headers, timeout=30)
        if resp.status_code == 304:
            return index_path
        resp.raise_for_status()
        index_path.write_bytes(lzma.decompress(resp.content))
        etag = resp.headers.get("ETag")
        if etag:
            etag_path.write_text(etag)
    except requests.exceptions.RequestException:
        if not index_path.exists():
            raise
        print(
            f"# Warning: could not refresh {index_url}, using cached copy",
            file=sys.stderr,
        )

    return index_path


def parse_index(index_path: Path) -> dict[str, dict[str, str]]:
    """normalized PORTNAME -> {origin: PKGVERSION}, python ports only."""
    index: dict[str, dict[str, str]] = {}
    with index_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            fields = line.split("|")
            if len(fields) < 2:
                continue
            pkgname, origin_path = fields[0], fields[1]
            if "/py-" not in origin_path:
                continue
            m = PKGNAME_RE.match(pkgname)
            if not m:
                continue
            raw_name, pkgversion = m.groups()
            raw_name = FLAVOR_PREFIX_RE.sub("", raw_name, count=1)
            origin = origin_path.removeprefix("/usr/ports/")
            index.setdefault(normalize(raw_name), {})[origin] = pkgversion
    return index


def find_port(name: str, port_index: dict[str, dict[str, str]]) -> list[str]:
    return sorted(port_index.get(normalize(name), {}))


def main() -> None:
    parser = argparse.ArgumentParser(
        usage="%(prog)s [-V] [--freebsd-version VERSION] [--pretty] [name ...]"
    )
    parser.add_argument("names", metavar="name", nargs="*")
    parser.add_argument(
        "-V", "--show-versions", action="store_true",
        help="also print the requested version (from stdin) and the port's "
             "current version",
    )
    parser.add_argument(
        "--freebsd-version", default=DEFAULT_FREEBSD_VERSION,
        help="FreeBSD version whose ports INDEX major branch to use, e.g. "
             f"14.4-RELEASE (default: {DEFAULT_FREEBSD_VERSION}); only the "
             "major version is actually used",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="print the mapped port list on stdout as a formatted table "
             "instead of the machine-readable format",
    )
    args = parser.parse_args()

    requested = {name: (None, None) for name in args.names}
    if not requested:
        for line in sys.stdin:
            parts = line.split()
            if not parts:
                continue
            # parts[1] (scope), parts[2] (type), and parts[3] (parent) are
            # find-native-deps.py's own bookkeeping, not consumed here.
            req_version = parts[4] if len(parts) > 4 else None
            # None: 6th field not supplied at all (unknown to us). "-":
            # supplied and the index has no FreeBSD wheel yet. Any other
            # value: the version already published there.
            index_version = parts[5] if len(parts) > 5 else None
            requested[parts[0]] = (req_version, index_version)

    port_index = parse_index(fetch_index(CACHE_DIR, args.freebsd_version))

    found = []
    ambiguous = []
    missing = []
    for name in sorted(requested):
        origins = find_port(name, port_index)
        if len(origins) == 1:
            found.append((name, origins[0]))
        elif len(origins) > 1:
            ambiguous.append((name, origins))
        else:
            missing.append(name)

    print(f"# Mapped: {len(found)}", file=sys.stderr)

    if args.show_versions:
        entries = []
        for name, origin in found:
            req_version, index_version = requested[name]
            pkgversion = port_index[normalize(name)][origin]
            port_version = bare_version(pkgversion)
            cmp = "?"
            outdated = False
            if req_version is not None:
                try:
                    req_v, port_v = Version(req_version), Version(port_version)
                    cmp = "=" if req_v == port_v else ">" if req_v > port_v else "<"
                    outdated = req_v > port_v
                except InvalidVersion:
                    pass
            note = " (port update needed)" if outdated else ""
            entries.append((
                name, origin,
                f"{req_version or '?'} {cmp} {pkgversion}{note}",
                index_version if index_version is not None else "?",
            ))
        if args.pretty:
            # Wheel first for readability; the plain format below keeps
            # the port origin first instead, since that format doubles as
            # a poudriere pkglist file.
            print(tabulate(
                entries, headers=["Wheel", "Port", "Requested <=> Port", "Index"]
            ))
        else:
            print("# PORT NAME\tWHEEL NAME\tREQUESTED <=> PORT\tINDEX")
            for name, origin, req_vs_port, index_version in entries:
                print(f"{origin}\t# {name} {req_vs_port}\t{index_version}")
    else:
        entries = [(name, origin) for name, origin in found]
        if args.pretty:
            print(tabulate(entries, headers=["Wheel", "Port"]))
        else:
            print("# PORT NAME\tWHEEL NAME")
            for name, origin in entries:
                print(f"{origin}\t# {name}")

    print(f"\n# Ambiguous, needs manual pick: {len(ambiguous)}", file=sys.stderr)
    for name, origins in ambiguous:
        print(f"# {name}: {', '.join(origins)}", file=sys.stderr)

    print(f"\n# No matching port found: {len(missing)}", file=sys.stderr)
    for name in missing:
        print(f"# {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
