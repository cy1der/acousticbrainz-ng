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

// Based on Essentia's streaming_rhythmextractor_multifeature example, writes
// the results to a YAML file instead of printing them

#include <iostream>
#include <essentia/algorithmfactory.h>
#include <essentia/streaming/algorithms/poolstorage.h>
#include <essentia/scheduler/network.h>

using namespace std;
using namespace essentia;
using namespace essentia::streaming;
using namespace essentia::scheduler;

int main(int argc, char* argv[]) {
  if (argc != 3) {
    cerr << "Usage: " << argv[0] << " input_audiofile output_yamlfile" << endl;
    return 1;
  }

  try {
    essentia::init();

    Pool pool;

    streaming::AlgorithmFactory& factory = streaming::AlgorithmFactory::instance();

    Algorithm* monoloader = factory.create("MonoLoader",
                                           "filename", string(argv[1]),
                                           "sampleRate", 44100.);
    Algorithm* rhythmextractor = factory.create("RhythmExtractor2013",
                                                "method", "multifeature");

    monoloader->output("audio")             >> rhythmextractor->input("signal");
    rhythmextractor->output("ticks")        >> PC(pool, "ticks");
    rhythmextractor->output("confidence")   >> PC(pool, "confidence");
    rhythmextractor->output("bpm")          >> PC(pool, "bpm");
    rhythmextractor->output("estimates")    >> PC(pool, "estimates");
    rhythmextractor->output("bpmIntervals") >> PC(pool, "bpm_intervals");

    Network network(monoloader);
    network.run();

    standard::Algorithm* output = standard::AlgorithmFactory::create("YamlOutput",
                                                                     "filename", string(argv[2]));
    output->input("pool").set(pool);
    output->compute();
    delete output;

    network.clear();
    essentia::shutdown();
  }
  catch (const exception& e) {
    cerr << "Error: " << e.what() << endl;
    return 1;
  }

  return 0;
}
