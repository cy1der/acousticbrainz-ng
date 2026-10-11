# AcousticBrainz-ng

This plugin is not affiliated with the [AcousticBrainz](https://acousticbrainz.org) project.

## Note

Although compatible, the plugin will likely not work on Windows due to an access violation error (exit code 3221225477) when ran with Picard. If you have a fix or a workaround, please let me know.

This branch targets Picard 3 (plugin API v3). For Picard 2, use the [`apiv2`](https://github.com/cy1der/acousticbrainz-ng/tree/apiv2) branch.

## Features

- Moods
- ReplayGain
- BPM
- Key
- [Tags](./models/msd-musicnn-1.json)

## Getting started

1. Install the plugin, either from Picard's plugin options page or with `picard-cli plugins install https://github.com/cy1der/acousticbrainz-ng`
2. Enable the plugin
3. If FFmpeg is not on your `PATH`, set the FFmpeg executable path in the plugin options page

Analysis results are cached in Picard's cache directory by default, this can be changed in the plugin options page.

Settings from the Picard 2 version of the plugin are migrated automatically, except for the binaries, models and cache paths.
