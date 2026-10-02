# Copyright 2025 LibreLane Contributors
#
# Adapted from OpenLane
#
# Copyright 2020-2022 Efabless Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ---------------------------------------------------------------------------
# Keyer (tt_um_ahan17x_keyer, branch sram-macro), SPDX-License-Identifier: Apache-2.0
# PDN configuration for the RM_IHPSG13_1P_256x16_c2_bm_bist macro on IHP
# sg13cmos5l (DECISIONS D-025).
#
# Attribution: the body of this file is LibreLane 3.1.0.dev3's default
# scripts/openroad/common/pdn_cfg.tcl with its macro grid replaced by the
# pdngen wrapper and the stripe verifier written by Thomas Gilbert for
# tt_um_loom (https://github.com/thomasgilbert481/tt_um_loom, Apache-2.0,
# src/pdn_cfg.tcl on its main branch; the reasoning is in that repository's
# docs/tt_cmos5l_facts.md, section 11). Changes here: the identifiers are
# prefixed keyer_ / KEYERPDN instead of loom_ / LOOMPDN, and this header
# describes the 256 x 16 macro. The logic is unchanged.
#
# WHY: on cmos5l the block's only stripe layer, Metal4, is also the layer of
# the macro's power pins, and TopMetal1 belongs to tt_top, so LibreLane's
# default macro grid (Metal4 <-> TopMetal1) is empty (PDN-0232) and pdngen
# alone cuts every stripe short of a FIXED macro, leaving it an island. The
# wrapper below marks the hard macros PLACED only while pdngen builds its
# shapes, so the Metal4 stripes run full height through the macro's own
# same-net power columns (every stripe is one full-height power pin, which
# the Tiny Tapeout precheck requires), then verifies that every stripe shape
# over a macro lies inside same-net pins and that every macro supply carries
# a stripe, and runs check_power_grid with errors enabled.
#
# THE STRIPE NUMBERS (src/config.json FP_PDN_*), from
# macro/RM_IHPSG13_1P_256x16_c2_bm_bist/RM_IHPSG13_1P_256x16_c2_bm_bist.lef
# (SIZE 236.8 x 118.78; columns 2.81 um wide, macro-local x):
#   VDD! / VDDARRAY!  4.26 + 11.24k (k = 0..7) and 151.05 + 11.24k (k = 0..7)
#   VSS!              9.88 + 11.24k and 145.43 + 11.24k
#   irregular middle band x 88 .. 151 (four full-height VDD!, five VSS!)
# These are the same x positions as the sibling 512 x 16 macro (same width,
# same bit-slice pitch), so its numbers apply unchanged:
#   VWIDTH 2.1 (Tiny Tapeout's value, the precheck's minimum power-port width),
#   VSPACING 3.52 (GROUND stripe at 3.52 + 2.1 = 5.62 from the POWER stripe,
#   the POWER-to-GROUND column distance), VPITCH 67.44 = 6 x 11.24 (steps
#   over the middle band), VOFFSET 26.36 from the core xMin 2.88, i.e. the
#   first POWER stripe at 29.24 = macro x 12 + 17.24, inside the k = 1 column
#   (15.5 .. 18.31) with 0.02 um to spare; the four crossings at macro-local
#   17.24, 84.68, 152.12, 219.56 (POWER) and 22.86, 90.30, 157.74, 225.18
#   (GROUND) all lie inside same-net columns. Verified by the checker below at
#   run time, so a different macro x fails the PDN step at once.
# Height: VDD! occupies local y 0 .. 38.825 (the signal-pin edge) and the
# full-height middle columns, VDDARRAY! local y 45.465 .. 118.78, with a
# Metal4 OBS across each column in between (39.085 .. 45.205, 6.64 um, under
# keyer_max_pin_gap). FS at (12, 40) puts the macro at die y 40 .. 158.78
# with its 108 Metal2 signal pins on its top edge at y 158.52 .. 158.78,
# facing the rows above. Do not add -snap_to_grid.

source $::env(SCRIPTS_DIR)/openroad/common/io.tcl
source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl
set_global_connections

set secondary []
foreach vdd $::env(VDD_NETS) gnd $::env(GND_NETS) {
    if { $vdd != $::env(VDD_NET)} {
        lappend secondary $vdd

        set db_net [[ord::get_db_block] findNet $vdd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $vdd]
            $net setSpecial
            $net setSigType "POWER"
        }
    }

    if { $gnd != $::env(GND_NET)} {
        lappend secondary $gnd

        set db_net [[ord::get_db_block] findNet $gnd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $gnd]
            $net setSpecial
            $net setSigType "GROUND"
        }
    }
}

set_voltage_domain -name CORE -power $::env(VDD_NET) -ground $::env(GND_NET) \
    -secondary_power $secondary



if { $::env(PDN_MULTILAYER) == 1 } {

    set arg_list [list]
    if { $::env(PDN_ENABLE_PINS) } {
        lappend arg_list -pins "$::env(PDN_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
    }

    define_pdn_grid \
        -name stdcell_grid \
        -starts_with POWER \
        -voltage_domain CORE \
        {*}$arg_list

    set arg_list [list]
    append_if_equals arg_list PDN_EXTEND_TO "core_ring" -extend_to_core_ring
    append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_VERTICAL_LAYER) \
        -width $::env(PDN_VWIDTH) \
        -pitch $::env(PDN_VPITCH) \
        -offset $::env(PDN_VOFFSET) \
        -spacing $::env(PDN_VSPACING) \
        -starts_with POWER \
        {*}$arg_list

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_HORIZONTAL_LAYER) \
        -width $::env(PDN_HWIDTH) \
        -pitch $::env(PDN_HPITCH) \
        -offset $::env(PDN_HOFFSET) \
        -spacing $::env(PDN_HSPACING) \
        -starts_with POWER \
        {*}$arg_list

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
} else {

    set arg_list [list]
    if { $::env(PDN_ENABLE_PINS) } {
        lappend arg_list -pins "$::env(PDN_VERTICAL_LAYER)"
    }

    define_pdn_grid \
        -name stdcell_grid \
        -starts_with POWER \
        -voltage_domain CORE \
        {*}$arg_list

    set arg_list [list]
    append_if_equals arg_list PDN_EXTEND_TO "core_ring" -extend_to_core_ring
    append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_VERTICAL_LAYER) \
        -width $::env(PDN_VWIDTH) \
        -pitch $::env(PDN_VPITCH) \
        -offset $::env(PDN_VOFFSET) \
        -spacing $::env(PDN_VSPACING) \
        -starts_with POWER \
        {*}$arg_list
}

# Adds the standard cell rails if enabled.
if { $::env(PDN_ENABLE_RAILS) == 1 } {
    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_RAIL_LAYER) \
        -width $::env(PDN_RAIL_WIDTH) \
        -followpins

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_RAIL_LAYER) $::env(PDN_VERTICAL_LAYER)"
}


# Adds the core ring if enabled.
if { $::env(PDN_CORE_RING) == 1 } {
    if { $::env(PDN_MULTILAYER) == 1 } {
        set arg_list [list]
        append_if_flag arg_list PDN_CORE_RING_ALLOW_OUT_OF_DIE -allow_out_of_die
        append_if_flag arg_list PDN_CORE_RING_CONNECT_TO_PADS -connect_to_pads
        append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary
        append_if_exists_argument arg_list PDN_CORE_RING_CONNECT_TO_PAD_LAYERS -connect_to_pad_layers

        set pdn_core_vertical_layer $::env(PDN_VERTICAL_LAYER)
        set pdn_core_horizontal_layer $::env(PDN_HORIZONTAL_LAYER)

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] } {
            set pdn_core_vertical_layer $::env(PDN_CORE_VERTICAL_LAYER)
        }

        if { [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            set pdn_core_horizontal_layer $::env(PDN_CORE_HORIZONTAL_LAYER)
        }

        add_pdn_ring \
            -grid stdcell_grid \
            -layers "$pdn_core_vertical_layer $pdn_core_horizontal_layer" \
            -widths "$::env(PDN_CORE_RING_VWIDTH) $::env(PDN_CORE_RING_HWIDTH)" \
            -spacings "$::env(PDN_CORE_RING_VSPACING) $::env(PDN_CORE_RING_HSPACING)" \
            -core_offset "$::env(PDN_CORE_RING_VOFFSET) $::env(PDN_CORE_RING_HOFFSET)" \
            {*}$arg_list

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
        }

        if { [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_HORIZONTAL_LAYER) $::env(PDN_VERTICAL_LAYER)"
        }

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] && [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_VERTICAL_LAYER) $::env(PDN_CORE_HORIZONTAL_LAYER)"
        }

    } else {
        throw APPLICATION "PDN_CORE_RING cannot be used when PDN_MULTILAYER is set to false."
    }
}


# =============================================================================
# Replacement for the default macro grid: full-height stripes through the
# macro's power columns, verified. See the header.
# =============================================================================

# Longest stretch of a macro that a stripe may cross without a same-net pin
# under it, and only between two same-net pins (the VDD!/VDDARRAY! split is
# 6.64 um). Microns.
set ::keyer_max_pin_gap 7.0

proc keyer_check_macro_stripes {} {
    set block [ord::get_db_block]
    set layer_name $::env(PDN_VERTICAL_LAYER)
    set dbu [$block getDbUnitsPerMicron]
    set max_gap [expr {round($::keyer_max_pin_gap * $dbu)}]
    set um [expr {1.0 / $dbu}]

    # --- 1. every power/ground pin rectangle of every hard macro on the stripe
    #        layer, in die coordinates, with the block net it is tied to
    set macros [list]
    set columns [list]
    set counts [dict create]
    foreach inst [$block getInsts] {
        if { ![[$inst getMaster] isBlock] } { continue }
        set iname [$inst getName]
        set orient [$inst getOrient]
        if { $orient ne "R0" && $orient ne "MX" } {
            error "KEYERPDN: $iname has orientation $orient; only R0 (N) and MX (FS) keep the Metal4 power columns vertical"
        }
        set bb [$inst getBBox]
        set bx0 [$bb xMin]
        set by0 [$bb yMin]
        set bx1 [$bb xMax]
        set by1 [$bb yMax]
        lappend macros [list $iname $bx0 $by0 $bx1 $by1]
        puts [format "KEYERPDN macro %s orient %s bbox %.3f %.3f %.3f %.3f status %s" $iname $orient \
            [expr {$bx0 * $um}] [expr {$by0 * $um}] [expr {$bx1 * $um}] [expr {$by1 * $um}] \
            [$inst getPlacementStatus]]
        foreach iterm [$inst getITerms] {
            set mterm [$iterm getMTerm]
            set sig [$mterm getSigType]
            if { $sig ne "POWER" && $sig ne "GROUND" } { continue }
            set key "$iname/[$mterm getName]"
            set net [$iterm getNet]
            if { $net eq "NULL" } {
                error "KEYERPDN: $key is not connected to any net (check PDN_MACRO_CONNECTIONS)"
            }
            dict set counts $key 0
            set ux1 ""
            foreach mpin [$mterm getMPins] {
                foreach box [$mpin getGeometry] {
                    if { [$box isVia] } { continue }
                    if { [[$box getTechLayer] getName] ne $layer_name } { continue }
                    set x1 [expr {$bx0 + [$box xMin]}]
                    set x2 [expr {$bx0 + [$box xMax]}]
                    if { $orient eq "R0" } {
                        set y1 [expr {$by0 + [$box yMin]}]
                        set y2 [expr {$by0 + [$box yMax]}]
                    } else {
                        set y1 [expr {$by1 - [$box yMax]}]
                        set y2 [expr {$by1 - [$box yMin]}]
                    }
                    lappend columns [list $net $key $x1 $y1 $x2 $y2]
                    if { $ux1 eq "" } {
                        lassign [list $x1 $y1 $x2 $y2] ux1 uy1 ux2 uy2
                    } else {
                        set ux1 [expr {min($ux1, $x1)}]
                        set uy1 [expr {min($uy1, $y1)}]
                        set ux2 [expr {max($ux2, $x2)}]
                        set uy2 [expr {max($uy2, $y2)}]
                    }
                }
            }
            # Cross-check the hand-written transform against OpenDB's own.
            set ib [$iterm getBBox]
            if { $ux1 ne "" && ($ux1 != [$ib xMin] || $uy1 != [$ib yMin] || $ux2 != [$ib xMax] || $uy2 != [$ib yMax]) } {
                error "KEYERPDN: transformed pins of $key ($ux1 $uy1 $ux2 $uy2) disagree with the ITerm bbox ([$ib xMin] [$ib yMin] [$ib xMax] [$ib yMax])"
            }
        }
    }
    if { [llength $macros] == 0 } {
        puts "KEYERPDN no hard macros; nothing to check"
        return
    }

    # --- 2. every stripe-layer shape of every net, checked against every macro
    set bad [list]
    foreach net [$block getNets] {
        if { ![$net isSpecial] } { continue }
        foreach swire [$net getSWires] {
            foreach sbox [$swire getWires] {
                if { [$sbox isVia] } { continue }
                if { [[$sbox getTechLayer] getName] ne $layer_name } { continue }
                set sx1 [$sbox xMin]
                set sy1 [$sbox yMin]
                set sx2 [$sbox xMax]
                set sy2 [$sbox yMax]
                foreach m $macros {
                    lassign $m iname mx0 my0 mx1 my1
                    if { $sx2 <= $mx0 || $sx1 >= $mx1 || $sy2 <= $my0 || $sy1 >= $my1 } { continue }
                    set desc [format "%s x %.3f..%.3f y %.3f..%.3f" [$net getName] \
                        [expr {$sx1 * $um}] [expr {$sx2 * $um}] [expr {$sy1 * $um}] [expr {$sy2 * $um}]]
                    set ylo [expr {max($sy1, $my0)}]
                    set yhi [expr {min($sy2, $my1)}]
                    # same-net pin rectangles that contain the stripe's x-range
                    set cover [list]
                    foreach c $columns {
                        lassign $c cnet key px1 py1 px2 py2
                        if { $cnet ne $net } { continue }
                        if { $px1 > $sx1 || $px2 < $sx2 } { continue }
                        if { $py2 <= $ylo || $py1 >= $yhi } { continue }
                        lappend cover [list $py1 $py2 $key]
                    }
                    set cover [lsort -integer -index 0 $cover]
                    set cur $ylo
                    set keys [list]
                    set why ""
                    foreach r $cover {
                        lassign $r py1 py2 key
                        if { $py1 > $cur } {
                            if { $cur == $ylo } {
                                set why [format "no same-net pin under it from y %.3f to %.3f" [expr {$cur * $um}] [expr {$py1 * $um}]]
                                break
                            }
                            if { $py1 - $cur > $max_gap } {
                                set why [format "crosses %.3f um without a same-net pin (y %.3f..%.3f)" \
                                    [expr {($py1 - $cur) * $um}] [expr {$cur * $um}] [expr {$py1 * $um}]]
                                break
                            }
                            puts [format "KEYERPDN   %s crosses a %.3f um gap between same-net pins at y %.3f..%.3f" \
                                $desc [expr {($py1 - $cur) * $um}] [expr {$cur * $um}] [expr {$py1 * $um}]]
                        }
                        set cur [expr {max($cur, $py2)}]
                        lappend keys $key
                    }
                    if { $why eq "" && $cur < $yhi } {
                        set why [format "no same-net pin under it from y %.3f to %.3f" [expr {$cur * $um}] [expr {$yhi * $um}]]
                    }
                    if { $why ne "" } {
                        lappend bad "$desc over $iname: $why"
                        continue
                    }
                    foreach key [lsort -unique $keys] { dict incr counts $key }
                    puts "KEYERPDN stripe $desc runs inside [join [lsort -unique $keys] { + }]"
                }
            }
        }
    }
    if { [llength $bad] > 0 } {
        foreach b $bad { puts "KEYERPDN BAD $b" }
        error "KEYERPDN: [llength $bad] stripe shape(s) over a macro are not inside same-net power pins (would short or float); fix FP_PDN_V* or the macro location"
    }

    # --- 3. every supply pin of every macro needs at least one stripe
    set missing [list]
    dict for {key n} $counts {
        puts "KEYERPDN $key: $n stripe(s)"
        if { $n == 0 } { lappend missing $key }
    }
    if { [llength $missing] > 0 } {
        error "KEYERPDN: no stripe runs through: [join $missing {, }]"
    }
}

# Wrap LibreLane's single `pdngen` call (scripts/openroad/pdn.tcl sources this
# file first, then runs `pdngen`, with -skip_trim only if PDN_SKIPTRIM).
#
# OpenROAD's pdngen proc (src/pdn/src/pdn.tcl at dcf36133, the revision
# LibreLane 3.1.0.dev3 pins in nix/openroad.nix) is: parse flags, then
#     pdn::check_setup ; pdn::build_grids $trim
#     pdn::write_to_db $add_pins $failed_via_report ; pdn::reset_shapes
# check_setup insists that every macro is placed AND fixed (PdnGen::checkDesign,
# PDN-0234/0235: CI run 6 stopped there when the macro was released for the
# whole call), while build_grids turns only fixed instances into obstructions.
# So the wrapper runs those same four steps itself and releases the hard
# macros only around build_grids. Any other flag goes to the original proc.
if { [info commands ::keyer_pdngen_unwrapped] eq "" } {
    rename ::pdngen ::keyer_pdngen_unwrapped
    proc ::pdngen { args } {
        foreach a $args {
            if { $a ne "-skip_trim" } {
                return [::keyer_pdngen_unwrapped {*}$args]
            }
        }
        foreach cmd {pdn::check_setup pdn::build_grids pdn::write_to_db pdn::reset_shapes} {
            if { [info commands ::$cmd] eq "" } {
                error "KEYERPDN: $cmd does not exist; this wrapper follows pdngen in OpenROAD dcf36133"
            }
        }
        set trim [expr {[lsearch -exact $args -skip_trim] < 0}]

        ::pdn::check_setup

        set released [list]
        foreach inst [[ord::get_db_block] getInsts] {
            if { [[$inst getMaster] isBlock] && [$inst isFixed] } {
                lappend released [list $inst [$inst getPlacementStatus]]
                $inst setPlacementStatus "PLACED"
            }
        }
        set rc [catch { ::pdn::build_grids $trim } msg opts]
        foreach r $released {
            lassign $r inst status
            $inst setPlacementStatus $status
        }
        if { $rc } {
            return -options $opts $msg
        }

        ::pdn::write_to_db 1 ""
        ::pdn::reset_shapes

        keyer_check_macro_stripes
        foreach net_name [concat $::env(VDD_NETS) $::env(GND_NETS)] {
            puts "KEYERPDN check_power_grid -net $net_name"
            check_power_grid -net $net_name
        }
    }
}
