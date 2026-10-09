#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${MIRROR_PYTHON:-python3}" "${script_dir}/mirror_images.py" "$@"
