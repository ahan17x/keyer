# External facts

Every fact the design, the firmware, the models, the tests or the docs
depend on that comes from outside this repository: where it is used, the
primary source, the source's wording, and a status. Written 2026-11-12
(session 8) from sources fetched that day; nothing here is marked verified
from memory.

**Rule (CLAUDE.md).** No external fact enters a spec, test, model or doc
without a row here with a fetched source. A fact from memory is entered as
`unverified` until it has one, and the doc that uses it says so.

**Status.**

- `verified`: the primary source was fetched in the session named in the
  row's source entry and the quoted words are from it.
- `computed`: recomputed independently in that session; the row says how.
- `unverified`: no primary source could be reached; the row says what was
  relied on instead. A fetched secondary source is still `unverified`.

Quotes are held to the few words that carry the fact (the documents are
copyrighted); the section number finds the rest. Line numbers are those of
the commit that added this file.

## Sources

| Id | Document | Where fetched from | Identity |
|---|---|---|---|
| S1 | Universal Serial Bus Specification, Revision 2.0 (`usb_20.pdf`, 650 pages) | `https://www.usb.org/sites/default/files/usb_20_20250603.zip`, linked from <https://www.usb.org/document-library/usb-20-specification> | zip sha256 `5fe9c53c…b3692`, pdf `d39698a3…9f61` |
| S2 | USB-IF white paper "Cyclic Redundancy Checks in USB" (`crcdes.pdf`, 6 pages) | <https://www.usb.org/sites/default/files/crcdes.pdf> | sha256 `e96f54d7…ebb4` |
| S3 | Catalogue of parametrised CRC algorithms (G. Cook) | <https://reveng.sourceforge.io/crc-catalogue/all.htm> | fetched 2026-11-12 |
| S4 | fpga4fun.com, "10BASE-T FPGA interface", parts 0, 2 and 3 | <https://www.fpga4fun.com/10BASE-T2.html> (`-T0`, `-T3`) | part 2 sha256 `5ec50ec5…` |
| S5 | UNH-IOL Ethernet Testing Service, Embedded MAU Test Suite v5.2 (secondary: it restates IEEE 802.3-2008 clause 14) | <https://www.iol.unh.edu/sites/default/files/testsuites/ethernet/CL14_MAU/MAU_Test_Suite_v5.2.pdf> | sha256 `265bbef9…` |
| S6 | IEEE Registration Authority, EtherType public listing | <https://standards-oui.ieee.org/ethertype/eth.txt> | fetched 2026-11-12 |
| S7 | pid.codes, entry 1209/0001 | <https://pid.codes/1209/0001/> (source `pidcodes/pidcodes.github.com`, `1209/0001/index.md`) | fetched 2026-11-12 |
| S8 | NXP UM10204, I2C-bus specification and user manual, Rev. 7.0, 1 October 2021 | <https://www.nxp.com/docs/en/user-guide/UM10204.pdf> | sha256 `dc91f00f…` |
| S9 | Worldsemi WS2812B datasheet (5 pages; the copy Adafruit hosts) | <https://cdn-shop.adafruit.com/datasheets/WS2812B.pdf> | fetched 2026-11-12 |
| S10 | Arm Debug Interface Architecture Specification ADIv5.0 to ADIv5.2, ARM IHI 0031G | `https://documentation-service.arm.com/static/622222b2e6f58973271ebc21` | 296 pages |
| S11 | RP2350 Datasheet, Raspberry Pi Ltd, release 8 (1,371 pages) | <https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf> | sha256 `a638d678…2e39` |
| S12 | Tiny Tapeout SDK, `TinyTapeout/tt-micropython-firmware`, `main` at `d485c7a998` (2026-09-08) | `src/ttboard/pins/gpio_map_dbv3.py`, `gpio_map.py` | GitHub |
| S13 | Tiny Tapeout IHP Verilog template, `TinyTapeout/ttihp-verilog-template`, `main` at `4b1204ab38` (2026-10-28) | `info.yaml`, `src/config.json`, `.github/workflows/gds.yaml` | GitHub |
| S14 | `TinyTapeout/tt-support-tools`, `main` at `9d13ff3e3e` (2026-11-05) and branch `ihp-sg13cmos5l` at `d66cf179e7` (2026-09-21) | `tech.py`, `project.py`, `precheck/precheck.py`, `tech/ihp-sg13cmos5l/tile_sizes.yaml` | GitHub |
| S15 | `TinyTapeout/tt-gds-action`, tag `ttihp27a` at `e7a50f8f46` (2026-10-29) and branch `ihp-cmos5l` at `3412659307` (2026-09-14) | `action.yml` | GitHub |
| S16 | IHP Open PDK, `IHP-GmbH/IHP-Open-PDK`, branch `dev` at `cc9afba57e` (2026-11-10); the macro files vendored in `macro/` are from `bf079026` | `ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/*.lib`; `macro/RM_IHPSG13_1P_256x16_c2_bm_bist/*` | GitHub and this repo |
| S17 | GitHub Docs, "Actions limits" | <https://docs.github.com/en/actions/reference/limits> | fetched 2026-11-12 |
| S18 | Jane Street blog, "Can you design a chip? Announcing the protocol emulator ASIC competition" (dated Sep 10, 2026, with an update of November 9th) | <https://blog.janestreet.com/protocol-emulator-asic-competition/> | sha256 `3e9ae6ad…` as fetched 2026-11-12 |
| S19 | Tiny Tapeout, "PCB - ETR" | <https://tinytapeout.com/specs/pcb-etr/> | fetched 2026-11-12 |
| S20 | RFC 9542 (IANA considerations and IETF use of IEEE 802 parameters; secondary for IEEE 802) | <https://www.rfc-editor.org/rfc/rfc9542.txt> | fetched 2026-11-12 |

**Not reachable.** IEEE Std 802.3 (any revision): IEEE Xplore returns an
empty response without a signed-in account, and the IEEE GET programme
needs one. IEEE Std 1149.1 (JTAG): paid. The IBM PS/2 hardware interface
technical reference: no primary copy found. The SMBus specification:
`smbus.org` did not answer. Every row that rests on one of these is
`unverified` below and in the file that uses it.

## 1. USB low speed

| # | Fact | Used at | Source | Source wording | Status |
|---|---|---|---|---|---|
| U1 | Bit rate 1.5 Mbit/s, a low-speed function's tolerance is 1.5% | `tools/protomodels_usb_host.py:91-92` (`BIT_RATE`, `RATE_TOL`), `fw/usb_ls_device.s:114` (`BITT` = 48 MHz / 1.5 Mbit/s) | S1 7.1.11; Table 7-10 `TLDRATE` 1.4775 to 1.5225 Mb/s | "(TLDRATE) is 1.50 Mb/s ±1.5% (15,000 ppm)" | verified |
| U2 | SYNC is KJKJKJKK: eight symbols | `docs/SEMANTICS.md:843`, `:887` (receiver starts on the eight decoded bits `0x80`), `docs/SERIALIZER.md:63`, `tools/protomodels_usb_host.py:19`, `tools/test_protomodels.py:137` | S1 7.1.10 | "3 KJ pairs followed by 2 K's for a total of eight symbols" | verified |
| U3 | **A hub may not shorten a low-/full-speed SYNC.** The allowance to drop SYNC bits is for high speed only; at low/full speed a hub may change the width of the first bit by under 5 ns and must keep the first edge | `docs/SEMANTICS.md:887` (all eight bits required), HANDOFF task 3 of session 7 | S1 7.1.10 (the sentence follows "The SYNC pattern used for high-speed transmission … 32 symbols"); 7.1.7.4.1; 11.7.2.2; 8.2 | 7.1.10: "Hubs are allowed to drop up to 4 bits from the start" (high speed). 7.1.7.4.1: "first bit of SOP … to less than ± 5 ns". 11.7.2.2: "the initial edge of the SYNC field is preserved" | verified. **No change**: the receiver's rule is not a spec bug. 8.2's "Received SYNC fields may be shorter as described in Chapter 7" points at the high-speed text; Chapter 7 describes no shortening at low or full speed |
| U4 | NRZI: a 0 is a transition, a 1 is none; bits least significant first | `docs/SEMANTICS.md:835-838` (`emit`), `tools/protomodels_usb_host.py:18` | S1 7.1.8 | "no change in level and a "0" is represented by a change in level" | verified |
| U5 | A 0 is stuffed after six consecutive ones, counted from the SYNC's final one, also directly before the EOP; seven ones are an error | `docs/SEMANTICS.md:806-808`, `:845`, `:876-878` (`rx_ones` starts at 1 after SYNC, `:890`), `tools/protomodels_usb_host.py:18-19`, `tools/test_protomodels.py:140-164`, `formal/ser_stuff_props.sv` | S1 7.1.9, 7.1.9.1 | "A zero is inserted after every six consecutive ones"; "ends the Sync Pattern is counted as the first one"; "even if it is the last bit before the end-of-packet" | verified |
| U6 | EOP: SE0 for two bit times, then J for one | `docs/SEMANTICS.md:826-829`, `:858-859`, `docs/SERIALIZER.md:62` | S1 7.1.7.4.1 | "SE0 state for two bit times followed by … J state for one bit time" | verified |
| U7 | A low-speed transmitter's EOP SE0 is 1.25 to 1.50 us wide | `tools/protomodels_usb_host.py:93` (`EOP_MIN`, `EOP_MAX`), `fw/usb_ls_device.s:60` | S1 7.1.13.2.1 (`TLEOPT`) | "SE0 for EOP width must be between 1.25 µs and 1.50 µs" | verified. In bit times: 1.25 / 0.6667 = 1.875 and 1.50 / 0.6667 = 2.25 (computed) |
| U8 | A device answers no sooner than 2 bit times after the host's EOP; the delay runs from SE0-to-J to the J-to-K of the answer | `tools/protomodels_usb_host.py:34-37`, `:94` (`RESP_MIN`), `fw/usb_ls_device.s:116` (`RESP`), `:84-87` | S1 7.1.18.1 | "A device must provide at least two bit times of inter-packet delay" | verified |
| U9 | A device must answer within 6.5 bit times with a detachable cable and within 7.5 with a captive one; a low-speed device has a captive cable, so its limit is 7.5 | `tools/protomodels_usb_host.py:94` (`RESP_MAX` = 6.5), `fw/usb_ls_device.s:87`, `tools/test_fw_usb_ls_device.py:107`, `test/test_usb_ls_device.py:46` | S1 7.1.18.1; 7.1.6.1 ("A low-speed device is required to have a captive cable") | "(TRSPIPD1) cable is 6.5 bit times"; "(TRSPIPD2) must be less than 7.5 bit times" | verified. **Annotated**: the model holds the firmware to 6.5, one bit time tighter than the 7.5 that applies to it; kept as margin, and the model and firmware headers now say which figure is which. The firmware answers in 3.7 to 4.7 |
| U10 | The host answers a device within 7.5 bit times | `fw/usb_ls_device.s:71-72` (`ACK_TMO`), `:121` | S1 7.1.18.1 | "maximum inter-packet delay for a host response is 7.5 bit times" | verified. **Wrong in the repo, fixed** (BUGS 52): the firmware comment said the host has 6.5. `ACK_TMO` = 24 covers 7.5 with a bit of margin; `test_host_handshake_at_the_latest_allowed_time` now holds it there |
| U11 | The host gives up on a response after 18 bit times (a device not before 16) | `tools/protomodels_usb_host.py:37-40`, `:95` (`TIMEOUT`) | S1 7.1.19.1 | "will not time out before 16 bit times but will timeout before 18"; "host will wait at least 18 bit times" | verified |
| U12 | The host leaves at least 2 bit times between its packets | `tools/protomodels_usb_host.py:57-58` (`gap_bits`) | S1 7.1.18.1 (`TIPD`) | "at least two bit times of J after the SE0 of an EOP" | verified |
| U13 | A device may treat SE0 longer than 2.5 us as a reset; the host drives reset for at least 10 ms | `fw/usb_ls_device.s:57-63` (`RST_TMO`), `:118`, `tools/protomodels_usb_host.py` (`reset`) | S1 7.1.7.5 (`TDETRST`, `TDRST`) | "for more than 2.5 µs (TDETRST) may treat that signal as a reset"; "at least 10 ms (TDRST)" | verified. 2.5 us = 3.75 bit times (computed) |
| U14 | Low speed: 1.5 k pull-up on D-; idle is J = D- high, D+ low; a low-speed device sees a keep-alive (a bare low-speed EOP) every frame | `fw/usb_ls_device.s:41-44`, `docs/info.md:306`, `tools/protomodels_usb_host.py:12-15`, `:61-63` (`keepalive_bits` = 1500 = 1 ms) | S1 7.1 (introduction), Table 7-2 note 4, 7.1.7.6 | "a 1.5 kΩ pull-up on the D-"; "The keep-alive is a low-speed EOP"; "at least one keep-alive … in every frame" | verified |
| U15 | CRC-5: x^5 + x^2 + 1, register seeded with ones, sent inverted, residual 01100 | `docs/SEMANTICS.md:738`, `:744`, `:906` (`0x14`, `0x1F`, `0x06`), `src/keyer_ser.v:316-342`, `tools/keyersim.py:90`, `tools/protomodels_usb_host.py:103-109`, `tools/test_protomodels.py:8-9`, `:84` | S1 8.3.5, 8.3.5.1 | "seeded with an all-ones pattern"; "00101B"; "five-bit residual at the receiver will be 01100B" | verified. The reflected forms are computed: 00101 reversed is `0x14`; 01100 reversed is `0x06`, which is also S3's `residue=0x06` for CRC-5/USB |
| U16 | CRC-16: x^16 + x^15 + x^2 + 1, residual 1000000000001101 | `docs/SEMANTICS.md:739-742` (`0xA001`, `0xB001`), `src/keyer_ser.v:116-118`, `tools/keyersim.py:91-93`, `tools/protomodels_usb_host.py:112-119`, `tools/test_protomodels.py:9`, `:114` | S1 8.3.5.2 | "1000000000000101B"; "16-bit residual will be 1000000000001101B" | verified. Reflected: `0x8005` reversed is `0xA001`, `0x800D` reversed is `0xB001` (computed; S3 gives `residue=0xb001` for CRC-16/USB) |
| U17 | CRC check values: CRC-16/USB of "123456789" is `0xB4C8`, CRC-5/USB is `0x19` | `tools/test_protomodels.py:127`, `tools/test_fw_usb_ls_device.py:116-117`, `tools/test_iss_ser.py:96-98` | S3 | `check=0xb4c8 … name="CRC-16/USB"`; `check=0x19 residue=0x06 name="CRC-5/USB"` | verified |
| U18 | The white paper's worked examples: three token fields and two data fields with their CRCs | `tools/test_protomodels.py:63-67` (`CRC5_EXAMPLES`), `:105-108` (`CRC16_EXAMPLES`), `tools/test_fw_usb_ls_device.py:119-121` | S2, "Examples of CRC computation" | "setup addr 15 endp e / crc5 10101000111 / 10111"; "out addr 3a endp a … 11100"; "in addr 70 endp 4 … 01110"; "data0 00 01 02 03 … 1111011101011110"; "data1 23 45 67 89 … 0111000000111000" | verified, digit for digit. The paper also gives two SOF examples the tests do not use. `test_protomodels.py`'s own `_crc_msb` recomputes all five (computed) |
| U19 | PID values and the check nibble | `tools/protomodels_usb_host.py:87-88` (`PID`), `tools/test_protomodels.py:93-99` | S1 8.3.1, Table 8-1 | "performing a one's complement of the packet type field"; OUT 0001B, IN 1001B, SOF 0101B, SETUP 1101B, DATA0 0011B, DATA1 1011B, ACK 0010B, NAK 1010B, STALL 1110B, PRE 1100B | verified |
| U20 | Low speed: control endpoints carry 8 bytes at most; interrupt endpoints 0 to 8 bytes with a period of 10 to 255 ms | `tools/protomodels_usb_host.py:96` (`LS_MAXPKT`), `:835`, `:855`, `tools/test_fw_usb_ls_device.py:249`, `:254`, descriptor bytes in `fw/usb_ls_device.s:313-398` | S1 5.5.3, 5.7.3, 5.7.4 | "for low-speed devices, it is 8 bytes"; "limited to specifying only 10 ms to 255 ms" | verified |
| U21 | The descriptors the firmware returns: device `12 01 10 01 FF 00 00 08 09 12 01 00 00 01 00 00 00 01`; configuration `09 02 19 00 01 01 00 80 32`, interface `09 04 00 00 01 FF 00 00 00`, endpoint `07 05 81 03 08 00 0A`. `bmAttributes` 0x80 is bus powered with the reserved bit set; `bMaxPower` 0x32 is 100 mA | `fw/usb_ls_device.s:10-16`, `:313-398`, `tools/test_fw_usb_ls_device.py:245-254` | S1 9.6.1 Table 9-8, 9.6.3 Table 9-10, 9.6.5, 9.6.6 Table 9-13 | "D7: Reserved (set to one)"; "Expressed in 2 mA units (i.e., 50 = 100 mA)" | verified for the fields quoted and the field order of Tables 9-8, 9-10, 9-12 and 9-13 (read in S1); the byte values are the firmware's own choices |
| U22 | VID 0x1209, PID 0x0001 is pid.codes' test PID | `fw/usb_ls_device.s:10-11` | S7 | "This PID is reserved for use in private testing." | verified. S7 also says it must not be used on anything redistributed: the firmware header already says to change both |
| U23 | SET_ADDRESS takes effect after the status stage | `fw/usb_ls_device.s:20-22` | S1 9.4.6 | "does not change its device address until after the Status stage" | verified |
| U24 | Source jitter of a low-speed function: 95 ns to the next transition | `tools/protomodels_usb_host.py:254` (`jitter_ns` = 25) | S1 Table 7-10 (`TUDJ1` -95 to 95 ns; `TDDJ1` -25 to 25 ns is the downstream port's) | Table 7-10 rows `TUDJ1`, `TDDJ1` | verified. **Annotated**: the model's 25 ns is the downstream-port figure and is stricter than what a device is allowed; the firmware's edges are on the clock grid, so it passes either |

## 2. 10BASE-T and Ethernet

IEEE 802.3 could not be fetched, so every row that cites a clause is
`unverified`; the secondary source that was fetched is named.

| # | Fact | Used at | Source | Wording relied on | Status |
|---|---|---|---|---|---|
| E1 | 10 Mbit/s, tolerance 0.01% (100 ppm) | `tools/protomodels_eth_10bt.py:55-56` (`rate_ppm`), `fw/eth_10bt_tx.s:12-15` | IEEE 802.3 7.3.2 / 14.3.1.2 (not fetched) | S4 part 3: "A 10BASE-T network works at 10Mbps" (rate only) | unverified (the tolerance is from memory) |
| E2 | Manchester: the first half of the bit cell is the complement of the bit, the second half the bit | `docs/SEMANTICS.md:836-837` (`line(~b)` then `line(b)`), `:920-922` (a rising mid-bit edge is a 1), `tools/protomodels_eth_10bt.py:43-45` | IEEE 802.3 7.3.1.1 (not fetched) | S4 part 3: "A logical '1' is sent as a low bit followed by a high bit." | unverified (S4 agrees) |
| E3 | Link test pulse every 16 ms, give or take 8 ms, counted from the last pulse or from the start of idle | `fw/eth_10bt_tx.s:17-24`, `:115-116`, `tools/protomodels_eth_10bt.py:30-36` (`lp_interval_ms`), `docs/info.md:132` | IEEE 802.3 14.2.1.1 (not fetched) | S5 test 14.1.1: "followed by a 16 ms [±] 8 ms period of silence and a link test pulse" (the sign is a glyph the text extraction lost) | unverified (S5, which cites 802.3-2008 14.2.1.1, agrees) |
| E4 | Link pulse: nominally 100 ns, positive; a receiver accepts 0.6 to 2.0 bit times | `fw/eth_10bt_tx.s:19-20`, `tools/protomodels_eth_10bt.py:24-29` (`lp_width_ns` = 60 to 200) | IEEE 802.3 14.3.1.2.1 Figure 14-12, 14.3.1.3.2 (not fetched) | S5 test 14.2.4 uses "a pulse width of 0.6 BT" as its narrow case | unverified (the 2.0 bit-time upper limit is from memory) |
| E5 | Preamble seven bytes 0x55, start frame delimiter 0xD5, sent least significant bit first | `docs/SEMANTICS.md:887` (sync `0xD5` in mode 2), `docs/SERIALIZER.md:63`, `fw/eth_10bt_tx.s:27`, `tools/protomodels_eth_10bt.py:57-58`, `:88` | IEEE 802.3 3.2.1, 3.2.2 (not fetched) | S4 part 2: "Ethernet preamble/SFD (synchronizer): 55 55 55 55 55 55 55 D5" | unverified (S4 agrees) |
| E6 | Frame length 64 to 1518 bytes from destination to FCS; minimum payload 46 | `tools/protomodels_eth_10bt.py:59-61` (`frame_len`), `fw/eth_10bt_tx.s:53-58` | IEEE 802.3 3.2.7, 4.4.2 (not fetched) | S4's published frame is 60 bytes plus FCS = 64 | unverified |
| E7 | FCS: CRC-32, polynomial 0x04C11DB7 (reflected 0xEDB88320), ones to start, sent complemented, least significant byte first; receiver residual 0xDEBB20E3; check value 0xCBF43926 | `docs/SEMANTICS.md:740-742`, `src/keyer_ser.v:116-118`, `tools/keyersim.py:91-93`, `tools/protomodels_eth_10bt.py:95-104`, `tools/test_protomodels.py:253-260`, `tools/test_fw_eth_10bt_tx.py:158`, `formal/ser_crc_ref.vh` | IEEE 802.3 3.2.9 (not fetched); S3; S4 | S3: `poly=0x04c11db7 init=0xffffffff refin=true refout=true xorout=0xffffffff check=0xcbf43926 residue=0xdebb20e3 name="CRC-32/ISO-HDLC"` | computed: `binascii.crc32` (zlib, not this project's code) on the 60 bytes of E8 gives the FCS the page prints, and on "123456789" gives S3's check value. That this algorithm is 802.3's is unverified against the standard |
| E8 | The published frame used as a vector, and its FCS `B3 31 88 1B` | `tools/test_protomodels.py:238-247` (`PUBLISHED_FRAME`, `PUBLISHED_FCS`) | S4 part 2 | "55 55 55 55 55 55 55 D5 00 10 A4 7B EA 80 00 12 34 56 78 90 08 00 45 00 00 2E B3 FE …"; "Ethernet checksum: B3 31 88 1B" | verified, byte for byte against the page |
| E9 | Inter-frame gap 96 bit times (9.6 us) | `fw/eth_10bt_tx.s:38-39`, `:118` (`IFG`), `tools/protomodels_eth_10bt.py:74-75` (`ifg_ns`) | IEEE 802.3 4.4.2 (not fetched) | none fetched | unverified (from memory) |
| E10 | Start of idle: the line stays positive for at least 250 ns after the last bit; the model also wants it back at zero within 450 ns | `docs/SEMANTICS.md:830-831`, `:859`, `docs/SERIALIZER.md:62` (six half-bits = 300 ns), `fw/eth_10bt_tx.s:33-37`, `tools/protomodels_eth_10bt.py:62-72` (`soi_ns`) | IEEE 802.3 14.3.1.2.1 Figure 14-10 (not fetched) | S4 part 3: "a positive pulse of about 3 bit-times"; S5 test 14.1.7 only says the waveform "shall fit into the template defined in Figure 14-10" | unverified (250 ns and 450 ns are from memory and secondary descriptions, as the model header already says) |
| E11 | Transmit jitter: zero crossings at 8.0 and 8.5 bit times within 20 ns without the twisted-pair model, 11 ns with it | `tools/protomodels_eth_10bt.py:50-54` (`half_tol_ns` = 10, stricter) | IEEE 802.3 14.3.1.2.3 (not fetched) | S5 tests 14.1.10 and 14.1.11: "at 8.0 BT [±] 11 ns and 8.5 BT [±] 11 ns" with the twisted-pair model, "[±] 20 ns" without (sign glyph lost in extraction) | unverified (S5 agrees) |
| E12 | EtherType 0x88B5 is a local experimental EtherType | `fw/eth_10bt_tx.s:30` | S6 | "88B5 IEEE 802.1 Working Group Local Experimental EtherType 1 as defined in IEEE Std 802." | verified |
| E13 | A MAC address with bit 0x02 of its first byte set is locally administered | `fw/eth_10bt_tx.s:29` (source `02:4b:45:59:45:52`) | IEEE Std 802 (not fetched); S20 section 2.1 | S20 describes the "Local bit" of the first octet | unverified (S20 is secondary) |
| E14 | Clock choice: a half-bit is 50 ns, so the core clock must be 20 or 40 MHz | `fw/eth_10bt_tx.s:12-15`, `docs/info.md:86-87`, `docs/SERIALIZER.md:183-188` | follows from E1 | 1 / (2 x 10 Mbit/s) = 50 ns; 50 MHz gives 2.5 cycles | computed |

## 3. UART, SPI mode 0, I2C

| # | Fact | Used at | Source | Source wording | Status |
|---|---|---|---|---|---|
| P1 | UART framing 8N1: idle high, a low start bit, eight data bits least significant first, a high stop bit | `tools/protomodels.py:28-64` (`UartDecoder`), `fw/uart.s` | no single standard defines asynchronous 8N1 framing; TIA-232-F covers levels only | none | unverified (convention, from memory) |
| P2 | Divider for 115200 baud: 434 at 50 MHz, 521 at 60 MHz | `docs/info.md:185-188`, `:226`, `fw/uart.s:6`, `:14` | arithmetic | 50,000,000 / 115,200 = 434.03; 60,000,000 / 115,200 = 520.83 | computed (errors +0.006% and -0.03%) |
| P3 | SPI mode 0: clock idles low, data sampled on the rising edge and changed on the falling edge, most significant bit first | `tools/protomodels.py:86-139`, `tools/protomodels_spi_slave.py`, `fw/spi_master.s:1`, `fw/spi_slave.s:1` | SPI has no standards body; the usual reference is Motorola's SPI Block Guide (not fetched) | none | unverified (from memory; S18's FAQ names "SPI modes 0-3" as a target, nothing more) |
| P4 | I2C: START is SDA falling while SCL is high, STOP is SDA rising while SCL is high; data is stable while SCL is high; nine clocks per byte with the acknowledge as the ninth; most significant bit first | `tools/protomodels.py:141-260` (`I2cSlave`), `tools/protomodels_i2c_slave.py:5-12`, `fw/i2c_master.s`, `fw/i2c_slave.s`, `tools/keyerhost.py` (`I2cDecoder`) | S8 3.1.3, 3.1.4, 3.1.5, 3.1.6 | "A HIGH to LOW transition on the SDA line while SCL is HIGH defines a START condition."; "must be stable during the HIGH period of the clock" | verified |
| P5 | I2C bit rates: 100 kbit/s Standard-mode, 400 kbit/s Fast-mode, 1 Mbit/s Fast-mode Plus | `fw/i2c_slave.s:78-80`, `fw/i2c_master.s:22`, `docs/info.md:281` | S8 section 1 | "up to 100 kbit/s in the Standard-mode, up to 400 kbit/s in the Fast-mode" | verified |
| P6 | The model's minimum SCL high and low times (`min_high`, `min_low`) | `tools/protomodels.py:150` (60 cycles each by default), `tools/protomodels_i2c_slave.py` | parameters in core cycles chosen per test, not S8's Table 11 figures | none | not an external fact: no test claims a Table 11 timing. Stated here so nobody reads it as one |
| P7 | An SMBus device times out after SCL has been low for 25 to 35 ms | `fw/i2c_slave.s:58` | SMBus specification, `TTIMEOUT` (smbus.org did not answer) | none | unverified (from memory) |
| P8 | A 24Cxx EEPROM: pointer write, sequential read, current-address read | `fw/i2c_slave.s:1`, `tools/protomodels.py:142-147`, `docs/info.md:126` | a 24C02 datasheet (not fetched) | none | unverified (from memory) |

## 4. Other protocols in the firmware

| # | Fact | Used at | Source | Source wording | Status |
|---|---|---|---|---|---|
| O1 | WS2812B: 800 kbit/s; T0H 0.40 us, T1H 0.80 us, T0L 0.85 us, T1L 0.45 us, each within 150 ns; period 1.25 us within 600 ns; reset low for more than 50 us | `fw/ws2812.s:1`, `:42-47`, `tools/protomodels_ws2812.py:13-15` | S9, "Data transfer time" | "T0H 0 code ,high voltage time 0.4us ±150ns"; "TH+TL=1.25µs±600ns"; "RES low voltage time Above 50µs"; "Send data at speeds of 800Kbps." | verified |
| O2 | WS2812B data order: green, red, blue, most significant bit first | `fw/ws2812.s:6` | S9 | "Follow the order of GRB to sent data and the high bit sent at first." | verified |
| O3 | Later WS2812B revisions want a 280 us reset | `fw/ws2812.s:47` | a later Worldsemi datasheet revision (not fetched; S9 says 50 us) | none | unverified (from memory) |
| O4 | SWD: request and data parity are even parity (the bit is 1 when the count of ones is odd); the ACK is three bits sent least significant first; there is a turnaround whenever the driver of the wire changes | `fw/swd.s:14-23`, `tools/protomodels_swd.py:13-22`, `:202-212` | S10 B4.1.3 (line turnaround), B4.1.6 (parity), B4.2 (the ACK field) | "If the number of bits with a value of 0b1 is odd, the parity bit is set to 0b1."; "The ACK value is transmitted LSB-first on the wire" | verified |
| O5 | SWD connect: a line reset of at least 50 clocks with SWDIO high, and the JTAG-to-SWD select value 0xE79E sent least significant bit first | `fw/swd.s:11-12`, `:110` | S10 (the numbers were not found in the extracted text of S10: they sit in figures or tables the extraction lost, and no section is cited for them here) | none | unverified (from memory) |
| O6 | The SW-DP `DPIDR` 0x2BA01477 the model answers with | `tools/protomodels_swd.py:65` | an example value (a Cortex-M3/M4 SW-DP), not a requirement | none | unverified; nothing depends on the value |
| O7 | JTAG: the TAP state diagram; Test-Logic-Reset selects IDCODE (or BYPASS); bit 0 of an IDCODE is 1; BYPASS is all ones and one bit long; five TCKs with TMS high reach Test-Logic-Reset | `fw/jtag_master.s:1`, `tools/protomodels_jtag_master.py:10-20`, `:34-52` | IEEE Std 1149.1, figure 6-1 (not fetched) | none | unverified (from memory) |
| O8 | PS/2: the device clocks at 10 to 16.7 kHz; a clock low phase is 30 to 50 us; eleven-bit frames with odd parity; the host inhibits by holding CLK low for at least 100 us | `fw/ps2_host.s:25`, `:45-67`, `tools/protomodels_ps2_host.py:12-25` | IBM PS/2 hardware interface technical reference (not found) | none | unverified (from memory) |

## 5. IHP CMOS5L and the SG13G2 macro

Read from the files; the vendored macro files are in this repository and
the standard-cell liberty was fetched from the PDK's `dev` branch (S16).

| # | Fact | Used at | Source file | Source wording | Status |
|---|---|---|---|---|---|
| I1 | The macro is 236.8 by 118.78 um: 28,127 um^2 | `docs/AREA.md:26` and every run entry, `PLAN.md:35` (28,100), `macro/…/README.md` | `macro/RM_IHPSG13_1P_256x16_c2_bm_bist/RM_IHPSG13_1P_256x16_c2_bm_bist.lef:28`; `…_typ_1p20V_25C.lib:148` | "SIZE 236.8 BY 118.78 ;"; "area : 28127.104 ;" | verified (236.8 x 118.78 = 28,127.1, computed) |
| I2 | The macro's data outputs allow 64 fF | `docs/AREA.md:394-396`, `:419-420`, DECISIONS D-043 | `…_typ_1p20V_25C.lib:1457` (the same line in the slow and fast files) | "max_capacitance : 0.064 ;" | verified |
| I3 | The macro's corners: typical 1.20 V 25 C, slow 1.08 V 125 C, fast 1.32 V -55 C | `macro/…/README.md`, `docs/AREA.md:64` | the three `.lib` files, lines 34-35 | "nom_temperature : 125 ;" "nom_voltage : 1.08 ;" (slow) | verified. The standard cells' fast corner is -40 C (I6); AREA.md's "-40 C" is the standard-cell figure |
| I4 | The macro's clock-to-output at the slow corner is about 5 ns (AREA reports 5.38 ns on the path, 0.59 ns for the flop it replaced) | `docs/AREA.md:124-132`, DECISIONS D-029 ("5.4 ns") | `…_slow_1p08V_125C.lib:1474-1481` (`A_CLK` to `A_DOUT`, rising edge) | "values ("4.9940,4.9967,5.0089,5.0231,5.0757,5.1307"" (first row) | verified for the table; 5.38 ns is the run's own STA figure for that path (load and slew beyond the first row), not a PDK number |
| I5 | A flop with reset is 48.99 um^2 | `docs/AREA.md:4`, `docs/SERIALIZER.md:150` (130 flops = 6,400 um^2) | S16 `sg13cmos5l_stdcell_typ_1p20V_25C.lib:10107-10108` | "cell (sg13cmos5l_dfrbpq_1) { area : 48.9888;" | verified (130 x 48.99 = 6,369, computed) |
| I6 | Standard-cell corners: `typ_1p20V_25C`, `slow_1p08V_125C`, `fast_1p32V_m40C` | `docs/AREA.md:64`, `:78`, `:343` | S16, the file names in `ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/` | file names | verified |
| I7 | The maximum transition the cells are characterised for is 2.507 ns | `docs/AREA.md:347` | S16 `sg13cmos5l_stdcell_typ_1p20V_25C.lib:37` (the same in the slow file) | "default_max_transition : 2.5074;" | verified |
| I8 | CMOS5L has no SRAM of its own: `sg13cmos5l_sram` is a link to the SG13G2 macros | `macro/…/README.md`, DECISIONS D-025 | S16, GitHub contents API for `ihp-sg13cmos5l/libs.ref` | `sg13cmos5l_sram` has type `symlink` | verified |
| I9 | "Tiny Tapeout's organisers confirmed (2026-09-28) that macros may go on the CMOS5L shuttle" | `macro/…/README.md` | a message not in this repository | S18 FAQ: "Yes, you can use the IHP SRAM macros, including more than one" | verified against S18 (the competition's own statement); the dated message itself is unverified |

## 6. Tiny Tapeout

| # | Fact | Used at | Source | Source wording | Status |
|---|---|---|---|---|---|
| T1 | A tile is about 202 x 155 um; a 6x4 block is 1289.28 x 710.64 um (916,214 um^2) | `docs/AREA.md:11-12`, `PLAN.md:69` | S13 `info.yaml`; S14 `tech/ihp-sg13cmos5l/tile_sizes.yaml` | "A single tile is about 202x155 uM."; `6x4: "0 0 1289.28 710.64"` | verified (1289.28 x 710.64 = 916,214, computed). **Wrong in the repo, annotated**: `PLAN.md:69` still said 24 x 200 x 150 = 720,000 um^2; AREA.md has had the right figure since the first run |
| T2 | 8x4 is 1724.16 x 710.64 um (1,225,257 um^2) | DECISIONS D-045 | S14 `tile_sizes.yaml` | `8x4: "0 0 1724.16 710.64"` | verified |
| T3 | 24 pins: 8 inputs, 8 outputs, 8 bidirectional, plus clock and reset | `info.yaml:29-57`, `docs/info.md`, `docs/SUBMISSION.md:16` | S13 `info.yaml` (`ui[0..7]`, `uo[0..7]`, `uio[0..7]`); S18 FAQ | "24 digital pins: 8 inputs, 8 outputs and 8 bidirectional, plus clock and reset" | verified |
| T4 | Clock: the template's default is 20 ns (50 MHz); 60 MHz is confirmed to work on the board; the pads are estimated good for about 66 MHz | `src/config.json` (`CLOCK_PERIOD` 20), `info.yaml:8` (`clock_hz` 50,000,000), `docs/SERIALIZER.md:183-185`, `fw/uart.s:6` and `fw/i2c_master.s:14` (60 MHz examples) | S13 `src/config.json`; S18 FAQ | "The value is in nanoseconds, so 20ns == 50MHz."; "Tiny Tapeout has confirmed 60 MHz is OK"; "a max output frequency of around 66 MHz" | verified. S18 adds that these are estimates: "There's no CMOS5L silicon back yet" |
| T5 | The CMOS5L flow signs off timing at the typical corner `nom_typ_1p20V_25C` | DECISIONS D-028, `docs/AREA.md:110`, `:354` | S14 `tech.py` (both branches), `project.py:740-741` (`main`) | `tt_corner = "nom_typ_1p20V_25C"`; `…-openroad-stapostpnr/{tt_corner}/checks.rpt` | verified. D-028's other claim, that `TIMING_VIOLATION_CORNERS` is `*typ*`, is not in S14's `tech.py` or `project.py`; it is verified from the flow itself: `resolved.json` of run 37549369058 has `TIMING_VIOLATION_CORNERS: ['*typ*']` and `DEFAULT_CORNER: nom_typ_1p20V_25C` (which file of the flow sets it was not traced) |
| T6 | What precheck checks on CMOS5L: KLayout SG13CMOS5L DRC, pin label overlap, zero area, KLayout checks, pin check, boundary check, layer check, cell name check, analog pin check (nine) | `docs/AREA.md:74`, `:120`, HANDOFF ("precheck 9/9"), `docs/SUBMISSION.md` | S14 `precheck/precheck.py`, the `checks` list and its `techs` filters | names as listed: "KLayout SG13CMOS5L DRC", "KLayout zero area", "KLayout Checks", "Pin check", "Boundary check", "Layer check", "Cell name check", "Analog pin check", "KLayout pin label overlapping drawing" | verified on the branch this repo's workflow uses. **Changed upstream**: `main` adds a tenth, "Verilog syntax check" (D-046) |
| T7 | The demo board's microcontroller is an RP2350 running MicroPython with the Tiny Tapeout SDK | `docs/info.md:155-165`, `tools/keyerhost.py:204-212` | S19; S18 | "RP2350 microcontroller running MicroPython with the Tiny Tapeout SDK" | verified |
| T8 | Demo board GPIO map: `ui_in[0..7]` on GP17 to GP24, `uio[0..7]` on GP25 to GP32, `uo_out[0..7]` on GP33 to GP40, project clock GP16, reset GP14; so SCK GP17, MOSI GP18, CS_n GP19, MISO GP33, `ui[3]` GP20, `uo[4]` GP37 | `docs/info.md:163-164`, `:183`, `tools/keyerhost.py:207-209`, `:224` | S12 `gpio_map_dbv3.py`, class `GPIOMapTTDBv3`; `gpio_map.py` selects it | "UI_IN0 = 17", "UI_IN3 = 20", "UO_OUT0 = 33", "UO_OUT4 = 37"; "GPIOMap = GPIOMapTTDBv3" | verified against the SDK. The same file also has a `GPIOMapTTDBv3Alpha` with other numbers (`UI_IN3 = 15`, `UO_OUT4 = 34`): an alpha board would need `pins=` in the driver. The KiCad schematic (`TinyTapeout/tt-demo-pcb`, `main` at `bbf3dd1f77`) was not read |
| T9 | On the RP2350, GP20 can be UART1 TX and GP37 UART1 RX | `docs/info.md:182-198` | S11, "GPIO Bank 0 Functions" table, function F2 | "20 SPI0 RX UART1 TX I2C0 SDA PWM2 A"; "37 SPI0 CSn UART1 RX I2C0 SCL PWM10 B" | verified. With T8 this closes HANDOFF's open item: the pin claim holds on the SDK's map, not on the datasheet alone. GP37 exists only on the 80-pin RP2350B; S12's map uses GPIOs up to 40, so the board has one |
| T10 | A deselected design is powered down and loses its state | `docs/info.md` (How to test: load after select), `tools/keyerhost.py` (`setup`) | S18 FAQ | "When your design is deselected it's powered down and loses its state" | verified |
| T11 | The template's `gds` workflow uses `TinyTapeout/tt-gds-action@ttihp27a` | `.github/workflows/gds.yaml:30-60` (this repo: `@ihp-cmos5l`) | S13 `.github/workflows/gds.yaml`; S15 | "uses: TinyTapeout/tt-gds-action@ttihp27a" | verified. **Changed upstream since session 7**: the repo's jobs are the template's of September. Open for Ahan as D-046 |

## 7. The competition

| # | Fact | Used at | Source | Source wording | Status |
|---|---|---|---|---|---|
| C1 | Deadline 18 January 2027 | `CLAUDE.md:3`, `PLAN.md:3`, `docs/HANDOFF.md` schedule | S18, "The rules" | "Submit your design by January 18th, 2027" | verified |
| C2 | Process and shuttle: IHP 130 nm CMOS5L through Tiny Tapeout, March 2027 shuttle | `CLAUDE.md:3-4`, `README.md:6`, `PLAN.md:4` | S18 | "IHP's 130nm CMOS5L process"; "March 2027 CMOS5L shuttle" | verified |
| C3 | **Area: the maximum is 8x4 tiles, and the post says to set 8x4** | `info.yaml:11` (`tiles: "6x4"`), `CLAUDE.md:4`, `README.md:6`, `PLAN.md:4`, `:53`, `docs/SUBMISSION.md:35` | S18, update of November 9th and "The rules" | "8x4 tiles are now confirmed for the March 2027 shuttle"; "Set the tile size in info.yaml to 8x4."; "The maximum area is 8x4 tiles per design." | verified. **Changed since session 7** (the repo was built to 6x4). Not changed here: it is Ahan's decision (D-045) |
| C4 | Judging: no fixed weighting; flexibility first; then the protocols shown, what is unique, design and verification; the write-up weighs heavily | `docs/SUBMISSION.md` (sections 1 to 3), `PLAN.md` | S18 FAQ "How will entries be judged?" | "There's no fixed weighting. Flexibility is the main thing"; "We'll also weigh your write-up heavily" | verified |
| C5 | What to submit: a public repository with RTL, tests and a passing GDS and precheck; architecture and instruction-set documentation; the assembler with example programs; a verification write-up | `docs/SUBMISSION.md`, HANDOFF schedule | S18 FAQ "What do I submit?" | "A link to your public repository with your RTL, tests and a passing GDS/precheck" | verified |
| C6 | AI tools are allowed and their use is to be described | `README.md` (Status), `docs/SUBMISSION.md` section 5 (added this session) | S18 FAQ "Can I use AI tools?" | "Yes. Please describe how you used them in your write-up." | verified. **Missing in the repo, added**: SUBMISSION.md had no such description (BUGS 54) |
| C7 | Protocol targets: UART, SPI and I2C first; low-speed USB and 10 Mbit Ethernet as stretch goals; JTAG, SWD, PS/2, CAN named as interesting | `README.md`, `docs/SUBMISSION.md:16-20`, `PLAN.md` | S18, "The challenge" | "Stretch goals include low-speed USB and 10Mbit Ethernet." | verified |
| C8 | Contact | `docs/SUBMISSION.md` (header, added this session) | S18 | "reach out to asic-competition@janestreet.com" | verified |
| C9 | Results in early February; about four designs are taped out; winners get the chip on a demo board; small non-functional fixes after the deadline are allowed | `docs/HANDOFF.md` schedule | S18 | "announce the selected designs in early February"; "tape out around four designs"; "small non-functional fixes, like re-hardening for a newer flow" | verified |

## 8. GitHub Actions

| # | Fact | Used at | Source | Source wording | Status |
|---|---|---|---|---|---|
| G1 | A job may run for 6 hours | `.github/workflows/mutation.yaml:7-9` ("far inside the 6 h limit"; `timeout-minutes: 300`), DECISIONS D-031 | S17, "Job execution time"; also S18 ("GitHub Actions' 6-hour limit") | "Each job in a workflow can run for up to 6 hours of execution time." | verified |
| G2 | A matrix may generate 256 jobs; the Free plan runs 20 standard jobs at once | `.github/workflows/mutation.yaml:31-33` (16 shards) | S17, "Job Matrix" and the concurrency table | "256 jobs / workflow run"; Free: "20" total concurrent jobs | verified (16 is inside both) |
| G3 | Artifacts of a run are kept 90 days | HANDOFF ("one download per run") | GitHub REST API, artifacts of run 37549369058 | `expires_at: 2027-01-04T23:57:28Z` for a run of 2026-10-06 | verified (90 days, computed). The October runs' logs are gone on 2027-01-04, the planned submission day: AREA.md already holds their numbers |
| G4 | "the account has an egress limit" | `docs/HANDOFF.md` (task 1 of session 7) | none found: S17 lists storage and compute, no download limit | none | unverified (HANDOFF now says so). `GDS_logs` is 147 MB compressed, about 1 GB unpacked |

## What changed because of this audit

- U10: `fw/usb_ls_device.s` said the host has 6.5 bit times to answer; USB
  gives it 7.5. Comment corrected; `test_host_handshake_at_the_latest_allowed_time`
  holds the firmware at 7.5 (BUGS 52). No instruction changed.
- U9, U24: the USB host model's response limit (6.5) and jitter limit
  (25 ns) are stricter than what the specification asks of a low-speed
  device (7.5 and 95 ns). Kept, and labelled in the model's header.
- U3: the question HANDOFF carried ("check what a low-speed hub may drop")
  is closed: nothing. The receiver keeps its eight-bit SYNC rule; no spec,
  model or RTL change.
- T1: `PLAN.md`'s early tile estimate is marked superseded (BUGS 53).
- T5: D-028's `TIMING_VIOLATION_CORNERS` claim, which could not be found in
  the tool source, is confirmed from a run's `resolved.json`.
- C6, C8: `docs/SUBMISSION.md` gains a draft of the description of AI use
  the post asks for, and the contact address (BUGS 54).
- C3, T11: two upstream changes of 2026-10-28 to 2026-11-09 (8x4 tiles; the
  template's `ttihp27a` action with a tenth precheck) are open decisions
  D-045 and D-046. Nothing under `src/`, `info.yaml` or `.github/` was
  changed by the audit.
- Every `unverified` row is marked in the file that uses it:
  `tools/protomodels_eth_10bt.py`, `fw/eth_10bt_tx.s`, `docs/SERIALIZER.md`
  (802.3: E1 to E6, E9 to E11, E13); `tools/protomodels.py` (UART, SPI,
  24Cxx: P1, P3, P8); `fw/i2c_slave.s` (SMBus: P7); `fw/ws2812.s` (280 us:
  O3); `fw/swd.s`, `tools/protomodels_swd.py` (connect sequence: O5);
  `fw/jtag_master.s`, `tools/protomodels_jtag_master.py` (1149.1: O7);
  `fw/ps2_host.s`, `tools/protomodels_ps2_host.py` (PS/2: O8);
  `docs/HANDOFF.md` (egress: G4). O6 and I9's dated message have nothing
  depending on them.

No row came out wrong in a way that touches the contract, the model or the
RTL: the wrong rows were a firmware comment (U10), a planning estimate (T1)
and the picture of the competition's rules (C3, C6, T11).
