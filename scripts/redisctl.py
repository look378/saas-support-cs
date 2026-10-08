"""Redis 进程管理（免安装 zip 版）。

与 PostgreSQL 相同，Windows 上不注册服务、不使用管理员权限，直接以普通进程运行。
Redis 5.x for Windows 没有原生后台模式（daemonize 不生效），因此由本脚本以
独立进程启动并记录 PID。

用法：
    python scripts/redisctl.py start
    python scripts/redisctl.py status
    python scripts/redisctl.py stop
    python scripts/redisctl.py restart
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def find_toolchain() -> Path:
    """向上查找工具链目录（不依赖固定层级）。"""
    for parent in [ROOT, *ROOT.parents]:
        cand = parent / ".toolchain"
        if (cand / "redis" / "redis-server.exe").exists():
            return cand
    raise SystemExit("未找到工具链目录 .toolchain（应包含 redis\\redis-server.exe）")


def ports_of(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def pid_file() -> Path:
    p = ROOT / "data" / "run"
    p.mkdir(parents=True, exist_ok=True)
    return p / "redis.pid"


def resolve_data_dir() -> Path:
    """解析 Redis 数据目录。

    实测约束：Windows 版 Redis 5.x 在非 ASCII 路径上会启动失败——
        Can't chdir to '...': Illegal byte sequence
    因此当工程路径含中文时，自动把数据目录落到同级的纯 ASCII 路径下，
    配置文件仍保留在工程内（Redis 读配置走的是普通文件读取，不受影响）。
    """
    preferred = ROOT / "data" / "redis"
    is_ascii = all(ord(ch) < 128 for ch in str(ROOT))
    if is_ascii:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred

    # 回退：工作区根目录下的 ASCII 目录
    for parent in ROOT.parents:
        cand = parent / ".toolchain" / "redis-data"
        if parent.name and all(ord(ch) < 128 for ch in str(parent)):
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    preferred.mkdir(parents=True, exist_ok=True)
    return preferred


def write_conf(conf_path: Path, port: int, data_dir: Path) -> None:
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    # 必须用 UTF-8：工程路径可能包含非 ASCII 字符（如中文目录名），
    # ascii 编码会直接抛 UnicodeEncodeError 导致服务起不来。
    conf_path.write_text(
        "\n".join(
            [
                "bind 127.0.0.1",
                f"port {port}",
                f"dir {data_dir.as_posix()}",
                "appendonly yes",
                "appendfsync everysec",
                "save 900 1",
                "daemonize no",
                'logfile ""',
            ]
        ),
        encoding="utf-8",
    )


def read_pid() -> int | None:
    f = pid_file()
    if not f.exists():
        return None
    raw = f.read_text(encoding="ascii").strip()
    return int(raw) if raw.isdigit() else None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    import subprocess as sp

    out = sp.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    return "redis-server" in out.lower()


def cmd_start(host: str, port: int) -> int:
    if ports_of(host, port):
        print(f"  [redis] 端口 {port} 已在监听，跳过启动")
        return 0
    toolchain = find_toolchain()
    exe = toolchain / "redis" / "redis-server.exe"
    conf = ROOT / "data" / "redis" / "redis.conf"
    data_dir = resolve_data_dir()
    write_conf(conf, port, data_dir)
    print(f"  [redis] 数据目录: {data_dir}")

    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "redis.log", "ab")
    err = open(logs / "redis.log.err", "ab")

    # DETACHED_PROCESS 让 Redis 独立于当前进程，避免随父进程退出被回收
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [str(exe), str(conf)],
        cwd=str(exe.parent),
        stdout=out,
        stderr=err,
        creationflags=flags,
    )
    pid_file().write_text(str(proc.pid), encoding="ascii")

    deadline = time.time() + 20
    while time.time() < deadline:
        if ports_of(host, port):
            print(f"  [redis] 已启动 PID={proc.pid} 端口={port}")
            return 0
        time.sleep(0.3)
    print(f"  [redis] 启动失败，见 {logs / 'redis.log.err'}")
    return 1


def cmd_status(host: str, port: int) -> int:
    running = ports_of(host, port)
    pid = read_pid()
    print(f"  [redis] 端口 {port} 监听={running} PID={pid}")
    return 0 if running else 1


def cmd_stop(host: str, port: int) -> int:
    pid = read_pid()
    if not ports_of(host, port) and not alive(pid):
        print("  [redis] 未在运行")
        pid_file().unlink(missing_ok=True)
        return 0
    # 优先用 redis-cli SHUTDOWN NOSAVE 优雅停止
    toolchain = find_toolchain()
    cli = toolchain / "redis" / "redis-cli.exe"
    if cli.exists():
        subprocess.run([str(cli), "-h", host, "-p", str(port), "shutdown", "nosave"],
                       capture_output=True, text=True)
        time.sleep(0.8)
    if ports_of(host, port) and pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    pid_file().unlink(missing_ok=True)
    print("  [redis] 已停止")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Redis 进程管理")
    ap.add_argument("action", choices=["start", "stop", "status", "restart"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    args = ap.parse_args()

    if args.action == "start":
        return cmd_start(args.host, args.port)
    if args.action == "status":
        return cmd_status(args.host, args.port)
    if args.action == "stop":
        return cmd_stop(args.host, args.port)
    cmd_stop(args.host, args.port)
    return cmd_start(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
