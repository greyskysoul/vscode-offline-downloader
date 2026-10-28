#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VS Code 离线安装包下载器

功能：
  1. 下载 VS Code 安装包（Windows exe、Linux deb/rpm/tgz、macOS zip），
     支持最新稳定版或指定版本，可通过交互式复选框或 --targets 选择平台；
  2. 下载远程开发 Server + CLI：Linux x64、Linux arm64；
  3. 按 download_config.yaml 下载插件（通用版本、平台专用版本和语言包匹配）；
  4. 支持 aria2 多连接、wget 或内置 urllib 下载，未指定时自动选择。

依赖：Python 3.8+、PyYAML；aria2p/aria2c 或 wget 可选。

用法示例：
  python vscode_offline_downloader.py all
  python vscode_offline_downloader.py installers --dry-run
  python vscode_offline_downloader.py installers --targets win32-x64-user,linux-deb-x64
  python vscode_offline_downloader.py extensions --config download_config.yaml
"""

from __future__ import annotations

import argparse
import atexit
import gzip
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:  # pragma: no cover - 在缺少依赖时给出友好提示
    yaml = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 vscode-offline-downloader/1.0"
)

UPDATE_API_URL = "https://update.code.visualstudio.com/api/update/{platform}/stable/latest"
UPDATE_DIRECT_URL = "https://update.code.visualstudio.com/{version}/{platform}/stable"
LATEST_VERSION_PLATFORM = "win32-x64-user"
DEFAULT_CONFIG_NAME = "download_config.yaml"
ARIA2_CONNECTIONS = 8
ARIA2_START_TIMEOUT = 10.0
PROGRESS_INTERVAL = 0.5

MARKETPLACE_QUERY_URL = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
)
MARKETPLACE_VSIX_URL = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
    "{publisher}/vsextensions/{extension}/{version}/vspackage"
)

# 远程开发目录
REMOTE_DIR = Path("远程开发") / "server+cli"

# 远程 server：两种 Linux 架构
REMOTE_SERVERS: List[Tuple[str, str]] = [
    ("server-linux-x64", "Linux x64 Server"),
    ("server-linux-arm64", "Linux arm64 Server"),
]

# CLI 默认使用 Alpine/musl 版本。Alpine 版为静态链接，
# 在 glibc 和 musl 发行版上均可运行。
REMOTE_CLIS: Dict[str, List[Tuple[str, str]]] = {
    "alpine": [
        ("cli-alpine-x64", "Linux x64 CLI（Alpine/musl）"),
        ("cli-alpine-arm64", "Linux arm64 CLI（Alpine/musl）"),
    ],
    "linux": [
        ("cli-linux-x64", "Linux x64 CLI（glibc）"),
        ("cli-linux-arm64", "Linux arm64 CLI（glibc）"),
    ],
}

TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VSCODE_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$"
)


# ---------------------------------------------------------------------------
# 数据结构与错误
# ---------------------------------------------------------------------------


class DownloaderError(Exception):
    """下载器可预期的错误。"""


@dataclass
class DownloadInfo:
    url: str
    filename: str
    version: Optional[str] = None
    sha256: Optional[str] = None


@dataclass
class ExtensionSpec:
    name: str
    path: str
    version: Optional[str] = None
    target: Optional[str] = None


@dataclass(frozen=True)
class PackageTarget:
    platform: str
    default: bool = False


# VS Code 稳定版可下载的安装包。可通过交互式复选框或 --targets 选择。
PACKAGE_TARGETS: List[PackageTarget] = [
    PackageTarget("win32-x64-user", True),
    PackageTarget("win32-arm64-user"),
    PackageTarget("win32-x64"),
    PackageTarget("win32-arm64"),
    PackageTarget("linux-deb-x64", True),
    PackageTarget("linux-deb-arm64", True),
    PackageTarget("linux-deb-armhf"),
    PackageTarget("linux-rpm-x64"),
    PackageTarget("linux-rpm-arm64"),
    PackageTarget("linux-rpm-armhf"),
    PackageTarget("linux-x64"),
    PackageTarget("linux-arm64"),
    PackageTarget("linux-armhf"),
    PackageTarget("darwin-arm64"),
    PackageTarget("darwin-universal"),
]

PACKAGE_TARGET_BY_ID: Dict[str, PackageTarget] = {
    target.platform: target for target in PACKAGE_TARGETS
}

# download_config.yaml 中 vscode 项可用的 platform -> package 组合
VSCODE_PLATFORM_PACKAGES: Dict[str, Tuple[str, ...]] = {
    "win32-x64": ("user", "system"),
    "win32-arm64": ("user", "system"),
    "linux-x64": ("deb", "rpm", "tgz"),
    "linux-arm64": ("deb", "rpm", "tgz"),
    "linux-armhf": ("deb", "rpm", "tgz"),
    "darwin-arm64": ("zip",),
    "darwin-universal": ("zip",),
}

# VS Code 安装包平台 -> 平台专用插件 target
EXTENSION_TARGETS_BY_VSCODE_PLATFORM: Dict[str, Tuple[str, ...]] = {
    "win32-x64-user": ("win32-x64",),
    "win32-x64": ("win32-x64",),
    "win32-arm64-user": ("win32-arm64",),
    "win32-arm64": ("win32-arm64",),
    "linux-deb-x64": ("linux-x64",),
    "linux-rpm-x64": ("linux-x64",),
    "linux-x64": ("linux-x64",),
    "linux-deb-arm64": ("linux-arm64",),
    "linux-rpm-arm64": ("linux-arm64",),
    "linux-arm64": ("linux-arm64",),
    "linux-deb-armhf": ("linux-armhf",),
    "linux-rpm-armhf": ("linux-armhf",),
    "linux-armhf": ("linux-armhf",),
    "darwin-arm64": ("darwin-arm64",),
    "darwin-universal": ("darwin-arm64", "darwin-x64"),
}


@dataclass
class DownloadConfig:
    vscode_targets: Optional[List[PackageTarget]]
    extensions: List[ExtensionSpec]
    vscode_version: Optional[str] = None


@dataclass
class RunStats:
    downloaded: int = 0
    skipped: int = 0
    planned: int = 0
    failures: List[str] = field(default_factory=list)

    def record(self, result: str, label: str) -> None:
        if result == "downloaded":
            self.downloaded += 1
        elif result == "skipped":
            self.skipped += 1
        elif result == "dry-run":
            self.planned += 1

    def record_failure(self, label: str, error: Exception) -> None:
        self.failures.append(label)
        print(f"  [!] {label}: {error}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def require_yaml() -> None:
    if yaml is None:
        raise DownloaderError(
            "缺少 PyYAML 依赖，请先运行：python -m pip install pyyaml"
        )


def detail(message: str) -> None:
    print(f"  {message}")


def warn(message: str) -> None:
    print(f"  [!] {message}", file=sys.stderr)


def format_size(num: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_size_compact(num: float) -> str:
    units = ["B", "K", "M", "G", "T"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}T"


def format_size_tiny(num: float) -> str:
    units = ["B", "K", "M", "G", "T"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.0f}T"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def display_width(text: str) -> int:
    """计算终端显示宽度，中文等宽字符按 2 列计算。"""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text

    suffix = "..."
    available = max(0, max_width - display_width(suffix))
    result: List[str] = []
    width = 0
    for char in text:
        char_width = (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if width + char_width > available:
            break
        result.append(char)
        width += char_width
    return "".join(result) + suffix


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_path_text(value: str) -> str:
    """允许在 Linux 上使用 Windows 风格的反斜杠路径。"""
    return value.replace("\\", os.sep)


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(normalize_path_text(str(value))).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def resolve_output_dir(base_dir: Path, value: str) -> Path:
    return resolve_path(base_dir, value)


def make_request(
    url: str,
    *,
    method: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
) -> Request:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    return Request(url, data=data, headers=request_headers, method=method)


def http_error_message(error: HTTPError, url: str) -> str:
    try:
        body = error.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    body = " ".join(body.split())[:400]
    message = f"HTTP {error.code} {error.reason}: {url}"
    if body:
        message += f"\n    {body}"
    return message


def get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Any] = None,
    timeout: float = 60.0,
    retries: int = 5,
) -> Any:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    last_error: Optional[DownloaderError] = None
    for attempt in range(1, retries + 1):
        request = make_request(url, headers=request_headers, data=data)
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read()
                return json.loads(raw.decode(charset, "replace"))
        except HTTPError as error:
            last_error = DownloaderError(http_error_message(error, url))
            if error.code in (400, 401, 403, 404):
                break
        except URLError as error:
            last_error = DownloaderError(
                f"网络错误: {error.reason} ({url})"
            )
        except json.JSONDecodeError as error:
            last_error = DownloaderError(
                f"服务器返回了无效的 JSON: {url}"
            )
        except (http.client.HTTPException, TimeoutError, OSError) as error:
            last_error = DownloaderError(f"网络错误: {error} ({url})")

        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 5))

    if last_error is None:
        last_error = DownloaderError(f"请求失败: {url}")
    raise last_error


def filename_from_headers(headers: Any, url: str) -> str:
    disposition = headers.get("Content-Disposition", "") if headers else ""
    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", disposition, re.IGNORECASE
    )
    if match:
        return unquote(match.group(1).strip().strip('"'))

    match = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip())

    name = os.path.basename(urlparse(url).path)
    return unquote(name) if name else "download"


# ---------------------------------------------------------------------------
# 下载核心
# ---------------------------------------------------------------------------


def _print_progress(
    done: int,
    total: Optional[int],
    elapsed: float,
    *,
    filename: Optional[str] = None,
    initial: int = 0,
    first: bool = False,
    final: bool = False,
    speed: Optional[float] = None,
) -> None:
    if speed is None:
        session_bytes = max(0, done - initial)
        speed = session_bytes / elapsed if elapsed > 0 else 0.0
    speed_text = f"{format_size(speed)}/s" if speed > 0 else "--"
    done_text = format_size(done)
    total_text = format_size(total) if total else "?"
    elapsed_text = format_duration(elapsed)

    if total and speed > 0:
        remaining = max(0, total - done)
        eta_text = format_duration(math.ceil(remaining / speed))
    else:
        eta_text = "--:--"

    width = shutil.get_terminal_size((100, 20)).columns

    # 进度条和百分比；窄屏时自动缩短。
    bar_width = max(5, min(30, width - 9))
    if total:
        percent = min(done / total * 100.0, 100.0)
        filled = int(bar_width * done / total)
        filled = max(0, min(bar_width, filled))
        bar_line = (
            "["
            + "=" * filled
            + "-" * (bar_width - filled)
            + "] "
            + f"{percent:5.1f}%"
        )
    else:
        bar_line = "[" + "-" * bar_width + "]   ?"

    # 速度、大小、耗时和 ETA，按终端宽度选择详细程度。
    full = (
        f"速度 {speed_text}   已下载 {done_text}/{total_text}   "
        f"已用 {elapsed_text}   ETA {eta_text}"
    )
    compact = (
        f"{speed_text}  {done_text}/{total_text}  "
        f"{elapsed_text}  ETA {eta_text}"
    )
    total_compact = format_size_compact(total) if total else "?"
    speed_compact = (
        f"{format_size_compact(speed)}/s" if speed > 0 else "--"
    )
    ultra = (
        f"{speed_compact}  "
        f"{format_size_compact(done)}/{total_compact}  "
        f"{elapsed_text}  ETA {eta_text}"
    )
    tiny_total = format_size_tiny(total) if total else "?"
    tiny_speed = (
        f"{format_size_tiny(speed)}/s" if speed > 0 else "--"
    )
    minimal = (
        f"{tiny_speed} {format_size_tiny(done)}/{tiny_total} "
        f"{elapsed_text}>{eta_text}"
    )
    stats = minimal
    for candidate in (full, compact, ultra, minimal):
        if display_width(candidate) <= width:
            stats = candidate
            break
    else:
        stats = truncate_display(minimal, width)

    if display_width(bar_line) > width:
        bar_line = truncate_display(bar_line, width)

    if not sys.stderr.isatty():
        if final:
            if filename:
                print(filename, file=sys.stderr)
            print(bar_line, file=sys.stderr)
            print(stats, file=sys.stderr)
        return

    if first:
        if filename:
            prefix = "正在下载: "
            available = max(1, width - display_width(prefix))
            name = truncate_display(filename, available)
            sys.stderr.write(prefix + name + "\n")
        sys.stderr.write(bar_line + "\n")
        sys.stderr.write(stats)
    else:
        # 回到进度条那一行，更新进度条和统计行
        sys.stderr.write("\r\x1b[1A\x1b[2K" + bar_line + "\n")
        sys.stderr.write("\x1b[2K" + stats)

    if final:
        sys.stderr.write("\n")
    sys.stderr.flush()


class SpeedMeter:
    """用滑动窗口计算实时速度，避免启动瞬间的缓存突发拉高显示值。"""

    def __init__(
        self,
        done: int = 0,
        *,
        warmup: float = 1.0,
        window: float = 1.0,
    ) -> None:
        now = time.monotonic()
        self.start_time = now
        self.warmup = warmup
        self.window = window
        self.samples: List[Tuple[float, int]] = [(now, done)]
        self.ready = False
        self.speed = 0.0

    def update(self, done: int, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        if not self.ready:
            if now - self.start_time < self.warmup:
                return 0.0
            self.ready = True
            self.samples = [(now, done)]
            return 0.0

        self.samples.append((now, done))
        cutoff = now - self.window
        while len(self.samples) > 2 and self.samples[0][0] < cutoff:
            self.samples.pop(0)

        first_time, first_done = self.samples[0]
        elapsed = now - first_time
        if elapsed > 0:
            self.speed = max(0, done - first_done) / elapsed
        return self.speed


def _remove_partial_files(part: Path) -> None:
    """删除临时文件和 aria2 控制文件。"""
    part.unlink(missing_ok=True)
    part.with_name(part.name + ".aria2").unlink(missing_ok=True)


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _aria2_available() -> bool:
    try:
        import aria2p  # noqa: F401
    except ImportError:
        return False
    return _command_exists("aria2c")


def resolve_download_method(requested: Optional[str]) -> str:
    """把 --download-method 转换为内部名称；未指定时自动选择。"""
    if requested == "none":
        return "native"
    if requested == "aria2":
        if not _aria2_available():
            raise DownloaderError(
                "aria2 下载需要 Python 3.11+、aria2p 和 aria2c：\n"
                "  python -m pip install aria2p\n"
                "  pkg install aria2        # Termux\n"
                "  sudo apt install aria2   # Debian/Ubuntu"
            )
        return "aria2"
    if requested == "wget":
        if not _command_exists("wget"):
            raise DownloaderError(
                "未找到 wget，请先安装，或使用 --download-method none"
            )
        return "wget"

    if _aria2_available():
        return "aria2"
    if _command_exists("wget"):
        return "wget"
    return "native"


def download_method_label(method: str) -> str:
    if method == "aria2":
        return f"aria2（{ARIA2_CONNECTIONS} 连接）"
    if method == "wget":
        return "wget"
    return "内置 urllib"


def _content_length(url: str, timeout: float) -> Optional[int]:
    try:
        request = make_request(url, method="HEAD")
        with urlopen(request, timeout=timeout) as response:
            value = response.headers.get("Content-Length")
            if value and value.isdigit():
                return int(value)
    except (HTTPError, URLError, http.client.HTTPException, OSError):
        pass
    return None


def _download_native(
    info: DownloadInfo,
    part: Path,
    *,
    display_name: str,
    timeout: float,
    allow_resume: bool,
) -> None:
    """使用 urllib 单连接下载。"""
    resume_from = 0
    if allow_resume and part.exists():
        resume_from = part.stat().st_size

    request_headers: Dict[str, str] = {}
    if resume_from > 0:
        request_headers["Range"] = f"bytes={resume_from}-"

    request = make_request(info.url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status == 416 and resume_from > 0:
            _remove_partial_files(part)
            raise DownloaderError("服务器拒绝断点续传，准备重新下载")

        content_encoding = (
            response.headers.get("Content-Encoding", "") or ""
        ).lower()
        gzipped = "gzip" in content_encoding

        if gzipped and resume_from > 0 and status == 206:
            # Range 作用于压缩后的字节流，无法直接拼接解压后的内容。
            _remove_partial_files(part)
            raise DownloaderError("服务器返回压缩内容，无法断点续传")

        if gzipped:
            stream: Any = gzip.GzipFile(fileobj=response)
            mode = "wb"
            downloaded = 0
            total: Optional[int] = None
        else:
            stream = response
            if resume_from > 0 and status == 206:
                mode = "ab"
                downloaded = resume_from
            else:
                mode = "wb"
                downloaded = 0
                resume_from = 0

            length_header = response.headers.get("Content-Length")
            if length_header and length_header.isdigit():
                total = int(length_header) + resume_from
            else:
                total = None

        start_time = time.monotonic()
        meter = SpeedMeter(resume_from)
        _print_progress(
            downloaded,
            total,
            0.0,
            filename=display_name,
            initial=resume_from,
            first=True,
        )
        last_progress = 0.0
        with part.open(mode) as fh:
            while True:
                chunk = stream.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL:
                    _print_progress(
                        downloaded,
                        total,
                        now - start_time,
                        filename=display_name,
                        initial=resume_from,
                        speed=meter.update(downloaded, now),
                    )
                    last_progress = now

        _print_progress(
            downloaded,
            total,
            time.monotonic() - start_time,
            filename=display_name,
            initial=resume_from,
            final=True,
        )

    final_size = part.stat().st_size
    if not gzipped and total is not None and final_size != total:
        raise DownloaderError(f"下载不完整: {final_size} / {total} 字节")


def _download_with_wget(
    info: DownloadInfo,
    part: Path,
    *,
    display_name: str,
    timeout: float,
) -> None:
    """使用 wget 下载，并复用脚本的进度显示。"""
    total = _content_length(info.url, timeout)
    initial = part.stat().st_size if part.exists() else 0
    if total is not None and initial > total:
        _remove_partial_files(part)
        initial = 0

    command = [
        "wget",
        "-q",
        "-c",
        "-T",
        str(max(1, int(timeout))),
        "-U",
        USER_AGENT,
        "-O",
        str(part),
        info.url,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    start_time = time.monotonic()
    meter = SpeedMeter(initial)
    _print_progress(
        initial,
        total,
        0.0,
        filename=display_name,
        initial=initial,
        first=True,
    )
    last_progress = 0.0
    stderr = ""
    try:
        while process.poll() is None:
            size = part.stat().st_size if part.exists() else 0
            now = time.monotonic()
            if now - last_progress >= PROGRESS_INTERVAL:
                _print_progress(
                    size,
                    total,
                    now - start_time,
                    filename=display_name,
                    initial=initial,
                    speed=meter.update(size, now),
                )
                last_progress = now
            time.sleep(0.1)
        _, stderr = process.communicate()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    if process.returncode != 0:
        message = (stderr or "").strip().splitlines()
        reason = message[-1] if message else f"退出码 {process.returncode}"
        raise DownloaderError(f"wget 下载失败: {reason}")

    final_size = part.stat().st_size if part.exists() else 0
    _print_progress(
        final_size,
        total,
        time.monotonic() - start_time,
        filename=display_name,
        initial=initial,
        final=True,
    )
    if total is not None and final_size != total:
        raise DownloaderError(f"下载不完整: {final_size} / {total} 字节")


class Aria2Backend:
    """管理本地 aria2c RPC 进程，并通过 aria2p 提交下载任务。"""

    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen] = None
        self.api: Any = None
        self.port: Optional[int] = None

    def _start(self, timeout: float) -> None:
        if self.api is not None:
            if self.process is not None and self.process.poll() is None:
                return
            self.close()

        try:
            import aria2p
        except ImportError as error:
            raise DownloaderError(
                "缺少 aria2p，请运行：python -m pip install aria2p"
            ) from error
        if not _command_exists("aria2c"):
            raise DownloaderError(
                "未找到 aria2c，请先安装：\n"
                "  pkg install aria2        # Termux\n"
                "  sudo apt install aria2   # Debian/Ubuntu"
            )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        secret = secrets.token_hex(16)

        command = [
            "aria2c",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-allow-origin-all=false",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={secret}",
            "--no-conf=true",
            "--quiet=true",
            "--console-log-level=warn",
            "--max-concurrent-downloads=1",
            "--file-allocation=none",
            "--summary-interval=0",
            "--download-result=hide",
            f"--stop-with-process={os.getpid()}",
        ]
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
        except OSError as error:
            raise DownloaderError(f"启动 aria2c 失败: {error}") from error

        self.api = aria2p.API(
            aria2p.Client(
                host="http://127.0.0.1",
                port=self.port,
                secret=secret,
                timeout=max(10.0, timeout),
            )
        )
        deadline = time.monotonic() + ARIA2_START_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                self.close()
                raise DownloaderError(f"aria2c 启动失败（退出码 {code}）")
            try:
                self.api.client.get_version()
                return
            except Exception:
                time.sleep(0.1)

        self.close()
        raise DownloaderError("aria2c RPC 启动超时")

    def download(
        self,
        info: DownloadInfo,
        part: Path,
        *,
        display_name: str,
        timeout: float,
    ) -> None:
        self._start(timeout)

        options = {
            "dir": str(part.parent),
            "out": part.name,
            "continue": "true",
            "split": str(ARIA2_CONNECTIONS),
            "max-connection-per-server": str(ARIA2_CONNECTIONS),
            "min-split-size": "1M",
            "file-allocation": "none",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
            "max-tries": "1",
            "retry-wait": "2",
            "timeout": str(max(1, int(timeout))),
            "connect-timeout": str(max(1, int(timeout))),
            "user-agent": USER_AGENT,
            "http-accept-gzip": "false",
            "summary-interval": "0",
            "download-result": "hide",
        }
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if proxy:
            options["all-proxy"] = proxy

        try:
            download = self.api.add_uris([info.url], options=options)
        except Exception as error:
            raise DownloaderError(f"aria2 添加下载失败: {error}") from error

        initial = download.completed_length or 0
        total = download.total_length or None
        start_time = time.monotonic()
        meter = SpeedMeter(initial)
        _print_progress(
            initial,
            total,
            0.0,
            filename=display_name,
            initial=initial,
            first=True,
        )
        last_progress = 0.0
        try:
            while True:
                download.update()
                total = download.total_length or total
                completed = download.completed_length or 0
                if download.is_complete:
                    break
                if download.has_failed or download.is_removed:
                    reason = download.error_message or download.status
                    raise DownloaderError(f"aria2 下载失败: {reason}")
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL:
                    _print_progress(
                        completed,
                        total,
                        now - start_time,
                        filename=display_name,
                        initial=initial,
                        speed=meter.update(completed, now),
                    )
                    last_progress = now
                time.sleep(PROGRESS_INTERVAL / 2)
        except BaseException:
            try:
                download.remove(force=True, files=False)
            except Exception:
                pass
            raise

        final_size = part.stat().st_size if part.exists() else 0
        _print_progress(
            final_size,
            total,
            time.monotonic() - start_time,
            filename=display_name,
            initial=initial,
            final=True,
        )
        try:
            download.purge()
        except Exception:
            pass
        if total is not None and final_size != total:
            raise DownloaderError(f"下载不完整: {final_size} / {total} 字节")

    def close(self) -> None:
        process = self.process
        self.process = None
        self.api = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass


_aria2_backend: Optional[Aria2Backend] = None


def _get_aria2_backend() -> Aria2Backend:
    global _aria2_backend
    if _aria2_backend is None:
        _aria2_backend = Aria2Backend()
        atexit.register(_aria2_backend.close)
    return _aria2_backend


def download_file(
    info: DownloadInfo,
    dest: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    retries: int = 3,
    timeout: float = 60.0,
    allow_resume: bool = True,
    verify: bool = True,
    download_method: str = "native",
) -> str:
    """
    下载文件，返回 downloaded / skipped / dry-run。

    - 下载到 .part 文件，完成后再原子重命名；
    - allow_resume=True 时支持 HTTP Range 断点续传；
    - verify=True 且 info.sha256 存在时校验 SHA256；
    - download_method 为 aria2 / wget / native。VSIX 固定使用 native，
      以正确处理 Marketplace 的 gzip 响应。
    """
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    method = download_method if allow_resume else "native"

    if dest.exists() and dest.stat().st_size > 0 and not force:
        if not dry_run:
            _remove_partial_files(part)
        detail(f"已存在，跳过: {dest}")
        return "skipped"

    if dry_run:
        detail(f"[dry-run] {download_method_label(method)} -> {dest}")
        detail(f"          {info.url}")
        return "dry-run"

    dest.parent.mkdir(parents=True, exist_ok=True)
    if force:
        dest.unlink(missing_ok=True)
        _remove_partial_files(part)
    elif not allow_resume:
        _remove_partial_files(part)
    elif method != "aria2":
        # aria2 会生成稀疏文件，切回 wget/原生时不能按文件大小续传。
        control = part.with_name(part.name + ".aria2")
        if control.exists():
            _remove_partial_files(part)

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if method == "aria2":
                _get_aria2_backend().download(
                    info,
                    part,
                    display_name=dest.name,
                    timeout=timeout,
                )
            elif method == "wget":
                _download_with_wget(
                    info,
                    part,
                    display_name=dest.name,
                    timeout=timeout,
                )
            else:
                _download_native(
                    info,
                    part,
                    display_name=dest.name,
                    timeout=timeout,
                    allow_resume=allow_resume,
                )

            if not part.exists():
                raise DownloaderError("下载文件不存在")
            final_size = part.stat().st_size
            if final_size <= 0:
                raise DownloaderError("下载文件为空")
            if verify and info.sha256:
                actual = sha256_file(part)
                if actual.lower() != info.sha256.lower():
                    _remove_partial_files(part)
                    raise DownloaderError(
                        f"SHA256 校验失败: 期望 {info.sha256}, 实际 {actual}"
                    )

            os.replace(part, dest)
            part.with_name(part.name + ".aria2").unlink(missing_ok=True)
            detail(f"完成: {dest} ({format_size(final_size)})")
            return "downloaded"

        except DownloaderError as error:
            last_error = error
            if not allow_resume:
                _remove_partial_files(part)
            if attempt >= retries:
                break
            wait = min(2 ** (attempt - 1), 8)
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            warn(f"下载失败: {error}")
            warn(f"{wait} 秒后重试（{attempt}/{retries}）")
            time.sleep(wait)
        except HTTPError as error:
            last_error = DownloaderError(http_error_message(error, info.url))
            if not allow_resume:
                _remove_partial_files(part)
            # 这类错误重试也没有意义
            if error.code in (400, 401, 403, 404):
                break
            if attempt >= retries:
                break
            wait = min(2 ** (attempt - 1), 8)
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            warn(f"下载失败: {last_error}")
            warn(f"{wait} 秒后重试（{attempt}/{retries}）")
            time.sleep(wait)
        except (
            URLError,
            http.client.HTTPException,
            TimeoutError,
            ConnectionError,
            OSError,
            EOFError,
        ) as error:
            last_error = DownloaderError(f"网络错误: {error}")
            if not allow_resume:
                _remove_partial_files(part)
            if attempt >= retries:
                break
            wait = min(2 ** (attempt - 1), 8)
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            warn(f"下载失败: {last_error}")
            warn(f"{wait} 秒后重试（{attempt}/{retries}）")
            time.sleep(wait)

    if last_error is None:
        last_error = DownloaderError("未知下载错误")
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    raise last_error


# ---------------------------------------------------------------------------
# 安装包选择（交互式复选框 / --targets）
# ---------------------------------------------------------------------------


def _render_target_menu(
    targets: List[PackageTarget],
    selected: List[bool],
    cursor: int,
    lines_printed: int,
) -> int:
    size = shutil.get_terminal_size((80, 24))
    width = max(20, size.columns)
    height = max(8, size.lines)

    lines: List[str] = []
    if width >= 30:
        lines.append("选择 VS Code 安装包：")
        lines.append("↑↓ 移动  空格 勾选  回车 确认")
        lines.append("a 全选  n 清空  q 取消")
    else:
        lines.append("选择安装包：")
        lines.append("↑↓ 空格 回车")
        lines.append("a 全选 n 清空 q 取消")

    # 预留标题、操作提示和上下滚动提示
    max_items = max(1, height - len(lines) - 2)
    total = len(targets)
    if total <= max_items:
        start = 0
        end = total
    else:
        start = max(0, min(cursor - max_items // 2, total - max_items))
        end = start + max_items

    if start > 0:
        lines.append(f"  ↑ 上方还有 {start} 项")

    for index in range(start, end):
        target = targets[index]
        pointer = ">" if index == cursor else " "
        mark = "x" if selected[index] else " "
        # 菜单里保留原始平台/包名，不做翻译，便于用户区分
        lines.append(f" {pointer} [{mark}] {target.platform}")

    if end < total:
        lines.append(f"  ↓ 下方还有 {total - end} 项")

    # 所有行都截断到终端宽度，避免自动换行破坏光标定位
    lines = [truncate_display(line, width) for line in lines]

    if lines_printed:
        # 先清掉上一次的菜单块，再写新的；行数变化时也不会错位。
        # 显式使用 \r\n，避免某些终端在 raw/cbreak 模式下把 \n 当成只下移不回车。
        sys.stdout.write(f"\x1b[{lines_printed}A")
        for _ in range(lines_printed):
            sys.stdout.write("\x1b[2K\r\n")
        sys.stdout.write(f"\x1b[{lines_printed}A")

    for line in lines:
        sys.stdout.write("\x1b[2K" + line + "\r\n")
    sys.stdout.flush()
    return len(lines)


def _select_targets_tui(
    targets: List[PackageTarget], selected: List[bool]
) -> Optional[List[PackageTarget]]:
    """Unix 下的方向键复选框。返回 None 表示当前终端不支持，改用文本模式。"""
    try:
        import termios
        import tty
    except ImportError:
        return None

    if not sys.stdin.isatty():
        return None

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        return None

    cursor = 0
    lines_printed = 0
    try:
        # raw 模式会关闭输出后处理，因此菜单里统一显式写 "\r\n"。
        tty.setraw(fd)
        while True:
            lines_printed = _render_target_menu(
                targets, selected, cursor, lines_printed
            )
            char = sys.stdin.read(1)
            if not char:
                raise KeyboardInterrupt

            if char == "\x1b":
                sequence = sys.stdin.read(2)
                if sequence in ("[A", "OA"):
                    cursor = (cursor - 1) % len(targets)
                elif sequence in ("[B", "OB"):
                    cursor = (cursor + 1) % len(targets)
            elif char in (" ", "\t"):
                selected[cursor] = not selected[cursor]
            elif char in ("\r", "\n"):
                break
            elif char in ("a", "A"):
                selected[:] = [True] * len(targets)
            elif char in ("n", "N"):
                selected[:] = [False] * len(targets)
            elif char in ("j", "J"):
                cursor = (cursor + 1) % len(targets)
            elif char in ("k", "K"):
                cursor = (cursor - 1) % len(targets)
            elif char in ("q", "Q", "\x03"):
                raise KeyboardInterrupt
    except termios.error:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except termios.error:
            pass
        sys.stdout.write("\r\n")
        sys.stdout.flush()

    return [
        target for target, checked in zip(targets, selected) if checked
    ]


def _select_targets_text(
    targets: List[PackageTarget], selected: List[bool]
) -> List[PackageTarget]:
    """不支持方向键时的文本复选框。"""
    width = shutil.get_terminal_size((80, 24)).columns
    while True:
        print(truncate_display("选择 VS Code 安装包：", width))
        for index, target in enumerate(targets, 1):
            mark = "x" if selected[index - 1] else " "
            line = f"  {index:2d}. [{mark}] {target.platform}"
            print(truncate_display(line, width))
        print(
            truncate_display(
                "输入编号切换；a 全选；n 清空；回车确认。",
                width,
            )
        )
        try:
            line = input("> ").strip()
        except EOFError:
            print()
            break

        if not line:
            break
        if line.lower() in ("a", "all"):
            selected[:] = [True] * len(targets)
            continue
        if line.lower() in ("n", "none"):
            selected[:] = [False] * len(targets)
            continue

        for token in re.split(r"[\s,]+", line):
            if not token:
                continue
            try:
                index = int(token) - 1
            except ValueError:
                print(f"无效编号: {token}")
                continue
            if 0 <= index < len(targets):
                selected[index] = not selected[index]
            else:
                print(f"编号超出范围: {token}")

    return [
        target for target, checked in zip(targets, selected) if checked
    ]


def parse_target_selection(raw: str) -> List[PackageTarget]:
    value = raw.strip().lower()
    if value in ("all", "*"):
        return list(PACKAGE_TARGETS)
    if value in ("none", "no", "0"):
        return []

    tokens = [token for token in re.split(r"[\s,]+", value) if token]
    by_platform = {
        target.platform.lower(): target for target in PACKAGE_TARGETS
    }
    selected: List[PackageTarget] = []
    unknown: List[str] = []
    for token in tokens:
        target = by_platform.get(token)
        if target is None:
            unknown.append(token)
        elif target not in selected:
            selected.append(target)

    if unknown:
        raise DownloaderError(
            "未知的安装包平台: "
            + ", ".join(unknown)
            + "\n可用值: "
            + ", ".join(target.platform for target in PACKAGE_TARGETS)
            + "\n也可以使用 all 或 none"
        )
    return selected


def default_package_targets() -> List[PackageTarget]:
    return [target for target in PACKAGE_TARGETS if target.default]


def choose_installer_targets(
    raw_targets: Optional[str],
    default_targets: Optional[List[PackageTarget]] = None,
) -> List[PackageTarget]:
    if raw_targets:
        selected = parse_target_selection(raw_targets)
    else:
        if default_targets is None:
            default_targets = default_package_targets()
        default_set = set(default_targets)
        defaults = [target in default_set for target in PACKAGE_TARGETS]

        if not sys.stdin.isatty():
            warn(
                "当前不是交互式终端，使用默认安装包选择；"
                "可用 --targets 指定要下载的平台。"
            )
            selected = list(default_targets)
        else:
            result = _select_targets_tui(PACKAGE_TARGETS, defaults)
            if result is None:
                result = _select_targets_text(PACKAGE_TARGETS, defaults)
            selected = result

    return selected


def select_installer_targets(
    raw_targets: Optional[str],
    config_targets: Optional[List[PackageTarget]],
    config_loaded: bool,
) -> List[PackageTarget]:
    """
    选择安装包平台。

    --targets 优先；否则使用配置中的 vscode.targets；
    没有配置文件时进入交互式选择。
    """
    if raw_targets is not None:
        selected = parse_target_selection(raw_targets)
    elif config_loaded:
        if config_targets is None:
            selected = default_package_targets()
        else:
            selected = list(config_targets)
    else:
        return choose_installer_targets(None, None)

    return selected


# ---------------------------------------------------------------------------
# VS Code 安装包 / 远程 server + CLI
# ---------------------------------------------------------------------------


_latest_vscode_version: Optional[str] = None


def resolve_filename_via_head(url: str, timeout: float) -> str:
    """用 HEAD 解析最终文件名；服务器不支持 HEAD 时退回 Range 请求。"""
    try:
        request = make_request(url, method="HEAD")
        with urlopen(request, timeout=timeout) as response:
            return filename_from_headers(response.headers, response.geturl())
    except HTTPError as error:
        if error.code not in (403, 405, 501):
            raise DownloaderError(http_error_message(error, url)) from error
    except URLError as error:
        raise DownloaderError(f"网络错误: {error.reason} ({url})") from error

    try:
        request = make_request(url, headers={"Range": "bytes=0-0"})
        with urlopen(request, timeout=timeout) as response:
            return filename_from_headers(response.headers, response.geturl())
    except HTTPError as error:
        raise DownloaderError(http_error_message(error, url)) from error
    except URLError as error:
        raise DownloaderError(f"网络错误: {error.reason} ({url})") from error


def resolve_update_download(
    platform: str, timeout: float, version: Optional[str] = None
) -> DownloadInfo:
    """获取指定版本（或最新稳定版）的下载地址和文件名。"""
    global _latest_vscode_version

    requested = (version or "").strip()
    if requested.lower() in ("", "latest"):
        api_url = UPDATE_API_URL.format(platform=platform)
        data = get_json(
            api_url, headers={"Accept": "application/json"}, timeout=timeout
        )
        url = data.get("url")
        if not url:
            raise DownloaderError(f"更新接口没有返回下载地址: {api_url}")
        url = str(url)
        filename = filename_from_headers({}, url)
        resolved_version = str(
            data.get("productVersion") or data.get("name") or ""
        )
        if platform == LATEST_VERSION_PLATFORM and resolved_version:
            # 让语言包解析复用同一个最新稳定版版本号
            _latest_vscode_version = resolved_version
        return DownloadInfo(
            url=url,
            filename=filename,
            version=resolved_version,
            sha256=data.get("sha256hash"),
        )

    if not VSCODE_VERSION_RE.match(requested):
        raise DownloaderError(
            f"VS Code 版本格式不正确: {requested!r}（示例: 1.128.1）"
        )

    url = UPDATE_DIRECT_URL.format(
        version=quote(requested, safe=""), platform=platform
    )
    filename = resolve_filename_via_head(url, timeout)
    return DownloadInfo(
        url=url,
        filename=filename,
        version=requested,
        sha256=None,
    )


def get_latest_vscode_version(timeout: float) -> str:
    """获取最新稳定版 VS Code 的版本号。"""
    global _latest_vscode_version
    if _latest_vscode_version:
        return _latest_vscode_version

    data = get_json(
        UPDATE_API_URL.format(platform=LATEST_VERSION_PLATFORM),
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    version = str(data.get("productVersion") or data.get("name") or "").strip()
    if not version:
        raise DownloaderError("无法获取最新稳定版 VS Code 版本")
    _latest_vscode_version = version
    return version


def download_installers(
    base_dir: Path,
    targets: List[PackageTarget],
    *,
    force: bool,
    dry_run: bool,
    retries: int,
    timeout: float,
    download_method: str,
    vscode_version: Optional[str] = None,
    stats: RunStats,
) -> None:
    print("\n[1/3] VS Code 安装包")
    if not targets:
        detail("未选择安装包，跳过。")
        return
    detail(f"下载方式: {download_method_label(download_method)}")

    resolved: List[Tuple[PackageTarget, DownloadInfo]] = []
    for target in targets:
        try:
            info = resolve_update_download(
                target.platform, timeout, vscode_version
            )
            resolved.append((target, info))
        except DownloaderError as error:
            stats.record_failure(target.platform, error)

    versions = {info.version for _, info in resolved if info.version}
    if len(versions) > 1:
        warn("不同安装包的版本不一致: " + ", ".join(sorted(versions)))

    for target, info in resolved:
        print(f"\n  {target.platform}")
        detail(f"版本: {info.version or 'latest'}")
        dest = base_dir / info.filename
        try:
            result = download_file(
                info,
                dest,
                force=force,
                dry_run=dry_run,
                retries=retries,
                timeout=timeout,
                allow_resume=True,
                verify=True,
                download_method=download_method,
            )
            stats.record(result, target.platform)
        except DownloaderError as error:
            stats.record_failure(target.platform, error)


def download_remote(
    base_dir: Path,
    cli_flavor: str,
    *,
    force: bool,
    dry_run: bool,
    retries: int,
    timeout: float,
    download_method: str,
    vscode_version: Optional[str] = None,
    stats: RunStats,
) -> None:
    print("\n[2/3] 远程开发 Server + CLI")
    platforms = list(REMOTE_SERVERS) + list(REMOTE_CLIS[cli_flavor])
    resolved: List[Tuple[str, DownloadInfo]] = []
    detail(f"下载方式: {download_method_label(download_method)}")

    for platform, label in platforms:
        try:
            info = resolve_update_download(
                platform, timeout, vscode_version
            )
            resolved.append((label, info))
        except DownloaderError as error:
            stats.record_failure(label, error)

    versions = {info.version for _, info in resolved if info.version}
    if len(versions) > 1:
        warn("Server/CLI 版本不一致: " + ", ".join(sorted(versions)))

    for label, info in resolved:
        print(f"\n  {label}")
        detail(f"版本: {info.version or 'latest'}")
        dest = base_dir / REMOTE_DIR / info.filename
        try:
            result = download_file(
                info,
                dest,
                force=force,
                dry_run=dry_run,
                retries=retries,
                timeout=timeout,
                allow_resume=True,
                verify=True,
                download_method=download_method,
            )
            stats.record(result, label)
        except DownloaderError as error:
            stats.record_failure(label, error)


# ---------------------------------------------------------------------------
# Marketplace 插件
# ---------------------------------------------------------------------------


_gallery_cache: Dict[Tuple[str, bool], List[Dict[str, Any]]] = {}
_platform_specific_cache: Dict[str, bool] = {}


def parse_extension_id(name: str) -> Tuple[str, str]:
    value = (name or "").strip()
    if "/" in value or "\\" in value or ".." in value:
        raise DownloaderError(f"插件名称包含非法字符: {value!r}")
    if value.count(".") != 1:
        raise DownloaderError(
            f"插件名称必须为 publisher.extension 格式: {value!r}"
        )
    publisher, extension = value.split(".", 1)
    if not publisher or not extension:
        raise DownloaderError(
            f"插件名称必须为 publisher.extension 格式: {value!r}"
        )
    return publisher, extension


def version_key(version: str) -> Tuple[Tuple[int, ...], int, str]:
    """用于比较 Marketplace 版本号，支持 1.2.3、2026.5.2026070801、1.2.3-beta。"""
    main, separator, pre = version.partition("-")
    parts: List[int] = []
    for token in main.split("."):
        match = re.match(r"^(\d+)", token)
        parts.append(int(match.group(1)) if match else 0)
    while len(parts) < 4:
        parts.append(0)
    # 正式版优先于预发布版
    return (tuple(parts), 0 if separator else 1, pre)


def get_extension_versions(
    name: str, timeout: float, *, all_versions: bool = False
) -> List[Dict[str, Any]]:
    cache_key = (name.lower(), all_versions)
    if cache_key in _gallery_cache:
        return _gallery_cache[cache_key]

    payload = {
        "filters": [
            {
                "criteria": [{"filterType": 7, "value": name}],
                "pageNumber": 1,
                "pageSize": 1,
            }
        ],
        # flags=1 返回全部历史版本，用于按 VS Code 系列选择语言包；
        # flags=2819 只返回各目标平台的最新版本，响应更小。
        "flags": 1 if all_versions else 2819,
    }
    data = get_json(
        MARKETPLACE_QUERY_URL,
        headers={"Accept": "application/json;api-version=7.2-preview.1"},
        payload=payload,
        timeout=timeout,
    )

    results = data.get("results") or []
    extensions = results[0].get("extensions") if results else []
    if not extensions:
        raise DownloaderError(f"Marketplace 中找不到插件: {name}")

    versions = extensions[0].get("versions") or []
    if not versions:
        raise DownloaderError(f"插件没有可用版本: {name}")

    _gallery_cache[cache_key] = versions
    return versions


def is_language_pack(name: str) -> bool:
    try:
        publisher, extension = parse_extension_id(name)
    except DownloaderError:
        return False
    return (
        publisher.lower() == "ms-ceintl"
        and extension.lower().startswith("vscode-language-pack-")
    )


def resolve_language_pack_version(
    spec: ExtensionSpec,
    timeout: float,
    vscode_version: Optional[str] = None,
) -> str:
    """
    语言包不接受 version 参数，始终按 VS Code 版本选择。

    - 优先使用与 VS Code 主次版本完全一致的系列；
    - 如果该系列不存在，使用小于目标版本的最近一个系列；
    - 在选中的系列里取最新构建。
    """
    versions = get_extension_versions(spec.name, timeout, all_versions=True)
    available_versions = [
        str(item.get("version"))
        for item in versions
        if item.get("version")
    ]
    if not available_versions:
        raise DownloaderError(f"语言包没有可用版本: {spec.name}")

    if vscode_version is None:
        vscode_version = get_latest_vscode_version(timeout)
    if spec.version:
        warn(
            f"语言包 {spec.name} 不接受 version 参数，已忽略；"
            f"将按 VS Code {vscode_version} 自动选择"
        )

    match = re.match(r"^(\d+)\.(\d+)", vscode_version)
    if not match:
        raise DownloaderError(f"无法解析 VS Code 版本: {vscode_version}")
    vscode_series = f"{match.group(1)}.{match.group(2)}"
    target_series = tuple(int(part) for part in vscode_series.split("."))

    available_series = sorted(
        {
            ".".join(version.split(".")[:2])
            for version in available_versions
            if len(version.split(".")) >= 2
        },
        key=lambda item: tuple(int(part) for part in item.split(".")),
    )

    if vscode_series in available_series:
        chosen_series = vscode_series
    else:
        previous = [
            series
            for series in available_series
            if tuple(int(part) for part in series.split(".")) < target_series
        ]
        if not previous:
            available_text = ", ".join(available_series[-12:])
            raise DownloaderError(
                f"没有找到不晚于 VS Code {vscode_version} 的语言包版本。"
                f"可用系列: {available_text}"
            )
        chosen_series = previous[-1]
        warn(
            f"没有找到与 VS Code {vscode_version} 完全匹配的语言包系列 "
            f"{vscode_series}，使用最近的 {chosen_series} 系列"
        )

    candidates = [
        version
        for version in available_versions
        if version.startswith(chosen_series + ".")
    ]
    if not candidates:
        raise DownloaderError(
            f"语言包系列 {chosen_series} 没有可用版本: {spec.name}"
        )
    return max(candidates, key=version_key)


def resolve_extension_version(
    spec: ExtensionSpec,
    timeout: float,
    vscode_version: Optional[str] = None,
) -> str:
    if is_language_pack(spec.name):
        return resolve_language_pack_version(
            spec, timeout, vscode_version
        )

    if spec.version:
        return spec.version

    versions = get_extension_versions(spec.name, timeout)

    if spec.target:
        candidates = [
            item
            for item in versions
            if (item.get("targetPlatform") or "").lower()
            == spec.target.lower()
        ]
    else:
        candidates = [
            item
            for item in versions
            if not item.get("targetPlatform")
            or (item.get("targetPlatform") or "").lower() == "universal"
        ]

    if not candidates:
        available = sorted(
            {
                str(item.get("targetPlatform"))
                for item in versions
                if item.get("targetPlatform")
            }
        )
        available_text = ", ".join(available) if available else "无（仅通用版本）"
        if spec.target:
            raise DownloaderError(
                f"{spec.name} 没有 {spec.target} 平台的版本；"
                f"可用平台: {available_text}"
            )
        raise DownloaderError(
            f"{spec.name} 没有通用版本，请在配置中为它添加 target；"
            f"可用平台: {available_text}"
        )

    best = max(
        candidates,
        key=lambda item: version_key(str(item.get("version", ""))),
    )
    return str(best.get("version"))


def marketplace_url(name: str, version: str, target: Optional[str]) -> str:
    publisher, extension = parse_extension_id(name)
    url = MARKETPLACE_VSIX_URL.format(
        publisher=quote(publisher, safe=""),
        extension=quote(extension, safe=""),
        version=quote(version, safe=""),
    )
    if target:
        url += "?" + urlencode({"targetPlatform": target})
    return url


def extension_filename(name: str, version: str, target: Optional[str]) -> str:
    suffix = f"@{target}" if target else ""
    return f"{name}-{version}{suffix}.vsix"


def default_package_for_platform(platform: str) -> str:
    if platform.startswith("win32-"):
        return "user"
    if platform.startswith("linux-"):
        return "deb"
    if platform.startswith("darwin-"):
        return "zip"
    raise DownloaderError(f"未知的 vscode platform: {platform!r}")


def vscode_platform_id(platform: str, package: str) -> str:
    if platform not in VSCODE_PLATFORM_PACKAGES:
        raise DownloaderError(
            f"未知的 vscode platform: {platform!r}；可用值: "
            + ", ".join(VSCODE_PLATFORM_PACKAGES)
        )
    allowed = VSCODE_PLATFORM_PACKAGES[platform]
    if package not in allowed:
        raise DownloaderError(
            f"platform {platform} 不支持 package {package!r}；可用值: "
            + ", ".join(allowed)
        )

    if platform.startswith("win32-"):
        return f"{platform}-user" if package == "user" else platform
    if platform.startswith("linux-"):
        arch = platform.split("-", 1)[1]
        if package == "deb":
            return f"linux-deb-{arch}"
        if package == "rpm":
            return f"linux-rpm-{arch}"
        return f"linux-{arch}"  # tgz
    return platform  # macOS zip


def parse_vscode_targets(
    raw_targets: Any,
) -> Optional[List[PackageTarget]]:
    if raw_targets is None:
        return None
    if not isinstance(raw_targets, list):
        raise DownloaderError("vscode.targets 必须是数组")

    selected: List[PackageTarget] = []
    for index, item in enumerate(raw_targets, 1):
        if isinstance(item, str):
            # 兼容直接写内部平台 ID，如 win32-x64-user
            platform_id = item.strip().lower()
            target = PACKAGE_TARGET_BY_ID.get(platform_id)
            if target is None:
                raise DownloaderError(
                    f"vscode 第 {index} 项未知平台: {item!r}；可用值: "
                    + ", ".join(PACKAGE_TARGET_BY_ID)
                )
        elif isinstance(item, dict):
            platform = str(item.get("platform") or "").strip().lower()
            if not platform:
                raise DownloaderError(
                    f"vscode 第 {index} 项缺少 platform"
                )
            package = str(item.get("package") or "").strip().lower()
            if not package:
                package = default_package_for_platform(platform)
            platform_id = vscode_platform_id(platform, package)
            target = PACKAGE_TARGET_BY_ID.get(platform_id)
            if target is None:
                raise DownloaderError(
                    f"vscode 第 {index} 项组合无效: "
                    f"platform={platform!r}, package={package!r}"
                )
        else:
            raise DownloaderError(
                f"vscode 第 {index} 项必须是字符串或包含 "
                "platform/package 的对象"
            )

        if target not in selected:
            selected.append(target)
    return selected


def parse_vscode_config(
    raw_vscode: Any,
) -> Tuple[Optional[str], Optional[List[PackageTarget]]]:
    """解析 vscode 段（version/targets）；也支持直接给出平台列表。"""
    if raw_vscode is None:
        return None, None
    if isinstance(raw_vscode, list):
        return None, parse_vscode_targets(raw_vscode)
    if not isinstance(raw_vscode, dict):
        raise DownloaderError(
            "vscode 必须是包含 version/targets 的对象，或平台列表"
        )

    raw_version = raw_vscode.get("version")
    version_text = ""
    if raw_version is not None:
        version_text = str(raw_version).strip()
        if version_text.lower() in ("", "latest", "*"):
            version_text = ""
        elif not VSCODE_VERSION_RE.match(version_text):
            raise DownloaderError(
                f"vscode.version 格式不正确: {version_text!r}"
                "（示例: 1.128.1）"
            )

    raw_targets = raw_vscode.get("targets")
    return version_text or None, parse_vscode_targets(raw_targets)


def extension_targets_for_vscode_targets(
    targets: Optional[List[PackageTarget]],
) -> List[str]:
    """把 VS Code 安装包平台转换为平台专用插件的 target 列表。"""
    if not targets:
        return []
    result: List[str] = []
    for target in targets:
        for extension_target in EXTENSION_TARGETS_BY_VSCODE_PLATFORM.get(
            target.platform, ()
        ):
            if extension_target not in result:
                result.append(extension_target)
    return result


def is_platform_specific_extension(name: str, timeout: float) -> bool:
    """
    判断插件是否只提供平台专用版本。

    有些插件（例如 cpptools）保留了很旧的通用版本，但新版本只按平台发布。
    如果平台版本明显新于通用版本，就按平台专用插件处理。
    """
    cache_key = name.lower()
    if cache_key in _platform_specific_cache:
        return _platform_specific_cache[cache_key]

    versions = get_extension_versions(name, timeout)
    universal: List[str] = []
    targeted: List[str] = []
    for item in versions:
        version = str(item.get("version") or "")
        if not version:
            continue
        target = (item.get("targetPlatform") or "").lower()
        if not target or target == "universal":
            universal.append(version)
        else:
            targeted.append(version)

    if not targeted:
        result = False
    elif not universal:
        result = True
    else:
        result = version_key(max(targeted, key=version_key)) > version_key(
            max(universal, key=version_key)
        )

    _platform_specific_cache[cache_key] = result
    return result


def adapt_extension_specs(
    specs: List[ExtensionSpec],
    selected_targets: List[str],
    *,
    timeout: float = 60.0,
) -> List[ExtensionSpec]:
    """
    让平台专用插件的架构跟随 VS Code 的选择。

    - 通用插件保持不变；
    - 配置里带 target 的插件按 (name, path) 分组；
    - 每个选中的架构生成一个下载项；
    - 如果配置里有该架构的精确条目，沿用其版本，否则用该架构的最新版本；
    - 没有写 target，但 Marketplace 上只有平台专用版本的插件，也会自动按架构展开。
    """
    if not selected_targets:
        return list(specs)

    groups: Dict[Tuple[str, str], List[ExtensionSpec]] = {}
    order: List[Tuple[str, str]] = []
    for spec in specs:
        key = (spec.name, spec.path)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(spec)

    adapted: List[ExtensionSpec] = []
    for key in order:
        group = groups[key]
        base = group[0]
        if not any(spec.target for spec in group):
            auto_platform = False
            if base.version is None:
                try:
                    auto_platform = is_platform_specific_extension(
                        base.name, timeout
                    )
                except DownloaderError as error:
                    warn(
                        f"无法判断 {base.name} 是否需要按架构下载，"
                        f"按通用版本处理: {error}"
                    )
            if auto_platform:
                for target in selected_targets:
                    adapted.append(
                        ExtensionSpec(
                            name=base.name,
                            path=base.path,
                            version=None,
                            target=target,
                        )
                    )
                continue

            adapted.append(
                ExtensionSpec(
                    name=base.name,
                    path=base.path,
                    version=base.version,
                    target=None,
                )
            )
            continue

        version_by_target = {
            spec.target: spec.version
            for spec in group
            if spec.target
        }
        for target in selected_targets:
            adapted.append(
                ExtensionSpec(
                    name=key[0],
                    path=key[1],
                    version=version_by_target.get(target),
                    target=target,
                )
            )
    return adapted


def parse_extension_specs(raw_extensions: Any) -> List[ExtensionSpec]:
    if raw_extensions is None:
        return []
    if not isinstance(raw_extensions, list):
        raise DownloaderError("extensions 必须是数组")

    specs: List[ExtensionSpec] = []
    for index, item in enumerate(raw_extensions, 1):
        if not isinstance(item, dict):
            raise DownloaderError(f"extensions 第 {index} 项必须是对象")

        name = str(item.get("name") or "").strip()
        if not name:
            raise DownloaderError(f"extensions 第 {index} 项缺少 name")
        parse_extension_id(name)

        raw_path = item.get("path", ".")
        if raw_path is None:
            raw_path = "."
        path_text = str(raw_path).strip() or "."

        raw_version = item.get("version")
        if raw_version is None:
            version_text = ""
        elif isinstance(raw_version, str):
            version_text = raw_version.strip()
        else:
            warn(
                f"extensions 第 {index} 项 version 不是字符串，"
                f"已按 {raw_version!r} 处理；建议使用字符串"
            )
            version_text = str(raw_version).strip()

        if version_text.lower() in ("", "latest", "*"):
            version_text = ""

        if is_language_pack(name) and version_text:
            warn(
                f"语言包 {name} 不接受 version 参数，已忽略；"
                "语言包会按 VS Code 版本自动选择"
            )
            version_text = ""

        raw_target = item.get("target")
        target_text: Optional[str] = None
        if raw_target is not None:
            target_text = str(raw_target).strip() or None
        if target_text and not TARGET_RE.match(target_text):
            raise DownloaderError(
                f"extensions 第 {index} 项 target 格式不正确: {target_text!r}"
            )

        specs.append(
            ExtensionSpec(
                name=name,
                path=path_text,
                version=version_text or None,
                target=target_text,
            )
        )

    return specs


def load_download_config(path: Path) -> DownloadConfig:
    require_yaml()
    if not path.exists():
        raise DownloaderError(f"找不到配置文件: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DownloaderError(f"读取配置文件失败: {path}\n{error}") from error

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise DownloaderError(f"解析 YAML 失败: {path}\n{error}") from error

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise DownloaderError("配置文件顶层必须是 YAML 对象")

    vscode_version, vscode_targets = parse_vscode_config(
        data.get("vscode")
    )
    return DownloadConfig(
        vscode_targets=vscode_targets,
        extensions=parse_extension_specs(data.get("extensions")),
        vscode_version=vscode_version,
    )


def download_extensions(
    specs: List[ExtensionSpec],
    base_dir: Path,
    *,
    force: bool,
    dry_run: bool,
    retries: int,
    timeout: float,
    vscode_version: Optional[str] = None,
    stats: RunStats,
) -> None:
    print(f"\n[3/3] VS Code 插件（{len(specs)} 个）")
    if not specs:
        detail("配置中没有插件。")
        return

    for index, spec in enumerate(specs, 1):
        label = spec.name + (f"@{spec.target}" if spec.target else "")
        print(f"\n  [{index}/{len(specs)}] {label}")
        try:
            version = resolve_extension_version(
                spec, timeout, vscode_version
            )
            if spec.version and version != spec.version:
                detail(f"版本: {spec.version} -> {version}")
            elif spec.version:
                detail(f"版本: {version}")
            elif is_language_pack(spec.name):
                detail(f"匹配 VS Code 的版本: {version}")
            else:
                detail(f"最新版本: {version}")

            dest_dir = resolve_output_dir(base_dir, spec.path)
            filename = extension_filename(spec.name, version, spec.target)
            dest = dest_dir / filename
            url = marketplace_url(spec.name, version, spec.target)
            info = DownloadInfo(url=url, filename=filename, version=version)

            result = download_file(
                info,
                dest,
                force=force,
                dry_run=dry_run,
                retries=retries,
                timeout=timeout,
                allow_resume=False,
                verify=False,
            )
            stats.record(result, label)
        except DownloaderError as error:
            message = str(error)
            if "HTTP 404" in message:
                message += (
                    "\n    提示: 请检查插件版本/目标平台；平台专用插件需要在配置中"
                    "添加 target（如 linux-x64、linux-arm64、win32-x64），"
                    "或将 version 留空以自动选择该平台的最新版本。"
                )
            stats.record_failure(label, DownloaderError(message))


# ---------------------------------------------------------------------------
# 汇总与命令行入口
# ---------------------------------------------------------------------------


def print_summary(stats: RunStats, dry_run: bool) -> None:
    print()
    if dry_run:
        print(
            f"计划完成: {stats.planned} 项待下载，"
            f"{stats.skipped} 项已存在，{len(stats.failures)} 项失败。"
        )
    else:
        print(
            f"完成: 下载 {stats.downloaded} 项，"
            f"跳过 {stats.skipped} 项，失败 {len(stats.failures)} 项。"
        )
    if stats.failures:
        print("失败项:", file=sys.stderr)
        for item in stats.failures:
            print(f"  - {item}", file=sys.stderr)


def _extension_plan_line(spec: ExtensionSpec) -> str:
    label = spec.name + (f"@{spec.target}" if spec.target else "")
    if is_language_pack(spec.name):
        version = "auto"
    else:
        version = spec.version or "latest"
    return f"{label}  {version}"


def installer_plan_section(
    targets: List[PackageTarget], version: Optional[str]
) -> Tuple[str, List[str]]:
    items = [target.platform for target in targets]
    title = (
        f"VS Code 安装包（{len(items)} 个，"
        f"版本 {version or 'latest'}）"
    )
    return title, items


def remote_plan_section(
    cli_flavor: str, version: Optional[str]
) -> Tuple[str, List[str]]:
    platforms = list(REMOTE_SERVERS) + list(REMOTE_CLIS[cli_flavor])
    items = [platform for platform, _ in platforms]
    title = (
        f"远程开发 Server + CLI（{len(items)} 个，"
        f"版本 {version or 'latest'}）"
    )
    return title, items


def extension_plan_section(
    specs: List[ExtensionSpec],
) -> Tuple[str, List[str]]:
    items = [_extension_plan_line(spec) for spec in specs]
    return f"VS Code 插件（{len(items)} 个）", items


def confirm_download_plan(
    sections: List[Tuple[str, List[str]]],
    base_dir: Path,
    *,
    yes: bool,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True

    print("\n即将下载以下内容：")
    total = 0
    for title, items in sections:
        print(f"\n{title}")
        if not items:
            print("  （无）")
            continue
        total += len(items)
        for item in items:
            print(f"  - {item}")

    print(f"\n下载目录: {base_dir}")
    if total == 0:
        print("没有需要下载的内容。")
        sys.stdout.flush()
        return True

    if yes:
        print("已通过 --yes 确认，开始下载。")
        sys.stdout.flush()
        return True

    if not sys.stdin.isatty():
        sys.stdout.flush()
        raise DownloaderError(
            "当前不是交互式终端，无法确认下载；请加 --yes 跳过确认。"
        )

    try:
        answer = input("确认开始下载？[y/N]: ").strip().lower()
    except EOFError:
        print()
        print("已取消，未下载任何内容。")
        sys.stdout.flush()
        return False

    if answer in ("y", "yes"):
        sys.stdout.flush()
        return True
    print("已取消，未下载任何内容。")
    sys.stdout.flush()
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VS Code 离线安装包下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python vscode_offline_downloader.py all\n"
            "  python vscode_offline_downloader.py installers --dry-run\n"
            "  python vscode_offline_downloader.py installers "
            "--download-method aria2\n"
            "  python vscode_offline_downloader.py installers --targets "
            "win32-x64-user,linux-deb-x64,linux-deb-arm64\n"
            "  python vscode_offline_downloader.py extensions --config download_config.yaml\n"
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="all",
        choices=["all", "installers", "remote", "extensions"],
        help="要执行的操作（默认 all）",
    )
    parser.add_argument(
        "--targets",
        default=None,
        help=(
            "逗号分隔的安装包平台，如 "
            "win32-x64-user,linux-deb-x64,linux-deb-arm64；"
            "all 表示全部，none 表示不下载安装包。"
            "不指定时：有配置文件则直接使用 vscode.targets；"
            "没有配置文件才显示复选框。"
        ),
    )
    parser.add_argument(
        "--download-method",
        choices=["aria2", "wget", "none"],
        default=None,
        help=(
            "下载方式：aria2（多连接）、wget 或 none（内置 urllib）；"
            "不指定时自动选择 aria2 > wget > 内置"
        ),
    )
    parser.add_argument(
        "--cli-flavor",
        choices=["alpine", "linux"],
        default="alpine",
        help="远程 CLI 类型，默认 alpine（静态链接，兼容性较好）",
    )
    parser.add_argument(
        "--no-remote",
        action="store_true",
        help="all 模式下不下载远程 Server/CLI",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help=(
            "配置文件路径；不指定时在当前目录查找 "
            f"{DEFAULT_CONFIG_NAME}"
        ),
    )
    parser.add_argument(
        "-C",
        "--base-dir",
        default=".",
        help="下载根目录，默认当前目录",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过下载前的 y/N 确认",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的文件",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示计划下载的内容，不实际写入文件",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="下载失败重试次数，默认 3",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="网络超时时间（秒），默认 60",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.retries < 1:
        parser.error("--retries 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    base_dir = resolve_path(Path.cwd(), args.base_dir)

    # -c 指定时相对当前目录；未指定时在当前目录查找默认配置文件
    if args.config is not None:
        config_path = resolve_path(Path.cwd(), args.config)
        config_explicit = True
    else:
        config_path = Path.cwd() / DEFAULT_CONFIG_NAME
        config_explicit = False
    config_exists = config_path.exists()

    try:
        download_method = "native"
        if args.action != "extensions":
            download_method = resolve_download_method(args.download_method)

        if args.action == "extensions":
            if not config_exists:
                raise DownloaderError(
                    f"找不到配置文件: {config_path}\n"
                    f"请准备 {DEFAULT_CONFIG_NAME}，或用 --config 指定路径。"
                )
            config = load_download_config(config_path)
            plugin_targets = extension_targets_for_vscode_targets(
                config.vscode_targets
            )
            specs = adapt_extension_specs(
                config.extensions, plugin_targets, timeout=args.timeout
            )
            if not confirm_download_plan(
                [extension_plan_section(specs)],
                base_dir,
                yes=args.yes,
                dry_run=args.dry_run,
            ):
                return 0
            stats = RunStats()
            download_extensions(
                specs,
                base_dir,
                force=args.force,
                dry_run=args.dry_run,
                retries=args.retries,
                timeout=args.timeout,
                vscode_version=config.vscode_version,
                stats=stats,
            )
            print_summary(stats, args.dry_run)
            return 1 if stats.failures else 0

        if args.action == "installers":
            config_targets: Optional[List[PackageTarget]] = None
            vscode_version: Optional[str] = None
            config_loaded = False
            if config_exists:
                try:
                    config = load_download_config(config_path)
                    config_targets = config.vscode_targets
                    vscode_version = config.vscode_version
                    config_loaded = True
                except DownloaderError as error:
                    if config_explicit:
                        raise
                    warn(
                        "读取配置文件失败，使用默认安装包选择: "
                        f"{error}"
                    )
            elif config_explicit:
                raise DownloaderError(f"找不到配置文件: {config_path}")

            selected_targets = select_installer_targets(
                args.targets, config_targets, config_loaded
            )
            if not confirm_download_plan(
                [installer_plan_section(selected_targets, vscode_version)],
                base_dir,
                yes=args.yes,
                dry_run=args.dry_run,
            ):
                return 0
            stats = RunStats()
            download_installers(
                base_dir,
                selected_targets,
                force=args.force,
                dry_run=args.dry_run,
                retries=args.retries,
                timeout=args.timeout,
                download_method=download_method,
                vscode_version=vscode_version,
                stats=stats,
            )
            print_summary(stats, args.dry_run)
            return 1 if stats.failures else 0

        if args.action == "remote":
            vscode_version = None
            if config_exists:
                config = load_download_config(config_path)
                vscode_version = config.vscode_version
            elif config_explicit:
                raise DownloaderError(f"找不到配置文件: {config_path}")
            if not confirm_download_plan(
                [remote_plan_section(args.cli_flavor, vscode_version)],
                base_dir,
                yes=args.yes,
                dry_run=args.dry_run,
            ):
                return 0
            stats = RunStats()
            download_remote(
                base_dir,
                args.cli_flavor,
                force=args.force,
                dry_run=args.dry_run,
                retries=args.retries,
                timeout=args.timeout,
                download_method=download_method,
                vscode_version=vscode_version,
                stats=stats,
            )
            print_summary(stats, args.dry_run)
            return 1 if stats.failures else 0

        # action == "all"
        if not config_exists:
            if config_explicit:
                raise DownloaderError(
                    f"找不到配置文件: {config_path}\n"
                    f"请准备 {DEFAULT_CONFIG_NAME}，或用 --config 指定路径。"
                )
            print(
                f"未找到 {DEFAULT_CONFIG_NAME}，跳过插件和远程组件，"
                "只下载你选择的 VS Code 安装包。"
            )
            selected_targets = select_installer_targets(
                args.targets, None, False
            )
            if not confirm_download_plan(
                [installer_plan_section(selected_targets, None)],
                base_dir,
                yes=args.yes,
                dry_run=args.dry_run,
            ):
                return 0
            stats = RunStats()
            download_installers(
                base_dir,
                selected_targets,
                force=args.force,
                dry_run=args.dry_run,
                retries=args.retries,
                timeout=args.timeout,
                download_method=download_method,
                stats=stats,
            )
            print_summary(stats, args.dry_run)
            return 1 if stats.failures else 0

        config = load_download_config(config_path)
        stats = RunStats()
        selected_targets = select_installer_targets(
            args.targets, config.vscode_targets, True
        )
        plugin_targets = extension_targets_for_vscode_targets(
            selected_targets
        )
        if not plugin_targets:
            plugin_targets = extension_targets_for_vscode_targets(
                config.vscode_targets
            )
        specs = adapt_extension_specs(
            config.extensions, plugin_targets, timeout=args.timeout
        )

        sections = [
            installer_plan_section(
                selected_targets, config.vscode_version
            )
        ]
        if not args.no_remote:
            sections.append(
                remote_plan_section(
                    args.cli_flavor, config.vscode_version
                )
            )
        sections.append(extension_plan_section(specs))
        if not confirm_download_plan(
            sections,
            base_dir,
            yes=args.yes,
            dry_run=args.dry_run,
        ):
            return 0

        download_installers(
            base_dir,
            selected_targets,
            force=args.force,
            dry_run=args.dry_run,
            retries=args.retries,
            timeout=args.timeout,
            download_method=download_method,
            vscode_version=config.vscode_version,
            stats=stats,
        )
        if args.no_remote:
            print("\n[2/3] 远程开发 Server + CLI")
            detail("已通过 --no-remote 跳过。")
        else:
            download_remote(
                base_dir,
                args.cli_flavor,
                force=args.force,
                dry_run=args.dry_run,
                retries=args.retries,
                timeout=args.timeout,
                download_method=download_method,
                vscode_version=config.vscode_version,
                stats=stats,
            )
        download_extensions(
            specs,
            base_dir,
            force=args.force,
            dry_run=args.dry_run,
            retries=args.retries,
            timeout=args.timeout,
            vscode_version=config.vscode_version,
            stats=stats,
        )
        print_summary(stats, args.dry_run)
        return 1 if stats.failures else 0

    except DownloaderError as error:
        print(f"\n错误: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
