"""BenchManager 单元测试：名额、空闲回收、续期规则、在途写保护、已回收墓碑。"""

import threading
import time
import unittest

from bench import (
    BenchLimitError,
    BenchManager,
    BenchNotFound,
    BenchReclaimed,
)
from switch import Switch

M1, M2 = "02:00:00:00:00:01", "02:00:00:00:00:02"


class TestBenchLifecycle(unittest.TestCase):
    def setUp(self):
        # 不启后台回收线程，全部用手动/惰性回收，时间由短超时驱动
        self.mgr = BenchManager(max_benches=3, idle_timeout=0.05, reaper_interval=999)

    def test_create_starts_from_blank_switch_and_isolated(self):
        b1 = self.mgr.create_bench()
        b2 = self.mgr.create_bench()
        self.assertIsNot(b1.switch, b2.switch)
        b1.switch.send_frame(1, M1, M2)
        # 台 2 仍是空白：VLAN/MAC 表/积压完全独立
        self.assertEqual(b2.switch.mac_tables, {})
        self.assertTrue(all(p.received_total == 0 for p in b2.switch.ports.values()))
        self.assertIn(M1, b1.switch.mac_tables[1])

    def test_max_three_benches_then_limit_error(self):
        tokens = [self.mgr.create_bench().token for _ in range(3)]
        self.assertEqual(self.mgr.active_count(), 3)
        with self.assertRaises(BenchLimitError):
            self.mgr.create_bench()
        # 回收一台后名额腾出，可再开
        self.mgr.force_reap(tokens[0])
        b = self.mgr.create_bench()
        self.assertEqual(self.mgr.active_count(), 3)
        self.assertNotIn(b.token, tokens)

    def test_idle_reclaim_after_timeout_and_tombstone(self):
        b = self.mgr.create_bench()
        token = b.token
        time.sleep(0.06)
        reaped = self.mgr.reap_idle()
        self.assertEqual(reaped, [token])
        self.assertEqual(self.mgr.active_count(), 0)
        # 旧链接访问 -> 已回收；补发写请求 -> 同样已回收，不会命中新台
        with self.assertRaises(BenchReclaimed):
            self.mgr.access(token)
        with self.assertRaises(BenchReclaimed):
            self.mgr.begin_write(token)

    def test_read_access_does_not_renew_but_lazy_reaps(self):
        mgr = BenchManager(idle_timeout=0.5, reaper_interval=999)
        b = mgr.create_bench()
        for _ in range(4):                          # 反复查看，不能续命
            time.sleep(0.1)                         # 累计 0.4s，仍未到期
            self.assertEqual(mgr.access(b.token), b)
        time.sleep(0.2)                             # 累计 0.6s > 0.5s
        with self.assertRaises(BenchReclaimed):    # 下一次读触发惰性回收
            mgr.access(b.token)

    def test_successful_write_renews_idle_timer(self):
        b = self.mgr.create_bench()
        for _ in range(4):
            time.sleep(0.03)                        # 累计超过超时，但每次都有成功写
            bench = self.mgr.begin_write(b.token)
            bench.switch.send_frame(1, M1, M2)
            self.mgr.end_write(bench, True)
        self.assertEqual(self.mgr.active_count(), 1) # 一直被续期，未回收
        self.mgr.reap_idle()
        self.assertEqual(self.mgr.active_count(), 1) # 刚写完立即检查也不会回收

    def test_failed_write_does_not_renew(self):
        b = self.mgr.create_bench()
        time.sleep(0.06)                            # 已空闲超时
        bench = self.mgr.begin_write(b.token)      # 在途登记时不做回收
        with self.assertRaises(ValueError):
            bench.switch.send_frame(1, "bad-mac", M2)
        self.mgr.end_write(bench, False)            # 失败不续期
        reaped = self.mgr.reap_idle()
        self.assertEqual(reaped, [b.token])

    def test_inflight_write_blocks_reclamation_but_keeps_slot(self):
        mgr = BenchManager(max_benches=1, idle_timeout=0.05, reaper_interval=999)
        b = mgr.create_bench()
        done = threading.Event()

        def slow_write():
            bench = mgr.begin_write(b.token)
            with bench.lock:
                done.set()
                time.sleep(0.15)                   # 写操作期间空闲早已超时
                bench.switch.send_frame(1, M1, M2)
            mgr.end_write(bench, True)

        t = threading.Thread(target=slow_write)
        t.start()
        done.wait(2)
        time.sleep(0.08)                            # 此时已超时且写仍在途
        self.assertEqual(mgr.reap_idle(), [])       # 在途写结束前不回收
        self.assertEqual(mgr.active_count(), 1)     # 仍占名额
        with self.assertRaises(BenchLimitError):    # 名额被占，开不了新台
            mgr.create_bench()
        t.join()
        # 写成功结束后续期，台仍可用
        self.assertEqual(mgr.access(b.token), b)
        self.assertIn(M1, b.switch.mac_tables[1])
        time.sleep(0.06)
        self.assertEqual(mgr.reap_idle(), [b.token])  # 结束后下一轮才回收

    def test_reclaimed_token_never_resolves_to_new_bench(self):
        old = self.mgr.create_bench()
        self.mgr.force_reap(old.token)
        with self.assertRaises(BenchReclaimed):
            self.mgr.access(old.token)
        new = self.mgr.create_bench()               # 新台占了腾出的名额
        self.assertNotEqual(old.token, new.token)
        with self.assertRaises(BenchReclaimed):     # 旧 token 仍是「已回收」
            self.mgr.access(old.token)
        self.assertEqual(self.mgr.access(new.token), new)

    def test_unknown_token_is_not_found(self):
        with self.assertRaises(BenchNotFound):
            self.mgr.access("never-existed-token")

    def test_reset_only_affects_own_bench(self):
        b1 = self.mgr.create_bench()
        b2 = self.mgr.create_bench()
        b1.switch.send_frame(1, M1, M2)
        b2.switch.send_frame(2, M2, M1)
        b1.switch.reset()
        self.assertEqual(b1.switch.mac_tables, {})
        self.assertIn(M2, b2.switch.mac_tables[1])   # 其他台不受复位影响

    def test_concurrent_writes_on_different_benches_run_on_separate_locks(self):
        b1 = self.mgr.create_bench()
        b2 = self.mgr.create_bench()
        errors = []

        def hammer(bench):
            try:
                for _ in range(100):
                    wb = self.mgr.begin_write(bench.token)
                    with wb.lock:
                        wb.switch.send_frame(1, M1, M2)
                    self.mgr.end_write(wb, True)
            except Exception as e:                   # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=hammer, args=(b,)) for b in (b1, b2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 每帧从端口1入、未知目的泛洪到其余4口；两台各 100 帧、各 400 次接收
        total_received = sum(p.received_total for p in b1.switch.ports.values()) + \
            sum(p.received_total for p in b2.switch.ports.values())
        self.assertEqual(total_received, 800)
        self.assertEqual(b1.switch.ports[2].received_total, 100)
        self.assertEqual(b2.switch.ports[2].received_total, 100)
        self.assertTrue(b1.switch.check_invariants())
        self.assertTrue(b2.switch.check_invariants())


if __name__ == "__main__":
    unittest.main()
