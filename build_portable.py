"""
USB 剪贴板双向同步 - 离线便携版自动化构建脚本
功能：
1. 使用 PyInstaller 将 python 运行时、依赖库、adb 二进制工具及 scrcpy-server 打包为独立单文件可执行程序。
2. 生成便携目录结构与详细《使用说明.txt》。
3. 压缩生成即开即用的《USB剪贴板双向同步-便携版.zip》。
"""

import os
import sys
import shutil
import zipfile
import subprocess
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BASE_DIR, "bin")
DIST_DIR = os.path.join(BASE_DIR, "dist")
BUILD_DIR = os.path.join(BASE_DIR, "build")
PORTABLE_OUT_DIR = os.path.join(DIST_DIR, "USB剪贴板双向同步-便携版")

REQUIRED_BINARIES = ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "scrcpy-server", "ShareToPC.apk"]


def check_binaries():
    print("[*] 检查必要底层二进制组件...")
    missing = []
    for fname in REQUIRED_BINARIES:
        path = os.path.join(BIN_DIR, fname)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            missing.append(fname)
    if missing:
        print(f"[-] 缺少必要组件: {missing}，正在尝试从系统提取...")
        # 尝试从 WinGet 路径提取
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        scrcpy_pkg = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        found = False
        if os.path.exists(scrcpy_pkg):
            for root, dirs, files in os.walk(scrcpy_pkg):
                if all(f in files for f in REQUIRED_BINARIES):
                    os.makedirs(BIN_DIR, exist_ok=True)
                    for f in REQUIRED_BINARIES:
                        shutil.copy2(os.path.join(root, f), os.path.join(BIN_DIR, f))
                    found = True
                    break
        if not found:
            raise FileNotFoundError(f"无法定位必要二进制组件: {missing}")
    print("[+] 全部底层组件检查通过！")


def run_pyinstaller():
    print("[*] 开始使用 PyInstaller 构建独立可执行程序...")
    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--collect-all=pystray",
        "--collect-all=PIL",
        "--name",
        "usb_clipboard_sync_bin",
        f"--add-data={os.path.join(BIN_DIR, 'adb.exe')};.",
        f"--add-data={os.path.join(BIN_DIR, 'AdbWinApi.dll')};.",
        f"--add-data={os.path.join(BIN_DIR, 'AdbWinUsbApi.dll')};.",
        f"--add-data={os.path.join(BIN_DIR, 'scrcpy-server')};.",
        f"--add-data={os.path.join(BIN_DIR, 'ShareToPC.apk')};.",
        os.path.join(BASE_DIR, "usb_clipboard_sync.py"),
    ]

    print("执行命令:", " ".join(pyinstaller_cmd))
    res = subprocess.run(pyinstaller_cmd, cwd=BASE_DIR)
    if res.returncode != 0:
        raise RuntimeError(f"PyInstaller 构建失败，退出码: {res.returncode}")

    # 重命名生成物为标准的中文名称
    raw_exe = os.path.join(DIST_DIR, "usb_clipboard_sync_bin.exe")
    named_exe = os.path.join(DIST_DIR, "USB剪贴板双向同步.exe")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "USB剪贴板双向同步.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(named_exe):
        try:
            os.remove(named_exe)
        except Exception:
            time.sleep(0.5)
            os.remove(named_exe)
    os.rename(raw_exe, named_exe)
    print(f"[+] PyInstaller 构建完成并重命名为: {named_exe}")


def assemble_portable_package():
    print("[*] 正在组装便携版发行目录...")
    if os.path.exists(PORTABLE_OUT_DIR):
        shutil.rmtree(PORTABLE_OUT_DIR)
    os.makedirs(PORTABLE_OUT_DIR, exist_ok=True)

    # 1. 复制生成的单个可执行程序与快捷组件 APK
    src_exe = os.path.join(DIST_DIR, "USB剪贴板双向同步.exe")
    dst_exe = os.path.join(PORTABLE_OUT_DIR, "USB剪贴板双向同步.exe")
    shutil.copy2(src_exe, dst_exe)

    src_apk = os.path.join(BIN_DIR, "ShareToPC.apk")
    dst_apk = os.path.join(PORTABLE_OUT_DIR, "ShareToPC.apk")
    if os.path.exists(src_apk):
        shutil.copy2(src_apk, dst_apk)

    # 2. 生成使用说明书
    readme_content = """========================================================================
           USB 剪贴板双向同步工具 (PC <-> Android) 便携免安装版
========================================================================

【软件特点】
1. 纯后台托盘驻留：
   - 打开后任务栏不产生任何窗口或按钮，完全静默驻留在右下角系统托盘。
   - 双态状态指示：绿色（已连接）、橙色（等待连接/断开）。
   - 如需查看运行日志，右键托盘菜单选择【显示控制台窗口】即可，关闭后自动恢复纯托盘模式。
2. 彻底离线免安装：自带全部 Python 运行时、ADB 驱动与通信服务，随拷随用。
3. 免安装手机 App：利用 USB 调试通道底层通信，手机无需安装任何第三方 APK。
4. 文字与图片双向无感实时同步：
   - 【电脑 -> 手机】：电脑端复制文字 / 网页图片 / 截图 / 复制本地图片文件 -> 手机端长按即可直接粘贴！
   - 【手机 -> 电脑】：
     * 方法一（截屏秒同步）：手机上任意界面截屏（如三指下滑截屏或按键截屏），电脑剪贴板立刻自动获得该截图，在电脑直接 Ctrl + V 粘贴！
     * 方法二（相册/文件管理一键发送原图）：在小米等相册或文件管理器中，选中照片/文件 -> 点击【发送】(或分享) -> 选择【发送到电脑】快捷入口，原图瞬间自动写入电脑剪贴板！
     * 方法三（主体抠图复制）：小米澎湃 OS 相册中长按图片主体提取浮起后点击【复制】，电脑端即可秒级 Ctrl + V 粘贴！
     * 方法四（文本无缝同步）：手机端长按选中文字点击【复制】，电脑端 Ctrl + V 即可粘贴！
5. 无感断线全自动重连：
   - 拔掉手机数据线或手机休眠断开后，无需重启程序，程序在后台持续守候。
   - 再次插上数据线后，系统秒级自动识别并建立同步通道。
6. 智能防回环：电脑与手机复制不会产生死循环，低资源消耗。

【使用步骤】
1. 使用 USB 数据线连接安卓手机与电脑。
2. 手机端开启【开发者选项】与【USB 调试】（并在弹出“允许 USB 调试吗”时勾选允许）。
3. 直接双击运行【USB剪贴板双向同步.exe】。
4. 程序启动后直接驻留在右下角托盘（任务栏无任何窗口），弹出绿色连接就绪气泡，此时即可自由复制文字与图片。
5. 如需退出，右键点击右下角托盘图标选择【退出程序】即可。

【进阶运行参数】
- USB剪贴板双向同步.exe --show-console  : 启动时显示控制台日志窗口
- USB剪贴板双向同步.exe --no-tray       : 纯控制台模式运行，不加载系统托盘

【常见问题】
Q: 打开后任务栏为什么没有任何窗口？
A: 这是专门设计的纯托盘静默模式！程序已驻留在屏幕右下角的小箭头托盘区域中（带双态绿/橙图标），不占用任务栏。

Q: 小米手机相册或者文件管理器里没有单纯的“复制”按钮，怎么复制照片到电脑？
A: 推荐以下三种极简方式：
   1. 点击相册照片左下角【发送/分享】 -> 在分享面板中点击【发送到电脑】，程序会自动将原图写入电脑剪贴板，电脑直接 Ctrl + V！
   2. 手机三指下滑截屏，电脑剪贴板立刻自动获得该截图，电脑直接 Ctrl + V！
   3. 在相册照片主体上长按 1 秒提取发光主体 -> 点击弹出菜单的【复制】，电脑直接 Ctrl + V！

Q: 拔出手机后重新插入能继续用吗？
A: 可以！程序内置了全自动重连机制，插上后 1~2 秒内自动重新连接并弹出气泡提示。

Q: 提示“等待安卓手机通过 USB 数据线连接”？
A: 请检查 USB 线是否插好，手机下拉菜单是否选择“传输文件”或已开启“USB 调试”。
"""
    readme_path = os.path.join(PORTABLE_OUT_DIR, "使用说明.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)

    print(f"[+] 便携版目录生成完毕: {PORTABLE_OUT_DIR}")

    # 3. 压缩为 ZIP 包
    zip_path = os.path.join(DIST_DIR, "USB剪贴板双向同步-便携版.zip")
    print(f"[*] 正在打包压缩为: {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(PORTABLE_OUT_DIR):
            for file in files:
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, os.path.dirname(PORTABLE_OUT_DIR))
                zf.write(full_p, rel_p)

    print(f"[+] 便携版 ZIP 压缩包生成完毕: {zip_path} ({os.path.getsize(zip_path)} 字节)")


def main():
    print("==================================================")
    print("      开始构建 USB 剪贴板双向同步 离线便携版")
    print("==================================================")
    check_binaries()
    run_pyinstaller()
    assemble_portable_package()

    # 清理临时 build 缓存与 spec
    if os.path.exists(BUILD_DIR):
        try:
            shutil.rmtree(BUILD_DIR)
        except Exception:
            pass
    for f in os.listdir(BASE_DIR):
        if f.endswith(".spec"):
            try:
                os.remove(os.path.join(BASE_DIR, f))
            except Exception:
                pass

    print("\n==================================================")
    print("         构建全部完成！成果位于 dist/ 目录")
    print("==================================================")


if __name__ == "__main__":
    main()
