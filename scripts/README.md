# Scripts

Standalone analysis tools for this project. Unlike `src/`, none of these
run as part of a poudriere hook — they're meant to be run by hand (or
piped together) ahead of a build, to figure out what a `uv`-managed
Python project actually needs from FreeBSD ports/wheels.

## Requirements

Both scripts need `requests`, `truststore`, `packaging`, and `tabulate`
installed (`pip install requests truststore packaging tabulate`) —
separate from anything the hooks in `src/` need.

## `find-native-deps.py`

Resolves a `uv.lock` (via `uv export`) and reports which dependencies
need a native/compiled build — i.e. ship no platform-independent ("any")
wheel on the queried Python package index — versus which are pure Python.
For each native dependency, also checks whether a FreeBSD wheel for the
target version/arch/Python has already been published to that index.

```
python3 find-native-deps.py [--ssh-target [user@]host] [--ssh-option OPT=VALUE ...]
                             [--index-url URL] [--freebsd-version VERSION]
                             [--freebsd-arch ARCH] [--python-version VERSION]
                             [--scope {listed,all,all+parents}]
                             [--sleep SECONDS] [-H] [--pretty] [uv-project-dir]
```

Run `python3 find-native-deps.py --help` (or read the module docstring)
for the full flag reference, including field layout and `--scope`
semantics.

## `map-to-ports.py`

Best-effort maps PyPI project names (typically piped in from
`find-native-deps.py`) to FreeBSD port origins, using the upstream ports
`INDEX` file rather than scanning a local ports tree.

```
python3 find-native-deps.py ... | python3 map-to-ports.py [-V] [--freebsd-version VERSION] [--pretty]
```

Run `python3 map-to-ports.py --help` (or read the module docstring) for
the full flag reference.
