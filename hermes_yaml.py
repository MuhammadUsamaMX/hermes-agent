"""Shared YAML 1.1 policy for config, manifests, and frontmatter.

Ruamel's native schema includes bare y/n booleans and rejects duplicate keys.
Every operation owns its parser/emitter; instances must not be shared by threads.
"""

from io import StringIO
from typing import Any, IO, overload

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError as YAMLError
from ruamel.yaml.resolver import VersionedResolver


class _Yaml11Resolver(VersionedResolver):
    # Quote strings like "off" without adding a %YAML directive to every config/snippet.
    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 1)


def _load(document: str | bytes, *, pure: bool) -> Any:
    yaml = YAML(typ="safe", pure=pure)
    yaml.version = (1, 1)
    return yaml.load(document)


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """Read standard YAML data; existing configs use YAML 1.1 booleans.

    The pure parser defines what parses: Windows ARM64 has no C extension, and libyaml rejects
    documents the pure parser accepts (``[{url: http://h}]``), so a C rejection is re-read pure.
    """
    document = stream if isinstance(stream, (str, bytes)) else stream.read()
    try:
        return _load(document, pure=False)
    except YAMLError:
        return _load(document, pure=True)


@overload
def safe_dump(
    data: Any, stream: None = None, *, default_flow_style: bool = False,
    sort_keys: bool = True, allow_unicode: bool = True, width: int = 80,
) -> str: ...


@overload
def safe_dump(
    data: Any, stream: IO[str], *, default_flow_style: bool = False,
    sort_keys: bool = True, allow_unicode: bool = True, width: int = 80,
) -> None: ...


def safe_dump(
    data: Any,
    stream: IO[str] | None = None,
    *,
    default_flow_style: bool = False,
    sort_keys: bool = True,
    allow_unicode: bool = True,
    width: int = 80,
) -> str | None:
    """Write standard YAML data with readable Unicode and indented block lists.

    Every caller persists this text and reads it back, so the document must parse to the data it
    was given: a fold at *width* that would change a value is re-emitted unfolded (see
    ``ROUNDTRIP_YAML_WIDTH``). ``width`` is therefore a wrapping hint, not a guarantee.
    """
    text = _emit(data, default_flow_style=default_flow_style, sort_keys=sort_keys,
                 allow_unicode=allow_unicode, width=width)
    if _reads_back_differently(text, data):
        # The fold, not the data, changed what the document means — emit again without folding.
        text = _emit(data, default_flow_style=default_flow_style, sort_keys=sort_keys,
                     allow_unicode=allow_unicode, width=ROUNDTRIP_YAML_WIDTH)
    if stream is not None:
        stream.write(text)
        return None
    return text


def _emit(
    data: Any,
    *,
    default_flow_style: bool,
    sort_keys: bool,
    allow_unicode: bool,
    width: int,
) -> str:
    # The C emitter ignores sequence offsets and escapes astral Unicode.
    yaml = YAML(typ="safe", pure=True)
    yaml.Resolver = _Yaml11Resolver
    yaml.default_flow_style = default_flow_style
    yaml.allow_unicode = allow_unicode
    yaml.width = width
    yaml.sort_base_mapping_type_on_output = sort_keys
    yaml.indent(mapping=2, sequence=4, offset=2)
    output = StringIO()
    yaml.dump(data, output)
    return output.getvalue()


def _reads_back_differently(text: str, data: Any) -> bool:
    """True when re-parsing our own output would not give the caller's data back."""
    try:
        return safe_load(text) != data
    except YAMLError:
        # Our own document does not parse: let the unfolded emission have a go, then accept
        # whatever it produces rather than failing a writer that never failed before.
        return True


# ruamel's emitter can change a double-quoted value when it folds a long line right after an
# escaped backslash (``D:\\Cent…`` → ``D:\\`` + bare newline): the fold reloads as a literal space
# and a no-op save mutates the stored value (#119844). The same fold inside a run of spaces in a
# plain scalar collapses ``two spaces`` to one, so the corruption is not double-quoted-specific.
# Config writes must be value-preserving, so no emitter in the tree may fold when folding would
# change what a reader gets back: ``roundtrip_yaml()`` never folds (``None`` does NOT disable
# folding on 0.18.x; only a large width does), and ``safe_dump`` re-emits unfolded when its own
# output does not read back as the data it was given.
ROUNDTRIP_YAML_WIDTH = 2**31 - 1


def roundtrip_yaml() -> YAML:
    """Create a fresh comment/quote-preserving editor for user-authored YAML."""
    yaml = YAML(typ="rt")
    yaml.width = ROUNDTRIP_YAML_WIDTH
    yaml.Resolver = _Yaml11Resolver
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml
