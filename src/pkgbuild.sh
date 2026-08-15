#!/bin/sh

event="$1"
shift

case "${PROCESS_PYTHON_WHEELS}" in
  yes)
    # Proceed with wheel processing
    ;;
  *)
    [ ${VERBOSE} -gt 0 ] && echo "Skipping Python wheel processing"
    exit 0
    ;;
esac

: "${PYTHON_WHEELS:=${POUDRIERE_DATA:?}/python-wheels/${MASTERNAME}}"
# FIXME There should be a var for the builder mount
WRKDIRS="$(echo "${MASTERMNT}" | sed "s#/ref\$#/${MY_JOBID}#")/wrkdirs"

find_executable() {
  local name="$1"
  local required="${2:-no}"

  for path in $(command -v "${name}") "${HOME}/.local/bin/${name}"; do
    [ -x "${path}" ] && echo "${path}" && return 0
  done

  if [ "${required}" = "yes" ]; then
    echo "Error: Required executable '${name}' not found" >&2
    exit 1
  fi

  return 1
}

if [ "${event}" = "success" ]; then
  port="$1"
  pkgname="$2"

  # Only process ports the user explicitly listed for this build, not
  # automatic ports pulled in only as a dependency of one of them
  # (same distinction as pkg's own "automatic" flag, pkg query %a).
  # all_pkgs is poudriere's build-time record of this: one line per
  # queued package, with the third field "listed" for anything named
  # directly on the command line/list file and something else (e.g.
  # "run", "build") for an automatic dependency. Not exposed to hooks
  # any other way; MASTERMNT is already the /ref mount, so this is the
  # same file poudriere itself reads via pkgname_is_listed().
  all_pkgs="${MASTERMNT:?}/.p/all_pkgs"
  if [ -f "${all_pkgs}" ] && ! awk -v pkgname="${pkgname}" \
      '$1 == pkgname && $3 == "listed" { found=1; exit } END { exit !found }' \
      "${all_pkgs}"; then
    [ ${VERBOSE} -gt 1 ] && echo "Skipping automatic port: ${port}"
    exit 0
  fi

  wheel_cmd="$(find_executable wheel yes)"
  build_tag="$(stat -f %m "${PYTHON_WHEELS:?}/.stamp")"
  metadata_dir="${PYTHON_WHEELS:?}/.metadata"
  # FIXME Cannot retrieve WRKDIR like poudriere does
  for wrkdir in "${WRKDIRS}/usr/ports/${port}"/work-py*; do
    whldir="${wrkdir}/whl"
    if [ -d "${whldir}" ]; then
      [ ${VERBOSE} -gt 0 ] && echo "Copying Python wheels to: ${PYTHON_WHEELS:?}"
      find "${whldir}" -type f -name "*.whl" | while read -r wheel; do
        # A patch bump alone (_pN in the platform tag) does not mean
        # the wheel's content changed, so it is normalized away
        # before this wheel is ever hashed or compared. Safe to
        # replace file-by-file: this wheel was never published or
        # indexed. Skip the subprocess (and the WHEEL/RECORD
        # consistency check it does even on a no-op) entirely when
        # there is no _pN to strip. Mirrors jail.sh's *-RELEASE-p*
        # check, normalized the same way the platform tag itself is
        # (lowercase, hyphens to underscores).
        base="$(basename "${wheel}" .whl)"
        platform_tag="${base##*-}"
        base_platform_tag="${platform_tag}"
        case "${platform_tag}" in
        *_release_p*_*)
          base_platform_tag="$(echo "${platform_tag}" | sed -E 's/_p[0-9]+_/_/')"
          [ ${VERBOSE} -gt 1 ] && echo "Normalizing new wheel: ${wheel}"
          wheel="${whldir}"/"$("${wheel_cmd:?}" tags --remove \
              --platform-tag="${base_platform_tag}" "${wheel}")"
          ;;
        *)
          # No normalization required
          ;;
        esac

        # Compare against the last known content for this group (name,
        # version, py/abi tag, patch-normalized platform). A rebuild
        # triggered by e.g. a PORTREVISION bump or an unrelated changed
        # option/dependency frequently produces byte-identical wheel
        # content; only install when the content actually changed, so
        # a new build tag isn't minted for nothing. Only the latest
        # hash per group is kept: reverting to older content will not
        # be recognized, which is an accepted, rare tradeoff.
        #
        # The metadata file also records the build tag actually used,
        # so jail.sh can look up the file(s) for this group's current
        # latest build tag without having to rank every surviving
        # build-tag variant on disk.
        group_key="${base%-*}-${base_platform_tag}"
        hash="$(sha256 -q "${wheel}")"
        metadata_file="${metadata_dir}/${group_key}"
        if [ -f "${metadata_file}" ] && \
            [ "${hash}" = "$(cut -w -f 1 "${metadata_file}")" ]; then
          [ ${VERBOSE} -gt 1 ] && echo "Discarding new wheel, content unchanged: ${wheel}"
          continue
        fi
        echo "${hash} ${build_tag}" > "${metadata_file}"

        [ ${VERBOSE} -gt 1 ] && echo "Retagging new wheel: ${wheel}"
        wheel="${whldir}"/"$("${wheel_cmd:?}" tags --remove --build="${build_tag}" "${wheel}")"

        [ ${VERBOSE} -gt 1 ] && echo "Copying new wheel: ${wheel}"
        # Some wheels are created with Python's TemporaryFile which has
        # mask of 0600. We need to normalize all to 0644.
        install -m 0644 "${wheel}" "${PYTHON_WHEELS}"
      done
    fi
  done
fi

exit 0
