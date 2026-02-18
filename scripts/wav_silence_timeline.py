#!/usr/bin/env python3
import argparse
import math
import struct
import wave


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print ASCII amplitude timeline (sound vs silence) for WAV file."
    )
    parser.add_argument("path", help="Path to WAV file")
    parser.add_argument("--win-ms", type=int, default=20, help="RMS window size in ms")
    parser.add_argument(
        "--silence-ratio",
        type=float,
        default=0.08,
        help="Silence threshold as ratio of max RMS (default: 0.08)",
    )
    parser.add_argument("--width", type=int, default=160, help="Output line width")
    args = parser.parse_args()

    with wave.open(args.path, "rb") as w:
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(n)

    if sw != 2:
        raise SystemExit(f"Unsupported sample width: {sw}. Expected 16-bit WAV.")

    vals = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    if ch > 1:
        vals = vals[0::ch]

    win = max(1, int(sr * args.win_ms / 1000))
    levels = []
    for i in range(0, len(vals), win):
        seg = vals[i : i + win]
        if not seg:
            continue
        rms = (sum(s * s for s in seg) / len(seg)) ** 0.5
        levels.append(rms)

    mx = max(levels) if levels else 1.0
    thr = mx * args.silence_ratio
    chars = " .:-=+*#%@"

    line = []
    for v in levels:
        if v < thr:
            line.append(" ")
        else:
            idx = min(len(chars) - 1, int(v / mx * (len(chars) - 1)))
            line.append(chars[idx])

    step = max(1, len(line) // args.width)
    compact = []
    for i in range(0, len(line), step):
        chunk = line[i : i + step]
        compact.append(max(chunk) if chunk else " ")
    timeline = "".join(compact)

    duration = len(vals) / sr if sr else 0.0
    marks = [" "] * len(timeline)
    for sec in range(int(math.floor(duration)) + 1):
        pos = min(len(timeline) - 1, int(sec / duration * len(timeline))) if duration > 0 else 0
        marks[pos] = "|"
    markline = "".join(marks)

    labels = [" "] * len(timeline)
    for sec in range(int(math.floor(duration)) + 1):
        txt = str(sec)
        pos = min(len(timeline) - len(txt), int(sec / duration * len(timeline))) if duration > 0 else 0
        for j, c in enumerate(txt):
            labels[pos + j] = c
    labelline = "".join(labels)

    silent = sum(1 for v in levels if v < thr)
    print("file:", args.path)
    print(
        f"duration={duration:.2f}s, windows={len(levels)}, win={args.win_ms}ms, "
        f"silence_threshold={thr:.1f} RMS"
    )
    print("amplitude over time (spaces ~= silence):")
    print(timeline)
    print(markline)
    print(labelline)
    if levels:
        print(f"silence windows: {silent}/{len(levels)} ({silent / len(levels) * 100:.1f}%)")
    else:
        print("silence windows: 0/0 (n/a)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
