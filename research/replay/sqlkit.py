"""Render the research SQL files with validated parameters, and freeze extract parts.

One code path for both uses, so the SQL that runs in the scratch-Postgres tests is byte-for-byte the SQL
that runs read-only against production after hours:

* ``render(path, **params)`` substitutes ``:NAME`` placeholders with quoted ISO dates (validated, never
  free text) and returns the file's guarded form: ``BEGIN READ ONLY`` ... ``COMMIT``.
* ``render_single_statement(path, **params)`` returns the same SELECT for clients that return only the
  last statement's rows (the Supabase MCP ``execute_sql``): session-level ``default_transaction_read_only``,
  ``statement_timeout`` and ``work_mem`` are set first and the SELECT is last.
* ``write_part`` / ``part_header`` produce part files in the format run_stage_a_b.load_parts verifies.
"""
import hashlib
import os
import re
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
_PLACEHOLDER = re.compile(r':([A-Z][A-Z_]*)\b')


def _quote(value) -> str:
    if isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError(f'parameter must be an ISO date, got {value!r}')
    date.fromisoformat(value)
    return f"'{value}'"


def _substitute(sql: str, params: dict) -> str:
    """Replace :NAME placeholders that appear OUTSIDE string literals and comments. Every placeholder found
    must be bound and every bound name must be used; text inside quotes (e.g. 'HH24:MI:SS') is untouched."""
    out, found = [], set()
    pattern = re.compile(r"(--[^\n]*)|('(?:[^']|'')*')|(?<![:\w]):([A-Z][A-Z_]*)\b")
    pos = 0
    for m in pattern.finditer(sql):
        out.append(sql[pos:m.start()])
        if m.group(3):
            name = m.group(3)
            found.add(name)
            if name not in params:
                raise ValueError(f'unbound placeholder :{name}')
            out.append(_quote(params[name]))
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(sql[pos:])
    unused = sorted(set(params) - found)
    if unused:
        raise ValueError(f'parameters not used by the SQL: {unused}')
    return ''.join(out)


def _settings(sql: str):
    timeout = re.search(r"statement_timeout\s*=\s*'([^']+)'", sql)
    work_mem = re.search(r"work_mem\s*=\s*'([^']+)'", sql)
    nestloop = re.search(r'enable_nestloop\s*=\s*(off|on)', sql)
    return timeout.group(1) if timeout else None, work_mem.group(1) if work_mem else None, \
        nestloop.group(1) if nestloop else None


def render(path: str, **params) -> str:
    with open(path, encoding='utf-8') as fh:
        sql = fh.read()
    out = _substitute(sql, params)
    if 'begin read only;' not in out.lower():
        raise ValueError(f'{path}: missing BEGIN READ ONLY guard')
    return out


def select_body(path: str, **params) -> str:
    sql = render(path, **params)
    start = re.search(r'(?im)^with\b', sql).start()
    end = sql.rindex('commit;')
    return sql[start:end].rstrip().rstrip(';')


def render_single_statement(path: str, **params) -> str:
    with open(path, encoding='utf-8') as fh:
        raw = fh.read()
    timeout, work_mem, nestloop = _settings(raw)
    head = ['set default_transaction_read_only = on;']
    if timeout:
        head.append(f"set statement_timeout = '{timeout}';")
    if work_mem:
        head.append(f"set work_mem = '{work_mem}';")
    if nestloop:
        head.append(f'set enable_nestloop = {nestloop};')
    return '\n'.join(head) + '\n' + select_body(path, **params) + ';'


def part_header(part: str, a: str, b: str, n: int, md5: str) -> str:
    return f'#format=nf_quotes_v2\n#part={part}\n#range={a}..{b}\n#n={n}\n#md5={md5}\n#body\n'


def write_part(out_dir: str, part: str, a: str, b: str, n: int, md5: str, body: str) -> dict:
    """Write part<NN>.txt; refuse if Postgres md5 / line count do not match the body handed over."""
    got = hashlib.md5(body.encode('utf-8')).hexdigest()
    lines = len(body.split('\n')) if body else 0
    if got != md5 or lines != int(n):
        raise ValueError(f'part {part}: body does not match Postgres (md5 {md5} vs {got}, n {n} vs {lines})')
    path = os.path.join(out_dir, f'part{part}.txt')
    text = part_header(part, a, b, n, md5) + body + '\n'
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return {'part': part, 'range': f'{a}..{b}', 'n': int(n), 'md5': md5,
            'file_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


EXTRACT_SQL = os.path.join(HERE, 'extract_nf_quotes.sql')
CROSSCHECK_SQL = os.path.join(HERE, 'stage_a_sql_crosscheck.sql')
FILL_SQL = os.path.normpath(os.path.join(HERE, '..', 'evidence', 'fill_reconciliation_v1.sql'))
PAYLOAD_SQL = os.path.normpath(os.path.join(HERE, '..', 'evidence', 'payload_snapshot_lists.sql'))
