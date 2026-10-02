# Kickoff prompt for the first Claude Code session

Run `claude` inside the repo directory, then paste everything below the line
as your first message. It repeats the essentials even though CLAUDE.md
carries them, so the session cannot start on the wrong foot.

---

Read CLAUDE.md and follow its read order: docs/HANDOFF.md, the last ten
entries of docs/DECISIONS.md, docs/isa.md, docs/BUGS.md, WORKLOG.md,
PLAN.md. Then run `bash scripts/check_all.sh quick` and report what passed
and what failed; do not fix anything yet.

Context for this project:

- This is my entry to the Jane Street protocol emulator ASIC competition
  (Tiny Tapeout, IHP 130 nm CMOS5L, 6x4 tiles, deadline 2027-01-18). The
  repo was built in one earlier AI session: ISA v0.1, assembler, cycle-exact
  Python model, UART/SPI/I2C firmware verified against protocol models,
  complete RTL, a cocotb harness that runs the model in lockstep with the
  RTL from reset, and formal proofs. Everything passes.
- A friend's parallel entry exists at github.com/thomasgilbert481/tt_um_loom,
  reviewed in docs/review-of-tt_um_loom.md. It was built by the same model
  family, so it looks like a sibling of ours. Rules: never copy its ISA,
  RTL, model, firmware or text. You may use its published facts about the
  Tiny Tapeout CMOS5L flow and, with attribution, its SRAM macro flow
  recipe (Apache-2.0 infrastructure), or wait for Tiny Tapeout's official
  macro template. Our design must differ from theirs on purpose.
- I own the design decisions. Propose, explain the trade-off, wait for my
  answer. Do not change RTL or the model in this session.

Do these in order, stopping where it says to stop:

1. Open decisions D-013 to D-016 in docs/DECISIONS.md. Take them one at a
   time: for each, give me your recommendation and the trade-off in at most
   four sentences, then wait for my answer before the next one. For the
   rename (D-013), propose five candidate names that do not use a weaving
   metaphor. Starting recommendations from the previous session, which you
   may argue against: adopt a NOW/deadline timer with timeouts on every
   wait (D-014 proposal B); keep two threads for the finer edge timing but
   make the thread count a parameter and synthesise both (D-015); commit to
   capture-and-replay plus staying small as the differentiator, with
   Hardcaml as an optional later item (D-016).
2. Write the outcomes as new DECISIONS entries (D-017 onward, append-only;
   mark each OPEN entry as closed by its successor). Apply the rename
   everywhere: project name, the `tt_um_` top-module name (must stay unique
   and keep my GitHub username, ahan17x), info.yaml, README, test/Makefile,
   tb.v, docs, the synthesis and check scripts. Run
   `bash scripts/check_all.sh` and show me it is green. Commit.
3. Start docs/SEMANTICS.md: the cycle-exact contract both the RTL and the
   model will be held to. Cover, in this order: the execution model (slots,
   fetch timing, what commits on which clock edge); reset state; the timer
   as decided in D-014; the pin unit (synchroniser latency, edge
   definition, pin-write visibility, open-drain rules); blocking
   instructions; FIFOs; host-interface effects and their timing. Use
   docs/isa.md and tools/keyersim.py as the starting statement of the
   semantics and the cocotb lockstep tests as evidence of the timing. Stop
   for my review after the timer and pin sections.
4. End the session with a dated WORKLOG.md entry, an updated docs/HANDOFF.md
   that says exactly what the next session does, and all tests green.
   Commit, do not push.
