"""Download winlibs gcc zip into Nuitka cache (with resume support)."""
import os
import sys
import urllib.request

URLS = [
    "https://gh-proxy.com/https://github.com/brechtsanders/winlibs_mingw/releases/download/15.2.0posix-13.0.0-msvcrt-r6/winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip",
    "https://ghfast.top/https://github.com/brechtsanders/winlibs_mingw/releases/download/15.2.0posix-13.0.0-msvcrt-r6/winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip",
    "https://github.com/brechtsanders/winlibs_mingw/releases/download/15.2.0posix-13.0.0-msvcrt-r6/winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip",
]

DEST = os.path.join(
    os.environ["LOCALAPPDATA"],
    r"Nuitka\Nuitka\Cache\downloads\gcc\x86_64\15.2.0posix-13.0.0-msvcrt-r6",
    "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip",
)


def main() -> int:
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    for url in URLS:
        try:
            existing = os.path.getsize(DEST) if os.path.exists(DEST) else 0
            req = urllib.request.Request(url)
            if existing > 0:
                req.add_header("Range", f"bytes={existing}-")
            print(f"[DL] {url} (resume from {existing})", flush=True)
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = resp.headers.get("Content-Length")
                print(f"[DL] status={resp.status} content-length={total}", flush=True)
                mode = "ab" if existing > 0 and resp.status == 206 else "wb"
                if mode == "wb":
                    existing = 0
                done = existing
                with open(DEST, mode) as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if done % (16 << 20) < (1 << 20):
                            print(f"[DL] {done / (1 << 20):.0f} MiB", flush=True)
            print(f"[DL] finished, size={os.path.getsize(DEST)}", flush=True)
            import zipfile
            with zipfile.ZipFile(DEST) as z:
                bad = z.testzip()
            if bad is None:
                print("[OK] zip verified", flush=True)
                return 0
            print(f"[ERROR] zip corrupt at {bad}, retry next url", flush=True)
            os.remove(DEST)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] {url}: {e}", flush=True)
    print("[FATAL] all urls failed", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
