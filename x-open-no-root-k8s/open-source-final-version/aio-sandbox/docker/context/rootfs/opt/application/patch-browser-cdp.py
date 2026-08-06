#!/usr/bin/env python3
"""patch-browser-cdp.py — rewrite the in-venv browser.py so that the
websocket URL returned by /v1/browser/info does NOT prepend /cdp.

Why (README §4.4.5): the upstream `_rewrite_websocket_urls` always adds
"/cdp" to the WebSocket path.  In the upstream that was fine because
python-server itself proxied /cdp/devtools/*.  In our offline build
python-server does NOT have such a handler — but our nginx does have a
`location ^/cdp/devtools/` block that strips /cdp and forwards to
chrome :9222 directly.  So we drop the in-app rewrite; nginx does it.

For more conservative auditing we keep everything else of the function
intact; only the path-prefix injection is removed.

Usage:  python3 patch-browser-cdp.py <venv-lib-dir>
If no venv path is given, falls back to /opt/server-venv (the offline
default installed by Dockerfile.offline §11).
"""
from __future__ import annotations
import re
import sys
import glob
import os


def patch_file(path: str) -> bool:
    src = open(path).read()
    if 'AIO-OFFLINE-PATCH' in src:
        # Already patched; idempotent.
        print(f'[skip] {path} already patched')
        return False

    # The original method body starts with:
    #
    #     def _rewrite_websocket_urls(
    #         self, url: str, ...
    #     ) -> str:
    #         # tries 3 *at-most* concatenation strategies
    #         ...
    #         return url
    #
    # We replace this whole def-block with one that just re-scheme's the
    # URL and re-hosts to the proxy_host (preserving the original path).
    new_def = (
        '    def _rewrite_websocket_urls(\n'
        '        self, url: str, proxy_host: str, ws_protocol: str, path_prefix: str = ""\n'
        '    ) -> str:\n'
        '        # AIO-OFFLINE-PATCH: do not add /cdp prefix; nginx ui_browser.conf\n'
        '        # already strips /cdp and forwards to chrome :9222.  See README §4.4.5.\n'
        '        from urllib.parse import urlparse, urlunparse\n'
        '        u = urlparse(url)\n'
        '        return urlunparse((ws_protocol, proxy_host, u.path, u.params, u.query, u.fragment))\n'
    )

    # Find the method block and its terminator (next "    def " or "    @").
    pattern = re.compile(
        r'    def _rewrite_websocket_urls\([^)]*\)[^\n]*:\n'
        r'(?:[^\n]*\n)*?'
        r'(?=\n    (?:def |@))',
        re.MULTILINE,
    )
    m = pattern.search(src)
    if not m:
        print(f'[warn] {path}: _rewrite_websocket_urls not found — leaving unchanged')
        return False

    new_src = src[:m.start()] + new_def + src[m.end():]

    # Edit in place atomically.
    tmp = path + '.patch.tmp'
    with open(tmp, 'w') as f:
        f.write(new_src)
    os.rename(tmp, path)
    print(f'[ok] {path}: patched _rewrite_websocket_urls')
    return True


def main(argv):
    if len(argv) >= 2:
        roots = [argv[1]]
    else:
        roots = ['/opt/server-venv']

    for root in roots:
        # A future Python may install into python3.X/site-packages
        # instead of python3/site-packages; pick whichever exists.
        for pattern in (
            f'{root}/lib/python3*/site-packages/app/services/browser.py',
            f'{root}/lib/python3/site-packages/app/services/browser.py',
        ):
            for path in sorted(glob.glob(pattern)):
                if patch_file(path):
                    break

    # Drop .pyc caches so uvicorn picks up the new code on next start.
    for pattern in (
        '/opt/server-venv/lib/python3*/site-packages/app/services/__pycache__/browser*',
        '/opt/server-venv/lib/python3/site-packages/app/services/__pycache__/browser*',
    ):
        for p in glob.glob(pattern):
            try:
                os.remove(p)
                print(f'[rm] {p}')
            except OSError:
                pass


if __name__ == '__main__':
    main(sys.argv)
