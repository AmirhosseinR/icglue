#!/usr/bin/env python3
"""
icglue_hier_drawio.py
---------------------
Turn the JSON netlist from icglue_schematic_extract.tcl into ONE draw.io file
showing the full hierarchy as *nested* rectangles:

    tb_spinnCore_dense
      +- spinnCore_dense
           +- <rf>
           +- spinnCore
                +- spinnArray, skew_bank_0..2, vector_rotator_0..1

Connections are drawn at every level. Port names are placed ABOVE the wire that
enters the module, so the wire never crosses the text.

Usage:
  python3 icglue_hier_drawio.py netlist.json -o schematic.drawio [--svg preview.svg]
"""

import argparse
import json
from xml.sax.saxutils import escape, quoteattr

# ---- geometry ----
TITLE_H = 26
PITCH = 30          # vertical distance between ports (room for a label above each)
PORT_GUTTER = 140  # inner margin reserved for boundary-port stubs + labels
STUB = 22           # length of the port stub sticking into the box
H_GAP = 90          # horizontal gap between layers of children
V_GAP = 34          # vertical gap between children in a layer
PAD = 22            # generic inner padding
LEAF_W = 172
BOTTOM_H = 26       # band at the bottom for clk/reset ports

FILL = {"rtl": "#EAF1FB", "tb": "#F3EEFB", "res": "#FCEDEA", "rf": "#EAF7EE"}
STROKE = {"rtl": "#3B5BA5", "tb": "#6C4BB6", "res": "#B5413B", "rf": "#3B8A4E"}


def is_clkrst(name):
    n = name.lower()
    # match clock / reset nets (clk, clock, rst, reset, reset_n, ...) but not
    # lookalikes such as 'acc_clr_bus' (clr != clk)
    import re
    return bool(re.search(r"(^|_)(clk|clock|rst|reset)(_|$|[0-9])", n))


def pin_dir(p):
    d = p.get("direction", "unknown")
    if d in ("input", "output", "bidirectional"):
        return d
    n = p["name"]
    if n.endswith(("_o", "_out", "_o_n")):
        return "output"
    if n.endswith(("_i", "_in")):
        return "input"
    if n.endswith(("_b", "_io", "_bi")):
        return "bidirectional"
    return "input"


def is_in(d):
    return d == "input"


def is_out(d):
    return d == "output"


def is_bi(d):
    return d == "bidirectional"


class Ctx:
    """Holds the module dict and an id counter."""
    def __init__(self, modules):
        self.M = {m["name"]: m for m in modules}
        self.n = 0
        self.cells = []          # flat list of mxCell xml strings
        self.edges = []

    def nid(self):
        self.n += 1
        return f"n{self.n}"


# ---------------------------------------------------------------------------
# net model within one module
# ---------------------------------------------------------------------------
def build_nets(m):
    nets = {}

    def net(name):
        return nets.setdefault(name, {"drivers": [], "sinks": [], "width": "",
                                      "bidir": False})

    width_of = {}
    for d in m.get("declarations", []):
        width_of[d["name"]] = str(d.get("size", ""))
    for p in m.get("ports", []):
        width_of[p["name"]] = str(p.get("size", ""))

    for inst in m.get("instances", []):
        for pin in inst["pins"]:
            conn = pin["connection"].strip()
            n = net(conn)
            if conn in width_of and not n["width"]:
                n["width"] = width_of[conn]
            ep = {"kind": "pin", "inst": inst["name"], "pin": pin["name"]}
            d = pin_dir(pin)
            if is_out(d):
                n["drivers"].append(ep)
            elif is_bi(d):
                n["bidir"] = True
                n["drivers"].append(ep)
            else:
                n["sinks"].append(ep)

    for p in m.get("ports", []):
        n = net(p["name"])
        if not n["width"]:
            n["width"] = str(p.get("size", ""))
        ep = {"kind": "port", "port": p["name"]}
        d = p["direction"]
        if is_in(d):
            n["drivers"].append(ep)      # input port feeds the interior
        elif is_out(d):
            n["sinks"].append(ep)
        else:
            n["bidir"] = True
            n["drivers"].append(ep)
    return nets


def layer_of(m, nets):
    insts = [i["name"] for i in m["instances"]]
    succ = {n: set() for n in insts}
    w = {}
    for name, net in nets.items():
        if is_clkrst(name):
            continue
        drv = [e["inst"] for e in net["drivers"] if e["kind"] == "pin"]
        snk = [e["inst"] for e in net["sinks"] if e["kind"] == "pin"]
        for a in drv:
            for b in snk:
                if a != b:
                    w[(a, b)] = w.get((a, b), 0) + 1
    seen = set()
    for (a, b) in list(w):
        if (a, b) in seen or (b, a) in seen:
            continue
        seen.add((a, b))
        if w.get((a, b), 0) >= w.get((b, a), 0):
            succ[a].add(b)
        else:
            succ[b].add(a)
    layer = {n: 0 for n in insts}
    for _ in range(len(insts) + 1):
        for a in insts:
            for b in succ[a]:
                if layer[b] < layer[a] + 1:
                    layer[b] = layer[a] + 1
    return layer


# ---------------------------------------------------------------------------
# recursive layout: returns a node dict with size + relative child placement
#   node = {path, module, kind, ports:[{name,dir,width}], w, h,
#           children:[node], childpos:{childname:(x,y)}}
# ports here are the *boundary* ports of the box == the instance's pins
# ---------------------------------------------------------------------------
def count_sides(ports):
    left = sum(1 for p in ports
               if not is_clkrst(p["name"]) and (is_in(p["dir"]) or is_bi(p["dir"])))
    right = sum(1 for p in ports if not is_clkrst(p["name"]) and is_out(p["dir"]))
    return left, right


def inst_ports(inst):
    out = []
    for p in inst["pins"]:
        out.append({"name": p["name"], "dir": pin_dir(p),
                    "connection": p["connection"].strip()})
    return out


def layout(ctx, path, ports, module_name):
    m = ctx.M.get(module_name, {"instances": [], "is_resource": True,
                                "ports": [], "declarations": []})
    # attach bus width to the box ports from the module's own port defs
    wmap = {p["name"]: str(p.get("size", "")) for p in m.get("ports", [])}
    for p in ports:
        if not p.get("width"):
            p["width"] = wmap.get(p["name"], "")
    children_insts = [] if m.get("is_resource") else m.get("instances", [])

    if not children_insts:
        left, right = count_sides(ports)
        h = TITLE_H + max(left, right, 1) * PITCH + PAD + BOTTOM_H
        w = LEAF_W
        return {"path": path, "module": module_name, "kind": "leaf",
                "ports": ports, "w": w, "h": h, "children": [], "childpos": {},
                "nets": {}, "res": m.get("is_resource", False)}

    nets = build_nets(m)
    layer = layer_of(m, nets)
    by_layer = {}
    child_nodes = []
    for inst in children_insts:
        cnode = layout(ctx, path + "/" + inst["name"], inst_ports(inst),
                       inst["of_module"])
        cnode["inst_name"] = inst["name"]
        by_layer.setdefault(layer[inst["name"]], []).append(cnode)
        child_nodes.append(cnode)

    # place children: x by layer, stacked vertically within a layer
    x = PORT_GUTTER
    childpos = {}
    content_h = 0
    for li in sorted(by_layer):
        col = by_layer[li]
        colw = max(c["w"] for c in col)
        y = TITLE_H + PAD
        for c in col:
            childpos[c["inst_name"]] = (x + (colw - c["w"]) / 2, y)
            y += c["h"] + V_GAP
        content_h = max(content_h, y - V_GAP)
        x += colw + H_GAP
    content_w = x - H_GAP + PORT_GUTTER

    # make sure the box is tall enough for its own boundary ports
    left, right = count_sides(ports)
    port_h = TITLE_H + max(left, right, 1) * PITCH + PAD + BOTTOM_H
    W = max(content_w, PORT_GUTTER * 2 + LEAF_W)
    H = max(content_h + PAD + BOTTOM_H, port_h)

    return {"path": path, "module": module_name, "kind": "container",
            "ports": ports, "w": W, "h": H, "children": child_nodes,
            "childpos": childpos, "nets": nets, "res": False}


# ---------------------------------------------------------------------------
# absolute coordinates (shared by draw.io + preview) and an orthogonal
# channel router that assigns each net its own vertical lane so wires don't
# pile on top of each other
# ---------------------------------------------------------------------------
CH_STEP = 11        # spacing between adjacent vertical routing channels
LANE = 16           # horizontal trunk offset step for fan-out


def compute_abs(root):
    box_abs = {}
    port_abs = {}

    def walk(node, ox, oy):
        box_abs[node["path"]] = (ox, oy, node["w"], node["h"])
        h, w = node["h"], node["w"]
        left = [p for p in node["ports"]
                if not is_clkrst(p["name"]) and (is_in(p["dir"]) or is_bi(p["dir"]))]
        right = [p for p in node["ports"]
                 if not is_clkrst(p["name"]) and is_out(p["dir"])]
        for side, ports in (("left", left), ("right", right)):
            n = len(ports)
            if not n:
                continue
            top = TITLE_H + PITCH * 0.6
            span = max(h - top - PAD - BOTTOM_H, (n - 1) * PITCH)
            step = span / max(n - 1, 1)
            for k, p in enumerate(ports):
                py = oy + top + k * step
                px = ox if side == "left" else ox + w
                port_abs[port_id(node["path"], p["name"])] = (px, py, side)
        for c in node["children"]:
            cx, cy = node["childpos"][c["inst_name"]]
            walk(c, ox + cx, oy + cy)

    walk(root, PAD, PAD)
    return box_abs, port_abs


def _stub_sign(ep, side):
    # direction the wire leaves the port, pointing into the routing region
    boundary = ep["kind"] == "port"
    if boundary:                       # container's own boundary port
        return +1 if side == "left" else -1
    return -1 if side == "left" else +1   # child pin


def route_points(node, src_ep, dst_ep, port_abs, chan):
    sid = _endpoint_id(node, src_ep)
    tid = _endpoint_id(node, dst_ep)
    if sid not in port_abs or tid not in port_abs or sid == tid:
        return None
    x1, y1, s1 = port_abs[sid]
    x2, y2, s2 = port_abs[tid]
    ax1 = x1 + _stub_sign(src_ep, s1) * STUB
    ax2 = x2 + _stub_sign(dst_ep, s2) * STUB

    # a dedicated vertical channel just outside the TARGET stub; nets landing on
    # the same target box are fanned into parallel channels so they never merge
    tkey = (node["path"] + "/" + dst_ep["inst"]) if dst_ep["kind"] == "pin" else node["path"]
    idx = chan.get(tkey, 0)
    chan[tkey] = idx + 1
    direction = 1 if ax2 >= ax1 else -1
    cx = ax2 - direction * (STUB + idx * CH_STEP)
    # keep the channel on the source side of the target stub and past the source
    if direction > 0:
        cx = max(cx, ax1 + CH_STEP)
        cx = min(cx, ax2 - 2)
    else:
        cx = min(cx, ax1 - CH_STEP)
        cx = max(cx, ax2 + 2)

    pts = [(x1, y1), (ax1, y1), (cx, y1), (cx, y2), (ax2, y2), (x2, y2)]
    # collapse duplicates
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > 0.5 or abs(p[1] - out[-1][1]) > 0.5:
            out.append(p)
    return out


def _net_targets(net):
    srcs = net["drivers"] if net["drivers"] else net["sinks"][:1]
    targets = net["sinks"] if net["sinks"] else net["drivers"][1:]
    return (srcs[0] if srcs else None), targets


# ---------------------------------------------------------------------------
# emit draw.io cells
# ---------------------------------------------------------------------------
def box_id(path):
    return "BOX::" + path


def port_id(path, name):
    return "PORT::" + path + "::" + name


def kind_style(node, is_top=False):
    if is_top:
        return FILL["tb"], STROKE["tb"]
    if node["res"]:
        return FILL["res"], STROKE["res"]
    if node["kind"] == "leaf" and "regfile" in node["module"]:
        return FILL["rf"], STROKE["rf"]
    return FILL["rtl"], STROKE["rtl"]


def emit_box(ctx, node, parent_id, x, y, is_top=False):
    """Emit this box (relative to parent) and recurse into children."""
    fill, stroke = kind_style(node, is_top)
    bid = box_id(node["path"])
    title = node["path"].split("/")[-1]
    sub = node["module"]
    label = f'<b>{escape(title)}</b>' + ("" if title == sub else f'<br><i>{escape(sub)}</i>')
    style = (f"rounded=0;html=1;whiteSpace=wrap;fillColor={fill};strokeColor={stroke};"
             "verticalAlign=top;fontSize=12;spacingTop=4;")
    ctx.cells.append(
        f'<mxCell id={quoteattr(bid)} value={quoteattr(label)} '
        f'style={quoteattr(style)} vertex="1" parent={quoteattr(parent_id)}>'
        f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{node["w"]:.0f}" '
        f'height="{node["h"]:.0f}" as="geometry"/></mxCell>')

    # boundary ports (labels above the stub so wires never cross the text)
    left = [p for p in node["ports"]
            if not is_clkrst(p["name"]) and (is_in(p["dir"]) or is_bi(p["dir"]))]
    right = [p for p in node["ports"]
             if not is_clkrst(p["name"]) and is_out(p["dir"])]
    bottom = [p for p in node["ports"] if is_clkrst(p["name"])]
    _emit_ports(ctx, node, bid, left, side="left")
    _emit_ports(ctx, node, bid, right, side="right")
    _emit_ports(ctx, node, bid, bottom, side="bottom")

    # children
    for c in node["children"]:
        cx, cy = node["childpos"][c["inst_name"]]
        emit_box(ctx, c, bid, cx, cy)


def _emit_ports(ctx, node, bid, ports, side):
    h = node["h"]
    w = node["w"]
    n = len(ports)
    if n == 0:
        return
    if side == "bottom":
        # clk/reset stubs clustered at the bottom-LEFT corner; not wired
        left_off, step = 34, 72
        for k, p in enumerate(ports):
            xf = min((left_off + k * step) / w, 0.95)
            pid = port_id(node["path"], p["name"])
            st = ("shape=ellipse;html=1;fillColor=#9AA0A6;strokeColor=#5F6368;"
                  "verticalLabelPosition=top;verticalAlign=bottom;"
                  "labelPosition=center;align=center;fontSize=9;fontColor=#5F6368;"
                  "spacingBottom=2;")
            ctx.cells.append(
                f'<mxCell id={quoteattr(pid)} value={quoteattr(escape(p["name"]))} '
                f'style={quoteattr(st)} vertex="1" parent={quoteattr(bid)}>'
                f'<mxGeometry x="{xf:.4f}" y="1.0" width="8" height="8" '
                f'relative="1" as="geometry"><mxPoint x="-4" y="-4" as="offset"/>'
                f'</mxGeometry></mxCell>')
        return
    # spread ports evenly over the vertical face (below the title band)
    top = TITLE_H + PITCH * 0.6
    span = max(h - top - PAD - BOTTOM_H, (n - 1) * PITCH)
    step = span / max(n - 1, 1)
    for k, p in enumerate(ports):
        yf = (top + k * step) / h
        xf = 0.0 if side == "left" else 1.0
        pid = port_id(node["path"], p["name"])
        # a small dot exactly on the boundary; the NAME sits ABOVE the stub,
        # nudged inward so the wire never crosses the text
        lab = escape(p["name"])
        align = "left" if side == "left" else "right"
        st = (f"shape=ellipse;html=1;fillColor={STROKE['rtl']};strokeColor={STROKE['rtl']};"
              "verticalLabelPosition=top;verticalAlign=bottom;"
              f"labelPosition=center;align={align};fontSize=9;spacing=2;"
              f"spacing{'Left' if side=='left' else 'Right'}={STUB};")
        ctx.cells.append(
            f'<mxCell id={quoteattr(pid)} value={quoteattr(lab)} '
            f'style={quoteattr(st)} vertex="1" parent={quoteattr(bid)}>'
            f'<mxGeometry x="{xf}" y="{yf:.4f}" width="8" height="8" '
            f'relative="1" as="geometry"><mxPoint x="-4" y="-4" as="offset"/>'
            f'</mxGeometry></mxCell>')


def _endpoint_id(node, ep):
    if ep["kind"] == "pin":
        return port_id(node["path"] + "/" + ep["inst"], ep["pin"])
    return port_id(node["path"], ep["port"])


def _emit_edges(ctx, node, port_abs, chan):
    if node["kind"] == "container":
        for name, net in node["nets"].items():
            if is_clkrst(name):
                continue
            wlabel = "" if str(net["width"]) in ("1", "") else f'[{net["width"]}]'
            src, targets = _net_targets(net)
            if src is None:
                continue
            sid = _endpoint_id(node, src)
            col = STROKE["rf"] if (net["bidir"] and not net["sinks"]) else "#5A5A5A"
            arrow = "none" if (net["bidir"] and not net["sinks"]) else "block"
            for t in targets:
                pts = route_points(node, src, t, port_abs, chan)
                if not pts:
                    continue
                tid = _endpoint_id(node, t)
                inner = pts[1:-1]     # draw.io connects endpoints; give interior waypoints
                wp = "".join(f'<mxPoint x="{x:.1f}" y="{y:.1f}"/>' for x, y in inner)
                st = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"
                      f"endArrow={arrow};startArrow=none;strokeColor={col};"
                      "fontSize=9;jettySize=auto;exitDx=0;exitDy=0;")
                eid = ctx.nid()
                ctx.edges.append(
                    f'<mxCell id={quoteattr(eid)} value={quoteattr(wlabel)} '
                    f'style={quoteattr(st)} edge="1" parent="1" '
                    f'source={quoteattr(sid)} target={quoteattr(tid)}>'
                    f'<mxGeometry relative="1" as="geometry">'
                    f'<Array as="points">{wp}</Array></mxGeometry></mxCell>')
    for c in node["children"]:
        _emit_edges(ctx, c, port_abs, chan)


# ---------------------------------------------------------------------------
# SVG preview (absolute coords from the same layout tree)
# ---------------------------------------------------------------------------
def render_svg(ctx, root, port_abs, chan):
    parts = []

    def walk(node, ox, oy, is_top=False):
        x, y, w, h = ox, oy, node["w"], node["h"]
        fill, stroke = kind_style(node, is_top)
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
                     f'rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
        title = node["path"].split("/")[-1]
        parts.append(f'<text x="{x+8:.0f}" y="{y+16:.0f}" font-weight="bold" '
                     f'font-size="12" fill="{stroke}">{escape(title)}</text>')
        if title != node["module"]:
            parts.append(f'<text x="{x+8:.0f}" y="{y+27:.0f}" font-style="italic" '
                         f'font-size="9" fill="#666">{escape(node["module"])}</text>')
        left = [p for p in node["ports"]
                if not is_clkrst(p["name"]) and (is_in(p["dir"]) or is_bi(p["dir"]))]
        right = [p for p in node["ports"]
                 if not is_clkrst(p["name"]) and is_out(p["dir"])]
        bottom = [p for p in node["ports"] if is_clkrst(p["name"])]
        for side, ports in (("left", left), ("right", right)):
            for p in ports:
                px, py, _ = port_abs[port_id(node["path"], p["name"])]
                parts.append(f'<circle cx="{px:.0f}" cy="{py:.0f}" r="2.5" fill="{stroke}"/>')
                if side == "left":
                    parts.append(f'<text x="{px+STUB:.0f}" y="{py-4:.0f}" font-size="8.5" '
                                 f'fill="#333">{escape(p["name"])}</text>')
                else:
                    parts.append(f'<text x="{px-STUB:.0f}" y="{py-4:.0f}" font-size="8.5" '
                                 f'text-anchor="end" fill="#333">{escape(p["name"])}</text>')
        nb = len(bottom)
        if nb:
            left_off, step = 34, 72
            for k, p in enumerate(bottom):
                xf = min((left_off + k * step) / w, 0.95)
                px = x + xf * w
                py = y + h
                parts.append(f'<circle cx="{px:.0f}" cy="{py:.0f}" r="2.5" fill="#5F6368"/>')
                parts.append(f'<text x="{px:.0f}" y="{py-5:.0f}" font-size="8" '
                             f'fill="#5F6368" text-anchor="middle">{escape(p["name"])}</text>')
        for c in node["children"]:
            cx, cy = node["childpos"][c["inst_name"]]
            walk(c, ox + cx, oy + cy)

    walk(root, PAD, PAD, is_top=True)

    def draw_edges(node):
        if node["kind"] == "container":
            for net_name, net in node["nets"].items():
                if is_clkrst(net_name):
                    continue
                src, targets = _net_targets(net)
                if src is None:
                    continue
                for t in targets:
                    pts = route_points(node, src, t, port_abs, chan)
                    if not pts:
                        continue
                    col = "#8a6d00" if (net["bidir"] and not net["sinks"]) else "#666"
                    poly = " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)
                    parts.append(f'<polyline points="{poly}" fill="none" '
                                 f'stroke="{col}" stroke-width="1" opacity="0.85"/>')
                    wl = net["width"]
                    if str(wl) not in ("1", ""):
                        mxp = pts[len(pts) // 2]
                        parts.append(f'<text x="{mxp[0]:.0f}" y="{mxp[1]-2:.0f}" '
                                     f'font-size="7.5" fill="{col}" text-anchor="middle">'
                                     f'[{escape(str(wl))}]</text>')
        for c in node["children"]:
            draw_edges(c)

    draw_edges(root)
    W = root["w"] + 2 * PAD
    H = root["h"] + 2 * PAD
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" '
            f'height="{H:.0f}" font-family="Helvetica"><rect width="{W:.0f}" '
            f'height="{H:.0f}" fill="white"/>' + "".join(parts) + '</svg>')


# ---------------------------------------------------------------------------
def find_top(ctx):
    # a testbench (mode 'tb') carries no useful structure, so the design top is
    # the top-most NON-tb module: ignore tb modules as instantiators, then pick
    # the non-tb module that nobody (non-tb) instantiates.
    is_tb = {n: (m.get("mode") == "tb") for n, m in ctx.M.items()}
    instantiated = set()
    for n, m in ctx.M.items():
        if is_tb[n]:
            continue
        for i in m.get("instances", []):
            instantiated.add(i["of_module"])
    tops = [n for n, m in ctx.M.items()
            if not is_tb[n] and m.get("instances") and n not in instantiated]
    return tops[0] if tops else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json")
    ap.add_argument("-o", "--out", default="schematic.drawio")
    ap.add_argument("--svg")
    args = ap.parse_args()

    doc = json.load(open(args.json))
    ctx = Ctx(doc["modules"])
    top = find_top(ctx)
    top_ports = [{"name": p["name"], "dir": p["direction"],
                  "connection": p["name"]} for p in ctx.M[top].get("ports", [])]
    root = layout(ctx, top, top_ports, top)

    emit_box(ctx, root, "1", PAD, PAD, is_top=True)
    box_abs, port_abs = compute_abs(root)
    chan = {}
    _emit_edges(ctx, root, port_abs, chan)

    body = "".join(ctx.cells) + "".join(ctx.edges)
    W = root["w"] + 2 * PAD
    H = root["h"] + 2 * PAD
    model = (f'<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" '
             f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
             f'pageWidth="{W:.0f}" pageHeight="{H:.0f}" math="0" shadow="0">'
             f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
             f'</mxGraphModel>')
    xml = f'<mxfile host="icglue-schematic"><diagram name={quoteattr(top)}>{model}</diagram></mxfile>'
    open(args.out, "w").write(xml)
    print(f"wrote {args.out}  ({W:.0f}x{H:.0f})")
    if args.svg:
        chan2 = {}
        open(args.svg, "w").write(render_svg(ctx, root, port_abs, chan2))
        print(f"wrote {args.svg}")


if __name__ == "__main__":
    main()
