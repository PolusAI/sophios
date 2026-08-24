"""The Sophios language version: what exists, and how one is chosen.

The scheme and its selection rules have one normative home — the language
reference, §7 — and one enforcement, `tests/core/test_lang_version.py`.
This module implements them and owns the version list itself;
it does not restate them, so it cannot disagree with them.

Today exactly one version exists, so resolution always lands on 0.0.1 — but
the *mechanism* is written for the general case and property-tested with
fabricated version lists, because the first real version bump is precisely
when nobody will want to discover the resolver was a stub.
"""
from typing import Final

from .diagnostics import Code, SophiosError

#: Every version that has ever existed, oldest first. Append-only.
KNOWN_VERSIONS: Final[tuple[str, ...]] = ('0.0.1',)

#: The newest version — what untagged sources resolve to.
LANG_VERSION: Final[str] = KNOWN_VERSIONS[-1]

#: The namespace under which the resolved version is annotated in emitted CWL,
#: so the annotation is a declared extension field and the output stays valid.
ANNOTATION_NAMESPACE: Final[str] = 'sophios'
ANNOTATION_NAMESPACE_URI: Final[str] = 'https://github.com/PolusAI/sophios#'
ANNOTATION_KEY: Final[str] = f'{ANNOTATION_NAMESPACE}:lang_version'


def resolve(requested: str | None = None,
            pins: tuple[str, ...] = (),
            *,
            known: tuple[str, ...] = KNOWN_VERSIONS) -> str:
    """Choose the one version this compilation runs under.

    `requested` is the explicit setting and always wins when given; asking for
    a version that does not exist is reported, never coerced. `pins` are the
    per-file tags found in the tree — exact pins, so more than one distinct
    pin is a conflict. With neither, the newest known version is chosen: never
    silently anything lower.

    `known` exists so the mechanism can be tested against fabricated version
    histories; production callers never pass it.
    """
    if requested is not None:
        if requested not in known:
            raise SophiosError.error(
                Code.UNKNOWN_LANG_VERSION,
                f'Unknown lang_version {requested!r}. Known versions: {", ".join(known)}')
        return requested

    distinct = sorted(set(pins), key=known.index) if set(pins) <= set(known) else None
    if distinct is None:
        bad = sorted(set(pins) - set(known))
        raise SophiosError.error(
            Code.UNKNOWN_LANG_VERSION,
            f'Unknown lang_version tag(s) {", ".join(map(repr, bad))}. Known versions: {", ".join(known)}')
    if len(distinct) > 1:
        raise SophiosError.error(
            Code.LANG_VERSION_CONFLICT,
            f'One compilation resolves to one version, but the tree pins {", ".join(distinct)}.',
            'Remove the conflicting lang_version tags, or set one explicitly to override them all.')
    if distinct:
        return distinct[0]

    return known[-1]
