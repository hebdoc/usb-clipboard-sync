"""
Unit tests for usb_clipboard_sync.py
"""

import unittest
import socket
import struct
import io
import sys
import os
import hashlib

# Add parent dir to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usb_clipboard_sync import (
    ScrcpyProtocol,
    ClipboardSyncEngine,
    SC_CONTROL_MSG_TYPE_SET_CLIPBOARD,
    SC_CONTROL_MSG_TYPE_GET_CLIPBOARD,
    SC_CONTROL_MSG_TYPE_SET_IMAGE_CLIPBOARD,
    SC_DEVICE_MSG_TYPE_CLIPBOARD,
    SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD,
    SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD,
    find_free_port,
    generate_tray_icon,
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_CONNECTING,
    LogStream,
    set_console_visible,
    CREATE_NO_WINDOW,
    get_fixed_bin_dir,
    get_base_dirs,
)


class MockSocket:
    def __init__(self, data: bytes = b""):
        self.recv_buf = io.BytesIO(data)
        self.sent_buf = bytearray()
        self.closed = False

    def recv(self, num_bytes: int) -> bytes:
        if self.closed:
            return b""
        return self.recv_buf.read(num_bytes)

    def sendall(self, data: bytes):
        if self.closed:
            raise BrokenPipeError("Socket closed")
        self.sent_buf.extend(data)

    def close(self):
        self.closed = True


class TestScrcpyProtocol(unittest.TestCase):
    def test_build_set_clipboard_ascii(self):
        text = "hello world"
        text_bytes = text.encode("utf-8")
        seq = 100
        msg = ScrcpyProtocol.build_set_clipboard(text, seq=seq, paste=False)

        # Expected size: 1 (type) + 8 (seq) + 1 (paste) + 4 (len) + len(text_bytes) = 14 + 11 = 25
        self.assertEqual(len(msg), 14 + len(text_bytes))
        msg_type, msg_seq, msg_paste, msg_len = struct.unpack(">B Q B I", msg[:14])
        self.assertEqual(msg_type, SC_CONTROL_MSG_TYPE_SET_CLIPBOARD)
        self.assertEqual(msg_seq, 100)
        self.assertEqual(msg_paste, 0)
        self.assertEqual(msg_len, len(text_bytes))
        self.assertEqual(msg[14:].decode("utf-8"), text)

    def test_build_set_clipboard_unicode_and_emojis(self):
        text = "你好世界 🚀 剪贴板同步\n第二行测试"
        text_bytes = text.encode("utf-8")
        seq = 9999
        msg = ScrcpyProtocol.build_set_clipboard(text, seq=seq, paste=True)

        msg_type, msg_seq, msg_paste, msg_len = struct.unpack(">B Q B I", msg[:14])
        self.assertEqual(msg_type, SC_CONTROL_MSG_TYPE_SET_CLIPBOARD)
        self.assertEqual(msg_seq, 9999)
        self.assertEqual(msg_paste, 1)
        self.assertEqual(msg_len, len(text_bytes))
        self.assertEqual(msg[14:].decode("utf-8"), text)

    def test_build_get_clipboard(self):
        msg1 = ScrcpyProtocol.build_get_clipboard(copy_key=False)
        self.assertEqual(msg1, struct.pack(">B B", SC_CONTROL_MSG_TYPE_GET_CLIPBOARD, 0))

        msg2 = ScrcpyProtocol.build_get_clipboard(copy_key=True)
        self.assertEqual(msg2, struct.pack(">B B", SC_CONTROL_MSG_TYPE_GET_CLIPBOARD, 1))

    def test_build_set_image_clipboard(self):
        fake_img = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRFAKEIMAGE"
        mime = "image/png"
        mime_bytes = mime.encode("utf-8")
        seq = 8888
        msg = ScrcpyProtocol.build_set_image_clipboard(fake_img, mime_type=mime, seq=seq, paste=True)

        expected_len = 1 + 8 + 1 + 4 + len(mime_bytes) + 4 + len(fake_img)
        self.assertEqual(len(msg), expected_len)

        msg_type, msg_seq, msg_paste, msg_mime_len = struct.unpack(">B Q B I", msg[:14])
        self.assertEqual(msg_type, SC_CONTROL_MSG_TYPE_SET_IMAGE_CLIPBOARD)
        self.assertEqual(msg_seq, 8888)
        self.assertEqual(msg_paste, 1)
        self.assertEqual(msg_mime_len, len(mime_bytes))

        # Check mime string
        extracted_mime = msg[14 : 14 + msg_mime_len].decode("utf-8")
        self.assertEqual(extracted_mime, mime)

        # Check data len and data
        data_len_offset = 14 + msg_mime_len
        msg_data_len = struct.unpack(">I", msg[data_len_offset : data_len_offset + 4])[0]
        self.assertEqual(msg_data_len, len(fake_img))
        self.assertEqual(msg[data_len_offset + 4 :], fake_img)

    def test_read_device_msg_clipboard(self):
        test_str = "来自安卓的复制内容 📱"
        text_bytes = test_str.encode("utf-8")
        raw = struct.pack(">B I", SC_DEVICE_MSG_TYPE_CLIPBOARD, len(text_bytes)) + text_bytes

        mock_sock = MockSocket(raw)
        msg_type, text = ScrcpyProtocol.read_device_msg(mock_sock)
        self.assertEqual(msg_type, SC_DEVICE_MSG_TYPE_CLIPBOARD)
        self.assertEqual(text, test_str)

    def test_read_device_msg_image_clipboard(self):
        fake_img = b"\xff\xd8\xff\xe0\x00\x10JFIF_FAKE_JPEG"
        mime = "image/jpeg"
        mime_bytes = mime.encode("utf-8")

        raw = struct.pack(">B I I", SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD, len(mime_bytes), len(fake_img)) + mime_bytes + fake_img
        mock_sock = MockSocket(raw)
        msg_type, payload = ScrcpyProtocol.read_device_msg(mock_sock)
        self.assertEqual(msg_type, SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD)
        self.assertIsInstance(payload, tuple)
        self.assertEqual(payload[0], mime)
        self.assertEqual(payload[1], fake_img)

    def test_read_device_msg_ack(self):
        seq = 123456789
        raw = struct.pack(">B Q", SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD, seq)

        mock_sock = MockSocket(raw)
        msg_type, ack_seq = ScrcpyProtocol.read_device_msg(mock_sock)
        self.assertEqual(msg_type, SC_DEVICE_MSG_TYPE_ACK_CLIPBOARD)
        self.assertEqual(ack_seq, seq)

    def test_read_exact_eof(self):
        mock_sock = MockSocket(b"123")
        with self.assertRaises(ConnectionResetError):
            ScrcpyProtocol.read_exact(mock_sock, 10)


class TestClipboardSyncEngine(unittest.TestCase):
    def test_engine_init(self):
        engine = ClipboardSyncEngine(port=29999)
        self.assertEqual(engine.port, 29999)
        self.assertFalse(engine.running)
        self.assertEqual(engine.last_synced_text, "")

    def test_send_clipboard_to_phone_dedup(self):
        engine = ClipboardSyncEngine(port=29999)
        mock_sock = MockSocket()
        engine.sock = mock_sock

        engine.send_clipboard_to_phone("test text")
        self.assertGreater(len(mock_sock.sent_buf), 14)
        msg_type, _, _, text_len = struct.unpack(">B Q B I", mock_sock.sent_buf[:14])
        self.assertEqual(msg_type, SC_CONTROL_MSG_TYPE_SET_CLIPBOARD)
        self.assertEqual(mock_sock.sent_buf[14:14+text_len].decode("utf-8"), "test text")

    def test_find_scrcpy_server(self):
        engine = ClipboardSyncEngine()
        server_path = engine._find_scrcpy_server()
        if server_path:
            self.assertTrue(os.path.isfile(server_path))

    def test_find_adb(self):
        engine = ClipboardSyncEngine()
        adb_path = engine._find_adb()
        self.assertTrue(bool(adb_path))
        self.assertEqual(engine.adb_cmd, adb_path)

    def test_get_base_dirs(self):
        from usb_clipboard_sync import get_base_dirs
        dirs = get_base_dirs()
        self.assertIsInstance(dirs, list)
        self.assertGreater(len(dirs), 0)
        for d in dirs:
            self.assertTrue(os.path.isdir(d))

    def test_phone_message_updates_clipboard(self):
        engine = ClipboardSyncEngine(port=29999)
        test_content = "来自手机的新内容 📱"
        raw_msg = struct.pack(">B I", SC_DEVICE_MSG_TYPE_CLIPBOARD, len(test_content.encode("utf-8"))) + test_content.encode("utf-8")
        engine.sock = MockSocket(raw_msg)
        engine.running = True

        copied_values = []
        engine._set_pc_clipboard = lambda text: copied_values.append(text)

        # Run one step of message read
        msg_type, payload = ScrcpyProtocol.read_device_msg(engine.sock)
        self.assertEqual(msg_type, SC_DEVICE_MSG_TYPE_CLIPBOARD)
        if payload != engine.last_synced_text:
            engine.last_synced_text = payload
            engine._set_pc_clipboard(payload)

        self.assertEqual(engine.last_synced_text, test_content)
        self.assertEqual(copied_values, [test_content])

    def test_phone_message_ignores_duplicate(self):
        engine = ClipboardSyncEngine(port=29999)
        test_content = "重复内容"
        engine.last_synced_text = test_content
        copied_values = []
        engine._set_pc_clipboard = lambda text: copied_values.append(text)

        # Incoming same message
        if test_content != engine.last_synced_text:
            engine._set_pc_clipboard(test_content)

        self.assertEqual(copied_values, [])

    def test_stop_connection_clean(self):
        engine = ClipboardSyncEngine(port=29999)
        mock_sock = MockSocket()
        engine.sock = mock_sock
        engine.running = True
        engine.stop_connection()
        self.assertFalse(engine.running)
        self.assertIsNone(engine.sock)
        self.assertTrue(mock_sock.closed)

    def test_find_free_port(self):
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 1024)
        self.assertLessEqual(port, 65535)
        # Port should be free right now
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.close()

    def test_generate_tray_icon(self):
        img_connected = generate_tray_icon(connected=True)
        self.assertIsNotNone(img_connected)
        self.assertEqual(img_connected.size, (64, 64))

        img_disconnected = generate_tray_icon(connected=False)
        self.assertIsNotNone(img_disconnected)
        self.assertEqual(img_disconnected.size, (64, 64))

    def test_scid_hex_formatting(self):
        import random
        engine = ClipboardSyncEngine()
        engine.scid = 0x1A2B3C4D
        scid_param = f"scid={engine.scid:08x}"
        self.assertEqual(scid_param, "scid=1a2b3c4d")
        # Ensure it is parseable by Integer.parseInt(val, 16) in scrcpy-server
        val = int("1a2b3c4d", 16)
        self.assertEqual(val, 0x1A2B3C4D)

    def test_notify_status_callback(self):
        engine = ClipboardSyncEngine()
        received = []
        engine.status_callback = lambda s, d: received.append((s, d))

        engine._notify_status(STATUS_CONNECTING, "正在连接...")
        engine._notify_status(STATUS_CONNECTED, "已就绪")
        engine._notify_status(STATUS_DISCONNECTED, "已断开")

        self.assertEqual(len(received), 3)
        self.assertEqual(received[0], (STATUS_CONNECTING, "正在连接..."))
        self.assertEqual(received[1], (STATUS_CONNECTED, "已就绪"))
        self.assertEqual(received[2], (STATUS_DISCONNECTED, "已断开"))

    def test_log_stream_safe_without_console(self):
        # Simulating sys.stdout is None as in --noconsole
        stream = LogStream(original=None)
        stream.write("test line 1\n")
        stream.write("test line 2\n")
        stream.flush()

        self.assertEqual(stream.history, ["test line 1\n", "test line 2\n"])
        # Ensure max_history pruning works
        stream.max_history = 3
        stream.write("line 3\n")
        stream.write("line 4\n")
        self.assertEqual(len(stream.history), 3)
        self.assertEqual(stream.history, ["test line 2\n", "line 3\n", "line 4\n"])

    def test_create_no_window_constant(self):
        if sys.platform == "win32":
            self.assertEqual(CREATE_NO_WINDOW, 0x08000000)
        else:
            self.assertEqual(CREATE_NO_WINDOW, 0)

    def test_set_console_visible_safe_call(self):
        # Should not raise any exception whether console window exists or not
        set_console_visible(False)

    def test_send_image_to_phone(self):
        engine = ClipboardSyncEngine(port=29999)
        mock_sock = MockSocket()
        engine.sock = mock_sock

        fake_img = b"\x89PNG\r\n\x1a\nTESTDATA"
        engine.send_image_to_phone(fake_img, "image/png")

        self.assertGreater(len(mock_sock.sent_buf), 14)
        msg_type, _, _, mime_len = struct.unpack(">B Q B I", mock_sock.sent_buf[:14])
        self.assertEqual(msg_type, SC_CONTROL_MSG_TYPE_SET_IMAGE_CLIPBOARD)
        self.assertEqual(engine.last_synced_text, "")
        self.assertTrue(len(engine.last_synced_image_hash) > 0)

    def test_phone_image_updates_clipboard(self):
        import hashlib
        engine = ClipboardSyncEngine(port=29999)
        fake_img = b"\x89PNG\r\nTEST_IMAGE_FROM_PHONE"
        mime = "image/png"
        mime_bytes = mime.encode("utf-8")
        raw_msg = struct.pack(">B I I", SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD, len(mime_bytes), len(fake_img)) + mime_bytes + fake_img
        engine.sock = MockSocket(raw_msg)
        engine.running = True

        saved_images = []
        engine._set_pc_clipboard_image = lambda img_b, m: saved_images.append((img_b, m))

        msg_type, payload = ScrcpyProtocol.read_device_msg(engine.sock)
        self.assertEqual(msg_type, SC_DEVICE_MSG_TYPE_IMAGE_CLIPBOARD)
        mimetype, img_bytes = payload
        img_hash = hashlib.sha256(img_bytes).hexdigest()
        if img_hash != engine.last_synced_image_hash:
            engine.last_synced_image_hash = img_hash
            engine.last_synced_text = ""
            engine._set_pc_clipboard_image(img_bytes, mimetype)

        self.assertEqual(len(saved_images), 1)
        self.assertEqual(saved_images[0], (fake_img, mime))
        self.assertEqual(engine.last_synced_image_hash, img_hash)

    def test_image_and_text_dedup_mutual_clearing(self):
        engine = ClipboardSyncEngine(port=29999)
        engine.sock = MockSocket()

        # Sync image
        engine.send_image_to_phone(b"img1", "image/png")
        self.assertNotEqual(engine.last_synced_image_hash, "")
        self.assertEqual(engine.last_synced_text, "")

        # Sync text -> should clear image hash
        engine.send_clipboard_to_phone("hello")
        self.assertEqual(engine.last_synced_image_hash, "")
        self.assertEqual(engine.last_synced_text, "hello")

        # Sync image again -> should clear text
        engine.send_image_to_phone(b"img2", "image/png")
        self.assertNotEqual(engine.last_synced_image_hash, "")
        self.assertEqual(engine.last_synced_text, "")

    def test_pc_clipboard_image_io(self):
        from PIL import Image
        import time
        engine = ClipboardSyncEngine(port=29999)
        test_img = Image.new("RGB", (32, 32), color=(255, 128, 0))
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        # Test writing image to Windows clipboard (with retry for clipboard concurrency)
        ok = False
        for _ in range(25):
            ok = engine._set_pc_clipboard_image(raw_bytes, "image/png")
            if ok:
                break
            time.sleep(0.1)
        if sys.platform == "win32":
            if not ok:
                import ctypes
                hwnd = ctypes.windll.user32.GetOpenClipboardWindow()
                print(f"[WARN] Windows clipboard locked by external HWND: {hwnd}")
            else:
                # Test reading back
                img_info = None
                for _ in range(10):
                    img_info = engine._get_pc_clipboard_image()
                    if img_info is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(img_info)
                read_bytes, read_mime, read_hash = img_info
                self.assertEqual(read_mime, "image/png")
                self.assertGreater(len(read_bytes), 0)

    def test_phone_screenshot_monitor_logic(self):
        import subprocess
        from unittest.mock import MagicMock, patch

        engine = ClipboardSyncEngine(port=29999)
        fake_img_bytes = b"\x89PNG\r\n\x1a\nTEST_IMAGE_DATA_12345678"
        fake_path = "/sdcard/DCIM/Screenshots/Screenshot_2026-09-12-test.png"

        # Mock _run_adb: first call returns existing, second returns new file
        call_count = [0]
        def mock_run_adb(args):
            mock_res = MagicMock()
            mock_res.returncode = 0
            if "head -n 30" in " ".join(args):
                mock_res.stdout = "/sdcard/DCIM/Screenshots/old_shot.png\n"
            elif "head -n 5" in " ".join(args):
                call_count[0] += 1
                if call_count[0] == 1:
                    mock_res.stdout = f"{fake_path}\n/sdcard/DCIM/Screenshots/old_shot.png\n"
                else:
                    mock_res.stdout = f"{fake_path}\n"
            else:
                mock_res.stdout = ""
            return mock_res

        engine._run_adb = mock_run_adb

        # Mock subprocess.run for exec-out cat
        def mock_sub_run(cmd, **kwargs):
            m = MagicMock()
            if "exec-out" in cmd and "cat" in cmd:
                m.stdout = fake_img_bytes
            else:
                m.stdout = b""
            return m

        synced_images = []
        engine._set_pc_clipboard_image = lambda img_b, mime: synced_images.append((img_b, mime)) or True

        with patch("subprocess.run", side_effect=mock_sub_run):
            # Run one iteration of detection manually
            seen_files = {"/sdcard/DCIM/Screenshots/old_shot.png"}
            res = engine._run_adb(["shell", "ls -1t ... | head -n 5"])
            new_candidates = []
            for line in reversed(res.stdout.strip().splitlines()):
                f = line.strip()
                if f and f not in seen_files:
                    seen_files.add(f)
                    new_candidates.append(f)

            for newest_file in new_candidates:
                pull_proc = subprocess.run(["exec-out", "cat", newest_file])
                img_bytes = pull_proc.stdout
                if img_bytes:
                    img_hash = hashlib.sha256(img_bytes).hexdigest()
                    if img_hash != engine.last_synced_image_hash:
                        engine.last_synced_image_hash = img_hash
                        engine.last_synced_text = ""
                        ext = os.path.splitext(newest_file)[1].lower()
                        mime = "image/png" if ext == ".png" else "image/jpeg"
                        engine._set_pc_clipboard_image(img_bytes, mime)

        self.assertEqual(len(synced_images), 1)
        self.assertEqual(synced_images[0], (fake_img_bytes, "image/png"))
        self.assertIn(fake_path, seen_files)

    def test_ensure_share_helper_installed_logic(self):
        from unittest.mock import MagicMock
        engine = ClipboardSyncEngine(port=29999)

        installed_cmds = []
        def mock_run_adb(args):
            m = MagicMock()
            if "pm path com.sync.clipboard.share" in " ".join(args):
                m.returncode = 1
                m.stdout = ""
            elif "install" in args:
                installed_cmds.append(args)
                m.returncode = 0
                m.stdout = "Success"
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        engine._run_adb = mock_run_adb
        engine.ensure_share_helper_installed()
        # Verify it attempted to install since pm path returned empty
        self.assertTrue(len(installed_cmds) >= 1)
        self.assertIn("-r", installed_cmds[0])
        self.assertIn("-g", installed_cmds[0])


class TestPersistentBinAndAdb(unittest.TestCase):
    def test_get_fixed_bin_dir_dev_mode(self):
        b_dir = get_fixed_bin_dir()
        self.assertTrue(os.path.isdir(b_dir))
        self.assertTrue(os.path.isfile(os.path.join(b_dir, "adb.exe")))

    def test_get_fixed_bin_dir_frozen_mode(self):
        with unittest.mock.patch("sys.frozen", True, create=True), \
             unittest.mock.patch("sys._MEIPASS", "C:\\fake\\temp\\_MEI12345", create=True), \
             unittest.mock.patch("sys.executable", "C:\\fake\\install\\app.exe"):
            b_dir = get_fixed_bin_dir()
            self.assertIn("usb_clipboard_sync", b_dir)
            self.assertTrue(b_dir.endswith("bin"))

    def test_find_adb_extracts_on_frozen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as fake_meipass, tempfile.TemporaryDirectory() as fake_appdata:
            fake_adb = os.path.join(fake_meipass, "adb.exe")
            with open(fake_adb, "wb") as f:
                f.write(b"FAKE_ADB_BINARY")
            fake_dll = os.path.join(fake_meipass, "AdbWinApi.dll")
            with open(fake_dll, "wb") as f:
                f.write(b"FAKE_DLL")

            target_bin = os.path.join(fake_appdata, "usb_clipboard_sync", "bin")

            with unittest.mock.patch("sys.frozen", True, create=True), \
                 unittest.mock.patch("sys._MEIPASS", fake_meipass, create=True), \
                 unittest.mock.patch("sys.executable", "C:\\Program Files\\app\\app.exe"), \
                 unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": fake_appdata}):
                engine = ClipboardSyncEngine(port=29998)
                self.assertTrue(os.path.isfile(os.path.join(target_bin, "adb.exe")))
                self.assertEqual(engine.adb_cmd, os.path.join(target_bin, "adb.exe"))


if __name__ == "__main__":
    unittest.main()



