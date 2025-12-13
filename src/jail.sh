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
: "${RETAG_LAST_PATCHES_COUNT:=3}"
HOOKS="$(cd "$(dirname "$0")" && pwd)"

# There are three ways multiplatform wheels:
# 1. Use the wheel package and add multiple platform tags to one wheel.
# 2. Create softlinks, but this might cause problems because there will be a
#    mismatch between filename and Tag in WHEEL file.
# 3. Use the wheel package and rewrite platform tag, but that will create new
#    file and consume a lot of disk space.
#
# We are using the first way.

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

create_multiplatform_wheels() {
  local wheel_cmd="$1"
  local os="$(echo "$2" | awk '{print tolower($0)}')"
  local version="$(echo "$3" | awk '{print tolower($0)}' | sed 's/[.-]/_/g')"
  local arch="$4"
  local platform_tag="${os}_${version}_${arch}"
  local base_version="$(echo "${version}" | sed -E 's/_p[0-9]+$//')"
  local patch="$(echo "${version}" | sed -nE 's/.+_p([0-9]+)$/\1/p')"
  patch="${patch:-0}"
  local platform_tags="$(awk -v os="${os}" -v base_version="${base_version}" -v patch="${patch}" -v arch="${arch}" -v last_p="${RETAG_LAST_PATCHES_COUNT}" '
  BEGIN {
    start = (patch - last_p + 1 > 0) ? patch - last_p + 1 : 0
    for (p = start; p <= patch; p++) {
      platform_tag = (p == 0 ? os "_" base_version "_" arch : os "_" base_version "_p" p "_" arch)
      printf "%s%s", (p > start ? "." : ""), platform_tag
    }
    print ""
  }')"
  [ ${VERBOSE} -gt 0 ] && echo "Creating multiplatform Python wheels in: ${PYTHON_WHEELS:?}"
  [ ${VERBOSE} -gt 1 ] && echo "Current platform tag: ${platform_tag}"
  [ ${VERBOSE} -gt 1 ] && echo "New multiplatform tags: ${platform_tags}"
  find "${PYTHON_WHEELS:?}" -maxdepth 1 -name "*${platform_tag}.whl" -newer "${PYTHON_WHEELS}/.stamp" | while read -r wheel; do
    [ ${VERBOSE} -gt 1 ] && echo "Retagging new wheel: ${wheel}"
    "${wheel_cmd:?}" tags --remove --platform-tag="${platform_tags}" "${wheel}" > /dev/null
  done
  find "${PYTHON_WHEELS:?}" -maxdepth 1 \( -name "*${os}_${base_version}_${arch}*.whl" -or \
      -name "*${os}_${base_version}_p*_${arch}*.whl" \) ! -name "*${platform_tag}.whl" ! -newer "${PYTHON_WHEELS}/.stamp" | while read -r wheel; do
    [ ${VERBOSE} -gt 1 ] && echo "Retagging existing wheel: ${wheel}"
    "${wheel_cmd:?}" tags --remove --platform-tag="${platform_tags}" "${wheel}" > /dev/null
  done
}

if [ "${event}" = "start" ]; then
  mkdir -p "${PYTHON_WHEELS:?}/"
  touch "${PYTHON_WHEELS}/.stamp"
fi

if [ "${event}" = "stop" ]; then
  os="$(uname -s)"
  version="$(cat "${POUDRIERED:?}"/jails/"${JAILNAME:?}"/version)"
  arch="$(cat "${POUDRIERED:?}"/jails/"${JAILNAME:?}"/arch)"
  case "${version}" in
    *-RELEASE-p*)
      wheel_cmd="$(find_executable wheel)"
      if [ -n "${wheel_cmd}" ]; then
        create_multiplatform_wheels "${wheel_cmd}" "${os}" "${version}" "${arch}"
      fi
      ;;
    *)
      # No retagging required
      ;;
  esac

  case "${GENERATE_STATIC_INDEX}" in
    yes)
      [ ${VERBOSE} -gt 0 ] && echo "Generating static index in: ${PYTHON_WHEELS:?}"
      [ ${VERBOSE} -gt 0 ] && vflag="-v" || vflag=""
      "${HOOKS}/generate-index.py" $vflag --inspect-metadata "${PYTHON_WHEELS:?}"
      ;;
    *)
      ;;
  esac
fi

exit 0
