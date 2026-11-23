"""Flask Web 层：多组共用的五端口虚拟交换机实验台。

路由分两层：
- 首页 ``/``：开新台、查看在役台与名额；
- 台内 ``/bench/<token>`` 与 ``/api/bench/<token>/...``：每组用同一个链接
  进入自己的台，台间状态完全独立。

台的生命周期（名额、空闲回收、在途写保护、已回收应答）见 ``bench.BenchManager``。
直接运行:  python app.py        （监听 0.0.0.0:8000，threaded=True）
容器运行:  见 Dockerfile / docker-compose.yml
"""

import os

from flask import Flask, jsonify, render_template, request

from bench import (
    BenchLimitError,
    BenchManager,
    BenchNotFound,
    BenchReclaimed,
    DEFAULT_IDLE_TIMEOUT,
    MAX_BENCHES,
)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def create_app(manager=None):
    app = Flask(__name__)
    if manager is None:
        manager = BenchManager(
            max_benches=int(os.environ.get("BENCH_MAX", MAX_BENCHES)),
            idle_timeout=_env_float("BENCH_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT),
            reaper_interval=_env_float("BENCH_REAP_INTERVAL", 10.0),
        )
    app.bench_manager = manager
    manager.start_reaper()

    def body():
        return request.get_json(silent=True) or {}

    def err(exc, status):
        return jsonify({"error": str(exc), "reclaimed": isinstance(exc, BenchReclaimed)}), status

    # ================= 页面 =================
    @app.get("/")
    def index():
        return render_template("index.html", status=manager.status())

    @app.get("/bench/<token>")
    def bench_page(token):
        try:
            bench = manager.access(token)
        except BenchReclaimed as e:
            # 旧链接再访问：明确提示已回收，不能操作新台
            return render_template("reclaimed.html", message=str(e)), 410
        except BenchNotFound as e:
            return render_template("reclaimed.html", message=str(e)), 404
        with bench.lock:
            info = bench.info()
            info["idle_timeout"] = int(manager.idle_timeout)
        return render_template("bench.html", token=token, info=info)

    # ================= 台管理 API =================
    @app.get("/api/benches")
    def api_benches():
        return jsonify(manager.status())

    @app.post("/api/benches")
    def api_create_bench():
        try:
            bench = manager.create_bench()
        except BenchLimitError as e:
            return err(e, 409)
        return jsonify({"token": bench.token, "url": f"/bench/{bench.token}",
                        "bench": bench.info()}), 201

    # ================= 台内 API =================
    @app.get("/api/bench/<token>/state")
    def api_state(token):
        try:
            bench = manager.access(token)                  # 只读，不续期
        except BenchReclaimed as e:
            return err(e, 410)
        except BenchNotFound as e:
            return err(e, 404)
        with bench.lock:
            payload = bench.switch.state()
            payload["bench"] = bench.info()
            payload["bench"]["idle_timeout"] = int(manager.idle_timeout)
        return jsonify(payload)

    def write_call(token, fn):
        """统一写流程：在途登记 -> 本台锁内执行 -> 成功才续期。"""
        try:
            bench = manager.begin_write(token)
        except BenchReclaimed as e:
            return err(e, 410)
        except BenchNotFound as e:
            return err(e, 404)
        success = False
        try:
            with bench.lock:
                result = fn(bench.switch)
            success = True
            return jsonify(result)
        except ValueError as e:                          # 业务输入错误
            return err(e, 400)
        finally:
            manager.end_write(bench, success)

    @app.post("/api/bench/<token>/frame")
    def api_frame(token):
        data = body()
        return write_call(token, lambda sw: sw.send_frame(
            data.get("port"), data.get("src", ""),
            data.get("dst", ""), data.get("payload", "")))

    @app.post("/api/bench/<token>/port/<int:port_id>/vlan")
    def api_set_vlan(token, port_id):
        return write_call(token, lambda sw: sw.set_vlan(port_id, body().get("vlan")))

    @app.post("/api/bench/<token>/port/<int:port_id>/pause")
    def api_pause(token, port_id):
        return write_call(token, lambda sw: sw.pause(port_id))

    @app.post("/api/bench/<token>/port/<int:port_id>/release")
    def api_release(token, port_id):
        return write_call(token, lambda sw: sw.release(port_id))

    @app.post("/api/bench/<token>/reset")
    def api_reset(token):
        # 复位只作用于本台：manager 内每台一个独立 Switch 实例
        return write_call(token, lambda sw: sw.reset())

    return app


app = create_app()

if __name__ == "__main__":
    # threaded=True：不同台的请求、同一台的并发写都可同时到达，
    # 台间互不阻塞，台内由 Bench/Switch 的锁保证原子生效。
    app.run(host="0.0.0.0", port=8000, threaded=True)
