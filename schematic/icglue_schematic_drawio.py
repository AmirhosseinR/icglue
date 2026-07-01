#!/usr/bin/env python3
"""
icglue_schematic_drawio.py
--------------------------
Stage 2 of the icglue -> draw.io schematic flow.

Reads the neutral JSON netlist produced by icglue_schematic_extract.tcl and
emits, for each parent module, a draw.io (mxGraphModel) diagram:

  * one box per child instance, with input/bidir pins on the left edge and
    output pins on the right edge
  * top-level module ports as boundary pins (inputs far left, outputs far right)
  * one orthogonal edge per net endpoint pair, labelled with bus width and
    marked with an inversion bubble where invert=true
  * a deterministic layered (left-to-right dataflow) auto-layout, baked into
    geometry so the file opens looking sensible but every node stays draggable

A parallel .svg preview is written from the *same* geometry for quick visual
inspection (optional; --no-svg to skip).

Usage:
  python3 icglue_schematic_drawio.py netlist.json -o outdir
"""

import argparse
import html
import json
import os
from xml.sax.saxutils import escape, quoteattr

# ---- visual constants -----------------------------------------------------
PIN_H = 18          # vertical spacing between pins
BOX_W = 150
BOX_PAD_TOP = 28    # header band for the instance name
BOX_PAD_BOT = 12
LAYER_DX = 240      # horizontal gap between layers
ROW_DY = 34         # vertical gap between boxes in a layer
MARGIN = 40
PORT_W = 12
PORT_H = 12

# palette (kept as CSS-ish hex; user can restyle freely in draw.io)
STYLE_INST = "rounded=0;whiteSpace=wrap;html=1;fillColor=#E8F0FE;strokeColor=#3B5BA5;verticalAlign=top;fontStyle=1;spacingTop=4;"
STYLE_INST_RES = "rounded=0;whiteSpace=wrap;html=1;fillColor=#FCE8E6;strokeColor=#B5413B;verticalAlign=top;fontStyle=1;spacingTop=4;"
STYLE_INST_ILM = "rounded=0;whiteSpace=wrap;html=1;fillColor=#E6F4EA;strokeColor=#3B8A4E;verticalAlign=top;fontStyle=1;spacingTop=4;"
STYLE_PIN_IN = "shape=mxgraph.electrical.miscellaneous.terminal;fillColor=#3B5BA5;strokeColor=none;"
STYLE_PORT_IN = "triangle;direction=east;whiteSpace=wrap;html=1;fillColor=#D7E3FC;strokeColor=#3B5BA5;"
STYLE_PORT_OUT = "triangle;direction=east;whiteSpace=wrap;html=1;fillColor=#D7E3FC;strokeColor=#3B5BA5;"
STYLE_PORT_BI = "hexagon;whiteSpace=wrap;html=1;fillColor=#FEF7E0;strokeColor=#B58B00;"
STYLE_EDGE = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;jettySize=auto;"
              "endArrow=block;endFill=1;strokeColor=#444444;fontSize=10;")
STYLE_EDGE_BI = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;jettySize=auto;"
                 "startArrow=none;endArrow=none;strokeColor=#B58B00;fontSize=10;")


def is_input(d):
    return d in ("input",)


def is_output(d):
    return d in ("output",)


def is_bidir(d):
    return d in ("bidirectional", "bidir", "inout")


class IdGen:
    def __init__(self):
        self.n = 1

    def __call__(self, prefix="c"):
        self.n += 1
        return f"{prefix}{self.n}"


# ---------------------------------------------------------------------------
# net model
# ---------------------------------------------------------------------------
def build_nets(mod):
    """Group endpoints by signal name. An endpoint is a (kind, ...) tuple.
    Instance pins key on their `connection`; top ports key on their own name.
    Returns dict: netname -> {'drivers': [...], 'sinks': [...], 'width': str}.
    """
    nets = {}

    def net(name):
        return nets.setdefault(name, {"drivers": [], "sinks": [], "width": "1",
                                      "bidir": False})

    # width lookup from declarations + ports
    width_of = {}
    for d in mod.get("declarations", []):
        width_of[d["name"]] = str(d.get("size", "1"))
    for p in mod.get("ports", []):
        width_of[p["name"]] = str(p.get("size", "1"))

    for inst in mod.get("instances", []):
        for pin in inst.get("pins", []):
            conn = pin["connection"].strip()
            ep = {"kind": "pin", "inst": inst["name"], "pin": pin["name"],
                  "invert": pin.get("invert", False)}
            n = net(conn)
            if conn in width_of:
                n["width"] = width_of[conn]
            d = pin["direction"]
            if is_output(d):
                n["drivers"].append(ep)
            elif is_bidir(d):
                n["bidir"] = True
                n["drivers"].append(ep)  # treated as undirected
            else:
                n["sinks"].append(ep)

    # top ports participate in the net named after the port
    for p in mod.get("ports", []):
        ep = {"kind": "port", "port": p["name"]}
        n = net(p["name"])
        n["width"] = str(p.get("size", "1"))
        d = p["direction"]
        if is_input(d):
            n["drivers"].append(ep)         # input port drives the inside
        elif is_output(d):
            n["sinks"].append(ep)           # output port is driven from inside
        else:
            n["bidir"] = True
            n["drivers"].append(ep)
    return nets


# ---------------------------------------------------------------------------
# layered layout (deterministic, cycle-tolerant longest-path)
# ---------------------------------------------------------------------------
def layer_instances(mod, nets):
    insts = [i["name"] for i in mod.get("instances", [])]
    idx = {n: k for k, n in enumerate(insts)}
    succ = {n: set() for n in insts}

    # aggregate directed weight between instance pairs (driver -> sink)
    weight = {}
    for net in nets.values():
        drv = [e["inst"] for e in net["drivers"] if e["kind"] == "pin"]
        snk = [e["inst"] for e in net["sinks"] if e["kind"] == "pin"]
        for a in drv:
            for b in snk:
                if a != b:
                    weight[(a, b)] = weight.get((a, b), 0) + 1

    # for each unordered pair keep the heavier direction as the forward edge,
    # so the dominant dataflow runs left-to-right and only true feedback bends back
    seen = set()
    for (a, b) in list(weight):
        if (a, b) in seen or (b, a) in seen:
            continue
        seen.add((a, b))
        w_ab = weight.get((a, b), 0)
        w_ba = weight.get((b, a), 0)
        if w_ab >= w_ba:
            succ[a].add(b)
        else:
            succ[b].add(a)

    # longest-path layering (cycle-free after orientation)
    layer = {n: 0 for n in insts}
    changed = True
    guard = 0
    while changed and guard < len(insts) + 2:
        changed = False
        guard += 1
        for a in insts:
            for b in succ[a]:
                if layer[b] < layer[a] + 1:
                    layer[b] = layer[a] + 1
                    changed = True
    return layer


# ---------------------------------------------------------------------------
# geometry assignment
# ---------------------------------------------------------------------------
def box_height(inst):
    left = sum(1 for p in inst["pins"]
               if is_input(p["direction"]) or is_bidir(p["direction"]))
    right = sum(1 for p in inst["pins"] if is_output(p["direction"]))
    rows = max(left, right, 1)
    return BOX_PAD_TOP + rows * PIN_H + BOX_PAD_BOT


def assign_geometry(mod, nets):
    layer = layer_instances(mod, nets)
    insts = mod.get("instances", [])
    by_layer = {}
    for inst in insts:
        by_layer.setdefault(layer[inst["name"]], []).append(inst)

    geom = {}            # inst name -> (x, y, w, h)
    n_layers = (max(layer.values()) + 1) if layer else 1

    # cap column height: a layer with many boxes wraps into sub-columns so we
    # don't produce one absurdly tall stack (common for leaf/unconnected cells)
    MAX_ROWS = 6
    x0 = MARGIN + LAYER_DX
    layer_x = {}
    cur_x = x0
    for li in range(n_layers):
        col = by_layer.get(li, [])
        layer_x[li] = cur_x
        n_sub = max(1, (len(col) + MAX_ROWS - 1) // MAX_ROWS)
        # place row-major down each sub-column
        sub_h = [MARGIN] * n_sub
        for k, inst in enumerate(col):
            sub = k // MAX_ROWS if n_sub > 1 else 0
            # distribute evenly across sub-columns instead of filling first
            sub = k % n_sub
            h = box_height(inst)
            x = cur_x + sub * (BOX_W + 70)
            geom[inst["name"]] = (x, sub_h[sub], BOX_W, h)
            sub_h[sub] += h + ROW_DY
        cur_x += n_sub * (BOX_W + 70) + (LAYER_DX - BOX_W)

    total_w = cur_x + LAYER_DX
    max_y = MARGIN
    for (_, y, _, h) in geom.values():
        max_y = max(max_y, y + h)
    return geom, layer, n_layers, total_w, max_y


# ---------------------------------------------------------------------------
# draw.io XML emission
# ---------------------------------------------------------------------------
def pin_cell_id(inst, pin):
    return f"P__{inst}__{pin}"


def port_cell_id(port):
    return f"BP__{port}"


def emit_module(mod, idg):
    nets = build_nets(mod)
    geom, layer, n_layers, total_w, max_y = assign_geometry(mod, nets)
    cells = []

    # boundary port columns
    in_ports = [p for p in mod["ports"] if is_input(p["direction"])]
    bi_ports = [p for p in mod["ports"] if is_bidir(p["direction"])]
    out_ports = [p for p in mod["ports"] if is_output(p["direction"])]

    left_x = MARGIN
    right_x = total_w - MARGIN - 80
    y = MARGIN
    for p in in_ports + bi_ports:
        st = STYLE_PORT_BI if is_bidir(p["direction"]) else STYLE_PORT_IN
        label = p["name"]
        if str(p.get("size", "1")) not in ("1", ""):
            label += f" [{p['size']}]"
        cells.append(vertex(port_cell_id(p["name"]), label, st,
                            left_x, y, 70, PORT_H + 8))
        y += PORT_H + 26
    y = MARGIN
    for p in out_ports:
        label = p["name"]
        if str(p.get("size", "1")) not in ("1", ""):
            label += f" [{p['size']}]"
        cells.append(vertex(port_cell_id(p["name"]), label, STYLE_PORT_OUT,
                            right_x, y, 70, PORT_H + 8))
        y += PORT_H + 26

    # instance boxes + pins
    for inst in mod["instances"]:
        x, iy, w, h = geom[inst["name"]]
        style = STYLE_INST
        if inst.get("is_res"):
            style = STYLE_INST_RES
        elif inst.get("is_ilm"):
            style = STYLE_INST_ILM
        label = f'{inst["name"]}<br><i>{inst["of_module"]}</i>'
        bid = f"I__{inst['name']}"
        cells.append(vertex(bid, label, style, x, iy, w, h, html_label=True))

        left_pins = [p for p in inst["pins"]
                     if is_input(p["direction"]) or is_bidir(p["direction"])]
        right_pins = [p for p in inst["pins"] if is_output(p["direction"])]
        for k, p in enumerate(left_pins):
            yf = (BOX_PAD_TOP + k * PIN_H + PIN_H / 2) / h
            cells.append(port_child(pin_cell_id(inst["name"], p["name"]),
                                    bid, p["name"], 0.0, yf, side="left"))
        for k, p in enumerate(right_pins):
            yf = (BOX_PAD_TOP + k * PIN_H + PIN_H / 2) / h
            cells.append(port_child(pin_cell_id(inst["name"], p["name"]),
                                    bid, p["name"], 1.0, yf, side="right"))

    # edges
    def ep_cell(ep):
        if ep["kind"] == "pin":
            return pin_cell_id(ep["inst"], ep["pin"])
        return port_cell_id(ep["port"])

    for name, net in nets.items():
        drivers = net["drivers"]
        sinks = net["sinks"]
        width = net["width"]
        wlabel = "" if str(width) in ("1", "") else f"[{width}]"
        if net["bidir"] and not sinks:
            # undirected chain among drivers
            eps = drivers
            for a, b in zip(eps, eps[1:]):
                cells.append(edge(idg("e"), ep_cell(a), ep_cell(b),
                                  STYLE_EDGE_BI, wlabel))
            continue
        # one driver fans out to all sinks (typical); if several drivers, chain
        srcs = drivers if drivers else sinks[:1]
        for s in sinks:
            src = srcs[0] if srcs else None
            if src is None or ep_cell(src) == ep_cell(s):
                continue
            inv = s.get("invert") or (srcs and srcs[0].get("invert"))
            st = STYLE_EDGE + ("startArrow=oval;startFill=0;" if inv else "")
            cells.append(edge(idg("e"), ep_cell(src), ep_cell(s), st,
                              wlabel + (" ~" if inv else "")))

    return cells, total_w, max_y + MARGIN


# ---- low-level mxCell builders -------------------------------------------
def vertex(cid, value, style, x, y, w, h, html_label=False):
    v = value if html_label else escape(value)
    return (f'<mxCell id={quoteattr(cid)} value={quoteattr(v)} '
            f'style={quoteattr(style)} vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>'
            f'</mxCell>')


def port_child(cid, parent, value, xf, yf, side):
    style = ("shape=ellipse;perimeter=none;html=1;fontSize=9;align=" +
             ("left;spacingLeft=6;" if side == "right" else "right;spacingRight=6;") +
             "fillColor=#3B5BA5;strokeColor=#1F3A6E;")
    return (f'<mxCell id={quoteattr(cid)} value={quoteattr(escape(value))} '
            f'style={quoteattr(style)} vertex="1" parent={quoteattr(parent)}>'
            f'<mxGeometry x="{xf}" y="{yf:.4f}" width="{PORT_W}" height="{PORT_H}" '
            f'relative="1" as="geometry">'
            f'<mxPoint x="{-PORT_W/2 if side=="left" else -PORT_W/2}" y="{-PORT_H/2}" as="offset"/>'
            f'</mxGeometry></mxCell>')


def edge(cid, src, tgt, style, label=""):
    return (f'<mxCell id={quoteattr(cid)} value={quoteattr(escape(label))} '
            f'style={quoteattr(style)} edge="1" parent="1" '
            f'source={quoteattr(src)} target={quoteattr(tgt)}>'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>')


# ---------------------------------------------------------------------------
# SVG preview (same geometry as the draw.io export) — for quick visual checks
# ---------------------------------------------------------------------------
def render_svg(mod):
    nets = build_nets(mod)
    geom, layer, n_layers, total_w, max_y = assign_geometry(mod, nets)
    H = max_y + MARGIN
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(total_w)}" '
           f'height="{int(H)}" font-family="Helvetica" font-size="11">']
    out.append(f'<rect width="{int(total_w)}" height="{int(H)}" fill="#ffffff"/>')

    pin_xy = {}  # (inst,pin) -> (x,y) ; ('port',name) -> (x,y)

    # instance boxes + pins
    for inst in mod["instances"]:
        x, iy, w, h = geom[inst["name"]]
        fill = "#E8F0FE"; stroke = "#3B5BA5"
        if inst.get("is_res"): fill, stroke = "#FCE8E6", "#B5413B"
        elif inst.get("is_ilm"): fill, stroke = "#E6F4EA", "#3B8A4E"
        out.append(f'<rect x="{x}" y="{iy}" width="{w}" height="{h}" rx="2" '
                   f'fill="{fill}" stroke="{stroke}"/>')
        out.append(f'<text x="{x+w/2}" y="{iy+16}" text-anchor="middle" '
                   f'font-weight="bold">{escape(inst["name"])}</text>')
        out.append(f'<text x="{x+w/2}" y="{iy+27}" text-anchor="middle" '
                   f'font-style="italic" fill="#555">{escape(inst["of_module"])}</text>')
        left = [p for p in inst["pins"]
                if is_input(p["direction"]) or is_bidir(p["direction"])]
        right = [p for p in inst["pins"] if is_output(p["direction"])]
        for k, p in enumerate(left):
            py = iy + BOX_PAD_TOP + k * PIN_H + PIN_H / 2
            pin_xy[(inst["name"], p["name"])] = (x, py)
            out.append(f'<circle cx="{x}" cy="{py}" r="3" fill="{stroke}"/>')
            out.append(f'<text x="{x+6}" y="{py+3}" font-size="9">{escape(p["name"])}</text>')
        for k, p in enumerate(right):
            py = iy + BOX_PAD_TOP + k * PIN_H + PIN_H / 2
            pin_xy[(inst["name"], p["name"])] = (x + w, py)
            out.append(f'<circle cx="{x+w}" cy="{py}" r="3" fill="{stroke}"/>')
            out.append(f'<text x="{x+w-6}" y="{py+3}" font-size="9" '
                       f'text-anchor="end">{escape(p["name"])}</text>')

    # boundary ports
    in_ports = [p for p in mod["ports"] if is_input(p["direction"])]
    bi_ports = [p for p in mod["ports"] if is_bidir(p["direction"])]
    out_ports = [p for p in mod["ports"] if is_output(p["direction"])]
    y = MARGIN
    for p in in_ports + bi_ports:
        c = "#FEF7E0" if is_bidir(p["direction"]) else "#D7E3FC"
        sc = "#B58B00" if is_bidir(p["direction"]) else "#3B5BA5"
        out.append(f'<rect x="{MARGIN}" y="{y}" width="78" height="20" rx="3" '
                   f'fill="{c}" stroke="{sc}"/>')
        lbl = p["name"] + (f' [{p["size"]}]' if str(p.get("size","1")) not in ("1","") else "")
        out.append(f'<text x="{MARGIN+39}" y="{y+14}" text-anchor="middle" font-size="9">{escape(lbl)}</text>')
        pin_xy[("port", p["name"])] = (MARGIN + 78, y + 10)
        y += PORT_H + 26
    rx = total_w - MARGIN - 80
    y = MARGIN
    for p in out_ports:
        out.append(f'<rect x="{rx}" y="{y}" width="78" height="20" rx="3" '
                   f'fill="#D7E3FC" stroke="#3B5BA5"/>')
        lbl = p["name"] + (f' [{p["size"]}]' if str(p.get("size","1")) not in ("1","") else "")
        out.append(f'<text x="{rx+39}" y="{y+14}" text-anchor="middle" font-size="9">{escape(lbl)}</text>')
        pin_xy[("port", p["name"])] = (rx, y + 10)
        y += PORT_H + 26

    # edges (orthogonal: H-V-H through a mid x)
    def key(ep):
        return (ep["inst"], ep["pin"]) if ep["kind"] == "pin" else ("port", ep["port"])

    for net in nets.values():
        srcs = net["drivers"] if net["drivers"] else net["sinks"][:1]
        col = "#B58B00" if net["bidir"] and not net["sinks"] else "#444"
        targets = net["sinks"] if net["sinks"] else net["drivers"][1:]
        for s in targets:
            if not srcs: break
            a = key(srcs[0]); b = key(s)
            if a not in pin_xy or b not in pin_xy or a == b: continue
            (x1, y1), (x2, y2) = pin_xy[a], pin_xy[b]
            mx = (x1 + x2) / 2
            out.append(f'<polyline points="{x1},{y1} {mx},{y1} {mx},{y2} {x2},{y2}" '
                       f'fill="none" stroke="{col}" stroke-width="1.2"/>')
            wl = net["width"]
            if str(wl) not in ("1", ""):
                out.append(f'<text x="{mx}" y="{(y1+y2)/2-2}" text-anchor="middle" '
                           f'font-size="8" fill="{col}">[{escape(str(wl))}]</text>')
    out.append("</svg>")
    return "\n".join(out)


def wrap_drawio(diagrams):
    body = "".join(diagrams)
    return f'<mxfile host="icglue-schematic">{body}</mxfile>'


def diagram_xml(name, cells, w, h):
    root = ('<root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + "".join(cells) + '</root>')
    model = (f'<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" '
             f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" '
             f'page="1" pageWidth="{int(w)}" pageHeight="{int(h)}" math="0" '
             f'shadow="0">{root}</mxGraphModel>')
    return f'<diagram name={quoteattr(name)}>{model}</diagram>'


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json")
    ap.add_argument("-o", "--outdir", default=".")
    ap.add_argument("--combined", action="store_true",
                    help="emit one .drawio with a tab per module")
    args = ap.parse_args()

    with open(args.json) as fh:
        doc = json.load(fh)
    os.makedirs(args.outdir, exist_ok=True)

    diagrams = []
    for mod in doc.get("modules", []):
        cells, w, h = emit_module(mod, IdGen())
        diagrams.append(diagram_xml(mod["name"], cells, w, h))
        if not args.combined:
            out = os.path.join(args.outdir, f"{mod['name']}.drawio")
            with open(out, "w") as fh:
                fh.write(wrap_drawio([diagram_xml(mod["name"], cells, w, h)]))
            print(f"wrote {out}")
            svgp = os.path.join(args.outdir, f"{mod['name']}.preview.svg")
            with open(svgp, "w") as fh:
                fh.write(render_svg(mod))

    if args.combined:
        out = os.path.join(args.outdir, "schematic.drawio")
        with open(out, "w") as fh:
            fh.write(wrap_drawio(diagrams))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
