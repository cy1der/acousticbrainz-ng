#! /bin/bash

install_packages=false
install_tf_lib=false
test=false

tflib_already_downloaded=$([ -f libtensorflow-cpu-linux-x86_64.tar.gz ] && echo true || echo false)

if [ "$EUID" -ne 0 ]; then
    echo "Must be root (to install packages and TensorFlow)"
    exit 1
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        --test)
            test=true
            shift
        ;;
        --install-packages)
            install_packages=true
            shift
        ;;
        --install-tf-lib)
            install_tf_lib=true
            shift
        ;;
        --help)
            echo "Usage: $0 [--test] [--install-packages] [--install-tf-lib]"
            echo "Options:"
            echo "  --test                Run tests after building"
            echo "  --install-packages    Install required packages (only needed on first run if not already installed)"
            echo "  --install-tf-lib      Install TensorFlow C library (only needed on first run if not already installed)"
            exit 0
        ;;
        *)
            echo "Unknown option: $1"
            exit 1
        ;;
    esac
done

if [ "$install_packages" = true ]; then
    apt-get install -y git wget build-essential cmake libeigen3-dev libyaml-dev libfftw3-dev libavcodec-dev libavformat-dev libavutil-dev libswresample-dev libsamplerate0-dev libtag1-dev libchromaprint-dev vamp-plugin-sdk
fi

if [ "$install_tf_lib" = true ]; then
    if [ "$tflib_already_downloaded" = false ]; then
        wget https://storage.googleapis.com/tensorflow/versions/2.18.0/libtensorflow-cpu-linux-x86_64.tar.gz
    fi
    tar -C /usr/local -xzf libtensorflow-cpu-linux-x86_64.tar.gz
    echo "/usr/local/lib" | tee /etc/ld.so.conf.d/tensorflow.conf
    /sbin/ldconfig
fi

if [ ! -d "essentia" ]; then
    git clone https://github.com/wo80/essentia.git
fi

cd essentia || return 1
git switch cmake
sed -i 's/find_package(Yaml QUIET)/find_package(YAML QUIET)/' CMakeLists.txt
cmake -B build -D BUILD_EXAMPLES=ON -D BUILD_TESTS="$([ "$test" = true ] && echo ON || echo OFF)" -D BUILD_PYTHON_BINDINGS=OFF -D BUILD_VAMP_PLUGIN=ON -D USE_TENSORFLOW=ON
cmake --build build --config Release --parallel "$(nproc)"
if [ "$test" = true ]; then
    ctest --test-dir build --output-on-failure -C Release
fi
cmake --install build --config Release

cd ..
mkdir -p out
find essentia/build/src/examples -type f -executable -exec cp {} out \;