<#
.SYNOPSIS
Builds the Essentia based tools used by the plugin on Windows.

.DESCRIPTION
Builds abng_key, abng_musicnn and abng_rhythm and installs them together with
the TensorFlow C library.

All dependencies are downloaded with verified checksums and linked statically,
except TensorFlow. FFmpeg is a prebuilt audio-only build made for MSVC
(https://github.com/wo80/ffmpeg-audio-only), building it from source would
require MSYS2.

Requirements: Visual Studio 2022 or the Visual Studio Build Tools with the
"Desktop development with C++" workload, which includes CMake and Ninja. The
script sets up the Visual Studio environment itself if it isn't already active.

.PARAMETER Output
Install directory (default: bin\windows-x86_64 in the repository)

.PARAMETER WorkDir
Download and build directory (default: build\ in the repository)

.PARAMETER TensorFlow
Use an existing TensorFlow C library (a directory containing include\ and lib\)
instead of downloading it

.PARAMETER Clean
Remove the work directory before building

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\build-binaries.ps1
#>

param(
    [string]$Output = "",
    [string]$WorkDir = "",
    [string]$TensorFlow = "",
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
# Invoke-WebRequest is extremely slow when it renders its progress bar
$ProgressPreference = 'SilentlyContinue'

$EssentiaCommit = 'c4db58ed928406982688c554344f34876dfef9cc'

$Sources = @{
    essentia      = @{ Version = $EssentiaCommit; Url = "https://github.com/wo80/essentia/archive/$EssentiaCommit.tar.gz"; Sha256 = '506502695f63cfab7ad3bcd80e35fcf1a53ff392124db0f1d57c83f3aa0639a9'; License = 'AGPL-3.0-or-later' }
    eigen         = @{ Version = '5.0.1'; Url = 'https://gitlab.com/libeigen/eigen/-/archive/5.0.1/eigen-5.0.1.tar.gz'; Sha256 = 'e9c326dc8c05cd1e044c71f30f1b2e34a6161a3b6ecf445d56b53ff1669e3dec'; License = 'MPL-2.0' }
    fftw          = @{ Version = '3.3.11'; Url = 'https://www.fftw.org/fftw-3.3.11.tar.gz'; Sha256 = '5630c24cdeb33b131612f7eb4b1a9934234754f9f388ff8617458d0be6f239a1'; License = 'GPL-2.0-or-later' }
    yaml          = @{ Version = '0.2.5'; Url = 'https://github.com/yaml/libyaml/archive/refs/tags/0.2.5.tar.gz'; Sha256 = 'fa240dbf262be053f3898006d502d514936c818e422afdcf33921c63bed9bf2e'; License = 'MIT' }
    libsamplerate = @{ Version = '0.2.2'; Url = 'https://github.com/libsndfile/libsamplerate/archive/refs/tags/0.2.2.tar.gz'; Sha256 = '16e881487f184250deb4fcb60432d7556ab12cb58caea71ef23960aec6c0405a'; License = 'BSD-2-Clause' }
    ffmpeg        = @{ Version = '9.0.1'; Url = 'https://github.com/wo80/ffmpeg-audio-only/releases/download/v9.0.1/ffmpeg-9.0.1-win64-static.zip'; Sha256 = 'f3bf2451419ea3114fcbd0a68155c91156b569cc4aedf56f28a30931d29bbc44'; License = 'LGPL-2.1-or-later' }
    tensorflow    = @{ Version = '2.18.1'; Url = 'https://storage.googleapis.com/tensorflow/versions/2.18.1/libtensorflow-cpu-windows-x86_64.zip'; Sha256 = '28acdcea6c6b34828cf0e95e67802b0f3577d51bc2e8915de811b7aa0b04452d'; License = 'Apache-2.0'; StripComponents = 0 }
}

# The prebuilt FFmpeg doesn't include its license, take it from the same release
$FFmpegLicenses = @(
    @{ Name = 'COPYING.LGPLv2.1'; Url = 'https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/COPYING.LGPLv2.1'; Sha256 = '246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30' }
    @{ Name = 'LICENSE.md'; Url = 'https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/LICENSE.md'; Sha256 = '2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af' }
)

$RepoDir = Split-Path -Parent $PSScriptRoot
$Platform = 'windows-x86_64'

if (-not $Output) { $Output = Join-Path $RepoDir "bin\$Platform" }
if (-not $WorkDir) { $WorkDir = Join-Path $RepoDir 'build' }

# Git for Windows ships a GNU tar which can't handle drive letters, always use
# the bsdtar included with Windows
$Tar = Join-Path $env:SystemRoot 'System32\tar.exe'

function Write-Step([string]$Message) {
    Write-Host "==> $Message"
}

function Assert-Success([string]$Description) {
    # $ErrorActionPreference doesn't apply to native commands, check their exit code
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Import-VisualStudioEnvironment {
    if (Get-Command cl.exe -ErrorAction SilentlyContinue) {
        return
    }

    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) {
        throw 'Visual Studio was not found, install Visual Studio 2022 or the Build Tools with the "Desktop development with C++" workload'
    }

    $installation = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installation) {
        throw 'No Visual Studio installation with the C++ build tools was found'
    }

    Write-Step "Using Visual Studio at $installation"
    $vcvars = Join-Path $installation 'VC\Auxiliary\Build\vcvars64.bat'
    $environment = & cmd.exe /c "`"$vcvars`" >nul && set"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set up the Visual Studio environment with $vcvars"
    }
    foreach ($line in $environment) {
        $separator = $line.IndexOf('=')
        if ($separator -gt 0) {
            [Environment]::SetEnvironmentVariable($line.Substring(0, $separator), $line.Substring($separator + 1), 'Process')
        }
    }
}

function Get-Download([string]$FileName, [string]$Url, [string]$Sha256) {
    # Downloads a file unless it already exists and verifies its checksum, returns its path
    $file = Join-Path $DownloadDir $FileName

    if (-not (Test-Path $file)) {
        Write-Step "Downloading $FileName"
        Invoke-WebRequest -Uri $Url -OutFile "$file.part" -UseBasicParsing
        Move-Item "$file.part" $file
    }

    $actual = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256) {
        Remove-Item $file
        throw "Checksum mismatch for $file (expected $Sha256, got $actual), removed the file"
    }
    return $file
}

function Get-Source([string]$Name) {
    # Downloads and extracts a source archive, returns its directory
    $source = $Sources[$Name]
    $extension = if ($source.Url.EndsWith('.zip')) { 'zip' } else { 'tar.gz' }
    $archive = Get-Download -FileName "$Name-$($source.Version).$extension" -Url $source.Url -Sha256 $source.Sha256
    # Most archives contain a single top-level directory, which is removed
    $strip = if ($source.ContainsKey('StripComponents')) { $source.StripComponents } else { 1 }

    $directory = Join-Path $SourceDir "$Name-$($source.Version)"
    if (-not (Test-Path $directory)) {
        $temporary = "$directory.tmp"
        if (Test-Path $temporary) { Remove-Item -Recurse -Force $temporary }
        New-Item -ItemType Directory -Path $temporary | Out-Null
        & $Tar -xf $archive -C $temporary --strip-components $strip
        Assert-Success "Extracting $archive"
        Move-Item $temporary $directory
    }
    return $directory
}

function Invoke-DependencyBuild([string]$Name, [scriptblock]$Build) {
    # Runs a dependency build unless it already completed for this version
    $stamp = Join-Path $Prefix "stamps\$Name-$($Sources[$Name].Version)"
    if (Test-Path $stamp) {
        Write-Step "$Name already built"
        return
    }
    Write-Step "Building $Name"
    & $Build
    New-Item -ItemType File -Path $stamp -Force | Out-Null
}

function Invoke-CMakeDependency([string]$Name, [string[]]$Options) {
    $source = Get-Source $Name
    $build = Join-Path $BuildDir $Name
    if (Test-Path $build) { Remove-Item -Recurse -Force $build }
    # Arguments are quoted, Windows PowerShell splits unquoted ones like -DX=3.5 at the dot
    & cmake -S $source -B $build -G Ninja `
        '-DCMAKE_BUILD_TYPE=Release' `
        "-DCMAKE_INSTALL_PREFIX=$Prefix" `
        "-DCMAKE_PREFIX_PATH=$Prefix" `
        '-DCMAKE_POLICY_VERSION_MINIMUM=3.5' `
        '-DBUILD_SHARED_LIBS=OFF' `
        '-DBUILD_TESTING=OFF' `
        @Options
    Assert-Success "Configuring $Name"
    & cmake --build $build
    Assert-Success "Building $Name"
    & cmake --install $build
    Assert-Success "Installing $Name"
}

if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and -not $TensorFlow) {
    throw "There is no official TensorFlow C library for $env:PROCESSOR_ARCHITECTURE, pass one with -TensorFlow"
}

Import-VisualStudioEnvironment

foreach ($tool in 'cmake', 'ninja') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found, it is included with the Visual Studio C++ workload"
    }
}

if ($Clean -and (Test-Path $WorkDir)) {
    Remove-Item -Recurse -Force $WorkDir
}

$DownloadDir = Join-Path $WorkDir 'downloads'
$SourceDir = Join-Path $WorkDir 'src'
$BuildDir = Join-Path $WorkDir "build-$Platform"
$Prefix = Join-Path $WorkDir "prefix-$Platform"
foreach ($directory in $DownloadDir, $SourceDir, $BuildDir, (Join-Path $Prefix 'stamps')) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

Invoke-DependencyBuild eigen {
    Invoke-CMakeDependency eigen @('-DEIGEN_BUILD_DOC=OFF', '-DEIGEN_BUILD_BLAS=OFF', '-DEIGEN_BUILD_LAPACK=OFF', '-DEIGEN_BUILD_DEMOS=OFF')
}

Invoke-DependencyBuild fftw {
    # FFTW checks at runtime which instruction sets the CPU supports
    Invoke-CMakeDependency fftw @('-DENABLE_FLOAT=ON', '-DENABLE_SSE2=ON', '-DENABLE_AVX=ON', '-DENABLE_AVX2=ON', '-DDISABLE_FORTRAN=ON', '-DBUILD_TESTS=OFF')
}

Invoke-DependencyBuild yaml {
    Invoke-CMakeDependency yaml @()
}

Invoke-DependencyBuild libsamplerate {
    Invoke-CMakeDependency libsamplerate @('-DLIBSAMPLERATE_EXAMPLES=OFF', '-DLIBSAMPLERATE_INSTALL=ON')
}

$FFmpegDir = Get-Source ffmpeg

$TensorFlowProvided = [bool]$TensorFlow
if (-not $TensorFlow) {
    $TensorFlow = Get-Source tensorflow
}
if (-not (Test-Path (Join-Path $TensorFlow 'include\tensorflow\c\c_api.h'))) {
    throw "No TensorFlow headers in $TensorFlow\include"
}
if (-not (Test-Path (Join-Path $TensorFlow 'lib\tensorflow.lib'))) {
    throw "No TensorFlow library at $TensorFlow\lib\tensorflow.lib"
}

Write-Step 'Building tools'
$ExtractorsBuild = Join-Path $BuildDir 'extractors'
$FFmpegInclude = Join-Path $FFmpegDir 'include'
$FFmpegLib = Join-Path $FFmpegDir 'lib'
$EssentiaDir = Get-Source essentia
& cmake -S (Join-Path $RepoDir 'extractors') -B $ExtractorsBuild -G Ninja `
    '-DCMAKE_BUILD_TYPE=Release' `
    "-DCMAKE_PREFIX_PATH=$Prefix" `
    "-DESSENTIA_SOURCE_DIR=$EssentiaDir" `
    '-DENABLE_STATIC_DEPENDENCIES=ON' `
    "-DAVCODEC_INCLUDE_DIRS=$FFmpegInclude" "-DAVCODEC_LIBRARIES=$FFmpegLib\avcodec.lib" `
    "-DAVFORMAT_INCLUDE_DIRS=$FFmpegInclude" "-DAVFORMAT_LIBRARIES=$FFmpegLib\avformat.lib" `
    "-DAVUTIL_INCLUDE_DIRS=$FFmpegInclude" "-DAVUTIL_LIBRARIES=$FFmpegLib\avutil.lib" `
    "-DSWRESAMPLE_INCLUDE_DIRS=$FFmpegInclude" "-DSWRESAMPLE_LIBRARIES=$FFmpegLib\swresample.lib" `
    "-DFFTW3f_INCLUDE_DIR=$Prefix\include" "-DFFTW3f_LIBRARY=$Prefix\lib\fftw3f.lib" `
    "-Dyaml_INCLUDE_DIR=$Prefix\include" "-Dyaml_LIBRARY=$Prefix\lib\yaml.lib" `
    "-DSampleRate_INCLUDE_DIR=$Prefix\include" "-DSampleRate_LIBRARY=$Prefix\lib\samplerate.lib" `
    "-DTensorFlow_INCLUDE_DIR=$TensorFlow\include" "-DTensorFlow_LIBRARY=$TensorFlow\lib\tensorflow.lib"
Assert-Success 'Configuring the tools'
& cmake --build $ExtractorsBuild
Assert-Success 'Building the tools'

Write-Step "Installing to $Output"
New-Item -ItemType Directory -Path $Output -Force | Out-Null
& cmake --install $ExtractorsBuild --component tools --prefix $Output
Assert-Success 'Installing the tools'
Copy-Item (Join-Path $TensorFlow 'lib\tensorflow.dll') $Output -Force
Remove-Item (Join-Path $Output 'LICENSE.tensorflow') -ErrorAction SilentlyContinue

# The licenses of everything included in the binaries, needed when
# distributing them
$LicenseDir = Join-Path $Output 'licenses'
if (Test-Path $LicenseDir) { Remove-Item -Recurse -Force $LicenseDir }

function Copy-LicenseFile([string]$Component, [string[]]$Files) {
    # Missing files are skipped, a user-supplied TensorFlow might not include them
    $directory = Join-Path $LicenseDir $Component
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    foreach ($file in $Files) {
        if (Test-Path $file) {
            Copy-Item $file $directory -Force
        }
    }
}

Copy-LicenseFile 'abng-tools' @(Join-Path $RepoDir 'extractors\LICENSE')
Copy-LicenseFile 'essentia' @(Join-Path $EssentiaDir 'COPYING.txt')
Copy-LicenseFile 'eigen' @(Get-ChildItem (Join-Path (Get-Source eigen) 'COPYING.*') | ForEach-Object FullName)
Copy-LicenseFile 'fftw' @(Join-Path (Get-Source fftw) 'COPYING')
Copy-LicenseFile 'yaml' @(Join-Path (Get-Source yaml) 'License')
Copy-LicenseFile 'libsamplerate' @(Join-Path (Get-Source libsamplerate) 'COPYING')
Copy-LicenseFile 'tensorflow' @((Join-Path $TensorFlow 'LICENSE'), (Join-Path $TensorFlow 'THIRD_PARTY_TF_C_LICENSES'))

$FFmpegLicenseDir = Join-Path $LicenseDir 'ffmpeg'
New-Item -ItemType Directory -Path $FFmpegLicenseDir -Force | Out-Null
foreach ($license in $FFmpegLicenses) {
    $file = Get-Download -FileName "ffmpeg-$($Sources.ffmpeg.Version)-$($license.Name)" -Url $license.Url -Sha256 $license.Sha256
    Copy-Item $file (Join-Path $FFmpegLicenseDir $license.Name) -Force
}

$notice = @(
    'abng_key, abng_musicnn and abng_rhythm are licensed under the AGPL-3.0-or-later.'
    'Their source code and the scripts used to build them are available at'
    'https://github.com/cy1der/acousticbrainz-ng (extractors/ and scripts/).'
    ''
    'They include the following components, whose license texts are in the'
    'directories next to this file:'
    ''
)
foreach ($name in 'essentia', 'eigen', 'fftw', 'yaml', 'libsamplerate', 'ffmpeg', 'tensorflow') {
    $source = $Sources[$name]
    if ($name -eq 'tensorflow' -and $TensorFlowProvided) {
        $notice += "- tensorflow ($($source.License)): provided by the user from $TensorFlow"
    } else {
        $notice += "- $name $($source.Version) ($($source.License)): $($source.Url)"
    }
}
$notice | Set-Content -Path (Join-Path $LicenseDir 'README.txt') -Encoding ASCII

# Without arguments the tools print their usage, anything else means they
# can't be loaded, e.g. because of a missing DLL
foreach ($tool in 'abng_key', 'abng_musicnn', 'abng_rhythm') {
    $executable = Join-Path $Output "$tool.exe"
    # Windows PowerShell turns redirected stderr into errors, which would stop the script
    $ErrorActionPreference = 'Continue'
    $result = & $executable 2>&1 | Out-String
    $ErrorActionPreference = 'Stop'
    if ($result -notmatch 'Usage:') {
        Write-Host $result
        throw "$executable doesn't run"
    }
}

Write-Step "Done, the binaries are in $Output"

# The checks above leave a non-zero $LASTEXITCODE, which callers like GitHub
# Actions would report as a failure
exit 0
