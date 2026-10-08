"""Write a minimal VobSub pair OUT.idx / OUT.sub with cues at the given times (seconds).
Each cue: a 16x2 solid rectangle shown for 2 s. usage: mkvobsub.py OUT t1 t2 ..."""
import struct, sys

def pts_bytes(pts):  # 33-bit PTS, '0010' prefix
    return bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
                  (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE)])

def pack_header(scr):
    # MPEG-2 pack header with SCR base `scr`, ext 0, mux rate 0x0189C3, no stuffing
    s = scr
    h = bytes([0x44 | ((s >> 27) & 0x38) | ((s >> 28) & 0x03), (s >> 20) & 0xFF,
               0x04 | ((s >> 12) & 0xF8) | ((s >> 13) & 0x03), (s >> 5) & 0xFF, 0x04 | ((s << 3) & 0xF8), 0x01,
               0x01, 0x89, 0xC3, 0xF8])
    return b"\x00\x00\x01\xba" + h

def spu(duration_s):
    rle_top = b"\x00\x01"        # whole line colour 1
    rle_bot = b"\x00\x01"
    data_off = 4
    top_off, bot_off = data_off, data_off + len(rle_top)
    ctrl1 = data_off + len(rle_top) + len(rle_bot)
    # first control sequence: start display, palette, alpha, coords, rle offsets
    cmds1 = (b"\x01" + b"\x03\x32\x10" + b"\x04\xff\xf0" +
             b"\x05" + bytes([0x00, 0x00, 0x0F, 0x00, 0x00, 0x01]) +   # x 0..15, y 0..1
             b"\x06" + struct.pack(">HH", top_off, bot_off) + b"\xff")
    ctrl2 = ctrl1 + 4 + len(cmds1)
    seq1 = struct.pack(">HH", 0, ctrl2) + cmds1
    delay = int(duration_s * 90000 / 1024)
    seq2 = struct.pack(">HH", delay, ctrl2) + b"\x02\xff"
    body = rle_top + rle_bot + seq1 + seq2
    total = 4 + len(body)
    if total % 2:
        body += b"\xff"; total += 1
    return struct.pack(">HH", total, ctrl1) + body

def pes(pts, payload):
    hdr = b"\x81\x80\x05" + pts_bytes(pts)
    body = hdr + b"\x20" + payload
    return b"\x00\x00\x01\xbd" + struct.pack(">H", len(body)) + body

out = sys.argv[1]
times = [float(t) for t in sys.argv[2:]]
sub = bytearray()
idx = ["# VobSub index file, v7 (do not modify this line!)", "size: 720x576", "org: 0, 0",
       "scale: 100%, 100%", "alpha: 100%", "smooth: OFF", "fadein/out: 0, 0", "align: OFF at LEFT TOP",
       "time offset: 0", "forced subs: OFF",
       "palette: 000000, ffffff, 808080, ff0000, 00ff00, 0000ff, ffff00, ff00ff, 00ffff, 101010, 202020, 303030, 404040, 505050, 606060, 707070",
       "custom colors: OFF, tridx: 0000, colors: 000000, 000000, 000000, 000000", "langidx: 0", "id: en, index: 0"]
for t in times:
    pos = len(sub)
    pts = int(round(t * 90000))
    chunk = pack_header(pts) + pes(pts, spu(2.0))
    chunk += b"\x00" * ((-len(chunk)) % 2048)          # pad to a 2048-byte sector (zeros are padding-ish)
    sub += chunk
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    idx.append(f"timestamp: {h:02}:{m:02}:{s:02}:{ms:03}, filepos: {pos:09x}")
open(out + ".sub", "wb").write(bytes(sub))
open(out + ".idx", "w").write("\n".join(idx) + "\n")
