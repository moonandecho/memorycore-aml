#!/usr/bin/env python3
"""bench_capacity.py - AML 参赛版容量基准（可复跑）。

用法（单机自包含：会临时起一个探针实例，默认端口 8875，数据目录在 /tmp，不碰生产数据）：
    python3 scripts/bench_capacity.py                          # 当前代码 + 三个 E3 开关开启
    BENCH_E3=0 python3 scripts/bench_capacity.py               # 关掉 E3 开关（对照臂）
    BENCH_REPO=<另一棵代码树> BENCH_PORT=8876 python3 scripts/bench_capacity.py

测什么：Add 64 并发 x 20 消息（墙钟 / p50 / p95 / 成功率），Search 32 并发 x 3 轮（墙钟 / p50 / p95 / 最大响应字节），
以及探针峰值 RSS。非 AML 旋钮（worker 数、嵌入并发与批量、请求预算）从部署用的 env 文件读取，与线上同口径；
host / port / 数据目录 / API key 由脚本覆盖。
"""
import concurrent.futures as cf, json, os, statistics, subprocess, sys, tempfile, time, urllib.error, urllib.request

REPO = os.environ.get("BENCH_REPO", "/home/echo/D/memorycore-aml")
PY_ = "/home/echo/D/memorycore-aml/.venv/bin/python"
ENV_FILE = "/home/echo/.config/memorycore-aml/aml.env"; PORT = int(os.environ.get("BENCH_PORT", "8875")); KEY = "bench"

def load_env():
    e = {}
    for line in open(ENV_FILE):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); e[k.strip()] = v.strip().strip('"').strip("'")
    return e

def post(path, payload, timeout=1200):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json", "X-Api-Key": KEY}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "t": time.time() - t0, "bytes": len(r.read()), "code": 200}
    except urllib.error.HTTPError as ex:
        return {"ok": False, "t": time.time() - t0, "code": ex.code, "err": ex.read()[:160].decode("utf-8", "ignore")}
    except Exception as ex:
        return {"ok": False, "t": time.time() - t0, "code": -1, "err": str(ex)[:160]}

def msgs(i, n=20):
    out = []
    for j in range(n):
        m = j % 4
        if m == 0 or m == 3:
            t = "Bench item %d-%d: the vault code is VAULT-%d-%d." % (i, j, i, j)
        elif m == 1:
            t = "第 %d-%d 条：今天讨论了预算、排期与验收口径，结论是先做小样再扩样。" % (i, j) + "补充说明" * 20
        else:
            t = "Note %d-%d: " % (i, j) + " ".join("detail%d" % k for k in range(60))
        out.append({"role": "user", "content": t})
    return out

def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, max(0, int(round(p * len(xs))) - 1))]

base = load_env()
work = tempfile.mkdtemp(prefix="/tmp/aml-bench.")
os.makedirs(work + "/home", exist_ok=True); os.makedirs(work + "/data", exist_ok=True)
env = {"PATH": "/usr/bin:/bin", "TMPDIR": "/tmp"}; env.update(base)
env.update({"HOME": work + "/home", "MNEMOSYNE_DATA_DIR": work + "/data", "MNEMOSYNE_DB_FILE": work + "/data/mnemosyne.db",
            "AML_HOST": "127.0.0.1", "AML_PORT": str(PORT), "AML_API_KEY": KEY})
if os.environ.get("BENCH_E3", "1") != "1":
    for _k in ("AML_RECALL_FUSION", "AML_RERANK_LEXICAL", "AML_RECALL_POOL_MULT"):
        env.pop(_k, None)
print("ARM %s repo=%s switches=%s" % (os.environ.get("ARM", "?"), REPO, {k: env.get(k) for k in ("AML_RECALL_FUSION", "AML_RERANK_LEXICAL", "AML_RECALL_POOL_MULT")}), flush=True)
print("启动探针: port=%d data=%s 开关=%s" % (PORT, work + "/data",
      {k: env.get(k) for k in ("AML_RECALL_FUSION", "AML_RERANK_LEXICAL", "AML_RECALL_POOL_MULT")}), flush=True)
log = open(work + "/svc.log", "wb")
proc = subprocess.Popen([PY_, "-m", "memorycore.aml_server"], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)

def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=3) as r:
            return r.status
    except Exception:
        return 0

for _ in range(90):
    if health() == 200: break
    time.sleep(1)
if health() != 200:
    print("FAIL: 探针未就绪"); print(open(work + "/svc.log").read()[-1500:]); sys.exit(1)
print("探针就绪", flush=True)

N, MESS = 64, 20
t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    adds = list(ex.map(lambda i: post("/add", {"request_id": "bench:%d" % i, "user_id": "bench:u%d" % i,
                                               "session_id": "bench-s%d" % i, "messages": msgs(i, MESS)}), range(N)))
add_wall = time.time() - t0
ok = [a for a in adds if a["ok"]]
print(json.dumps({"phase": "add", "concurrency": N, "messages_per_req": MESS, "wall_s": round(add_wall, 1),
                  "success": len(ok), "fail": N - len(ok), "p50_s": round(statistics.median([a["t"] for a in ok]), 1) if ok else None,
                  "p95_s": round(pct([a["t"] for a in ok], 0.95), 1) if ok else None,
                  "max_s": round(max([a["t"] for a in ok]), 1) if ok else None,
                  "errs": [a.get("err") for a in adds if not a["ok"]][:3]}, ensure_ascii=False), flush=True)

S = 32
rounds = []
for rnd in range(1, 4):
    def one(k):
        i = k % N
        q = ["What is the vault code for item %d" % i, "第 %d-5 条讨论了什么" % i, "summary of notes %d" % i][k % 3]
        return post("/search", {"query": q, "user_id": "bench:u%d" % i, "top_k": 10})
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=S) as ex:
        rs = list(ex.map(one, range(S)))
    wall = time.time() - t0
    oks = [r for r in rs if r["ok"]]
    rounds.append({"round": rnd, "wall_s": round(wall, 1), "success": len(oks), "fail": S - len(oks),
                   "p50_s": round(statistics.median([r["t"] for r in oks]), 2) if oks else None,
                   "p95_s": round(pct([r["t"] for r in oks], 0.95), 2) if oks else None,
                   "max_s": round(max([r["t"] for r in oks]), 2) if oks else None,
                   "max_resp_bytes": max([r["bytes"] for r in oks]) if oks else None,
                   "errs": [r.get("err") for r in rs if not r["ok"]][:2]})
    print(json.dumps(rounds[-1], ensure_ascii=False), flush=True)

vmhwm = 0
try:
    for line in open("/proc/%d/status" % proc.pid):
        if line.startswith("VmHWM"): vmhwm = int(line.split()[1])
except Exception: pass
print(json.dumps({"phase": "search_summary", "concurrency": S, "rounds": rounds, "peak_rss_mib": round(vmhwm / 1024, 1)}, ensure_ascii=False), flush=True)
proc.terminate()
print("BENCH_DONE", flush=True)
