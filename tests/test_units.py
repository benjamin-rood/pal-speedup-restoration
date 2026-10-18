#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Unit tests for the pure functions of pal_restore.py: no media, no FFmpeg, under a second.

    tests/test_units.py            (or: uv run -q --with numpy python tests/test_units.py)

Expected values are computed here from exact fractions, not copied from the code's output.
"""
import importlib.util
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pal_restore", HERE.parent / "pal_restore.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

RATIO = Fraction(25025, 24000)


def ms_of(h: int, m: int, s: int, ms: int) -> int:
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def scaled(ms: int) -> int:
    return round(ms * RATIO)


class ScaleAndFormat(unittest.TestCase):
    def test_scale_error_is_at_most_half_a_millisecond(self):
        for ms in range(0, 7_200_000, 997):
            self.assertLessEqual(abs(pr._scale_ms(ms, RATIO) - ms * RATIO), Fraction(1, 2), ms)

    def test_scale_is_monotone_and_zero_stays_zero(self):
        self.assertEqual(pr._scale_ms(0, RATIO), 0)
        values = [pr._scale_ms(ms, RATIO) for ms in range(0, 5000)]
        self.assertEqual(values, sorted(values))

    def test_fmt_hms_milliseconds(self):
        self.assertEqual(pr._fmt_hms(ms_of(1, 2, 3, 4), 3, ",", 2, False), "01:02:03,004")
        self.assertEqual(pr._fmt_hms(0, 3, ".", 2, False), "00:00:00.000")

    def test_fmt_hms_hours_beyond_two_digits_are_not_truncated(self):
        self.assertEqual(pr._fmt_hms(ms_of(100, 0, 0, 0), 3, ",", 2, False), "100:00:00,000")

    def test_fmt_hms_centiseconds(self):
        self.assertEqual(pr._fmt_hms(990, 2, ".", 1, False), "0:00:00.99")
        self.assertEqual(pr._fmt_hms(ms_of(0, 59, 59, 990), 2, ".", 1, False), "0:59:59.99")

    def test_scale_to_centiseconds_rounds_once_from_the_exact_value(self):
        # 2.000 s scaled is 2.0854 s = 208.54 cs: 209, not 208 (which rounding 2085 ms again gives).
        self.assertEqual(pr._scale_ms(2000, RATIO, 10), 2090)
        for ms in range(0, 3_600_000, 7919):
            self.assertLessEqual(abs(pr._scale_ms(ms, RATIO, 10) - ms * RATIO), 5)

    def test_fmt_hms_hourless_drops_hours_only_when_zero(self):
        self.assertEqual(pr._fmt_hms(ms_of(0, 1, 1, 0), 3, ".", 2, True), "01:01.000")
        self.assertEqual(pr._fmt_hms(ms_of(1, 0, 1, 0), 3, ".", 2, True), "01:00:01.000")


class SplitAndRead(unittest.TestCase):
    def test_split_lines_loses_nothing_and_splits_only_at_cr_lf_crlf(self):
        text = "a\r\nb\nc\rd\x0be f\x85g"
        parts = pr._split_lines(text)
        self.assertEqual("".join(parts), text)
        self.assertEqual(parts, ["a\r\n", "b\n", "c\r", "d\x0be f\x85g"])

    def test_split_lines_without_final_newline(self):
        self.assertEqual(pr._split_lines("a\nb"), ["a\n", "b"])
        self.assertEqual(pr._split_lines(""), [])

    def test_read_text_detects_byte_order_marks_and_falls_back_to_latin1(self):
        cases = [(b"\xef\xbb\xbf" + "é".encode("utf-8"), "utf-8"),
                 (b"\xff\xfe" + "é".encode("utf-16-le"), "utf-16-le"),
                 (b"\xfe\xff" + "é".encode("utf-16-be"), "utf-16-be"),
                 ("é".encode("latin-1"), "latin-1")]
        with tempfile.TemporaryDirectory() as d:
            for data, enc in cases:
                f = Path(d) / "x.srt"
                f.write_bytes(data)
                text, bom, got = pr._read_text(f)
                self.assertEqual((text, got), ("é", enc))
                self.assertEqual(bom + text.encode(enc), data)

    def test_retime_sidecar_keeps_byte_order_mark_and_encoding(self):
        src = "1\r\n00:00:01,000 --> 00:00:02,000\r\nhé\r\n"
        with tempfile.TemporaryDirectory() as d:
            for bom, enc in pr._BOMS:
                a, b = Path(d) / "a.srt", Path(d) / "b.srt"
                a.write_bytes(bom + src.encode(enc))
                pr.retime_sidecar(a, b, RATIO)
                out = b.read_bytes()
                self.assertTrue(out.startswith(bom))
                self.assertEqual(out[len(bom):].decode(enc),
                                 src.replace("00:00:01,000 --> 00:00:02,000", "00:00:01,043 --> 00:00:02,085"))


class RetimeSrtVtt(unittest.TestCase):
    def test_srt_timing_lines_scale_and_everything_else_is_untouched(self):
        t1, t2 = ms_of(0, 1, 2, 3), ms_of(0, 1, 5, 0)
        text = (f"1\n00:01:02,003 --> 00:01:05,000\nline with 00:00:09,000 --> 00:00:10,000 in text? no\n"
                "00:00:01,000\n\n")
        got = pr.retime_text(".srt", text, RATIO)
        fmt = lambda ms: pr._fmt_hms(ms, 3, ",", 2, False)
        self.assertEqual(got.split("\n")[1], f"{fmt(scaled(t1))} --> {fmt(scaled(t2))}")
        self.assertEqual(got.split("\n")[2], text.split("\n")[2])      # text line, never a timing line
        self.assertEqual(got.split("\n")[3], "00:00:01,000")           # a lone timestamp is not a cue

    def test_srt_line_endings_are_preserved_per_line(self):
        text = "1\r\n00:00:01,000 --> 00:00:02,000\r\nx\n2\r00:00:03,000 --> 00:00:04,000\rz"
        got = pr.retime_text(".srt", text, RATIO)
        self.assertEqual(got.count("\r\n"), 2)
        self.assertEqual(got.count("\n"), 3)
        self.assertEqual(got.count("\r"), 4)

    def test_ratio_one_changes_nothing(self):
        text = "1\n00:00:01,000 --> 00:00:02,500\nx\n\n2\n01:00:00,001 --> 100:00:00,999\ny\n"
        self.assertEqual(pr.retime_text(".srt", text, Fraction(1)), text)

    def test_srt_dot_separator_and_short_fraction_are_accepted(self):
        got = pr.retime_text(".srt", "00:00:01.000 --> 00:00:02.000\n", RATIO)
        self.assertEqual(got, "00:00:01.043 --> 00:00:02.085\n")

    def test_vtt_hourless_stays_hourless_and_headers_and_settings_survive(self):
        text = ("WEBVTT\n\nNOTE 00:00:01.000 --> 00:00:02.000 is a note\n\n"
                "01:00.000 --> 01:02.000 align:start position:10%\nhello\n\n"
                "00:01:00.000 --> 00:01:02.000\nhi\n")
        got = pr.retime_text(".vtt", text, RATIO).split("\n")
        self.assertEqual(got[0], "WEBVTT")
        self.assertEqual(got[2], "NOTE 00:00:01.000 --> 00:00:02.000 is a note")
        fmt = lambda ms: pr._fmt_hms(ms, 3, ".", 2, True)
        self.assertEqual(got[4], f"{fmt(scaled(60_000))} --> {fmt(scaled(62_000))} align:start position:10%")
        full = lambda ms: pr._fmt_hms(ms, 3, ".", 2, False)
        self.assertEqual(got[7], f"{full(scaled(60_000))} --> {full(scaled(62_000))}")
        self.assertEqual(got[7], "00:01:02.562 --> 00:01:04.648")


class RetimeAss(unittest.TestCase):
    HEAD = "[Script Info]\nTitle: x\n\n[Events]\n"

    def test_reordered_format_scales_the_named_fields_and_keeps_commas_in_text(self):
        text = (self.HEAD + "Format: End, Start, Layer, Text\n"
                "Dialogue: 0:00:02.00,0:00:01.00,0,Hello, 0:00:09.00, world\n")
        got = pr.retime_text(".ass", text, RATIO).split("\n")[5]
        self.assertEqual(got, "Dialogue: 0:00:02.09,0:00:01.04,0,Hello, 0:00:09.00, world")

    def test_start_and_end_scaled_in_centiseconds_and_text_untouched(self):
        text = (self.HEAD + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,at 0:00:05.00, really\n"
                "Comment: 0,1:00:00.00,1:00:01.50,Default,,0,0,0,,note\n")
        got = pr.retime_text(".ass", text, RATIO).split("\n")
        self.assertEqual(got[5], "Dialogue: 0,0:00:01.04,0:00:02.09,Default,,0,0,0,,at 0:00:05.00, really")
        e0 = scaled(ms_of(1, 0, 0, 0))
        e1 = scaled(ms_of(1, 0, 1, 500))
        want = f"Comment: 0,{pr._fmt_hms(e0, 2, '.', 1, False)},{pr._fmt_hms(e1, 2, '.', 1, False)},Default,,0,0,0,,note"
        self.assertEqual(got[6], want)

    def test_no_format_line_uses_the_default_field_order(self):
        text = "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,x\n"
        self.assertEqual(pr.retime_text(".ssa", text, RATIO),
                         "[Events]\nDialogue: 0,0:00:01.04,0:00:02.09,Default,,0,0,0,,x\n")

    def test_lines_outside_the_events_section_are_untouched(self):
        text = ("[Script Info]\nDialogue: 0,0:00:01.00,0:00:02.00,x,,0,0,0,,not an event\n"
                "[V4+ Styles]\nDialogue: 0,0:00:01.00,0:00:02.00,x,,0,0,0,,still not\n")
        self.assertEqual(pr.retime_text(".ass", text, RATIO), text)

    def test_ass_event_times_reads_what_retime_wrote(self):
        text = (self.HEAD + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,a\n"
                "Dialogue: 0,10:00:00.00,10:00:01.00,Default,,0,0,0,,b\n")
        before = pr.ass_event_times(text)
        after = pr.ass_event_times(pr.retime_text(".ass", text, RATIO))
        self.assertEqual(len(before), 2)
        for (a0, a1), (b0, b1) in zip(before, after):
            self.assertAlmostEqual(b0, a0 * float(RATIO), delta=0.0051)
            self.assertAlmostEqual(b1, a1 * float(RATIO), delta=0.0051)

    def test_ass_event_times_marks_unreadable_lines(self):
        text = "[Events]\nFormat: Start, End, Text\nDialogue: nope,0:00:01.00,x\n"
        self.assertEqual(pr.ass_event_times(text), [None])


class AudioFilter(unittest.TestCase):
    def parse(self, f: str) -> tuple[int, int, int]:
        a, b, c = f.split(",")
        return (int(a.split("=")[1].split(":")[0]), int(b.split("=")[1]), int(c.split("=")[1].split(":")[0]))

    def test_ratio_is_exact_integer_and_band_is_never_narrowed(self):
        for src in (8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000):
            b, c, out = self.parse(pr.audio_filter(RATIO, src, 48000))
            self.assertEqual(Fraction(b, c), RATIO, src)
            self.assertGreaterEqual(c, src, src)
            self.assertLess(c - src, RATIO.denominator, src)           # the smallest such multiple
            self.assertEqual(out, 48000)

    def test_known_values(self):
        self.assertEqual(self.parse(pr.audio_filter(RATIO, 48000, 48000))[:2], (50050, 48000))
        self.assertEqual(self.parse(pr.audio_filter(RATIO, 96000, 96000))[:2], (100100, 96000))

    def test_uses_soxr_at_the_highest_precision(self):
        self.assertEqual(pr.audio_filter(RATIO, 48000, 48000).count("resampler=soxr:precision=28"), 2)


def trace(*pairs: tuple[str, int], pad: int = 40) -> str:
    return "\n".join(f"[trace_headers @ 0x55d0] {i}  {n:<{pad}}= {v}" for i, (n, v) in enumerate(pairs))


class HeaderRates(unittest.TestCase):
    def test_h264_rate_is_time_scale_over_twice_the_tick(self):
        t = trace(("seq_parameter_set_id", 0), ("timing_info_present_flag", 1),
                  ("num_units_in_tick", 1001), ("time_scale", 48000),
                  ("seq_parameter_set_id", 1), ("timing_info_present_flag", 0),
                  ("seq_parameter_set_id", 2), ("timing_info_present_flag", 1),
                  ("num_units_in_tick", 1), ("time_scale", 50))
        self.assertEqual(pr.rates_from_trace("h264", "seq_parameter_set_id", t),
                         [Fraction(24000, 1001), Fraction(25)])

    def test_h264_header_without_timing_declares_nothing(self):
        t = trace(("seq_parameter_set_id", 0), ("timing_info_present_flag", 0))
        self.assertEqual(pr.rates_from_trace("h264", "seq_parameter_set_id", t), [])

    def test_hevc_reads_vps_and_vui_timing_separately(self):
        t = trace(("nal_unit_type", 32), ("vps_timing_info_present_flag", 1),
                  ("vps_num_units_in_tick", 1001), ("vps_time_scale", 24000),
                  ("nal_unit_type", 33), ("vui_timing_info_present_flag", 1),
                  ("vui_num_units_in_tick", 1), ("vui_time_scale", 25))
        self.assertEqual(pr.rates_from_trace("hevc", "nal_unit_type", t),
                         [Fraction(24000, 1001), Fraction(25)])

    def test_hevc_headers_without_timing_declare_nothing(self):
        t = trace(("nal_unit_type", 32), ("vps_timing_info_present_flag", 0),
                  ("nal_unit_type", 33), ("vui_timing_info_present_flag", 0))
        self.assertEqual(pr.rates_from_trace("hevc", "nal_unit_type", t), [])

    def test_mpeg2_code_and_extension(self):
        t = trace(("frame_rate_code", 4), ("frame_rate_code", 3), ("frame_rate_extension_n", 1),
                  ("frame_rate_extension_d", 1), ("frame_rate_code", 15))
        self.assertEqual(pr.rates_from_trace("mpeg2video", "frame_rate_code", t),
                         [Fraction(30000, 1001), Fraction(25)])       # 25 * (1+1) / (1+1); code 15 is reserved

    def test_lines_before_the_first_header_and_noise_are_ignored(self):
        t = ("ffmpeg version\n" + trace(("time_scale", 1), ("seq_parameter_set_id", 0),
                                       ("timing_info_present_flag", 1), ("num_units_in_tick", 1001),
                                       ("time_scale", 48000)) + "\nnot a trace line = 7\n")
        self.assertEqual(pr.rates_from_trace("h264", "seq_parameter_set_id", t), [Fraction(24000, 1001)])

    def test_empty_trace_means_no_headers(self):
        self.assertEqual(pr.rates_from_trace("h264", "seq_parameter_set_id", ""), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
