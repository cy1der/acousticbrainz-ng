#!/usr/bin/env python3
"""Checks that the analysis tools work by running them on a generated audio file.

Usage: test-binaries.py BINARIES_DIR [MODELS_DIR]

BINARIES_DIR is the directory containing abng_key, abng_musicnn and
abng_rhythm, e.g. bin/linux-x86_64. MODELS_DIR defaults to the repository's
models directory. Only needs the Python standard library.
"""

import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import wave

SAMPLE_RATE = 44100
DURATION = 20
BPM = 120


def write_test_audio(path: str) -> None:
    # An A minor chord with a click on every beat, so that the rhythm and key
    # extractors have something to find
    frequencies = (220.0, 261.63, 329.63)
    beat = int(SAMPLE_RATE * 60 / BPM)
    click = int(SAMPLE_RATE * 0.01)
    frames = bytearray()
    for i in range(SAMPLE_RATE * DURATION):
        t = i / SAMPLE_RATE
        value = 0.15 * sum(math.sin(2 * math.pi * f * t) for f in frequencies)
        if i % beat < click:
            value += 0.5 * math.sin(2 * math.pi * 1000 * t)
        sample = int(max(-1.0, min(1.0, value)) * 32767)
        frames += struct.pack("<hh", sample, sample)

    with wave.open(path, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(bytes(frames))


def run(arguments: list) -> None:
    env = dict(os.environ, TF_ENABLE_ONEDNN_OPTS="0")
    result = subprocess.run(arguments, capture_output=True, text=True, errors="replace", env=env)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(f"{os.path.basename(arguments[0])} failed with exit code {result.returncode}")


def yaml_value(path: str, key: str) -> str:
    # The tools write simple YAML, a regex is enough to read a scalar
    with open(path, encoding="utf-8") as f:
        match = re.search(rf'^\s*{key}:\s*"?([^"\n]+)"?\s*$', f.read(), re.M)
    if not match:
        raise SystemExit(f"{key} not found in {path}")
    return match.group(1)


def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)

    binaries = sys.argv[1]
    repository = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    models = sys.argv[2] if len(sys.argv) == 3 else os.path.join(repository, "models")
    exe = ".exe" if os.name == "nt" else ""

    with tempfile.TemporaryDirectory() as directory:
        audio = os.path.join(directory, "test.wav")
        write_test_audio(audio)

        rhythm = os.path.join(directory, "rhythm.yaml")
        run([os.path.join(binaries, "abng_rhythm" + exe), audio, rhythm])
        bpm = float(yaml_value(rhythm, "bpm"))
        print(f"abng_rhythm: {bpm:.1f} BPM")
        if not 60 <= bpm <= 240:
            raise SystemExit(f"Implausible BPM {bpm}")

        key = os.path.join(directory, "key.yaml")
        run([os.path.join(binaries, "abng_key" + exe), audio, key])
        print(f"abng_key: {yaml_value(key, 'key')} {yaml_value(key, 'key_scale')}")

        model = os.path.join(models, "msd-musicnn-1")
        predictions = os.path.join(directory, "msd.json")
        run([os.path.join(binaries, "abng_musicnn" + exe), model + ".pb", audio, predictions, "--batch-size", "4"])
        with open(predictions, encoding="utf-8") as f:
            means = json.load(f)["predictions"]["mean"]
        with open(model + ".json", encoding="utf-8") as f:
            classes = json.load(f)["classes"]
        if len(means) != len(classes) or not all(0 <= value <= 1 for value in means):
            raise SystemExit(f"Unexpected MusicNN predictions: {means}")
        top = sorted(zip(means, classes), reverse=True)[:3]
        print("abng_musicnn: " + ", ".join(f"{name} {value:.2f}" for value, name in top))

    print("All tools work")


if __name__ == "__main__":
    main()
