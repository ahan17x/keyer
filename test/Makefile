# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = keyer_fifo.v keyer_imem.v keyer_pins.v keyer_core.v keyer_host.v keyer_capture.v tt_um_ahan17x_keyer.v
# The vendored simulation model of the SRAM program memory (D-025), needed for
# both the RTL and the gate-level case: keyer_imem instantiates the macro by
# default and the gate-level netlist does too. The behavioural core comes
# first because the wrapper instantiates it; -DFUNCTIONAL selects that path.
# The port-only blackbox src/RM_IHPSG13_1P_256x16_c2_bm_bist.v is NOT compiled
# here: it would redefine the module.
MACRO_DIR     = $(PWD)/../macro/RM_IHPSG13_1P_256x16_c2_bm_bist
MACRO_MODELS  = $(MACRO_DIR)/RM_IHPSG13_1P_core_behavioral_bm_bist.v
MACRO_MODELS += $(MACRO_DIR)/RM_IHPSG13_1P_256x16_c2_bm_bist.v

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
COMPILE_ARGS    += -DFUNCTIONAL
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
VERILOG_SOURCES += $(MACRO_MODELS)

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v
# the cell models instantiate primitives (ihp_mux2, ...) defined here; the
# template omits it and the gate-level compile fails without it (docs/AREA.md)
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v
VERILOG_SOURCES += $(MACRO_MODELS)

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)
# Waveform dump is opt-in: make DUMP=1
ifeq ($(DUMP),1)
COMPILE_ARGS    += -DDUMP
endif
# The assembler, ISS and protocol models live in ../tools
export PYTHONPATH := $(PWD)/../tools:$(PYTHONPATH)
# Use the repo's virtualenv if scripts/setup_mac.sh created one
ifneq ($(wildcard $(PWD)/../.venv/bin),)
export PATH := $(PWD)/../.venv/bin:$(PATH)
endif

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test,test_host,test_corners,test_jtag_master,test_ws2812,test_ps2_host,test_i2c_slave,test_spi_slave,test_swd

# include cocotb's make rules to take care of the simulator setup
# cocotb-config is taken from the repo's virtualenv when it exists
# (scripts/setup_mac.sh): $(shell ...) runs when make parses this file, before
# the PATH export above can help, so a plain `make` would otherwise fail with
# "cocotb-config: Command not found" (docs/SETUP.md).
COCOTB_CONFIG := $(firstword $(wildcard $(PWD)/../.venv/bin/cocotb-config) cocotb-config)
include $(shell $(COCOTB_CONFIG) --makefiles)/Makefile.sim

# Tiny Tapeout's test workflow and its gl_test action end with
# `! grep failure results.xml`. cocotb 2 writes failures="0" into every
# results file, which that grep matches, so a passing run was reported as a
# failure (DECISIONS D-023, D-027). When the run has no <failure> element the
# attribute is dropped, so that check means what it was written to mean. The
# default goal runs the simulation and then this step.
.DEFAULT_GOAL := keyer_all
.PHONY: keyer_all
keyer_all: sim
	@$(PYTHON_BIN) -c "import re; p='$(COCOTB_RESULTS_FILE)'; s=open(p).read(); open(p,'w').write(s if '<failure' in s else re.sub(' failures=.0.', '', s))"
