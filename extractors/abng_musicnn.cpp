/*
 * Copyright (C) 2006-2021  Music Technology Group - Universitat Pompeu Fabra
 *
 * This file is part of Essentia
 *
 * Essentia is free software: you can redistribute it and/or modify it under
 * the terms of the GNU Affero General Public License as published by the Free
 * Software Foundation (FSF), either version 3 of the License, or (at your
 * option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 * FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
 * details.
 *
 * You should have received a copy of the Affero GNU General Public License
 * version 3 along with this program.  If not, see http://www.gnu.org/licenses/
 */

// Based on Essentia's streaming_musicnn_predict example and its
// TensorflowPredictMusiCNN algorithm. Computes the mel bands of the audio once,
// runs any number of MusicNN models converted to ONNX on them with ONNX Runtime,
// and writes the mean of the patch-wise predictions of each model to a JSON file

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include <onnxruntime_cxx_api.h>

#include <essentia/algorithmfactory.h>
#include <essentia/streaming/algorithms/poolstorage.h>
#include <essentia/scheduler/network.h>

#ifdef _WIN32
#include <windows.h>
#elif defined(__linux__)
#include <unistd.h>
#include <climits>
#endif

using namespace std;
using namespace essentia;
using namespace essentia::streaming;
using namespace essentia::scheduler;

// The MusicNN input, fixed by the training setup
// (https://github.com/jordipons/musicnn-training/blob/master/src/config_file.py)
// and TensorflowPredictMusiCNN's defaults
const int FRAME_SIZE = 512;
const int HOP_SIZE = 256;
const int PATCH_SIZE = 187;
const int PATCH_HOP_SIZE = 93;
const int BANDS = 96;

const char* WEBGPU_EP = "WebGpuExecutionProvider";

void printUsage(const char* program) {
  cerr << "Usage: " << program << " [options] input_audiofile model.onnx output_jsonfile [model.onnx output_jsonfile ...]" << endl;
  cerr << "       " << program << " --list-devices" << endl;
  cerr << "  --device cpu|gpu: where to run the models (default: cpu). gpu uses CoreML on macOS and the" << endl;
  cerr << "                    WebGPU execution provider next to this program elsewhere, and falls back" << endl;
  cerr << "                    to the CPU when the GPU can't be used" << endl;
  cerr << "  --batch-size N:   number of patches per model run (default: 4). Lower values reduce the" << endl;
  cerr << "                    peak memory usage" << endl;
  cerr << "  --threads N:      number of CPU threads per model run (default: all cores)" << endl;
  cerr << "  --list-devices:   print the GPUs the WebGPU execution provider can use as JSON lines" << endl;
}

// The directory of this program, for the execution provider libraries
basic_string<ORTCHAR_T> programDirectory() {
#ifdef _WIN32
  vector<wchar_t> buffer(MAX_PATH);
  while (true) {
    DWORD length = GetModuleFileNameW(nullptr, buffer.data(), (DWORD)buffer.size());
    if (length == 0) return L"";
    if (length < buffer.size()) {
      wstring path(buffer.data(), length);
      return path.substr(0, path.find_last_of(L"\\/") + 1);
    }
    buffer.resize(buffer.size() * 2);
  }
#elif defined(__linux__)
  char buffer[PATH_MAX];
  ssize_t length = readlink("/proc/self/exe", buffer, sizeof(buffer) - 1);
  if (length <= 0) return "";
  string path(buffer, length);
  return path.substr(0, path.find_last_of('/') + 1);
#else
  return "";
#endif
}

basic_string<ORTCHAR_T> webgpuLibrary() {
#ifdef _WIN32
  return programDirectory() + L"onnxruntime_providers_webgpu.dll";
#else
  return programDirectory() + "libonnxruntime_providers_webgpu.so";
#endif
}

bool fileExists(const basic_string<ORTCHAR_T>& path) {
#ifdef _WIN32
  return GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES;
#else
  return ifstream(path).good();
#endif
}

string hex4(uint32_t value) {
  char buffer[16];
  snprintf(buffer, sizeof(buffer), "%04x", value);
  return buffer;
}

// ONNX Runtime's errors include source locations and span several lines, keeps
// the part meant for people, e.g. "Failed to get a WebGPU adapter: ..."
string shortError(const string& message) {
  string text;
  for (char c : message) {
    if (isspace((unsigned char)c)) {
      if (!text.empty() && text.back() != ' ') text += ' ';
    }
    else {
      text += c;
    }
  }
  while (!text.empty() && text.back() == ' ') text.pop_back();
  for (const char* marker : {"was false. ", "] : "}) {
    size_t position = text.rfind(marker);
    if (position != string::npos) return text.substr(position + strlen(marker));
  }
  return text;
}

void gpuUnavailable(const string& reason) {
  cerr << "abng_musicnn: GPU unavailable (" << shortError(reason) << "), using the CPU" << endl;
}

// Whether the WebGPU execution provider has been loaded
bool webgpuLoaded = false;

// Registers the WebGPU execution provider, returns its GPU devices. Throws
// with a message for the user when it isn't available
vector<Ort::ConstEpDevice> webgpuDevices(Ort::Env& env) {
  if (!webgpuLoaded) {
    basic_string<ORTCHAR_T> library = webgpuLibrary();
    if (library.empty() || !fileExists(library)) {
      throw runtime_error("the WebGPU execution provider isn't installed, install the GPU tools");
    }
    env.RegisterExecutionProviderLibrary("webgpu", library);
    webgpuLoaded = true;
  }

  vector<Ort::ConstEpDevice> devices;
  for (const Ort::ConstEpDevice& device : env.GetEpDevices()) {
    if (string(device.EpName()) == WEBGPU_EP && device.Device().Type() == OrtHardwareDeviceType_GPU) {
      devices.push_back(device);
    }
  }
  if (devices.empty()) {
    throw runtime_error("no GPU found, check the graphics drivers (Vulkan on Linux)");
  }
  return devices;
}

int listDevices(Ort::Env& env) {
  try {
    for (const Ort::ConstEpDevice& device : webgpuDevices(env)) {
      Ort::ConstHardwareDevice hardware = device.Device();
      const char* discrete = hardware.Metadata().GetValue("Discrete");
      cout << "{\"ep\": \"webgpu\", \"vendor_id\": \"" << hex4(hardware.VendorId())
           << "\", \"device_id\": \"" << hex4(hardware.DeviceId())
           << "\", \"discrete\": " << (discrete && string(discrete) == "1" ? "true" : "false") << "}" << endl;
    }
  }
  catch (const exception& e) {
    cerr << "Error: " << e.what() << endl;
    return 1;
  }
  return 0;
}

// Session options for the device, gpu is cleared when the GPU can't be used
Ort::SessionOptions sessionOptions(Ort::Env& env, bool& gpu, int threads) {
  Ort::SessionOptions options;
  options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
  options.SetInterOpNumThreads(1);
  if (threads > 0) options.SetIntraOpNumThreads(threads);
  if (!gpu) return options;

  try {
#ifdef __APPLE__
    // FP32 on the GPU, the Neural Engine computes in FP16 which changes the
    // results
    options.AppendExecutionProvider("CoreML", {{"ModelFormat", "MLProgram"},
                                               {"MLComputeUnits", "CPUAndGPU"},
                                               {"AllowLowPrecisionAccumulationOnGPU", "0"}});
#else
    // The WebGPU execution provider picks the GPU itself, preferring a
    // discrete one. Without preserveDevice it recreates the device for each
    // model, which takes longer than running it
    vector<Ort::ConstEpDevice> devices = {webgpuDevices(env).front()};
    unordered_map<string, string> providerOptions = {{"preserveDevice", "1"}};
    options.AppendExecutionProvider_V2(env, devices, providerOptions);
    // Fail instead of silently running parts of the models on the CPU
    options.AddConfigEntry("session.disable_cpu_ep_fallback", "1");
#endif
  }
  catch (const exception& e) {
    gpuUnavailable(e.what());
    gpu = false;
    return sessionOptions(env, gpu, threads);
  }
  return options;
}

vector<char> readFile(const string& filename) {
  ifstream file(filename, ios::binary);
  if (!file) throw runtime_error("can't read " + filename);
  return vector<char>(istreambuf_iterator<char>(file), istreambuf_iterator<char>());
}

// Runs a model on all patches, returns the predictions of each patch
vector<vector<Real> > predict(Ort::Session& session, const vector<Real>& patches, size_t count, int batchSize) {
  Ort::AllocatorWithDefaultOptions allocator;
  Ort::AllocatedStringPtr inputName = session.GetInputNameAllocated(0, allocator);
  Ort::AllocatedStringPtr outputName = session.GetOutputNameAllocated(0, allocator);
  const char* inputs[] = {inputName.get()};
  const char* outputs[] = {outputName.get()};
  Ort::MemoryInfo memory = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);

  vector<vector<Real> > predictions;
  const size_t patchValues = PATCH_SIZE * BANDS;
  for (size_t start = 0; start < count; start += batchSize) {
    size_t batch = min((size_t)batchSize, count - start);
    int64_t shape[] = {(int64_t)batch, PATCH_SIZE, BANDS};
    Ort::Value input = Ort::Value::CreateTensor<float>(memory, const_cast<Real*>(&patches[start * patchValues]),
                                                       batch * patchValues, shape, 3);
    vector<Ort::Value> output = session.Run(Ort::RunOptions{nullptr}, inputs, &input, 1, outputs, 1);

    vector<int64_t> outputShape = output[0].GetTensorTypeAndShapeInfo().GetShape();
    if (outputShape.size() != 2 || outputShape[0] != (int64_t)batch) {
      throw runtime_error("unexpected model output shape");
    }
    const float* values = output[0].GetTensorData<float>();
    for (size_t i = 0; i < batch; ++i) {
      predictions.emplace_back(values + i * outputShape[1], values + (i + 1) * outputShape[1]);
    }
  }
  return predictions;
}

// Writes the mean of the predictions like Essentia's PoolAggregator and
// YamlOutput, to a temporary file first so that an interrupted run doesn't
// leave an incomplete result behind
void writeMean(const vector<vector<Real> >& predictions, const string& outputFilename) {
  Pool pool;
  for (const vector<Real>& prediction : predictions) {
    pool.add("predictions", prediction);
  }
  Pool aggrPool;
  const char* stats[] = {"mean"};
  standard::Algorithm* aggr = standard::AlgorithmFactory::create("PoolAggregator",
                                                                "defaultStats", arrayToVector<string>(stats));
  aggr->input("input").set(pool);
  aggr->output("output").set(aggrPool);
  aggr->compute();
  delete aggr;

  string temporary = outputFilename + ".tmp";
  standard::Algorithm* output = standard::AlgorithmFactory::create("YamlOutput",
                                                                   "format", "json",
                                                                   "filename", temporary);
  output->input("pool").set(aggrPool);
  output->compute();
  delete output;

  remove(outputFilename.c_str());
  if (rename(temporary.c_str(), outputFilename.c_str()) != 0) {
    throw runtime_error("can't write " + outputFilename);
  }
}

// Exits without unloading the WebGPU execution provider, which sometimes
// crashes in its driver threads when the process exits. The results are
// already written then
int finish(int code) {
  if (webgpuLoaded) {
    cout.flush();
    cerr.flush();
    fflush(nullptr);
    _Exit(code);
  }
  return code;
}

int main(int argc, char* argv[]) {
  vector<string> positional;
  int batchSize = 4;
  int threads = 0;
  bool gpu = false;
  bool list = false;

  for (int i = 1; i < argc; ++i) {
    string arg = argv[i];
    if (arg == "--batch-size" && i + 1 < argc) {
      batchSize = atoi(argv[++i]);
      if (batchSize < 1) {
        printUsage(argv[0]);
        return 1;
      }
    }
    else if (arg == "--threads" && i + 1 < argc) {
      threads = atoi(argv[++i]);
      if (threads < 1) {
        printUsage(argv[0]);
        return 1;
      }
    }
    else if (arg == "--device" && i + 1 < argc) {
      string device = argv[++i];
      if (device != "cpu" && device != "gpu") {
        printUsage(argv[0]);
        return 1;
      }
      gpu = device == "gpu";
    }
    else if (arg == "--list-devices") {
      list = true;
    }
    else if (!arg.empty() && arg[0] == '-') {
      printUsage(argv[0]);
      return 1;
    }
    else {
      positional.push_back(arg);
    }
  }

  if (list != positional.empty() || (!list && positional.size() % 2 != 1)) {
    printUsage(argv[0]);
    return 1;
  }

  try {
    // Never destroyed, see finish()
    Ort::Env& env = *new Ort::Env(ORT_LOGGING_LEVEL_ERROR, "abng_musicnn");
    if (list) return finish(listDevices(env));

    const string& audioFilename = positional[0];

    essentia::init();

    Pool pool;
    AlgorithmFactory& factory = streaming::AlgorithmFactory::instance();
    Algorithm* audio = factory.create("MonoLoader",
                                      "filename", audioFilename,
                                      "sampleRate", 16000.);
    Algorithm* frameCutter = factory.create("FrameCutter",
                                            "frameSize", FRAME_SIZE,
                                            "hopSize", HOP_SIZE);
    Algorithm* bands = factory.create("TensorflowInputMusiCNN");

    audio->output("audio")       >> frameCutter->input("signal");
    frameCutter->output("frame") >> bands->input("frame");
    bands->output("bands")       >> PC(pool, "bands");

    Network network(audio);
    network.run();

    // Overlapping patches of PATCH_SIZE frames, the frames after the last
    // complete patch are discarded, like TensorflowPredictMusiCNN does
    const vector<vector<Real> >& frames = pool.value<vector<vector<Real> > >("bands");
    if (frames.size() < (size_t)PATCH_SIZE) {
      throw runtime_error("the audio is too short, MusicNN needs at least 3 seconds");
    }
    size_t count = (frames.size() - PATCH_SIZE) / PATCH_HOP_SIZE + 1;
    vector<Real> patches;
    patches.reserve(count * PATCH_SIZE * BANDS);
    for (size_t patch = 0; patch < count; ++patch) {
      for (size_t frame = patch * PATCH_HOP_SIZE; frame < patch * PATCH_HOP_SIZE + PATCH_SIZE; ++frame) {
        if (frames[frame].size() != (size_t)BANDS) throw runtime_error("unexpected number of mel bands");
        patches.insert(patches.end(), frames[frame].begin(), frames[frame].end());
      }
    }
    network.clear();

    Ort::SessionOptions options = sessionOptions(env, gpu, threads);
    // Printed once the first model runs, creating the session can still fail
    bool reported = false;

    // One model at a time, the sessions of all models together would need
    // several GB of memory
    for (size_t i = 1; i < positional.size(); i += 2) {
      const string& modelFilename = positional[i];
      const string& outputFilename = positional[i + 1];
      vector<char> model = readFile(modelFilename);
      vector<vector<Real> > predictions;
      try {
        Ort::Session session(env, model.data(), model.size(), options);
        predictions = predict(session, patches, count, batchSize);
      }
      catch (const Ort::Exception& e) {
        if (!gpu) throw;
        // E.g. no usable GPU, a model the GPU can't run or a lost device
        gpuUnavailable(e.what());
        gpu = false;
        options = sessionOptions(env, gpu, threads);
        Ort::Session session(env, model.data(), model.size(), options);
        predictions = predict(session, patches, count, batchSize);
      }
      if (!reported) {
        cout << "device: " << (gpu ? "gpu" : "cpu") << endl;
        reported = true;
      }
      writeMean(predictions, outputFilename);
    }

    essentia::shutdown();
  }
  catch (const exception& e) {
    cerr << "Error: " << e.what() << endl;
    return finish(1);
  }

  return finish(0);
}
