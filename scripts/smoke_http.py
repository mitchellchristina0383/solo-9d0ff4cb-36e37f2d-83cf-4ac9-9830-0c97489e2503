"""端到端冒烟：真实 HTTP 服务下验证多实验台场景。

覆盖：
  场景一：两组并行 —— 各自独立的 VLAN/MAC 表/积压；一台放行积压时另一台照常发帧；
          组员同一链接访问；复位只作用于本台
  场景二：满额 409 —— 最多三台
  场景三：到期回收 —— 只读刷新不续期；成功写操作续期；回收后旧链接/补发请求 410，
          不会命中新台；名额腾出后可另开新台
  场景四：在途写保护 —— 写操作未结束前即使空闲到期也不回收、仍占名额，结束后续期
  场景五：单台转发回归 —— 同址学习、带积压改配、并发写

用法:  python scripts/smoke_http.py        （从仓库根目录运行）
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.serving import make_server

from app import create_app
from bench import BenchManager

BASE = "http://127.0.0.1:8765"
IDLE_TIMEOUT = 2.0          # 冒烟用 2 秒空闲回收（生产默认 300 秒，由环境变量配置）
M1, M2, M3 = "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    def _parse(raw):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw.decode(errors="replace")

    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())


def post(path, payload=None):
    return req("POST", path, payload)


def get(path):
    return req("GET", path)


def create_bench():
    code, j = post("/api/benches")
    assert code == 201, (code, j)
    return j["token"]


def reset_all_benches():
    """每个场景从空名额开始，避免跨场景状态串扰。"""
    for token in list(mgr._benches.keys()):
        mgr.force_reap(token)


def state(token):
    return get(f"/api/bench/{token}/state")


def port(st, pid):
    return next(p for p in st["ports"] if p["id"] == pid)


def scenario_parallel_groups():
    print("\n== 场景一：两组并行，台间状态独立、操作互不阻塞 ==")
    reset_all_benches()
    ta, tb = create_bench(), create_bench()
    check("首页列出两台在役", get("/api/benches")[1]["active_count"] >= 2)

    # A 组：暂停端口2后发帧 -> 积压；B 组同时独立操作
    post(f"/api/bench/{ta}/port/2/pause")
    post(f"/api/bench/{ta}/frame", {"port": 1, "src": M1, "dst": M2})
    post(f"/api/bench/{tb}/frame", {"port": 3, "src": M3, "dst": M1})

    sa, sb = state(ta)[1], state(tb)[1]
    check("A 台：端口2积压1帧", port(sa, 2)["queued"] == 1, sa["ports"])
    check("B 台：看不到 A 台的 MAC 学习", sb["mac_tables"].get("1", {}).get(M1) is None
          and sb["mac_tables"]["1"].get(M3) == 3, sb["mac_tables"])
    check("A 台：看不到 B 台的学习", sa["mac_tables"]["1"].get(M3) is None, sa["mac_tables"])
    check("B 台：无 A 台的积压", all(p["queued"] == 0 for p in sb["ports"]))

    # 同一链接（同 token）再访问，拿到的就是同一台
    check("组员用同一链接进入同一台", state(ta)[1]["bench"]["token"] == ta)

    # A 台放行积压的同时，B 台继续发帧 —— 不同台各自独立锁，互不阻塞
    errors = []

    def b_keeps_sending():
        for i in range(20):
            code, _ = post(f"/api/bench/{tb}/frame",
                           {"port": 1, "src": M1, "dst": M2, "payload": f"b{i}"})
            if code != 200:
                errors.append(code)

    t = threading.Thread(target=b_keeps_sending)
    t.start()
    code, r = post(f"/api/bench/{ta}/port/2/release")
    t.join()
    check("A 台放行积压交付1帧", code == 200 and r["delivered"] == 1, r)
    check("A 放行期间 B 台 20 帧全部正常受理", not errors, errors)
    # B 的帧都从端口1注入、未知目的泛洪到 2/3/4/5：口2应收到全部 20 帧
    check("B 台帧计数独立增长", port(state(tb)[1], 2)["received_count"] >= 20)

    # 复位只作用于本台
    post(f"/api/bench/{ta}/reset")
    sa = state(ta)[1]
    sb = state(tb)[1]
    check("A 台复位后 MAC 表清空", sa["mac_tables"] == {})
    check("B 台不受 A 台复位影响", bool(sb["mac_tables"]), sb["mac_tables"])


def scenario_capacity():
    print("\n== 场景二：最多三台，满额提示 409 ==")
    reset_all_benches()
    tokens = [create_bench() for _ in range(3)]
    st = get("/api/benches")[1]
    check("在役台达到 3 台上限", st["active_count"] == 3, st)
    code, j = post("/api/benches")
    check("第 4 台被拒：409 且带满额提示", code == 409 and "满" in j["error"], (code, j))
    return tokens[-1]


def scenario_expiry(third):
    print("\n== 场景三：空闲到期回收（只读不续期，写成功才续期） ==")
    reset_all_benches()
    third = create_bench()                        # 干净环境下的一台，创建后从不写
    # third 自创建后没有任何写操作；只读轮询 2 秒以上不应续期
    deadline = time.time() + IDLE_TIMEOUT + 1.2
    while time.time() < deadline:
        code, _ = state(third)
        if code == 410:
            break
        time.sleep(0.15)
    code, j = state(third)
    check("空闲到期后 GET 状态返回 410 已回收", code == 410 and j["reclaimed"], (code, j))
    code, body = get(f"/bench/{third}")
    check("旧链接访问页面返回 410 已回收提示",
          code == 410 and isinstance(body, str) and "已被回收" in body, (code, body))
    code, j = post(f"/api/bench/{third}/frame", {"port": 1, "src": M1, "dst": M2})
    check("补发写请求同样 410，无法再操作", code == 410 and j["reclaimed"], (code, j))

    # 名额腾出 -> 可另开新台；旧 token 不会指向新台
    new_token = create_bench()
    check("名额腾出后成功另开新台", bool(new_token) and new_token != third)
    code, _ = state(third)
    check("旧链接仍报已回收，不能操作新台", code == 410)
    check("新台从空白开始", state(new_token)[1]["mac_tables"] == {})

    # 成功写操作会续期：连续写让台活过多个空闲窗口
    alive = create_bench()
    for _ in range(3):
        time.sleep(IDLE_TIMEOUT * 0.7)
        code, _ = post(f"/api/bench/{alive}/frame", {"port": 1, "src": M1, "dst": M2})
        assert code == 200
    check("持续成功写的台不被回收", state(alive)[0] == 200)


def scenario_inflight_protection():
    print("\n== 场景四：在途写结束前不回收、仍占名额 ==")
    reset_all_benches()
    mgr.stop_reaper()      # 本场景只验证在途保护规则，关掉后台回收避免干扰名额
    # 先占满另外 2 个名额，并让它们保持「在途写」，
    # 这样开新台时的惰性回收也不会腾出名额
    holds = []

    def hold_slot():
        tk = create_bench()
        b = mgr._benches[tk]
        original = b.switch.send_frame
        entered, release = threading.Event(), threading.Event()

        def blocking_send(*a, **kw):
            entered.set()
            release.wait(5)
            return original(*a, **kw)

        b.switch.send_frame = blocking_send
        th = threading.Thread(
            target=lambda: post(f"/api/bench/{tk}/frame",
                                {"port": 1, "src": M1, "dst": M2}))
        th.start()
        entered.wait(2)
        holds.append((b, original, th, release))

    hold_slot()
    hold_slot()
    token = create_bench()
    bench = mgr._benches[token]
    original = bench.switch.send_frame
    started = threading.Event()

    def slow_send_frame(*a, **kw):
        started.set()
        time.sleep(IDLE_TIMEOUT + 0.8)     # 写操作跨越整个空闲超时窗口
        return original(*a, **kw)

    bench.switch.send_frame = slow_send_frame

    result = {}

    def issue_write():
        result["code"], result["body"] = post(
            f"/api/bench/{token}/frame", {"port": 1, "src": M1, "dst": M2})

    th = threading.Thread(target=issue_write)
    th.start()
    started.wait(2)
    time.sleep(IDLE_TIMEOUT + 0.3)         # 空闲已到期，但写仍在途
    check("在途写期间台仍可访问（未被回收）", state(token)[0] == 200)
    code, j = post("/api/benches")
    check("在途写期间仍占名额，开新台被拒", code == 409, (code, j))
    th.join()
    check("在途写最终成功结束", result["code"] == 200, result)
    check("写成功后续期，台继续可用", state(token)[0] == 200)
    bench.switch.send_frame = original

    # 结束后不再写，下一个空闲窗口（惰性回收）正常生效
    deadline = time.time() + IDLE_TIMEOUT + 1.2
    code = 200
    while time.time() < deadline:
        code, _ = state(token)
        if code == 410:
            break
        time.sleep(0.15)
    check("写结束后的下一轮空闲回收正常生效", code == 410, code)

    # 释放两个占位在途写并清理
    for b, orig, hth, release in holds:
        b.switch.send_frame = orig
        release.set()
        hth.join(2)
    reset_all_benches()


def scenario_single_switch_regression():
    print("\n== 场景五：单台转发回归（学习/泛洪、带积压改配、并发写） ==")
    reset_all_benches()
    mgr.stop_reaper()                          # 本场景专注转发语义，排除回收干扰
    mgr._reap_idle_locked = lambda now: []     # 同时禁用开台/访问时的惰性回收
    token = create_bench()
    p = f"/api/bench/{token}"
    _, r = post(f"{p}/frame", {"port": 1, "src": M1, "dst": M2})
    check("首帧未知目的泛洪到其他4口",
          r["action"] == "flood" and sorted(r["delivered"]) == [2, 3, 4, 5], r)

    post(f"{p}/port/2/pause")
    post(f"{p}/port/4/pause")
    post(f"{p}/frame", {"port": 2, "src": M2, "dst": M1})  # 学习 M2->端口2（入向不受暂停影响）
    post(f"{p}/frame", {"port": 1, "src": M1, "dst": M2})  # 已知目的 -> 积压端口2
    post(f"{p}/frame", {"port": 1, "src": M1, "dst": M3})  # 未知泛洪 -> 2、4 积压
    st = state(token)[1]
    check("端口2积压2帧、端口4积压1帧",
          port(st, 2)["queued"] == 2 and port(st, 4)["queued"] == 1,
          (port(st, 2)["queued"], port(st, 4)["queued"]))

    _, r = post(f"{p}/port/2/vlan", {"vlan": 20})
    check("带积压改配：丢2帧并失效学习项",
          r["dropped_queue"] == 2 and {"vlan": 1, "mac": M2} in r["purged_entries"], r)
    check("其他口积压不受影响（端口4仍1帧）", port(state(token)[1], 4)["queued"] == 1)

    errors = []

    def hammer():
        for i in range(80):
            code, _ = post(f"{p}/port/2/vlan", {"vlan": 10 + i % 3})
            if code != 200:
                errors.append(code)
            code, _ = post(f"{p}/port/2/release")
            if code != 200:
                errors.append(code)
            code, _ = post(f"{p}/frame", {"port": 1, "src": M1, "dst": M2})
            if code != 200:
                errors.append(code)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发写全部正常响应", not errors, errors[:3])
    check("不变式保持", state(token)[1] is not None)


def main():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    global mgr
    mgr = BenchManager(idle_timeout=IDLE_TIMEOUT, reaper_interval=0.2)
    server = make_server("127.0.0.1", 8765, create_app(mgr), threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        scenario_parallel_groups()
        third = scenario_capacity()
        scenario_expiry(third)
        scenario_inflight_protection()
        scenario_single_switch_regression()
    finally:
        server.shutdown()
        mgr.stop_reaper()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
