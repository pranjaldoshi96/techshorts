#!/usr/bin/env bash
# Makes Techgist's notification sound, Techgist/techgist.caf: an original two-note chime (E6 then B6, 0.6 s),
# synthesised from sine waves here. Nothing is sampled or copied from an existing sound.
# Needs python3 and afconvert (both come with macOS and Xcode's command-line tools).
# Usage: scripts/make_chime.sh
set -euo pipefail
cd "$(dirname "$0")/.."
wav="$(mktemp -t techgist-chime).wav"
trap 'rm -f "$wav"' EXIT

python3 - "$wav" <<'PY'
import math, struct, sys, wave

RATE = 44100
LENGTH = 0.6
PEAK = 10 ** (-6 / 20)          # -6 dBFS

def note(freq, start, decay):
    """A soft bell: the fundamental, a quiet octave, and a faint, slightly sharp third partial that fades fastest."""
    out = [0.0] * int(RATE * LENGTH)
    for i in range(int(start * RATE), len(out)):
        t = (i - start * RATE) / RATE
        attack = min(1.0, t / 0.006)
        body = math.exp(-t / decay)
        out[i] = attack * (
            1.00 * body * math.sin(2 * math.pi * freq * t)
            + 0.22 * math.exp(-t / (decay * 0.6)) * math.sin(2 * math.pi * freq * 2 * t)
            + 0.07 * math.exp(-t / (decay * 0.3)) * math.sin(2 * math.pi * freq * 3.01 * t)
        )
    return out

e6 = note(1318.51, 0.00, 0.16)
b6 = note(1975.53, 0.15, 0.20)
mix = [a + 0.9 * b for a, b in zip(e6, b6)]

# Fade the last 40 ms to silence so the sound never clicks, then set the peak.
fade = int(0.04 * RATE)
for i in range(fade):
    mix[-1 - i] *= i / fade
top = max(abs(s) for s in mix)
samples = [int(round(s / top * PEAK * 32767)) for s in mix]

with wave.open(sys.argv[1], "wb") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(RATE)
    f.writeframes(struct.pack("<%dh" % len(samples), *samples))
PY

# iOS notification sounds: Linear PCM or IMA4 in a .caf, under 30 seconds.
afconvert -f caff -d ima4 -c 1 "$wav" Techgist/techgist.caf
afinfo Techgist/techgist.caf | grep -E "duration|Data format"
