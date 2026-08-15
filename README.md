# Building and Processing Python wheels with poudriere for FreeBSD

> [!IMPORTANT]
> This is work in progress and might change in nature!

## Requirements

* `poudriere-devel` installed
* `python3` installed
* `py-wheel` (`wheel` command) installed; required to normalize and deduplicate wheels, see below

## Installation/Configuration

* Apply [this patch](https://github.com/freebsd/freebsd-ports/compare/main...michael-o:freebsd-ports:python-wheels.patch) to your ports tree
* Add `PYDISTUTILS_BUILD_WHEEL=yes` to your `make.conf`, PEP 517-based wheels are built by default
* Copy the hooks from `src/` to `${POUDRIERED}/hooks/plugins/python-wheels/`
* Add `NO_PACKAGE_BUILDING=yes ; export PROCESS_PYTHON_WHEELS=yes` to your `poudriere.conf`
* If you build with `-b <branch>` (or otherwise set `PACKAGE_FETCH_BRANCH` in your `poudriere.conf`),
  add at least `PACKAGE_FETCH_BLACKLIST="py<PYTHON_SUFFIX>-*"` or explicit non-prefixed package names
  (not all packages are prefixed) to your `poudriere.conf`, since fetching a prebuilt package instead
  of building locally skips the wheel-collection hook entirely
* By default only ports listed explicitly on the build command line/list file are collected, not
  automatic (transitive) dependencies; add `export PYTHON_WHEELS_SCOPE=all` to your `poudriere.conf`
  to collect wheels for every built port instead
* If you want a static simple index being generated, add `export GENERATE_STATIC_INDEX=yes` to your `poudriere.conf`
* If you are running ZFS, optionally create the Python wheels dataset:

  ```
  zfs create -o compression=off ${ZPOOL}${ZROOTFS}/data/python-wheels
  ```

## Building/Processing Python Wheels

Run your poudriere build as usual, as soon as a Python package is built it will:
* in case of distutils build the wheel,
* in case of PEP 517 the wheel is already built, since `PEP517_BUILD_CMD` runs automatically as part of the port's normal build phase (no opt-in required, unlike `PYDISTUTILS_BUILD_WHEEL` for distutils).

Poudriere will process the wheels by
* collecting them after successful package build from the port's work directory,
* discarding a freshly built wheel whose content is unchanged from the last one published for the same name, version, and Python/ABI/platform (a rebuild triggered by e.g. a `PORTREVISION` bump or an unrelated dependency/option change frequently produces byte-identical wheels),
* adding a [build tag](https://packaging.python.org/en/latest/specifications/binary-distribution-format/) to wheels whose content actually changed,
* adding [multiplatform tags](https://packaging.python.org/en/latest/specifications/binary-distribution-format/) to wheels for RELEASE versions with patches from p-2 to p (three in total),
* generating a static simple index

Your wheels are ready to be served by a web server.
