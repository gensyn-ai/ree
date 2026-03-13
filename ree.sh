#!/usr/bin/env bash
# ===- ree.sh --------------------------------------------------------------===#

# Copyright 2026 Gensyn, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the “Software”), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# ===-----------------------------------------------------------------------===#

set -euo pipefail

# This script wraps the REE Docker container and passes through command line arguments to the gensyn-sdk.
#
# By default this script calls the gensyn-sdk `run-all` subcommand.
#
# Output artifacts will exist in ~/.cache/gensyn/ after running this script.
#
# For --prompt-file and --receipt-path, pass paths from your host filesystem.
# They will be automatically mapped into the container.
#
# Example usage:
#   ./ree.sh --model-name Qwen/Qwen3-8B --prompt-text "hello" --max-new-tokens 50
#   ./ree.sh --model-name Qwen/Qwen3-8B --prompt-file prompts.jsonl --max-new-tokens 50
#   ./ree.sh verify --receipt-path ~/.cache/gensyn/.../metadata/receipt_123.json
#
# See the gensyn-sdk documentation for more detailed usage options.
#

IMAGE_REMOTE="gensynai/ree:v0.1.0"
IMAGE_LOCAL="ree"

emit_phase() {
  # Phase protocol for ree.py:
  #   __REE_TUI_PHASE__:<phase-name>
  # It is disabled by default and only enabled when REE_TUI_PHASES=1.
  if [[ "${REE_TUI_PHASES:-0}" == "1" ]]; then
    echo "__REE_TUI_PHASE__:$1"
  fi
}

emit_phase "pull:start"

# Pull the latest image, and suppress Docker CLI hints (e.g., "What's Next?").
# NOTE: On macOS, make sure Docker Desktop is running before executing this script.
DOCKER_CLI_HINTS=false docker pull "${IMAGE_REMOTE}"

docker tag "${IMAGE_REMOTE}" "${IMAGE_LOCAL}"
emit_phase "pull:done"

ARGS=("$@")
SUBCOMMAND="run-all"

# Allow explicit subcommands, but default to run-all when omitted.
if [ ${#ARGS[@]} -gt 0 ] && [[ "${ARGS[0]}" != -* ]]; then
  SUBCOMMAND="${ARGS[0]}"
  ARGS=("${ARGS[@]:1}")
fi

# We mount the local cache into the container as well.
# This is useful for two reasons:
# 1) HF finds cached models inside .cache/huggingface
# 2) the gensyn-sdk uses the provided tasks-root to store exported and compiled models,
#   which we set to .cache/gensyn/ when we call the container.
DOCKER_MOUNTS=(-v "${HOME}/.cache:/home/gensyn/.cache")

# If user passes --prompt-file, mount that host file location and rewrite the
# arg to an in-container path so gensyn-sdk can read it.
prompt_file_host=""
prompt_file_value_index=-1
receipt_path_host=""
receipt_path_value_index=-1
cpu_only=0
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  case "${ARGS[i]}" in
    --cpu-only)
      cpu_only=1
      ;;
    --prompt-file)
      if [ $((i + 1)) -ge ${#ARGS[@]} ]; then
        echo "Error: --prompt-file requires a path value." >&2
        exit 2
      fi
      prompt_file_host="${ARGS[i + 1]}"
      prompt_file_value_index=$((i + 1))
      ;;
    --receipt-path)
      if [ $((i + 1)) -ge ${#ARGS[@]} ]; then
        echo "Error: --receipt-path requires a path value." >&2
        exit 2
      fi
      receipt_path_host="${ARGS[i + 1]}"
      receipt_path_value_index=$((i + 1))
      ;;
  esac
done

if [ -n "${prompt_file_host}" ]; then
  if [ ! -f "${prompt_file_host}" ]; then
    echo "Error: prompt file not found: ${prompt_file_host}" >&2
    exit 2
  fi

  prompt_file_abs_dir="$(cd "$(dirname "${prompt_file_host}")" && pwd -P)"
  prompt_file_base="$(basename "${prompt_file_host}")"
  prompt_file_container_dir="/mnt/prompt-file"
  prompt_file_container_path="${prompt_file_container_dir}/${prompt_file_base}"

  DOCKER_MOUNTS+=(-v "${prompt_file_abs_dir}:${prompt_file_container_dir}:ro")

  ARGS[${prompt_file_value_index}]="${prompt_file_container_path}"
fi

if [ -n "${receipt_path_host}" ]; then
  if [ ! -f "${receipt_path_host}" ]; then
    echo "Error: receipt file not found: ${receipt_path_host}" >&2
    exit 2
  fi

  receipt_path_abs_dir="$(cd "$(dirname "${receipt_path_host}")" && pwd -P)"
  receipt_path_base="$(basename "${receipt_path_host}")"
  receipt_path_container_dir="/mnt/receipt-path"
  receipt_path_container_path="${receipt_path_container_dir}/${receipt_path_base}"

  DOCKER_MOUNTS+=(-v "${receipt_path_abs_dir}:${receipt_path_container_dir}:ro")

  ARGS[${receipt_path_value_index}]="${receipt_path_container_path}"
fi

emit_phase "prepare:args"


# We need to let the container have permissions for modifying ~/.cache.
# This is only needed on Linux.
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
DOCKER_GPU_ARGS=()

install_acl_linux() {
  if command -v setfacl >/dev/null 2>&1; then
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    return 1
  fi
  if ! sudo -n true >/dev/null 2>&1; then
    echo "Warning: sudo requires a password. Please run manually: sudo apt install -y acl" >&2
    return 1
  fi

  # Keep sudo non-interactive here to avoid TUI hangs waiting on a hidden prompt.
  if command -v apt >/dev/null 2>&1; then
    sudo -n apt install -y acl && return 0
  fi
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive sudo -n apt-get install -y acl && return 0
  fi
  if command -v dnf >/dev/null 2>&1; then
    sudo -n dnf install -y acl && return 0
  fi
  if command -v yum >/dev/null 2>&1; then
    sudo -n yum install -y acl && return 0
  fi
  if command -v zypper >/dev/null 2>&1; then
    sudo -n zypper --non-interactive install acl && return 0
  fi
  if command -v pacman >/dev/null 2>&1; then
    sudo -n pacman --noconfirm -S acl && return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    sudo -n apk add acl && return 0
  fi

  return 1
}

set_acl_best_effort() {
  if "$@" 2>/dev/null; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    # Keep sudo non-interactive to avoid blocking without a visible password prompt.
    sudo -n "$@" 2>/dev/null && return 0
  fi

  return 1
}

if [[ "${OS}" == "linux" ]]; then
  emit_phase "prepare:acl:start"
  if [[ "${cpu_only}" -eq 0 ]]; then
    DOCKER_GPU_ARGS=(--gpus all)
  fi
  mkdir -p "$HOME/.cache/gensyn" "$HOME/.cache/huggingface"

  GENSYN_UID="$(docker run --rm --entrypoint id "$IMAGE_LOCAL" -u gensyn)"

  # If container user UID already matches host user, no ACL setup is needed.
  if [[ "${GENSYN_UID}" != "$(id -u)" ]]; then
    if ! command -v setfacl >/dev/null 2>&1; then
      if ! install_acl_linux; then
	  echo "Warning: setfacl unavailable; container may not be able to write to ~/.cache." >&2
      fi
    fi

    if command -v setfacl >/dev/null 2>&1; then
      # The container bind-mounts $HOME/.cache, so it must be able to traverse
      # the parent directory even if the writable cache dirs below have their
      # own ACLs.
      if ! set_acl_best_effort setfacl -m "u:${GENSYN_UID}:x" "$HOME/.cache"; then
        echo "Warning: could not apply traverse ACL to ~/.cache." >&2
      fi

      # Access ACLs can be recursive; default ACLs must only be applied on directories.
      if ! set_acl_best_effort setfacl -R -m "u:${GENSYN_UID}:rwx" \
        "$HOME/.cache/gensyn" "$HOME/.cache/huggingface"; then
        echo "Warning: could not apply recursive ACLs to cache directories." >&2
      fi

      if ! find "$HOME/.cache/gensyn" "$HOME/.cache/huggingface" -type d -print0 \
        | xargs -0 -r setfacl -m "d:u:${GENSYN_UID}:rwx" 2>/dev/null; then
        if ! find "$HOME/.cache/gensyn" "$HOME/.cache/huggingface" -type d -print0 \
          | xargs -0 -r sudo -n setfacl -m "d:u:${GENSYN_UID}:rwx" 2>/dev/null; then
          echo "Warning: could not apply default ACLs to one or more cache directories." >&2
        fi
      fi
    fi
  fi
fi

emit_phase "prepare:done"

# The image ENTRYPOINT is /runtime/bin/gensyn-sdk.
# Keep only core defaults here and forward all user-supplied args.
#
# The container user home is /home/gensyn, so SDK logs show container paths like
#   /home/gensyn/.cache/gensyn/Qwen--Qwen3-0.6B/.../receipt_xxx.json
# We pipe output through sed to rewrite these to the host-equivalent:
#   ~/.cache/gensyn/Qwen--Qwen3-0.6B/.../receipt_xxx.json
CONTAINER_CACHE="/home/gensyn/.cache"
HOST_CACHE="${HOME}/.cache"

TASKS_ROOT_ARGS=()
if [[ "${SUBCOMMAND}" != "validate" ]]; then
  TASKS_ROOT_ARGS=(--tasks-root "${CONTAINER_CACHE}/gensyn")
fi

DOCKER_CMD=(
  docker run --rm
  ${DOCKER_GPU_ARGS+"${DOCKER_GPU_ARGS[@]}"}
  "${DOCKER_MOUNTS[@]}"
  "${IMAGE_LOCAL}"
  "${SUBCOMMAND}"
  "${TASKS_ROOT_ARGS[@]+"${TASKS_ROOT_ARGS[@]}"}"
  "${ARGS[@]}"
)

printf 'Running command:\n'
printf '%q ' "${DOCKER_CMD[@]}"
printf '\n'

emit_phase "run:start"
if "${DOCKER_CMD[@]}" 2>&1 | sed "s|${CONTAINER_CACHE}|${HOST_CACHE}|g"; then
  emit_phase "run:done"
  echo
  # Find and print the most recent receipt file for convenience.
  # Guard against missing cache dir (set -euo pipefail would exit otherwise).
  emit_phase "receipt:scan"
  latest_receipt="$(find "${HOST_CACHE}/gensyn" -name 'receipt_*.json' -print 2>/dev/null \
    | sort | tail -1 || true)"
  if [ -n "${latest_receipt}" ]; then
    emit_phase "receipt:found"
    echo "Receipt: ${latest_receipt}"
  fi
  emit_phase "complete"
else
  status=${PIPESTATUS[0]}
  emit_phase "run:failed"
  exit "${status}"
fi
