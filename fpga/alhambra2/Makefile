# Post-synthesis simulation of the iCE40 netlist: the host-interface and
# pads-only cocotb tests of test/ (the set the ASIC's gate-level job runs)
# against what Yosys made of the design for the FPGA, block RAMs included.
# The lockstep tests read RTL internals and skip, as they do at gate level.
#
#   make -C fpga/alhambra2 sim        (bash fpga/alhambra2/sim.sh does this)
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
HERE     := $(PWD)
TEST_DIR := $(HERE)/../../test
SRC_DIR  := $(HERE)/../../src
YOSYS_DATDIR := $(shell yosys-config --datdir)
NETLIST  := $(HERE)/build/keyer_ice40_netlist.v

SIM_BUILD        = build/sim
COCOTB_RESULTS_FILE = build/sim/results.xml
VERILOG_SOURCES  = $(NETLIST) $(YOSYS_DATDIR)/ice40/cells_sim.v $(TEST_DIR)/tb.v
TOPLEVEL         = tb
COCOTB_TEST_MODULES = test,test_host
export GATES := yes
export PYTHONPATH := $(TEST_DIR):$(TEST_DIR)/../tools:$(PYTHONPATH)
ifneq ($(wildcard $(HERE)/../../.venv/bin),)
export PATH := $(HERE)/../../.venv/bin:$(PATH)
endif
COCOTB_CONFIG := $(firstword $(wildcard $(HERE)/../../.venv/bin/cocotb-config) cocotb-config)

.DEFAULT_GOAL := sim

# the Tiny Tapeout top alone (no board wrapper), so test/tb.v can instantiate it
$(NETLIST): $(wildcard $(SRC_DIR)/*.v) $(SRC_DIR)/keyer_isa.vh
	mkdir -p build
	yosys -q -l build/synth_sim.log -p "read_verilog -DKEYER_IMEM_FLOPS -I$(SRC_DIR) \
	  $(SRC_DIR)/keyer_fifo.v $(SRC_DIR)/keyer_imem.v $(SRC_DIR)/keyer_pins.v $(SRC_DIR)/keyer_core.v \
	  $(SRC_DIR)/keyer_host.v $(SRC_DIR)/keyer_capture.v $(SRC_DIR)/tt_um_ahan17x_keyer.v; \
	  synth_ice40 -top tt_um_ahan17x_keyer; write_verilog -noattr $(NETLIST)"

include $(shell $(COCOTB_CONFIG) --makefiles)/Makefile.sim
