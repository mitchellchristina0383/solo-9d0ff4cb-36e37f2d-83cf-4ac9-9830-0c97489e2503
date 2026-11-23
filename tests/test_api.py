"""HTTP API 层测试（Flask test client）：台管理 + 台内转发接口。"""

import unittest

from app import create_app
from bench import BenchManager

M1 = "02:00:00:00:00:01"
M2 = "02:00:00:00:00:02"


class TestApi(unittest.TestCase):
    def setUp(self):
        # 关闭后台回收线程，避免测试结束后残留线程
        self.mgr = BenchManager(idle_timeout=300, reaper_interval=999)
        self.client = create_app(self.mgr).test_client()
        r = self.client.post("/api/benches")
        self.assertEqual(r.status_code, 201)
        self.token = r.get_json()["token"]
        self.prefix = f"/api/bench/{self.token}"

    def post(self, path, payload=None):
        return self.client.post(path, json=payload)

    def get(self, path):
        return self.client.get(path)
    def test_index_and_bench_pages_served(self):
        self.assertIn("开新练习台".encode(), self.get("/").data)
        r = self.get(f"/bench/{self.token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("五端口虚拟交换机".encode(), r.data)

    def test_frame_flow_over_http(self):
        r = self.post(f"{self.prefix}/frame",
                      {"port": 1, "src": M1, "dst": M2, "payload": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["action"], "flood")
        st = self.get(f"{self.prefix}/state").get_json()
        self.assertEqual(st["mac_tables"]["1"][M1], 1)
        self.assertEqual(st["ports"][1]["received_count"], 1)
        self.assertEqual(st["bench"]["token"], self.token)

    def test_pause_release_over_http(self):
        self.post(f"{self.prefix}/port/2/pause")
        self.post(f"{self.prefix}/frame", {"port": 1, "src": M1, "dst": M2})
        st = self.get(f"{self.prefix}/state").get_json()
        self.assertEqual(st["ports"][1]["queued"], 1)
        r = self.post(f"{self.prefix}/port/2/release")
        self.assertEqual(r.get_json()["delivered"], 1)

    def test_vlan_reconfig_over_http(self):
        self.post(f"{self.prefix}/port/2/pause")
        self.post(f"{self.prefix}/frame", {"port": 2, "src": M2, "dst": M1})
        self.post(f"{self.prefix}/frame", {"port": 1, "src": M1, "dst": M2})
        r = self.post(f"{self.prefix}/port/2/vlan", {"vlan": 20})
        j = r.get_json()
        self.assertTrue(j["changed"])
        self.assertEqual(j["dropped_queue"], 1)
        self.assertEqual(j["purged_entries"], [{"vlan": 1, "mac": M2}])

    def test_bad_requests_return_400(self):
        self.assertEqual(self.post(f"{self.prefix}/frame",
                                   {"port": 1, "src": "xx", "dst": M2}).status_code, 400)
        self.assertEqual(self.post(f"{self.prefix}/frame",
                                   {"port": 1, "src": M1,
                                    "dst": "ff:ff:ff:ff:ff:ff"}).status_code, 400)
        self.assertEqual(self.post(f"{self.prefix}/port/6/pause").status_code, 400)
        self.assertEqual(self.post(f"{self.prefix}/port/1/vlan",
                                   {"vlan": 0}).status_code, 400)

    def test_reset_only_resets_own_bench(self):
        other = self.client.post("/api/benches").get_json()["token"]
        self.post(f"{self.prefix}/frame", {"port": 1, "src": M1, "dst": M2})
        self.post(f"/api/bench/{other}/frame", {"port": 1, "src": M1, "dst": M2})
        self.post(f"{self.prefix}/reset")
        st = self.get(f"{self.prefix}/state").get_json()
        self.assertEqual(st["mac_tables"], {})
        other_st = self.get(f"/api/bench/{other}/state").get_json()
        self.assertIn(M1, other_st["mac_tables"]["1"])     # 另一台不受影响

    def test_full_capacity_returns_409(self):
        self.client.post("/api/benches")                   # 第 2 台
        self.client.post("/api/benches")                   # 第 3 台（满）
        r = self.client.post("/api/benches")
        self.assertEqual(r.status_code, 409)
        self.assertIn("满", r.get_json()["error"])
        st = self.get("/api/benches").get_json()
        self.assertEqual(st["active_count"], 3)

    def test_reclaimed_bench_returns_410_on_page_and_api(self):
        token = self.token
        self.mgr.force_reap(token)
        # 旧链接访问页面
        r = self.get(f"/bench/{token}")
        self.assertEqual(r.status_code, 410)
        self.assertIn("已被回收".encode(), r.data)
        # 补发各种请求
        self.assertEqual(self.get(f"{self.prefix}/state").status_code, 410)
        self.assertEqual(self.post(f"{self.prefix}/frame",
                                   {"port": 1, "src": M1, "dst": M2}).status_code, 410)
        self.assertEqual(self.post(f"{self.prefix}/reset").status_code, 410)
        body = self.get(f"{self.prefix}/state").get_json()
        self.assertTrue(body["reclaimed"])
        # 名额腾出后可另开新台，且旧 token 不会指向新台
        r = self.client.post("/api/benches")
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(r.get_json()["token"], token)
        self.assertEqual(self.get(f"{self.prefix}/state").status_code, 410)

    def test_unknown_token_returns_404(self):
        self.assertEqual(self.get("/api/bench/nope/state").status_code, 404)
        self.assertEqual(self.post("/api/bench/nope/reset").status_code, 404)
        self.assertEqual(self.get("/bench/nope").status_code, 404)

    def test_get_state_does_not_renew_but_write_does(self):
        mgr = BenchManager(idle_timeout=0.05, reaper_interval=999)
        client = create_app(mgr).test_client()
        token = client.post("/api/benches").get_json()["token"]
        import time
        for _ in range(3):
            time.sleep(0.03)
            client.get(f"/api/bench/{token}/state")       # 只看，不续期
        r = client.get(f"/api/bench/{token}/state")       # 惰性回收已发生
        self.assertEqual(r.status_code, 410)

        token2 = client.post("/api/benches").get_json()["token"]
        for _ in range(3):
            time.sleep(0.03)
            client.post(f"/api/bench/{token2}/frame",
                        json={"port": 1, "src": M1, "dst": M2})  # 成功写续期
        self.assertEqual(client.get(f"/api/bench/{token2}/state").status_code, 200)


if __name__ == "__main__":
    unittest.main()
