#!/usr/bin/env python3
"""
icglue_hier_drawio.py
---------------------
Turn the JSON netlist from icglue_schematic_extract.tcl into ONE draw.io file
showing the full hierarchy as *nested* rectangles.

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
PORT_GUTTER = 140   # inner margin reserved for boundary-port stubs + labels
STUB = 22           # length of the port stub sticking into the box (routing)
LABEL_SPACING = 14  # horizontal gap between the port dot and its name label
H_GAP = 110         # horizontal gap between layers of children
V_GAP = 46          # vertical gap between children in a layer
PAD = 22            # generic inner padding
LEAF_W = 172
BOTTOM_H = 26       # band at the bottom for clk/reset ports
PORT_PITCH_MAX = 2 * PITCH   # widest a face will spread its ports to reach
                             # their partners' rows before it stops growing
LANE_SEP = 6        # minimum gap between routing lanes; two wires closer than
                    # this read as a single line, so the router must not treat
                    # them as independent tracks
CORRIDOR_MAX = 240  # tallest clear band placement will open across a column
                    # for wires passing over it. Tuned: raising it further
                    # keeps cutting bends but starts flinging siblings apart,
                    # so total wire length and vertical travel get worse -- a
                    # sprawling diagram, not a cleaner one. 240 is the joint
                    # minimum of both sprawl measures.

FILL = {"rtl": "#EDF1F6", "tb": "#EFEDF6", "res": "#F5F0E8", "rf": "#ECF2ED"}
STROKE = {"rtl": "#41617F", "tb": "#5F5488", "res": "#9A7A45", "rf": "#557A60"}
TOPFILL = "#F7F9FC"          # outermost container: near-neutral
WIRE = "#556170"             # data wire colour
WIRE_BUS = "#38455A"         # bus wire colour (slightly darker/heavier)
LABEL_BG = "#FFFFFF"


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
def _is_reset(name):
    n = name.lower()
    import re
    return bool(re.search(r"(^|_)(rst|reset)(_|$|[0-9])", n))


def port_sides(ports):
    """Return (left, right) ordered port lists.
    Left column = data inputs/bidir first, then reset(s), then clock(s) at the
    very bottom. Right column = data outputs. clk/reset are left unwired.
    """
    left = [p for p in ports
            if not is_clkrst(p["name"]) and (is_in(p["dir"]) or is_bi(p["dir"]))]
    right = [p for p in ports if not is_clkrst(p["name"]) and is_out(p["dir"])]
    clkrst = [p for p in ports if is_clkrst(p["name"])]
    # reset above, clock at the bottom (lowest signal)
    clkrst.sort(key=lambda p: 0 if _is_reset(p["name"]) else 1)
    return left + clkrst, right


def count_sides(ports):
    left, right = port_sides(ports)
    return len(left), len(right)


# approximate rendered width (px) of a port's name label at fontSize 12
def _label_px(name):
    return len(name) * 6.6 + LABEL_SPACING + 8


def leaf_width(ports):
    """Width that guarantees left-side and right-side labels never collide in
    the middle of the box (the fixed LEAF_W could not hold long port names)."""
    left, right = port_sides(ports)
    lw = max((_label_px(p["name"]) for p in left), default=0)
    rw = max((_label_px(p["name"]) for p in right), default=0)
    return max(LEAF_W, int(lw + rw + PAD))


def inst_ports(inst):
    out = []
    for p in inst["pins"]:
        out.append({"name": p["name"], "dir": pin_dir(p),
                    "connection": p["connection"].strip()})
    return out


def order_layers(m, nets, layer):
    """Order instances within each layer by barycenter of their neighbours in
    the adjacent layer, reducing edge crossings. Returns {layer: [names]}."""
    insts = [i["name"] for i in m["instances"]]
    adj = {n: set() for n in insts}
    for name, net in nets.items():
        if is_clkrst(name):
            continue
        members = [e["inst"] for e in (net["drivers"] + net["sinks"])
                   if e["kind"] == "pin"]
        for a in members:
            for b in members:
                if a != b:
                    adj[a].add(b)
    by_layer = {}
    for n in insts:
        by_layer.setdefault(layer[n], []).append(n)
    order = {n: i for L in by_layer for i, n in enumerate(by_layer[L])}
    layers_sorted = sorted(by_layer)
    for sweep in range(6):
        seq = layers_sorted if sweep % 2 == 0 else list(reversed(layers_sorted))
        for L in seq:
            ref = L - 1 if sweep % 2 == 0 else L + 1
            if ref not in by_layer:
                continue
            refpos = {n: i for i, n in enumerate(by_layer[ref])}

            def bary(n):
                ns = [refpos[x] for x in adj[n] if x in refpos]
                return (sum(ns) / len(ns)) if ns else order[n]
            by_layer[L].sort(key=bary)
            for i, n in enumerate(by_layer[L]):
                order[n] = i
    return by_layer


def _pack_column(order, desired, height, gap, top):
    """Place items (in the given vertical order) as close as possible to their
    desired y while keeping order and a minimum gap; never start above `top`."""
    n = len(order)
    y = [0.0] * n
    for i in range(n):
        d = desired[i]
        if i == 0:
            y[i] = max(d, top)
        else:
            y[i] = max(d, y[i - 1] + height[order[i - 1]] + gap)
    return y


def _refit(node):
    """Recompute a container's width/height from its (possibly moved) children
    so nothing spills outside the box after placement refinement. Bottom-up."""
    for c in node["children"]:
        _refit(c)
    if node["kind"] == "container" and node["children"]:
        maxbot = maxright = 0
        for c in node["children"]:
            cx, cy = node["childpos"][c["inst_name"]]
            maxbot = max(maxbot, cy + c["h"])
            maxright = max(maxright, cx + c["w"])
        left, right = count_sides(node["ports"])
        port_h = TITLE_H + max(left, right, 1) * PITCH + PAD + BOTTOM_H
        # min_h is the height grow_faces decided this box needs so its ports
        # can reach their partners' rows; never shrink back below it
        node["h"] = max(maxbot + PAD + BOTTOM_H, port_h, node.get("min_h", 0.0))
        node["w"] = max(maxright + PORT_GUTTER, PORT_GUTTER * 2 + LEAF_W)


CORRIDOR = "\x00corridor"     # virtual column member, never a real instance


def _column_corridor(node, col, box_abs, port_abs):
    """The horizontal band a column must keep clear for wires passing over it.

    A net whose source sits left of a column and whose sink sits right of it
    has to traverse that column's x-range. When every row is occupied by a box
    the router has no straight path and must weave, turning a logically
    straight connection into a multi-bend detour. Placement, not routing, has
    to fix that: the column needs a real gap at the right height.

    The band is packed as a virtual column member rather than enforced as a
    hard keep-out. That matters: a hard band has to shove blocks clear of it,
    and since the band is derived from port rows, moving the blocks moves the
    very ports it was measured from -- self-defeating. Measured on the sample
    design, the hard variant halved the number of straight wires.

    Returns (top_y, height) in the parent's coordinate space, or None.
    """
    if not col:
        return None
    paths = {c["path"] for c in col}
    _ox, oy, _w, _h = box_abs[node["path"]]
    x0 = min(box_abs[c["path"]][0] for c in col)
    x1 = max(box_abs[c["path"]][0] + box_abs[c["path"]][2] for c in col)

    wants = []
    for name, net in node["nets"].items():
        if is_clkrst(name):
            continue
        src, targets = _net_targets(net)
        if src is None:
            continue
        sid = _endpoint_id(node, src)
        if sid not in port_abs:
            continue
        sx, sy, _ss = port_abs[sid]
        for t in targets:
            tid = _endpoint_id(node, t)
            if tid not in port_abs or tid == sid:
                continue
            # a net that lands on a box in this column is not passing over it
            if any(sid.startswith("PORT::" + p + "::")
                   or tid.startswith("PORT::" + p + "::") for p in paths):
                continue
            tx, ty, _ts = port_abs[tid]
            if not (min(sx, tx) < x0 - 1 and max(sx, tx) > x1 + 1):
                continue
            # Only nets whose two ends are already level. For those, a box in
            # the way is the sole reason the wire is not straight, so opening a
            # gap converts it to a clean run. A net whose ends sit at different
            # rows has to jog regardless; reserving space for it buys nothing
            # and just pushes the blocks apart (measured on a wider design:
            # +320px of height for zero extra straight wires).
            if abs(sy - ty) < 0.6:
                wants.append(sy)
    if not wants:
        return None
    lo, hi = min(wants), max(wants)
    # Only as tall as the wires crossing here actually need. Budgeting by the
    # number of distinct rows keeps a busy column's corridor generous and a
    # quiet column's corridor small.
    lanes = len({round(v / LANE_SEP) for v in wants})
    span = min(hi - lo, CORRIDOR_MAX, lanes * PITCH)
    return (lo - oy, span)


def _wmedian(pairs):
    """Weighted median of (value, weight) pairs."""
    if not pairs:
        return None
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0
    for v, w in pairs:
        acc += w
        if acc * 2 >= tot:
            return v
    return pairs[-1][0]


def refine_placement(root, iters=12):
    """Coupled vertical placement + port alignment, weighted by bus width.

    Each pass: (1) shift every module by the width-weighted median vertical
    offset needed to line its ports up with the ports they drive/receive,
    packing each layer column to avoid overlap; (2) grow containers to fit;
    (3) run the per-port aligner. Weighting by net width means a 256-bit
    datapath is made dead-straight while incidental 1-bit controls give way —
    exactly how an engineer would draw it: straight fat buses, minor jogs on
    control lines."""
    # width-weighted port adjacency: pid -> [(neighbour_pid, width), ...]
    wadj = {}

    def add(n):
        if n["kind"] == "container":
            for nm, net in n["nets"].items():
                if is_clkrst(nm):
                    continue
                ws = str(net["width"])
                w = int(ws) if ws.isdigit() else 1
                w = max(w, 1)
                eps = [_endpoint_id(n, e) for e in (net["drivers"] + net["sinks"])]
                for a in eps:
                    for b in eps:
                        if a != b:
                            wadj.setdefault(a, []).append((b, w))
        for c in n["children"]:
            add(c)
    add(root)

    nodes = []

    def collect(n):
        nodes.append(n)
        for c in n["children"]:
            collect(c)
    collect(root)

    for _ in range(iters):
        box_abs, port_abs = compute_abs(root)
        for node in nodes:
            if node["kind"] != "container" or not node["children"]:
                continue
            cols = {}
            for c in node["children"]:
                cx, _cy = node["childpos"][c["inst_name"]]
                cols.setdefault(round(cx, 2), []).append(c)
            for _x, col in cols.items():
                col.sort(key=lambda c: node["childpos"][c["inst_name"]][1])
                heights = {c["inst_name"]: c["h"] for c in col}
                order = [c["inst_name"] for c in col]
                desired = []
                for c in col:
                    port_deltas = []
                    for p in c["ports"]:
                        if is_clkrst(p["name"]):
                            continue
                        pid = port_id(c["path"], p["name"])
                        if pid not in port_abs:
                            continue
                        py = port_abs[pid][1]
                        nb = [(port_abs[q][1] - py, w)
                              for q, w in wadj.get(pid, ()) if q in port_abs]
                        med = _wmedian(nb)
                        if med is not None:
                            pw = sum(w for _, w in nb)
                            port_deltas.append((med, pw))
                    oy = node["childpos"][c["inst_name"]][1]
                    d = _wmedian(port_deltas)
                    if d is not None:
                        oy += d
                    desired.append(oy)
                # reserve a clear band for wires that pass over this column,
                # by packing it as if it were another (invisible) member
                corr = _column_corridor(node, col, box_abs, port_abs)
                if corr is not None:
                    ctop, chgt = corr
                    at = sum(1 for d in desired if d < ctop)   # keep box order
                    order.insert(at, CORRIDOR)
                    desired.insert(at, ctop)
                    heights[CORRIDOR] = chgt
                placed = _pack_column(order, desired, heights, V_GAP,
                                      TITLE_H + PAD)
                for nm, yv in zip(order, placed):
                    if nm == CORRIDOR:
                        continue
                    ox = node["childpos"][nm][0]
                    node["childpos"][nm] = (ox, yv)
        _refit(root)
        align_ports(root)


def harmonize_orders(root, iters=4):
    """Make a container's BOUNDARY ports follow the vertical order of the
    interior child ports they connect to, so a pass-through signal keeps ONE
    consistent order at every level of the hierarchy. Without this, the outer
    face may list ports in a different order than the inner face, and the
    monotone port packer then physically cannot put both endpoints on the same
    row — the wire is forced to jog even though there is empty space. Only
    boundary faces are reordered (interior orders, set by their own children,
    are left intact), so it converges instead of oscillating the way a global
    re-sort does."""
    nodes = []

    def collect(n):        # children first (bottom-up)
        for c in n["children"]:
            collect(c)
        nodes.append(n)
    collect(root)

    for _ in range(iters):
        _, port_abs = compute_abs(root)
        for node in nodes:
            if node["kind"] != "container":
                continue
            key = {}
            for name, net in node["nets"].items():
                if is_clkrst(name):
                    continue
                bports = [e["port"] for e in (net["drivers"] + net["sinks"])
                          if e["kind"] == "port"]
                if not bports:
                    continue
                ys = []
                for e in (net["drivers"] + net["sinks"]):
                    if e["kind"] == "pin":
                        pid = port_id(node["path"] + "/" + e["inst"], e["pin"])
                        if pid in port_abs:
                            ys.append(port_abs[pid][1])
                if ys:
                    ys.sort()
                    med = ys[len(ys) // 2]
                    for bp in bports:
                        key[bp] = med
            left = [p for p in node["ports"] if not is_clkrst(p["name"])
                    and (is_in(p["dir"]) or is_bi(p["dir"]))]
            right = [p for p in node["ports"]
                     if not is_clkrst(p["name"]) and is_out(p["dir"])]
            clk = [p for p in node["ports"] if is_clkrst(p["name"])]
            # stable sort: ports with a known target row move to match it,
            # unconnected ones keep their relative position
            left.sort(key=lambda p: key.get(p["name"], 1e9))
            right.sort(key=lambda p: key.get(p["name"], 1e9))
            clk.sort(key=lambda p: 0 if _is_reset(p["name"]) else 1)
            node["ports"] = left + right + clk
        align_ports(root)


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
        w = leaf_width(ports)
        return {"path": path, "module": module_name, "kind": "leaf",
                "ports": ports, "w": w, "h": h, "children": [], "childpos": {},
                "nets": {}, "res": m.get("is_resource", False)}

    nets = build_nets(m)
    layer = layer_of(m, nets)
    node_by_name = {}
    child_nodes = []
    for inst in children_insts:
        cnode = layout(ctx, path + "/" + inst["name"], inst_ports(inst),
                       inst["of_module"])
        cnode["inst_name"] = inst["name"]
        node_by_name[inst["name"]] = cnode
        child_nodes.append(cnode)
    # crossing-reduced order within each layer
    ordered = order_layers(m, nets, layer)
    by_layer = {L: [node_by_name[n] for n in names] for L, names in ordered.items()}

    # place children: x by layer, columns vertically centred on a common midline
    col_h = {}
    for li in sorted(by_layer):
        col = by_layer[li]
        col_h[li] = sum(c["h"] for c in col) + V_GAP * (len(col) - 1)
    max_h = max(col_h.values()) if col_h else 0
    x = PORT_GUTTER
    childpos = {}
    content_h = 0
    for li in sorted(by_layer):
        col = by_layer[li]
        colw = max(c["w"] for c in col)
        y = TITLE_H + PAD + (max_h - col_h[li]) / 2.0
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
# absolute coordinates (shared by draw.io + preview), then an orthogonal
# router: A* over a per-container lane grid, with negotiated congestion so two
# different nets never end up drawn on the same track
# ---------------------------------------------------------------------------


def compute_abs(root):
    box_abs = {}
    port_abs = {}

    def walk(node, ox, oy):
        box_abs[node["path"]] = (ox, oy, node["w"], node["h"])
        h, w = node["h"], node["w"]
        left, right = port_sides(node["ports"])
        pm = node.get("port_y")
        for side, ports in (("left", left), ("right", right)):
            n = len(ports)
            if not n:
                continue
            top = TITLE_H + PITCH * 0.6
            span = max(h - top - PAD - BOTTOM_H, (n - 1) * PITCH)
            step = span / max(n - 1, 1)
            for k, p in enumerate(ports):
                if pm and p["name"] in pm:
                    py = oy + pm[p["name"]]
                else:
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


def _net_targets(net):
    srcs = net["drivers"] if net["drivers"] else net["sinks"][:1]
    targets = net["sinks"] if net["sinks"] else net["drivers"][1:]
    return (srcs[0] if srcs else None), targets


def _seg_hits(x0, y0, x1, y1, obstacles):
    """True if the axis-aligned segment crosses any obstacle interior."""
    for (ax0, ay0, ax1, ay1) in obstacles:
        if abs(x0 - x1) < 1e-6:                     # vertical
            if ax0 < x0 < ax1:
                lo, hi = sorted((y0, y1))
                if min(hi, ay1) - max(lo, ay0) > 1e-6:
                    return True
        else:                                       # horizontal
            if ay0 < y0 < ay1:
                lo, hi = sorted((x0, x1))
                if min(hi, ax1) - max(lo, ax0) > 1e-6:
                    return True
    return False


def _edge_key(a, b):
    return (a, b) if a <= b else (b, a)


def _grid_coords(region, boxes, stubs):
    """Routing lanes for one container.

    Lanes closer together than LANE_SEP are collapsed. Port rows land on
    arbitrary fractional coordinates (the aligner solves for them), so an
    un-deduped grid ends up holding lanes a fraction of a pixel apart -- e.g.
    395.13 and 395.42. The router then treats those as two independent tracks
    and lets two different nets 'share' the row without ever registering a
    conflict, while on screen they are drawn as one line. Enforcing a minimum
    lane separation is what makes the per-net ownership test in _astar_grid
    mean what it says.

    Stub coordinates are mandatory (a wire must dock exactly on its port) so
    they are always kept; box edges and the densified in-between lanes are
    optional and only survive if they clear everything already kept.
    """
    rx0, ry0, rx1, ry1 = region

    def build(required, preferred, lo, hi):
        out = sorted(v for v in set(required) if lo - 1 <= v <= hi + 1)

        def add(v):
            if lo - 1 <= v <= hi + 1 and all(abs(v - k) >= LANE_SEP for k in out):
                out.append(v)
                out.sort()
        for v in sorted(set(preferred)):     # hug box edges where there's room
            add(v)
        for a, b in list(zip(out, out[1:])):  # spare lanes for detouring wires
            g = b - a
            if g > 2 * LANE_SEP:
                k = min(6, int(g // LANE_SEP) - 1)
                for i in range(1, k + 1):
                    add(a + g * i / (k + 1))
        return sorted(set(round(v, 2) for v in out))

    xs = build({p[0] for p in stubs} | {rx0, rx1},
               [v for b in boxes for v in (b[0], b[2])], rx0, rx1)
    ys = build({p[1] for p in stubs} | {ry0, ry1},
               [v for b in boxes for v in (b[1], b[3])], ry0, ry1)
    return xs, ys


def _astar_grid(s, t, xs, ys, obstacles, owner, hist, cong, net_name):
    import heapq
    if s[0] not in xs:
        xs = sorted(set(xs) | {s[0]})
    if t[0] not in xs:
        xs = sorted(set(xs) | {t[0]})
    if s[1] not in ys:
        ys = sorted(set(ys) | {s[1]})
    if t[1] not in ys:
        ys = sorted(set(ys) | {t[1]})
    xi = {x: i for i, x in enumerate(xs)}
    yi = {y: i for i, y in enumerate(ys)}
    TURN = 60
    start = (xi[s[0]], yi[s[1]])
    goal = (xi[t[0]], yi[t[1]])
    pq = [(0.0, start[0], start[1], 2, None)]
    prev = {}
    seen = {}
    while pq:
        cost, ix, iy, d, par = heapq.heappop(pq)
        key = (ix, iy, d)
        if key in seen and seen[key] <= cost:
            continue
        seen[key] = cost
        prev[key] = par
        if (ix, iy) == goal:
            # reconstruct
            path = []
            k = key
            while k is not None:
                path.append((xs[k[0]], ys[k[1]]))
                k = prev[k]
            return path[::-1]
        x, y = xs[ix], ys[iy]
        for dx, dy, nd in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
            ni, nj = ix + dx, iy + dy
            if not (0 <= ni < len(xs) and 0 <= nj < len(ys)):
                continue
            nx, ny = xs[ni], ys[nj]
            if _seg_hits(x, y, nx, ny, obstacles):
                continue
            ek = _edge_key((x, y), (nx, ny))
            step = abs(nx - x) + abs(ny - y)
            turn = TURN if (d != 2 and d != nd) else 0
            # sharing a track with the SAME net (a fan-out trunk) is free and
            # encouraged; sharing with a DIFFERENT net is a visual collision
            # (two unrelated wires drawn on top of each other) and must cost
            # more than any plausible detour, so the loser always moves lane.
            claimant = owner.get(ek)
            block = 0.0 if claimant in (None, net_name) else cong
            congc = block + 40.0 * hist.get(ek, 0.0)
            heapq.heappush(pq, (cost + step + turn + congc, ni, nj, nd, key))
    return None


def route_container(node, box_abs, port_abs):
    """Route all nets of a container on a shared grid with negotiated-congestion
    (PathFinder-style) so distinct nets never render on top of each other; a
    net's own fan-out branches may freely share a trunk. Cached."""
    if "routes" in node:
        return node["routes"]
    MARG = 12
    boxes = []
    for c in node["children"]:
        bx, by, bw, bh = box_abs[c["path"]]
        boxes.append((bx - MARG, by - MARG, bx + bw + MARG, by + bh + MARG))
    cx, cy, cw, ch = box_abs[node["path"]]
    region = (cx + 2, cy + 2, cx + cw - 2, cy + ch - 2)

    jobs = []
    for name, net in node["nets"].items():
        if is_clkrst(name):
            continue
        ws = str(net["width"])
        nw = int(ws) if ws.isdigit() else (1 if ws in ("", "1") else 64)
        src, targets = _net_targets(net)
        if src is None:
            continue
        sid = _endpoint_id(node, src)
        if sid not in port_abs:
            continue
        x1, y1, s1 = port_abs[sid]
        sstub = (x1 + _stub_sign(src, s1) * STUB, y1)
        for t in targets:
            tid = _endpoint_id(node, t)
            if tid not in port_abs or tid == sid:
                continue
            x2, y2, s2 = port_abs[tid]
            tstub = (x2 + _stub_sign(t, s2) * STUB, y2)
            jobs.append([sid, tid, (x1, y1), (x2, y2), sstub, tstub, nw, name])

    stubs = [j[4] for j in jobs] + [j[5] for j in jobs]
    xs, ys = _grid_coords(region, boxes, stubs)
    # short nets first: they take direct lanes, long nets negotiate around
    order = sorted(range(len(jobs)),
                   key=lambda i: abs(jobs[i][4][0] - jobs[i][5][0])
                   + abs(jobs[i][4][1] - jobs[i][5][1]))
    hist = {}
    routes = {}
    for it in range(6):
        owner = {}          # edge -> net name that currently holds this track
        routes = {}
        cong = 150.0 + 150.0 * it     # already beats a 2-turn detour (120) on pass 1
        for i in order:
            sid, tid, s, t, sstub, tstub, _nw, net_name = jobs[i]
            path = _astar_grid(sstub, tstub, xs, ys, boxes, owner, hist, cong, net_name)
            if not path:
                path = [sstub, tstub]
            for a, b in zip(path, path[1:]):
                ek = _edge_key(a, b)
                claimant = owner.get(ek)
                if claimant is None:
                    owner[ek] = net_name
                elif claimant != net_name:
                    # forced collision (grid too tight to avoid it this pass) ->
                    # escalate so the next negotiation round routes around it
                    hist[ek] = hist.get(ek, 0.0) + 1.0
            routes[(sid, tid)] = _collapse([s] + path + [t])
    net_of = {(j[0], j[1]): j[7] for j in jobs}
    _resolve_track_collisions(routes, net_of, boxes)
    node["routes"] = routes
    return routes


def _resolve_track_collisions(routes, net_of, boxes, rounds=40):
    """Safety net on top of the negotiated router: nudge any interior segment
    that still ends up collinear with a DIFFERENT net's segment after
    negotiation. Two unrelated wires drawn on the exact same line read as one
    wire / misaligned ports, so this is enforced as a hard invariant rather
    than left to the (occasionally under-converged) congestion cost."""
    keys = list(routes.keys())

    def all_segs(pts):
        # every segment, including the first/last one that docks onto a port
        # stub -- needed so a movable interior segment can be detected as
        # colliding with a straight stub-to-stub net's only segment, even
        # though that segment itself must never move (else the wire would
        # appear detached from its port).
        out = []
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            movable = 0 < i < len(pts) - 2
            if abs(y0 - y1) < 0.5 and abs(x0 - x1) >= 0.5:
                out.append((i, "H", y0, min(x0, x1), max(x0, x1), movable))
            elif abs(x0 - x1) < 0.5 and abs(y0 - y1) >= 0.5:
                out.append((i, "V", x0, min(y0, y1), max(y0, y1), movable))
        return out

    def hits_obstacle(orient, coord, lo, hi):
        if orient == "H":
            return _seg_hits(lo, coord, hi, coord, boxes)
        return _seg_hits(coord, lo, coord, hi, boxes)

    def nudge(pts, idx, orient, lo, hi):
        for delta in (8, -8, 16, -16, 24, -24, 32, -32, 40, -40, 48, -48):
            nc = (pts[idx][1] if orient == "H" else pts[idx][0]) + delta
            if hits_obstacle(orient, nc, lo, hi):
                continue
            if orient == "H":
                pts[idx] = (pts[idx][0], nc)
                pts[idx + 1] = (pts[idx + 1][0], nc)
            else:
                pts[idx] = (nc, pts[idx][1])
                pts[idx + 1] = (nc, pts[idx + 1][1])
            return True
        return False

    # conflicts that neither side can move away from (both segments dock onto
    # ports, or every candidate lane is blocked). Parked so one unfixable pair
    # can't stall the scan before it reaches the fixable ones.
    stuck = set()
    for _ in range(rounds):
        moved = False
        for ia in range(len(keys)):
            ka = keys[ia]
            for idx_a, orient_a, coord_a, lo_a, hi_a, mov_a in all_segs(routes[ka]):
                for ib in range(ia + 1, len(keys)):
                    kb = keys[ib]
                    if net_of.get(ka) == net_of.get(kb):
                        continue
                    for idx_b, orient_b, coord_b, lo_b, hi_b, mov_b in all_segs(routes[kb]):
                        if orient_a != orient_b or abs(coord_a - coord_b) >= LANE_SEP:
                            continue
                        if min(hi_a, hi_b) - max(lo_a, lo_b) <= 0.5:
                            continue
                        tag = (ka, idx_a, kb, idx_b)
                        if tag in stuck:
                            continue
                        # too close to read as two wires -> nudge whichever
                        # side has room (prefer kb, fall back to ka)
                        if mov_b and nudge(routes[kb], idx_b, orient_b, lo_b, hi_b):
                            moved = True
                        elif mov_a and nudge(routes[ka], idx_a, orient_a, lo_a, hi_a):
                            moved = True
                        else:
                            stuck.add(tag)
                            continue
                        break
                    if moved:
                        break
                if moved:
                    break
            if moved:
                break
        if not moved:
            break
    for k in keys:
        routes[k] = _collapse(routes[k])


def _collapse(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) < 0.5 and abs(p[1] - out[-1][1]) < 0.5:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if abs(a[0] - b[0]) < 0.5 and abs(b[0] - p[0]) < 0.5:
                out[-1] = p; continue
            if abs(a[1] - b[1]) < 0.5 and abs(b[1] - p[1]) < 0.5:
                out[-1] = p; continue
        out.append(p)
    return out


def route_avoiding(node, src_ep, dst_ep, box_abs, port_abs):
    routes = route_container(node, box_abs, port_abs)
    return routes.get((_endpoint_id(node, src_ep), _endpoint_id(node, dst_ep)))


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


def emit_box(ctx, node, box_abs, is_top=False):
    """Emit this module as its OWN group (box + only its own ports) at the top
    level, positioned with absolute coordinates. Child modules are emitted the
    same way (parent = root layer), NOT nested inside this box — so every module
    can be moved independently. Visual nesting is preserved purely by geometry.
    """
    fill, stroke = kind_style(node, is_top)
    if is_top:
        fill = TOPFILL
    bid = box_id(node["path"])
    x, y, w, h = box_abs[node["path"]]
    title = node["path"].split("/")[-1]
    sub = node["module"]
    label = f'<b>{escape(title)}</b>' + ("" if title == sub else f'<br><i>{escape(sub)}</i>')
    # container=0 -> the box keeps its ports as children (they move with it) but
    # will not swallow other module groups dropped on top of it
    style = (f"rounded=1;arcSize=4;html=1;whiteSpace=wrap;fillColor={fill};"
             f"strokeColor={stroke};verticalAlign=top;fontSize=14;spacingTop=6;"
             f"fontColor={stroke};shadow=1;container=0;")
    ctx.cells.append(
        f'<mxCell id={quoteattr(bid)} value={quoteattr(label)} '
        f'style={quoteattr(style)} vertex="1" parent="1">'
        f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" '
        f'height="{h:.0f}" as="geometry"/></mxCell>')

    # boundary ports are children of THIS box only (the group = box + its ports)
    left, right = port_sides(node["ports"])
    _emit_ports(ctx, node, bid, left, side="left")
    _emit_ports(ctx, node, bid, right, side="right")

    # child modules: separate top-level groups (emitted after -> drawn on top)
    for c in node["children"]:
        emit_box(ctx, c, box_abs)


def _emit_ports(ctx, node, bid, ports, side):
    h = node["h"]
    n = len(ports)
    if n == 0:
        return
    # spread ports evenly over the vertical face (below the title band)
    top = TITLE_H + PITCH * 0.6
    span = max(h - top - PAD - BOTTOM_H, (n - 1) * PITCH)
    step = span / max(n - 1, 1)
    pm = node.get("port_y")
    for k, p in enumerate(ports):
        yf = (pm[p["name"]] / h) if (pm and p["name"] in pm) else ((top + k * step) / h)
        xf = 0.0 if side == "left" else 1.0
        pid = port_id(node["path"], p["name"])
        lab = escape(p["name"])
        align = "left" if side == "left" else "right"
        clk = is_clkrst(p["name"])
        dot = "#9AA0A6" if clk else STROKE["rtl"]
        edge = "#5F6368" if clk else STROKE["rtl"]
        fcol = "fontColor=#5F6368;" if clk else ""
        st = (f"shape=ellipse;html=1;fillColor={dot};strokeColor={edge};"
              "verticalLabelPosition=top;verticalAlign=bottom;"
              f"labelPosition=center;align={align};fontSize=12;spacing=2;{fcol}"
              f"spacing{'Left' if side=='left' else 'Right'}={LABEL_SPACING};")
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


def _emit_edges(ctx, node, port_abs, box_abs):
    if node["kind"] == "container":
        for name, net in node["nets"].items():
            if is_clkrst(name):
                continue
            wlabel = "" if str(net["width"]) in ("1", "") else f'{net["width"]}'
            is_bus = bool(wlabel)
            src, targets = _net_targets(net)
            if src is None:
                continue
            sid = _endpoint_id(node, src)
            bidir = net["bidir"] and not net["sinks"]
            if bidir:
                col, sw = STROKE["rf"], 1.6
            else:
                col = WIRE_BUS if is_bus else WIRE
                sw = 2.0 if is_bus else 1.2
            arrow = "none" if bidir else "classicThin"
            for t in targets:
                pts = route_avoiding(node, src, t, box_abs, port_abs)
                if not pts:
                    continue
                tid = _endpoint_id(node, t)
                straight = (len(pts) == 2 and abs(pts[0][1] - pts[1][1]) < 0.5)
                if straight:
                    # clear horizontal run -> plain straight line (nicest to read)
                    st = (f"edgeStyle=none;html=1;endArrow={arrow};endSize=7;"
                          f"startArrow=none;strokeColor={col};strokeWidth={sw};"
                          f"fontSize=10;fontColor={col};labelBackgroundColor={LABEL_BG};")
                    geo = '<mxGeometry relative="1" as="geometry"/>'
                else:
                    inner = pts[1:-1]
                    wp = "".join(f'<mxPoint x="{x:.1f}" y="{y:.1f}"/>' for x, y in inner)
                    st = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;"
                          f"endArrow={arrow};endSize=7;startArrow=none;strokeColor={col};"
                          f"strokeWidth={sw};fontSize=10;fontColor={col};"
                          f"labelBackgroundColor={LABEL_BG};jettySize=auto;exitDx=0;exitDy=0;")
                    geo = ('<mxGeometry relative="1" as="geometry">'
                           f'<Array as="points">{wp}</Array></mxGeometry>')
                eid = ctx.nid()
                ctx.edges.append(
                    f'<mxCell id={quoteattr(eid)} value={quoteattr(wlabel)} '
                    f'style={quoteattr(st)} edge="1" parent="1" '
                    f'source={quoteattr(sid)} target={quoteattr(tid)}>'
                    f'{geo}</mxCell>')
    for c in node["children"]:
        _emit_edges(ctx, c, port_abs, box_abs)


# ---------------------------------------------------------------------------
# SVG preview (absolute coords from the same layout tree)
# ---------------------------------------------------------------------------
def render_svg(root, port_abs, box_abs):
    parts = []

    def walk(node, ox, oy, is_top=False):
        x, y, w, h = ox, oy, node["w"], node["h"]
        fill, stroke = kind_style(node, is_top)
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
                     f'rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
        title = node["path"].split("/")[-1]
        parts.append(f'<text x="{x+8:.0f}" y="{y+16:.0f}" font-weight="bold" '
                     f'font-size="14" fill="{stroke}">{escape(title)}</text>')
        if title != node["module"]:
            parts.append(f'<text x="{x+8:.0f}" y="{y+27:.0f}" font-style="italic" '
                         f'font-size="9" fill="#666">{escape(node["module"])}</text>')
        left, right = port_sides(node["ports"])
        for side, ports in (("left", left), ("right", right)):
            for p in ports:
                px, py, _ = port_abs[port_id(node["path"], p["name"])]
                clk = is_clkrst(p["name"])
                dot = "#5F6368" if clk else stroke
                tcol = "#5F6368" if clk else "#333"
                parts.append(f'<circle cx="{px:.0f}" cy="{py:.0f}" r="2.5" fill="{dot}"/>')
                if side == "left":
                    parts.append(f'<text x="{px+LABEL_SPACING:.0f}" y="{py-4:.0f}" font-size="12" '
                                 f'fill="{tcol}">{escape(p["name"])}</text>')
                else:
                    parts.append(f'<text x="{px-LABEL_SPACING:.0f}" y="{py-4:.0f}" font-size="12" '
                                 f'text-anchor="end" fill="{tcol}">{escape(p["name"])}</text>')
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
                    pts = route_avoiding(node, src, t, box_abs, port_abs)
                    if not pts:
                        continue
                    bidir = net["bidir"] and not net["sinks"]
                    wl = "" if str(net["width"]) in ("1", "") else str(net["width"])
                    is_bus = bool(wl)
                    if bidir:
                        col, sw = "#557A60", 1.6
                    else:
                        col = "#38455A" if is_bus else "#556170"
                        sw = 2.0 if is_bus else 1.2
                    poly = " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)
                    parts.append(f'<polyline points="{poly}" fill="none" '
                                 f'stroke="{col}" stroke-width="{sw}" '
                                 f'stroke-linejoin="round" opacity="0.9"/>')
                    if is_bus:
                        mxp = pts[len(pts) // 2]
                        tw = len(wl) * 6 + 6
                        parts.append(f'<rect x="{mxp[0]-tw/2:.0f}" y="{mxp[1]-9:.0f}" '
                                     f'width="{tw:.0f}" height="12" rx="2" fill="#FFFFFF" '
                                     f'opacity="0.85"/>')
                        parts.append(f'<text x="{mxp[0]:.0f}" y="{mxp[1]:.0f}" '
                                     f'font-size="9" fill="{col}" text-anchor="middle">'
                                     f'{escape(wl)}</text>')
        for c in node["children"]:
            draw_edges(c)

    draw_edges(root)
    W = root["w"] + 2 * PAD
    H = root["h"] + 3 * PAD + 132
    parts.append(legend_svg(PAD, root["h"] + 2 * PAD))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" '
            f'height="{H:.0f}" font-family="Helvetica"><rect width="{W:.0f}" '
            f'height="{H:.0f}" fill="white"/>' + "".join(parts) + '</svg>')


# ---------------------------------------------------------------------------
def legend_cells(ctx, x, y):
    """A compact legend rendered as draw.io cells."""
    W, H = 250, 132
    ctx.cells.append(
        f'<mxCell id="LEGEND" value="&lt;b&gt;Legend&lt;/b&gt;" '
        f'style="rounded=1;arcSize=6;html=1;whiteSpace=wrap;fillColor=#FFFFFF;'
        f'strokeColor=#9AA6B4;verticalAlign=top;fontSize=12;spacingTop=6;'
        f'align=left;spacingLeft=10;shadow=1;container=0;" vertex="1" parent="1">'
        f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{W}" height="{H}" as="geometry"/></mxCell>')
    rows = [("line", WIRE_BUS, 2.0, "bus (multi-bit, width labelled)"),
            ("line", WIRE, 1.2, "control (1-bit)"),
            ("line", STROKE["rf"], 1.6, "bidirectional"),
            ("dot", "#9AA0A6", 0, "clk / reset (implicit, not routed)")]
    ry = y + 30
    for kind, col, sw, text in rows:
        eid = ctx.nid()
        if kind == "line":
            st = (f"endArrow=classicThin;endSize=6;html=1;rounded=0;"
                  f"strokeColor={col};strokeWidth={sw};")
            ctx.edges.append(
                f'<mxCell id={quoteattr(eid)} style={quoteattr(st)} edge="1" parent="1">'
                f'<mxGeometry relative="1" as="geometry">'
                f'<mxPoint x="{x+12:.0f}" y="{ry:.0f}" as="sourcePoint"/>'
                f'<mxPoint x="{x+44:.0f}" y="{ry:.0f}" as="targetPoint"/></mxGeometry></mxCell>')
        else:
            ctx.cells.append(
                f'<mxCell id={quoteattr(eid)} style="shape=ellipse;html=1;'
                f'fillColor={col};strokeColor=#5F6368;" vertex="1" parent="1">'
                f'<mxGeometry x="{x+24:.0f}" y="{ry-4:.0f}" width="8" height="8" as="geometry"/></mxCell>')
        lid = ctx.nid()
        ctx.cells.append(
            f'<mxCell id={quoteattr(lid)} value={quoteattr(escape(text))} '
            f'style="text;html=1;align=left;verticalAlign=middle;fontSize=10;" '
            f'vertex="1" parent="1"><mxGeometry x="{x+52:.0f}" y="{ry-9:.0f}" '
            f'width="{W-60}" height="18" as="geometry"/></mxCell>')
        ry += 22
    return H


def legend_svg(x, y):
    W, H = 250, 132
    p = [f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="4" fill="#FFFFFF" '
         f'stroke="#9AA6B4"/>',
         f'<text x="{x+10}" y="{y+18}" font-weight="bold" font-size="12" fill="#333">Legend</text>']
    rows = [("line", "#38455A", 2.0, "bus (multi-bit, width labelled)"),
            ("line", "#556170", 1.2, "control (1-bit)"),
            ("line", "#557A60", 1.6, "bidirectional"),
            ("dot", "#9AA0A6", 0, "clk / reset (implicit, not routed)")]
    ry = y + 34
    for kind, col, sw, text in rows:
        if kind == "line":
            p.append(f'<line x1="{x+12}" y1="{ry}" x2="{x+44}" y2="{ry}" '
                     f'stroke="{col}" stroke-width="{sw}"/>')
        else:
            p.append(f'<circle cx="{x+28}" cy="{ry}" r="4" fill="{col}" stroke="#5F6368"/>')
        p.append(f'<text x="{x+52}" y="{ry+4}" font-size="10" fill="#333">{escape(text)}</text>')
        ry += 22
    return "".join(p)


def optimize_ports(root):
    """Iteratively reorder each module's input/output ports by the vertical
    barycentre of the ports they connect to, so connected ports line up and
    wires need fewer turns. clk/reset stay pinned at the bottom (clock lowest).
    """
    nodes = []

    def collect(n):
        nodes.append(n)
        for c in n["children"]:
            collect(c)
    collect(root)

    # global port adjacency across every net at every level
    adj = {}

    def add_nets(n):
        if n["kind"] == "container":
            for net in n["nets"].values():
                eps = [_endpoint_id(n, e) for e in (net["drivers"] + net["sinks"])]
                for a in eps:
                    for b in eps:
                        if a != b:
                            adj.setdefault(a, set()).add(b)
        for c in n["children"]:
            add_nets(c)
    add_nets(root)

    for _ in range(8):
        _, port_abs = compute_abs(root)

        def bary(node, p):
            pid = port_id(node["path"], p["name"])
            ys = [port_abs[q][1] for q in adj.get(pid, ()) if q in port_abs]
            return sum(ys) / len(ys) if ys else 1e9   # unconnected sink to bottom

        for node in nodes:
            ports = node["ports"]
            leftg = [p for p in ports
                     if not is_clkrst(p["name"]) and (is_in(p["dir"]) or is_bi(p["dir"]))]
            rightg = [p for p in ports if not is_clkrst(p["name"]) and is_out(p["dir"])]
            clkg = [p for p in ports if is_clkrst(p["name"])]
            leftg.sort(key=lambda p: bary(node, p))
            rightg.sort(key=lambda p: bary(node, p))
            clkg.sort(key=lambda p: 0 if _is_reset(p["name"]) else 1)
            node["ports"] = leftg + rightg + clkg


def _build_adj(root):
    adj = {}

    def add(n):
        if n["kind"] == "container":
            for name, net in n["nets"].items():
                if is_clkrst(name):
                    continue
                eps = [_endpoint_id(n, e) for e in (net["drivers"] + net["sinks"])]
                for a in eps:
                    for b in eps:
                        if a != b:
                            adj.setdefault(a, set()).add(b)
        for c in n["children"]:
            add(c)
    add(root)
    return adj


def _assign_monotone(desired, lo, hi, gap):
    """Place ordered items as close as possible (L2-optimal) to their desired
    positions, keeping input order and a minimum gap, inside [lo, hi].
    Unconnected items (desired huge) sink down.

    Isotonic regression (pool-adjacent-violators) on desired[i]-i*gap, the
    standard reduction for fitting a monotone sequence under a minimum-
    separation constraint. A greedy left-to-right clamp is not equivalent: it
    only ever pushes the port that violates the gap, never pulling an earlier
    one back to share the error, so slack further up the row goes unused. PAV
    pools each conflicting run and places it at the group average, the closest
    monotone fit in the least-squares sense.
    """
    n = len(desired)
    if n == 0:
        return []
    d = [v - i * gap for i, v in enumerate(desired)]
    blocks = []  # each: [sum, count, start_index]
    for i, v in enumerate(d):
        blocks.append([v, 1, i])
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s2, c2, _st2 = blocks.pop()
            s1, c1, st1 = blocks.pop()
            blocks.append([s1 + s2, c1 + c2, st1])
    yprime = [0.0] * n
    for s, c, st in blocks:
        avg = s / c
        for k in range(st, st + c):
            yprime[k] = avg
    y = [yprime[i] + i * gap for i in range(n)]

    if y[0] < lo:
        y = [v + (lo - y[0]) for v in y]
    if y[-1] > hi:
        if y[-1] - y[0] > (hi - lo) + 1e-6:            # doesn't fit at all: uniform
            step = (hi - lo) / max(n - 1, 1)
            y = [lo + i * step for i in range(n)]
        else:
            y = [v + (hi - y[-1]) for v in y]
            if y[0] < lo:                              # still doesn't fit: uniform
                step = (hi - lo) / max(n - 1, 1)
                y = [lo + i * step for i in range(n)]
    return y


def _face_desired(node, ports, adj, port_abs, oy):
    """Desired y (relative to the box top) for each port on a face: the mean
    row of the ports it connects to. Interior (own-child) partners win over
    exterior ones, so a box lines up with what it contains. None = unconnected.
    """
    prefix = "PORT::" + node["path"] + "/"
    out = []
    for p in ports:
        pid = port_id(node["path"], p["name"])
        neigh = [q for q in adj.get(pid, ()) if q in port_abs]
        inner = [q for q in neigh if q.startswith(prefix)]
        use = inner if inner else neigh
        ys = [port_abs[q][1] - oy for q in use]
        out.append(sum(ys) / len(ys) if ys else None)
    return out


def grow_faces(root, adj):
    """Raise box heights so each face can actually reach the rows its ports'
    partners sit on.

    Boxes are first sized to the bare minimum (n_ports * PITCH). When the
    modules a box talks to are spread further apart than that, its ports get
    clamped into a face far shorter than the required spread, and no amount of
    port-position solving can straighten those wires — the room simply is not
    there. Growing the box is the only fix; moving it (refine_placement) only
    corrects the mean offset, never the spread.

    Only ever grows, so the outer refinement loop converges."""
    box_abs, port_abs = compute_abs(root)

    def walk(node):
        _ox, oy, _w, h = box_abs[node["path"]]
        left, right = port_sides(node["ports"])
        need = 0.0
        for ports in (left, right):
            aligns = [p for p in ports if not is_clkrst(p["name"])]
            clks = [p for p in ports if is_clkrst(p["name"])]
            if not aligns:
                continue
            des = [v for v in _face_desired(node, aligns, adj, port_abs, oy)
                   if v is not None]
            n = len(aligns)
            span = (max(des) - min(des)) if des else 0.0
            span = max(span, (n - 1) * PITCH)                # minimum packing
            span = min(span, (n - 1) * PORT_PITCH_MAX)       # stay compact
            need = max(need, TITLE_H + PITCH * 0.6 + span
                       + len(clks) * PITCH + PAD + BOTTOM_H)
        if need > h + 0.5:
            node["min_h"] = max(node.get("min_h", 0.0), need)
            node["h"] = max(node["h"], need)
        for c in node["children"]:
            walk(c)
    walk(root)


def align_ports(root):
    """Iteratively set each port's vertical position to the barycentre of the
    ports it connects to, so wires run straight (0 turns) wherever endpoints can
    line up. Uniform fallback when a face can't fit the desired spread."""
    adj = _build_adj(root)
    nodes = []

    def collect(n):
        nodes.append(n)
        for c in n["children"]:
            collect(c)
    collect(root)
    box_abs, _ = compute_abs(root)          # box positions are fixed

    for _ in range(24):
        _, port_abs = compute_abs(root)
        for node in nodes:
            _ox, oy, _w, h = box_abs[node["path"]]
            face_top = TITLE_H + PITCH * 0.6
            face_bot = h - PAD - BOTTOM_H
            left, right = port_sides(node["ports"])
            pm = node.setdefault("port_y", {})
            for ports in (left, right):
                if not ports:
                    continue
                aligns = [p for p in ports if not is_clkrst(p["name"])]
                clks = [p for p in ports if is_clkrst(p["name"])]
                # reserve the bottom band for clk/reset so their (unconnected)
                # placement never pushes the aligned data ports off their targets
                n_clk = len(clks)
                a_bot = face_bot - (n_clk * PITCH if n_clk else 0)

                # unconnected ports sink to the bottom of the face
                desired = [1e9 if v is None else v
                           for v in _face_desired(node, aligns, adj, port_abs, oy)]
                ass = _assign_monotone(desired, face_top, a_bot, PITCH)
                for p, yv in zip(aligns, ass):
                    pm[p["name"]] = yv
                # clk/reset pinned at the very bottom (reset above, clock lowest)
                for k, p in enumerate(clks):
                    pm[p["name"]] = face_bot - (n_clk - 1 - k) * PITCH


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
    ap.add_argument("--no-svg", action="store_true",
                    help="do not write the .svg preview")
    args = ap.parse_args()

    doc = json.load(open(args.json))
    ctx = Ctx(doc["modules"])
    top = find_top(ctx)
    if top is None:
        raise SystemExit("no non-testbench top module found in netlist")
    # convenience: if -o points at a directory (or ends with a separator),
    # write <dir>/<top>.drawio inside it instead of erroring
    import os
    out = args.out
    if out.endswith(("/", os.sep)) or os.path.isdir(out):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, f"{top}.drawio")
    else:
        d = os.path.dirname(out)
        if d:
            os.makedirs(d, exist_ok=True)
    args.out = out
    # default the preview next to the .drawio unless suppressed
    if args.svg is None and not args.no_svg:
        args.svg = os.path.splitext(out)[0] + ".svg"

    top_ports = [{"name": p["name"], "dir": p["direction"],
                  "connection": p["name"]} for p in ctx.M[top].get("ports", [])]
    root = layout(ctx, top, top_ports, top)
    optimize_ports(root)
    align_ports(root)
    refine_placement(root, iters=8)
    for _ in range(3):               # let order + placement co-adapt
        harmonize_orders(root, iters=2)
        refine_placement(root, iters=4)
    harmonize_orders(root, iters=3)
    # One growth pass, on converged geometry, to hand clamped faces the room
    # they need to line up. Deliberately single-shot: a face's desired spread
    # is derived from where its partners' ports sit, so re-growing on already
    # grown geometry feeds back on itself (box grows -> its ports spread ->
    # partners' desired spread grows -> they grow) and diverges. Measured:
    # one round 29/37 wires straight, two rounds 27/37 and a taller canvas.
    grow_faces(root, _build_adj(root))
    refine_placement(root, iters=4)
    harmonize_orders(root, iters=2)
    refine_placement(root, iters=4)

    box_abs, port_abs = compute_abs(root)
    emit_box(ctx, root, box_abs, is_top=True)
    _emit_edges(ctx, root, port_abs, box_abs)
    leg_h = legend_cells(ctx, PAD, root["h"] + 2 * PAD)

    body = "".join(ctx.cells) + "".join(ctx.edges)
    W = root["w"] + 2 * PAD
    H = root["h"] + 3 * PAD + leg_h
    model = (f'<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" '
             f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
             f'pageWidth="{W:.0f}" pageHeight="{H:.0f}" math="0" shadow="0">'
             f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
             f'</mxGraphModel>')
    xml = f'<mxfile host="icglue-schematic"><diagram name={quoteattr(top)}>{model}</diagram></mxfile>'
    open(args.out, "w").write(xml)
    print(f"wrote {args.out}  ({W:.0f}x{H:.0f})")
    if args.svg:
        open(args.svg, "w").write(render_svg(root, port_abs, box_abs))
        print(f"wrote {args.svg}")


if __name__ == "__main__":
    main()