#!/usr/bin/env python3
"""
Identify resolved dependencies (from a uv.lock, via `uv export`) that ship no
platform-independent wheel on the queried Python package index, i.e. that
need a native/compiled build.

Usage: find-native-deps.py [--ssh-target [user@]host]
                            [--ssh-option OPT=VALUE ...] [--index-url URL]
                            [--extra-index-url URL ...]
                            [--freebsd-version VERSION] [--freebsd-arch ARCH]
                            [--python-version VERSION]
                            [--scope {listed,all,all+parents}]
                            [--sleep SECONDS] [-H] [--pretty] [uv-project-dir]

With --ssh-target, `uv export` runs on the remote host (project_dir is
resolved there); wheel lookups against --index-url/--extra-index-url
still happen locally.

--index-url defaults to $PIP_INDEX_URL if set (same env var pip itself
honors), else pypi.org's own index -- --index-url on the command line
always wins over either.

--extra-index-url URL (repeatable) adds a fallback index, consulted in
order after --index-url; defaults to $PIP_EXTRA_INDEX_URL if set
(space-separated, same as pip), else none. Uses uv's own default
"first-index" strategy, NOT pip's "merge every index" behavior: for a
given package, the first index (--index-url, then each --extra-index-url
in order) that has ANY record of it at all is authoritative -- later
ones are never consulted for that package, even if this one's answer for
the requested version is a miss. This exists specifically to avoid
"dependency confusion" (a later, untrusted index shadowing or
supplementing results from an earlier, trusted one) -- see
check_index()'s docstring for the exact mechanics.

Authentication against --index-url/--extra-index-url, if required, is
handled the same way pip/requests do it -- no flag of our own: $NETRC if
set, else ~/.netrc (~/_netrc on Windows), matched by hostname. This is
requests' own default behavior (session.trust_env), not something this
script implements; embedding credentials directly in an index URL also
still works, same as pip.

--ssh-option OPT=VALUE (repeatable) passes an ssh(1) -o option through to
every ssh invocation, e.g. --ssh-option Port=2222 --ssh-option
IdentityFile=~/.ssh/id_foo. Ignored without --ssh-target.

--python-version defaults to the contents of PROJECT_DIR/.python-version
(or the remote one, via --ssh-target) -- the exact interpreter uv resolved
uv.lock against, which requires-python in pyproject.toml (only a floor,
e.g. ">=3.10") cannot tell us. It feeds both marker evaluation
(python_version/python_full_version) and wheel interpreter/ABI tag
matching (e.g. "3.10" -> cp310), so getting it right matters even before
any FreeBSD-specific logic runs.

Native package lines on stdout are "name scope type parent version
freebsd-version", in that order:
* scope is "listed" if uv.lock records the dependency as direct -- of
  the project itself, or (under --all-packages) of any workspace member
  -- or "transitive" if it was only pulled in indirectly. This is
  CycloneDX's own dependency graph (uv export --format cyclonedx1.5
  already emits one), not a second uv invocation.
* type is "native" (needs a compiled build, no "any" wheel on the
  index), "pure" (platform-neutral, has an "any" wheel), or "unknown"
  (not found on the index at this exact version at all). Every row in
  the main list is
  "native" by construction (that is what this script reports on) -- type
  only varies on a synthetic "parent" row (--scope all+parents), since a
  parent package can turn out to be pure Python or unknown even though
  what it pulled in needs a native build.
* parent is, for a "transitive" row, the comma-separated names of the
  listed (direct) dependency(-ies) whose transitive closure reaches it
  -- i.e. why it is here at all; "-" for every other row ("listed" and
  the synthetic "parent" row itself have no package parent of their
  own). This field is always present, independent of --scope, so
  map-to-ports.py's field positions never shift -- --scope all+parents
  only controls whether the *synthetic rows* naming those parents get
  added, not whether this field exists.
* freebsd-version is, for the same index response used to decide
  "native or pure", the highest version -- across ALL releases, not just
  the one requested -- already published there with a wheel tagged for
  --freebsd-version/--freebsd-arch/--python-version exactly as
  pkgbuild.sh and jail.sh tag it, regardless of what the project has
  pinned; "-" if the index has none yet.
map-to-ports.py understands this field order (name, then version at
position 5, then freebsd-version at position 6) and displays a column
for the latter.

--scope {listed,all,all+parents} (default: all) controls which resolved
packages are even looked up against --index-url:
* "listed" looks up only direct dependencies -- of the project itself,
  or (under --all-packages) of any workspace member -- narrowing the
  report to what the project asked for directly and saving index
  requests for everything it did not.
* "all" (default) looks up everything, listed and transitive alike.
* "all+parents" is "all" plus, right after each transitive package's
  first appearance, one synthetic row per listed (direct) dependency
  whose transitive closure reaches it. That row is for the parent
  package itself (its own type/requested/index fields, looked up the
  same as any other package, even if the parent turned out to be pure
  Python or not found), with scope replaced by the literal string
  "parent" -- a normal row for that package, just relocated next to what
  it pulled in, and de-duplicated across every transitive package that
  shares it or that is itself already shown as native. --pretty only
  shows the Parent column under this scope (elsewhere it is real data
  but not actionable without the synthetic rows, so it would just be
  noise); the plain/machine-readable format always includes the field,
  per the field note above, since map-to-ports.py's positions must not
  shift.

-H/--index-history appends a field (labeled "Index History" in
--pretty, last regardless of --scope): every matching version in the
index, ascending, comma-separated (or "-" if none) -- for reference
only, ignored by map-to-ports.py.

--pretty replaces the machine-readable stdout format (meant to be piped
into map-to-ports.py) with a formatted table for human consumption.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests
import truststore
from packaging.markers import Marker
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import Version
from tabulate import tabulate

truststore.inject_into_ssl()

DEFAULT_INDEX_URL = "https://pypi.org/simple"
SIMPLE_API_ACCEPT = "application/vnd.pypi.simple.v1+json"
DEFAULT_FREEBSD_VERSION = "14.4-RELEASE"
DEFAULT_FREEBSD_ARCH = "amd64"


def freebsd_marker_env(
    freebsd_version: str, arch: str, python_version: str
) -> dict[str, str]:
    """uv/pip environment markers as they resolve on the FreeBSD build
    target, not on whatever host this script happens to run on. Only the
    fields a dependency marker could plausibly test on platform are set;
    anything else falls back to packaging's own default marker
    environment -- which is why python_version/python_full_version MUST
    be set here too: left unset, they silently default to the host
    interpreter's own version (e.g. 3.12 on this dev machine) rather than
    the project's actual target, changing which markers evaluate true.
    sys_platform on FreeBSD was "freebsd<major>" (Python's own
    sys.platform reported exactly that, regardless of minor
    version/branch) through Python 3.13; from 3.14 on it is
    unconditionally "freebsd", no version suffix at all (gh-129393,
    landed for 3.14) -- so which one to emit depends on python_version.
    """
    major = freebsd_version.split(".", 1)[0]
    py_major, py_minor = python_version.split(".")[:2]
    # py_minor may carry a free-threaded build's trailing "t" (e.g.
    # "3.13t"); irrelevant to the version comparison below, so strip it.
    py_version = (int(py_major), int(py_minor.removesuffix("t")))
    sys_platform = "freebsd" if py_version >= (3, 14) else f"freebsd{major}"
    return {
        "os_name": "posix",
        "platform_system": "FreeBSD",
        "sys_platform": sys_platform,
        "platform_machine": arch,
        "python_version": f"{py_major}.{py_minor}",
        "python_full_version": python_version,
    }


def build_dep_edges(bom: dict) -> dict[str, set[str]]:
    """ref -> set of refs it directly depends on, straight from CycloneDX's
    own dependencies[] graph (ref/dependsOn) -- shared by every function
    below that needs to walk it, so the graph is only parsed once.
    """
    return {
        entry["ref"]: set(entry.get("dependsOn", []))
        for entry in bom.get("dependencies", [])
        if entry.get("ref")
    }


def purl_less_refs(bom: dict) -> set[str]:
    """bom-refs of components uv resolved from local sources (workspace
    members, the project root, path/git dependencies), identified the same
    way resolved_packages() treats them as "local": no purl.
    """
    return {
        c["bom-ref"]
        for c in bom.get("components", [])
        if c.get("bom-ref") and not c.get("purl")
    }


def direct_dependency_refs(bom: dict, dep_edges: dict[str, set[str]]) -> set[str]:
    """bom-refs that are a direct dependency of the project root, or (under
    --all-packages) of a workspace member -- i.e. what "listed" means, as
    opposed to a dependency only reachable transitively through one of
    those. uv's cyclonedx1.5 output already models this: metadata.component
    is the top-level root, dependencies[] gives ref -> dependsOn edges, and
    a workspace member shows up as one of the root's dependsOn entries
    while itself being a purl-less component (same as the root). Only that
    one level of workspace-member expansion is needed -- a workspace never
    nests members inside each other's dependsOn beyond the root.
    """
    purl_less = purl_less_refs(bom)
    root_ref = bom.get("metadata", {}).get("component", {}).get("bom-ref")
    roots = {root_ref} if root_ref else set()
    roots |= dep_edges.get(root_ref, set()) & purl_less

    listed = set()
    for root in roots:
        listed |= dep_edges.get(root, set())
    # Workspace members themselves show up as a "dependsOn" entry of the
    # project root, same as any real package -- but they are not index
    # packages (already filtered out as "local" by their missing purl),
    # so they must not count as "listed" here.
    return listed - purl_less


def transitive_parent_names(
    bom: dict, dep_edges: dict[str, set[str]], listed_refs: set[str]
) -> dict[str, set[str]]:
    """ref -> names of the listed (direct) dependencies whose transitive
    closure reaches it -- i.e. "who pulled this in", for the Parent field
    (and, under --scope all+parents, the synthetic rows naming them).
    Walks the dependency graph once per listed ref rather than per
    package, since the interesting direction is "everything below this
    direct dependency", not "everything above this transitive one".
    """
    name_of = {
        c["bom-ref"]: c.get("name")
        for c in bom.get("components", [])
        if c.get("bom-ref") and c.get("name")
    }
    parents: dict[str, set[str]] = {}
    for listed_ref in listed_refs:
        listed_name = name_of.get(listed_ref)
        if not listed_name:
            continue
        seen = set()
        stack = list(dep_edges.get(listed_ref, set()))
        while stack:
            ref = stack.pop()
            if ref in seen:
                continue
            seen.add(ref)
            parents.setdefault(ref, set()).add(listed_name)
            stack.extend(dep_edges.get(ref, set()))
    return parents


def ssh_command(ssh_target: str, ssh_options: list[str], remote_cmd: str) -> list[str]:
    """["ssh", "-o", opt, ..., destination, remote_cmd] -- shared by every
    ssh invocation, so --ssh-option is honored consistently everywhere.
    """
    cmd = ["ssh"]
    for opt in ssh_options:
        cmd += ["-o", opt]
    cmd += [ssh_target, remote_cmd]
    return cmd


def resolved_packages(
    project_dir: str,
    ssh_target: str | None,
    ssh_options: list[str],
    marker_env: dict[str, str],
    compute_parents: bool = True,
) -> tuple[list[tuple[str, str, str, tuple[str, ...]]], list[tuple[str, str, str]], list[str]]:
    uv_export = [
        "uv", "export", "--frozen", "--format", "cyclonedx1.5",
        "--all-packages", "--all-extras", "--no-hashes",
    ]
    if ssh_target:
        remote_cmd = f"cd {shlex.quote(project_dir)} && {shlex.join(uv_export)}"
        proc = subprocess.run(
            ssh_command(ssh_target, ssh_options, remote_cmd),
            capture_output=True,
            check=True,
            text=True,
        )
    else:
        proc = subprocess.run(
            uv_export,
            cwd=project_dir,
            capture_output=True,
            check=True,
            text=True,
        )
    bom = json.loads(proc.stdout)
    dep_edges = build_dep_edges(bom)
    listed_refs = direct_dependency_refs(bom, dep_edges)
    # The full transitive walk is only useful for --scope all+parents (the
    # Parent field/synthetic rows); skip it otherwise rather than compute
    # data that would just be discarded.
    parents_by_ref = (
        transitive_parent_names(bom, dep_edges, listed_refs) if compute_parents else {}
    )

    # uv.lock can resolve the same name+version more than once with
    # different markers (e.g. separate extras/marker branches). Only
    # treat it as excluded from FreeBSD if EVERY one of its component
    # entries evaluates false; a single FreeBSD-relevant branch is
    # enough to keep it. Likewise, "listed" if ANY of its bom-ref
    # entries is a direct dependency of a root -- the same package can
    # be pulled in directly by the project AND transitively by another
    # dependency at once. Required-by names are unioned across all of a
    # key's bom-refs for the same reason.
    markers_by_key = {}
    scope_by_key = {}
    parents_by_key: dict[tuple[str, str], set[str]] = {}
    local = []
    for component in bom.get("components", []):
        name = component.get("name")
        version = component.get("version")
        if not name or not version:
            continue
        # Workspace members, the project root, and path/git dependencies
        # carry no purl -- uv resolved them from local sources, not any
        # package index, so their "version" is whatever pyproject.toml
        # says and has no relationship to what --index-url would serve
        # under that same name. Querying the index for them would be
        # comparing against an unrelated (or, worse, coincidentally
        # same-named) package.
        if not component.get("purl"):
            local.append(name)
            continue
        key = (name, version)
        marker_str = next(
            (
                p["value"]
                for p in component.get("properties", [])
                if p.get("name") == "uv:package:marker"
            ),
            None,
        )
        markers_by_key.setdefault(key, []).append(marker_str)
        ref = component.get("bom-ref")
        if ref in listed_refs:
            scope_by_key[key] = "listed"
        else:
            scope_by_key.setdefault(key, "transitive")
        parents_by_key.setdefault(key, set()).update(parents_by_ref.get(ref, set()))

    packages = []
    skipped = []
    for (name, version), markers in sorted(markers_by_key.items()):
        relevant = any(
            m is None or Marker(m).evaluate(marker_env) for m in markers
        )
        key = (name, version)
        scope = scope_by_key[key]
        required_by = tuple(sorted(parents_by_key.get(key, ())))
        if relevant:
            packages.append((name, version, scope, required_by))
        else:
            skipped.append((name, version, " | ".join(markers)))
    return packages, skipped, sorted(set(local))


REQUIRES_PYTHON_RE = re.compile(r"""requires-python\s*=\s*["'][^"'\d]*(\d+\.\d+)""")


def read_python_version(project_dir: str, ssh_target: str | None, ssh_options: list[str]) -> str:
    """The exact CPython version uv resolved uv.lock against -- read from
    PROJECT_DIR/.python-version. Falls back to the floor of pyproject.toml's
    requires-python (e.g. ">=3.10" -> "3.10") if that file does not exist,
    with a warning: a floor is not necessarily the interpreter uv actually
    used, just the oldest one it MUST support.
    """
    if ssh_target:
        remote_cmd = (
            f"cat {shlex.quote(project_dir.rstrip('/') + '/.python-version')} "
            f"2>/dev/null || grep -m1 requires-python "
            f"{shlex.quote(project_dir.rstrip('/') + '/pyproject.toml')}"
        )
        proc = subprocess.run(
            ssh_command(ssh_target, ssh_options, remote_cmd),
            capture_output=True, check=True, text=True,
        )
        output = proc.stdout.strip()
        m = REQUIRES_PYTHON_RE.search(output)
        if m:
            print(
                f"# Warning: {project_dir}/.python-version not found on {ssh_target}; "
                f"using requires-python floor {m.group(1)} as an approximation",
                file=sys.stderr,
            )
            return m.group(1)
        return output

    python_version_file = Path(project_dir) / ".python-version"
    if python_version_file.exists():
        return python_version_file.read_text().strip()

    pyproject = Path(project_dir) / "pyproject.toml"
    m = REQUIRES_PYTHON_RE.search(pyproject.read_text())
    if not m:
        raise SystemExit(
            f"error: {python_version_file} not found and no requires-python "
            f"floor in {pyproject}; pass --python-version explicitly"
        )
    print(
        f"# Warning: {python_version_file} not found; using requires-python "
        f"floor {m.group(1)} as an approximation",
        file=sys.stderr,
    )
    return m.group(1)


def target_cpython_tag(python_version: str) -> str:
    """"3.10" (or "3.10.5") -> "cp310", matching a wheel's interpreter/abi
    tag for a non-abi3 CPython extension -- what pkgbuild.sh's build
    produces, one exact tag per Python version, same as its FreeBSD
    platform tag has no range matching either.
    """
    major, minor = python_version.split(".")[:2]
    return f"cp{major}{minor}"


def _cpython_tag_version(cpython_tag: str) -> tuple[int, int]:
    """"cp311" -> (3, 11). Assumes a single-digit major version, true for
    every CPython 3.x tag that exists today.
    """
    digits = cpython_tag.removeprefix("cp")
    return int(digits[0]), int(digits[1:])


def wheel_matches_target(tag, target_tag: str, target_cpython: str) -> bool:
    """Whether a wheel's Tag is installable on our FreeBSD/arch/Python
    target: exact platform match, plus one of --
    * "py3": interpreter-agnostic -- the wheel's compiled code (if any)
      never calls into the CPython C API, so it needs no cp3XX tag even
      though it is still platform-specific (e.g. a native CLI tool
      bundled as a wheel, invoked as a subprocess rather than imported);
    * an exact interpreter match (e.g. cp312 for a non-abi3 extension,
      built once per Python version, no range matching);
    * abi3 with an interpreter tag AT OR BELOW our target (e.g. a wheel
      declaring cp311-abi3 is installable on 3.11, 3.12, 3.13, ... --
      that floor, not the exact build interpreter, is what abi3 tags
      record).
    """
    if tag.platform != target_tag:
        return False
    if tag.interpreter == "py3" or tag.interpreter == target_cpython:
        return True
    if tag.abi == "abi3" and tag.interpreter.startswith("cp"):
        try:
            return _cpython_tag_version(tag.interpreter) <= _cpython_tag_version(
                target_cpython
            )
        except ValueError:
            return False
    return False


def target_platform_tag(freebsd_version: str, arch: str) -> str:
    """Build the exact wheel platform tag pkgbuild.sh/jail.sh would stamp
    for this FreeBSD version/arch, e.g. "14.4-RELEASE-p6" ->
    "freebsd_14_4_release_p6". A wheel carries a patch level in its tag
    only when jail.sh's windowing kicked in (jail version matched
    *-RELEASE-p*), and jail.sh always windows on the LITERAL patch
    numbers, one exact tag per patch, dot-joined -- there is no range or
    prefix matching involved on our side; each candidate wheel tag is
    checked for exact equality against this target tag, which already
    picks the right window member if one exists. Passing a version with
    no patch (e.g. "14.4-STABLE" or a bare "14.4-RELEASE") therefore
    correctly matches only patch-less-tagged wheels, not any patch.
    """
    version = re.sub(r"[.-]", "_", freebsd_version.lower())
    return f"freebsd_{version}_{arch.lower()}"


class _PEP503LinkParser(HTMLParser):
    """Extracts filenames from a PEP 503 ("legacy") Simple Index HTML
    page: one <a href="...">filename</a> per file, filename as the link
    text (the href itself may be relative, absolute, or carry a #sha256=
    fragment -- none of that matters here, only the visible filename
    does, same as what the PEP 691 JSON API's "filename" field gives us).
    """

    def __init__(self) -> None:
        super().__init__()
        self.filenames: list[str] = []
        self._in_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._in_link = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_link = False

    def handle_data(self, data: str) -> None:
        if self._in_link and data.strip():
            self.filenames.append(data.strip())


def parse_simple_index_response(resp: requests.Response, url: str) -> dict:
    """Normalize either Simple Repository API response format to the PEP
    691 JSON shape ({"files": [{"filename": ...}, ...]}) that the rest of
    this script consumes -- some indexes (e.g. GitLab's PyPI package
    registry) always serve the older PEP 503 HTML format regardless of
    the Accept header sent, never the newer JSON one.
    """
    content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            return resp.json()
        except ValueError as exc:
            raise SystemExit(
                f"error: {url} declared a JSON Content-Type ({content_type}) "
                f"but sent an unparseable body; first 200 bytes: "
                f"{resp.text[:200]!r}"
            ) from exc
    parser = _PEP503LinkParser()
    try:
        parser.feed(resp.text)
    except Exception as exc:  # noqa: BLE001 -- surface any parser failure the same way
        raise SystemExit(
            f"error: {url} returned Content-Type {content_type!r}, tried "
            f"parsing it as a PEP 503 HTML Simple Index, and failed; first "
            f"200 bytes: {resp.text[:200]!r}"
        ) from exc
    return {"files": [{"filename": f} for f in parser.filenames]}


def query_index(
    session: requests.Session,
    name: str,
    version: str,
    index_url: str,
    target_tag: str,
    target_cpython: str,
) -> tuple[bool | None, str | None, list[Version]] | None:
    """Query a single index's Simple API for one package. Returns None if
    that index has no record of the package at all (404) -- the caller
    uses this to decide whether to fall through to the next index.
    Otherwise, (has_any_wheel, highest_freebsd_version,
    all_freebsd_versions), same meaning as check_index()'s return value.
    """
    url = f"{index_url.rstrip('/')}/{canonicalize_name(name)}/"
    resp = session.get(url, headers={"Accept": SIMPLE_API_ACCEPT}, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = parse_simple_index_response(resp, url)
    target = Version(version)
    matched_this_version = False
    has_any = False
    freebsd_versions = set()
    for entry in data.get("files", []):
        filename = entry.get("filename", "")
        try:
            _, file_version, _, tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        if file_version == target:
            matched_this_version = True
            if any(tag.abi == "none" and tag.platform == "any" for tag in tags):
                has_any = True
        if any(
            wheel_matches_target(tag, target_tag, target_cpython) for tag in tags
        ):
            freebsd_versions.add(file_version)
    all_freebsd_versions = sorted(freebsd_versions)
    highest_freebsd = all_freebsd_versions[-1] if all_freebsd_versions else None
    return (
        True if has_any else (False if matched_this_version else None),
        str(highest_freebsd) if highest_freebsd is not None else None,
        all_freebsd_versions,
    )


def check_index(
    session: requests.Session,
    name: str,
    version: str,
    index_urls: list[str],
    target_tag: str,
    target_cpython: str,
) -> tuple[bool | None, str | None, list[Version], int]:
    """(has_any_wheel, highest_freebsd_version, all_freebsd_versions,
    index_position).

    index_urls is --index-url followed by every --extra-index-url, in
    that order. Queried with uv's own default "first-index" strategy, not
    pip's "merge every index" behavior: the first index in the list that
    has ANY record of this package name is authoritative for it, full
    stop -- later indexes are never consulted, even if this one's answer
    for the requested version is a plain miss. Only an index with no
    record of the package AT ALL (404) falls through to the next one.
    This is what uv itself defaults to, specifically to avoid "dependency
    confusion": a later, unrelated index must not be able to shadow or
    supplement an earlier, presumably-trusted one for a package the
    earlier index already claims to know about.

    has_any_wheel is True/False for the requested release, or None if
    none of index_urls has any record of it at all. highest_freebsd_version
    is the highest version, across ALL releases in whichever index
    answered, that already has a wheel installable on our target -- exact
    FreeBSD/arch platform tag, plus either an exact CPython interpreter
    match, "py3" (interpreter-agnostic despite still being platform-
    specific -- its compiled code, if any, never calls into the CPython C
    API, e.g. a native CLI tool bundled as a wheel), or an abi3 wheel
    whose interpreter floor is at or below our target (e.g. cp311-abi3 is
    installable on 3.11, 3.12, 3.13, ...) -- see wheel_matches_target().
    This is what poudriere/pkgbuild.sh has already produced and pushed
    there, independent of what this project pinned, or None if no such
    wheel exists yet. A wheel for an incompatible CPython version (e.g.
    an exact cp312 tag when targeting 3.10) is not installable there and
    must not count as a match, same as a wheel for a different FreeBSD
    version/arch must not.
    all_freebsd_versions holds every one of those matching versions,
    ascending, for -H/--index-history reference; highest_freebsd is
    always its last element (or None if it is empty).
    index_position is the 0-based index into index_urls that answered --
    0 for --index-url itself, 1 for the first --extra-index-url, etc. --
    or None if no index in the list had any record of the package at all
    (has_any_wheel is also None in exactly that case).
    """
    for position, index_url in enumerate(index_urls):
        result = query_index(
            session, name, version, index_url, target_tag, target_cpython
        )
        if result is not None:
            return (*result, position)
    return None, None, [], None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ssh-target", metavar="[user@]host",
        help="run uv export on this host via ssh",
    )
    parser.add_argument(
        "--ssh-option", metavar="OPT=VALUE", action="append", default=[],
        dest="ssh_options",
        help="ssh(1) -o option, repeatable, e.g. --ssh-option Port=2222; "
             "ignored without --ssh-target",
    )
    parser.add_argument(
        "--index-url", default=os.environ.get("PIP_INDEX_URL", DEFAULT_INDEX_URL),
        help="base URL of a PEP 691 Simple Repository API index (default: "
             f"${{PIP_INDEX_URL}} if set, else {DEFAULT_INDEX_URL})",
    )
    parser.add_argument(
        "--extra-index-url", metavar="URL", action="append", default=None,
        dest="extra_index_urls",
        help="additional index URL, repeatable, consulted in order after "
             "--index-url -- but ONLY for a package --index-url has no "
             "record of at all, same as uv's default first-index "
             "strategy (never merged/compared across indexes for a "
             "package the first one already knows); default: "
             "$PIP_EXTRA_INDEX_URL if set (space-separated), else none",
    )
    parser.add_argument(
        "--sleep", type=float, default=0,
        help="seconds to sleep between index requests (default: 0, disabled)",
    )
    parser.add_argument(
        "--freebsd-version", default=DEFAULT_FREEBSD_VERSION,
        help="FreeBSD version/branch a published wheel must be tagged for, "
             f"e.g. 14.4-RELEASE, 14.4-STABLE (default: {DEFAULT_FREEBSD_VERSION})",
    )
    parser.add_argument(
        "--freebsd-arch", default=DEFAULT_FREEBSD_ARCH,
        help=f"target architecture (default: {DEFAULT_FREEBSD_ARCH})",
    )
    parser.add_argument(
        "--python-version",
        help="target CPython version, e.g. 3.10 (default: read from "
             "PROJECT_DIR/.python-version)",
    )
    parser.add_argument(
        "--scope", choices=("listed", "all", "all+parents"), default="all",
        help="which resolved dependencies to look up against --index-url: "
             "\"listed\" (direct dependencies only), \"all\" (default, "
             "including transitive), or \"all+parents\" (same as \"all\", "
             "plus a synthetic row -- scope \"parent\" -- right after each "
             "transitive package's first appearance, for every listed "
             "dependency whose transitive closure pulled it in)",
    )
    parser.add_argument(
        "-H", "--index-history", action="store_true",
        help="append a 7th field listing every matching version in the "
             "index (ascending, comma-separated), for reference",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="print the native/compiled package list on stdout as a "
             "formatted table instead of the machine-readable format "
             "map-to-ports.py expects",
    )
    parser.add_argument("project_dir", nargs="?", default=".")
    args = parser.parse_args()

    show_parents = args.scope == "all+parents"

    python_version = args.python_version or read_python_version(
        args.project_dir, args.ssh_target, args.ssh_options
    )
    marker_env = freebsd_marker_env(args.freebsd_version, args.freebsd_arch, python_version)
    packages, skipped, local = resolved_packages(
        args.project_dir, args.ssh_target, args.ssh_options, marker_env,
        compute_parents=show_parents,
    )
    if args.scope == "listed":
        packages = [p for p in packages if p[2] == "listed"]

    target_tag = target_platform_tag(args.freebsd_version, args.freebsd_arch)
    target_cpython = target_cpython_tag(python_version)

    extra_index_urls = args.extra_index_urls
    if extra_index_urls is None:
        extra_index_urls = os.environ.get("PIP_EXTRA_INDEX_URL", "").split()
    index_urls = [args.index_url, *extra_index_urls]

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # A "Source" field/column (which index answered: "default" for
    # --index-url, "1"/"2"/... for the Nth --extra-index-url) only means
    # anything once there is more than one index to distinguish between --
    # with --index-url alone every row would just say "default", pure
    # noise. Only added when extra indexes are actually configured.
    show_index_source = len(index_urls) > 1

    def index_source_label(position: int | None) -> str:
        if position is None:
            return "?"
        return "default" if position == 0 else str(position)

    native = []
    pure = []
    unknown = []
    # Every resolved package's own lookup result, by name, regardless of
    # native/pure/unknown -- so a synthetic "parent" row (--scope
    # all+parents) can show that dependency's real data (including its own
    # Type) instead of placeholders, even if the parent itself is pure
    # Python or was not found on the index at all.
    result_by_name: dict[str, tuple[str, str, str | None, list[Version], int | None]] = {}
    for name, version, scope, required_by in packages:
        has_any, freebsd_version, all_freebsd_versions, index_position = check_index(
            session, name, version, index_urls, target_tag, target_cpython
        )
        pkg_type = "pure" if has_any else ("unknown" if has_any is None else "native")
        result_by_name[name] = (
            pkg_type, version, freebsd_version, all_freebsd_versions, index_position
        )
        if has_any is None:
            unknown.append((name, version))
        elif has_any:
            pure.append((name, version))
        else:
            native.append((name, version, scope, required_by))
        if args.sleep > 0:
            time.sleep(args.sleep)

    def row_of(name: str, scope: str, required_by: tuple[str, ...] = ()) -> tuple:
        pkg_type, version, freebsd_version, all_freebsd_versions, index_position = (
            result_by_name.get(name, ("?", "?", None, [], None))
        )
        # Parent is always emitted in this field position, regardless of
        # --scope: map-to-ports.py's stdin parsing relies on a fixed field
        # count, and a column that only sometimes exists would silently
        # misalign it. Its VALUE, though, is only computed at all under
        # --scope all+parents (resolved_packages()'s compute_parents) --
        # otherwise required_by is always empty and this is just "-",
        # same as "listed"/"parent" rows and their own package parent
        # (only a transitive package has one to begin with).
        parent = ",".join(required_by) if scope == "transitive" and required_by else "-"
        row = [name, scope, pkg_type, parent, version, freebsd_version or "-"]
        if show_index_source:
            row.append(index_source_label(index_position))
        if args.index_history:
            row.append(",".join(str(v) for v in all_freebsd_versions) or "-")
        return row

    print(f"# Native/compiled (no any wheel on the index): {len(native)}", file=sys.stderr)
    headers = ["Package", "Scope", "Type", "Parent", "Requested", "Index"]
    if show_index_source:
        headers.append("Source")
    if args.index_history:
        headers.append("Index History")
    rows = []
    # A parent already shown (as a "parent" row, or because it is itself
    # a native package elsewhere in this same list) is not repeated, even
    # if more than one transitive package below it shares it.
    parents_shown = {name for name, _, scope, _ in native if scope == "listed"}
    parent_rows = 0
    for name, version, scope, required_by in native:
        rows.append(row_of(name, scope, required_by))
        if show_parents and scope == "transitive":
            for parent_name in required_by:
                if parent_name in parents_shown:
                    continue
                parents_shown.add(parent_name)
                rows.append(row_of(parent_name, "parent"))
                parent_rows += 1
    if parent_rows:
        print(
            f"# Plus {parent_rows} parent row(s) below their transitive "
            "dependency, type/index shown for reference (not necessarily "
            "native themselves)",
            file=sys.stderr,
        )

    if show_index_source:
        for position, index_url in enumerate(index_urls):
            print(f"# index {index_source_label(position)}: {index_url}", file=sys.stderr)

    if args.pretty:
        # --pretty shows Source right after the wheel name for readability;
        # the plain/machine-readable format above keeps it after Index
        # instead, since that format's field positions are load-bearing
        # for map-to-ports.py and must not shift.
        if show_index_source:
            src = headers.index("Source")
            headers = [headers[0], headers[src], *headers[1:src], *headers[src + 1 :]]
            rows = [[row[0], row[src], *row[1:src], *row[src + 1 :]] for row in rows]
        # Parent is only ever computed under --scope all+parents (see
        # resolved_packages()'s compute_parents); --pretty hides the
        # column entirely otherwise, since it would just be a column of
        # "-". The plain/machine-readable format above never hides it:
        # its field position is fixed for map-to-ports.py, "-" or not.
        if not show_parents:
            par = headers.index("Parent")
            headers = [h for h in headers if h != "Parent"]
            rows = [row[:par] + row[par + 1 :] for row in rows]
        print(tabulate(rows, headers=headers))
    else:
        for row in rows:
            print(" ".join(row))

    print(f"\n# Pure Python (has an any wheel): {len(pure)}", file=sys.stderr)
    print(f"# Not found on the index at this exact version: {len(unknown)}", file=sys.stderr)
    for name, version in unknown:
        print(f"# unknown: {name} {version}", file=sys.stderr)

    print(f"\n# Skipped, marker excludes FreeBSD: {len(skipped)}", file=sys.stderr)
    for name, version, marker_str in skipped:
        print(f"# skipped: {name} {version} ({marker_str})", file=sys.stderr)

    print(f"\n# Local (workspace/path/root, no index to query): {len(local)}", file=sys.stderr)
    for name in local:
        print(f"# local: {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
