"""多实验台管理：名额分配、空闲回收与在途写保护。

每个实训室小组占用一个 Bench（内含一台从零开始的五端口 Switch），
BenchManager 负责：

- 最多同时保留 MAX_BENCHES 台，满额开台抛 BenchLimitError；
- 每台独立维护 VLAN / MAC 表 / 积压，复位只作用于本台；
- 空闲回收：建台或上一次「成功结束的写操作」后 idle_timeout 秒未再发生
  成功写操作即回收；GET 状态/页面只看不动，不续期；
- 在途写保护：写操作开始时 inflight 计数 +1，结束前即使空闲到期也不回收，
  该台仍可正常使用并继续占名额，写操作结束后下一轮回收周期才处理；
- 已回收的台保留墓碑（tombstone），旧链接/补发请求一律得到「已回收」应答，
  不会被解析到之后新开的台。

线程模型：manager 一把 RLock 保护注册表/名额/墓碑；每个 Bench 自带一把锁，
写流程为「manager 登记在途 -> 取 bench 锁执行 -> manager 记成功写时间」，
不同台的写操作互不阻塞（每台独立锁）。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque

from switch import Switch

MAX_BENCHES = 3
DEFAULT_IDLE_TIMEOUT = 300.0          # 空闲 5 分钟回收
TOMBSTONE_LIMIT = 200                 # 已回收台墓碑上限，防止无限增长


class BenchError(Exception):
    """台管理相关错误的基类。"""


class BenchNotFound(BenchError):
    """token 从未对应过任何台（未知链接）。"""


class BenchReclaimed(BenchError):
    """token 对应的台已被回收，旧链接/补发请求一律拒绝。"""


class BenchLimitError(BenchError):
    """在役台已达 MAX_BENCHES，无法再开新台。"""


class Bench:
    """一台独立实验台：自带交换机、锁与生命周期时间戳。"""

    def __init__(self, token, switch, now):
        self.token = token
        self.switch = switch
        self.lock = threading.RLock()          # 串行化本台写操作
        self.created_at = now                  # 墙钟时间，仅用于页面展示
        self.last_write = now                  # 单调时钟，空闲判定基准
        self.inflight = 0                      # 在途写操作计数

    def info(self):
        return {
            "token": self.token,
            "created_at": time.strftime("%H:%M:%S", time.localtime(self.created_at)),
            "idle_seconds": max(0, int(time.monotonic() - self.last_write)),
            "inflight": self.inflight,
        }


class BenchManager:
    def __init__(self, max_benches=MAX_BENCHES, idle_timeout=DEFAULT_IDLE_TIMEOUT,
                 reaper_interval=10.0):
        self._lock = threading.RLock()
        self._benches: dict[str, Bench] = {}
        self._tombstones: deque[str] = deque(maxlen=TOMBSTONE_LIMIT)
        self.max_benches = max_benches
        self.idle_timeout = idle_timeout
        self.reaper_interval = reaper_interval
        self._stop = threading.Event()
        self._thread = None

    # ---------- 生命周期 ----------
    def start_reaper(self):
        """启动后台回收线程（幂等）。"""
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._reap_loop, name="bench-reaper", daemon=True)
            self._thread.start()

    def stop_reaper(self):
        self._stop.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    def _reap_loop(self):
        while not self._stop.wait(self.reaper_interval):
            try:
                self.reap_idle()
            except Exception:                       # noqa: BLE001 回收线程不能崩
                pass

    # ---------- 开台 ----------
    def create_bench(self):
        """新开一台空白交换机实验台；满额抛 BenchLimitError。"""
        now_mono = time.monotonic()
        with self._lock:
            self._reap_idle_locked(now_mono)
            if len(self._benches) >= self.max_benches:
                raise BenchLimitError(
                    f"实验台已满（最多 {self.max_benches} 台），请等某台空闲回收后再开新台")
            token = uuid.uuid4().hex
            bench = Bench(token, Switch(), time.time())
            bench.last_write = now_mono
            self._benches[token] = bench
            return bench

    # ---------- 访问 ----------
    def _resolve_locked(self, token):
        bench = self._benches.get(token)
        if bench is not None:
            return bench
        if token in self._tombstones:
            raise BenchReclaimed("该实验台已被回收，无法继续操作；请回首页另开新台")
        raise BenchNotFound("实验台不存在或链接有误")

    def access(self, token):
        """读访问（页面/GET 状态）：顺带惰性回收，不续期。"""
        with self._lock:
            self._reap_idle_locked(time.monotonic())
            return self._resolve_locked(token)

    def begin_write(self, token):
        """写操作入口：登记在途并返回台；在途期间该台不会被回收。"""
        with self._lock:
            # 注意：写请求到达本身不续期，只有成功结束才续期，
            # 因此这里不做惰性回收，避免「补发请求把台续命」。
            bench = self._resolve_locked(token)
            bench.inflight += 1
            return bench

    def end_write(self, bench, success):
        """写操作出口：成功才刷新写时间戳（续期），并清零在途计数。"""
        with self._lock:
            bench.inflight = max(0, bench.inflight - 1)
            if success:
                bench.last_write = time.monotonic()

    # ---------- 回收 ----------
    def reap_idle(self):
        with self._lock:
            return self._reap_idle_locked(time.monotonic())

    def _reap_idle_locked(self, now):
        """回收空闲超时且无在途写的台，返回被回收 token 列表。"""
        reaped = []
        for token, bench in list(self._benches.items()):
            if bench.inflight > 0:
                continue                            # 已受理写操作未结束，保留并占名额
            if now - bench.last_write >= self.idle_timeout:
                del self._benches[token]
                self._tombstones.append(token)
                reaped.append(token)
        return reaped

    def force_reap(self, token):
        """测试/管理用：立即回收指定台。"""
        with self._lock:
            bench = self._benches.pop(token, None)
            if bench is None:
                self._resolve_locked(token)         # 触发对应的 NotFound/Reclaimed
                return
            self._tombstones.append(token)

    # ---------- 状态汇总 ----------
    def active_count(self):
        with self._lock:
            return len(self._benches)

    def status(self):
        """首页用：名额概况 + 在役台清单（不含交换机内部状态）。"""
        with self._lock:
            self._reap_idle_locked(time.monotonic())
            return {
                "max_benches": self.max_benches,
                "active_count": len(self._benches),
                "idle_timeout": int(self.idle_timeout),
                "benches": [b.info() for b in self._benches.values()],
            }
