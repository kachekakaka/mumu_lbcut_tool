"""只向指定测试宿主的新控制台发送 Ctrl+C；发送器自身忽略信号。"""
import ctypes
import sys
import time

api = ctypes.WinDLL('kernel32', use_last_error=True)
api.FreeConsole()
if not api.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
api.SetConsoleCtrlHandler(None, True)
if not api.GenerateConsoleCtrlEvent(0, 0):
    raise ctypes.WinError(ctypes.get_last_error())
time.sleep(.2)
api.FreeConsole()
