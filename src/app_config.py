#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author: Bertrand256
# Created on: 2017-03
import argparse
import base64
import codecs
import datetime
import glob
import json
import os
import re
import copy
import shutil
import sys
import threading
import time
from enum import Enum
from io import StringIO, BytesIO
from configparser import ConfigParser
from random import randint
from shutil import copyfile
import logging
from typing import Optional, Callable, Dict, Tuple
import bitcoin
from logging.handlers import RotatingFileHandler

from PyQt5 import QtCore
from PyQt5.QtCore import QLocale, QObject
from PyQt5.QtWidgets import QMessageBox
from cryptography.fernet import Fernet

import app_defs
import base58
import gewel_utils
import hw_intf
from app_defs import APP_NAME_SHORT, APP_NAME_LONG, HWType, APP_DATA_DIR_NAME, DEFAULT_LOG_FORMAT, get_known_loggers
from app_utils import encrypt, decrypt
import app_cache
import default_config
import app_utils
from common import CancelException
from db_intf import DBCache
from encrypted_files import read_file_encrypted, write_file_encrypted, NotConnectedToHardwareWallet
from hw_common import HwSessionInfo
from wnd_utils import WndUtils


CURRENT_CFG_FILE_VERSION = 4
CACHE_ITEM_LOGGERS_LOGLEVEL = 'LoggersLogLevel'
CACHE_ITEM_LOG_FORMAT = 'LogFormat'


GMN_ROLE_OWNER = 0x1
GMN_ROLE_OPERATOR = 0x2
GMN_ROLE_VOTING = 0x4


class InputKeyType():
    PRIVATE = 1
    PUBLIC = 2


class AppFeatueStatus(QObject):
    # Priority of the feature value.
    #  0: default value implemented in the source code
    #  2: value read from the app cache
    #  4: value read from the project github repository (can be lowered or rised)
    #  6: value read from the Gewel network (the highest priority by default)
    PRIORITY_DEFAULT = 0
    PRIORITY_APP_CACHE = 2
    PRIORITY_NETWORK = 6

    value_changed = QtCore.pyqtSignal(object, int)  # args: object being changed, new value

    def __init__(self, initial_value, initial_priority):
        QObject.__init__(self)
        self.__value = initial_value
        self.__priority = initial_priority

    def set_value(self, value, priority):
        if (self.__priority is None or priority >= self.__priority) and value is not None:
            if self.__value != value:
                changed = True
            else:
                changed = False
            self.__value = value
            self.__priority = priority
            if changed:
                self.value_changed.emit(self, value)

    def get_value(self):
        return self.__value

    def reset(self):
        self.__value = None
        self.__priority = None


class AppConfig(object):
    def __init__(self):
        self.initialized = False
        self.app_dir = ''  # will be passed in the init method
        self.app_version = ''
        QLocale.setDefault(app_utils.get_default_locale())
        self.date_format = app_utils.get_default_locale().dateFormat(QLocale.ShortFormat)
        self.date_time_format = app_utils.get_default_locale().dateTimeFormat(QLocale.ShortFormat)

        # List of Gewel network configurations. Multiple conn configs advantage is to give the possibility to use
        # another config if particular one is not functioning (when using "public" RPC service, it could be node's
        # maintanance)
        self.gewel_net_configs = []

        # to distribute the load evenly over "public" RPC services, we choose radom connection (from enabled ones)
        # if it is set to False, connections will be used accoording to its order in gewel_net_configs list
        self.random_gewel_net_config = True

        # list of all enabled geweld configurations (GewelNetworkConnectionCfg) - they will be used accourding to
        # the order in list
        self.active_gewel_net_configs = []

        # list of misbehaving gewel network configurations - they will have the lowest priority during next
        # connections
        self.defective_net_configs = []

        # the contents of the app-params.json configuration file read from the project GitHub repository
        self._remote_app_params = {}
        self._gewel_blockchain_info = {}
        self.non_deterministic_mns_status = AppFeatueStatus(True, 0)
        self.deterministic_mns_status = AppFeatueStatus(False, 0)
        self.__dip3_active = None
        self.__spork15_active = None

        self.hw_type = None  # TREZOR, KEEPKEY, LEDGERNANOS
        self.hw_keepkey_psw_encoding = 'NFC'  # Keepkey passphrase UTF8 chars encoding:
                                              #  NFC: compatible with official Keepkey client app
                                              #  NFKD: compatible with Trezor

        self.gewel_network = 'MAINNET'

        self.block_explorer_tx_mainnet = 'https://insight.gewel.io/insight/tx/%TXID%'
        self.block_explorer_addr_mainnet = 'https://insight.gewel.io/insight/address/%ADDRESS%'
        self.block_explorer_tx_testnet = 'https://testnet-insight.gewel.io/insight/tx/%TXID%'
        self.block_explorer_addr_testnet = 'https://testnet-insight.gewel.io/insight/address/%ADDRESS%'
        self.tx_api_url_mainnet = 'https://insight.gewel.io/insight-api'
        self.tx_api_url_testnet = 'https://testnet-insight.gewel.io/insight-api'
        self.gewel_central_proposal_api = 'https://central.gewel.io/api/v1/proposal?hash=%HASH%'
        self.gewel_nexus_proposal_api = 'https://api.gewelnexus.org/proposals/%HASH%'

        # public RPC connection configurations
        self.public_conns_mainnet: Dict[str, GewelNetworkConnectionCfg] = {}
        self.public_conns_testnet: Dict[str, GewelNetworkConnectionCfg] = {}

        self.check_for_updates = True
        self.backup_config_file = True
        self.read_proposals_external_attributes = False  # if True, some additional attributes will be downloaded from
                                                        # external sources
        self.dont_use_file_dialogs = False
        self.confirm_when_voting = True
        self.add_random_offset_to_vote_time = True  # To avoid identifying one user's masternodes by vote time
        self.csv_delimiter = ';'
        self.masternodes = []
        self.last_bip32_base_path = ''
        self.bip32_recursive_search = True
        self.modified = False
        self.cache_dir = ''
        self.tx_cache_dir = ''
        self.app_config_file_name = ''
        self.log_dir = ''
        self.log_file = ''
        self.log_level_str = ''
        self.db_intf = None
        self.db_cache_file_name = ''
        self.cfg_backup_dir = ''
        self.app_last_version = ''
        self.data_dir = ''
        self.encrypt_config_file = False
        self.config_file_encrypted = False

        # attributes related to encryption cache data with hardware wallet:
        self.hw_generated_key = b"\xab\x0fs}\x8b\t\xb4\xc3\xb8\x05\xba\xd1\x96\x9bq`I\xed(8w\xbf\x95\xf0-\x1a\x14\xcb\x1c\x1d+\xcd"
        self.hw_encryption_key = None
        self.fernet = None
        self.log_handler = None

    def init(self, app_dir):
        """ Initialize configuration after openning the application. """
        self.app_dir = app_dir
        app_defs.APP_PATH = app_dir
        app_defs.APP_IMAGE_DIR = self.get_app_img_dir()

        try:
            with open(os.path.join(app_dir, 'version.txt')) as fptr:
                lines = fptr.read().splitlines()
                self.app_version = app_utils.extract_app_version(lines)
        except:
            pass

        parser = argparse.ArgumentParser()
        parser.add_argument('--config', help="Path to a configuration file", dest='config')
        parser.add_argument('--data-dir', help="Root directory for configuration file, cache and log subdirs",
                            dest='data_dir')
        args = parser.parse_args()

        app_user_dir = ''
        if args.data_dir:
            if os.path.exists(args.data_dir):
                if os.path.isdir(args.data_dir):
                    app_user_dir = args.data_dir
                else:
                    app_user_dir = ''
                    WndUtils.errorMsg('--data-dir parameter doesn\'t point to a directory. Using the default '
                                      'data directory.')
            else:
                app_user_dir = ''
                WndUtils.errorMsg('--data-dir parameter doesn\'t point to an existing directory. Using the default '
                                  'data directory.')

        if not app_user_dir:
            home_dir = os.path.expanduser('~')
            app_user_dir = os.path.join(home_dir, APP_DATA_DIR_NAME)

        self.data_dir = app_user_dir
        self.cache_dir = os.path.join(app_user_dir, 'cache')
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

        cache_file_name = os.path.join(self.cache_dir, 'gmt_cache_v2.json')
        app_cache.init(cache_file_name, self.app_version)
        self.app_last_version = app_cache.get_value('app_version', '', str)
        self.app_config_file_name = ''

        if args.config is not None:
            # set config file name to what user passed in the 'config' argument
            self.app_config_file_name = args.config
            if not os.path.exists(self.app_config_file_name):
                msg = 'Config file "%s" does not exist.' % self.app_config_file_name
                print(msg)
                raise Exception(msg)

        if not self.app_config_file_name:
            # if the user hasn't passed a config file name in command line argument, read the config file name used the
            # last time the application was running (use cache data); if there is no information in cache, use the
            # default name 'config.ini'
            self.app_config_file_name = app_cache.get_value(
                'AppConfig_ConfigFileName', default_value=os.path.join(app_user_dir, 'config.ini'), type=str)

        # setup logging
        self.log_dir = os.path.join(app_user_dir, 'logs')
        self.log_file = os.path.join(self.log_dir, 'gmt.log')
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

        self.log_handler = RotatingFileHandler(filename=self.log_file, mode='a', maxBytes=2000000, backupCount=30)
        logger = logging.getLogger()
        formatter = logging.Formatter(fmt=DEFAULT_LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S')
        self.log_handler.setFormatter(formatter)
        logger.addHandler(self.log_handler)
        self.set_log_level('INFO')
        logging.info(f'===========================================================================')
        logging.info(f'Application started (v {self.app_version})')
        self.restore_loggers_config()

        # directory for configuration backups:
        self.cfg_backup_dir = os.path.join(app_user_dir, 'backup')
        if not os.path.exists(self.cfg_backup_dir):
            os.makedirs(self.cfg_backup_dir)

        if not self.app_last_version or app_utils.is_version_bigger(self.app_version, self.app_last_version):
            app_cache.save_data()

        self.initialized = True

    def close(self):
        self.save_cache_settings()
        self.save_loggers_config()
        app_cache.finish()
        self.db_intf.close()

    def save_cache_settings(self):
        if self.non_deterministic_mns_status.get_value() is not None:
            app_cache.set_value('NON_DETERMINISTIC_MNS_' + self.gewel_network,
                                self.non_deterministic_mns_status.get_value())
        if self.deterministic_mns_status.get_value() is not None:
            app_cache.set_value('DETERMINISTIC_MNS_' + self.gewel_network, self.deterministic_mns_status.get_value())

    def restore_cache_settings(self):
        ena = app_cache.get_value('NON_DETERMINISTIC_MNS_' + self.gewel_network, True, bool)
        self.non_deterministic_mns_status.set_value(ena, AppFeatueStatus.PRIORITY_APP_CACHE)

        ena = app_cache.get_value('DETERMINISTIC_MNS_' + self.gewel_network, False, bool)
        self.deterministic_mns_status.set_value(ena, AppFeatueStatus.PRIORITY_APP_CACHE)

    def copy_from(self, src_config):
        self.gewel_network = src_config.gewel_network
        self.gewel_net_configs = copy.deepcopy(src_config.gewel_net_configs)
        self.random_gewel_net_config = src_config.random_gewel_net_config
        self.hw_type = src_config.hw_type
        self.hw_keepkey_psw_encoding = src_config.hw_keepkey_psw_encoding
        self.block_explorer_tx_mainnet = src_config.block_explorer_tx_mainnet
        self.block_explorer_tx_testnet = src_config.block_explorer_tx_testnet
        self.block_explorer_addr_mainnet = src_config.block_explorer_addr_mainnet
        self.block_explorer_addr_testnet = src_config.block_explorer_addr_testnet
        self.gewel_central_proposal_api = src_config.gewel_central_proposal_api
        self.check_for_updates = src_config.check_for_updates
        self.backup_config_file = src_config.backup_config_file
        self.read_proposals_external_attributes = src_config.read_proposals_external_attributes
        self.dont_use_file_dialogs = src_config.dont_use_file_dialogs
        self.confirm_when_voting = src_config.confirm_when_voting
        self.add_random_offset_to_vote_time = src_config.add_random_offset_to_vote_time
        self.csv_delimiter = src_config.csv_delimiter
        if self.initialized:
            # if this object is the main AppConfig object (it's initialized)
            if self.log_level_str != src_config.log_level_str:
                self.set_log_level(src_config.log_level_str)
                self.reset_loggers()
        else:
            # ... otherwise just copy attribute without reconfiguring logger
            self.log_level_str = src_config.log_level_str
        self.encrypt_config_file = src_config.encrypt_config_file

    def configure_cache(self):
        if self.is_testnet():
            db_cache_file_name = 'gmt_cache_testnet_v2.db'
        else:
            db_cache_file_name = 'gmt_cache_v2.db'
        self.tx_cache_dir = os.path.join(self.cache_dir, 'tx-' + self.hw_coin_name)
        if not os.path.exists(self.tx_cache_dir):
            os.makedirs(self.tx_cache_dir)
            if self.is_testnet():
                # move testnet json files to a subdir (don't do this for mainnet files
                # util there most of users move to gmt v0.1.0
                try:
                    for file in glob.glob(os.path.join(self.cache_dir, 'insight_gewel_testnet*.json')):
                        shutil.move(file, self.tx_cache_dir)
                except Exception as e:
                    logging.exception(str(e))

        new_db_cache_file_name = os.path.join(self.cache_dir, db_cache_file_name)
        if self.db_intf:
            if self.db_cache_file_name != new_db_cache_file_name:
                self.db_cache_file_name = new_db_cache_file_name
                self.db_intf.close()
                self.db_intf.open(self.db_cache_file_name)
        else:
            self.db_intf = DBCache()
            self.db_intf.open(new_db_cache_file_name)
            self.db_cache_file_name = new_db_cache_file_name
        self.restore_cache_settings()

    def clear_configuration(self):
        """
        Clears all the data structures that are loaded during the reading of the
        configuration file. This method is called before the reading new configuration
        from a file.
        :return:
        """
        self.gewel_net_configs.clear()
        self.active_gewel_net_configs.clear()
        self.defective_net_configs.clear()
        self.masternodes.clear()

    def simple_decrypt(self, str_to_decrypt: str, string_can_be_unencrypted: bool, validator: Callable = None) -> str:
        """"
        :param string_can_be_unencrypted: passed True when importing data from the old config format where some
            data wasn't encrypted yet; we want to avoid clogging a log file with errors when actually
            there is no error
        """
        decrypted = ''
        try:
            if str_to_decrypt:
                decrypted = decrypt(str_to_decrypt, APP_NAME_LONG, iterations=5)
            else:
                decrypted = ''
        except Exception as e:
            if string_can_be_unencrypted:
                if validator:
                    if not validator(str_to_decrypt):
                        logging.exception('Unencrypted data validation failed')
                    else:
                        return str_to_decrypt
            logging.exception('String decryption error: ' + str(e))
        return decrypted

    def simple_encrypt(self, str_to_encrypt: str) -> str:
        return encrypt(str_to_encrypt, APP_NAME_LONG, iterations=5)

    def read_from_file(self, hw_session: HwSessionInfo, file_name: Optional[str] = None,
                       create_config_file: bool = False):
        if not file_name:
            file_name = self.app_config_file_name
        configuration_corrected = False
        errors_while_reading = False
        hw_type_sav = self.hw_type

        if os.path.exists(file_name):
            config = ConfigParser()
            try:
                while True:
                    mem_file = ''
                    ret_info = {}
                    try:
                        for data_chunk in read_file_encrypted(file_name, ret_info, hw_session):
                            mem_file += data_chunk.decode('utf-8')
                        break
                    except NotConnectedToHardwareWallet as e:
                        ret = WndUtils.queryDlg(
                            'Configuration file read error: ' + str(e) + '\n\n' +
                            'Click \'Retry\' to try again, \'Restore Defaults\' to continue with default '
                            'configuration or \'Cancel\' to exit.',
                            buttons=QMessageBox.Retry | QMessageBox.Cancel | QMessageBox.RestoreDefaults,
                            default_button=QMessageBox.Yes, icon=QMessageBox.Critical)

                        if ret == QMessageBox.Cancel:
                            raise CancelException('Couldn\'t read configuration file.')
                        elif ret == QMessageBox.Default:
                            break
                        elif ret == QMessageBox.Open:
                            if self.app_config_file_name:
                                dir = os.path.dirname(self.app_config_file_name)
                            else:
                                dir = self.data_dir

                            file_name = WndUtils.open_config_file_query(dir, None, None)
                            if file_name:
                                self.read_from_file(hw_session, file_name)
                                return
                            else:
                                raise Exception('Couldn\'t read the configuration. Exiting...')

                config_file_encrypted = ret_info.get('encrypted', False)

                config.read_string(mem_file)
                self.clear_configuration()

                section = 'CONFIG'
                ini_version = config.get(section, 'CFG_VERSION', fallback=CURRENT_CFG_FILE_VERSION)
                try:
                    ini_version = int(ini_version)
                except Exception:
                    ini_version = CURRENT_CFG_FILE_VERSION

                log_level_str = config.get(section, 'log_level', fallback='WARNING')
                if log_level_str not in ('CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'NOTSET'):
                    log_level_str = 'WARNING'
                if self.log_level_str != log_level_str:
                    self.set_log_level(log_level_str)

                gewel_network = config.get(section, 'gewel_network', fallback='MAINNET')
                if gewel_network not in ('MAINNET', 'TESTNET'):
                    logging.warning(f'Invalid gewel_network value: {gewel_network}')
                    gewel_network = 'MAINNET'
                self.gewel_network = gewel_network

                if self.is_mainnet():
                    def_bip32_path = "44'/49344'/0'/0/0"
                else:
                    def_bip32_path = "44'/1'/0'/0/0"
                self.last_bip32_base_path = config.get(section, 'bip32_base_path', fallback=def_bip32_path)
                if not self.last_bip32_base_path:
                    self.last_bip32_base_path = def_bip32_path
                self.bip32_recursive_search = config.getboolean(section, 'bip32_recursive', fallback=True)
                self.hw_type = config.get(section, 'hw_type', fallback=HWType.trezor)
                if self.hw_type not in (HWType.trezor, HWType.keepkey, HWType.ledger_nano_s):
                    logging.warning('Invalid hardware wallet type: ' + self.hw_type)
                    self.hw_type = HWType.trezor

                self.hw_keepkey_psw_encoding = config.get(section, 'hw_keepkey_psw_encoding', fallback='NFC')
                if self.hw_keepkey_psw_encoding not in ('NFC', 'NFKD'):
                    logging.warning('Invalid value of the hw_keepkey_psw_encoding config option: ' +
                                    self.hw_keepkey_psw_encoding)
                    self.hw_keepkey_psw_encoding = 'NFC'

                self.random_gewel_net_config = self.value_to_bool(config.get(section, 'random_gewel_net_config',
                                                                            fallback='1'))
                self.check_for_updates = self.value_to_bool(config.get(section, 'check_for_updates', fallback='1'))
                self.backup_config_file = self.value_to_bool(config.get(section, 'backup_config_file', fallback='1'))
                self.read_proposals_external_attributes = \
                    self.value_to_bool(config.get(section, 'read_external_proposal_attributes', fallback='1'))
                self.dont_use_file_dialogs = self.value_to_bool(config.get(section, 'dont_use_file_dialogs',
                                                                          fallback='0'))
                self.confirm_when_voting = self.value_to_bool(config.get(section, 'confirm_when_voting',
                                                                          fallback='1'))
                self.add_random_offset_to_vote_time = \
                    self.value_to_bool(config.get(section, 'add_random_offset_to_vote_time', fallback='1'))
                self.encrypt_config_file = \
                    self.value_to_bool(config.get(section, 'encrypt_config_file', fallback='0'))

                # with ini ver 3 we changed the connection password encryption scheme, so connections in new ini
                # file will be saved under different section names - with this we want to disallow the old app
                # version to read such network configuration entries, because passwords won't be decoded properly
                if ini_version < 3:
                    conn_cfg_section_name = 'NETCFG'
                else:
                    conn_cfg_section_name = 'CONNECTION'

                was_error = False
                for section in config.sections():
                    try:
                        if re.match('MN\d', section):
                            try:
                                mn = MasternodeConfig()
                                mn.name = config.get(section, 'name', fallback='')
                                mn.ip = config.get(section, 'ip', fallback='')
                                mn.port = config.get(section, 'port', fallback='')
                                mn.privateKey = self.simple_decrypt(
                                    config.get(section, 'private_key', fallback='').strip(), ini_version < 4,
                                    lambda x: gewel_utils.validate_wif_privkey(x, self.gewel_network) )
                                mn.collateralBip32Path = config.get(section, 'collateral_bip32_path', fallback='').strip()
                                mn.collateralAddress = config.get(section, 'collateral_address', fallback='').strip()
                                mn.collateralTx = config.get(section, 'collateral_tx', fallback='').strip()
                                mn.collateralTxIndex = config.get(section, 'collateral_tx_index', fallback='').strip()
                                mn.use_default_protocol_version = self.value_to_bool(
                                    config.get(section, 'use_default_protocol_version', fallback='1'))
                                mn.protocol_version = config.get(section, 'protocol_version', fallback='').strip()
                                mn.is_deterministic = self.value_to_bool(
                                    config.get(section, 'is_deterministic', fallback='0'))

                                roles = int(config.get(section, 'gmn_user_roles', fallback='0').strip())
                                if not roles:
                                    role_old = int(config.get(section, 'gmn_user_role', fallback='0').strip())
                                    # try reding old roles and map them to the current role-set
                                    if role_old:
                                        if role_old == 1:
                                            mn.gmn_user_roles = GMN_ROLE_OWNER | GMN_ROLE_OPERATOR | GMN_ROLE_VOTING
                                        elif role_old == 2:
                                            mn.gmn_user_roles = GMN_ROLE_OPERATOR
                                        elif role_old == 3:
                                            mn.gmn_user_roles = GMN_ROLE_VOTING
                                else:
                                    mn.gmn_user_roles = roles
                                if not mn.gmn_user_roles:
                                    mn.gmn_user_roles = GMN_ROLE_OWNER | GMN_ROLE_OPERATOR | GMN_ROLE_VOTING

                                mn.gmn_tx_hash = config.get(section, 'gmn_tx_hash', fallback='').strip()
                                mn.gmn_owner_key_type = int(config.get(section, 'gmn_owner_key_type',
                                                                   fallback=str(InputKeyType.PRIVATE)).strip())
                                mn.gmn_operator_key_type = int(config.get(section, 'gmn_operator_key_type',
                                                                   fallback=str(InputKeyType.PRIVATE)).strip())
                                mn.gmn_voting_key_type = int(config.get(section, 'gmn_voting_key_type',
                                                                   fallback=str(InputKeyType.PRIVATE)).strip())
                                if mn.gmn_owner_key_type == InputKeyType.PRIVATE:
                                    mn.gmn_owner_private_key = self.simple_decrypt(
                                        config.get(section, 'gmn_owner_private_key', fallback='').strip(), False)
                                else:
                                    mn.gmn_owner_address = config.get(section, 'gmn_owner_address', fallback='').strip()
                                    
                                if mn.gmn_operator_key_type == InputKeyType.PRIVATE:
                                    mn.gmn_operator_private_key = self.simple_decrypt(
                                        config.get(section, 'gmn_operator_private_key', fallback='').strip(), False)
                                else:
                                    mn.gmn_operator_public_key = config.get(section, 'gmn_operator_public_key', 
                                                                            fallback='').strip()

                                if mn.gmn_voting_key_type == InputKeyType.PRIVATE:
                                    mn.gmn_voting_private_key = self.simple_decrypt(
                                        config.get(section, 'gmn_voting_private_key', fallback='').strip(), False)
                                else:
                                    mn.gmn_voting_address = config.get(section, 'gmn_voting_address', fallback='').strip()

                                self.masternodes.append(mn)
                            except Exception as e:
                                logging.error('Error reading masternode configuration from file. '
                                              'Config section name: ' + section + ': ' + str(e))
                                was_error = True
                        elif re.match(conn_cfg_section_name+'\d', section):
                            # read network configuration from new config file format
                            cfg = GewelNetworkConnectionCfg('rpc')
                            cfg.enabled = self.value_to_bool(config.get(section, 'enabled', fallback='1'))
                            cfg.host = config.get(section, 'host', fallback='').strip()
                            cfg.port = config.get(section, 'port', fallback='').strip()
                            cfg.use_ssl = self.value_to_bool(config.get(section, 'use_ssl', fallback='0').strip())
                            cfg.username = config.get(section, 'username', fallback='').strip()
                            cfg.set_encrypted_password(config.get(section, 'password', fallback=''),
                                                       config_version=ini_version)
                            cfg.use_ssh_tunnel = self.value_to_bool(config.get(section, 'use_ssh_tunnel', fallback='0'))
                            cfg.ssh_conn_cfg.host = config.get(section, 'ssh_host', fallback='').strip()
                            cfg.ssh_conn_cfg.port = config.get(section, 'ssh_port', fallback='').strip()
                            cfg.ssh_conn_cfg.username = config.get(section, 'ssh_username', fallback='').strip()
                            cfg.testnet = self.value_to_bool(config.get(section, 'testnet', fallback='0'))
                            skip_adding = False
                            if not skip_adding:
                                self.gewel_net_configs.append(cfg)
                    except Exception as e:
                        logging.exception(str(e))

                # set the new config file name
                if ini_version < 4:
                    file_part, ext_part = os.path.splitext(file_name)
                    file_name = file_part + f'-v{CURRENT_CFG_FILE_VERSION}' + ext_part

                self.app_config_file_name = file_name
                app_cache.set_value('AppConfig_ConfigFileName', self.app_config_file_name)
                self.config_file_encrypted = config_file_encrypted

                if was_error:
                    WndUtils.warnMsg('There was an error reading configuration file. '
                                     'Look into the log file for more details.')

            except CancelException:
                self.hw_type = hw_type_sav
                raise

            except Exception as e:
                logging.exception('Read configuration error:')
                errors_while_reading = True
                self.hw_type = hw_type_sav
                ret =  WndUtils.queryDlg('Configuration file read error: ' + str(e) + '\n\n' +
                                         'Click \'Restore Defaults\' to continue with default configuration,'
                                         '\'Open\' to choose another configuration file or \'\Cancel\' to exit.',
                                 buttons=QMessageBox.RestoreDefaults | QMessageBox.Cancel | QMessageBox.Open,
                                 default_button=QMessageBox.Yes, icon=QMessageBox.Critical)
                if ret == QMessageBox.Cancel:
                    raise CancelException('Couldn\'t read configuration file.')
                elif ret == QMessageBox.Open:
                    if self.app_config_file_name:
                        dir = os.path.dirname(self.app_config_file_name)
                    else:
                        dir = self.data_dir

                    file_name = WndUtils.open_config_file_query(dir, None, None)
                    if file_name:
                        self.read_from_file(hw_session, file_name)
                        return
                    else:
                        raise Exception('Couldn\'t read the configuration. Exiting...')
                self.app_config_file_name = None
                self.modified = True

        elif file_name:
            if not create_config_file:
                raise Exception(f'The configuration file \'{file_name}\' does not exist.')
            else:
                self.modified = True
            # else: file will be created while saving

        try:
            cfgs = self.decode_connections(default_config.geweld_default_connections)
            if cfgs:
                added, updated = self.import_connections(cfgs, force_import=False, limit_to_network=None)
                if added or updated:
                    configuration_corrected = True

                for c in cfgs:
                    if c.mainnet:
                        self.public_conns_mainnet[c.get_conn_id()] = c
                    else:
                        self.public_conns_testnet[c.get_conn_id()] = c

            if not errors_while_reading:
                # if there were errors while reading configuration, don't save the file automatically but
                # let the user change the new file name instead
                if configuration_corrected:
                    # we are migrating settings from old configuration file - save config file in a new format
                    self.save_to_file(hw_session=hw_session)
                else:
                    if ini_version < CURRENT_CFG_FILE_VERSION:
                        self.save_to_file(hw_session=hw_session)

        except Exception:
            logging.exception('An exception occurred while loading default connection configuration.')

        self.configure_cache()

    def save_to_file(self, hw_session: HwSessionInfo, file_name: Optional[str] = None):
        """
        Saves current configuration to a file with the name 'file_name'. If the 'file_name' argument is empty
        configuration is saved under the current configuration file name (self.app_config_file_name).
        :param file_name:
        :return:
        """

        if not file_name:
            file_name = self.app_config_file_name
        if not file_name:
            if self.app_config_file_name:
                dir = os.path.dirname(self.app_config_file_name)
            else:
                dir = self.data_dir

            file_name = WndUtils.save_config_file_query(dir, None, None)
            if not file_name:
                WndUtils.warnMsg('File not saved.')
                return

        # backup old ini file
        if self.backup_config_file:
            if os.path.exists(file_name):
                tm_str = datetime.datetime.now().strftime('%Y-%m-%d %H_%M')
                back_file_name = os.path.join(self.cfg_backup_dir, 'config_' + tm_str + '.ini')
                try:
                    copyfile(file_name, back_file_name)
                except:
                    pass

        section = 'CONFIG'
        config = ConfigParser()
        config.add_section(section)
        config.set(section, 'CFG_VERSION', str(CURRENT_CFG_FILE_VERSION))
        config.set(section, 'log_level', self.log_level_str)
        config.set(section, 'gewel_network', self.gewel_network)
        if not self.hw_type:
            self.hw_type = HWType.trezor
        config.set(section, 'hw_type', self.hw_type)
        config.set(section, 'hw_keepkey_psw_encoding', self.hw_keepkey_psw_encoding)
        config.set(section, 'bip32_base_path', self.last_bip32_base_path)
        config.set(section, 'random_gewel_net_config', '1' if self.random_gewel_net_config else '0')
        config.set(section, 'check_for_updates', '1' if self.check_for_updates else '0')
        config.set(section, 'backup_config_file', '1' if self.backup_config_file else '0')
        config.set(section, 'dont_use_file_dialogs', '1' if self.dont_use_file_dialogs else '0')
        config.set(section, 'read_external_proposal_attributes',
                   '1' if self.read_proposals_external_attributes else '0')
        config.set(section, 'confirm_when_voting', '1' if self.confirm_when_voting else '0')
        config.set(section, 'add_random_offset_to_vote_time', '1' if self.add_random_offset_to_vote_time else '0')
        config.set(section, 'encrypt_config_file', '1' if self.encrypt_config_file else '0')

        # save mn configuration
        for idx, mn in enumerate(self.masternodes):
            section = 'MN' + str(idx+1)
            config.add_section(section)
            config.set(section, 'name', mn.name)
            config.set(section, 'ip', mn.ip)
            config.set(section, 'port', str(mn.port))
            # the private key encryption method used below is a very basic one, just to not have them stored
            # in plain text; more serious encryption is used when enabling the 'Encrypt config file' option
            config.set(section, 'private_key', self.simple_encrypt(mn.privateKey))
            config.set(section, 'collateral_bip32_path', mn.collateralBip32Path)
            config.set(section, 'collateral_address', mn.collateralAddress)
            config.set(section, 'collateral_tx', mn.collateralTx)
            config.set(section, 'collateral_tx_index', str(mn.collateralTxIndex))
            config.set(section, 'use_default_protocol_version', '1' if mn.use_default_protocol_version else '0')
            config.set(section, 'protocol_version', str(mn.protocol_version))
            config.set(section, 'is_deterministic', '1' if mn.is_deterministic else '0')
            config.set(section, 'gmn_user_roles', str(mn.gmn_user_roles))
            config.set(section, 'gmn_tx_hash', mn.gmn_tx_hash)
            config.set(section, 'gmn_owner_private_key', self.simple_encrypt(mn.gmn_owner_private_key))
            config.set(section, 'gmn_operator_private_key', self.simple_encrypt(mn.gmn_operator_private_key))
            config.set(section, 'gmn_voting_private_key', self.simple_encrypt(mn.gmn_voting_private_key))
            config.set(section, 'gmn_owner_key_type', str(mn.gmn_owner_key_type))
            config.set(section, 'gmn_operator_key_type', str(mn.gmn_operator_key_type))
            config.set(section, 'gmn_voting_key_type', str(mn.gmn_voting_key_type))
            config.set(section, 'gmn_owner_address', mn.gmn_owner_address)
            config.set(section, 'gmn_operator_public_key', mn.gmn_operator_public_key)
            config.set(section, 'gmn_voting_address', mn.gmn_voting_address)
            mn.modified = False

        # save gewel network connections
        for idx, cfg in enumerate(self.gewel_net_configs):
            section = 'CONNECTION' + str(idx+1)
            config.add_section(section)
            config.set(section, 'method', cfg.method)
            config.set(section, 'enabled', '1' if cfg.enabled else '0')
            config.set(section, 'host', cfg.host)
            config.set(section, 'port', cfg.port)
            config.set(section, 'username', cfg.username)
            config.set(section, 'password', cfg.get_password_encrypted())
            config.set(section, 'use_ssl', '1' if cfg.use_ssl else '0')
            config.set(section, 'use_ssh_tunnel', '1' if cfg.use_ssh_tunnel else '0')
            if cfg.use_ssh_tunnel:
                config.set(section, 'ssh_host', cfg.ssh_conn_cfg.host)
                config.set(section, 'ssh_port', cfg.ssh_conn_cfg.port)
                config.set(section, 'ssh_username', cfg.ssh_conn_cfg.username)
                # SSH password is not saved until HW encrypting feature will be finished
            config.set(section, 'testnet', '1' if cfg.testnet else '0')

        # ret_info = {}
        # read_file_encrypted(file_name, ret_info, hw_session)
        if self.encrypt_config_file:
            f_ptr = StringIO()
            config.write(f_ptr)
            f_ptr.seek(0)
            mem_data = bytes()
            while True:
                data_chunk = f_ptr.read(1000)
                if not data_chunk:
                    break
                if isinstance(data_chunk, str):
                    mem_data += bytes(data_chunk, 'utf-8')
                else:
                    mem_data += data_chunk

            write_file_encrypted(file_name, hw_session, mem_data)
            self.config_file_encrypted = True
        else:
            config.write(codecs.open(file_name, 'w', 'utf-8'))
            self.config_file_encrypted = False

        self.modified = False
        self.app_config_file_name = file_name
        app_cache.set_value('AppConfig_ConfigFileName', self.app_config_file_name)

    def reset_network_dependent_dyn_params(self):
        self.non_deterministic_mns_status.reset()
        self.deterministic_mns_status.reset()
        self.__dip3_active = None
        self.__spork15_active = None
        self.apply_remote_app_params()

    def set_remote_app_params(self, params: Dict):
        """ Set the dictionary containing the app live parameters stored in the project repository
        (remote app-params.json).
        """
        self._remote_app_params = params
        self.apply_remote_app_params()

    def apply_remote_app_params(self):
        def get_feature_config_remote(symbol) -> Tuple[Optional[bool], Optional[int]]:
            features = self._remote_app_params.get('features')
            if features:
                feature = features.get(symbol)
                if feature:
                    a = feature.get(self.gewel_network.lower())
                    if a:
                        prio = a.get('priority', 0)
                        status = a.get('status')
                        if status in ('enabled', 'disabled'):
                            return (True if status == 'enabled' else False, prio)
            return None, None

        if self._remote_app_params:
            self.non_deterministic_mns_status.set_value(*get_feature_config_remote('NON_DETERMINISTIC_MNS'))
            self.deterministic_mns_status.set_value(*get_feature_config_remote('DETERMINISTIC_MNS'))

    def read_gewel_network_app_params(self, geweld_intf):
        """ Read parameters having impact on the app's behavior (sporks/dips) from the Gewel network. Called
        after connecting to the network. """

        if self.__dip3_active is None:
            try:
                self._gewel_blockchain_info = geweld_intf.getblockchaininfo()
                bip9 = self._gewel_blockchain_info.get("bip9_softforks")
                if bip9:
                    dip3 = bip9.get('dip0003')
                    if dip3:
                        status = dip3.get('status')
                        if status == 'active':
                            self.__dip3_active = True
                        else:
                            self.__dip3_active = False
                        self.deterministic_mns_status.set_value(self.__dip3_active, AppFeatueStatus.PRIORITY_NETWORK)
            except Exception as e:
                logging.error(str(e))

        if self.__spork15_active is None:
            try:
                spork_block = geweld_intf.get_spork_value('SPORK_15_DETERMINISTIC_MNS_ENABLED')
                if isinstance(spork_block, int):
                    height = geweld_intf.getblockcount()
                    self.__spork15_active = (height >= spork_block)
                else:
                    self.__spork15_active = False
                self.non_deterministic_mns_status.set_value(not self.__spork15_active, AppFeatueStatus.PRIORITY_NETWORK)
            except Exception as e:
                logging.error(str(e))

    def is_non_deterministic_mns_enabled(self):
        return self.non_deterministic_mns_status.get_value() is True

    def is_deterministic_mns_enabled(self):
        return self.deterministic_mns_status.get_value() is True

    def is_dip3_active(self, geweld_intf):
        if self.__dip3_active is None:
            self.read_gewel_network_app_params(geweld_intf)
        return self.__dip3_active is True

    def is_spork_15_active(self, geweld_intf):
        if self.__spork15_active is None:
            self.read_gewel_network_app_params(geweld_intf)
        return self.__spork15_active is True

    def get_default_protocol(self) -> int:
        prot = None
        if self._remote_app_params:
            dp = self._remote_app_params.get('defaultGeweldProtocol')
            if dp:
                prot = dp.get(self.gewel_network.lower())
        return prot

    def get_spork_state_from_config(self, spork_nr: int, gewel_network: str, default_state: bool):
        state = default_state
        if self._remote_app_params:
            sporks = self._remote_app_params.get('sporks')
            if sporks and isinstance(sporks, list):
                for spork in sporks:
                    name = spork.get('name', '')
                    active = spork.get('active')
                    if name.find('SPORK_' + str(spork_nr) + '_') == 0:
                        state = active.get(gewel_network.lower(), state)
        return state

    def value_to_bool(self, value, default=None):
        """
        Cast value to bool:
          - if value is int, 1 will return True, 0 will return False, others will be invalid
          - if value is str, '1' will return True, '0' will return False, others will be invalid 
        :param value: 
        :return: 
        """
        if isinstance(value, bool):
            v = value
        elif isinstance(value, int):
            if value == 1:
                v = True
            elif value == 0:
                v = False
            else:
                v = default
        elif isinstance(value, str):
            if value == '1':
                v = True
            elif value == '0':
                v = False
            else:
                v = default
        else:
            v = default
        return v

    def set_log_level(self, new_log_level_str: str):
        """
        Method called when log level has been changed by the user. New log
        :param new_log_level: new log level (symbol as INFO,WARNING,etc) to be set.
        """
        if self.log_level_str != new_log_level_str:
            ll_sav = self.log_level_str
            lg = logging.getLogger()
            if lg:
                lg.setLevel(new_log_level_str)
                if ll_sav:
                    logging.info('Changed log level to: %s' % new_log_level_str)
            self.log_level_str = new_log_level_str

    def reset_loggers(self):
        """Resets loggers to the default log level """
        for lname in logging.Logger.manager.loggerDict:
            l = logging.Logger.manager.loggerDict[lname]
            if isinstance(l, logging.Logger):
                l.setLevel(0)

    def save_loggers_config(self):
        lcfg = {}
        for lname in logging.Logger.manager.loggerDict:
            l = logging.Logger.manager.loggerDict[lname]
            if isinstance(l, logging.Logger):
                lcfg[lname] = l.level
        app_cache.set_value(CACHE_ITEM_LOGGERS_LOGLEVEL, lcfg)

        if self.log_handler and self.log_handler.formatter:
            fmt = self.log_handler.formatter._fmt
            app_cache.set_value(CACHE_ITEM_LOG_FORMAT, fmt)


    def restore_loggers_config(self):
        lcfg = app_cache.get_value(CACHE_ITEM_LOGGERS_LOGLEVEL, {}, dict)
        if lcfg:
            for lname in lcfg:
                level = lcfg[lname]
                l = logging.getLogger(lname)
                if isinstance(l, logging.Logger):
                    l.setLevel(level)
        else:
            # setting-up log level of external (non-gmt) loggers to avoid cluttering the log file
            for lname in get_known_loggers():
                if lname.external:
                    l = logging.getLogger(lname.name)
                    if isinstance(l, logging.Logger):
                        l.setLevel('WARNING')

        fmt = app_cache.get_value(CACHE_ITEM_LOG_FORMAT, DEFAULT_LOG_FORMAT, str)
        if fmt and self.log_handler:
            formatter = logging.Formatter(fmt=fmt, datefmt='%Y-%m-%d %H:%M:%S')
            self.log_handler.setFormatter(formatter)

    def is_config_complete(self):
        for cfg in self.gewel_net_configs:
            if cfg.enabled:
                return True
        return False

    def prepare_conn_list(self):
        """
        Prepare list of enabled connections for connecting to gewel network.
        :return: list of GewelNetworkConnectionCfg objects order randomly (random_gewel_net_config == True) or according
            to order in configuration
        """
        tmp_list = []
        for cfg in self.gewel_net_configs:
            if cfg.enabled and self.is_testnet() == cfg.testnet:
                tmp_list.append(cfg)
        if self.random_gewel_net_config:
            ordered_list = []
            while len(tmp_list):
                idx = randint(0, len(tmp_list)-1)
                ordered_list.append(tmp_list[idx])
                del tmp_list[idx]
            self.active_gewel_net_configs = ordered_list
        else:
            self.active_gewel_net_configs = tmp_list

    def get_ordered_conn_list(self):
        if not self.active_gewel_net_configs:
            self.prepare_conn_list()
        return self.active_gewel_net_configs

    def conn_config_changed(self):
        self.active_gewel_net_configs = []
        self.defective_net_configs = []

    def conn_cfg_failure(self, cfg):
        """
        Mark conn configuration as not functioning (node could be shut down in the meantime) - this connection will
        be sent to the end of queue of active connections.
        :param cfg: 
        :return: 
        """
        self.defective_net_configs.append(cfg)

    def decode_connections(self, raw_conn_list):
        """
        Decodes list of dicts describing connection to a list of GewelNetworkConnectionCfg objects.
        :param raw_conn_list: 
        :return: list of connection objects
        """
        connn_list = []
        for conn_raw in raw_conn_list:
            try:
                if 'use_ssh_tunnel' in conn_raw and 'host' in conn_raw and 'port' in conn_raw and \
                   'username' in conn_raw and 'password' in conn_raw and 'use_ssl' in conn_raw:
                    cfg = GewelNetworkConnectionCfg('rpc')
                    cfg.use_ssh_tunnel = conn_raw['use_ssh_tunnel']
                    cfg.host = conn_raw['host']
                    cfg.port = conn_raw['port']
                    cfg.username = conn_raw['username']
                    cfg.set_encrypted_password(conn_raw['password'], config_version=CURRENT_CFG_FILE_VERSION)
                    cfg.use_ssl = conn_raw['use_ssl']
                    if cfg.use_ssh_tunnel:
                        if 'ssh_host' in conn_raw:
                            cfg.ssh_conn_cfg.host = conn_raw['ssh_host']
                        if 'ssh_port' in conn_raw:
                            cfg.ssh_conn_cfg.port = conn_raw['ssh_port']
                        if 'ssh_user' in conn_raw:
                            cfg.ssh_conn_cfg.port = conn_raw['ssh_user']
                    cfg.testnet = conn_raw.get('testnet', False)
                    connn_list.append(cfg)
            except Exception as e:
                logging.exception('Exception while decoding connections.')
        return connn_list

    def decode_connections_json(self, conns_json):
        """
        Decodes connections list from JSON string.
        :param conns_json: list of connections as JSON string in the following form:
         [
            {
                'use_ssh_tunnel': bool,
                'host': str,
                'port': str,
                'username': str,
                'password': str,
                'use_ssl': bool,
                'ssh_host': str, non-mandatory
                'ssh_port': str, non-mandatory
                'ssh_user': str non-mandatory
            },
        ]
        :return: list of GewelNetworkConnectionCfg objects or None if there was an error while importing
        """
        try:
            conns_json = conns_json.strip()
            if conns_json.endswith(','):
                conns_json = conns_json[:-1]
            conns = json.loads(conns_json)

            if isinstance(conns, dict):
                conns = [conns]
            return self.decode_connections(conns)
        except Exception as e:
            return None

    def encode_connections_to_json(self, conns):
        encoded_conns = []

        for conn in conns:
            ec = {
                'use_ssh_tunnel': conn.use_ssh_tunnel,
                'host': conn.host,
                'port': conn.port,
                'username': conn.username,
                'password': conn.get_password_encrypted(),
                'use_ssl': conn.use_ssl
            }
            if conn.use_ssh_tunnel:
                ec['ssh_host'] = conn.ssh_conn_cfg.host
                ec['ssh_port'] = conn.ssh_conn_cfg.port
                ec['ssh_username'] = conn.ssh_conn_cfg.username
            encoded_conns.append(ec)
        return json.dumps(encoded_conns, indent=4)

    def import_connections(self, in_conns, force_import, limit_to_network: Optional[str]):
        """
        Imports connections from a list. Used at the app's start to process default connections and/or from
          a configuration dialog, when user pastes from a clipboard a string, describing connections he 
          wants to add to the configuration. The latter feature is used for a convenience.
        :param in_conns: list of GewelNetworkConnectionCfg objects.
        :returns: tuple (list_of_added_connections, list_of_updated_connections)
        """

        added_conns = []
        updated_conns = []
        if in_conns:
            # import default mainnet connections if there is so mainnet conenctions in the current configuration
            # the same for testnet
            mainnet_conn_count = 0
            testnet_conn_count = 0
            for conn in self.gewel_net_configs:
                if conn.testnet:
                    testnet_conn_count += 1
                else:
                    mainnet_conn_count += 1

            for nc in in_conns:
                if (self.gewel_network == 'MAINNET' and nc.testnet == False) or \
                   (self.gewel_network == 'TESTNET' and nc.testnet == True) or not limit_to_network:
                    id = nc.get_conn_id()
                    # check if new connection is in existing list
                    conn = self.get_conn_cfg_by_id(id)
                    if not conn:
                        if force_import or not app_cache.get_value('imported_default_conn_' + nc.get_conn_id(),
                                                                   False, bool) or \
                           (testnet_conn_count == 0 and nc.testnet) or  (mainnet_conn_count == 0 and nc.mainnet):
                            # this new connection was not automatically imported before
                            self.gewel_net_configs.append(nc)
                            added_conns.append(nc)
                            app_cache.set_value('imported_default_conn_' + nc.get_conn_id(), True)
                    elif not conn.identical(nc) and force_import:
                        conn.copy_from(nc)
                        updated_conns.append(conn)
        return added_conns, updated_conns

    def get_conn_cfg_by_id(self, id):
        """
        Returns GewelNetworkConnectionCfg object by its identifier or None if does not exists.
        :param id: Identifier of the sought connection.
        :return: GewelNetworkConnectionCfg object or None if does not exists.
        """
        for conn in self.gewel_net_configs:
            if conn.get_conn_id() == id:
                return conn
        return None

    def conn_cfg_success(self, cfg):
        """
        Mark conn configuration as functioning. If it was placed on self.defective_net_configs list before, now
        will be removed from it.
        """
        if cfg in self.defective_net_configs:
            # remove config from list of defective config
            idx = self.defective_net_configs.index(cfg)
            self.defective_net_configs.pop(idx)

    def get_mn_by_name(self, name):
        for mn in self.masternodes:
            if mn.name == name:
                return mn
        return None

    def add_mn(self, mn):
        if mn not in self.masternodes:
            existing_mn = self.get_mn_by_name(mn.name)
            if not existing_mn:
                self.masternodes.append(mn)
            else:
                raise Exception('Masternode with this name: ' + mn.name + ' already exists in configuration')

    def is_modified(self) -> bool:
        modified = self.modified
        if not modified:
            for mn in self.masternodes:
                if mn.modified:
                    modified = True
                    break
        return modified

    def is_testnet(self) -> bool:
        return self.gewel_network == 'TESTNET'

    def is_mainnet(self) -> bool:
        return self.gewel_network == 'MAINNET'

    @property
    def hw_coin_name(self):
        if self.is_testnet():
            return 'Gewel Testnet'
        else:
            return 'Gewel'

    def get_block_explorer_tx(self):
        if self.gewel_network == 'MAINNET':
            return self.block_explorer_tx_mainnet
        else:
            return self.block_explorer_tx_testnet

    def get_block_explorer_addr(self):
        if self.gewel_network == 'MAINNET':
            return self.block_explorer_addr_mainnet
        else:
            return self.block_explorer_addr_testnet

    def get_tx_api_url(self):
        if self.gewel_network == 'MAINNET':
            return self.tx_api_url_mainnet
        else:
            return self.tx_api_url_testnet

    def get_hw_type(self):
        return self.hw_type

    def initialize_hw_encryption(self, hw_session: HwSessionInfo):
        if threading.current_thread() != threading.main_thread():
            raise Exception('This function must be called from the main thread.')

        if not self.fernet:
            self.hw_encryption_key = None
            self.fernet = None
            # encrypt generated_key with hardware wallet: it will be used to encrypt data in db cache
            try:
                if self.hw_type in (HWType.trezor, HWType.keepkey):
                    v = hw_intf.hw_encrypt_value(hw_session, [10, 100, 1000], 'bip32address',
                                                 self.hw_generated_key, False, False)
                    self.hw_encryption_key = base64.urlsafe_b64encode(v[0])
                else:
                    # Ledger doesn't have encryption features like Trezor, so as an encryption
                    # key we use the public key of the wallet's BIP32 root
                    key = app_utils.SHA256.new(hw_session.base_public_key + self.hw_generated_key).digest()
                    self.hw_encryption_key = base64.urlsafe_b64encode(key)

                self.fernet = Fernet(self.hw_encryption_key)
                return True
            except Exception as e:
                logging.warning("Couldn't encrypt data with hardware wallet: " + str(e))
                return False
        else:
            return True

    def hw_encrypt_string(self, data_str):
        if self.fernet:
            return self.fernet.encrypt(data_str)
        else:
            return None

    def hw_decrypt_string(self, data_str):
        if self.fernet:
            try:
                return self.fernet.decrypt(data_str)
            except Exception:
                return None
        else:
            return None

    def get_app_img_dir(self):
        return os.path.join(self.app_dir, '', 'img')

    def is_connection_public(self, conn: 'GewelNetworkConnectionCfg'):
        conns = self.public_conns_mainnet if self.is_mainnet() else self.public_conns_testnet
        if conn.get_conn_id() in conns:
            return True
        return False


class MasternodeConfig:
    def __init__(self):
        self.name = ''
        self.__ip = ''
        self.__port = '9999'
        self.__privateKey = ''
        self.__collateralBip32Path = ''
        self.__collateralAddress = ''
        self.__collateralTx = ''
        self.__collateralTxIndex = ''
        self.use_default_protocol_version = True
        self.__protocol_version = ''
        self.is_deterministic = False
        self.__gmn_user_roles = GMN_ROLE_OWNER | GMN_ROLE_OPERATOR | GMN_ROLE_VOTING
        self.__gmn_tx_hash = ''
        self.__gmn_owner_key_type = InputKeyType.PRIVATE
        self.__gmn_operator_key_type = InputKeyType.PRIVATE
        self.__gmn_voting_key_type = InputKeyType.PRIVATE
        self.__gmn_owner_private_key = ''
        self.__gmn_operator_private_key = ''
        self.__gmn_voting_private_key = ''
        self.__gmn_owner_address = ''
        self.__gmn_operator_public_key = ''
        self.__gmn_voting_address = ''
        self.new = False
        self.modified = False
        self.lock_modified_change = False

    def set_modified(self):
        if not self.lock_modified_change:
            self.modified = True

    def copy_from(self, src_mn: 'MasternodeConfig'):
        self.ip = src_mn.ip
        self.port = src_mn.port
        self.privateKey = src_mn.privateKey
        self.collateralBip32Path = src_mn.collateralBip32Path
        self.collateralAddress = src_mn.collateralAddress
        self.collateralTx = src_mn.collateralTx
        self.collateralTxIndex = src_mn.collateralTxIndex
        self.use_default_protocol_version = src_mn.use_default_protocol_version
        self.protocol_version = src_mn.protocol_version
        self.is_deterministic = src_mn.is_deterministic
        self.gmn_user_roles = src_mn.gmn_user_roles
        self.gmn_tx_hash = src_mn.gmn_tx_hash
        self.gmn_owner_key_type = src_mn.gmn_owner_key_type
        self.gmn_operator_key_type = src_mn.gmn_operator_key_type
        self.gmn_voting_key_type = src_mn.gmn_voting_key_type
        self.gmn_owner_private_key = src_mn.gmn_owner_private_key
        self.gmn_operator_private_key = src_mn.gmn_operator_private_key
        self.gmn_voting_private_key = src_mn.gmn_voting_private_key
        self.gmn_owner_address = src_mn.gmn_owner_address
        self.gmn_operator_public_key = src_mn.gmn_operator_public_key
        self.gmn_voting_address = src_mn.gmn_voting_address
        self.new = True
        self.modified = True
        self.lock_modified_change = False

    @property
    def ip(self):
        if self.__ip:
            return self.__ip.strip()
        else:
            return self.__ip

    @ip.setter
    def ip(self, new_ip):
        if new_ip:
            self.__ip = new_ip.strip()
        else:
            self.__ip = new_ip

    @property
    def port(self):
        if self.__port:
            return self.__port.strip()
        else:
            return self.__port

    @port.setter
    def port(self, new_port):
        if new_port:
            self.__port = new_port.strip()
        else:
            self.__port = new_port

    @property
    def privateKey(self):
        if self.__privateKey:
            return self.__privateKey.strip()
        else:
            return self.__privateKey

    @privateKey.setter
    def privateKey(self, new_private_key):
        if new_private_key:
            self.__privateKey = new_private_key.strip()
        else:
            self.__privateKey = new_private_key

    @property
    def collateralBip32Path(self):
        if self.__collateralBip32Path:
            return self.__collateralBip32Path.strip()
        else:
            return self.__collateralBip32Path

    @collateralBip32Path.setter
    def collateralBip32Path(self, new_collateral_bip32_path):
        if new_collateral_bip32_path:
            self.__collateralBip32Path = new_collateral_bip32_path.strip()
        else:
            self.__collateralBip32Path = new_collateral_bip32_path

    @property
    def collateralAddress(self):
        if self.__collateralAddress:
            return self.__collateralAddress.strip()
        else:
            return self.__collateralAddress

    @collateralAddress.setter
    def collateralAddress(self, new_collateral_address):
        if new_collateral_address:
            self.__collateralAddress = new_collateral_address.strip()
        else:
            self.__collateralAddress = new_collateral_address

    @property
    def collateralTx(self):
        if self.__collateralTx:
            return self.__collateralTx.strip()
        else:
            return self.__collateralTx

    @collateralTx.setter
    def collateralTx(self, new_collateral_tx):
        if new_collateral_tx:
            self.__collateralTx = new_collateral_tx.strip()
        else:
            self.__collateralTx = new_collateral_tx

    @property
    def collateralTxIndex(self):
        if self.__collateralTxIndex:
            return self.__collateralTxIndex.strip()
        else:
            return self.__collateralTxIndex

    @collateralTxIndex.setter
    def collateralTxIndex(self, new_collateral_tx_index):
        if new_collateral_tx_index:
            self.__collateralTxIndex = new_collateral_tx_index.strip()
        else:
            self.__collateralTxIndex = new_collateral_tx_index

    @property
    def protocol_version(self):
        if self.__protocol_version:
            return self.__protocol_version.strip()
        else:
            return self.__protocol_version

    @protocol_version.setter
    def protocol_version(self, new_protocol_version):
        if new_protocol_version:
            self.__protocol_version = new_protocol_version.strip()
        else:
            self.__protocol_version = new_protocol_version

    @property
    def gmn_user_roles(self):
        return self.__gmn_user_roles

    @gmn_user_roles.setter
    def gmn_user_roles(self, roles):
        self.__gmn_user_roles = roles

    @property
    def gmn_tx_hash(self):
        return self.__gmn_tx_hash

    @gmn_tx_hash.setter
    def gmn_tx_hash(self, tx_hash: str):
        if tx_hash is None:
            tx_hash = ''
        self.__gmn_tx_hash = tx_hash.strip()

    @property
    def gmn_owner_private_key(self):
        return self.__gmn_owner_private_key

    @gmn_owner_private_key.setter
    def gmn_owner_private_key(self, gmn_owner_private_key: str):
        if gmn_owner_private_key is None:
            gmn_owner_private_key = ''
        self.__gmn_owner_private_key = gmn_owner_private_key.strip()

    @property
    def gmn_owner_address(self):
        return self.__gmn_owner_address

    @gmn_owner_address.setter
    def gmn_owner_address(self, address):
        self.__gmn_owner_address = address

    @property
    def gmn_operator_private_key(self):
        return self.__gmn_operator_private_key

    @gmn_operator_private_key.setter
    def gmn_operator_private_key(self, gmn_operator_private_key: str):
        if gmn_operator_private_key is None:
            gmn_operator_private_key = ''
        self.__gmn_operator_private_key = gmn_operator_private_key.strip()

    @property
    def gmn_operator_public_key(self):
        return self.__gmn_operator_public_key

    @gmn_operator_public_key.setter
    def gmn_operator_public_key(self, key):
        self.__gmn_operator_public_key = key

    @property
    def gmn_voting_private_key(self):
        return self.__gmn_voting_private_key

    @gmn_voting_private_key.setter
    def gmn_voting_private_key(self, gmn_voting_private_key: str):
        if gmn_voting_private_key is None:
            gmn_voting_private_key = ''
        self.__gmn_voting_private_key = gmn_voting_private_key.strip()

    @property
    def gmn_voting_address(self):
        return self.__gmn_voting_address

    @gmn_voting_address.setter
    def gmn_voting_address(self, address):
        self.__gmn_voting_address = address

    @property
    def gmn_owner_key_type(self):
        return self.__gmn_owner_key_type

    @gmn_owner_key_type.setter
    def gmn_owner_key_type(self, type: InputKeyType):
        if type not in (InputKeyType.PRIVATE, InputKeyType.PUBLIC):
            raise Exception('Invalid owner key type')
        self.__gmn_owner_key_type = type

    @property
    def gmn_operator_key_type(self):
        return self.__gmn_operator_key_type

    @gmn_operator_key_type.setter
    def gmn_operator_key_type(self, type: InputKeyType):
        if type not in (InputKeyType.PRIVATE, InputKeyType.PUBLIC):
            raise Exception('Invalid operator key type')
        self.__gmn_operator_key_type = type

    @property
    def gmn_voting_key_type(self):
        return self.__gmn_voting_key_type

    @gmn_voting_key_type.setter
    def gmn_voting_key_type(self, type: InputKeyType):
        if type not in (InputKeyType.PRIVATE, InputKeyType.PUBLIC):
            raise Exception('Invalid voting key type')
        self.__gmn_voting_key_type = type

    def get_current_key_for_voting(self, app_config: AppConfig, geweld_intf):
        if app_config.is_spork_15_active(geweld_intf) and self.is_deterministic:
            return self.gmn_voting_private_key
        else:
            return self.privateKey

    def get_gmn_owner_public_address(self, gewel_network) -> Optional[str]:
        if self.__gmn_owner_key_type == InputKeyType.PRIVATE:
            if self.__gmn_owner_private_key:
                address = gewel_utils.wif_privkey_to_address(self.__gmn_owner_private_key, gewel_network)
                return address
        else:
            if self.__gmn_owner_address:
                return self.__gmn_owner_address
        return ''

    def get_gmn_owner_pubkey_hash(self) -> Optional[str]:
        if self.gmn_owner_key_type == InputKeyType.PRIVATE:
            if self.__gmn_owner_private_key:
                pubkey = gewel_utils.wif_privkey_to_pubkey(self.__gmn_owner_private_key)
                pubkey_bin = bytes.fromhex(pubkey)
                pub_hash = bitcoin.bin_hash160(pubkey_bin)
                return pub_hash.hex()
        else:
            if self.__gmn_owner_address:
                ret = gewel_utils.address_to_pubkey_hash(self.__gmn_owner_address)
                if ret:
                    return ret.hex()
        return ''

    def get_gmn_voting_public_address(self, gewel_network) -> Optional[str]:
        if self.__gmn_voting_key_type == InputKeyType.PRIVATE:
            if self.__gmn_voting_private_key:
                address = gewel_utils.wif_privkey_to_address(self.__gmn_voting_private_key, gewel_network)
                return address
        else:
            if self.__gmn_voting_address:
                return self.__gmn_voting_address
        return ''

    def get_gmn_voting_pubkey_hash(self) -> Optional[str]:
        if self.__gmn_voting_key_type == InputKeyType.PRIVATE:
            if self.__gmn_voting_private_key:
                pubkey = gewel_utils.wif_privkey_to_pubkey(self.__gmn_voting_private_key)
                pubkey_bin = bytes.fromhex(pubkey)
                pub_hash = bitcoin.bin_hash160(pubkey_bin)
                return pub_hash.hex()
        else:
            if self.__gmn_voting_address:
                ret = gewel_utils.address_to_pubkey_hash(self.__gmn_voting_address)
                if ret:
                    return ret.hex()
        return ''

    def get_gmn_operator_pubkey(self) -> Optional[str]:
        if self.__gmn_operator_key_type == InputKeyType.PRIVATE:
            if self.__gmn_operator_private_key:
                pubkey = gewel_utils.bls_privkey_to_pubkey(self.__gmn_operator_private_key)
                return pubkey
        else:
            return self.__gmn_operator_public_key
        return ''


class SSHConnectionCfg(object):
    def __init__(self):
        self.__host = ''
        self.__port = ''
        self.__username = ''
        self.__password = ''

    @property
    def host(self):
        return self.__host

    @host.setter
    def host(self, host):
        self.__host = host

    @property
    def port(self):
        return self.__port

    @port.setter
    def port(self, port):
        self.__port = port

    @property
    def username(self):
        return self.__username

    @username.setter
    def username(self, username):
        self.__username = username

    @property
    def password(self):
        return self.__password

    @password.setter
    def password(self, password):
        self.__password = password


class GewelNetworkConnectionCfg(object):
    def __init__(self, method):
        self.__enabled = True
        self.method = method    # now only 'rpc'
        self.__host = ''
        self.__port = ''
        self.__username = ''
        self.__password = ''
        self.__use_ssl = False
        self.__use_ssh_tunnel = False
        self.__ssh_conn_cfg = SSHConnectionCfg()
        self.__testnet = False

    def get_description(self):
        if self.__use_ssh_tunnel:
            desc, host, port = ('SSH ', self.ssh_conn_cfg.host, self.ssh_conn_cfg.port)
        else:
            if self.__use_ssl:
                desc, host, port = ('https://', self.__host, self.__port)
            else:
                desc, host, port = ('', self.__host, self.__port)
        desc = '%s%s:%s' % (desc, (host if host else '???'), (port if port else '???'))
        return desc

    def get_conn_id(self):
        """
        Returns identifier of this connection, built on attributes that uniquely characteraize the connection. 
        :return: 
        """
        if self.__use_ssh_tunnel:
            id = 'SSH:' + self.ssh_conn_cfg.host + ':' + self.__host + ':' + self.__port + ':' + str(self.__testnet)
        else:
            id = 'DIRECT:' + self.__host + ':' + self.__port + ':' + str(self.__testnet)
        id = bitcoin.sha256(id)
        return id

    def identical(self, cfg2):
        """
        Checks if connection object passed as an argument has exactly the same values as self object.
        :param cfg2: GewelNetworkConnectionCfg object to compare
        :return: True, if objects have identical attributes.
        """
        return self.host == cfg2.host and self.port == cfg2.port and self.username == cfg2.username and \
               self.password == cfg2.password and self.use_ssl == cfg2.use_ssl and \
               self.use_ssh_tunnel == cfg2.use_ssh_tunnel and \
               (not self.use_ssh_tunnel or (self.ssh_conn_cfg.host == cfg2.ssh_conn_cfg.host and
                                            self.ssh_conn_cfg.port == cfg2.ssh_conn_cfg.port and
                                            self.ssh_conn_cfg.username == cfg2.ssh_conn_cfg.username)) and \
               self.testnet == cfg2.testnet

    def copy_from(self, cfg2):
        """
        Copies alle attributes from another instance of this class.
        :param cfg2: Another instance of this type from which attributes will be copied.
        """
        self.host = cfg2.host
        self.port = cfg2.port
        self.username = cfg2.username
        self.password = cfg2.password
        self.use_ssh_tunnel = cfg2.use_ssh_tunnel
        self.use_ssl = cfg2.use_ssl
        self.testnet = self.testnet
        if self.use_ssh_tunnel:
            self.ssh_conn_cfg.host = cfg2.ssh_conn_cfg.host
            self.ssh_conn_cfg.port = cfg2.ssh_conn_cfg.port
            self.ssh_conn_cfg.username = cfg2.ssh_conn_cfg.username

    def is_http_proxy(self):
        """
        Returns if current config is a http proxy. Method is not very brilliant for now: we assume, that 
        proxy uses SSL while normal, "local" geweld does not.
        """
        if self.__use_ssl:
            return True
        else:
            return False

    @property
    def enabled(self):
        return self.__enabled

    @enabled.setter
    def enabled(self, active):
        if not isinstance(active, bool):
            raise Exception('Invalid type of "enabled" argument')
        else:
            self.__enabled = active

    @property
    def method(self):
        return self.__method

    @method.setter
    def method(self, method):
        if method != 'rpc':
            raise Exception('Not allowed method type: %s' % method)
        self.__method = method

    @property
    def host(self):
        return self.__host

    @host.setter
    def host(self, host):
        self.__host = host

    @property
    def port(self):
        return self.__port

    @port.setter
    def port(self, port):
        if isinstance(port, int):
            port = str(port)
        self.__port = port

    @property
    def username(self):
        return self.__username

    @username.setter
    def username(self, username):
        self.__username = username

    @property
    def password(self):
        return self.__password

    @password.setter
    def password(self, password):
        self.__password = password

    def get_password_encrypted(self):
        try:
            psw = encrypt(self.__password, APP_NAME_LONG, iterations=5)
            return psw
        except:
            return self.__password

    def set_encrypted_password(self, password, config_version: int):
        try:
            # check if password is a hexadecimal string - then it probably is an encrypted string with AES
            if config_version < 3:
                iterations = 100000
            else:
                # we don't need strong encryption of passwords from connections saved in ini file
                # for strong encryption user can encrypt the whole ini file with hardware wallet
                iterations = 5
            int(password, 16)
            try:
                p = decrypt(password, APP_NAME_LONG, iterations=iterations)
            except Exception:
                p = ''
            password = p
        except Exception as e:
            logging.warning('Password decryption error: ' + str(e))

        self.__password = password

    @property
    def use_ssl(self):
        return self.__use_ssl

    @use_ssl.setter
    def use_ssl(self, use_ssl):
        if not isinstance(use_ssl, bool):
            raise Exception('Ivalid type of "use_ssl" argument')
        self.__use_ssl = use_ssl

    @property
    def use_ssh_tunnel(self):
        return self.__use_ssh_tunnel

    @use_ssh_tunnel.setter
    def use_ssh_tunnel(self, use_ssh_tunnel):
        if not isinstance(use_ssh_tunnel, bool):
            raise Exception('Ivalid type of "use_ssh_tunnel" argument')
        self.__use_ssh_tunnel = use_ssh_tunnel

    @property
    def ssh_conn_cfg(self):
        return self.__ssh_conn_cfg

    @property
    def testnet(self):
        return self.__testnet

    @property
    def mainnet(self):
        return not self.__testnet

    @testnet.setter
    def testnet(self, testnet):
        if not isinstance(testnet, bool):
            raise Exception('Ivalid type of "testnet" argument')
        self.__testnet = testnet
