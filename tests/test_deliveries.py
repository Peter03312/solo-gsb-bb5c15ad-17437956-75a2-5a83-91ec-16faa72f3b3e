"""幂等投递判定（POST /deliveries、GET /deliveries/{id}）。

覆盖：首次判定冻结（含失败）、超时重试 / 并发同判、编号相同地址不同冲突、
无效地址与未配置策略不占编号、策略改变后仍返回首次判定、失败不切换版本。
"""
import os
import tempfile
import threading

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM delivery_decisions;"
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


BASE_RULES = [
    {
        "match_type": "exact",
        "match_key": "shared@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "base@mail.example.com"}],
    },
    {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "base-catchall@mail.example.com"}],
    },
]

CANDIDATE_RULES = [
    {
        "match_type": "exact",
        "match_key": "shared@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "cand@mail.example.com"}],
    },
    # 候选里 blocked@ 拒收；其他地址无精确规则也无域兜底 → no_route
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
]


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def publish_two_versions(client):
    assert put_rules(client, 0, BASE_RULES).status_code == 200
    assert publish(client, 1).status_code == 200
    assert put_rules(client, 1, CANDIDATE_RULES).status_code == 200
    resp = publish(client, 2)
    assert resp.status_code == 200, resp.text
    return 1, 2


def set_policy(client, revision, baseline, candidate, percent):
    return client.put("/policy", json={
        "revision": revision,
        "baseline_version": baseline,
        "candidate_version": candidate,
        "candidate_percent": percent,
    })


def submit(client, delivery_id, address):
    return client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})


# ---------- 首次受理：冻结当前策略快照与选中版本 ----------

def test_first_submit_persists_decision(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    resp = submit(client, "job-1", "shared@corp.example.com")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_id"] == "job-1"
    assert body["address"] == "shared@corp.example.com"
    assert body["status"] == "ok"
    assert body["policy_revision"] == 1
    assert body["baseline_version"] == v1
    assert body["candidate_version"] == v2
    assert body["candidate_percent"] == 0
    assert body["selection"] == "baseline"
    assert body["version"] == v1
    assert body["endpoints"] == [
        {"mailbox": "base@mail.example.com",
         "paths": [["shared@corp.example.com", "base@mail.example.com"]]}
    ]
    assert "error" not in body


def test_submit_normalizes_id_and_address(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    first = submit(client, " job-1 ", "shared@corp.example.com").json()
    # 编号去首尾空白后一致；地址大小写/空白归一后一致 → 同一次首次判定
    again = submit(client, "job-1", "  SHARED@CORP.EXAMPLE.COM  ")
    assert again.status_code == 200
    assert again.json() == first
    # 库里只有一行
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM delivery_decisions").fetchone()[0] == 1
    conn.close()


# ---------- 重试：策略随后改变，仍返回完全相同的首次判定 ----------

def test_retry_after_policy_change_replays_first_decision(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)  # 首次：0% → 基线 v1
    first = submit(client, "job-1", "shared@corp.example.com")
    assert first.status_code == 200
    first_body = first.json()

    # 策略改为 100% 候选（修订号 2），同一地址现在本该选 v2 得到 cand@
    assert set_policy(client, 1, v1, v2, 100).status_code == 200
    current = client.post("/deliver", json={"address": "shared@corp.example.com"}).json()
    assert current["version"] == v2  # 非幂等接口确实按新策略走

    retry = submit(client, "job-1", "shared@corp.example.com")
    assert retry.status_code == 200
    assert retry.json() == first_body  # 逐字段相同：版本、策略修订号、比例、终点路径
    assert retry.json()["version"] == v1
    assert retry.json()["policy_revision"] == 1
    assert retry.json()["candidate_percent"] == 0


def test_failed_first_decision_is_frozen_and_replayed(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全候选：blocked@ 在 v2 拒收
    first = submit(client, "job-2", "blocked@corp.example.com")
    assert first.status_code == 422
    first_detail = first.json()["detail"]
    assert first_detail["error"] == "rejected"
    assert first_detail["version"] == v2
    assert first_detail["selection"] == "candidate"
    assert first_detail["policy_revision"] == 1
    assert first_detail["address"] == "blocked@corp.example.com"
    assert first_detail["chain"][-1] == "blocked@corp.example.com"

    # 改成 0%：基线对 blocked@ 命中域兜底本可成功——但重试必须仍是首次的拒收失败
    assert set_policy(client, 1, v1, v2, 0).status_code == 200
    retry = submit(client, "job-2", "blocked@corp.example.com")
    assert retry.status_code == 422
    assert retry.json()["detail"] == first_detail


def test_no_route_failure_persisted_and_replayed_identically(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 候选无域兜底：陌生地址 no_route
    first = submit(client, "job-3", "stranger@corp.example.com")
    assert first.status_code == 422
    first_detail = first.json()["detail"]
    assert first_detail["error"] == "no_route"
    assert first_detail["version"] == v2
    for _ in range(3):
        retry = submit(client, "job-3", "stranger@corp.example.com")
        assert retry.status_code == 422
        assert retry.json()["detail"] == first_detail


def test_selected_version_failure_does_not_switch_version(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    resp = submit(client, "job-4", "blocked@corp.example.com")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # 整次失败，停在候选，不退回基线
    assert detail["selection"] == "candidate"
    assert detail["version"] == v2
    assert "endpoints" not in resp.json()


# ---------- 编号相同地址不同 → 冲突 ----------

def test_same_id_different_address_conflicts(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    assert submit(client, "job-1", "shared@corp.example.com").status_code == 200

    clash = submit(client, "job-1", "other@corp.example.com")
    assert clash.status_code == 409
    detail = clash.json()["detail"]
    assert detail["error"] == "delivery_id_conflict"
    assert detail["delivery_id"] == "job-1"
    assert detail["existing_address"] == "shared@corp.example.com"
    assert "endpoints" not in clash.json()

    # 冲突不改变首次判定：原地址继续取回原判定；新地址也没绑进去
    replay = submit(client, "job-1", "shared@corp.example.com")
    assert replay.status_code == 200
    assert replay.json()["version"] == v1


# ---------- 无效地址 / 未配置策略不占用编号 ----------

def test_invalid_address_does_not_occupy_id(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    bad = submit(client, "job-x", "not-an-address")
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"] == "invalid_address"
    # 同一编号随后用于合法地址：正常首受理（未被占用、未误报冲突）
    ok = submit(client, "job-x", "shared@corp.example.com")
    assert ok.status_code == 200, ok.text


def test_missing_policy_does_not_occupy_id(client):
    publish_two_versions(client)
    resp = submit(client, "job-y", "shared@corp.example.com")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "policy_not_configured"
    # 配置策略后同一编号可用
    set_policy(client, 0, 1, 2, 0)
    ok = submit(client, "job-y", "shared@corp.example.com")
    assert ok.status_code == 200, ok.text


def test_empty_delivery_id_rejected(client):
    publish_two_versions(client)
    set_policy(client, 0, 1, 2, 0)
    assert submit(client, "", "shared@corp.example.com").status_code == 422
    assert submit(client, "   ", "shared@corp.example.com").status_code == 422
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM delivery_decisions").fetchone()[0] == 0
    conn.close()


# ---------- 并发：相同编号 + 相同地址只有一个首次判定 ----------

def test_concurrent_same_id_address_share_one_decision(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    n = 12
    barrier = threading.Barrier(n)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        resp = submit(client, "concurrent-1", "shared@corp.example.com")
        with lock:
            results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n
    assert all(status == 200 for status, _ in results)
    bodies = [body for _, body in results]
    assert all(b == bodies[0] for b in bodies)  # 全部拿到完全相同的首次判定
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM delivery_decisions WHERE delivery_id = 'concurrent-1'").fetchone()[0] == 1
    conn.close()


def test_concurrent_same_id_different_address_first_wins_others_conflict(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def worker(address: str):
        barrier.wait()
        resp = submit(client, "race-1", address)
        with lock:
            detail = resp.json().get("detail", {})
            outcomes.append((resp.status_code, detail.get("error", "ok")))

    t1 = threading.Thread(target=worker, args=("shared@corp.example.com",))
    t2 = threading.Thread(target=worker, args=("other@corp.example.com",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    statuses = sorted(s for s, _ in outcomes)
    assert statuses == [200, 409]
    assert {e for _, e in outcomes} == {"ok", "delivery_id_conflict"}
    # 先落库的地址成为该编号的唯一绑定
    conn = db.connect()
    row = conn.execute("SELECT address FROM delivery_decisions WHERE delivery_id = 'race-1'").fetchone()
    conn.close()
    assert row[0] in {"shared@corp.example.com", "other@corp.example.com"}


# ---------- GET 取回判定 ----------

def test_get_delivery_returns_persisted_success(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 0)
    posted = submit(client, "job-1", "shared@corp.example.com").json()
    got_resp = client.get("/deliveries/job-1")
    assert got_resp.status_code == 200
    assert got_resp.json() == posted
    assert "created_at" in got_resp.json()


def test_get_delivery_returns_failed_decision_with_error(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submitted = submit(client, "job-2", "blocked@corp.example.com")
    assert submitted.status_code == 422
    submitted_detail = submitted.json()["detail"]

    got_resp = client.get("/deliveries/job-2")
    assert got_resp.status_code == 200 # 查询本身不是一次投递，不返回 422
    got = got_resp.json()
    assert got["status"] == "failed"
    assert got["error"] == submitted_detail
    assert got["endpoints"] == []
    assert got["version"] == v2


def test_get_unknown_delivery_is_404(client):
    resp = client.get("/deliveries/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "delivery_not_found"
    assert resp.json()["detail"]["delivery_id"] == "nope"
