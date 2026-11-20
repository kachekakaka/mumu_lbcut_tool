"""有界的单客户端命令：不清理进程树，不接触共享 ADB server。"""
import os
import subprocess
import time
from src.mvp.windows_job import close_process_handle


def read_windows_output(proc, timeout):
    """单读者轮询匿名管道，继承管道的其他进程不会卡住本次读取。"""
    import ctypes
    from ctypes import wintypes as w
    import msvcrt

    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, w.LPVOID, ctypes.POINTER(w.DWORD), w.LPVOID]
    peek.restype = w.BOOL
    streams = (proc.stdout, proc.stderr)
    handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in streams]
    outputs = [bytearray(), bytearray()]
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        exited = proc.poll() is not None
        progress = False
        for stream, handle, output in zip(streams, handles, outputs):
            available = w.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(available), None):
                code = ctypes.get_last_error()
                if code not in (109, 232, 233):  # 管道关闭或断开
                    raise ctypes.WinError(code)
            elif available.value:
                output.extend(stream.read(min(65536, available.value)))
                progress = True
        if not progress and exited:
            return bytes(outputs[0]), bytes(outputs[1])
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout, bytes(outputs[0]), bytes(outputs[1]))
        if not progress:
            time.sleep(max(0, min(.01, deadline - time.monotonic())) if deadline is not None else .01)


def decode_output(value):
    return (value or b"").decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def run_client(cmd, *, timeout, cwd=None, env=None, binary=False):
    if timeout <= 0:
        raise TimeoutError('命令阶段预算已耗尽')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                            cwd=cwd, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        out, err = read_windows_output(proc, timeout) if os.name == 'nt' else proc.communicate(timeout=timeout)
        if not binary:
            out, err = decode_output(out), decode_output(err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=.2)
        raise
    finally:
        proc.stdout.close()
        proc.stderr.close()
        close_process_handle(proc)
