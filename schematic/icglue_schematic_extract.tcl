#!/usr/bin/env tclsh
#
# icglue_schematic_extract.tcl
# -----------------------------
# Stage 1 of the icglue -> draw.io schematic flow.
#
# Loads the ICGlue package, runs a user construction script (.tcl / .sng / .icng),
# then walks the populated design database via the public ig::db::* read API and
# emits a neutral JSON netlist on stdout (or to -o FILE).
#
# The JSON is deliberately tool-agnostic: Stage 2 (Python) turns it into a
# draw.io diagram, but any renderer could consume it.
#
# Usage:
#   tclsh icglue_schematic_extract.tcl [-o out.json] CONSTRUCT_FILE
#
# Environment:
#   Requires the ICGlue package to be on the Tcl auto_path (same as bin/icglue).

set icglue_silent_load "true"
package require Tcl 8.6
package require ICGlue

# ---------------------------------------------------------------------------
# tiny JSON emitter (no external deps; values are escaped strings or raw)
# ---------------------------------------------------------------------------
namespace eval json {
    proc str {s} {
        set map [list \\ \\\\ \" \\\" \n \\n \r \\r \t \\t]
        return "\"[string map $map $s]\""
    }
    # emit a dict given as {key rawvalue key rawvalue ...} where rawvalue is
    # already JSON-encoded
    proc obj {pairs} {
        set items {}
        foreach {k v} $pairs { lappend items "[str $k]:$v" }
        return "{[join $items ,]}"
    }
    proc arr {items} { return "\[[join $items ,]\]" }
    proc bool {b} { return [expr {$b ? "true" : "false"}] }
    proc num {n} { if {$n eq ""} {return "null"} ; return $n }
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
proc attr {obj name {default {}}} {
    return [ig::db::get_attribute -object $obj -attribute $name -default $default]
}

# direction of a named port inside a module ("input"/"output"/"bidirectional")
# used to decide which side of an instance box a pin sits on
proc port_dir_of {module_id port_name} {
    foreach p [ig::db::get_ports -of $module_id] {
        if {[attr $p name] eq $port_name} { return [attr $p direction] }
    }
    return "unknown"
}

proc module_to_json {mod} {
    set name     [attr $mod name]
    set resource [attr $mod resource false]
    set is_res [string is true -strict $resource]
    set mode     [attr $mod mode rtl]
    set lang     [attr $mod language]

    # ---- ports (module boundary) ----
    set ports {}
    set port_ids {}
    catch {set port_ids [ig::db::get_ports -of $mod]}
    foreach p $port_ids {
        lappend ports [json::obj [list \
            name      [json::str [attr $p name]] \
            direction [json::str [attr $p direction]] \
            size      [json::str [attr $p size 1]] \
        ]]
    }

    # ---- declarations (internal wires) ----
    set decls {}
    set decl_ids {}
    catch {set decl_ids [ig::db::get_declarations -of $mod]}
    foreach d $decl_ids {
        lappend decls [json::obj [list \
            name [json::str [attr $d name]] \
            size [json::str [attr $d size 1]] \
        ]]
    }

    # ---- child instances + pins ----
    set insts {}
    set inst_ids {}
    if {!$is_res} { catch {set inst_ids [ig::db::get_instances -of $mod]} }
    foreach inst $inst_ids {
        set submod   [ig::db::get_modules -of $inst]
        set sub_name [attr $submod name]

        set pins {}
        foreach pin [ig::db::get_pins -of $inst] {
            set pname [attr $pin name]
            set conn  [ig::aux::adapt_pin_connection $pin]
            set inv   [attr $pin invert false]
            lappend pins [json::obj [list \
                name       [json::str $pname] \
                connection [json::str $conn] \
                direction  [json::str [port_dir_of $submod $pname]] \
                invert     [json::bool [string is true -strict $inv]] \
            ]]
        }

        lappend insts [json::obj [list \
            name      [json::str [attr $inst name]] \
            of_module [json::str $sub_name] \
            is_ilm    [json::bool [string is true -strict [attr $submod ilm false]]] \
            is_res    [json::bool [string is true -strict [attr $submod resource false]]] \
            pins      [json::arr $pins] \
        ]]
    }

    return [json::obj [list \
        name        [json::str $name] \
        mode        [json::str $mode] \
        language    [json::str $lang] \
        is_resource [json::bool [string is true -strict $resource]] \
        is_ilm      [json::bool [string is true -strict [attr $mod ilm false]]] \
        ports       [json::arr $ports] \
        declarations [json::arr $decls] \
        instances   [json::arr $insts] \
    ]]
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
proc main {argv} {
    set outfile "-"
    set infile  ""
    for {set i 0} {$i < [llength $argv]} {incr i} {
        set a [lindex $argv $i]
        switch -glob -- $a {
            -o      { incr i ; set outfile [lindex $argv $i] }
            -o=*    { set outfile [string range $a 3 end] }
            default { set infile $a }
        }
    }
    if {$infile eq ""} {
        puts stderr "usage: icglue_schematic_extract.tcl \[-o out.json] CONSTRUCT_FILE"
        exit 1
    }

    # icglue validates output types against a loaded template set when modules
    # are created, so load one (the construct DSL needs it even though we never
    # emit Verilog here). Mirror bin/icglue's template-dir discovery.
    set tdirs {}
    if {[info exists ::env(ICGLUE_TEMPLATE_PATH)]} {
        foreach d [split $::env(ICGLUE_TEMPLATE_PATH) ":"] { lappend tdirs $d }
    }
    set tname "default"
    if {[info exists ::env(ICGLUE_TEMPLATE)]} { set tname $::env(ICGLUE_TEMPLATE) }
    foreach d $tdirs {
        if {[file isdirectory $d]} { ig::templates::add_template_dir $d }
    }
    ig::templates::load_template $tname

    # run the construction script (mirror bin/icglue dispatch)
    if {[regexp {\.(ic)?sng$} $infile]} {
        ig::sng::evaluate_file $infile
    } else {
        ig::construct::run_script $infile {}
    }

    # resolve signal sizes for non-resource modules (as bin/icglue does)
    set mods [ig::db::get_modules -all]
    foreach m $mods {
        if {[attr $m dummy false]} { continue }
        if {![string is true -strict [attr $m resource false]]} {
            ig::aux::adapt_signal_sizes $m
        }
    }

    # emit only modules that actually have children (they are the schematics);
    # resource/leaf modules show up as instance boxes inside their parents.
    set mod_jsons {}
    foreach m $mods {
        if {[attr $m dummy false]} { continue }
        lappend mod_jsons [module_to_json $m]
    }

    set doc [json::obj [list \
        generator [json::str "icglue_schematic_extract"] \
        modules   [json::arr $mod_jsons] \
    ]]

    if {$outfile eq "-"} {
        puts $doc
    } else {
        set fh [open $outfile w]
        puts $fh $doc
        close $fh
    }
}

main $argv
