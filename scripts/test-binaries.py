#!/usr/bin/env python3
"""Checks that the analysis tools work by running them on a generated audio file.

Usage: test-binaries.py [--require-ffmpeg] [--gpu | --require-gpu] BINARIES_DIR [MODELS_DIR]

BINARIES_DIR is the directory containing abng_key, abng_musicnn and
abng_rhythm, e.g. bin/linux-x86_64. MODELS_DIR defaults to the repository's
models directory. The ffmpeg and ffprobe programs in BINARIES_DIR are checked
too if they are there, --require-ffmpeg fails when they aren't.

The MusicNN predictions are compared to the ones of the TensorFlow based
tools-2 release in reference-predictions.json. --gpu also runs the models with
--device gpu, which falls back to the CPU without a usable GPU, --require-gpu
fails when it does. Only needs the Python standard library.
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
import zlib

SAMPLE_RATE = 44100
DURATION = 20
BPM = 120

# Largest accepted difference to the reference predictions. The converted
# models differ by less than 1e-5, the rest allows for differences in decoding
# and resampling between platforms
TOLERANCE = 1e-3


def cover_art_tag() -> bytes:
    # An ID3 tag with a 1x1 PNG as cover art. FFmpeg shows embedded cover art
    # as a video stream, like in the FLAC and MP3 files people analyze
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))
    body = b"\x00image/png\x00\x03\x00" + png
    frame = b"APIC" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    size = bytes((len(frame) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + size + frame


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

    tag = cover_art_tag()
    tag += b"\x00" * (len(tag) % 2)
    with open(path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        f.write(b"id3 " + struct.pack("<I", len(tag)) + tag)
        riff_size = f.tell() - 8
        f.seek(4)
        f.write(struct.pack("<I", riff_size))


def run(arguments: list) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="replace")
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

    output = run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
                  "-of", "default=noprint_wrappers=1:nokey=1", surround]).stdout
    if output.split() != ["audio", "video"]:
        raise SystemExit(f"Expected an audio and a cover art stream, ffprobe found {output.split()}")

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
                          "[a0][a1]concat=n=2:v=0:a=1,loudnorm=print_format=json", "-vn", "-sn", "-dn",
                          "-f", "null", "-"]).stderr)

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


def test_musicnn(musicnn: str, models: str, audio: str, directory: str, device: str) -> bool:
    """Runs all models in one call, returns whether they ran on the GPU"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference-predictions.json"), encoding="utf-8") as f:
        reference = json.load(f)["predictions"]

    arguments = [musicnn, "--device", device, "--batch-size", "16" if device == "gpu" else "4", audio]
    for name in sorted(reference):
        arguments += [os.path.join(models, name + ".onnx"), os.path.join(directory, f"{name}-{device}.json")]
    result = run(arguments)
    used = "gpu" if "device: gpu" in result.stdout else "cpu"
    for line in result.stderr.splitlines():
        if "GPU unavailable" in line:
            print(line)

    worst = 0.0
    for name, expected in reference.items():
        with open(os.path.join(directory, f"{name}-{device}.json"), encoding="utf-8") as f:
            means = json.load(f)["predictions"]["mean"]
        with open(os.path.join(models, name + ".json"), encoding="utf-8") as f:
            classes = json.load(f)["classes"]
        if len(means) != len(classes) or len(means) != len(expected):
            raise SystemExit(f"Unexpected {name} predictions: {means}")
        difference = max(abs(value - reference_value) for value, reference_value in zip(means, expected))
        if difference > TOLERANCE:
            raise SystemExit(f"The {name} predictions differ from the reference by {difference}: {means}, expected {expected}")
        worst = max(worst, difference)

    with open(os.path.join(directory, f"msd-musicnn-1-{device}.json"), encoding="utf-8") as f:
        means = json.load(f)["predictions"]["mean"]
    with open(os.path.join(models, "msd-musicnn-1.json"), encoding="utf-8") as f:
        classes = json.load(f)["classes"]
    top = sorted(zip(means, classes), reverse=True)[:3]
    print(f"abng_musicnn ({used.upper()}): {len(reference)} models match the reference "
          f"(largest difference {worst:.1e}), " + ", ".join(f"{name} {value:.2f}" for value, name in top))
    return used == "gpu"


def main() -> None:
    arguments = sys.argv[1:]
    flags = {argument for argument in arguments if argument.startswith("--")}
    arguments = [argument for argument in arguments if not argument.startswith("--")]
    if len(arguments) not in (1, 2) or flags - {"--require-ffmpeg", "--gpu", "--require-gpu"}:
        raise SystemExit(__doc__)
    require_ffmpeg = "--require-ffmpeg" in flags
    require_gpu = "--require-gpu" in flags
    gpu = require_gpu or "--gpu" in flags

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

        musicnn = os.path.join(binaries, "abng_musicnn" + exe)
        test_musicnn(musicnn, models, audio, directory, "cpu")
        if gpu and not test_musicnn(musicnn, models, audio, directory, "gpu") and require_gpu:
            raise SystemExit("abng_musicnn didn't use the GPU")

        if os.path.exists(ffmpeg) and os.path.exists(ffprobe):
            test_ffmpeg(ffmpeg, ffprobe, audio, directory)

    print("All tools work")


if __name__ == "__main__":
    main()
