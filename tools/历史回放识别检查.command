#!/bin/zsh
set -eu
project_dir="${0:A:h:h}"
export PYTHONPATH="${project_dir}/src"
export PYTHONDONTWRITEBYTECODE=1
exec "${project_dir}/.venv/bin/python" -B -m jcc_replay_demo "$@"
