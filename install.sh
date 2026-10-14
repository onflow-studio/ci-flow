#!/usr/bin/env bash
# Installs ci-flow for the current user: copies this tree's code to
# ~/.local/share/ci-flow/lib/<version>/ (lib/current points at it), links cimon
# and ci-flow into ~/.local/bin, and writes and starts the ci-flowd user unit.
# The units run the installed copy, never this checkout, so switching branches
# here changes nothing until you install again.
#
#   ./install.sh            install and (re)start ci-flowd
#   ./install.sh --no-start write the unit without starting it
set -euo pipefail

here=$(dirname "$(readlink -f "$0")")
exec python3 "$here/ci-flow" install "$@"
