#!/usr/bin/env python3
"""Converts the MusicNN models from Essentia's models page to the ONNX files in
models/, which the analysis tools run with ONNX Runtime.

Usage: convert-models.py [--keep DIR]

Only needed when the models change, the converted models are part of the
repository. Downloads the original TensorFlow graphs, checks them against the
checksums below, converts them and checks that ONNX Runtime gives the same
predictions as TensorFlow. --keep DIR keeps the downloads in DIR.

Needs Python 3.12 with, in a virtual environment:

    pip install tensorflow-cpu==2.18.1 tf2onnx==1.17.0 onnx==1.22.0 onnxruntime==1.31.0 "numpy<2.1" "protobuf<5"
"""

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# name, url, sha256 of the original TensorFlow graph
MODELS = [
    ("danceability-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/danceability/danceability-musicnn-msd-2.pb", "08e9a9f717ceb22026e39a18d0b28cd7052a196ce913a3ff30b99c2f9ac2f0b7"),
    ("gender-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/gender/gender-musicnn-msd-2.pb", "374a9d34f6335c8ef2d4daa0f30f503faa658e3a9101199bea0044ee39bee1c7"),
    ("genre_electronic-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/genre_electronic/genre_electronic-musicnn-msd-2.pb", "a3e0ee977c9e28400a5b29542ec5335186d96799aa1fce7547e278de3e3d2b4f"),
    ("genre_rosamerica-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/genre_rosamerica/genre_rosamerica-musicnn-msd-2.pb", "6a0cf8ad9d92556900322c856c4a1e85f92e476aa2c4e903294c9c3f7a8f9c45"),
    ("genre_tzanetakis-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/genre_tzanetakis/genre_tzanetakis-musicnn-msd-2.pb", "8694e5d38c183bc14a54f4fa8c377945c80eaa604fea1d32db96ef1f77948ec2"),
    ("mood_acoustic-musicnn-mtt-2", "https://essentia.upf.edu/models/classifiers/mood_acoustic/mood_acoustic-musicnn-mtt-2.pb", "ffe11ea9e626195430ce4bad3ed89e4aa56615da3046ae8b3a89b64712970fb3"),
    ("mood_aggressive-musicnn-mtt-2", "https://essentia.upf.edu/models/classifiers/mood_aggressive/mood_aggressive-musicnn-mtt-2.pb", "c1bdc81c36765826661fa73e8a3b2212f4f4ccaf2ebd148904343a3334a27438"),
    ("mood_electronic-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/mood_electronic/mood_electronic-musicnn-msd-2.pb", "e4b35ea4e0a67efd69684d9686ee215223a9c2da11c0d5748ab04fbe5bf79f66"),
    ("mood_happy-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/mood_happy/mood_happy-musicnn-msd-2.pb", "db888fca780ea850d7a5016a281056b5a951adf01e6e8ea606fd09924a6a17fa"),
    ("mood_party-musicnn-mtt-2", "https://essentia.upf.edu/models/classifiers/mood_party/mood_party-musicnn-mtt-2.pb", "42532f233cb65d81ea0c72ec8fcec3fbda0fb23a5aa89138f409256f55ea5649"),
    ("mood_relaxed-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/mood_relaxed/mood_relaxed-musicnn-msd-2.pb", "2ba595cf1573550a5957e727b7ecb21ace442b8b6f271d19a13628e35004c4bc"),
    ("mood_sad-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/mood_sad/mood_sad-musicnn-msd-2.pb", "20e2a3e07d8d6db55f18e6a7cfee7ac129e7080533b4bbc270fb018644ad50aa"),
    ("msd-musicnn-1", "https://essentia.upf.edu/models/feature-extractors/musicnn/msd-musicnn-1.pb", "cdea0722bcee7f731286843f2233e3aa69887bb5c3e2dce011eff55f38d04f3e"),
    ("tonal_atonal-musicnn-mtt-2", "https://essentia.upf.edu/models/classifiers/tonal_atonal/tonal_atonal-musicnn-mtt-2.pb", "2a916a9af4be9b99972709d39935a377c869a627c359e4979a0a2c2de4e26a43"),
    ("voice_instrumental-musicnn-msd-2", "https://essentia.upf.edu/models/classifiers/voice_instrumental/voice_instrumental-musicnn-msd-2.pb", "b734bca3fc99257cf0088211b44bd36e8a26fbb1f9ce67e1e97d39f188094b0a"),
]

# The tensors TensorflowPredictMusiCNN uses
INPUT = "model/Placeholder:0"
OUTPUT = "model/Sigmoid:0"
OPSET = 17

# Largest accepted difference between the TensorFlow and ONNX Runtime
# predictions of a patch
TOLERANCE = 1e-4


def download(url: str, path: str, checksum: str) -> None:
    if not os.path.exists(path):
        print(f"Downloading {url}")
        with urllib.request.urlopen(url, timeout=60) as response, open(path + ".part", "wb") as output:
            shutil.copyfileobj(response, output)
        os.replace(path + ".part", path)
    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    if actual != checksum:
        os.remove(path)
        raise SystemExit(f"Checksum mismatch for {url} (expected {checksum}, got {actual}), removed the file")


def main() -> None:
    arguments = sys.argv[1:]
    keep = None
    if arguments[:1] == ["--keep"] and len(arguments) == 2:
        keep = arguments[1]
    elif arguments:
        raise SystemExit(__doc__)

    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
    import numpy as np
    import onnxruntime
    import tensorflow as tf
    import tf2onnx

    # Patches in the range of the log-compressed mel bands
    patches = np.random.default_rng(0).uniform(0, 4, (32, 187, 96)).astype(np.float32)

    with tempfile.TemporaryDirectory() as temporary:
        downloads = keep or temporary
        os.makedirs(downloads, exist_ok=True)
        for name, url, checksum in MODELS:
            graph_path = os.path.join(downloads, name + ".pb")
            download(url, graph_path, checksum)

            graph_def = tf.compat.v1.GraphDef()
            with open(graph_path, "rb") as f:
                graph_def.ParseFromString(f.read())

            output = os.path.join(REPOSITORY, "models", name + ".onnx")
            tf2onnx.convert.from_graph_def(graph_def, input_names=[INPUT], output_names=[OUTPUT],
                                           opset=OPSET, output_path=output)

            graph = tf.Graph()
            with graph.as_default():
                tf.compat.v1.import_graph_def(graph_def, name="")
            with tf.compat.v1.Session(graph=graph) as session:
                expected = session.run(OUTPUT, {INPUT: patches})
            session = onnxruntime.InferenceSession(output, providers=["CPUExecutionProvider"])
            actual = session.run(None, {session.get_inputs()[0].name: patches})[0]
            difference = float(np.abs(expected - actual).max())
            if difference > TOLERANCE:
                os.remove(output)
                raise SystemExit(f"The converted {name} differs from the original by {difference}")
            print(f"{name}: converted, largest difference {difference:.1e}")


if __name__ == "__main__":
    main()
