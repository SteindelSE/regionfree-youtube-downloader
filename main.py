
# =============================================
# RegionFree YouTube Downloader
# Copyright (c) 2026 SteindelSE. All rights reserved.
#
# Original Creator: SteindelSE
# https://github.com/SteindelSE
#
# This file is part of RegionFree YouTube Downloader and may not be copied,
# modified, or distributed except as permitted by the license.
# =============================================

# =========================
# Imports
# =========================

import argparse
import atexit
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

import urllib.request
import requests
import yt_dlp
from stem import Signal
from stem.control import Controller


# =========================
# Constants
# =========================

TOR_SOCKS_PORT = 12212
TOR_CONTROL_PORT = 12213

SAFE_COUNTRIES = ["se", "ch", "nl", "is", "no", "fi", "dk", "de"]


# =========================
# Globals
# =========================

tor_process = None
torrc_path = None
tor_started_by_us = False
VERBOSE = False


# =========================
# Utility Functions
# =========================

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name)
    return name.replace(" ", "_")


def get_downloads_folder() -> str:
    if os.name == "nt":
        downloads = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
        if os.path.isdir(downloads):
            return downloads
    else:
        downloads = os.path.expanduser("~/Downloads")
        if os.path.isdir(downloads):
            return downloads

    return os.path.dirname(os.path.abspath(__file__))


def download_with_watchdog(
    url: str,
    dest_path: str,
    label: str = "Downloading",
    timeout: int = 30,
    chunk_size: int = 1024 * 64,
):
    """
    Stream-download a file with progress output and a stall watchdog.
    Aborts if no data is received for `timeout` seconds.
    """
    last_data_time = time.time()
    downloaded = 0

    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))

        start_time = time.time()

        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    if time.time() - last_data_time > timeout:
                        raise RuntimeError(
                            f"{label} stalled for {timeout} seconds"
                        )
                    continue

                f.write(chunk)
                downloaded += len(chunk)
                last_data_time = time.time()

                # Progress output
                elapsed = max(time.time() - start_time, 0.1)
                speed = downloaded / elapsed / 1024  # KB/s

                if total:
                    percent = downloaded / total * 100
                    msg = (
                        f"\r{label}: "
                        f"{percent:6.2f}% "
                        f"({downloaded // 1024} KB / {total // 1024} KB) "
                        f"{speed:6.1f} KB/s"
                    )
                else:
                    msg = (
                        f"\r{label}: "
                        f"{downloaded // 1024} KB "
                        f"{speed:6.1f} KB/s"
                    )

                print(msg, end="", flush=True)

    print()  # newline after completion


# =========================
# ffmpeg Bootstrap (RESTORED)
# =========================

def ensure_ffmpeg() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ffmpeg_dir = os.path.join(script_dir, "ffmpeg-latest")

    for root, _, files in os.walk(ffmpeg_dir):
        for file in files:
            if file.lower() == "ffmpeg.exe":
                return os.path.join(root, file)

    print("ffmpeg not found. Downloading ffmpeg...")

    ffmpeg_url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-git-essentials.7z"
    ffmpeg_7z = os.path.join(script_dir, "ffmpeg.7z")

    download_with_watchdog(
        ffmpeg_url,
        ffmpeg_7z,
        label="ffmpeg"
    )

    def find_7z():
        if shutil.which("7z"):
            return "7z"

        sevenzip = os.path.join(script_dir, "7zr.exe")
        if not os.path.exists(sevenzip):
            print("Downloading portable 7-Zip...")
            download_with_watchdog(
                "https://www.7-zip.org/a/7zr.exe",
                sevenzip,
                label="7-Zip"
            )
        return sevenzip

    sevenzip = find_7z()

    subprocess.run(
        [sevenzip, "x", ffmpeg_7z, f"-o{ffmpeg_dir}"],
        check=True
    )

    os.remove(ffmpeg_7z)

    for root, _, files in os.walk(ffmpeg_dir):
        for file in files:
            if file.lower() == "ffmpeg.exe":
                return os.path.join(root, file)

    raise RuntimeError("ffmpeg.exe not found after extraction")


# =========================
# Tor Functions
# =========================

def renew_tor_ip():
    with Controller.from_port(port=TOR_CONTROL_PORT) as controller:
        controller.authenticate()
        controller.signal(Signal.NEWNYM)


def is_tor_proxy_live(host="127.0.0.1", port=TOR_SOCKS_PORT, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def test_tor_connection():
    proxies = {
        "http": f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}",
        "https": f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}",
    }
    r = requests.get("https://check.torproject.org/api/ip", proxies=proxies, timeout=10)
    if r.ok:
        data = r.json()
        print(f"Tor IP: {data.get('IP')} | IsTor: {data.get('IsTor')}")


def terminate_tor_process():
    global tor_process, torrc_path, tor_started_by_us

    if tor_process and tor_process.poll() is None and tor_started_by_us:
        tor_process.terminate()
        try:
            tor_process.wait(timeout=5)
        except Exception:
            tor_process.kill()

    tor_process = None
    tor_started_by_us = False

    if torrc_path and os.path.exists(torrc_path):
        os.remove(torrc_path)
        torrc_path = None


# =========================
# Tor Startup
# =========================

def ensure_tor_files() -> str:
    """
    Ensure Tor Expert Bundle is present locally and return absolute path to tor.exe
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tor_dir = os.path.join(script_dir, "tor")
    tor_bin = "tor.exe" if os.name == "nt" else "tor"
    tor_path = os.path.join(tor_dir, tor_bin)

    geoip = os.path.join(script_dir, "data", "geoip")
    geoip6 = os.path.join(script_dir, "data", "geoip6")

    # Already present → fast path
    if os.path.exists(tor_path) and os.path.exists(geoip) and os.path.exists(geoip6):
        return tor_path

    print("Required Tor files missing. Downloading Tor Expert Bundle...")

    sys_os = platform.system().lower()
    sys_arch = platform.machine().lower()

    if sys_os.startswith("win"):
        os_handle = "windows"
        arch = "x86_64" if "64" in sys_arch else "i686"
    elif sys_os == "darwin":
        os_handle = "macos"
        arch = "aarch64" if "arm" in sys_arch else "x86_64"
    elif sys_os == "linux":
        os_handle = "linux"
        if "aarch64" in sys_arch or "arm64" in sys_arch:
            arch = "aarch64"
        elif "arm" in sys_arch:
            arch = "armv7"
        else:
            arch = "x86_64"
    else:
        raise RuntimeError(f"Unsupported OS: {sys_os}")


    index_url = "https://dist.torproject.org/torbrowser/"
    html = urllib.request.urlopen(index_url).read().decode()

    versions = [
        v for v in re.findall(r'href="([0-9.]+)/"', html) if "a" not in v
    ]
    versions.sort(key=lambda s: list(map(int, s.split("."))))
    latest = versions[-1]

    version_url = f"{index_url}{latest}/"
    html = urllib.request.urlopen(version_url).read().decode()

    pattern = rf"tor-expert-bundle-{os_handle}-{arch}-.*?\.tar\.gz"
    match = re.search(pattern, html)
    if not match:
        raise RuntimeError("Could not locate Tor Expert Bundle")

    tarball = match.group(0)
    tarball_path = os.path.join(script_dir, tarball)

    print(f"Downloading {tarball}...")
    download_with_watchdog(
        version_url + tarball,
        tarball_path,
        label=f"Tor {latest}"
    )


    print("Extracting Tor...")
    with tarfile.open(tarball_path, "r:gz") as tar:
        tar.extractall(script_dir)

    os.remove(tarball_path)

    os.makedirs(tor_dir, exist_ok=True)
    data_dir = os.path.join(script_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    for root, _, files in os.walk(script_dir):
        for file in files:
            if file in {"tor", "tor.exe"}:
                shutil.move(os.path.join(root, file), os.path.join(tor_dir, file))
            elif file in {"geoip", "geoip6"}:
                shutil.move(os.path.join(root, file), os.path.join(data_dir, file))

    return tor_path


def start_tor_process(country_code: str):
    global tor_process, torrc_path, tor_started_by_us

    terminate_tor_process()

    # Ensure Tor exists and get absolute path
    tor_path = ensure_tor_files()
    if not os.path.exists(tor_path):
        raise RuntimeError(f"Tor executable not found at {tor_path}")

    torrc_content = f"""
GeoIPFile ./data/geoip
GeoIPv6File ./data/geoip6
SocksPort {TOR_SOCKS_PORT}
ControlPort {TOR_CONTROL_PORT}
ExitNodes {{{country_code}}}
"""

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".torrc") as f:
        f.write(torrc_content)
        torrc_path = f.name

    tor_process = subprocess.Popen(
        [tor_path, "-f", torrc_path],
        stdout=subprocess.PIPE if VERBOSE else subprocess.DEVNULL,
        stderr=subprocess.PIPE if VERBOSE else subprocess.DEVNULL,
        text=True,
    )

    tor_started_by_us = True


def wait_for_tor(timeout=40):
    start = time.time()
    while time.time() - start < timeout:
        if is_tor_proxy_live():
            return True
        time.sleep(1)
    return False


# =========================
# Download Logic
# =========================

def download_video(url: str):
    ffmpeg_path = ensure_ffmpeg()
    downloads = get_downloads_folder()

    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best",
        "proxy": f"socks5://127.0.0.1:{TOR_SOCKS_PORT}",
        "ffmpeg_location": ffmpeg_path,
        "outtmpl": os.path.join(downloads, "%(title)s.%(ext)s"),
        "windowsfilenames": True,
        "restrictfilenames": True,
        "retries": 1,
        "merge_output_format": "mp4",
    }

    for country in SAFE_COUNTRIES:
        print(f"\nTrying Tor exit node: {country.upper()}")
        start_tor_process(country)

        if not wait_for_tor():
            print("Tor failed to start, trying next country")
            continue

        test_tor_connection()

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            print("Download succeeded!")
            return
        except Exception as e:
            print(f"Failed via {country.upper()}: {e}")

    print("All Tor exit nodes failed.")


# =========================
# Cleanup & Main
# =========================

atexit.register(terminate_tor_process)
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    VERBOSE = args.verbose

    url = input("Enter a video URL: ").strip()
    download_video(url)
