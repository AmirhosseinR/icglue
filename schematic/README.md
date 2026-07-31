# icglue → draw.io schematic generator

Generate a single, editable **draw.io schematic** of an icglue design directly
from the connectivity icglue already parsed. The diagram nests modules the way
the RTL does and draws every connection at every level.

```
construct.icglue ──► [Stage 1: Tcl]  ──►  netlist.json  ──►  [Stage 2: Python]  ──►  <top>.drawio (+ .svg)
                          icglue_schematic_extract.tcl                  icglue_hier_drawio.py
```

## Quick start

```bash
# Verilog/SystemVerilog to draw.io schematic
python3 schematic/verilog_block_drawio.py schematic/sample.v

# icglue design to draw.io schematic
bash schematic/run.sh path/to/design.icglue OUTDIR
# -> OUTDIR/<top>.drawio  and  OUTDIR/<top>.svg
```

`run.sh` sets `ICGLUE_TEMPLATE_PATH`/`TCLLIBPATH`, extracts the netlist, and
renders the schematic. `-o` accepts either a file or a directory.

## What the diagram shows

* **Nested hierarchy** — the design top (testbenches, `mode=tb`, are skipped) as
  the outer rectangle, its children nested inside, recursively down to leaves.
* **Every connection at every level**, grouped into nets (driver → sinks).
* **Ports** with the name placed *above* the wire so text never sits under a line.
* **clk / reset** kept on the left edge, greyed and intentionally *unrouted*
  (they are global; drawing them just adds noise).
* **Bus vs control** — multi-bit buses are drawn heavier and labelled with their
  width; single-bit control lines are thin.
* **Per-module groups** — each module box owns only its own ports, so in draw.io
  you can grab and move any module (with its ports) independently. There is no
  global group.
* **Obstacle-avoiding routing** — an A* router over a per-container lane grid
  routes wires through free space and around unrelated modules. Lanes closer
  than a few px are merged, and each track is owned by at most one net, so two
  unrelated wires are never drawn on top of each other. Verified to produce
  zero wire-through-module crossings and zero overlapping tracks.
* **Legend** describing line weights and the clk/reset convention.

## Two stages

**Stage 1 – `icglue_schematic_extract.tcl`** loads the ICGlue package, loads a
template set (needed for module creation), runs the construct script, resolves
bus widths, then walks `ig::db::*` to emit a neutral JSON netlist: every module
with its ports, internal declarations, child instances and their pins
(name, connection, direction, invert). Resource blackboxes are handled (their
pin directions are inferred from the `_i`/`_o` naming convention).

**Stage 2 – `icglue_hier_drawio.py`** reads the JSON and renders one
`mxGraphModel` file: recursive layered layout (barycenter-ordered to reduce
crossings), absolute-positioned per-module groups, the wire router, styling,
and a legend. It also writes a matching `.svg` preview (`--no-svg` to skip).

Placement and port assignment then co-adapt to straighten wires: boxes shift to
line their ports up with what they connect to, faces grow when the modules they
talk to are spread wider than the default box height, and each column reserves
a clear band for wires that pass straight over it.

**Standalone – `verilog_block_drawio.py`** needs no icglue at all: it parses a
Verilog/SystemVerilog module header and draws that one block, using the same
styling so both tools' output matches.


## Build (once, to run icglue itself)

Needs `tcl8.6-dev`, `tcllib`, `libglib2.0-dev`. Build the C core with
`make -C lib PKG_CFG_LIBS="glib-2.0 tcl8.6"`, assemble `lib/ICGlue/`
(symlink `tcllib/*.tcl`, copy `icglue.so`), then
`tclsh scripts/tcl_pkggen.tcl lib/ICGlue`. 

>>In Codespaces the `.devcontainer` does this automatically.