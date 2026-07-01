#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root

sudo apt-get update
sudo apt-get install -y build-essential pkg-config \
     tcl8.6-dev tcllib libglib2.0-dev python3

# 1) compile the C core (Ubuntu 24.04 ships tcl8.6.pc, not tcl.pc)
make -C lib PKG_CFG_LIBS="glib-2.0 tcl8.6"

# 2) assemble the ICGlue Tcl package and its index
mkdir -p lib/ICGlue
cp -f lib/binaries/icglue.so lib/ICGlue/
for f in tcllib/*.tcl; do ln -sf "../../$f" "lib/ICGlue/"; done
tclsh scripts/tcl_pkggen.tcl lib/ICGlue
echo "icglue build complete."
