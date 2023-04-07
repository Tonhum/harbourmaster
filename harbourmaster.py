#!/usr/bin/env python3

import contextlib
import datetime
import fnmatch
import hashlib
import json
import pathlib
import re
import shutil
import sys
import tempfile
import textwrap
import zipfile

from difflib import Differ
from pathlib import Path

## Insert our extra modules.
sys.path.insert(0, str(Path(__file__).parent / 'libs'))

import requests

################################################################################
## Override this for custom config folder, otherwise it will reside in `SCRIPT_DIRECTORY/config`
CONFIG_PATH=None
PORTS_PATH=None
UPDATE_FREQUENCY=(60 * 60 * 22) # Only check automatically once a day.

SOURCE_DEFAULT_PORTMASTER = """
{
    "prefix": "pm",
    "api": "PortMasterV1",
    "name": "PortMaster",
    "url": "https://api.github.com/repos/PortsMaster/PortMaster-Releases/releases/latest",
    "last_checked": null,
    "version": 1,
    "data": {}
}
""".strip()

DEBUG=True

################################################################################
## Default CONFIG_PATH

if CONFIG_PATH is None:
    CONFIG_PATH = Path(__file__).parent / 'config'
elif isinstance(CONFIG_PATH, str):
    CONFIG_PATH = Path(__file__)
elif isinstance(config_dir, pathlib.PurePath):
    # This is good.
    pass
else:
    print(f"Error: {CONFIG_PATH!r} is set to something weird.")
    exit(255)

## Default PORTS_PATH
if PORTS_PATH is None:
    PORTS_PATH = Path(__file__).parent.parent
elif isinstance(CONFIG_PATH, str):
    PORTS_PATH = Path(__file__)
elif isinstance(config_dir, pathlib.PurePath):
    # This is good.
    pass
else:
    print(f"Error: {PORTS_PATH!r} is set to something weird.")
    exit(255)

################################################################################
## 
def fetch(url):
    r = requests.get(url)
    if r.status_code != 200:
        print(f"Failed to download {r.status_code}")
        return None

    return r

def fetch_json(url):
    r = fetch(url)
    if r is None:
        return None

    return r.json()

def fetch_text(url):
    r = fetch(url)
    if r is None:
        return None

    return r.text

def datetime_compare(time_a, time_b=None):
    if isinstance(time_a, str):
        time_a = datetime.datetime.fromisoformat(time_a)

    if time_b is None:
        time_b = datetime.datetime.now()
    elif isinstance(time_b, str):
        time_b = datetime.datetime.fromisoformat(time_b)

    return (time_b - time_a).seconds

@contextlib.contextmanager
def make_temp_directory():
    temp_dir = tempfile.mkdtemp()
    if DEBUG:
        print(f"Created temp dir {temp_dir}")
    try:
        yield Path(temp_dir)

    finally:
        if DEBUG:
            print(f"Cleaning up {temp_dir}")

        shutil.rmtree(temp_dir)




################################################################################
## APIS

class BaseSource():
    def __init__(self, hm, file_name, config):
        pass


class PortMasterV1(BaseSource):
    VERSION = 2

    def __init__(self, hm, file_name, config):
        self._hm = hm
        self._file_name = file_name
        self._config = config
        self._prefix = config['prefix']

        if config['version'] != self.VERSION:
            print("Cache out of date.")
            self.update()
        elif self._config['last_checked'] is None:
            print("First check.")
            self.update()
        elif datetime_compare(self._config['last_checked']) > UPDATE_FREQUENCY:
            print("Auto Update.")
            self.update()
        else:
            self._data = self._config['data']['data']
            self._info = self._config['data']['info']
            self.ports = self._config['data']['ports']
            self.utils = self._config['data']['utils']

    def clean_name(self, text):
        return text.lower()

    def update(self, force=False):
        print(f"{self._config['name']}: Fetching latest ports")
        data = fetch_json(self._config['url'])

        self._data = {}
        self._info = {}
        self.ports = []
        self.utils = []

        ## Load data from the assets.
        for asset in data['assets']:
            result = {
                'name': asset['name'],
                'size': asset['size'],
                'url': asset['browser_download_url'],
                }

            self._data[self.clean_name(asset['name'])] = result

            if asset['name'].lower().endswith('.squashfs'):
                self.utils.append(self.clean_name(asset['name']))

        print(f"{self._config['name']}: Fetching info")
        for line in fetch_text(self._data['ports.md']['url']).split('\n'):
            line = line.strip()
            if line == '':
                continue

            info = self._parse_port_info(line)
            info_key = self.clean_name(info['file'])

            self._info[info_key] = info

            self.ports.append(info_key)

        self._config['version'] = self.VERSION

        self._config['data']['ports'] = self.ports
        self._config['data']['utils'] = self.utils
        self._config['data']['info']  = self._info
        self._config['data']['data']  = self._data

        self._config['last_checked'] = datetime.datetime.now().isoformat()

        self._save()
        print(f"{self._config['name']}: Done.")

    def _save(self):
        with self._file_name.open('w') as fh:
            json.dump(self._config, fh, indent=4)

    def _parse_port_info(self, text):
        # Super jank
        keys = {
            'title_f': 'title',
            'title_p': 'title',
            'locat': 'file',
            }

        info = {
            'title': '',
            'desc': '',
            'file': '',
            'porter': '',
            'opengl': False,
            'power': False,
            'rtr': False,
            'genres': [],
            }

        for key, value in re.findall(r'(?:^|\s)(\w+)=\"(.+?)"(?=\s+\w+=|$)', text.strip()):
            if key.lower() == 'title_f':
                info['opengl'] = True

            if key.lower() == 'title_p':
                info['power'] = True

            key = keys.get(key.lower(), key.lower())
            if key == 'title':
                value = value[:-2].replace('_', ' ')

            # Zips with spaces in their names get replaced with '.'
            if '%20' in value:
                value = value.replace('%20', '.')
                value = value.replace('..', '.')

            # Special keys
            if key == 'runtype':
                key, value = 'rtr', True
            elif key == 'genres':
                value = value.split(',')

            info[key] = value

        return info


    def port_info(self, port_name):
        assert port_name in self.ports, f"{port_name} not found."

        return self._info[port_name]


    def download(self, port_name):
        zip_file = self._hm.temp_dir / port_name

        md5_source = fetch_text(self._data[port_name + '.md5']['url'])
        if md5_source is None:
            print(f"Unable to download md5 file: {self._data[port_name + '.md5']['url']!r}")
            return None

        md5_source = md5_source.strip()

        r = requests.get(self._data[port_name]['url'], stream=True)

        if r.status_code != 200:
            print(f"Unable to download port file: {self._data[port_name]['url']!r}")
            return None

        total_length = r.headers.get('content-length')
        if total_length is None:
            total_length = self._data[port_name]['size']
        else:
            total_length = int(total_length)

        md5 = hashlib.md5()

        print(f"Downloading {self._data[port_name]['url']!r} - {total_length / 1024 / 1024:.02f} MB")

        length = 0
        with zip_file.open('wb') as fh:
            for data in r.iter_content(chunk_size=(104096), decode_unicode=False):
                md5.update(data)
                fh.write(data)
                length += len(data)

                amount = int(length / total_length * 50)
                sys.stdout.write(f"\r[{'.' * amount}{' ' * (50 - amount)}] - {length // 1024 // 1024:3d} / {total_length // 1024 // 1024:3d} MB")
                sys.stdout.flush()

            print("\n")

        md5_file = md5.hexdigest()

        if md5_file != md5_source:
            zip_file.unlink()
            print(f"Port file doesn't mate the md5 file: {md5_file} != {md5_source}")
            return None

        print("Success!")
        return zip_file


    def portmd(self, port_name):
        info = self.port_info(port_name)
        output = []

        if info['opengl']:
            output.append(f'Title_P="{info["title"].replace(" ", "_")} ."')
        elif info['power']:
            output.append(f'Title_F="{info["title"].replace(" ", "_")} ."')
        else:
            output.append(f'Title="{info["title"].replace(" ", "_")} ."')

        output.append(f'Desc="{info["desc"]}"')
        output.append(f'porter="{info["porter"]}"')
        output.append(f'locat="{info["file"]}"')
        if info['rtr']:
            output.append(f'runtype="rtr"')

        return ' '.join(output)

SOURCE_APIS = {
    'PortMasterV1': PortMasterV1,
    }

################################################################################
## Config loading
class HarbourMaster():
    def __init__(self, cfg_path=None, ports_path=None, temp_dir=None):
        """
        config = load_config()
        """

        if cfg_path is None:
            cfg_path = CONFIG_PATH

        if ports_path is None:
            ports_path = PORTS_PATH

        if isinstance(cfg_path, str):
            cfg_path = Path(cfg_path)
        elif not isinstance(cfg_path, pathlib.PurePath):
            raise ValueError('cfg_path')

        if isinstance(ports_path, str):
            ports_path = Path(ports_path)
        elif not isinstance(ports_path, pathlib.PurePath):
            raise ValueError('ports_path')

        self.temp_dir = temp_dir
        self.cfg_path = cfg_path
        self.ports_path = ports_path
        self.sources = {}
        self.ports = []
        self.utils = []

        if not cfg_path.is_dir():
            cfg_path.mkdir(0o755)

            with (cfg_path / '000_portmaster.source.json').open('w') as fh:
                fh.write(SOURCE_DEFAULT_PORTMASTER)

            with (cfg_path / 'config.json').open('w') as fh:
                fh.write('{"first_run": true}')

        self.load_sources()

    def load_sources(self):
        source_files = list(self.cfg_path.glob('*.source.json'))
        source_files.sort()

        for source_file in source_files:
            with source_file.open() as fh:
                source_data = json.load(fh)

                assert 'prefix' in source_data, f'Missing key "prefix" in {source_file}.'
                assert 'api' in source_data, f'Missing key "api" in {source_file}.'
                assert 'name' in source_data, f'Missing key "name" in {source_file}.'
                assert 'last_checked' in source_data, f'Missing key "last_checked" in {source_file}.'
                assert 'data' in source_data, f'Missing key "data" in {source_file}.'
                assert source_data['api'] in SOURCE_APIS, f'Unknown api {source_data["api"]}.'

            source = SOURCE_APIS[source_data['api']](self, source_file, source_data)

            self.sources[source_data['prefix']] = source

################################################################################
## Commands
def do_update(hm, argv):
    """
    Update available ports, checks for new releases.
    """
    if len(argv) == 0:
        argv = ('all', )

    if argv[0].lower() == 'all':
        print('Updating all port sources:')
        for source in hm.sources:
            hm.sources[source].update()
    else:
        for arg in argv:
            if arg not in hm.sources:
                print(f'Unknown source {arg}')
                continue

            print(f'Updating {arg}:')
            hm.sources[arg].update()

    return 0


def do_list(hm, argv):
    """
    List available ports
    """
    print("Available ports:")
    for source_prefix, source in hm.sources.items():
        for port in source.ports:
            port_info = source.port_info(port)
            print(f"- {source_prefix}/{port}: {port_info['title']}")
            print(f"      {port_info['desc']}")
            print()

    return 0


def do_portsmd(hm, argv):
    """
    List available ports
    """
    for source_prefix, source in hm.sources.items():
        for port in source.ports:
            print(source.portmd(port))
            print()

    return 0


def do_install(hm, argv):
    """
    Install a port

    {command} install Half-Life.zip               # Install from highest priority repo
    {command} install pm/Half-Life.zip            # Install specifically from portmaster repo
    {command} install kloptops/Half-Life.zip      # Install specifically from kloptops repo
    """
    if len(argv) == 0:
        print("Missing arguments.")
        return do_help(hm, ['install'])

    for arg in argv:
        if '/' in arg:
            repo, port_name = arg.split('/', 1)
        else:
            repo = '*'
            port_name = arg

        for source_prefix, source in hm.sources.items():
            if not fnmatch.fnmatch(source_prefix, repo):
                continue

            if source.clean_name(port_name) not in source.ports:
                continue

            file_name = source.download(source.clean_name(port_name))

    return 0


def do_help(hm, argv):
    """
    Shows general help or help for a particular command.

    {command} help
    {command} help list
    """
    command = sys.argv[0]

    if len(argv) > 0:
        if argv[0].lower() not in all_commands:
            print(f"Error: unknown help command {argv[0]!r}")
            do_help(hm, build_configs, [])
            return

        print(textwrap.dedent(all_commands[argv[0].lower()].__doc__.format(command=command)).strip())
        return

    print(f"{command} <update> [source or all] ")
    # print(f"{command} <install/upgrade> [source/]<port_name> ")
    # print(f"{command} <uninstall> [source/]<port_name> ")
    print(f"{command} <list/portsmd> [source or all] [... filters]")
    print(f"{command} <help> <command>")
    print()
    print("All available commands: " + (', '.join(all_commands.keys())))


all_commands = {
    'update': do_update,
    'portsmd': do_portsmd,
    'list': do_list,
    'install': do_install,
    'help': do_help,
    }

def main(argv):

    with make_temp_directory() as temp_dir:

        hm = HarbourMaster(temp_dir=temp_dir)

        if len(argv) == 1:
            all_commands['help'](hm, [])
            return 1

        if argv[1].lower() not in all_commands:
            print(f'Command {argv[1]!r} not found.')
            all_commands['help'](hm, [])
            return 2

        return all_commands[argv[1].lower()](hm, argv[2:])


if __name__ == '__main__':
    exit(main(sys.argv))
