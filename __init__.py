import os

from picard.plugin3.api import PluginApi

from .actions import (
    AcousticBrainzNGAction,
    AcousticBrainzNGDeleteCacheAction,
    apply_cached_results,
)
from .analyzer import AcousticBrainzNG, get_instance, set_instance
from .constants import LEGACY_OPTION_PREFIX, LEGACY_OPTION_TYPES, PLUGIN_OPTIONS
from .options import AcousticBrainzNGOptionsPage


def _migrate_legacy_settings(api: PluginApi) -> None:
    # Carry over the settings of the Picard 2 version of this plugin. Paths to
    # the bundled binaries, models and cache are not migrated, they pointed to
    # the old plugin install
    if api.plugin_persist["legacy_settings_migrated"]:
        return

    setting = api.global_config.setting
    for name, qtype in LEGACY_OPTION_TYPES.items():
        legacy_name = LEGACY_OPTION_PREFIX + name
        try:
            if legacy_name not in setting or name in api.plugin_config:
                continue
            value = setting.raw_value(legacy_name, qtype=qtype)
            if name == "ffmpeg_path" and not (value and os.path.isfile(value)):
                continue
            api.plugin_config[name] = value
            api.logger.info(f"Migrated setting {legacy_name} = {value!r}")
        except Exception as e:
            api.logger.warning(f"Failed to migrate setting {legacy_name}: {e}")

    api.plugin_persist["legacy_settings_migrated"] = True


def enable(api: PluginApi) -> None:
    for name, default, bounds in PLUGIN_OPTIONS:
        api.plugin_config.register_option(name, default, bounds=bounds)
    api.plugin_persist.register_option("legacy_settings_migrated", False)
    _migrate_legacy_settings(api)

    plugin = AcousticBrainzNG(api)
    set_instance(plugin)
    api.tagger.register_cleanup(plugin.shutdown)

    api.register_options_page(AcousticBrainzNGOptionsPage)
    for action in (AcousticBrainzNGAction, AcousticBrainzNGDeleteCacheAction):
        api.register_album_action(action)
        api.register_track_action(action)
        api.register_cluster_action(action)
        api.register_file_action(action)
    api.register_file_post_addition_to_track_processor(apply_cached_results)


def disable() -> None:
    plugin = get_instance()
    if plugin is not None:
        plugin.shutdown()
        set_instance(None)
