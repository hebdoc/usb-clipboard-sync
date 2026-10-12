"""
USB 剪贴板双向同步工具 (PC <-> Android)
功能：
1. 电脑端复制文字，安卓手机立即同步，可直接在手机粘贴
2. 安卓手机复制文字，电脑端立即同步，可直接在电脑粘贴
基于 ADB 与 scrcpy-server 底层协议实现，无需在手机安装额外 APP。
"""

import sys
import os
import time
import socket
import struct
import subprocess
import threading
import ctypes
from ctypes import wintypes
import random
import hashlib
import io
from typing import Optional, Tuple, Callable

class LogStream:
    """安全的日志缓冲流：防止在 --noconsole / pythonw 模式下因 sys.stdout 为 None 报错，并保留最近日志历史"""

    def __init__(self, original=None):
        self.original = original
        self.history = []
        self.max_history = 500
        self.lock = threading.Lock()

    def write(self, s: str):
        if not s:
            return
        with self.lock:
            self.history.append(s)
            if len(self.history) > self.max_history:
                self.history = self.history[-self.max_history:]
        if self.original and hasattr(self.original, "write"):
            try:
                self.original.write(s)
            except Exception:
                pass

    def flush(self):
        if self.original and hasattr(self.original, "flush"):
            try:
                self.original.flush()
            except Exception:
                pass


# 确保控制台编码并包装标准输出/错误，避免无控制台模式下崩溃
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log_stream = LogStream(sys.stdout)
sys.stdout = log_stream
sys.stderr = log_stream

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    from PIL import Image, ImageDraw, ImageGrab
    import pystray
except ImportError:
    Image = None
    ImageDraw = None
    ImageGrab = None
    pystray = None

# 常量定义
SC_CONTROL_MSG_TYPE_GET_CLIPBOARD = 8
SC_CONTROL_MSG_TYPE_SET_CLIPBOARD = 9
SC_CONTROL_MSG_TYPE_SET_IMAGE_CLIPBOARD = 21

SC_DEVICE_MSG_TYPE_CLIPBOARD = 0
SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD = 1
SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD = 3

STATUS_DISCONNECTED = "disconnected"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"

DEFAULT_PORT = 28282
DEFAULT_SOCKET_NAME = "scrcpy"
REMOTE_JAR_PATH = "/data/local/tmp/scrcpy-sync-server.jar"

# Windows 子进程不弹出黑窗口的标志
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_free_port() -> int:
    """获取一个可用的本地空闲 TCP 端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def set_console_visible(visible: bool):
    """显示或隐藏 Windows 控制台窗口（支持无控制台启动后动态分配与显隐）"""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if visible:
            if not hwnd:
                # 之前完全没有控制台（--noconsole 启动），动态分配并重新定向输出
                ctypes.windll.kernel32.AllocConsole()
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    ctypes.windll.kernel32.SetConsoleTitleW("USB 剪贴板双向同步 - 运行日志")
                    try:
                        conout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                        log_stream.original = conout
                        with log_stream.lock:
                            for chunk in log_stream.history:
                                conout.write(chunk)
                            conout.flush()
                    except Exception:
                        pass
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5)
        else:
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def generate_tray_icon(connected: bool):
    """动态生成内存托盘图标 (双态)"""
    if Image is None or ImageDraw is None:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 板身 (深蓝商务风)
    d.rectangle([12, 12, 52, 58], fill="#1E3A8A", outline="#FFFFFF", width=2)
    # 顶部夹子
    d.rectangle([24, 6, 40, 14], fill="#94A3B8", outline="#FFFFFF", width=2)
    # 文本线条
    d.line([18, 24, 46, 24], fill="#FFFFFF", width=2)
    d.line([18, 32, 46, 32], fill="#FFFFFF", width=2)
    d.line([18, 40, 36, 40], fill="#FFFFFF", width=2)
    # 状态指示灯（右下角：绿色已连接 / 橙色等待连接）
    dot_color = "#22C55E" if connected else "#F97316"
    d.ellipse([40, 40, 58, 58], fill=dot_color, outline="#FFFFFF", width=2)
    return img


class ScrcpyProtocol:
    """处理 scrcpy-server 二进制控制协议报文封包与解包"""

    @staticmethod
    def build_set_clipboard(text: str, seq: int = 1, paste: bool = False) -> bytes:
        """
        构建向设备设置剪贴板的控制报文
        格式:
          - type: 1 字节 (9)
          - sequence: 8 字节 uint64 (大端)
          - paste: 1 字节 bool (0 或 1)
          - length: 4 字节 uint32 (大端)
          - text: UTF-8 编码文本
        """
        text_bytes = text.encode("utf-8")
        paste_flag = 1 if paste else 0
        header = struct.pack(">B Q B I", SC_CONTROL_MSG_TYPE_SET_CLIPBOARD, seq, paste_flag, len(text_bytes))
        return header + text_bytes

    @staticmethod
    def build_set_image_clipboard(
        image_bytes: bytes, mime_type: str = "image/png", seq: int = 1, paste: bool = False
    ) -> bytes:
        """
        构建向设备设置图片剪贴板的控制报文 (符合 scrcpy PR #6676)
        格式:
          - type: 1 字节 (21)
          - sequence: 8 字节 uint64 (大端)
          - paste: 1 字节 bool (0 或 1)
          - mimetype_len: 4 字节 uint32 (大端)
          - mimetype: UTF-8 编码 MIME 字符串
          - size: 4 字节 uint32 (大端)
          - data: 原始图片二进制数据
        """
        mime_bytes = mime_type.encode("utf-8")
        header = struct.pack(
            ">B Q B I",
            SC_CONTROL_MSG_TYPE_SET_IMAGE_CLIPBOARD,
            seq,
            1 if paste else 0,
            len(mime_bytes),
        )
        body = mime_bytes + struct.pack(">I", len(image_bytes)) + image_bytes
        return header + body

    @staticmethod
    def build_get_clipboard(copy_key: bool = False) -> bytes:
        """
        构建从设备获取剪贴板的控制报文
        格式:
          - type: 1 字节 (8)
          - copy_key: 1 字节 bool (0 或 1)
        """
        return struct.pack(">B B", SC_CONTROL_MSG_TYPE_GET_CLIPBOARD, 1 if copy_key else 0)

    @staticmethod
    def read_exact(sock: socket.socket, num_bytes: int) -> bytes:
        """从 socket 读取指定字节数"""
        buf = bytearray()
        while len(buf) < num_bytes:
            try:
                chunk = sock.recv(num_bytes - len(buf))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionResetError("Socket 连接已关闭")
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def read_device_msg(sock: socket.socket) -> Tuple[int, any]:
        """
        读取并解析从设备发来的通知报文
        返回: (msg_type, payload)
          - (SC_DEVICE_MSG_TYPE_CLIPBOARD, "文本内容")
          - (SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD, seq_number)
          - (SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD, (mimetype, image_bytes))
        """
        type_buf = ScrcpyProtocol.read_exact(sock, 1)
        msg_type = type_buf[0]

        if msg_type == SC_DEVICE_MSG_TYPE_CLIPBOARD:
            len_buf = ScrcpyProtocol.read_exact(sock, 4)
            text_len = struct.unpack(">I", len_buf)[0]
            text_bytes = ScrcpyProtocol.read_exact(sock, text_len)
            text = text_bytes.decode("utf-8", errors="replace")
            return msg_type, text
        elif msg_type == SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD:
            seq_buf = ScrcpyProtocol.read_exact(sock, 8)
            seq = struct.unpack(">Q", seq_buf)[0]
            return msg_type, seq
        elif msg_type == SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD:
            len_header = ScrcpyProtocol.read_exact(sock, 8)
            mimetype_len, data_len = struct.unpack(">I I", len_header)
            mimetype_bytes = ScrcpyProtocol.read_exact(sock, mimetype_len) if mimetype_len > 0 else b""
            mimetype = mimetype_bytes.decode("utf-8", errors="replace")
            image_data = ScrcpyProtocol.read_exact(sock, data_len) if data_len > 0 else b""
            return msg_type, (mimetype, image_data)
        else:
            return msg_type, None


def get_base_dirs():
    """返回所有可能的资源搜索根目录（支持源码运行、便携版运行及 PyInstaller 单文件打包）"""
    dirs = []
    # 1. PyInstaller 打包后的解压临时目录
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)
    # 2. 当前运行可执行文件所在目录
    if hasattr(sys, "executable") and sys.executable:
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    # 3. 当前脚本所在目录
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    dirs.append(cur_dir)
    # 4. 当前工作目录
    dirs.append(os.getcwd())

    seen = set()
    res = []
    for d in dirs:
        norm = os.path.normpath(d)
        if norm not in seen and os.path.isdir(norm):
            seen.add(norm)
            res.append(norm)
    return res


class ClipboardSyncEngine:
    def __init__(
        self,
        port: int = DEFAULT_PORT,
        serial: Optional[str] = None,
        status_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.port = port
        self.serial = serial
        self.status_callback = status_callback
        self.sock: Optional[socket.socket] = None
        self.server_proc: Optional[subprocess.Popen] = None
        self.running = False
        self.should_exit = False
        self.scid = 0
        self.last_synced_text = ""
        self.last_synced_image_hash = ""
        self.last_clip_seq = 0
        self.lock = threading.RLock()
        self.adb_cmd = "adb"
        self._find_adb()
        self.local_server_path = None
        self._find_scrcpy_server()

    def _notify_status(self, status: str, detail: str):
        """向上层 UI / 托盘上报当前连接状态变更"""
        if self.status_callback:
            try:
                self.status_callback(status, detail)
            except Exception:
                pass

    def _find_adb(self) -> str:
        """查找自带或系统的 adb 可执行文件路径"""
        candidates = []
        for base in get_base_dirs():
            candidates.append(os.path.join(base, "adb.exe"))
            candidates.append(os.path.join(base, "adb"))
            candidates.append(os.path.join(base, "bin", "adb.exe"))
            candidates.append(os.path.join(base, "bin", "adb"))
            candidates.append(os.path.join(base, "platform-tools", "adb.exe"))

        for c in candidates:
            if os.path.isfile(c):
                adb_dir = os.path.dirname(c)
                # 确保 adb 所在目录位于 PATH 前列以加载同目录的 DLL
                cur_path = os.environ.get("PATH", "")
                if adb_dir.lower() not in cur_path.lower():
                    os.environ["PATH"] = adb_dir + os.pathsep + cur_path
                self.adb_cmd = c
                return c

        try:
            where_out = subprocess.check_output(
                ["where.exe", "adb"], stderr=subprocess.DEVNULL, text=True, creationflags=CREATE_NO_WINDOW
            )
            for line in where_out.splitlines():
                line = line.strip()
                if line and os.path.isfile(line):
                    self.adb_cmd = line
                    return line
        except Exception:
            pass

        self.adb_cmd = "adb"
        return "adb"

    def _find_scrcpy_server(self) -> Optional[str]:
        """查找本地存在的 scrcpy-server 二进制文件"""
        candidates = []

        for base in get_base_dirs():
            candidates.append(os.path.join(base, "scrcpy-server"))
            candidates.append(os.path.join(base, "scrcpy-server.jar"))
            candidates.append(os.path.join(base, "bin", "scrcpy-server"))
            candidates.append(os.path.join(base, "bin", "scrcpy-server.jar"))

        # 从 PATH 中的 scrcpy 获取
        try:
            where_out = subprocess.check_output(
                ["where.exe", "scrcpy"], stderr=subprocess.DEVNULL, text=True, creationflags=CREATE_NO_WINDOW
            )
            for line in where_out.splitlines():
                line = line.strip()
                if line:
                    scrcpy_dir = os.path.dirname(line)
                    candidates.append(os.path.join(scrcpy_dir, "scrcpy-server"))
                    candidates.append(os.path.join(scrcpy_dir, "scrcpy-server.jar"))
        except Exception:
            pass

        # 常见 WinGet 路径
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            winget_pkg = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
            if os.path.exists(winget_pkg):
                for root, dirs, files in os.walk(winget_pkg):
                    if "scrcpy-server" in files:
                        candidates.append(os.path.join(root, "scrcpy-server"))
                    if "scrcpy-server.jar" in files:
                        candidates.append(os.path.join(root, "scrcpy-server.jar"))

        for c in candidates:
            if os.path.isfile(c) and os.path.getsize(c) > 10000:
                self.local_server_path = c
                return c

        self.local_server_path = None
        return None

    def _run_adb(self, args: list, **kwargs) -> subprocess.CompletedProcess:
        cmd = [self.adb_cmd]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, **kwargs)

    def get_connected_device(self) -> Optional[str]:
        """获取连接的 ADB 设备（排查所有设备，不带 -s 参数）"""
        try:
            res = subprocess.run(
                [self.adb_cmd, "devices"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
            )
            if res.returncode != 0:
                return None
            devices = []
            for line in res.stdout.splitlines()[1:]:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            if not devices:
                return None
            if self.serial and self.serial in devices:
                return self.serial
            self.serial = devices[0]
            return self.serial
        except Exception:
            return None

    def ensure_server_on_device(self) -> bool:
        """确保 scrcpy 服务端 jar 存在于安卓设备上且与本地版本一致"""
        if not self.local_server_path or not os.path.exists(self.local_server_path):
            print("[-] 未在本地找到 scrcpy-server 文件，请确认 scrcpy 已安装。", flush=True)
            return False

        local_size = os.path.getsize(self.local_server_path)
        # 检查远端文件大小是否与本地完全一致
        check = self._run_adb(["shell", f"ls -l {REMOTE_JAR_PATH} 2>/dev/null"])
        if check.returncode == 0 and check.stdout.strip():
            parts = check.stdout.strip().split()
            if len(parts) >= 5:
                try:
                    remote_size = int(parts[4])
                    if remote_size == local_size:
                        return True
                except ValueError:
                    pass

        print(f"[*] 正在推送同步服务端到手机: {REMOTE_JAR_PATH} ({local_size} 字节)...", flush=True)
        push_res = self._run_adb(["push", self.local_server_path, REMOTE_JAR_PATH])
        if push_res.returncode != 0:
            print(f"[-] 推送失败: {push_res.stderr}", flush=True)
            return False
        return True

    def ensure_share_helper_installed(self):
        """检查并安装轻量分享组件（供手机相册/文件管理器【发送】时一键同步原图）"""
        apk_candidates = []
        for base in get_base_dirs():
            apk_candidates.append(os.path.join(base, "ShareToPC.apk"))
            apk_candidates.append(os.path.join(base, "bin", "ShareToPC.apk"))

        apk_path = None
        for c in apk_candidates:
            if os.path.isfile(c) and os.path.getsize(c) > 1000:
                apk_path = c
                break

        if not apk_path:
            return

        try:
            chk = self._run_adb(["shell", "pm path com.sync.clipboard.share 2>/dev/null"])
            if chk.returncode != 0 or not chk.stdout.strip():
                print("[*] 正在为手机配置相册/文件管理【发送到电脑】快捷组件...", flush=True)
                self._run_adb(["install", "-r", "-g", apk_path])
        except Exception:
            pass

    def start_connection(self) -> bool:
        """启动后台服务并建立连接（使用动态 SCID 与空闲端口避免会话冲突）"""
        device = self.get_connected_device()
        if not device:
            return False

        if not self.ensure_server_on_device():
            return False

        self.ensure_share_helper_installed()

        # 生成本次会话独立的随机 SCID，用于隔离安卓端的抽象套接字
        self.scid = random.randint(0x10000000, 0x7FFFFFFF)
        socket_name = f"{DEFAULT_SOCKET_NAME}_{self.scid:08x}"

        # 动态分配本机空闲 TCP 端口，杜绝端口残留冲突
        try:
            self.port = find_free_port()
        except Exception:
            self.port = DEFAULT_PORT

        # 端口转发
        fwd_res = self._run_adb(["forward", f"tcp:{self.port}", f"localabstract:{socket_name}"])
        if fwd_res.returncode != 0:
            print(f"[-] adb forward 失败: {fwd_res.stderr}", flush=True)
            return False

        # 启动安卓端 app_process 服务
        cmd = [self.adb_cmd]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend([
            "shell",
            f"CLASSPATH={REMOTE_JAR_PATH} app_process / com.genymobile.scrcpy.Server 4.1 "
            f"scid={self.scid:08x} video=false audio=false tunnel_forward=true cleanup=false send_device_meta=false log_level=info"
        ])

        self.server_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW
        )

        # 尝试连接 Socket
        connected = False
        self.sock = None
        for _ in range(40):
            time.sleep(0.1)
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                dummy = s.recv(1)
                if dummy == b"\x00":
                    # 连接成功后重置为常驻阻塞模式，避免无数据时超时误报断开
                    s.settimeout(None)
                    self.sock = s
                    connected = True
                    break
                s.close()
            except Exception:
                pass

        if not connected:
            print("[-] 连接服务端 Socket 超时", flush=True)
            self.stop_connection()
            return False

        # 初始化当前剪贴板状态
        self._init_clipboard_state()
        return True

    def _init_clipboard_state(self):
        """同步初始剪贴板（支持文本与图片）"""
        img_info = self._get_pc_clipboard_image()
        if img_info:
            with self.lock:
                self.last_synced_image_hash = img_info[2]
                self.last_synced_text = ""
        else:
            pc_text = self._get_pc_clipboard()
            if pc_text:
                with self.lock:
                    self.last_synced_text = pc_text
                    self.last_synced_image_hash = ""

        if sys.platform == "win32":
            try:
                self.last_clip_seq = ctypes.windll.user32.GetClipboardSequenceNumber()
            except Exception:
                pass

    def _get_pc_clipboard(self) -> str:
        """获取 Windows 电脑剪贴板文本"""
        if pyperclip is not None:
            try:
                return pyperclip.paste() or ""
            except Exception:
                pass
        return ""

    def _set_pc_clipboard(self, text: str):
        """设置 Windows 电脑剪贴板文本"""
        if pyperclip is not None:
            try:
                pyperclip.copy(text)
                if sys.platform == "win32":
                    try:
                        self.last_clip_seq = ctypes.windll.user32.GetClipboardSequenceNumber()
                    except Exception:
                        pass
                return
            except Exception as e:
                print(f"[!] 写入电脑剪贴板失败: {e}", flush=True)

    def _get_pc_clipboard_image(self) -> Optional[Tuple[bytes, str, str]]:
        """从 Windows 电脑剪贴板获取图片（返回 image_bytes, mime_type, sha256_hash）"""
        if ImageGrab is None or Image is None:
            return None
        for _ in range(5):
            try:
                clip_data = ImageGrab.grabclipboard()
                if isinstance(clip_data, Image.Image):
                    buf = io.BytesIO()
                    clip_data.save(buf, format="PNG")
                    data = buf.getvalue()
                    h = hashlib.sha256(data).hexdigest()
                    return data, "image/png", h
                elif isinstance(clip_data, list) and len(clip_data) == 1 and isinstance(clip_data[0], str):
                    filepath = clip_data[0]
                    if os.path.isfile(filepath) and filepath.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")
                    ):
                        try:
                            with Image.open(filepath) as img:
                                buf = io.BytesIO()
                                img.save(buf, format="PNG")
                                data = buf.getvalue()
                                h = hashlib.sha256(data).hexdigest()
                                return data, "image/png", h
                        except Exception:
                            pass
                if clip_data is not None:
                    break
            except Exception:
                pass
            time.sleep(0.02)
        return None

    def _set_pc_clipboard_image(self, image_bytes: bytes, mime_type: str = "image/png") -> bool:
        """将接收到的图片写入 Windows 电脑剪贴板（同时注册设置 CF_DIB 与 PNG 格式）"""
        if sys.platform != "win32" or Image is None:
            return False
        try:
            img = Image.open(io.BytesIO(image_bytes))

            # 1. 转换为 CF_DIB (无 14 字节文件头 BMP 数据)
            output_bmp = io.BytesIO()
            img.convert("RGB").save(output_bmp, "BMP")
            bmp_data = output_bmp.getvalue()
            dib_data = bmp_data[14:]  # 剥离 BITMAPFILEHEADER

            # 2. 保留原生 PNG 数据 (保留透明度)
            output_png = io.BytesIO()
            img.save(output_png, "PNG")
            png_data = output_png.getvalue()

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            GMEM_MOVEABLE = 0x0002
            CF_DIB = 8

            kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.restype = ctypes.c_int
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.restype = ctypes.c_int
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = ctypes.c_int
            user32.EmptyClipboard.argtypes = []
            user32.EmptyClipboard.restype = ctypes.c_int
            user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            user32.SetClipboardData.restype = ctypes.c_void_p
            user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            user32.RegisterClipboardFormatW.restype = ctypes.c_uint

            try:
                user32.CloseClipboard()
            except Exception:
                pass

            opened = False
            for _ in range(20):
                if user32.OpenClipboard(None):
                    opened = True
                    break
                time.sleep(0.03)
            if not opened:
                return False

            try:
                user32.EmptyClipboard()

                # 写入 CF_DIB 格式（传统软件兼容性最好，如微信、QQ、画图、Office 等）
                h_dib = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib_data))
                if h_dib:
                    ptr = kernel32.GlobalLock(h_dib)
                    if ptr:
                        ctypes.memmove(ptr, dib_data, len(dib_data))
                        kernel32.GlobalUnlock(h_dib)
                        user32.SetClipboardData(CF_DIB, h_dib)

                # 写入注册的 PNG 格式（现代化透明通道软件兼容）
                cf_png = user32.RegisterClipboardFormatW("PNG")
                if cf_png:
                    h_png = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(png_data))
                    if h_png:
                        ptr = kernel32.GlobalLock(h_png)
                        if ptr:
                            ctypes.memmove(ptr, png_data, len(png_data))
                            kernel32.GlobalUnlock(h_png)
                            user32.SetClipboardData(cf_png, h_png)
                return True
            finally:
                user32.CloseClipboard()
                try:
                    self.last_clip_seq = user32.GetClipboardSequenceNumber()
                except Exception:
                    pass
        except Exception as e:
            print(f"[!] 写入电脑图片剪贴板失败: {e}", flush=True)
            return False

    def send_clipboard_to_phone(self, text: str):
        """电脑端复制文字 -> 同步给手机"""
        if not self.sock:
            return
        try:
            msg = ScrcpyProtocol.build_set_clipboard(text, seq=1, paste=False)
            with self.lock:
                self.last_synced_text = text
                self.last_synced_image_hash = ""
                self.sock.sendall(msg)
            preview = text.replace("\r", " ").replace("\n", " ")
            if len(preview) > 35:
                preview = preview[:32] + "..."
            print(f"[电脑 -> 手机] 成功同步文字: \"{preview}\"", flush=True)
        except Exception as e:
            print(f"[-] 发送文字到手机失败: {e}", flush=True)
            self.running = False

    def send_image_to_phone(self, image_bytes: bytes, mime_type: str = "image/png"):
        """电脑端复制图片 -> 同步给手机 (符合 scrcpy PR #6676)"""
        if not self.sock:
            return
        try:
            msg = ScrcpyProtocol.build_set_image_clipboard(
                image_bytes, mime_type=mime_type, seq=1, paste=False
            )
            img_hash = hashlib.sha256(image_bytes).hexdigest()
            with self.lock:
                self.last_synced_image_hash = img_hash
                self.last_synced_text = ""
                self.sock.sendall(msg)
            size_kb = len(image_bytes) / 1024
            print(f"[电脑 -> 手机] 成功同步图片 (格式: {mime_type}, 大小: {size_kb:.1f} KB)", flush=True)
        except Exception as e:
            print(f"[-] 发送图片到手机失败: {e}", flush=True)
            self.running = False

    def _phone_listener_loop(self):
        """后台线程：接收安卓手机剪贴板的更新事件（支持文字与图片）"""
        while self.running and self.sock:
            try:
                msg_type, payload = ScrcpyProtocol.read_device_msg(self.sock)
                if msg_type == SC_DEVICE_MSG_TYPE_CLIPBOARD:
                    new_text = payload or ""
                    with self.lock:
                        if new_text and new_text != self.last_synced_text:
                            self.last_synced_text = new_text
                            self.last_synced_image_hash = ""
                            self._set_pc_clipboard(new_text)
                            preview = new_text.replace("\r", " ").replace("\n", " ")
                            if len(preview) > 35:
                                preview = preview[:32] + "..."
                            print(f"[手机 -> 电脑] 成功同步文字: \"{preview}\"", flush=True)
                elif msg_type == SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD:
                    mimetype, img_bytes = payload
                    if img_bytes:
                        img_hash = hashlib.sha256(img_bytes).hexdigest()
                        with self.lock:
                            if img_hash != self.last_synced_image_hash:
                                self.last_synced_image_hash = img_hash
                                self.last_synced_text = ""
                                self._set_pc_clipboard_image(img_bytes, mimetype)
                                size_kb = len(img_bytes) / 1024
                                print(f"[手机 -> 电脑] 成功同步图片 (格式: {mimetype}, 大小: {size_kb:.1f} KB)", flush=True)
                elif msg_type == SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD:
                    pass
            except (ConnectionResetError, ConnectionAbortedError, socket.error) as e:
                if self.running:
                    print(f"[!] 与手机的连接断开 ({e})", flush=True)
                    self.running = False
                break
            except Exception as e:
                if self.running:
                    print(f"[!] 手机监听异常: {e}", flush=True)
                    self.running = False
                break

    def _pc_monitor_loop(self):
        """后台线程：监听电脑剪贴板变更（支持文字与图片）"""
        while self.running and self.sock:
            try:
                changed = False
                if sys.platform == "win32":
                    current_seq = ctypes.windll.user32.GetClipboardSequenceNumber()
                    if current_seq != self.last_clip_seq:
                        self.last_clip_seq = current_seq
                        changed = True
                else:
                    changed = True

                if changed:
                    time.sleep(0.06)  # 等待剪贴板写入完成

                    # 1. 优先检查剪贴板是否复制了图片
                    img_info = self._get_pc_clipboard_image()
                    if img_info:
                        img_bytes, mime_type, img_hash = img_info
                        should_send_img = False
                        with self.lock:
                            if img_hash != self.last_synced_image_hash:
                                should_send_img = True
                        if should_send_img:
                            self.send_image_to_phone(img_bytes, mime_type)
                    else:
                        # 2. 若不是图片，检查是否复制了文本
                        curr_text = self._get_pc_clipboard()
                        should_send_text = False
                        with self.lock:
                            if curr_text and curr_text != self.last_synced_text:
                                should_send_text = True
                        if should_send_text:
                            self.send_clipboard_to_phone(curr_text)
            except Exception:
                pass
            time.sleep(0.1)

    def _phone_screenshot_monitor_loop(self):
        """后台线程：监听手机相册/截屏目录的新图片，秒级自动同步到电脑剪贴板"""
        seen_files = set()
        # 启动时先获取当前已存在的最近图片，避免启动时重复同步手机历史截图
        try:
            init_res = self._run_adb([
                "shell",
                "ls -1t /sdcard/DCIM/Screenshots/* /sdcard/Pictures/Screenshots/* 2>/dev/null | head -n 30",
            ])
            if init_res.returncode == 0 and init_res.stdout.strip():
                for line in init_res.stdout.strip().splitlines():
                    f = line.strip()
                    if f:
                        seen_files.add(f)
        except Exception:
            pass

        while self.running and self.sock:
            try:
                res = self._run_adb([
                    "shell",
                    "ls -1t /sdcard/DCIM/Screenshots/* /sdcard/Pictures/Screenshots/* 2>/dev/null | head -n 5",
                ])
                if res.returncode == 0 and res.stdout.strip():
                    new_candidates = []
                    for line in reversed(res.stdout.strip().splitlines()):
                        f = line.strip()
                        if f and f not in seen_files:
                            seen_files.add(f)
                            new_candidates.append(f)

                    for newest_file in new_candidates:
                        time.sleep(0.15)  # 等待手机完成文件落盘写入
                        cmd = [self.adb_cmd]
                        if self.serial:
                            cmd.extend(["-s", self.serial])
                        cmd.extend(["exec-out", "cat", newest_file])
                        pull_proc = subprocess.run(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW,
                        )
                        img_bytes = pull_proc.stdout
                        if img_bytes and len(img_bytes) > 100:
                            img_hash = hashlib.sha256(img_bytes).hexdigest()
                            should_sync = False
                            with self.lock:
                                if img_hash != self.last_synced_image_hash:
                                    self.last_synced_image_hash = img_hash
                                    self.last_synced_text = ""
                                    should_sync = True

                            if should_sync:
                                ext = os.path.splitext(newest_file)[1].lower()
                                mime = "image/png" if ext == ".png" else "image/jpeg"
                                if self._set_pc_clipboard_image(img_bytes, mime):
                                    fname = os.path.basename(newest_file)
                                    size_kb = len(img_bytes) / 1024
                                    print(f"[手机 -> 电脑] 成功同步截屏/照片: {fname} ({size_kb:.1f} KB)", flush=True)

                    if len(seen_files) > 500:
                        seen_files = set(list(seen_files)[-200:])
            except Exception:
                pass
            time.sleep(0.7)

    def stop_connection(self):
        """释放当前连接并清理相关会话资源"""
        self.running = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        if self.server_proc:
            try:
                self.server_proc.terminate()
                self.server_proc.wait(timeout=1)
            except Exception:
                pass
            self.server_proc = None

        try:
            self._run_adb(["forward", "--remove", f"tcp:{self.port}"])
        except Exception:
            pass

        self.serial = None

    def run_forever(self):
        """主循环：包含自动检测设备与全自动断线无缝重连"""
        print("==================================================", flush=True)
        print("    USB 剪贴板双向同步工具 (PC <-> Android)", flush=True)
        print("==================================================", flush=True)
        print("[*] 正在检测已连接的 USB 调试设备...", flush=True)

        while not self.should_exit:
            try:
                device = self.get_connected_device()
                if not device:
                    self._notify_status(STATUS_DISCONNECTED, "等待手机连接 (请开启 USB 调试)...")
                    print("[*] 等待安卓手机通过 USB 数据线连接 (请开启 USB 调试)...", end="\r", flush=True)
                    time.sleep(1.0)
                    continue

                self._notify_status(STATUS_CONNECTING, f"正在连接设备: {device}...")
                print(f"\n[+] 发现安卓设备: {device}", flush=True)
                print("[*] 正在启动双向剪贴板同步通道...", flush=True)

                if not self.start_connection():
                    self._notify_status(STATUS_DISCONNECTED, "通道启动失败，正在重试...")
                    print("[-] 启动失败，将在 2 秒后重试...", flush=True)
                    time.sleep(2.0)
                    continue

                self._notify_status(STATUS_CONNECTED, f"已就绪: {device}")
                print("[+] 双向剪贴板同步已就绪！", flush=True)
                print("    - 电脑端复制文字/图片 -> 手机端即可直接粘贴", flush=True)
                print("    - 手机端复制文字/图片 -> 电脑端即可直接粘贴", flush=True)
                print("    - 可在托盘菜单隐藏控制台或按 Ctrl + C 退出\n", flush=True)

                self.running = True
                t_phone = threading.Thread(target=self._phone_listener_loop, daemon=True)
                t_pc = threading.Thread(target=self._pc_monitor_loop, daemon=True)
                t_shot = threading.Thread(target=self._phone_screenshot_monitor_loop, daemon=True)

                t_phone.start()
                t_pc.start()
                t_shot.start()

                while self.running and not self.should_exit:
                    time.sleep(0.5)

                self.stop_connection()
                self._notify_status(STATUS_DISCONNECTED, "手机已断开，等待重新连接...")
                print("\n[!] 连接已断开，等待重新连接...", flush=True)
                time.sleep(1.0)

            except KeyboardInterrupt:
                print("\n[*] 正在退出并清理资源...", flush=True)
                self.should_exit = True
                self.stop_connection()
                print("[+] 已退出。", flush=True)
                break
            except Exception as e:
                print(f"\n[-] 发生异常: {e}", flush=True)
                self.stop_connection()
                time.sleep(2.0)


class TrayManager:
    """Windows 任务栏系统托盘管理器"""

    def __init__(self, engine: ClipboardSyncEngine, auto_hide_console: bool = True):
        self.engine = engine
        self.auto_hide_console = auto_hide_console

        # 检查启动时是否实际存在可见的控制台窗口
        has_console = False
        if sys.platform == "win32":
            try:
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd and ctypes.windll.user32.IsWindowVisible(hwnd):
                    has_console = True
            except Exception:
                pass

        self.console_visible = has_console
        self.icon: Optional[pystray.Icon] = None
        self.current_status_text = "等待手机连接..."
        self.connected = False
        self.lock = threading.Lock()

    def set_console(self, visible: bool):
        self.console_visible = visible
        set_console_visible(visible)
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def toggle_console(self, icon=None, item=None):
        self.set_console(not self.console_visible)

    def on_reconnect(self, icon=None, item=None):
        print("[*] 正在触发重新连接...", flush=True)
        self.engine.stop_connection()

    def on_exit(self, icon=None, item=None):
        print("[*] 正在退出...", flush=True)
        self.engine.should_exit = True
        self.engine.stop_connection()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.set_console(True)
        os._exit(0)

    def _create_menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda text: f"状态: {self.current_status_text}", lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda text: "隐藏控制台窗口" if self.console_visible else "显示控制台窗口",
                self.toggle_console,
                default=True,
            ),
            pystray.MenuItem("重新连接", self.on_reconnect),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", self.on_exit),
        )

    def on_status_change(self, status: str, detail: str):
        with self.lock:
            self.current_status_text = detail
            new_connected = (status == STATUS_CONNECTED)
            status_changed = (new_connected != self.connected)
            self.connected = new_connected

            if self.icon:
                try:
                    self.icon.icon = generate_tray_icon(self.connected)
                    self.icon.title = f"USB 剪贴板同步 - {detail}"
                    self.icon.update_menu()
                except Exception:
                    pass

                # 发送 Windows 系统气泡通知
                if status_changed:
                    try:
                        if self.connected:
                            self.icon.notify(f"{detail}\n电脑与手机双向文字/图片剪贴板同步已就绪！", "USB 剪贴板同步")
                        else:
                            self.icon.notify("手机连接已断开，正在后台守候重新连接...", "USB 剪贴板同步")
                    except Exception:
                        pass

    def run(self):
        if pystray is None or Image is None:
            print("[!] 系统缺少 pystray 或 Pillow，以无托盘控制台模式运行。", flush=True)
            self.engine.run_forever()
            return

        # 如果启用自动隐藏且当前有可见控制台，立即隐藏，消除闪烁
        if self.auto_hide_console and self.console_visible:
            self.set_console(False)

        initial_icon = generate_tray_icon(False)
        self.icon = pystray.Icon(
            "usb_clipboard_sync",
            initial_icon,
            "USB 剪贴板双向同步",
            menu=self._create_menu(),
        )

        # 后台启动同步引擎线程
        t_engine = threading.Thread(target=self.engine.run_forever, daemon=True)
        t_engine.start()

        # 主线程进入托盘消息循环
        try:
            self.icon.run()
        except KeyboardInterrupt:
            self.on_exit()


def main():
    show_console = "--show-console" in sys.argv
    no_tray = "--no-tray" in sys.argv

    engine = ClipboardSyncEngine()

    if no_tray or pystray is None or Image is None:
        engine.run_forever()
    else:
        tray = TrayManager(engine, auto_hide_console=(not show_console))
        engine.status_callback = tray.on_status_change
        tray.run()


if __name__ == "__main__":
    main()
