#!/bin/sh
set -u

odin_edu_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
odin_edu_state_root="${ODIN_EDU_STATE_ROOT:-${odin_edu_repo_root}/.odin-edu-sandboxes}"
odin_edu_python="${ODIN_EDU_PYTHON:-python3}"
odin_edu_result=0

if [ ! -d "${odin_edu_state_root}" ]; then
    exit 0
fi

for odin_edu_state_file in "${odin_edu_state_root}"/*/state.json; do
    [ -f "${odin_edu_state_file}" ] || continue
    if ! odin_edu_phase=$("${odin_edu_python}" -c \
        'import json, pathlib, sys; value=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")); phase=value.get("phase"); print(phase if isinstance(phase, str) else "")' \
        "${odin_edu_state_file}"); then
        odin_edu_result=1
        continue
    fi
    case "${odin_edu_phase}" in
        ACTIVE|EXPIRED)
            odin_edu_sandbox_dir=$(dirname -- "${odin_edu_state_file}")
            odin_edu_sandbox_id=$(basename -- "${odin_edu_sandbox_dir}")
            if ! PYTHONPATH="${odin_edu_repo_root}" "${odin_edu_python}" -m ops.edu_sandbox \
                --state-root "${odin_edu_state_root}" reconcile "${odin_edu_sandbox_id}"; then
                odin_edu_result=1
            fi
            ;;
    esac
done

exit "${odin_edu_result}"
