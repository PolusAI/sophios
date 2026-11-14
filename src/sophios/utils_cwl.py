from importlib.resources import files
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Final

import cwltool.context
from cwltool.errors import WorkflowException
import cwltool.load_tool
from cwltool.pack import pack
from cwltool.process import Process, custom_schemas, use_custom_schema
from schema_salad.exceptions import SchemaSaladException
from schema_salad.fetcher import DefaultFetcher
import yaml

from .lang.diagnostics import SophiosError
from .lang.error_codes import SophiosErrorCode
from .wic_types import Cwl, Yaml


def tool_process(cwl: Yaml, source: str) -> Yaml:
    """The process a tool document defines: the document itself, or its ``main`` if it is packed.

    A CWL runner runs a packed document through its ``main`` process and
    refuses one without; Sophios reads a tool the same way, once, where the
    document is loaded, so every reader of a tool's CWL sees one process.
    ``main`` is the ``$graph`` entry of that id, or the whole document when
    ``cwltool --pack`` packs a single tool (no ``$graph``, ``id: '#main'``).
    It is written out as a file of its own, so it takes the ``cwlVersion``,
    ``$namespaces`` and ``$schemas`` the packed document declares once at its
    top, and drops the ``id`` that placed it in the graph. A packer writes
    ``main``'s ports as ``#main/<port>``; they lose that prefix, so each port
    keeps the name it is bound by.

    Args:
        cwl (Yaml): A loaded CWL document.
        source (str): Where it came from, for the diagnostic.

    Raises:
        SophiosError: ``SUBWORKFLOW_INVALID`` for a ``$graph`` with no ``main``.
    """
    graph = cwl.get('$graph')
    if not isinstance(graph, list):
        return _as_main(cwl) if cwl.get('id') in ('main', '#main') else cwl
    processes = [entry for entry in graph if isinstance(entry, dict)]
    main = next((entry for entry in processes if entry.get('id') in ('main', '#main')), None)
    if main is None:
        ids = ', '.join(str(entry.get('id')) for entry in processes)
        raise SophiosError.error(
            SophiosErrorCode.SUBWORKFLOW_INVALID,
            f'{source} is a packed CWL document ($graph) with no main process; it holds: {ids}. '
            'CWL runs a packed document through its main, so name the process to run main, '
            'or unpack it into a file of its own')
    shared: Yaml = {key: cwl[key] for key in ('cwlVersion', '$namespaces', '$schemas') if key in cwl}
    return _as_main({**shared, **main})


def _as_main(process: Yaml) -> Yaml:
    """A packed ``main`` as a file of its own: without its ``id``, and its ports named as they are bound."""
    process = {key: value for key, value in process.items() if key != 'id'}
    for ports in ('inputs', 'outputs'):
        if isinstance(process.get(ports), list):
            process[ports] = [_without_main_prefix(port) for port in process[ports]]
    return process


def _without_main_prefix(port: Any) -> Any:
    """A port of a packed ``main`` with its ``id`` as ``<port>``, not ``#main/<port>`` or ``main/<port>``."""
    if isinstance(port, dict) and isinstance(port.get('id'), str):
        return {**port, 'id': port['id'].removeprefix('#').removeprefix('main/')}
    return port


#: The keys schema-salad resolves against the document that writes them.
_REFERENCE_KEYS: Final = ('$include', '$import', '$mixin')
#: A URI scheme of two letters or more, so a Windows drive (`C:`) is not one.
_URI_SCHEME: Final = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]+:')
#: cwltool's extension schemas, by CWL version, as `cwltool --enable-ext` registers them.
_EXTENSIONS: Final = (('v1.0', 'extensions.yml'), ('v1.1', 'extensions-v1.1.yml'), ('v1.2', 'extensions-v1.2.yml'))
_EXTENSION_NAMESPACE: Final = 'http://commonwl.org/cwltool'


class PackError(Exception):
    """cwltool could not pack a tool: its message, as cwltool wrote it."""


def _relative(location: Any) -> bool:
    """Whether a written location is resolved against the document that writes it."""
    return (isinstance(location, str) and '$(' not in location and '${' not in location
            and not _URI_SCHEME.match(location)
            and not PurePosixPath(location).is_absolute() and not PureWindowsPath(location).is_absolute())


def has_relative_reference(cwl: Any) -> bool:
    """Whether a tool refers to anything by a path relative to its own document.

    An `$include`, `$import` or `$mixin`, a relative `$schemas` entry, or a
    File or Directory `location` or `path` that is neither a URI, an absolute
    path, nor an expression. Such a tool means something only where it was
    written. `dockerLoad` and `dockerImport` are left alone, as schema-salad leaves them.
    """
    match cwl:
        case dict():
            schemas = cwl.get('$schemas', [])
            return (any(key in cwl for key in _REFERENCE_KEYS)
                    or any(_relative(schema) for schema in (schemas if isinstance(schemas, list) else [schemas]))
                    or cwl.get('class') in ('File', 'Directory') and any(
                        _relative(cwl.get(key)) for key in ('location', 'path'))
                    or any(has_relative_reference(value) for value in cwl.values()))
        case list():
            return any(has_relative_reference(item) for item in cwl)
        case _:
            return False


def _register_extensions() -> None:
    """Load cwltool's extension schemas, which `cwltool.main` does for a run with `--enable-ext`.

    Not through `cwltool.main`: it imports `pwd`, which Windows does not have,
    and Sophios compiles there. Registering drops cwltool's cached schema, so a
    version already registered is left as it is.
    """
    for version, name in _EXTENSIONS:
        text = files('cwltool').joinpath(name).read_text('utf-8')
        if custom_schemas.get(version) != (_EXTENSION_NAMESPACE, text):
            use_custom_schema(version, _EXTENSION_NAMESPACE, text)


class InvalidTool(Exception):
    """cwltool's validator refuses a tool: what it found wrong, one line to each place, without terminal colour."""


#: The position cwltool's message line starts with, `<file or URI>:<line>:<column>:`, and the nesting after it.
_POSITION: Final = re.compile(r'^\S+?:(\d+):(\d+):( *)')
_COLOUR: Final = re.compile(r'\x1b\[[0-9;]*m')
#: What schema-salad heads a nested resolution error with: the loader's object id, and the document's URI.
_IDENTIFIED: Final = re.compile(r'\(\d+\) (?:\([^)\s]*\) )?')
_ATTEMPT: Final = re.compile(r"tried (?:array of <[^>]*>|\S+) but\b\s*|,? tried \S+$")


def _plain(error: Exception, name: str, *, positions: bool) -> str:
    """cwltool's message for `error` as a reader needs it: each place a line, its position and what is wrong there.

    cwltool wraps the message to the width of a terminal, colours it, writes the document's path at the head of every
    line and at each mention of an identifier, and says `tried 'X' but` before each alternative it rejected. An error
    met while resolving the document is nested, each level headed by the loader's object id and the document's URI:
    the levels stay lines, without the ids. The document is `name`; the positions go when they count lines of a
    document no file holds.
    """
    places: list[list[str]] = []
    for line in _COLOUR.sub('', str(error)).splitlines():
        if found := _POSITION.match(line):
            places.append([f"{found[1]}:{found[2]}" if positions else '', found[3] + line[found.end():].strip()])
        elif _IDENTIFIED.match(line.lstrip()):
            places.append(['', line[:len(line) - len(line.lstrip())] + line.strip()])
        elif places:
            places[-1][1] += ' ' + line.strip()
        else:
            places.append(['', line.strip()])
    named = re.compile(rf"[^\s']*{re.escape(name)}#?")
    lines = [(position, _ATTEMPT.sub('', named.sub('', _IDENTIFIED.sub('', text))).replace("Object ''", 'The tool')
              .rstrip()) for position, text in places]
    return '\n'.join(f'{position} {text}' if position else text for position, text in lines)


def load_validated(uri: str, text: str | None = None) -> tuple[cwltool.context.LoadingContext, str]:
    """cwltool's loader and validator, on the one loading context Sophios uses, for the tool at `uri`.

    The tool is read from `uri`, or is `text` when no file holds it (an inline `run:` body, a tool built in Python):
    `uri` is then the document it is written as, which need not exist, and a reference relative to it is resolved
    there. `$schemas` are resolved against the tool but not fetched, as a run with `--skip-schemas` does. `make_tool`
    makes the process.

    Raises:
        SchemaSaladException, WorkflowException: cwltool's own, for a tool it refuses.

    Returns:
        The loading context, and the document's URI.
    """
    class _Serving(DefaultFetcher):
        """The fetcher cwltool loads with, reading `text` for `uri` and every other URI as usual."""

        def fetch_text(self, url: str, content_types: list[str] | None = None) -> str:
            return text if text is not None and url == uri else super().fetch_text(url, content_types)

        def check_exists(self, url: str) -> bool:
            # A remote reference (an ontology under `$schemas`) is not fetched, so it is not asked for either.
            return (text is not None and url == uri) or url.startswith(('http://', 'https://')) \
                or super().check_exists(url)

    _register_extensions()
    context, document, resolved = cwltool.load_tool.fetch_document(uri, cwltool.context.LoadingContext(
        {'fetcher_constructor': _Serving, 'skip_schemas': True}))
    return cwltool.load_tool.resolve_and_validate_document(context, document, resolved)


def make_tool(uri: str, text: str | None = None) -> Process | None:
    """The process cwltool makes of the tool at `uri` (or `text`, as `load_validated` says): every check it makes.

    Beyond the schema, making the tool validates its hints and parses its JavaScript expressions, as
    `cwltool --validate` does.

    Raises:
        SchemaSaladException, WorkflowException: cwltool's own, for a tool it refuses.

    Returns:
        The process, or None where `cwltool.workflow` cannot be imported (Windows lacks `pwd`), and the document
        check alone ran.
    """
    context, resolved = load_validated(uri, text)
    try:
        from cwltool.workflow import default_make_tool  # pylint: disable=import-outside-toplevel
    except ImportError:  # Windows lacks `pwd`, which cwltool.workflow needs: only the document is checked.
        return None
    context.construct_tool_object = default_make_tool
    return cwltool.load_tool.make_tool(resolved, context)


def validate(path: str, cwl: Cwl | None = None) -> None:
    """Check that cwltool accepts the tool at `path`, as it does to run it.

    A tool that is a file is validated as that file, so a position names a line the author can open. A tool no file
    holds, which `cwl` is given for (an inline `run:` body, a tool built in Python), is validated as `cwl` written as
    `path`, whatever file is there.

    Raises:
        InvalidTool: what cwltool found wrong, as `_plain` says it.
    """
    location = Path(path).absolute()
    try:
        make_tool(location.as_uri(), None if cwl is None else yaml.safe_dump(cwl, sort_keys=False))
    except (SchemaSaladException, WorkflowException) as error:
        raise InvalidTool(_plain(error, location.name, positions=cwl is None)) from error
    except ValueError:
        # cwltool names every file relative to the working directory, and `os.path.relpath` raises for a path on
        # another Windows drive: such a tool cannot be loaded by cwltool, so it is not validated.
        return


def packed(text: str, base_uri: str) -> Yaml:
    """A tool packed by cwltool as if its text were the document at `base_uri`.

    Every reference relative to `base_uri` is resolved there: an `$include`
    becomes the text it names, an `$import` its content, a relative
    `location` an absolute URI. The result is read as Sophios reads any
    packed tool (`tool_process`). Nothing is read
    from `base_uri` itself, which need not exist: an inline `run:` body has no file.
    `$schemas` are resolved against the tool but not fetched.

    Args:
        text (str): The tool, as YAML or JSON.
        base_uri (str): The document the text was written as.

    Raises:
        PackError: cwltool's own message, when it cannot load or pack the tool.

    Returns:
        Yaml: The packed tool.
    """
    try:
        context, uri = load_validated(base_uri, text)
        tool = json.loads(json.dumps(pack(context, uri)))
    except (SchemaSaladException, WorkflowException) as error:
        raise PackError(str(error)) from error
    return tool_process(tool, base_uri)
