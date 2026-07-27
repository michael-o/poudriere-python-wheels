# Building and Processing Python wheels with poudriere for FreeBSD

> [!IMPORTANT]
> This is work in progress and might change in nature!

## Requirements

* `poudriere-devel` installed
* `python` installed if below is desired
* `py-wheel` (`wheel` command) installed; required to normalize and deduplicate wheels, see below

## Installation/Configuration

* Apply [this patch](https://github.com/freebsd/freebsd-ports/compare/main...michael-o:freebsd-ports:build-python-wheels.patch) to your ports tree
* Add `PYDISTUTILS_BUILD_WHEEL=yes` to your `make.conf`, PEP 517-based wheels are built by default
* Copy the hooks from `src/` to `${POUDRIERED}/hooks/plugins/python-wheels/`
* Add `NO_PACKAGE_BUILDING=yes ; export PROCESS_PYTHON_WHEELS=yes` to your `poudriere.conf`
* If you are running ZFS, optionally create the Python wheels dataset:

  ```
  zfs create -o compression=off ${ZPOOL}${ZROOTFS}/data/python-wheels
  ```

## Building/Processing Python Wheels

Run your poudriere build as usual, as soon as a Python package is built it will:
* in case of distutils build the wheel,
* in case of PEP 517 the wheel is already built.

Poudriere will process the wheels by
* collecting them after successful package build from the port's work directory,
* discarding a freshly built wheel whose content is unchanged from the last one published for the same name, version, and Python/ABI/platform (a rebuild triggered by e.g. a `PORTREVISION` bump or an unrelated dependency/option change frequently produces byte-identical wheels),
* adding a [build tag](https://packaging.python.org/en/latest/specifications/binary-distribution-format/) to wheels whose content actually changed,
* adding [multiplatform tags](https://packaging.python.org/en/latest/specifications/binary-distribution-format/) to wheels for RELEASE versions with patches from p-2 to p (three in total),
* generates a static simple index (if `export GENERATE_STATIC_INDEX=yes` is set in your `poudriere.conf`).

Your wheels are ready to be served by a web server.

