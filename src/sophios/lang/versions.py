"""The Sophios language version: what exists, and how one is chosen.

Owns the version list and implements the selection rules normatively defined
in the language spec, §6.
"""
from collections.abc import Iterable
from typing import Final

from .diagnostics import SophiosError
from .error_codes import SophiosErrorCode
from .spans import SourceSpan

#: Every version that has ever existed, oldest first. Append-only. The newest is what untagged sources resolve to.
KNOWN_VERSIONS: Final[tuple[str, ...]] = ('0.0.1', '0.1.0')

#: The version a tool (`command:` or `script:`) is first syntax in.
TOOLS_SINCE: Final = '0.1.0'


def resolve(requested: str | None = None,
            pins: tuple[str, ...] = (),
            *,
            known: tuple[str, ...] = KNOWN_VERSIONS,
            needs: Iterable[tuple[str, SourceSpan | None]] = ()) -> str:
    """Choose the one version this compilation runs under.

    `requested` is the explicit setting and always wins when given; asking
    for a version that does not exist is reported, never coerced. `pins` are
    the per-file tags found in the tree, and more than one distinct pin is a
    conflict. With neither, the newest known version is chosen. `needs` are
    the versions the syntax the tree reaches is first in, each with where it
    is written: a chosen version older than one is reported there.

    `known` exists so the mechanism can be tested against fabricated version
    histories; production callers never pass it.
    """
    asked = set(pins) if requested is None else {requested}
    if unknown := sorted(asked - set(known)):
        raise SophiosError.error(
            SophiosErrorCode.UNKNOWN_LANG_VERSION,
            f'Unknown lang_version {", ".join(map(repr, unknown))}. Known versions: {", ".join(known)}')
    if len(asked) > 1:
        raise SophiosError.error(
            SophiosErrorCode.LANG_VERSION_CONFLICT,
            f'One compilation resolves to one version, but the tree pins {", ".join(sorted(asked, key=known.index))}.',
            'Remove the conflicting lang_version tags, or set one explicitly to override them all.')
    chosen = next(iter(asked), known[-1])
    for version, span in needs:
        if known.index(version) > known.index(chosen):
            raise SophiosError.error(
                SophiosErrorCode.LANG_VERSION_TOO_OLD,
                f'This is lang_version {version} syntax, and the compilation is pinned to {chosen}: pin {version}, or '
                'remove the pin (`--lang_version`, `wic: lang_version:`, `compile(lang_version=)`).', span=span)
    return chosen
