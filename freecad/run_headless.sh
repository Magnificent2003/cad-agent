#!/usr/bin/env bash
# freecadcmd-compatible entry point for the upstream MCP headless tool.
set -euo pipefail

component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$component_root/run.sh" freecadcmd "$@"
