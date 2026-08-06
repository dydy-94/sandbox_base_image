#!/usr/bin/env python3
"""Standalone Python port of the bonus asset section in
prepare-apt-archives.sh — fetches google-chrome .deb, code-server,
noVNC, websocat without depending on docker or bash."""
import os
import sys
import urllib.request
import tarfile
import shutil
import stat
import subprocess

CTX = os.path.dirname(os.path.abspath(__file__))


def curl(url, dest, timeout=240):
    print(f"  -> {url}", flush=True)
    # Try curl first (handles redirects well)
    try:
        subprocess.run(
            ["curl.exe", "-fsSL", "--connect-timeout", "15", "--max-time",
             str(timeout), "--retry", "2", "-o", dest, url],
            check=True,
        )
        return os.path.getsize(dest)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    # Fallback to urllib
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return os.path.getsize(dest)
    except Exception as e:
        print(f"  WARN: urllib failed: {e}", flush=True)
        if os.path.exists(dest):
            os.unlink(dest)
        return 0


# ---- 1. google-chrome .deb -------------------------------------------------
print("=== google-chrome ===")
chrome_dir = os.path.join(CTX, "chrome-deb")
os.makedirs(chrome_dir, exist_ok=True)
open(os.path.join(chrome_dir, ".keep"), "a").close()
deb = os.path.join(chrome_dir, "google-chrome-stable_amd64.deb")
if not os.path.exists(deb) or os.path.getsize(deb) < 100000:
    for url in [
        "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb",
        "https://mirrors.tuna.tsinghua.edu.cn/chromium-browser/google-chrome-stable_current_amd64.deb",
    ]:
        size = curl(url, deb)
        if size > 100000 and deb.endswith(".deb"):
            # Validate: peek at the gzip header
            with open(deb, "rb") as f:
                sig = f.read(8)
            if sig.startswith(b"!<arch>\n"):
                print(f"  ok: {size} bytes")
                break
            print("  not a valid .deb; retrying")
            os.unlink(deb)
    else:
        print("  WARN: chrome download failed; will be missing in image")

# ---- 2. code-server --------------------------------------------------------
print("\n=== code-server ===")
code_dir = os.path.join(CTX, "code-server")
os.makedirs(code_dir, exist_ok=True)
open(os.path.join(code_dir, ".keep"), "a").close()
CODE_VERSION = "4.96.4"
tarball = os.path.join(code_dir, f"code-server-{CODE_VERSION}-linux-amd64.tar.gz")
if not os.path.exists(tarball) or os.path.getsize(tarball) < 1_000_000:
    ok = False
    for url in [
        f"https://github.com/coder/code-server/releases/download/v{CODE_VERSION}/code-server-{CODE_VERSION}-linux-amd64.tar.gz",
        f"https://npmmirror.com/mirrors/code-server/v{CODE_VERSION}/code-server-{CODE_VERSION}-linux-amd64.tar.gz",
    ]:
        size = curl(url, tarball)
        if size > 1_000_000:
            print(f"  ok: {size} bytes")
            ok = True
            break
        if os.path.exists(tarball):
            os.unlink(tarball)
    if not ok:
        print("  WARN: code-server download failed")
# Generate install.sh
if os.path.exists(tarball) and os.path.getsize(tarball) > 1_000_000:
    with open(os.path.join(code_dir, "install.sh"), "w", encoding="utf-8") as f:
        f.write(f"""#!/bin/bash
set -e
cd /opt/code-server
tar -xzf code-server-{CODE_VERSION}-linux-amd64.tar.gz
ln -sf /opt/code-server/code-server-{CODE_VERSION}-linux-amd64/bin/code-server /usr/local/bin/code-server
echo "code-server installed"
""")
    os.chmod(os.path.join(code_dir, "install.sh"), 0o755)
elif not os.path.exists(os.path.join(code_dir, "install.sh")):
    with open(os.path.join(code_dir, "install.sh"), "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\necho 'WARN: code-server tarball missing'")
    os.chmod(os.path.join(code_dir, "install.sh"), 0o755)

# ---- 3. noVNC --------------------------------------------------------------
print("\n=== noVNC ===")
novnc_dir = os.path.join(CTX, "novnc")
os.makedirs(novnc_dir, exist_ok=True)
open(os.path.join(novnc_dir, ".keep"), "a").close()
NOVNC_VERSION = "1.6.0"
unpacked = os.path.join(novnc_dir, "noVNC")
tarball = os.path.join(novnc_dir, "novnc.tar.gz")
if not os.path.isdir(unpacked):
    ok = False
    for url in [
        f"https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.tar.gz",
        f"https://npmmirror.com/mirrors/noVNC/v{NOVNC_VERSION}.tar.gz",
    ]:
        size = curl(url, tarball)
        if size > 100_000:
            print(f"  ok: {size} bytes, extracting…", flush=True)
            try:
                with tarfile.open(tarball, "r:gz") as t:
                    t.extractall(novnc_dir)
                extracted_dir = os.path.join(novnc_dir, f"noVNC-{NOVNC_VERSION}")
                if os.path.isdir(extracted_dir):
                    if os.path.isdir(unpacked):
                        shutil.rmtree(unpacked)
                    shutil.move(extracted_dir, unpacked)
                os.unlink(tarball)
                ok = True
                break
            except Exception as e:
                print(f"  extract failed: {e}")
                if os.path.exists(tarball):
                    os.unlink(tarball)
    if not ok:
        print("  WARN: noVNC download failed")
else:
    print("  already extracted")

# ---- 4. websocat -----------------------------------------------------------
print("\n=== websocat ===")
web_dir = os.path.join(CTX, "websocat")
os.makedirs(web_dir, exist_ok=True)
open(os.path.join(web_dir, ".keep"), "a").close()
WEBSOCAT_VERSION = "1.13.0"
bin_path = os.path.join(web_dir, "websocat-x86_64-unknown-linux-musl")
if not os.path.exists(bin_path) or os.path.getsize(bin_path) < 100_000:
    ok = False
    for url in [
        f"https://github.com/vi/websocat/releases/download/v{WEBSOCAT_VERSION}/websocat.x86_64-unknown-linux-musl",
        f"https://npmmirror.com/mirrors/websocat/v{WEBSOCAT_VERSION}/websocat.x86_64-unknown-linux-musl",
    ]:
        size = curl(url, bin_path)
        if size > 100_000:
            os.chmod(bin_path, 0o755)
            print(f"  ok: {size} bytes")
            ok = True
            break
        if os.path.exists(bin_path):
            os.unlink(bin_path)
    if not ok:
        print("  WARN: websocat download failed")

print("\n=== summary ===")
for label, path in [
    ("chrome deb", os.path.join(chrome_dir, "google-chrome-stable_amd64.deb")),
    ("code-server tarball", tarball if "tarball" in dir() else ""),
    ("noVNC dir", unpacked),
    ("websocat", bin_path),
]:
    if not path:
        continue
    if os.path.isfile(path):
        print(f"  {label}: {os.path.getsize(path):,} bytes")
    elif os.path.isdir(path):
        total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fn in os.walk(path) for f in fn)
        print(f"  {label}: {len(os.listdir(path))} entries, {total:,} bytes")
    else:
        print(f"  {label}: MISSING")
