#!/usr/bin/env python3
"""
verilog_block_drawio.py
-----------------------
Read a Verilog / SystemVerilog module header (the port list) straight from an
.v / .sv file and render it as a single draw.io block, styled to match the
schematics produced by icglue_hier_drawio.py.

No icglue, no netlist: this is for the common case of "I have an RTL file, I
want a block diagram of its interface".

  python3 verilog_block_drawio.py rtl/obi_read_udma.sv -o OUTDIR/
  python3 verilog_block_drawio.py rtl/*.sv -o OUTDIR/        # one file per module

What it draws
  * one rounded module box, same palette / shadow / fonts as the icglue tool
  * inputs on the left, outputs on the right, inouts on the left (marked)
  * clk / reset pinned to the bottom-left, greyed -- same convention
  * bus ports drawn with a heavier stub and labelled with their width
    ([DATA_WIDTH-1:0] etc. is kept verbatim -- parameter expressions are not
    evaluated, because the symbolic width is what you want to read)
  * `// ---- section ----` comments in the port list are used only to space
    the port groups apart; the comment text itself is not drawn
  * the parameter list is rendered as a small note attached to the box
  * a legend, matching the icglue schematics

Parsing notes
  * ANSI-style headers (direction+type in the port list) are supported, which
    is what any modern SV uses. Old-style (`module m(a,b); input a;`) is also
    handled by scanning the body for direction declarations.
  * `logic`, `wire`, `reg`, `bit`, signed/unsigned, packed dimensions, and
    multiple ports per declaration (`input logic a, b, c;`) are handled.
  * comments are used for grouping only; their text is never drawn.
"""

import argparse
import os
import re
import sys
from xml.sax.saxutils import escape, quoteattr

# ---- reuse the icglue schematic styling so both tools look identical -------
TITLE_H = 26
PITCH = 30
LABEL_SPACING = 14
PAD = 22
LEAF_W = 172
CHAR_W = 6.2

FILL = {"rtl": "#EDF1F6", "res": "#F5F0E8", "rf": "#ECF2ED"}
STROKE = {"rtl": "#41617F", "res": "#9A7A45", "rf": "#557A60"}
WIRE = "#556170"
WIRE_BUS = "#38455A"
BIDIR = "#557A60"
CLK_DOT = "#9AA0A6"
CLK_TEXT = "#5F6368"
NOTE_FILL = "#FFFFFF"
NOTE_STROKE = "#9AA6B4"

SECTION_GAP = 16          # blank space between commented port groups


# ---------------------------------------------------------------------------
# stripping / tokenising
# ---------------------------------------------------------------------------
def _strip_block_comments(src):
    """Remove /* ... */ but keep line comments (we still want banners)."""
    return re.sub(r"/\*.*?\*/", " ", src, flags=re.S)


def _is_section_banner(comment):
    """A `// ---- foo ----` / `// === foo ===` style separator introduces a
    logical group of ports. Returns a concise title, or None."""
    c = comment.strip()
    if not re.match(r"^[-=*#\s]*\S", c):
        return None
    # needs a run of separator characters to count as a banner, not a note
    if not re.search(r"[-=*#]{3,}", c):
        return None
    title = re.sub(r"[-=*#]+", " ", c).strip()
    if not title:
        return None
    # banners often carry a parenthetical / after-colon aside; the head is the
    # label, the rest is prose that would swamp the diagram
    title = re.split(r"\s*[(:;]", title, maxsplit=1)[0].strip()
    if len(title) > 42:
        title = title[:39].rstrip() + "..."
    return title or None


DIR_RE = re.compile(r"\b(input|output|inout)\b")
TYPE_RE = re.compile(r"\b(logic|wire|reg|bit|integer|byte|shortint|int|longint)\b")
SIGN_RE = re.compile(r"\b(signed|unsigned)\b")
RANGE_RE = re.compile(r"\[[^\]]*\]")


def _clean_name(tok):
    tok = tok.strip()
    tok = re.sub(r"=.*$", "", tok)          # default value
    tok = RANGE_RE.sub("", tok)             # unpacked dimension
    tok = tok.strip().strip(",;")
    m = re.search(r"([A-Za-z_]\w*)\s*$", tok)
    return m.group(1) if m else ""


def parse_module(src, want=None):
    """Parse the first (or named) module header. Returns a dict with name,
    params [(name, value, comment)], and ports [(name, dir, width, comment,
    section)]."""
    src = _strip_block_comments(src)

    # locate `module <name> [#(params)] (ports);`
    mre = re.compile(r"\bmodule\s+([A-Za-z_]\w*)", re.M)
    mods = list(mre.finditer(src))
    if not mods:
        return None
    m = mods[0]
    if want:
        for cand in mods:
            if cand.group(1) == want:
                m = cand
                break
    name = m.group(1)

    i = m.end()
    params_txt = ""
    # optional parameter block  #( ... )
    j = src.find("(", i)
    hashpos = src.find("#", i)
    if hashpos != -1 and (j == -1 or hashpos < j):
        k = src.find("(", hashpos)
        depth, e = 0, k
        while e < len(src):
            if src[e] == "(":
                depth += 1
            elif src[e] == ")":
                depth -= 1
                if depth == 0:
                    break
            e += 1
        params_txt = src[k + 1:e]
        i = e + 1

    # port list ( ... ) ;
    k = src.find("(", i)
    if k == -1:
        return None
    depth, e = 0, k
    while e < len(src):
        if src[e] == "(":
            depth += 1
        elif src[e] == ")":
            depth -= 1
            if depth == 0:
                break
        e += 1
    ports_txt = src[k + 1:e]
    body = src[e:]

    params = _parse_params(params_txt)
    ports = _parse_ports(ports_txt)
    if not any(p["dir"] for p in ports):
        ports = _parse_old_style(ports, body)
    ports = [p for p in ports if p["name"]]
    return {"name": name, "params": params, "ports": ports}


def _parse_params(txt):
    # strip line comments BEFORE splitting on commas -- a comma inside a param
    # comment would otherwise break the split (same class of bug as ports)
    stream = "\n".join(re.sub(r"//.*$", "", ln) for ln in txt.split("\n"))
    out = []
    for chunk in stream.split(","):
        code = " ".join(chunk.split())
        if not code:
            continue
        code = re.sub(r"\bparameter\b|\blocalparam\b", "", code)
        # `parameter type T = logic` -- the type keyword is not the name
        code = re.sub(r"^\s*type\b", "", code)
        code = TYPE_RE.sub("", code)
        code = SIGN_RE.sub("", code)        # int unsigned WIDTH = 8
        code = RANGE_RE.sub("", code)       # parameter [7:0] P = 3
        mm = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", code, re.S)
        if mm:
            out.append({"name": mm.group(1),
                        "value": " ".join(mm.group(2).split()),
                        "comment": ""})
        else:                                # no default value
            nm = re.match(r"\s*([A-Za-z_]\w*)\s*$", code)
            if nm:
                out.append({"name": nm.group(1), "value": "?", "comment": ""})
    return out


def _parse_ports(txt):
    """ANSI-style port list. Direction/type/width persist across ports that
    omit them (`input logic a, b` -> b is also an input logic).

    Comments are stripped line-by-line *before* the list is split on commas --
    otherwise a comma inside a comment (e.g. `// data in, from the drain`)
    would break the split, and stray words like "output" inside comment prose
    would be mis-read as a port direction. Section banners are captured first
    so the grouping gaps survive the strip."""
    # pass 1: walk lines, note the active section for each following port, and
    # build a comment-free code stream that still has real commas as the only
    # port separators. A sentinel \x00 records "a new section starts here".
    clean_lines = []
    for line in txt.split("\n"):
        cm = re.search(r"//(.*)$", line)
        code = line
        if cm:
            body = cm.group(1)
            code = line[:cm.start()]
            banner = _is_section_banner(body)
            # a banner is a comment-only line (no port code before the //)
            if banner and not code.strip():
                clean_lines.append("\x00")     # section break marker
        if code.strip():
            clean_lines.append(code)
    stream = "\n".join(clean_lines)

    ports = []
    section_pending = False
    section_id = 0
    cur_dir = cur_w = ""
    # split into "segments" separated by real commas OR section markers, so a
    # marker that falls between two ports is preserved as a break
    for raw in re.split(r"(,|\x00)", stream):
        if raw == "\x00":
            section_pending = True
            continue
        if raw == ",":
            continue
        code = " ".join(raw.split())
        if not code:
            continue
        d = DIR_RE.search(code)
        if d:
            cur_dir = d.group(1)
            code_after = code[d.end():]
        else:
            code_after = code
        rest = TYPE_RE.sub("", code_after)
        rest = SIGN_RE.sub("", rest)
        rngs = RANGE_RE.findall(rest)
        if d:                                # new declaration: reset the width
            cur_w = rngs[0] if rngs else ""
        elif rngs:
            cur_w = rngs[0]
        nm = _clean_name(rest)
        if not nm:
            continue
        # the section marker attaches to the next real port that appears
        if section_pending:
            section_id += 1
            section_pending = False
        ports.append({"name": nm, "dir": cur_dir, "width": cur_w,
                      "comment": "", "section": str(section_id)})
    return ports


def _parse_old_style(ports, body):
    """Non-ANSI header: directions are declared in the body."""
    decl = {}
    for mm in re.finditer(r"\b(input|output|inout)\b([^;]*);", body):
        d = mm.group(1)
        rest = TYPE_RE.sub("", mm.group(2))
        rest = SIGN_RE.sub("", rest)
        rngs = RANGE_RE.findall(rest)
        w = rngs[0] if rngs else ""
        for tok in rest.split(","):
            nm = _clean_name(tok)
            if nm:
                decl[nm] = (d, w)
    for p in ports:
        if p["name"] in decl:
            p["dir"], p["width"] = decl[p["name"]]
    return ports


# ---------------------------------------------------------------------------
# layout + emit  (matches icglue_hier_drawio.py conventions)
# ---------------------------------------------------------------------------
def is_clkrst(name):
    n = name.lower()
    # clk/clock and rst/reset, each allowing an optional active-low 'n'
    # (clk_i, rst_n, resetn, rstn_i) and an optional async 'a' prefix on rst
    # (arst_n, arstn). The (^|_) prefix and (_|$|digit) suffix keep substrings
    # inside unrelated words (first, burst, restore) from matching.
    return bool(re.search(r"(^|_)(clk|clock|a?rst|reset)n?(_|$|[0-9])", n))


def _is_reset(name):
    return bool(re.search(r"(^|_)(a?rst|reset)n?(_|$|[0-9])", name.lower()))


def _bit_count(rng):
    """Turn a Verilog `[msb:lsb]` packed range into the bit-count expression
    icglue uses (e.g. `N*DATA_WIDTH`), instead of a `[hi:lo]` slice.

      [DATA_WIDTH-1:0]     -> DATA_WIDTH
      [N*DATA_WIDTH-1:0]   -> N*DATA_WIDTH
      [DATA_WIDTH/8-1:0]   -> DATA_WIDTH/8
      [7:0]                -> 8
      [1:0]                -> 2
      [ADDR_WIDTH-1:0]     -> ADDR_WIDTH

    Only the common `[<hi>:0]` form is simplified symbolically; anything more
    exotic falls back to `hi-lo+1` (numeric) or the raw slice text."""
    inner = rng.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    if ":" not in inner:
        return inner.strip()
    hi, lo = (s.strip() for s in inner.split(":", 1))
    if lo == "0":
        # [<expr>-1 : 0]  ->  <expr>
        m = re.match(r"^(.*\S)\s*-\s*1$", hi)
        if m:
            return m.group(1).strip()
        # [<N> : 0]  ->  N+1  (numeric only; symbolic stays explicit)
        if hi.isdigit():
            return str(int(hi) + 1)
    # both numeric: compute hi-lo+1
    if hi.lstrip("-").isdigit() and lo.lstrip("-").isdigit():
        return str(int(hi) - int(lo) + 1)
    # symbolic non-zero lsb: keep the slice so no information is lost
    return f"{hi}:{lo}"


def is_bus(p):
    """A width like [7:0] / [DATA_WIDTH-1:0] means multi-bit."""
    w = p["width"]
    if not w:
        return False
    inner = w.strip()[1:-1]
    return inner.strip() not in ("0", "0:0")


def width_label(p):
    if not is_bus(p):
        return ""
    return _bit_count(p["width"])


def sides(ports):
    """left = inputs + inouts (clk/reset last, clock lowest); right = outputs."""
    left = [p for p in ports
            if not is_clkrst(p["name"]) and p["dir"] in ("input", "inout")]
    right = [p for p in ports
             if not is_clkrst(p["name"]) and p["dir"] == "output"]
    clk = [p for p in ports if is_clkrst(p["name"])]
    clk.sort(key=lambda p: 0 if _is_reset(p["name"]) else 1)
    return left, right, clk


NAME_CHAR_W = 8.3       # approx char width at the 16px port-name font


def _label_w(ports):
    if not ports:
        return 26
    # the port name is drawn at 16px; size the label zone to it (the small
    # bus-width suffix rides in the same zone and is never the limiting factor)
    return max(len(p["name"]) for p in ports) * NAME_CHAR_W + 30


def _place(ports, y0):
    """Assign a y to each port. Where the RTL's port list starts a new
    commented group, leave a small gap so the groups stay visually separated --
    but do not draw the comment text itself (it's prose, not a diagram label).
    Returns (positions, y_end)."""
    ys = {}
    y = y0
    last = None
    for p in ports:
        s = p.get("section") or ""
        if last is not None and s != last:
            y += SECTION_GAP        # blank space only, no banner
        last = s
        ys[p["name"]] = y
        y += PITCH
    return ys, y


def build(mod):
    ports = mod["ports"]
    left, right, clk = sides(ports)

    lw = _label_w(left + clk)
    rw = _label_w(right)
    w = max(LEAF_W, lw + rw + 40)
    w = min(w, 900)

    y0 = TITLE_H + PITCH * 0.8
    ly, lend = _place(left, y0)
    ry, rend = _place(right, y0)
    body_end = max(lend, rend)

    # clk/reset band at the very bottom
    clk_y = {}
    y = body_end + 14
    for p in clk:
        clk_y[p["name"]] = y
        y += PITCH
    h = max(y + 10, body_end + 20)

    return {"w": w, "h": h, "left": left, "right": right, "clk": clk,
            "ly": ly, "ry": ry, "clk_y": clk_y}


def emit_drawio(mod, L, out):
    cells, edges = [], []
    bx, by = PAD, PAD
    w, h = L["w"], L["h"]
    fill, stroke = FILL["rtl"], STROKE["rtl"]

    label = f'<b>{escape(mod["name"])}</b>'
    style = (f"rounded=1;arcSize=4;html=1;whiteSpace=wrap;fillColor={fill};"
             f"strokeColor={stroke};verticalAlign=top;fontSize=16;spacingTop=6;"
             f"fontColor={stroke};shadow=1;container=0;")
    bid = f"BOX::{mod['name']}"
    cells.append(
        f'<mxCell id={quoteattr(bid)} value={quoteattr(label)} '
        f'style={quoteattr(style)} vertex="1" parent="1">'
        f'<mxGeometry x="{bx}" y="{by}" width="{w:.0f}" height="{h:.0f}" '
        f'as="geometry"/></mxCell>')

    def port_cells(plist, ymap, side):
        for p in plist:
            yy = ymap[p["name"]]
            xf = 0.0 if side == "left" else 1.0
            clk = is_clkrst(p["name"])
            dot = CLK_DOT if clk else stroke
            edge = CLK_TEXT if clk else stroke
            fcol = f"fontColor={CLK_TEXT};" if clk else ""
            align = "left" if side == "left" else "right"
            lab = p["name"]
            st = (f"shape=ellipse;html=1;fillColor={dot};strokeColor={edge};"
                  "verticalLabelPosition=top;verticalAlign=bottom;"
                  f"labelPosition=center;align={align};fontSize=16;spacing=2;{fcol}"
                  f"spacing{'Left' if side == 'left' else 'Right'}={LABEL_SPACING};")
            pid = f"PORT::{mod['name']}::{p['name']}"
            cells.append(
                f'<mxCell id={quoteattr(pid)} value={quoteattr(lab)} '
                f'style={quoteattr(st)} vertex="1" parent={quoteattr(bid)}>'
                f'<mxGeometry x="{xf}" y="{yy / h:.4f}" width="8" height="8" '
                f'relative="1" as="geometry"><mxPoint x="-4" y="-4" as="offset"/>'
                f'</mxGeometry></mxCell>')
            # the stub outside the box, heavier + width-labelled for a bus
            if clk:
                continue
            bus = is_bus(p)
            col = WIRE_BUS if bus else WIRE
            sw = 2.0 if bus else 1.2
            if p["dir"] == "inout":
                col, sw = BIDIR, 1.6
            x0 = bx if side == "left" else bx + w
            x1 = x0 - 56 if side == "left" else x0 + 56
            arrow = "none" if p["dir"] == "inout" else "classicThin"
            # arrow points INTO the box for inputs, OUT of it for outputs
            sp, tp = ((x1, by + yy), (x0, by + yy)) if side == "left" \
                else ((x0, by + yy), (x1, by + yy))
            est = (f"edgeStyle=none;html=1;endArrow={arrow};endSize=7;"
                   f"startArrow=none;strokeColor={col};strokeWidth={sw};"
                   f"fontSize=10;fontColor={col};labelBackgroundColor=#FFFFFF;")
            edges.append(
                f'<mxCell id={quoteattr("STUB::" + p["name"])} '
                f'value={quoteattr(width_label(p))} '
                f'style={quoteattr(est)} edge="1" parent="1">'
                f'<mxGeometry relative="1" as="geometry">'
                f'<mxPoint x="{sp[0]:.0f}" y="{sp[1]:.0f}" as="sourcePoint"/>'
                f'<mxPoint x="{tp[0]:.0f}" y="{tp[1]:.0f}" as="targetPoint"/>'
                f'</mxGeometry></mxCell>')

    port_cells(L["left"], L["ly"], "left")
    port_cells(L["right"], L["ry"], "right")
    port_cells(L["clk"], L["clk_y"], "left")

    # parameter note beside the box
    note_h = 0
    if mod["params"]:
        rows = "".join(
            f'{escape(p["name"])} = {escape(p["value"])}<br>'
            for p in mod["params"])
        note_h = 26 + 16 * len(mod["params"])
        nw = max(150, max(len(f'{p["name"]} = {p["value"]}')
                          for p in mod["params"]) * 6.4 + 24)
        cells.append(
            f'<mxCell id="PARAMS" value={quoteattr("<b>parameters</b><br>" + rows)} '
            f'style="rounded=1;arcSize=6;html=1;whiteSpace=wrap;'
            f'fillColor={NOTE_FILL};strokeColor={NOTE_STROKE};align=left;'
            f'verticalAlign=top;fontSize=10;spacingLeft=8;spacingTop=4;'
            f'dashed=1;shadow=0;" vertex="1" parent="1">'
            f'<mxGeometry x="{bx + w + 90:.0f}" y="{by}" width="{nw:.0f}" '
            f'height="{note_h}" as="geometry"/></mxCell>')

    leg_y = by + h + 30
    leg_h = _legend(cells, edges, bx, leg_y)

    W = bx + w + 90 + 240 + PAD
    H = leg_y + leg_h + PAD
    body = "".join(cells) + "".join(edges)
    model = (f'<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" '
             f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
             f'pageWidth="{W:.0f}" pageHeight="{H:.0f}" math="0" shadow="0">'
             f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
             f'</mxGraphModel>')
    xml = (f'<mxfile host="verilog-block">'
           f'<diagram name={quoteattr(mod["name"])}>{model}</diagram></mxfile>')
    open(out, "w").write(xml)
    return W, H


def _legend(cells, edges, x, y):
    W, H = 250, 108
    cells.append(
        f'<mxCell id="LEGEND" value="&lt;b&gt;Legend&lt;/b&gt;" '
        f'style="rounded=1;arcSize=6;html=1;whiteSpace=wrap;fillColor=#FFFFFF;'
        f'strokeColor=#9AA6B4;verticalAlign=top;fontSize=12;spacingTop=6;'
        f'align=left;spacingLeft=10;shadow=1;container=0;" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{W}" height="{H}" as="geometry"/></mxCell>')
    rows = [("line", WIRE_BUS, 2.0, "bus (width labelled)"),
            ("line", WIRE, 1.2, "control (1-bit)"),
            ("line", BIDIR, 1.6, "inout"),
            ("dot", CLK_DOT, 0, "clk / reset")]
    ry = y + 30
    for i, (kind, col, sw, text) in enumerate(rows):
        if kind == "line":
            st = (f"endArrow=classicThin;endSize=6;html=1;rounded=0;"
                  f"strokeColor={col};strokeWidth={sw};")
            edges.append(
                f'<mxCell id={quoteattr(f"LEG::{i}")} style={quoteattr(st)} '
                f'edge="1" parent="1"><mxGeometry relative="1" as="geometry">'
                f'<mxPoint x="{x + 12}" y="{ry}" as="sourcePoint"/>'
                f'<mxPoint x="{x + 44}" y="{ry}" as="targetPoint"/>'
                f'</mxGeometry></mxCell>')
        else:
            cells.append(
                f'<mxCell id={quoteattr(f"LEG::{i}")} '
                f'style="shape=ellipse;html=1;fillColor={col};'
                f'strokeColor={CLK_TEXT};" vertex="1" parent="1">'
                f'<mxGeometry x="{x + 24}" y="{ry - 4}" width="8" height="8" '
                f'as="geometry"/></mxCell>')
        cells.append(
            f'<mxCell id={quoteattr(f"LEGT::{i}")} value={quoteattr(text)} '
            f'style="text;html=1;align=left;verticalAlign=middle;fontSize=10;" '
            f'vertex="1" parent="1"><mxGeometry x="{x + 52}" y="{ry - 9}" '
            f'width="{W - 60}" height="18" as="geometry"/></mxCell>')
        ry += 20
    return H


# ---------------------------------------------------------------------------
def emit_svg(mod, L, out):
    """Preview SVG mirroring the draw.io output."""
    p = []
    bx, by = PAD, PAD
    w, h = L["w"], L["h"]
    fill, stroke = FILL["rtl"], STROKE["rtl"]
    p.append(f'<rect x="{bx}" y="{by}" width="{w:.0f}" height="{h:.0f}" rx="3" '
             f'fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
    p.append(f'<text x="{bx + 8}" y="{by + 18}" font-weight="bold" font-size="16" '
             f'fill="{stroke}">{escape(mod["name"])}</text>')

    def draw(plist, ymap, side):
        for q in plist:
            yy = by + ymap[q["name"]]
            xx = bx if side == "left" else bx + w
            clk = is_clkrst(q["name"])
            dot = CLK_DOT if clk else stroke
            tcol = CLK_TEXT if clk else "#333"
            if not clk:
                bus = is_bus(q)
                col = WIRE_BUS if bus else WIRE
                sw = 2.0 if bus else 1.2
                if q["dir"] == "inout":
                    col, sw = BIDIR, 1.6
                x1 = xx - 56 if side == "left" else xx + 56
                p.append(f'<line x1="{xx}" y1="{yy:.0f}" x2="{x1}" y2="{yy:.0f}" '
                         f'stroke="{col}" stroke-width="{sw}"/>')
                wl = width_label(q)
                if wl:
                    mx = (xx + x1) / 2
                    p.append(f'<text x="{mx:.0f}" y="{yy - 4:.0f}" font-size="9" '
                             f'fill="{col}" text-anchor="middle">{escape(wl)}</text>')
            p.append(f'<circle cx="{xx}" cy="{yy:.0f}" r="2.5" fill="{dot}"/>')
            if side == "left":
                p.append(f'<text x="{xx + LABEL_SPACING}" y="{yy - 4:.0f}" '
                         f'font-size="16" fill="{tcol}">{escape(q["name"])}</text>')
            else:
                p.append(f'<text x="{xx - LABEL_SPACING}" y="{yy - 4:.0f}" '
                         f'font-size="16" text-anchor="end" '
                         f'fill="{tcol}">{escape(q["name"])}</text>')

    draw(L["left"], L["ly"], "left")
    draw(L["right"], L["ry"], "right")
    draw(L["clk"], L["clk_y"], "left")

    nx = bx + w + 90
    if mod["params"]:
        nh = 26 + 16 * len(mod["params"])
        nw = max(150, max(len(f'{q["name"]} = {q["value"]}')
                          for q in mod["params"]) * 6.4 + 24)
        p.append(f'<rect x="{nx}" y="{by}" width="{nw:.0f}" height="{nh}" rx="4" '
                 f'fill="#FFFFFF" stroke="{NOTE_STROKE}" stroke-dasharray="4 2"/>')
        p.append(f'<text x="{nx + 8}" y="{by + 15}" font-size="10" '
                 f'font-weight="bold" fill="#333">parameters</text>')
        yy = by + 31
        for q in mod["params"]:
            p.append(f'<text x="{nx + 8}" y="{yy}" font-size="10" fill="#444">'
                     f'{escape(q["name"])} = {escape(q["value"])}</text>')
            yy += 16

    W = nx + 260 + PAD
    H = by + h + 30 + 108 + PAD
    # legend
    ly = by + h + 30
    p.append(f'<rect x="{bx}" y="{ly}" width="250" height="108" rx="4" '
             f'fill="#FFFFFF" stroke="#9AA6B4"/>')
    p.append(f'<text x="{bx + 10}" y="{ly + 18}" font-weight="bold" font-size="12" '
             f'fill="#333">Legend</text>')
    rows = [("line", WIRE_BUS, 2.0, "bus (width labelled)"),
            ("line", WIRE, 1.2, "control (1-bit)"),
            ("line", BIDIR, 1.6, "inout"),
            ("dot", CLK_DOT, 0, "clk / reset")]
    ry = ly + 34
    for kind, col, sw, text in rows:
        if kind == "line":
            p.append(f'<line x1="{bx + 12}" y1="{ry}" x2="{bx + 44}" y2="{ry}" '
                     f'stroke="{col}" stroke-width="{sw}"/>')
        else:
            p.append(f'<circle cx="{bx + 28}" cy="{ry}" r="4" fill="{col}" '
                     f'stroke="{CLK_TEXT}"/>')
        p.append(f'<text x="{bx + 52}" y="{ry + 4}" font-size="10" '
                 f'fill="#333">{escape(text)}</text>')
        ry += 20

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" '
           f'height="{H:.0f}" font-family="Helvetica">'
           f'<rect width="{W:.0f}" height="{H:.0f}" fill="white"/>'
           + "".join(p) + '</svg>')
    open(out, "w").write(svg)


def main():
    ap = argparse.ArgumentParser(
        description="Verilog/SystemVerilog module header -> draw.io block diagram")
    ap.add_argument("files", nargs="+", help=".v / .sv source file(s)")
    ap.add_argument("-o", "--out", default=".",
                    help="output file or directory (default: cwd)")
    ap.add_argument("-m", "--module", help="module name, if the file has several")
    ap.add_argument("--no-svg", action="store_true", help="skip the .svg preview")
    args = ap.parse_args()

    outs = []
    for f in args.files:
        src = open(f).read()
        mod = parse_module(src, args.module)
        if not mod:
            print(f"{f}: no module header found", file=sys.stderr)
            continue
        L = build(mod)
        if os.path.isdir(args.out) or args.out.endswith(("/", os.sep)) \
                or len(args.files) > 1:
            os.makedirs(args.out, exist_ok=True)
            out = os.path.join(args.out, f'{mod["name"]}.drawio')
        else:
            out = args.out
            d = os.path.dirname(out)
            if d:
                os.makedirs(d, exist_ok=True)
        W, H = emit_drawio(mod, L, out)
        nl = sum(1 for p in mod["ports"] if p["dir"] == "input")
        nr = sum(1 for p in mod["ports"] if p["dir"] == "output")
        nb = sum(1 for p in mod["ports"] if p["dir"] == "inout")
        print(f'wrote {out}  ({mod["name"]}: {nl} in, {nr} out'
              + (f", {nb} inout" if nb else "") + f", {len(mod['params'])} params)")
        outs.append(out)
        if not args.no_svg:
            svg = os.path.splitext(out)[0] + ".svg"
            emit_svg(mod, L, svg)
            print(f"wrote {svg}")
    return outs


if __name__ == "__main__":
    main()
