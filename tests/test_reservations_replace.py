"""保留地址整体替换接口的端到端测试。

直接运行：``python3 -m tests.test_reservations_replace``（或 pytest 收集）。

覆盖：
* 规范化：重叠、相邻、重复的输入网段被合并为覆盖范围相同且数量最少的
  规范 CIDR（两个 /26 + 一个 /25 -> /24；两个 /25 + 一个 /24 -> /23；
  相邻两个 /20 -> /19）；空列表清空保留区；保留整个父网段合法；
* 422：主机位非零、无法解析、越出父网段、地址族不符、版本号非法、
  缺字段；
* 409：版本过期、目标范围与已分配地址相交（整体与部分相交）；
* 409 不改动地址池（版本、保留集、已分配集、空闲 CIDR 全部保持不变）；
* 成功后 GET /api/pool 与后续预演、提交立即采用新保留区：原保留空间
  立即可分配，新保留空间立即被预演避开。
"""
import ipaddress
import os
import tempfile

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_replace.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _pool():
    return client.get("/api/pool").json()


def _preview(prefixes):
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": prefixes}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _replace(version, cidrs):
    return client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": cidrs},
    )


def _cidr_num_key(cidr: str) -> int:
    return int(ipaddress.ip_network(cidr, strict=False).network_address)


def _is_free(cidr: str, free_cidrs: list[str]) -> bool:
    """cidr 完整落在某个规范空闲块内（空闲块是最大块，需按包含判断）。"""
    net = ipaddress.ip_network(cidr)
    return any(
        net.subnet_of(ipaddress.ip_network(free)) for free in free_cidrs
    )


def _assert_pool_unchanged(before):
    after = _pool()
    assert after["version"] == before["version"]
    assert after["reserved_cidrs"] == before["reserved_cidrs"]
    assert after["allocated_cidrs"] == before["allocated_cidrs"]
    assert after["free_cidrs"] == before["free_cidrs"]


def main():
    # ---- 初始状态：v1，保留 10.10.0.0/20 与 10.10.255.0/24 ----
    pool = _pool()
    assert pool["version"] == 1
    assert pool["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]

    # 基线预演：当前唯一恰好整块的 /24 空闲块是池尾 10.10.254.0/24。
    plan = _preview([24])
    assert plan["allocations"] == ["10.10.254.0/24"], plan
    assert plan["pool_version"] == 1

    # ---- 非法输入 -> 422，池不变 ----
    bad_bodies = [
        {"pool_version": 1, "desired_cidrs": ["10.10.254.1/24"]},  # 主机位非零
        {"pool_version": 1, "desired_cidrs": ["not-a-cidr"]},  # 无法解析
        {"pool_version": 1, "desired_cidrs": ["10.11.0.0/24"]},  # 越出父网段
        {"pool_version": 1, "desired_cidrs": ["10.10.0.0/15"]},  # 大于父网段
        {"pool_version": 1, "desired_cidrs": ["2001:db8::/48"]},  # 地址族不符
        {"pool_version": 0, "desired_cidrs": []},  # 版本号非法
        {"pool_version": 1},  # 缺 desired_cidrs
        {"desired_cidrs": []},  # 缺 pool_version
    ]
    for body in bad_bodies:
        r = client.post("/api/pool/reservations/replace", json=body)
        assert r.status_code == 422, (body, r.status_code, r.text)
    assert _pool()["version"] == 1
    print("invalid input -> 422 ok")

    # ---- 版本过期 -> 409，池不变 ----
    before = _pool()
    r = _replace(99, ["10.10.200.0/24"])
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]
    _assert_pool_unchanged(before)
    print("stale version -> 409, pool untouched")

    # ---- 保留整个父网段（合法）：空闲空间为空，预演不可行 ----
    r = _replace(1, ["10.10.0.0/16"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "replaced"
    assert body["pool_version"] == 2
    assert body["reserved_cidrs"] == ["10.10.0.0/16"]
    assert body["free_cidrs"] == []
    pool2 = _pool()
    assert pool2["version"] == 2
    assert pool2["reserved_cidrs"] == ["10.10.0.0/16"]
    assert pool2["free_cidr_count"] == 0
    plan = _preview([24])
    assert plan["feasible"] is False and plan["allocations"] == []
    print("reserve entire parent ok; preview infeasible")

    # ---- 空列表：清空保留区，整个父网段恢复空闲 ----
    r = _replace(2, [])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 3
    assert body["reserved_cidrs"] == []
    assert body["free_cidrs"] == ["10.10.0.0/16"]
    assert _pool()["reserved_cidrs"] == []
    print("empty list clears reservations ok")

    # ---- 替换为单个 /24：GET /api/pool 与预演立即采用新保留区 ----
    r = _replace(3, ["10.10.254.0/24"])
    assert r.status_code == 200, r.text
    assert r.json()["reserved_cidrs"] == ["10.10.254.0/24"]
    pool4 = _pool()
    assert pool4["version"] == 4
    assert pool4["reserved_cidrs"] == ["10.10.254.0/24"]
    # 预演立即避开新保留的 10.10.254.0/24，并选中刚解除保留的
    # 10.10.255.0/24（替换前它是保留子网，不可分配）。
    plan = _preview([24])
    assert plan["pool_version"] == 4
    assert plan["allocations"] == ["10.10.255.0/24"], plan
    committed = _commit(4, plan["allocations"])
    assert committed["pool_version"] == 5
    print("pool/preview/commit use new reservations immediately ok")

    # ---- 目标范围与已分配地址相交 -> 409，池不变 ----
    before = _pool()
    r = _replace(5, ["10.10.255.0/24"])  # 恰为已分配块
    assert r.status_code == 409, r.text
    assert "overlaps allocated" in r.json()["detail"]
    # 多条输入中仅一条与已分配地址部分相交（255.128/25 ⊂ 255.0/24）。
    r = _replace(5, ["10.10.200.0/24", "10.10.255.128/25"])
    assert r.status_code == 409, r.text
    assert "overlaps allocated" in r.json()["detail"]
    _assert_pool_unchanged(before)
    print("overlap with allocated -> 409, pool untouched")

    # ---- 规范化：重叠 + 相邻 + 重复输入合并为最少规范 CIDR ----
    # 10.10.64.0/26 + 10.10.64.64/26 + 10.10.64.128/25 -> 10.10.64.0/24
    # 10.10.128.0/25 + 10.10.128.128/25 + 10.10.129.0/24（+重复的
    # 10.10.128.0/24）-> 10.10.128.0/23
    r = _replace(
        5,
        [
            "10.10.64.0/26",
            "10.10.64.64/26",
            "10.10.64.128/25",
            "10.10.128.0/25",
            "10.10.128.128/25",
            "10.10.129.0/24",
            "10.10.128.0/24",
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 6
    # 响应按网络地址升序返回规范化结果。
    assert body["reserved_cidrs"] == ["10.10.64.0/24", "10.10.128.0/23"]
    assert body["allocated_cidrs"] == ["10.10.255.0/24"]
    pool6 = _pool()
    assert pool6["version"] == 6
    assert sorted(pool6["reserved_cidrs"], key=_cidr_num_key) == [
        "10.10.64.0/24",
        "10.10.128.0/23",
    ]
    # 新保留区不再空闲；已分配块仍被占用。
    assert not _is_free("10.10.64.0/24", pool6["free_cidrs"])
    assert not _is_free("10.10.128.0/23", pool6["free_cidrs"])
    assert not _is_free("10.10.255.0/24", pool6["free_cidrs"])
    print("overlapping/adjacent input normalized to minimal CIDRs ok")

    # ---- 相邻网段进一步合并：两个相邻 /20 -> 一个 /19 ----
    r = _replace(6, ["10.10.0.0/20", "10.10.16.0/20"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 7
    assert body["reserved_cidrs"] == ["10.10.0.0/19"]
    pool7 = _pool()
    assert pool7["reserved_cidrs"] == ["10.10.0.0/19"]
    assert pool7["allocated_cidrs"] == ["10.10.255.0/24"]
    # 预演立即避开新保留区：最优 /24 落在池尾整块空闲的 10.10.254.0/24。
    plan = _preview([24])
    assert plan["pool_version"] == 7
    assert plan["allocations"] == ["10.10.254.0/24"], plan
    print("adjacent cidrs merged; preview avoids new reserved ok")

    print("ALL RESERVATIONS-REPLACE TESTS PASSED")


if __name__ == "__main__":
    main()
