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

if [ "${event}" = "success" ]; then
  port="$1"
  pkgname="$2"
  # FIXME Cannot retrieve WRKDIR like poudriere does
  for wrkdir in "${WRKDIRS}/usr/ports/${port}"/work-py*; do
    whldir="${wrkdir}/whl"
    if [ -d "${whldir}" ]; then
      [ ${VERBOSE} -gt 0 ] && echo "Copying Python wheels to: ${PYTHON_WHEELS:?}"
      find "${whldir}" -type f -name "*.whl" | while read -r wheel; do
        [ ${VERBOSE} -gt 1 ] && echo "Copying wheel: ${wheel}"
        # Some wheels are created with Python's TemporaryFile which has
        # mask of 0600. We need to normalize all to 0644.
        install -m 0644 "${wheel}" "${PYTHON_WHEELS}"
      done
    fi
  done
fi

exit 0
