#!/usr/bin/env python3
"""Checks that the analysis tools work by running them on a generated audio file.

Usage: test-binaries.py [--require-ffmpeg] BINARIES_DIR [MODELS_DIR]

BINARIES_DIR is the directory containing abng_key, abng_musicnn and
abng_rhythm, e.g. bin/linux-x86_64. MODELS_DIR defaults to the repository's
models directory. The ffmpeg and ffprobe programs in BINARIES_DIR are checked
too if they are there, --require-ffmpeg fails when they aren't. Only needs the
Python standard library.
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


def write_test_audio(path: str, channels: int = 2) -> None:
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
        frames += struct.pack("<h", sample) * channels

    with wave.open(path, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(bytes(frames))


def run(arguments: list) -> subprocess.CompletedProcess:
    env = dict(os.environ, TF_ENABLE_ONEDNN_OPTS="0")
    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(f"{os.path.basename(arguments[0])} failed with exit code {result.returncode}")
    return result


def loudness(output: str) -> float:
    match = re.search(r'"input_i"\s*:\s*"([^"]+)"', output)
    if not match:
        raise SystemExit("No loudness measurement in the ffmpeg output")
    return float(match.group(1))


def test_ffmpeg(ffmpeg: str, ffprobe: str, stereo: str, directory: str) -> None:
    # The same commands as the plugin
    surround = os.path.join(directory, "Sürround 5.1.wav")
    write_test_audio(surround, channels=6)

    output = run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                  "-of", "default=noprint_wrappers=1:nokey=1", surround]).stdout
    if output.strip() != "6":
        raise SystemExit(f"ffprobe found {output.strip()} channels instead of 6")

    output = run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", stereo, "-map", "0:a:0",
                  "-c", "copy", "-f", "md5", "-"]).stdout
    if not re.search(r"MD5=[0-9a-f]{32}", output):
        raise SystemExit(f"No MD5 in the ffmpeg output: {output}")

    track = loudness(run([ffmpeg, "-hide_banner", "-nostdin", "-i", stereo, "-vn", "-sn",
                          "-af", "loudnorm=print_format=json", "-f", "null", "-"]).stderr)

    album = loudness(run([ffmpeg, "-hide_banner", "-nostdin", "-i", stereo, "-i", surround, "-filter_complex",
                          "[0:a:0]aformat=channel_layouts=stereo[a0];[1:a:0]aformat=channel_layouts=stereo[a1];"
                          "[a0][a1]concat=n=2:v=0:a=1,loudnorm=print_format=json", "-f", "null", "-"]).stderr)

    downmix = os.path.join(directory, "downmix.wav")
    run([ffmpeg, "-hide_banner", "-nostdin", "-i", surround, "-ac", "2", "-c:a", "pcm_f32le", "-y", downmix])
    output = run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                  "-of", "default=noprint_wrappers=1:nokey=1", downmix]).stdout
    if output.strip() != "2":
        raise SystemExit(f"The downmix has {output.strip()} channels instead of 2")

    print(f"ffmpeg: track {track:.1f} LUFS, album {album:.1f} LUFS, hashing, probing and downmixing work")


def yaml_value(path: str, key: str) -> str:
    # The tools write simple YAML, a regex is enough to read a scalar
    with open(path, encoding="utf-8") as f:
        match = re.search(rf'^\s*{key}:\s*"?([^"\n]+)"?\s*$', f.read(), re.M)
    if not match:
        raise SystemExit(f"{key} not found in {path}")
    return match.group(1)


def main() -> None:
    arguments = sys.argv[1:]
    require_ffmpeg = "--require-ffmpeg" in arguments
    arguments = [argument for argument in arguments if argument != "--require-ffmpeg"]
    if len(arguments) not in (1, 2):
        raise SystemExit(__doc__)

    binaries = arguments[0]
    repository = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    models = arguments[1] if len(arguments) == 2 else os.path.join(repository, "models")
    exe = ".exe" if os.name == "nt" else ""
    ffmpeg = os.path.join(binaries, "ffmpeg" + exe)
    ffprobe = os.path.join(binaries, "ffprobe" + exe)
    if require_ffmpeg and not (os.path.exists(ffmpeg) and os.path.exists(ffprobe)):
        raise SystemExit(f"ffmpeg and ffprobe aren't in {binaries}")

    with tempfile.TemporaryDirectory() as temporary:
        # Non-ASCII names, outside of any single legacy Windows code page, to
        # check that the tools handle any file name
        directory = os.path.join(temporary, "Tëst Ünïcödé 音楽")
        os.mkdir(directory)
        audio = os.path.join(directory, "Ëarnëd 音楽.wav")
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

        if os.path.exists(ffmpeg) and os.path.exists(ffprobe):
            test_ffmpeg(ffmpeg, ffprobe, audio, directory)

    print("All tools work")


if __name__ == "__main__":
    main()
