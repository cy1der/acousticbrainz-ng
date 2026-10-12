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

// Based on Essentia's streaming_musicnn_predict example, writes the mean of
// the patch-wise predictions to a JSON file

#include <cstdlib>
#include <iostream>
#include <essentia/algorithmfactory.h>
#include <essentia/streaming/algorithms/poolstorage.h>
#include <essentia/scheduler/network.h>

using namespace std;
using namespace essentia;
using namespace essentia::streaming;
using namespace essentia::scheduler;

void printUsage(const char* program) {
  cerr << "Usage: " << program << " pb_graph input_audiofile output_jsonfile [--batch-size N]" << endl;
  cerr << "  --batch-size N: number of patches per TensorFlow session run (default: 64)." << endl;
  cerr << "                  Lower values reduce the peak memory usage" << endl;
}

int main(int argc, char* argv[]) {
  vector<string> positional;
  int batchSize = 64;

  for (int i = 1; i < argc; ++i) {
    string arg = argv[i];
    if (arg == "--batch-size" && i + 1 < argc) {
      batchSize = atoi(argv[++i]);
      if (batchSize < 1) {
        printUsage(argv[0]);
        return 1;
      }
    }
    else if (!arg.empty() && arg[0] == '-') {
      printUsage(argv[0]);
      return 1;
    }
    else {
      positional.push_back(arg);
    }
  }

  if (positional.size() != 3) {
    printUsage(argv[0]);
    return 1;
  }

  const string& graphName = positional[0];
  const string& audioFilename = positional[1];
  const string& outputFilename = positional[2];

  try {
    essentia::init();

    Pool pool;
    Pool aggrPool;

    AlgorithmFactory& factory = streaming::AlgorithmFactory::instance();

    Algorithm* audio = factory.create("MonoLoader",
                                      "filename", audioFilename,
                                      "sampleRate", 16000.);

    Algorithm* tfp   = factory.create("TensorflowPredictMusiCNN",
                                      "graphFilename", graphName,
                                      "output", "model/Sigmoid",
                                      "batchSize", batchSize);

    audio->output("audio")     >>  tfp->input("signal");
    tfp->output("predictions") >>  PC(pool, "predictions");

    Network n(audio);
    n.run();

    const char* stats[] = {"mean"};

    standard::Algorithm* aggr = standard::AlgorithmFactory::create("PoolAggregator",
                                                                  "defaultStats", arrayToVector<string>(stats));
    aggr->input("input").set(pool);
    aggr->output("output").set(aggrPool);
    aggr->compute();
    delete aggr;

    standard::Algorithm* output = standard::AlgorithmFactory::create("YamlOutput",
                                                                     "format", "json",
                                                                     "filename", outputFilename);
    output->input("pool").set(aggrPool);
    output->compute();
    delete output;

    n.clear();
    essentia::shutdown();
  }
  catch (const exception& e) {
    cerr << "Error: " << e.what() << endl;
    return 1;
  }

  return 0;
}
