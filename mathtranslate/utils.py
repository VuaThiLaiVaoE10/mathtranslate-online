from . import __version__
from .config import config
from .update import get_latest_version
import ast
import json
import sys
import re
language_list = '''
Afrikaans            af
Irish                ga
Albanian             sq
Italian              it
Arabic               ar
Japanese             ja
Azerbaijani          az
Kannada              kn
Basque               eu
Korean               ko
Bengali              bn
Latin                la
Belarusian           be
Latvian              lv
Bulgarian            bg
Lithuanian           lt
Catalan              ca
Macedonian           mk
Chinese_Simplified   zh-CN
Malay                ms
Chinese_Traditional  zh-TW
Maltese              mt
Croatian             hr
Norwegian            no
Czech                cs
Persian              fa
Danish               da
Polish               pl
Dutch                nl
Portuguese           pt
English              en
Romanian             ro
Esperanto            eo
Russian              ru
Estonian             et
Serbian              sr
Filipino             tl
Slovak               sk
Finnish              fi
Slovenian            sl
French               fr
Spanish              es
Galician             gl
Swahili              sw
Georgian             ka
Swedish              sv
German               de
Tamil                ta
Greek                el
Telugu               te
Gujarati             gu
Thai                 th
Haitian_Creole       ht
Turkish              tr
Hebrew               iw
Ukrainian            uk
Hindi                hi
Urdu                 ur
Hungarian            hu
Vietnamese           vi
Icelandic            is
Welsh                cy
Indonesian           id
Yiddish              yi
'''

split = lambda s: re.split(r'\s+', s)


def parse_language_options(raw_language_list=None):
    """Parse language_list into sorted (DisplayName, code) pairs."""
    text = language_list if raw_language_list is None else raw_language_list
    options = []
    seen_codes = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        code = parts[-1].strip()
        display_name = " ".join(parts[:-1]).replace("_", " ").strip()
        if not display_name or not code or code in seen_codes:
            continue
        seen_codes.add(code)
        options.append((display_name, code))
    options.sort(key=lambda item: (item[0].lower(), item[1]))
    return options


def normalize_arxiv_id(raw_value: str) -> str:
    """Extract an arXiv identifier from a raw ID, arxiv: prefix, or arXiv URL."""
    value = str(raw_value or "").strip()
    if not value:
        return ""
    if value.lower().startswith("arxiv:"):
        value = value[6:].strip()
    match = re.search(
        r"arxiv\.org/(?:abs|pdf|e-print|html)/([^?#\s]+?)(?:\.pdf)?(?:[?#].*)?$",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip("/")
    return value


def check_update(require_updated=False):
    try:
        latest = get_latest_version()
    except Exception:
        return
    updated = __version__ == latest
    if updated:
        print("The current mathtranslate is latest")
    else:
        print("The current mathtranslate is not latest, please update by `pip install --upgrade mathtranslate`")
        if (not config.test_environment) and require_updated:
            sys.exit()


def add_arguments(parser):
    from .engines import AVAILABLE_ENGINES
    engines_str = ', '.join(AVAILABLE_ENGINES)
    parser.add_argument("-engine", default=config.default_engine, help=f'translation engine, available options include {engines_str}. default is {config.default_engine}')
    parser.add_argument("-from", default=config.default_language_from, dest='l_from', help=f'language from, default is {config.default_language_from}')
    parser.add_argument("-to", default=config.default_language_to, dest='l_to', help=f'language to, default is {config.default_language_to}')
    parser.add_argument("-threads", default=config.default_threads, type=int, help='threads for tencent translation, default is auto')
    parser.add_argument("-commands", type=str, help='add commands for translation from a file')
    parser.add_argument("--force-utf8", action='store_true', help='force reading file by utf8')
    parser.add_argument("--list", action='store_true', help='list codes for languages')
    parser.add_argument("--setkey", action='store_true', help='set id and key of tencent translator')
    parser.add_argument("--setdefault", action='store_true', help='set default translation engine and languages')
    parser.add_argument("--debug", action='store_true', help='Debug options for developers')
    parser.add_argument("--nocache", action='store_true', help='Debug options for developers')


def _validate_additional_commands(raw_commands):
    if not isinstance(raw_commands, (list, tuple)):
        raise ValueError("additional_commands must be a list of (name, nargs, arg_indices) entries")
    validated = []
    for entry in raw_commands:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError(f"Invalid command entry: {entry!r}")
        name, nargs, indices = entry
        if not isinstance(name, str) or not name:
            raise ValueError(f"Command name must be a non-empty string: {name!r}")
        if not isinstance(nargs, int) or isinstance(nargs, bool) or nargs < 0:
            raise ValueError(f"Command argument count must be a non-negative integer: {nargs!r}")
        if not isinstance(indices, (list, tuple)) or any(
            not isinstance(idx, int) or isinstance(idx, bool) or idx < 0
            for idx in indices
        ):
            raise ValueError(f"Command argument indices must be non-negative integers: {indices!r}")
        validated.append((name, nargs, tuple(indices)))
    return validated


def parse_additional_commands(content):
    """Safely parse custom -commands content from JSON or legacy Python assignment format."""
    stripped = content.strip()
    if not stripped:
        raise ValueError("Commands file is empty")

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        data = None
    else:
        if isinstance(data, dict):
            if "additional_commands" not in data:
                raise ValueError("JSON commands object must contain 'additional_commands'")
            return _validate_additional_commands(data["additional_commands"])
        return _validate_additional_commands(data)

    try:
        tree = ast.parse(content, mode='exec')
    except SyntaxError as exc:
        raise ValueError(f"Invalid commands file syntax: {exc}") from exc

    if not tree.body:
        raise ValueError("Commands file contains no definitions")

    found_value = None
    for stmt in tree.body:
        if (
            not isinstance(stmt, ast.Assign)
            or len(stmt.targets) != 1
            or not isinstance(stmt.targets[0], ast.Name)
            or stmt.targets[0].id != 'additional_commands'
        ):
            raise ValueError("Unsupported or unsafe statement in commands file")
        found_value = ast.literal_eval(stmt.value)

    return _validate_additional_commands(found_value)


def load_additional_commands(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    return parse_additional_commands(content)


def process_options(options):
    if options.setkey:
        print('Tencent secretID')
        config.set_variable(config.tencent_secret_id_path, config.tencent_secret_id_default)
        print('Tencent secretKey')
        config.set_variable(config.tencent_secret_key_path, config.tencent_secret_key_default)
        print('saved!')
        config.load()
        print('secretID:', config.tencent_secret_id)
        print('secretKey:', config.tencent_secret_key)
        sys.exit()

    if options.setdefault:
        print('Translation engine (google or tencent, default google)')
        config.set_variable(config.default_engine_path, config.default_engine_default)
        print('Translation language from (default en)')
        config.set_variable(config.default_language_from_path, config.default_language_from_default)
        print('Translation language to (default zh-CN)')
        config.set_variable(config.default_language_to_path, config.default_language_to_default)
        print('saved!')
        config.load()
        print('engine:', config.default_engine)
        print('language from:', config.default_language_from)
        print('language to:', config.default_language_to)
        sys.exit()

    if options.list:
        print(language_list)
        print('tencent translator does not support some of them')
        sys.exit()

    if options.force_utf8:
        from . import encoding
        encoding.force_utf8 = True

    if options.engine == 'tencent':
        haskey = (config.tencent_secret_id is not None) and (config.tencent_secret_key is not None)
        if not haskey:
            print('Please save ID and key for tencent translation api first by')
            print('translate_tex --setkey')
            sys.exit()
        if options.l_from == 'zh-CN':
            options.l_from = 'zh'
        if options.l_to == 'zh-CN':
            options.l_to = 'zh'
        if options.threads == 0:
            options.threads = 1
        elif options.threads > 1:
            options.threads = 1
            print('tencent engine does not support multi-threading, set to 1')

    if options.threads < 0:
        print('threads must be a non-zero integer number (>=0 where 0 means auto), set to auto')
        options.threads = 0

    additional_commands = []
    if options.commands:
        additional_commands = load_additional_commands(options.commands)
    config.mularg_command_list = config.raw_mularg_command_list + additional_commands

    print("Start")
    print('engine', options.engine)
    print('language from', options.l_from)
    print('language to', options.l_to)

    print('threads', options.threads if options.threads > 0 else 'auto')
    print()
