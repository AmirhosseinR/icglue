#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ICGLUE_TEMPLATE_PATH="$ROOT/share/icglue/templates"
export TCLLIBPATH="$ROOT/lib"

IN="${1:?usage: run.sh CONSTRUCT_FILE [OUTDIR]}"
OUT="${2:-out}"
mkdir -p "$OUT"
tclsh   "$ROOT/schematic/icglue_schematic_extract.tcl" -o "$OUT/netlist.json" "$IN"
python3 "$ROOT/schematic/icglue_schematic_drawio.py"   "$OUT/netlist.json"   -o "$OUT"
echo "done -> $OUT/*.drawio"
