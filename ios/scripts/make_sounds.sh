#!/usr/bin/env bash
# Makes the alert sounds a reader can choose besides the chime (scripts/make_chime.sh), all synthesised here,
# nothing sampled or copied:
#   Techgist/techgist-flash.caf   "News flash": a quick rising three-note sting ending on a held bright note
#   Techgist/techgist-wire.caf    "News wire": five teleprinter ticks and a bell
#   Techgist/techgist-soft.caf    "Soft": one low, mellow note
# Needs python3 and afconvert. Usage: scripts/make_sounds.sh
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d -t techgist-sounds)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$tmp" <<'PY'
import math, random, struct, sys, wave

RATE = 44100
PEAK = 10 ** (-5 / 20)

def tone(out, freq, start, decay, gain=1.0, partials=((1, 1.0), (2, 0.35), (3, 0.18), (4, 0.08))):
    """A struck, slightly brassy note added into `out`: each higher partial is quieter and fades sooner."""
    for i in range(int(start * RATE), len(out)):
        t = (i - start * RATE) / RATE
        attack = min(1.0, t / 0.004)
        out[i] += gain * attack * sum(a * math.exp(-t / (decay / n ** 0.6)) * math.sin(2 * math.pi * freq * n * t)
                                      for n, a in partials)

def tick(out, start, gain=0.5):
    """A teleprinter key: 12 ms of filtered noise with a low thump."""
    rnd = random.Random(int(start * 1000))
    last = 0.0
    for i in range(int(start * RATE), min(len(out), int((start + 0.03) * RATE))):
        t = (i - start * RATE) / RATE
        last = 0.6 * last + 0.4 * rnd.uniform(-1, 1)
        out[i] += gain * math.exp(-t / 0.006) * (last + 0.8 * math.sin(2 * math.pi * 190 * t))

def write(name, out):
    fade = int(0.04 * RATE)
    for i in range(fade):
        out[-1 - i] *= i / fade
    top = max(abs(s) for s in out)
    data = [int(round(s / top * PEAK * 32767)) for s in out]
    with wave.open(f"{sys.argv[1]}/{name}.wav", "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(RATE)
        f.writeframes(struct.pack("<%dh" % len(data), *data))

# News flash: G5, C6, E6 a tenth of a second apart, then G6 held.
flash = [0.0] * int(RATE * 1.1)
for k, (f, d) in enumerate([(783.99, 0.10), (1046.50, 0.10), (1318.51, 0.10), (1567.98, 0.45)]):
    tone(flash, f, 0.09 * k, d, gain=1.0 if k < 3 else 1.2)
write("flash", flash)

# News wire: five ticks, then a small bell.
wire = [0.0] * int(RATE * 1.0)
for k in range(5):
    tick(wire, 0.065 * k)
tone(wire, 2093.0, 0.40, 0.30, gain=0.8, partials=((1, 1.0), (2.76, 0.3), (5.4, 0.12)))
write("wire", wire)

# Soft: one mellow A4 with an octave under it.
soft = [0.0] * int(RATE * 0.8)
tone(soft, 440.0, 0.0, 0.30, partials=((1, 1.0), (2, 0.12)))
tone(soft, 220.0, 0.0, 0.25, gain=0.4, partials=((1, 1.0),))
write("soft", soft)
PY

for name in flash wire soft; do
    afconvert -f caff -d ima4 -c 1 "$tmp/$name.wav" "Techgist/techgist-$name.caf"
    afinfo "Techgist/techgist-$name.caf" | grep -E "duration"
done
