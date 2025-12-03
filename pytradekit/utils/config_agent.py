import sys
import os
import ast
import configparser
import argparse
from dotenv import dotenv_values
from pytradekit.utils.dynamic_types import ConfigSection
from pytradekit.utils.exceptions import DependencyException


class ConfigAgent:

    def __init__(self, project_root):
        self.__project_root = project_root
        self.outer, self.outer_name = self._load_outer_config()
        self.private = self._load_private_config()
        self.cmd_arg = self._load_cmd_arg_config()

    def reload_outer(self):
        self.outer, self.outer_name = self._load_outer_config()

    def _load_inner_config(self):
        try:
            filepath = os.path.join(self.__project_root, ConfigSection.inner_config_path.value)
            if not os.path.exists(filepath):
                raise FileNotFoundError(f"Config file {filepath} not found.")

            inner_config = configparser.ConfigParser()
            inner_config.read(filepath)
            return inner_config
        except Exception as e:
            raise DependencyException(f"Error loading inner config: {e}") from e

    def _load_outer_config(self):
        parser = argparse.ArgumentParser(description='Load settings from an ini file.')
        parser.add_argument('config_path', help='Path to the ini config file.', nargs='?', default=None)
        try:
            args = parser.parse_args()
            if args.config_path:
                config_data = self._load_from_ini(args.config_path)
                config_name = os.path.splitext(os.path.basename(args.config_path))[0]
                return config_data, config_name
            else:
                print("No external config provided. Continuing without it.")
                return None, None
        except argparse.ArgumentError as e:
            raise DependencyException("Error parsing command-line arguments. Continuing without external config.") from e

    def _load_private_config(self):
        env_path = os.path.join(self.__project_root, '..', '.env')
        try:
            config = dotenv_values(env_path)
            return config
        except Exception as e:
            raise DependencyException(f"Error loading private config from {env_path}") from e

    @staticmethod
    def _load_cmd_arg_config():
        if len(sys.argv) > 1:
            try:
                return str(sys.argv[1])
            except ValueError as e:
                raise DependencyException("Warning: Command line argument is not an integer. Setting cmd_arg to None.") from e
        else:
            return None

    @staticmethod
    def _load_from_ini(ini_path):
        """
        给定配置文件路径，获取文件中的配置信息。运行时的配置文件，靠它来获取信息
        """
        try:
            if not os.path.exists(ini_path):
                raise DependencyException(f"Config file {ini_path} not found.")

            config = configparser.ConfigParser()
            config.read(ini_path)
            return config
        except Exception as e:
            raise DependencyException(f"Error loading from {ini_path}") from e

    @staticmethod
    def get_str(ini_cfg, section, key):
        try:
            if isinstance(ini_cfg, configparser.ConfigParser):
                value = ini_cfg[section][key]
            elif isinstance(ini_cfg, dict):
                value = ini_cfg.get(key)
            else:
                print(f"Unsupported type for ini_cfg: {type(ini_cfg)}")
                return None

            if value is not None and value.strip().upper() == 'NONE':
                return None
            return value
        except Exception as e:
            raise DependencyException(f"Key {key} not found under section [{section}]") from e

    def get_list(self, ini_cfg, section, key):
        str_value = self.get_str(ini_cfg, section, key)
        return str_value.replace(' ', '').split(',')

    @staticmethod
    def get_int(ini_cfg, section, key):
        try:
            return ini_cfg.getint(section, key)
        except Exception as e:
            raise DependencyException(f"Key {key} not found under section [{section}] or not an integer") from e

    @staticmethod
    def get_float(ini_cfg, section, key):
        try:
            return ini_cfg.getfloat(section, key)
        except Exception as e:
            raise DependencyException(f"Key {key} not found under section [{section}] or not a float") from e

    @staticmethod
    def get_boolean(ini_cfg, section, key):
        try:
            return ini_cfg.getboolean(section, key)
        except Exception as e:
            raise DependencyException(f"Key {key} not found under section [{section}] or not a boolean") from e

    @staticmethod
    def get_dict(ini_cfg, section, key):
        try:
            return ast.literal_eval(ini_cfg[section][key])
        except Exception as e:
            raise DependencyException(f"Key {key} not found under section [{section}] or not a dict") from e

    @staticmethod
    def get_keys_from_section(ini_cfg, section):
        if section in ini_cfg:
            keys = ini_cfg[section].keys()
            keys_list = list(keys)
            keys_list = [i.upper() for i in keys_list]
            return keys_list
        else:
            print(f"Section {section} not found in the config file.")
            return []

    @staticmethod
    def get_section(ini_cfg) -> list:
        try:
            return ini_cfg.sections()
        except Exception as e:
            raise DependencyException(f"Error loading sections from {ini_cfg}: {e}") from e

    def set_str_value(self, section, key, value):
        if self.outer is None:
            raise DependencyException("No outer configuration loaded.")
        try:
            print(f"Writing {value} to [{section}][{key}] in {self.outer_name}")
            self.outer.set(section, key, str(value))
            with open(f'{self.__project_root}/cfg/{self.outer_name}.ini', 'w', encoding="utf-8") as configfile:
                self.outer.write(configfile)
        except Exception as e:
            raise DependencyException(f"Error setting value for [{section}][{key}]: {e}") from e
