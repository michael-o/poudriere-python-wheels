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
  wheel_cmd="$(find_executable wheel)"
  build_tag="$(stat -f %m "${PYTHON_WHEELS:?}/.stamp")"
  # FIXME Cannot retrieve WRKDIR like poudriere does
  for wrkdir in "${WRKDIRS}/usr/ports/${port}"/work-py*; do
    whldir="${wrkdir}/whl"
    if [ -d "${whldir}" ]; then
      [ ${VERBOSE} -gt 0 ] && echo "Copying Python wheels to: ${PYTHON_WHEELS:?}"
      find "${whldir}" -type f -name "*.whl" | while read -r wheel; do
        if [ -n "${wheel_cmd}" ]; then
          [ ${VERBOSE} -gt 1 ] && echo "Retagging new wheel: ${wheel}"
          wheel="${whldir}"/"$("${wheel_cmd:?}" tags --remove --build="${build_tag}" "${wheel}")"
        fi
        [ ${VERBOSE} -gt 1 ] && echo "Copying new wheel: ${wheel}"
        # Some wheels are created with Python's TemporaryFile which has
        # mask of 0600. We need to normalize all to 0644.
        install -m 0644 "${wheel}" "${PYTHON_WHEELS}"
      done
    fi
  done
fi

exit 0
