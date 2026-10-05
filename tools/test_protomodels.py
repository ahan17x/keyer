"""Published and hand-built vectors for the protocol models.

The protocol models (tools/protomodels_usb_host.py, protomodels_eth_10bt.py)
judge the firmware, and until now only the firmware exercised them. These
tests check their coding functions and their decoders against values that
do not come from this project:

- USB 2.0 section 8.3.5: the generator polynomials and the residuals a
  receiver must find (CRC-5 `01100`, CRC-16 `1000000000001101`). The
  section itself gives no worked numbers; the worked examples are those of
  the USB-IF white paper "Cyclic Redundancy Checks in USB" (crcdes.pdf),
  which 8.3.5 implementations are customarily checked against: three token
  fields and two data fields, as bit strings in the order sent.
- CRC-32 (IEEE 802.3): the catalogue check value, CRC-32("123456789") =
  0xCBF43926, and one published frame with its FCS: the UDP frame of
  fpga4fun.com's 10BASE-T pages ("Ethernet checksum: B3 31 88 1B").
- USB packets built by hand from USB 2.0 chapter 8 (fields, least
  significant bit first) and 7.1.8, 7.1.9 (NRZI: a 0 is a transition, a 1
  is none; a 0 is inserted after six ones, also when they are the last
  bits before the end of packet): the J/K strings below were worked out on
  paper, bit by bit, as the comments show.

The reference CRC here (`_crc_msb`) is written in the white paper's form, a
shift-left register over the bits in the order sent, not in the reflected
form the models (and the serializer) use.
"""

import binascii
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protomodels_eth_10bt as eth  # noqa: E402
import protomodels_usb_host as usb  # noqa: E402


def bits_of(s):
    return [int(c) for c in s if c in "01"]


def _crc_msb(bits, poly, n, complement=True):
    """The white paper's shift register: all ones to start, the bits in the
    order sent, the result read most significant bit first (the order it is
    sent in). complement=False gives the receiver's residual."""
    r = (1 << n) - 1
    for b in bits:
        top = (r >> (n - 1)) & 1
        r = (r << 1) & ((1 << n) - 1)
        if top ^ b:
            r ^= poly
    if complement:
        r ^= (1 << n) - 1
    return "".join(str((r >> (n - 1 - i)) & 1) for i in range(n))


def sent(value, n):
    """An n-bit field as the string of bits in the order sent (bit 0 first)."""
    return "".join(str((value >> i) & 1) for i in range(n))


# ---------------------------------------------------------------- USB CRC-5

# (what, the 11 bits in the order sent, the CRC in the order sent)
CRC5_EXAMPLES = (
    ("setup addr 15 endp e", "10101000111", "10111"),
    ("out addr 3a endp a", "01011100101", "11100"),
    ("in addr 70 endp 4", "00001110010", "01110"),
)


def test_crc5_white_paper_examples():
    for what, data, crc in CRC5_EXAMPLES:
        assert _crc_msb(bits_of(data), 0x05, 5) == crc, what        # the reference itself
        assert sent(usb.crc5(bits_of(data)), 5) == crc, what


def test_crc5_token_fields_and_residual():
    # the same three tokens through token_bytes(): ADDR 7 bits, ENDP 4 bits, CRC5
    for (what, data, crc), (pid, addr, ep) in zip(CRC5_EXAMPLES, (("SETUP", 0x15, 0xE), ("OUT", 0x3A, 0xA),
                                                                  ("IN", 0x70, 0x4))):
        b = usb.token_bytes(pid, addr, ep)
        assert b[0] == usb.PID[pid]
        assert sent(b[1] | b[2] << 8, 16) == data + crc, what
        # USB 2.0 8.3.5.1: the receiver's residual over field and CRC is 01100
        assert _crc_msb(bits_of(data + crc), 0x05, 5, complement=False) == "01100"
    # the tokens every USB trace starts with: SETUP and IN to address 0, endpoint 0
    assert usb.token_bytes("SETUP", 0, 0) == [0x2D, 0x00, 0x10]
    assert usb.token_bytes("IN", 0, 0) == [0x69, 0x00, 0x10]
    # a flipped CRC bit no longer leaves the residual
    bad = usb.token_bytes("SETUP", 0x15, 0xE, bad_crc5=True)
    assert _crc_msb(bits_of(sent(bad[1] | bad[2] << 8, 16)), 0x05, 5, complement=False) != "01100"


def test_pids_carry_their_check_field():
    """USB 2.0 8.3.1, Table 8-1: the upper four bits of a PID byte are the
    complement of the lower four; the lower four as the table lists them."""
    table = {"OUT": 0b0001, "IN": 0b1001, "SOF": 0b0101, "SETUP": 0b1101, "DATA0": 0b0011, "DATA1": 0b1011,
             "ACK": 0b0010, "NAK": 0b1010, "STALL": 0b1110, "PRE": 0b1100}
    assert set(usb.PID) == set(table)
    for name, low in table.items():
        assert usb.PID[name] == low | ((low ^ 0xF) << 4), name


# --------------------------------------------------------------- USB CRC-16

CRC16_EXAMPLES = (
    ([0x00, 0x01, 0x02, 0x03], "00000000100000000100000011000000", "1111011101011110"),
    ([0x23, 0x45, 0x67, 0x89], "11000100101000101110011010010001", "0111000000111000"),
)


def test_crc16_white_paper_examples():
    for data, bits, crc in CRC16_EXAMPLES:
        assert "".join(str(b) for b in usb.lsb_bits(data)) == bits
        assert _crc_msb(bits_of(bits), 0x8005, 16) == crc
        assert sent(usb.crc16(data), 16) == crc
        # USB 2.0 8.3.5.2: the residual over data and CRC
        assert _crc_msb(bits_of(bits + crc), 0x8005, 16, complement=False) == "1000000000001101"
        # and through data_bytes(): PID, the data, the CRC low byte first
        p = usb.data_bytes(1, data)
        assert p[0] == 0x4B and p[1:-2] == data
        assert "".join(str(b) for b in usb.lsb_bits(p[-2:])) == crc


def test_crc16_known_packets():
    # CRC-16/USB check value of the CRC catalogue
    assert usb.crc16(b"123456789") == 0xB4C8
    # the first DATA0 of nearly every enumeration: GET_DESCRIPTOR(device), wLength 64
    assert usb.data_bytes(0, usb.setup_packet(0x80, 6, 0x0100, 0, 64)) == \
        [0xC3, 0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x40, 0x00, 0xDD, 0x94]
    # a zero-length DATA1 (the status stage): the CRC of nothing is 0x0000
    assert usb.data_bytes(1, []) == [0x4B, 0x00, 0x00]
    assert usb.data_bytes(0, [0x00, 0x01, 0x02, 0x03], corrupt_crc=True)[-2:] != \
        usb.data_bytes(0, [0x00, 0x01, 0x02, 0x03])[-2:]


# ------------------------------------------------- USB packets on the wire

def states(s):
    """'KJ0' text to the model's line states (0 is SE0)."""
    return [{"J": usb.J, "K": usb.K, "0": usb.SE0}[c] for c in s if c in "JK0"]


# SETUP token to address 0, endpoint 0: bytes 2D 00 10. From J, a 0 toggles
# the line and a 1 leaves it:
#   SYNC  00000001            K J K J K J K K
#   2D    1 0 1 1 0 1 0 0     K J J J K K J K
#   00    0 0 0 0 0 0 0 0     J K J K J K J K
#   10    0 0 0 0 1 0 0 0     J K J K K J K J
#   EOP                       SE0 SE0 J
# No run of six ones, so nothing is stuffed.
SETUP_0_0 = "KJKJKJKK KJJJKKJK JKJKJKJK JKJKKJKJ 00J"

# DATA0 with the one payload byte FF: bytes C3 FF 00 FF (the CRC-16 of FF
# is FF00, sent 00 then FF). Ones are counted from the start of SYNC:
#   SYNC  00000001            K J K J K J K K        (1 one)
#   C3    1 1 0 0 0 0 1 1     K K J K J K K K        (2 ones at the end)
#   FF    1 1 1 1             K K K K                (6 ones)
#         stuffed 0           J
#         1 1 1 1             J J J J
#   00    0 0 0 0 0 0 0 0     K J K J K J K J
#   FF    1 1 1 1 1 1         J J J J J J            (6 ones)
#         stuffed 0           K
#         1 1                 K K
#   EOP                       SE0 SE0 J
DATA0_FF = "KJKJKJKK KKJKJKKK KKKK J JJJJ KJKJKJKJ JJJJJJ K KK 00J"

# DATA0 with the payload byte F9: bytes C3 F9 80 FD (CRC-16 FD80). The
# packet ends in six ones, so the stuffed 0 is the last bit before the end
# of packet (USB 2.0 7.1.9):
#   SYNC  00000001            K J K J K J K K
#   C3    1 1 0 0 0 0 1 1     K K J K J K K K
#   F9    1 0 0 1 1 1 1 1     K J K K K K K K        (5 ones at the end)
#   80    0 0 0 0 0 0 0 1     J K J K J K J J
#   FD    1 0 1 1 1 1 1 1     J K K K K K K K        (6 ones)
#         stuffed 0           J
#   EOP                       SE0 SE0 J
DATA0_F9 = "KJKJKJKK KKJKJKKK KJKKKKKK JKJKJKJJ JKKKKKKK J 00J"


def test_setup_token_on_the_wire():
    assert usb.encode(usb.token_bytes("SETUP", 0, 0)) == states(SETUP_0_0)
    assert len(states(SETUP_0_0)) == 32 + 3


def test_data0_bit_stuffing_on_the_wire():
    assert usb.crc16([0xFF]) == 0xFF00 and usb.crc16([0xF9]) == 0xFD80
    assert usb.encode(usb.data_bytes(0, [0xFF])) == states(DATA0_FF)
    assert usb.encode(usb.data_bytes(0, [0xF9])) == states(DATA0_F9)
    # 40 data bits, two stuffed bits (one in the second case), the EOP
    assert len(states(DATA0_FF)) == 40 + 2 + 3 and len(states(DATA0_F9)) == 40 + 1 + 3
    # a stuffed line never holds for more than six bit times before the EOP
    for text in (SETUP_0_0, DATA0_FF, DATA0_F9):
        s = states(text)[:-3]
        run = longest = 1
        for a, b in zip(s, s[1:]):
            run = run + 1 if a == b else 1
            longest = max(longest, run)
        assert longest <= 7                          # six ones after a 0: seven equal symbols at most
    # without stuffing the FF packet is 40 symbols and holds K for ten
    raw = usb.encode(usb.data_bytes(0, [0xFF]), stuff=False)
    assert len(raw) == 40 + 3 and raw[14:24] == [usb.K] * 10


def _runs(text, T, start):
    """The hand-built packet as the host model records a device's packet:
    (state, first cycle, cycles) per run of equal states, EOP included."""
    runs, t = [], start
    for s in states(text):
        if runs and runs[-1][0] == s:
            runs[-1][2] += T
        else:
            runs.append([s, t, T])
        t += T
    return [tuple(r) for r in runs]


def test_host_model_decodes_the_hand_built_packets():
    """The decoder the firmware's packets go through (UsbLsHost._analyse:
    NRZI, unstuffing, PID check, CRC-16) on the hand-built packets, as if a
    device had sent them three bit times after the host's packet."""
    for text, want in ((DATA0_FF, [0xC3, 0xFF, 0x00, 0xFF]), (DATA0_F9, [0xC3, 0xF9, 0x80, 0xFD])):
        host = usb.UsbLsHost(bit_cycles=32)
        runs = _runs(text, 32, 1000)
        assert runs[-1][0] == usb.J and runs[-2][0] == usb.SE0
        pkt = usb.Packet(1000)
        host._analyse(pkt, runs[:-1], usb.J, 1000 - 3 * 32)
        assert pkt.bytes == want and pkt.name == "DATA0" and pkt.ok, (pkt.bytes, pkt.errors, host.errors)
        assert pkt.payload == want[1:-2]
    # the same packet with one CRC bit wrong (FD -> FC: the last byte's bit 0,
    # which also removes the stuffed bit) is reported, not accepted
    host = usb.UsbLsHost(bit_cycles=32)
    bad = "KJKJKJKK KKJKJKKK KJKKKKKK JKJKJKJJ KJJJJJJJ 00J"       # C3 F9 80 FC
    pkt = usb.Packet(1000)
    host._analyse(pkt, _runs(bad, 32, 1000)[:-1], usb.J, 1000 - 3 * 32)
    assert pkt.bytes == [0xC3, 0xF9, 0x80, 0xFC] and not pkt.ok


# ------------------------------------------------------------ Ethernet CRC-32

# fpga4fun.com, "10BASE-T FPGA interface": a UDP frame from 00:12:34:56:78:90
# to 00:10:A4:7B:EA:80, 192.168.0.44 port 1024 to 192.168.0.4 port 1024, 18
# payload bytes 00 .. 11, and the page's "Ethernet checksum: B3 31 88 1B".
PUBLISHED_FRAME = bytes.fromhex(
    "0010A47BEA80" "001234567890" "0800"                # destination, source, type
    "4500002EB3FE00008011" "0540" "C0A8002C" "C0A80004"   # IP header
    "04000400001A2DE8"                                    # UDP header
    "000102030405060708090A0B0C0D0E0F1011")               # payload
PUBLISHED_FCS = bytes.fromhex("B331881B")


def test_crc32_check_value_and_published_frame():
    assert eth.crc32(b"123456789") == 0xCBF43926
    assert len(PUBLISHED_FRAME) == 60
    assert eth.crc32(PUBLISHED_FRAME) == int.from_bytes(PUBLISHED_FCS, "little") == 0x1B8831B3
    assert eth.crc32(PUBLISHED_FRAME) == binascii.crc32(PUBLISHED_FRAME)
    f = eth.frame_bytes(PUBLISHED_FRAME[0:6], PUBLISHED_FRAME[6:12], 0x0800, PUBLISHED_FRAME[14:])
    assert bytes(f) == PUBLISHED_FRAME + PUBLISHED_FCS
    # 802.3 3.2.9 as a receiver sees it: the register over frame and FCS
    # ends at the residual 0xDEBB20E3 (before the final complement)
    assert eth.crc32(PUBLISHED_FRAME + PUBLISHED_FCS) ^ 0xFFFFFFFF == 0xDEBB20E3
    # the IP header of the published frame sums to FFFF (RFC 791), a check
    # on the transcription of the frame itself
    ip = PUBLISHED_FRAME[14:34]
    s = sum(ip[i] << 8 | ip[i + 1] for i in range(0, 20, 2))
    assert (s & 0xFFFF) + (s >> 16) == 0xFFFF


def test_receiver_model_accepts_the_published_frame():
    """The 10BASE-T receiver model on the published frame as a Manchester
    waveform (40 MHz: two cycles per half-bit): it recovers the bytes, finds
    the FCS good, and rejects the frame with one payload bit flipped."""
    def receive(body):
        rx = eth.Eth10BTReceiver()
        for i, (p, n) in enumerate(eth.wave(list(body), lead=40, trail=400)):
            rx.sample(i, p, n)
        rx.finish()
        return rx

    rx = receive(PUBLISHED_FRAME + PUBLISHED_FCS)
    assert len(rx.frames) == 1
    f = rx.frames[0]
    assert bytes(f.bytes) == PUBLISHED_FRAME + PUBLISHED_FCS
    assert f.fcs_ok and f.fcs == 0x1B8831B3 and f.type == 0x0800
    assert bytes(f.dst) == bytes.fromhex("0010A47BEA80") and bytes(f.src) == bytes.fromhex("001234567890")
    assert bytes(f.payload) == PUBLISHED_FRAME[14:]
    assert f.ok

    flipped = bytearray(PUBLISHED_FRAME + PUBLISHED_FCS)
    flipped[45] ^= 0x10
    rx = receive(bytes(flipped))
    assert len(rx.frames) == 1 and not rx.frames[0].fcs_ok
    assert not rx.frames[0].ok and not receive(PUBLISHED_FRAME + PUBLISHED_FCS).frames[0].errors
