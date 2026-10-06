"""Leitor de TOML para Pythons sem `tomllib` (< 3.11), sem dependências.

Em Python 3.11+ usa-se o `tomllib` da biblioteca padrão. Este módulo existe para
que o Trebuchet corra em Raspberry Pi OS antigos (Bullseye traz Python 3.9,
Buster 3.7) sem `pip install`.

Só cobre o subconjunto que os perfis e as máquinas usam:
  - `chave = valor` com strings ("..." com escapes, '...' literais), inteiros,
    floats, booleanos e listas simples de valores desses tipos;
  - tabelas `[nome]` e listas de tabelas `[[nome]]`;
  - comentários com `#`.
Tudo o resto (datas, inline tables, strings multilinha, chaves com pontos)
levanta TOMLDecodeError em vez de ser lido mal em silêncio.
"""
from __future__ import annotations

import json
import re

try:  # Python 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - depende da versão do Python
    _tomllib = None


class TOMLDecodeError(ValueError):
    """Ficheiro TOML inválido, ou fora do subconjunto suportado."""


_KEY = re.compile(r"[A-Za-z0-9_-]+")
_INT = re.compile(r"[+-]?(0|[1-9][0-9]*(_[0-9]+)*)$")
_FLOAT = re.compile(r"[+-]?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$")


def _strip_comment(line: str) -> str:
    """Remove o comentário final, respeitando # dentro de strings."""
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
    """Lê uma string no início de `text`; devolve (valor, resto)."""
    q = text[0]
    if text.startswith(q * 3):
        raise TOMLDecodeError(f"linha {lineno}: strings multilinha não são suportadas")
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
                raise TOMLDecodeError(f"linha {lineno}: escape inválido na string") from None
        i += 1
    raise TOMLDecodeError(f"linha {lineno}: string sem fecho")


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
    raise TOMLDecodeError(f"linha {lineno}: valor não suportado: {token!r}")


def _parse_value(text: str, lineno: int):
    text = text.strip()
    if not text:
        raise TOMLDecodeError(f"linha {lineno}: valor em falta")
    if text[0] in ('"', "'"):
        value, rest = _parse_string(text, lineno)
        if rest.strip():
            raise TOMLDecodeError(f"linha {lineno}: texto a mais depois da string")
        return value
    if text[0] == "[":
        return _parse_array(text, lineno)
    if text[0] == "{":
        raise TOMLDecodeError(f"linha {lineno}: inline tables não são suportadas")
    return _parse_scalar(text, lineno)


def _parse_array(text: str, lineno: int):
    if not text.endswith("]"):
        raise TOMLDecodeError(f"linha {lineno}: lista tem de caber numa só linha e fechar com ]")
    body = text[1:-1].strip()
    items = []
    while body:
        if body[0] in ('"', "'"):
            value, body = _parse_string(body, lineno)
        else:
            m = re.match(r"[^,]+", body)
            if not m:
                raise TOMLDecodeError(f"linha {lineno}: lista inválida")
            value = _parse_scalar(m.group(0), lineno)
            body = body[m.end():]
        items.append(value)
        body = body.strip()
        if body.startswith(","):
            body = body[1:].strip()
        elif body:
            raise TOMLDecodeError(f"linha {lineno}: falta vírgula na lista")
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
                raise TOMLDecodeError(f"linha {lineno}: cabeçalho inválido")
            name = line[2:-2].strip()
            if not _KEY.fullmatch(name):
                raise TOMLDecodeError(f"linha {lineno}: nome de tabela não suportado: {name!r}")
            arr = root.setdefault(name, [])
            if not isinstance(arr, list):
                raise TOMLDecodeError(f"linha {lineno}: {name!r} já definido como outro tipo")
            current = {}
            arr.append(current)
            continue
        if line.startswith("["):
            if not line.endswith("]"):
                raise TOMLDecodeError(f"linha {lineno}: cabeçalho inválido")
            name = line[1:-1].strip()
            if not _KEY.fullmatch(name):
                raise TOMLDecodeError(f"linha {lineno}: nome de tabela não suportado: {name!r}")
            if name in defined_tables or name in root:
                raise TOMLDecodeError(f"linha {lineno}: tabela {name!r} repetida")
            defined_tables.add(name)
            current = {}
            root[name] = current
            continue
        if "=" not in line:
            raise TOMLDecodeError(f"linha {lineno}: esperava chave = valor")
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key[0] in ('"', "'"):
            try:
                key, rest = _parse_string(key, lineno)
            except TOMLDecodeError:
                raise
            if rest.strip():
                raise TOMLDecodeError(f"linha {lineno}: chave inválida")
        elif not _KEY.fullmatch(key):
            raise TOMLDecodeError(f"linha {lineno}: chave não suportada: {key!r}")
        if key in current:
            raise TOMLDecodeError(f"linha {lineno}: chave {key!r} repetida")
        current[key] = _parse_value(value, lineno)
    return root


def load(fh) -> dict:
    """Como tomllib.load: `fh` é um ficheiro aberto em modo binário."""
    data = fh.read()
    if _tomllib is not None:
        try:
            return _tomllib.loads(data.decode("utf-8"))
        except _tomllib.TOMLDecodeError as exc:
            raise TOMLDecodeError(str(exc)) from None
    try:
        return loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise TOMLDecodeError(f"ficheiro não é UTF-8: {exc}") from None


def load_fallback(fh) -> dict:
    """O leitor próprio, mesmo quando o tomllib existe (usado nos testes)."""
    return loads(fh.read().decode("utf-8"))
