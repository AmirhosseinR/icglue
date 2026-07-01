#!/usr/bin/env bash
#
# schematic/run.sh  —  icglue construct file  ->  single nested draw.io schematic
#
#   bash schematic/run.sh CONSTRUCT_FILE [OUTDIR]
#
# Produces  OUTDIR/<top>.drawio  (+ a .svg preview) where <top> is the design
# top module (the testbench is skipped automatically).
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ICGLUE_TEMPLATE_PATH="$ROOT/share/icglue/templates"
export TCLLIBPATH="$ROOT/lib"

IN="${1:?usage: run.sh CONSTRUCT_FILE [OUTDIR]}"
OUTDIR="${2:-out}"
mkdir -p "$OUTDIR"

# 1) extract the netlist from the icglue database (Tcl)
tclsh "$ROOT/schematic/icglue_schematic_extract.tcl" \
      -o "$OUTDIR/netlist.json" "$IN"

# 2) render one nested draw.io file (Python).  -o accepts a directory:
#    it writes <OUTDIR>/<top>.drawio + <top>.svg inside it.
python3 "$ROOT/schematic/icglue_hier_drawio.py" \
        "$OUTDIR/netlist.json" -o "$OUTDIR"

echo "done -> $OUTDIR/*.drawio"