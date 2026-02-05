from pytradekit.utils.dynamic_types import ConfigField, RunningMode
from pytradekit.utils.config_agent import ConfigAgent
from pytradekit.utils.log_setup import LoggerConfig, get_logger
from pytradekit.utils.tools import find_project_root, encrypt_decrypt, find_run_file_name


def setup_config_logger_mode(file_path, mode_config_field: ConfigField, development_webhook_key, normal_webhook_key):
    config = ConfigAgent(find_project_root(file_path))
    running_mode = config.get_str(
        config.outer,
        mode_config_field.section,
        mode_config_field.key)

    if running_mode == RunningMode.development_flag.name:
        channel_webhook = encrypt_decrypt(config.private[development_webhook_key],
                                          'decrypt')
    else:
        channel_webhook = encrypt_decrypt(config.private[normal_webhook_key],
                                          'decrypt')

    logger_config = LoggerConfig(
        find_run_file_name(file_path),
        channel_webhook,
        channel_webhook)
    logger = get_logger(logger_config)
    return config, logger, running_mode
