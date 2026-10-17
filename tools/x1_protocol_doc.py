#!/usr/bin/env python3
"""Regenerate (or --check) the digest-bound parts of the frozen X1 protocol document from code:
the collector-contract line, the digest line and the canonical ```json block. Prose is untouched."""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app', 'src', 'main', 'python'))
import paper_buying_research as p  # noqa: E402

DOC = os.path.join(HERE, '..', 'docs', 'research', p.PROTOCOL_ID + '.md')


def render(text):
    text = re.sub(r'^Collector contract: `[^`]*`', 'Collector contract: `%s`' % p.COLLECTOR_CONTRACT_VERSION,
                  text, count=1, flags=re.M)
    text = re.sub(r'(^Protocol digest [^\n]*?\*\*`)[0-9a-f]{64}(`\*\*)', r'\g<1>%s\g<2>' % p.DIGEST,
                  text, count=1, flags=re.M)
    body = json.dumps(p.PROTOCOL, indent=2, sort_keys=True)
    return re.sub(r'```json\n.*?\n```', lambda _m: '```json\n%s\n```' % body, text, count=1, flags=re.S)


def main(argv):
    with open(DOC) as fh:
        text = fh.read()
    new = render(text)
    if '--check' in argv:
        print('OK' if new == text else 'STALE')
        return 0 if new == text else 1
    with open(DOC, 'w') as fh:
        fh.write(new)
    print(p.DIGEST)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
