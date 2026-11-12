#!/usr/bin/env bash
set -euo pipefail

key_file="${LUBAN_API_KEY_FILE:-${HOME}/.config/luban/api_key}"

if [[ ! -f "$key_file" || -L "$key_file" ]]; then
  echo "API key file must be a regular non-symlink file: $key_file" >&2
  exit 1
fi

key_mode="$(stat -c '%a' "$key_file")"
if [[ "$key_mode" != "600" ]]; then
  echo "API key file must have mode 600" >&2
  exit 1
fi

IFS= read -r OPENAI_API_KEY < "$key_file"
if [[ -z "$OPENAI_API_KEY" ]]; then
  echo "API key file has no usable first line" >&2
  exit 1
fi
export OPENAI_API_KEY
unset key_mode

if [[ "$#" -eq 0 ]]; then
  echo "Usage: $0 command [args ...]" >&2
  exit 2
fi

exec "$@"
