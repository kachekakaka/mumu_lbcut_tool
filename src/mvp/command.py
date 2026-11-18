"""有界的单客户端命令：不清理进程树，不接触共享 ADB server。"""
import os
import subprocess


def run_client(cmd, *, timeout, cwd=None, env=None, binary=False):
    if timeout <= 0:
        raise TimeoutError('命令阶段预算已耗尽')
    options = {} if binary else dict(text=True, encoding='utf-8', errors='replace')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                            **options)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # Windows communicate 的守护读取线程可能被继承管道的常驻进程占住。
            # 此时 close() 会等待同一缓冲锁，不能把有界超时变回无限等待。
            for name in ('stdout', 'stderr'):
                reader = getattr(proc, name + '_thread', None)
                if reader is None or not reader.is_alive():
                    getattr(proc, name).close()
        raise
