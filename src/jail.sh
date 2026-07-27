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
  local base_platform_tag="${os}_${base_version}_${arch}"
  local patch="$(echo "${version}" | sed -nE 's/.+_p([0-9]+)$/\1/p')"
  patch="${patch:-0}"
  local wheel_redirect="/dev/null"
  [ "$VERBOSE" -gt 1 ] && wheel_redirect="/dev/stdout"
  local platform_tags="$(
    awk -v os="${os}" -v base_version="${base_version}" -v patch="${patch}" \
        -v arch="${arch}" -v last_p="${RETAG_LAST_PATCHES_COUNT}" '
    BEGIN {
      start = (patch - last_p + 1 > 0) ? patch - last_p + 1 : 0
      n = 0

      # Build all tags
      for (p = start; p <= patch; p++) {
        tag = p == 0 \
              ? os "_" base_version "_" arch \
              : os "_" base_version "_p" p "_" arch
        tags[n++] = tag
      }

      # Lexicographic sort (same as Python sorted())
      for (i = 0; i < n; i++) {
        for (j = i + 1; j < n; j++) {
          if (tags[i] > tags[j]) {
            tmp = tags[i]
            tags[i] = tags[j]
            tags[j] = tmp
          }
        }
      }

      # Join with dots
      for (i = 0; i < n; i++) {
        printf "%s%s", (i > 0 ? "." : ""), tags[i]
      }
      print ""
    }'
  )"
  [ ${VERBOSE} -gt 0 ] && echo "Creating multiplatform Python wheels in: ${PYTHON_WHEELS:?}"
  [ ${VERBOSE} -gt 1 ] && echo "Current platform tag: ${platform_tag}"
  [ ${VERBOSE} -gt 1 ] && echo "New multiplatform tags: ${platform_tags}"
  # pkgbuild.sh records the build tag it stamped for each group's
  # current latest content in PYTHON_WHEELS/.metadata/<group_key>. Only
  # files matching that build tag are candidates for a new window:
  # older build tags are frozen at whatever window they already had and
  # must not be touched again, since they may be referenced in a lock
  # file. A single build tag can still have more than one file on disk
  # (each earlier patch bump's window extension is itself kept, for the
  # same reason), so this looks up all of them, not just one.
  find "${PYTHON_WHEELS:?}/.metadata" -maxdepth 1 -type f \
      -name "*-${base_platform_tag}" | while read -r metadata_file; do
    local group_key="$(basename "${metadata_file}")"
    local build_tag="$(cut -w -f 2 "${metadata_file}")"
    local prefix="${group_key%-${base_platform_tag}}"
    local name_version="$(echo "${prefix}" | cut -d'-' -f1,2)"
    local pytag_abitag="$(echo "${prefix}" | cut -d'-' -f3-)"
    find "${PYTHON_WHEELS:?}" -maxdepth 1 \
        -name "${name_version}-${build_tag}-${pytag_abitag}-*.whl" | while read -r wheel; do
      local wheel_base="${wheel%*-*.whl}"
      local retagged_wheel="${wheel_base}-${platform_tags}.whl"
      if [ -e "${retagged_wheel}" ]; then
        [ ${VERBOSE} -gt 1 ] && echo "Skipping already existing retagged wheel: ${retagged_wheel}"
      elif [ "${wheel}" -nt "${PYTHON_WHEELS}/.stamp" ]; then
        [ ${VERBOSE} -gt 1 ] && echo "Retagging new wheel: ${wheel}"
        # It is safe to remove the original wheel because it has not been seen before, thus not been indexed.
        "${wheel_cmd:?}" tags --remove --platform-tag="${platform_tags}" "${wheel}" > "${wheel_redirect}"
      else
        [ ${VERBOSE} -gt 1 ] && echo "Retagging existing wheel: ${wheel}"
        # Existing wheels cannot be removed because they might be referenced in a lock file
        "${wheel_cmd:?}" tags --platform-tag="${platform_tags}" "${wheel}" > "${wheel_redirect}"
      fi
    done
  done
}

if [ "${event}" = "start" ]; then
  mkdir -p "${PYTHON_WHEELS:?}/.metadata"
  touch "${PYTHON_WHEELS}/.stamp"
fi

if [ "${event}" = "stop" ]; then
  os="$(uname -s)"
  version="$(cat "${POUDRIERED:?}"/jails/"${JAILNAME:?}"/version)"
  arch="$(cat "${POUDRIERED:?}"/jails/"${JAILNAME:?}"/arch)"
  wheel_cmd="$(find_executable wheel yes)"
  case "${version}" in
    *-RELEASE-p*)
      create_multiplatform_wheels "${wheel_cmd}" "${os}" "${version}" "${arch}"
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
