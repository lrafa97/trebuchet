"""TOML reader for Pythons without `tomllib` (< 3.11), no dependencies.

On Python 3.11+ the standard library `tomllib` is used. This module exists so
that Trebuchet runs on old Raspberry Pi OS releases (Bullseye ships Python 3.9,
Buster 3.7) without `pip install`.

It only covers the subset that profiles and machines use:
  - `key = value` with strings ("..." with escapes, '...' literals), integers,
    floats, booleans and simple lists of those types;
  - tables `[name]` and arrays of tables `[[name]]`;
  - comments with `#`.
Everything else (dates, inline tables, multiline strings, dotted keys)
raises TOMLDecodeError instead of being silently misread.
"""
from __future__ import annotations

import json
import re

try:  # Python 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on the Python version
    _tomllib = None


class TOMLDecodeError(ValueError):
    """Invalid TOML file, or outside the supported subset."""


_KEY = re.compile(r"[A-Za-z0-9_-]+")
_INT = re.compile(r"[+-]?(0|[1-9][0-9]*(_[0-9]+)*)$")
_FLOAT = re.compile(r"[+-]?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$")


def _strip_comment(line: str) -> str:
    """Remove the trailing comment, respecting # inside strings."""
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in ('"', "'"):
            quote = c
        elif c == "#":
            return line[:i]
        i += 1
    return line


def _parse_string(text: str, lineno: int):
    """Read a string at the start of `text`; return (value, rest)."""
    q = text[0]
    if text.startswith(q * 3):
        raise TOMLDecodeError(f"line {lineno}: multiline strings are not supported")
    i = 1
    while i < len(text):
        c = text[i]
        if q == '"' and c == "\\":
            i += 2
            continue
        if c == q:
            raw = text[1:i]
            if q == "'":
                return raw, text[i + 1:]
            try:
                return json.loads('"' + raw + '"'), text[i + 1:]
            except ValueError:
                raise TOMLDecodeError(f"line {lineno}: invalid escape in string") from None
        i += 1
    raise TOMLDecodeError(f"line {lineno}: unterminated string")


def _parse_scalar(token: str, lineno: int):
    token = token.strip()
    if token == "true":
        return True
    if token == "false":
        return False
    if _INT.match(token):
        return int(token.replace("_", ""))
    if _FLOAT.match(token):
        return float(token)
    raise TOMLDecodeError(f"line {lineno}: unsupported value: {token!r}")


def _parse_value(text: str, lineno: int):
    text = text.strip()
    if not text:
        raise TOMLDecodeError(f"line {lineno}: missing value")
    if text[0] in ('"', "'"):
        value, rest = _parse_string(text, lineno)
        if rest.strip():
            raise TOMLDecodeError(f"line {lineno}: extra text after string")
        return value
    if text[0] == "[":
        return _parse_array(text, lineno)
    if text[0] == "{":
        raise TOMLDecodeError(f"line {lineno}: inline tables are not supported")
    return _parse_scalar(text, lineno)


def _parse_array(text: str, lineno: int):
    if not text.endswith("]"):
        raise TOMLDecodeError(f"line {lineno}: array must fit on one line and end with ]")
    body = text[1:-1].strip()
    items = []
    while body:
        if body[0] in ('"', "'"):
            value, body = _parse_string(body, lineno)
        else:
            m = re.match(r"[^,]+", body)
            if not m:
                raise TOMLDecodeError(f"line {lineno}: invalid array")
            value = _parse_scalar(m.group(0), lineno)
            body = body[m.end():]
        items.append(value)
        body = body.strip()
        if body.startswith(","):
            body = body[1:].strip()
        elif body:
            raise TOMLDecodeError(f"line {lineno}: missing comma in array")
    return items


def loads(text: str) -> dict:
    root: dict = {}
    current = root
    defined_tables = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[["):
            if not line.endswith("]]"):
                raise TOMLDecodeError(f"line {lineno}: invalid header")
            name = line[2:-2].strip()
            if not _KEY.fullmatch(name):
                raise TOMLDecodeError(f"line {lineno}: unsupported table name: {name!r}")
            arr = root.setdefault(name, [])
            if not isinstance(arr, list):
                raise TOMLDecodeError(f"line {lineno}: {name!r} already defined as another type")
            current = {}
            arr.append(current)
            continue
        if line.startswith("["):
            if not line.endswith("]"):
                raise TOMLDecodeError(f"line {lineno}: invalid header")
            name = line[1:-1].strip()
            if not _KEY.fullmatch(name):
                raise TOMLDecodeError(f"line {lineno}: unsupported table name: {name!r}")
            if name in defined_tables or name in root:
                raise TOMLDecodeError(f"line {lineno}: table {name!r} repeated")
            defined_tables.add(name)
            current = {}
            root[name] = current
            continue
        if "=" not in line:
            raise TOMLDecodeError(f"line {lineno}: expected key = value")
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key[0] in ('"', "'"):
            try:
                key, rest = _parse_string(key, lineno)
            except TOMLDecodeError:
                raise
            if rest.strip():
                raise TOMLDecodeError(f"line {lineno}: invalid key")
        elif not _KEY.fullmatch(key):
            raise TOMLDecodeError(f"line {lineno}: unsupported key: {key!r}")
        if key in current:
            raise TOMLDecodeError(f"line {lineno}: key {key!r} repeated")
        current[key] = _parse_value(value, lineno)
    return root


def load(fh) -> dict:
    """Like tomllib.load: `fh` is a file opened in binary mode."""
    data = fh.read()
    if _tomllib is not None:
        try:
            return _tomllib.loads(data.decode("utf-8"))
        except _tomllib.TOMLDecodeError as exc:
            raise TOMLDecodeError(str(exc)) from None
    try:
        return loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise TOMLDecodeError(f"file is not UTF-8: {exc}") from None


def load_fallback(fh) -> dict:
    """The built-in reader, even when tomllib is available (used in tests)."""
    return loads(fh.read().decode("utf-8"))
