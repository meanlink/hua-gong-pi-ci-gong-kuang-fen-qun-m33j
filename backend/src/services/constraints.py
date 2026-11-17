"""按批次号维护的工艺评审互斥/同群约束。

这一模块承担三件事：
1. 约束文本的规范化与校验（批次缺号、自指、传递闭包矛盾）。
2. 约束版本的并发安全写入：现行版本唯一，重复请求不新增版本。
3. 带 must-link / cannot-link 约束的 KMeans 分群，并区分“已证明无解”与“搜索超时”。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterable

import numpy as np
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import (
    CONSTRAINT_SEARCH_NODE_LIMIT,
    CONSTRAINT_SEARCH_TIMEOUT_SECONDS,
)
from src.models import ConstraintVersion, Dataset, Job, Report
from src.services.audit import record
from src.utils.serialization import checksum

MAX_PAIRS = 4000
MAX_ISSUES = 40

# SQLite 不支持 SELECT ... FOR UPDATE；单进程内用模块锁替代，保证同样串行。
_SQLITE_WRITE_LOCK = threading.RLock()


class ConstraintAnalysisError(Exception):
    """约束不可解 / 搜索超时 / 约束与数据不一致，三者用 kind 区分。"""

    def __init__(self, kind: str, message: str, evidence: list[str] | None = None, search: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.evidence = evidence or []
        self.search = search or {}

    def result(self) -> dict:
        return {
            "status": self.kind,
            "failure": {
                "kind": self.kind,
                "message": self.message,
                "evidence": self.evidence,
                "search": self.search,
            },
        }


# ---------------------------------------------------------------------------
# 规范化与校验
# ---------------------------------------------------------------------------

def _pair_list(raw, label: str, issues: list[dict]) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        issues.append({"code": "malformed", "message": f"{label}必须是批次号对组成的数组。", "pairs": []})
        return []
    if len(raw) > MAX_PAIRS:
        issues.append({"code": "too_many", "message": f"{label}最多 {MAX_PAIRS} 对。", "pairs": []})
        raw = raw[:MAX_PAIRS]
    result: set[tuple[str, str]] = set()
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            issues.append({
                "code": "malformed",
                "message": f"{label}第 {index} 项必须是两个批次号。",
                "pairs": [str(entry)],
            })
            continue
        a, b = str(entry[0]).strip(), str(entry[1]).strip()
        if not a or not b:
            issues.append({
                "code": "missing_batch",
                "message": f"{label}第 {index} 项存在空白批次号（缺号）。",
                "pairs": [[a, b]],
            })
            continue
        if a == b:
            issues.append({
                "code": "self_pair",
                "message": f"{label}不允许同一批次自我成组：{a}。",
                "pairs": [[a, b]],
            })
            continue
        result.add((a, b) if a <= b else (b, a))
    return sorted(result)


def _must_adjacency(pairs: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for a, b in pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _must_path(adj: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    if start not in adj or goal not in adj:
        return None
    queue = [start]
    previous = {start: None}
    while queue:
        node = queue.pop(0)
        if node == goal:
            path = []
            while node is not None:
                path.append(node)
                node = previous[node]
            return list(reversed(path))
        for nxt in sorted(adj.get(node, ())):
            if nxt not in previous:
                previous[nxt] = node
                queue.append(nxt)
    return None


def normalize_and_validate(records: list[dict], must_raw, cannot_raw) -> dict:
    """校验约束并用证据说明问题；issues 非空时版本不可保存。"""
    issues: list[dict] = []
    ids = [str(row.get("batch_id", "")).strip() for row in records]
    id_set = {i for i in ids if i}

    # 批次号是约束的唯一定位键：重复或空号必须显式拒绝，避免把约束套到错误的批次上。
    seen_ids: set[str] = set()
    duplicates: set[str] = set()
    for batch in ids:
        if not batch:
            issues.append({"code": "missing_batch_id", "message": "数据中存在空白批次号，无法作为约束定位键。", "pairs": []})
        elif batch in seen_ids:
            duplicates.add(batch)
        else:
            seen_ids.add(batch)
    if duplicates:
        issues.append({
            "code": "duplicate_batch_id",
            "message": "批次号必须唯一，以下批次号重复：" + "、".join(sorted(duplicates)[:20]) + ("…" if len(duplicates) > 20 else "") + "。",
            "pairs": [[b] for b in sorted(duplicates)[:20]],
        })

    must = _pair_list(must_raw, "同群关系", issues)
    cannot = _pair_list(cannot_raw, "互斥关系", issues)

    unknown: set[str] = set()
    for label, pairs in (("同群关系", must), ("互斥关系", cannot)):
        for a, b in pairs:
            missing = [x for x in (a, b) if x not in id_set]
            if missing:
                unknown.update(missing)
                issues.append({
                    "code": "unknown_batch",
                    "message": f"{label}引用了数据集中不存在的批次：{'、'.join(sorted(set(missing)))}（缺号）。",
                    "pairs": [[a, b]],
                })

    # 直接矛盾
    both = set(must) & set(cannot)
    for a, b in sorted(both):
        issues.append({
            "code": "contradiction",
            "message": f"同一对批次不能既同群又互斥：{a} × {b}。",
            "pairs": [[a, b]],
        })

    # 传递闭包后仍须满足互斥：A—B、B—C 同群时，A×C 互斥即矛盾。
    adj = _must_adjacency(must)
    for a, b in cannot:
        if a not in id_set or b not in id_set or (a, b) in both:
            continue
        path = _must_path(adj, a, b)
        if path:
            issues.append({
                "code": "contradiction",
                "message": (
                    f"传递同群与互斥冲突：{'—'.join(path)} 经同群传递必须同群，"
                    f"但要求 {a} × {b} 不得同群。"
                ),
                "pairs": [[a, b]],
                "witness": path,
            })

    # 去重并截断，保证响应可读。
    unique: list[dict] = []
    seen = set()
    for issue in issues:
        key = (issue["code"], issue["message"])
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    truncated = len(unique) > MAX_ISSUES
    unique = unique[:MAX_ISSUES]

    return {
        "issues": unique,
        "truncated": truncated,
        "must_links": [list(p) for p in must],
        "cannot_links": [list(p) for p in cannot],
        "constraint_hash": constraint_hash(must, cannot),
        "unknown_batches": sorted(unknown),
    }


def constraint_hash(must: Iterable[tuple[str, str]], cannot: Iterable[tuple[str, str]]) -> str:
    return checksum({
        "must_links": sorted([list(p) for p in must]),
        "cannot_links": sorted([list(p) for p in cannot]),
    })


def validate_only(records, must_links, cannot_links) -> dict:
    return normalize_and_validate(records, must_links, cannot_links)


# ---------------------------------------------------------------------------
# 版本写入与失效传播
# ---------------------------------------------------------------------------

@contextmanager
def serialized_dataset_write(db, dataset_id: int):
    """版本切换必须串行：MySQL/PostgreSQL 锁数据流行，SQLite 用进程内锁。"""
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect in ("mysql", "mariadb", "postgresql"):
        db.execute(select(Dataset.id).where(Dataset.id == dataset_id).with_for_update())
        yield
    else:
        with _SQLITE_WRITE_LOCK:
            yield


def _next_version_number(db, dataset_id: int) -> int:
    values = db.scalars(select(ConstraintVersion.version).where(ConstraintVersion.dataset_id == dataset_id))
    return max(values, default=0) + 1


def invalidate_stale(db, dataset: Dataset, new_version: ConstraintVersion, old_version: ConstraintVersion | None) -> dict:
    """把绑定旧约束的任务标记失效，并暂停引用它们的简报。

    无约束任务（constraint_version_id 为 NULL）不受版本切换影响，原有无约束分析继续可用。
    失效是幂等的：运行中的结果在落库前还会再核对一次现行版本。
    """
    now = time.time()
    old_label = f"v{old_version.version}" if old_version else "无"
    reason = (
        f"约束规则已由 {old_label} 更新为 v{new_version.version}；"
        "该分析绑定旧版规则，结论失效，不能批准或进入简报。"
    )
    jobs = list(db.scalars(select(Job).where(
        Job.dataset_id == dataset.id,
        Job.constraint_version_id.is_not(None),
        Job.constraint_version_id != new_version.id,
    )))
    invalidated_jobs = 0
    for job in jobs:
        if job.invalidated_at is None:
            job.invalidated_at = now
            job.invalidation_reason = reason
            invalidated_jobs += 1
        if job.status in ("queued", "running"):
            # 旧版在途结果直接拒收，不保留半成品。
            job.status = "invalidated"
            job.result = {}
        if job.approved:
            job.approved = False

    reports_off = 0
    if jobs:
        report_rows = list(db.scalars(select(Report).where(
            Report.job_id.in_([job.id for job in jobs]),
            Report.enabled.is_(True),
        )))
        for report in report_rows:
            report.enabled = False
            report.disabled_reason = reason
            reports_off += 1
    return {"jobs": invalidated_jobs, "reports": reports_off, "reason": reason}


def create_version(db, payload, user, idempotency_key: str | None = None, audit_actor: str | None = None):
    dataset = db.get(Dataset, payload.dataset_id)
    if not dataset:
        raise HTTPException(404, "数据批次不存在")

    key = (idempotency_key or getattr(payload, "idempotency_key", None) or "").strip() or None
    if key and len(key) > 120:
        raise HTTPException(422, "Idempotency-Key 过长（最多 120 字符）。")

    checked = normalize_and_validate(dataset.records, payload.must_links, payload.cannot_links)

    def _audit(version):
        if audit_actor:
            record(db, audit_actor, "constraint.version.create", {
                "dataset_id": version.dataset_id, "version_id": version.id, "version": version.version,
                "must_pairs": len(version.must_links), "cannot_pairs": len(version.cannot_links),
                "constraint_hash": version.constraint_hash, "idempotency_key": key,
            })

    if key:
        existing = db.scalar(select(ConstraintVersion).where(
            ConstraintVersion.dataset_id == dataset.id,
            ConstraintVersion.idempotency_key == key,
        ))
        if existing:
            if existing.constraint_hash != checked["constraint_hash"]:
                raise HTTPException(409, "同一幂等键提交了不同的约束内容，请使用新的 Idempotency-Key。")
            return existing, True

    if checked["issues"]:
        raise HTTPException(422, {
            "message": "约束无法保存，请先修正以下问题。",
            "issues": checked["issues"],
            "truncated": checked["truncated"],
        })

    # 并发保护：进入临界区前先取快照，锁内再读现行版本；若被别的请求换过版则拒绝。
    dataset_snapshot = db.get(Dataset, dataset.id).active_constraint_version_id
    provided_base = getattr(payload, "base_version_id", None)

    with serialized_dataset_write(db, dataset.id):
        db.expire_all()
        dataset = db.get(Dataset, dataset.id)
        if key:
            existing = db.scalar(select(ConstraintVersion).where(
                ConstraintVersion.dataset_id == dataset.id,
                ConstraintVersion.idempotency_key == key,
            ))
            if existing:
                if existing.constraint_hash != checked["constraint_hash"]:
                    raise HTTPException(409, "同一幂等键提交了不同的约束内容，请使用新的 Idempotency-Key。")
                return existing, True
        current_id = dataset.active_constraint_version_id
        expected = provided_base if provided_base is not None else dataset_snapshot
        if current_id != expected:
            current = db.get(ConstraintVersion, current_id) if current_id is not None else None
            raise HTTPException(409, {
                "message": "约束版本已被其他请求更新，本次换版未生效，也不会新增版本；请刷新后重试。",
                "current_version_id": current_id,
                "current_version": current.version if current else None,
                "expected_version_id": expected,
            })

        # 内容未变化时视为重试：不新增版本，也不连带失效旧分析。
        current = db.get(ConstraintVersion, current_id) if current_id is not None else None
        if current is not None and current.constraint_hash == checked["constraint_hash"]:
            return current, True

        old_version = current
        version = ConstraintVersion(
            dataset_id=dataset.id,
            version=_next_version_number(db, dataset.id),
            must_links=checked["must_links"],
            cannot_links=checked["cannot_links"],
            constraint_hash=checked["constraint_hash"],
            note=(payload.note or "").strip()[:500],
            idempotency_key=key,
            base_version_id=current_id,
            created_by=user.id,
            created_at=time.time(),
        )
        db.add(version)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            if key:
                existing = db.scalar(select(ConstraintVersion).where(
                    ConstraintVersion.dataset_id == dataset.id,
                    ConstraintVersion.idempotency_key == key,
                ))
                if existing:
                    return existing, True
            raise HTTPException(409, "并发换版冲突，请刷新后重试。")
        dataset.active_constraint_version_id = version.id
        counts = invalidate_stale(db, dataset, version, old_version)
        _audit(version)
        db.flush()
        # 在临界区内提交：MySQL 的行锁随事务结束释放，SQLite 用进程锁；
        # 两者都保证并发换版只有一个成功，后来者读到的已是新现行版本。
        db.commit()
    return version, False


def job_is_current(db, job: Job) -> bool:
    """任务绑定的约束版本是否仍是数据集现行版本。无约束任务恒为有效。"""
    if job.constraint_version_id is None:
        return True
    if job.invalidated_at is not None or job.status == "invalidated":
        return False
    dataset = db.get(Dataset, job.dataset_id)
    return bool(dataset and dataset.active_constraint_version_id == job.constraint_version_id)


# ---------------------------------------------------------------------------
# 约束聚类
# ---------------------------------------------------------------------------

def _components(batch_ids: list[str], must_links: list[list[str]]):
    parent = {b: b for b in batch_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in must_links:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

    groups: dict[str, list[str]] = {}
    for b in batch_ids:
        groups.setdefault(find(b), []).append(b)
    order = {b: i for i, b in enumerate(batch_ids)}
    comps = sorted(groups.values(), key=lambda members: min(order[m] for m in members))
    return [sorted(members, key=lambda m: order[m]) for members in comps]


class _SearchTimeout(Exception):
    pass


def _greedy_coloring(adj_sets, k, x_comp, weights, deadline):
    """近似线性的贪心着色：按度数降序，优先并入最近的可用已有颜色。

    成功时就是一个合法可行解（满足互斥）；失败返回 None，交给精确搜索判定无解或超时。
    大规模数据走这条路，避免 O(n²) 的逐点选择拖垮主流程。
    """
    n = len(adj_sets)
    colors = [-1] * n
    dim = x_comp.shape[1]
    sums = np.zeros((k, dim))
    totals = np.zeros(k)
    used = 0
    order = sorted(range(n), key=lambda v: (-len(adj_sets[v]), v))
    for v in order:
        if time.monotonic() > deadline:
            return None
        forbidden = {colors[u] for u in adj_sets[v] if colors[u] != -1}
        existing = [c for c in range(used) if c not in forbidden]
        if existing:
            def score(c):
                return float(np.linalg.norm(x_comp[v] - sums[c] / totals[c]))
            chosen = min(existing, key=score)
        elif used < k:
            chosen = used
            used += 1
        else:
            return None
        colors[v] = chosen
        sums[chosen] += x_comp[v] * weights[v]
        totals[chosen] += weights[v]
    return colors


def _dsatur_coloring(adj_sets, k, x_comp, weights, deadline, node_limit):
    """带对称性剪枝的 DSATUR 精确着色；返回 (colors, state)。

    colors 为 None 时，state["timed_out"] 区分“预算耗尽”与“已穷举证明无解”。
    用显式栈而非递归：5000 批时递归深度会超过 Python 默认上限。
    """
    n = len(adj_sets)
    colors = [-1] * n
    state = {"nodes": 0, "timed_out": False}
    if n == 0:
        return [], state
    overall = np.average(x_comp, axis=0, weights=weights)

    def check_budget():
        state["nodes"] += 1
        if state["nodes"] > node_limit or time.monotonic() > deadline:
            state["timed_out"] = True
            raise _SearchTimeout()

    def select_vertex():
        best, best_key = None, None
        for v in range(n):
            if colors[v] != -1:
                continue
            sat = len({colors[u] for u in adj_sets[v] if colors[u] != -1})
            key = (sat, len(adj_sets[v]), -v)
            if best_key is None or key > best_key:
                best, best_key = v, key
        return best

    def candidates_for(v, used, sums, totals):
        forbidden = {colors[u] for u in adj_sets[v] if colors[u] != -1}
        cands = [c for c in range(used) if c not in forbidden]
        if used < k:
            cands.append(used)  # 对称性剪枝：新颜色只尝试第一个未用色。

        def score(c):
            if c >= used or totals[c] <= 0:
                return float(np.linalg.norm(x_comp[v] - overall))
            cent = sums[c] / totals[c]
            return float(np.linalg.norm(x_comp[v] - cent))
        return sorted(cands, key=score)

    def recompute_sums(used):
        sums = np.zeros((max(k, used, 1), x_comp.shape[1]))
        totals = np.zeros(max(k, used, 1))
        for i in range(n):
            c = colors[i]
            if c >= 0:
                sums[c] += x_comp[i] * weights[i]
                totals[c] += weights[i]
        return sums, totals

    first = select_vertex()
    # 每帧保存：待着色点、候选颜色顺序、下一个候选下标、放置前的已用颜色数。
    stack = [[first, candidates_for(first, 0, *recompute_sums(0)), 0, 0]]
    colored = 0
    try:
        while stack:
            check_budget()
            frame = stack[-1]
            v, cands, pos, used_before = frame
            if pos >= len(cands):
                if colors[v] != -1:
                    colors[v] = -1
                    colored -= 1
                stack.pop()
                continue
            c = cands[pos]
            frame[2] += 1
            colors[v] = c
            if pos == 0:
                colored += 1
            used_now = max(used_before, c + 1)
            if colored == n:
                return colors.copy(), state
            nxt = select_vertex()
            if nxt is None:
                return colors.copy(), state
            stack.append([nxt, candidates_for(nxt, used_now, *recompute_sums(used_now)), 0, used_now])
    except _SearchTimeout:
        return None, state
    return None, state


def _split_cluster(members, x_comp, weights):
    """把独立集按单维最优切分点分成两簇，保证两簇非空。

    用前缀和向量化计算每个切点的两簇 SSE（O(n log n)），大组也很快。
    """
    m = len(members)
    if m < 2:
        return members, []
    pts = x_comp[members]
    w = weights[members]
    n_features = pts.shape[1]
    big = 1e300
    best = (big, None, None)
    for dim in range(n_features):
        order = np.argsort(pts[:, dim], kind="stable")
        values = pts[order, dim]
        ww = w[order]
        pw = np.concatenate([[0.0], np.cumsum(ww)])
        pwx = np.concatenate([np.zeros((1, n_features)), np.cumsum(pts[order] * ww[:, None], axis=0)])
        pwx2 = np.concatenate([[0.0], np.cumsum(ww * (pts[order] ** 2).sum(axis=1))])
        total_w = pw[-1]
        cuts = np.arange(1, m)
        valid = values[cuts - 1] != values[cuts]
        if not valid.any():
            continue
        cuts = cuts[valid]
        left_w = pw[cuts]
        right_w = total_w - left_w
        left_sq = pwx2[cuts] - (pwx[cuts] ** 2).sum(axis=1) / left_w
        right_total = pwx2[-1] - pwx2[cuts]
        right_sums = pwx[-1] - pwx[cuts]
        right_sq = right_total - (right_sums ** 2).sum(axis=1) / right_w
        sse = left_sq + right_sq
        idx = int(np.argmin(sse))
        if sse[idx] < best[0]:
            cut = int(cuts[idx])
            best = (float(sse[idx]), [members[i] for i in order[:cut]], [members[i] for i in order[cut:]])
    if best[1] is None:
        half = m // 2
        return members[:half], members[half:]
    return best[1], best[2]


def _cluster_stats(assign, k, x_comp, weights):
    stats = []
    for c in range(k):
        idx = [i for i in range(len(assign)) if assign[i] == c]
        if not idx:
            stats.append((0.0, np.zeros(x_comp.shape[1]), 0.0))
            continue
        w = weights[idx]
        pts = x_comp[idx]
        total_w = float(w.sum())
        sums = np.sum(pts * w[:, None], axis=0)
        sumsq = float(np.sum(w * np.sum(pts * pts, axis=1)))
        stats.append((total_w, sums, sumsq))
    return stats


def _refine(assign, k, x_comp, weights, adj_sets, deadline):
    """在约束允许的移动中做确定性局部搜索，压低簇内平方和（逐点增量更新）。

    adj_sets 是“同群组”之间的互斥图；某组能否移入颜色 c，取决于它是否有邻组已在 c。
    """
    assign = list(assign)
    n = len(assign)
    remaining = max(0.0, deadline - time.monotonic())
    local_deadline = time.monotonic() + max(0.05, min(remaining, 3.0))
    stats = _cluster_stats(assign, k, x_comp, weights)
    sizes = [row[0] for row in stats]
    improved = True
    passes = 0
    while improved and passes < 40:
        if time.monotonic() > local_deadline:
            break
        improved = False
        passes += 1
        for i in range(n):
            if time.monotonic() > local_deadline:
                break
            src = assign[i]
            if sizes[src] <= weights[i] + 1e-12:
                continue  # 移走会让源群变空
            forbidden = {assign[u] for u in adj_sets[i]}
            wi = weights[i]
            pi = x_comp[i]
            best_delta = -1e-9
            best_target = None
            for target in range(k):
                if target == src or target in forbidden or sizes[target] == 0:
                    continue
                # 增量 SSE：源群失去 i，目标群得到 i。
                sw, ss, sq = stats[src]
                new_src = (sq - wi * float(pi @ pi) - float((ss - wi * pi) @ (ss - wi * pi)) / (sw - wi))
                tw, ts, tq = stats[target]
                new_tgt = ((tq + wi * float(pi @ pi)) - float((ts + wi * pi) @ (ts + wi * pi)) / (tw + wi))
                delta = (new_src + new_tgt) - (sq - float(ss @ ss) / sw + tq - float(ts @ ts) / tw)
                if delta < best_delta:
                    best_delta = delta
                    best_target = target
            if best_target is not None:
                assign[i] = best_target
                # 用一次精确刷新保证数值稳定。
                stats = _cluster_stats(assign, k, x_comp, weights)
                sizes = [row[0] for row in stats]
                improved = True
    return assign


def _ensure_k(assign, k, x_comp, weights):
    """把少于 K 个非空簇的可行解拆分到恰好 K 群；独立集拆分不会破坏互斥。"""
    assign = list(assign)
    while len(set(assign)) < k:
        sizes = {}
        for idx, c in enumerate(assign):
            sizes.setdefault(c, []).append(idx)
        splittable = [(len(v), c) for c, v in sizes.items() if len(v) >= 2]
        if not splittable:
            return None
        _, target = max(splittable)
        left, right = _split_cluster(sizes[target], x_comp, weights)
        new_color = max(assign) + 1
        for idx in right:
            assign[idx] = new_color
    # 压缩为 0..k-1，保持确定性
    mapping = {c: i for i, c in enumerate(sorted(set(assign)))}
    return [mapping[c] for c in assign]


def _witness_clique(adj_sets, limit_nodes=2000, deadline=None):
    """贪心 + 回溯找互斥图中的团，只用于给出“指定群数无解”的可读证据。

    有节点与时间预算，避免在大图上拖垮主流程；找不到也不影响后续精确判别。
    """
    n = len(adj_sets)
    best: list[int] = []
    # 候选集合计算是 O(n)，大图上不划算；团证据只在中小图上求。
    if n > 300:
        return best
    budget = {"left": limit_nodes}

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    def expand(current, candidates):
        nonlocal best
        if budget["left"] <= 0 or timed_out():
            return
        budget["left"] -= 1
        if len(current) + len(candidates) <= len(best):
            return
        if not candidates:
            if len(current) > len(best):
                best = current[:]
            return
        # 取候选里度数最大的点，先扩展大团。
        v = max(candidates, key=lambda x: (len(adj_sets[x] & set(candidates)), -x))
        expand(current + [v], [u for u in candidates if u in adj_sets[v] and u > v])
        expand(current, [u for u in candidates if u != v])

    try:
        expand([], list(range(n)))
    except RecursionError:
        pass
    return best


def analyze_constrained(records, params, k, must_links, cannot_links, version_meta=None, timeout=None):
    """带约束的标准化 KMeans。返回结果字典；不可解/超时抛 ConstraintAnalysisError。"""
    version_meta = version_meta or {}
    timeout = float(timeout if timeout is not None else CONSTRAINT_SEARCH_TIMEOUT_SECONDS)
    deadline = time.monotonic() + max(0.05, timeout)
    started = time.monotonic()

    batch_ids = [str(row.get("batch_id", "")).strip() for row in records]
    # 分析前再确认一次：版本里不能出现当前数据没有的批次，也不能有直接矛盾/重复批次号。
    checked = normalize_and_validate(records, must_links, cannot_links)
    blocking_codes = ("unknown_batch", "self_pair", "contradiction", "missing_batch", "duplicate_batch_id", "missing_batch_id")
    blocking = [i for i in checked["issues"] if i["code"] in blocking_codes]
    if blocking:
        raise ConstraintAnalysisError(
            "invalid",
            "约束版本与当前数据不一致，无法计算。",
            [i["message"] for i in blocking],
        )

    x = np.asarray([[float(row[f]) for f in ("temperature_c", "pressure_bar", "residence_min", "yield_pct")] for row in records])
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    if np.any(std < 1e-9):
        raise ConstraintAnalysisError("invalid", "恒定特征无法标准化。")
    z = (x - mean) / std

    comps = _components(batch_ids, must_links)
    if len(comps) < k:
        raise ConstraintAnalysisError(
            "infeasible",
            f"同群传递闭包后仅有 {len(comps)} 组，少于指定群数 K={k}，不可能保证每群非空。",
            [
                f"传递同群组共 {len(comps)} 组： " + "；".join("{" + "、".join(group) + "}" for group in comps[:12]) + ("…" if len(comps) > 12 else ""),
            ],
            {"groups": len(comps), "k": k},
        )

    index_of = {b: i for i, b in enumerate(batch_ids)}
    comp_index = {b: ci for ci, members in enumerate(comps) for b in members}
    weights = np.asarray([len(members) for members in comps], dtype=float)
    z_comp = np.asarray([z[[index_of[b] for b in members]].mean(axis=0) for members in comps])

    adj_sets = [set() for _ in comps]
    for a, b in cannot_links:
        if a in comp_index and b in comp_index:
            ia, ib = comp_index[a], comp_index[b]
            if ia == ib:
                raise ConstraintAnalysisError(
                    "invalid", "互斥批次经同群传递落入同一组，约束自相矛盾。", [f"{a} × {b} 同属一组。"]
                )
            adj_sets[ia].add(ib)
            adj_sets[ib].add(ia)

    clip_size = max(k + 1, 2)
    clique = _witness_clique(adj_sets, deadline=min(deadline, time.monotonic() + 0.5))
    if len(clique) >= clip_size:
        names = ["{" + "、".join(comps[i][:4]) + ("…" if len(comps[i]) > 4 else "") + "}" for i in clique]
        raise ConstraintAnalysisError(
            "infeasible",
            f"互斥关系构成 {len(clique)} 组两两互斥，超过指定群数 K={k}，无解。",
            ["互斥团证据：" + "；".join(names)],
            {"clique_size": len(clique), "k": k, "clique_groups": [comps[i] for i in clique]},
        )

    colors = None
    state = {"nodes": 0, "timed_out": False}
    method = "exact_coloring"
    if len(comps) > 800:
        # 大图先走线性贪心：成功即合法可行解，失败再退回精确搜索判定无解/超时。
        greedy = _greedy_coloring(adj_sets, k, z_comp, weights, deadline)
        if greedy is not None:
            colors = greedy
            method = "greedy_coloring"
    if colors is None:
        colors, state = _dsatur_coloring(adj_sets, k, z_comp, weights, deadline, CONSTRAINT_SEARCH_NODE_LIMIT)

    search = {
        "method": method,
        "nodes": state["nodes"],
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "timed_out": state["timed_out"],
        "timeout_seconds": timeout,
    }
    if colors is None:
        if state["timed_out"]:
            raise ConstraintAnalysisError(
                "timeout",
                f"约束搜索在 {timeout:.1f} 秒 / {state['nodes']} 个节点内未找到可行分群；这是搜索超时，不代表已证明无解。",
                ["可重试或放宽互斥关系；系统不会把超时当作无解。"],
                search,
            )
        raise ConstraintAnalysisError(
            "infeasible",
            f"已穷举搜索 {state['nodes']} 个节点，证明在 K={k} 下无法满足全部同群/互斥关系。",
            ["可减少互斥关系或增大 K 后重试。"],
            {**search, "proven": True},
        )

    assign = _ensure_k(colors, k, z_comp, weights)
    if assign is None:
        raise ConstraintAnalysisError(
            "infeasible", f"无法在 K={k} 下同时满足约束并保证每群非空。", ["同群组不足以拆分为 K 个非空群。"], search
        )
    assign = _refine(assign, k, z_comp, weights, adj_sets, deadline)
    refinement_limited = time.monotonic() > deadline
    search["refinement_limited"] = refinement_limited
    search["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)

    # 逐批展开并复核约束。
    batch_cluster = [0] * len(batch_ids)
    for ci, members in enumerate(comps):
        for b in members:
            batch_cluster[index_of[b]] = assign[ci]
    for a, b in must_links:
        if batch_cluster[index_of[a]] != batch_cluster[index_of[b]]:
            raise ConstraintAnalysisError("invalid", "内部复核失败：同群批次未被分到同一群。", [f"{a}、{b}"], search)
    for a, b in cannot_links:
        if batch_cluster[index_of[a]] == batch_cluster[index_of[b]]:
            raise ConstraintAnalysisError("invalid", "内部复核失败：互斥批次被分到同一群。", [f"{a}×{b}"], search)

    centers_raw = np.asarray([x[[i for i, c in enumerate(batch_cluster) if c == cluster]].mean(axis=0) for cluster in range(k)])
    order = np.argsort(centers_raw[:, 0])
    display = {int(cluster): pos + 1 for pos, cluster in enumerate(order)}

    z_assignment = np.asarray(batch_cluster)
    try:
        from sklearn.metrics import silhouette_score
        # 大样本用固定子样本，保证结果可复现且不拖慢任务。
        if len(records) > 3000:
            score = float(silhouette_score(z, z_assignment, sample_size=2000, random_state=42))
        else:
            score = float(silhouette_score(z, z_assignment))
    except Exception:
        score = 0.0

    # 对照：无约束 KMeans 的目标值，说明“贴近原分群目标”的程度。
    try:
        from sklearn.cluster import KMeans
        plain = KMeans(n_clusters=k, n_init=20, random_state=42).fit(z)
        plain_inertia = float(plain.inertia_)
        if len(records) > 3000:
            plain_silhouette = float(silhouette_score(z, plain.labels_, sample_size=2000, random_state=42))
        else:
            plain_silhouette = float(silhouette_score(z, plain.labels_))
    except Exception:
        plain_inertia, plain_silhouette = None, None

    counts = np.bincount(z_assignment, minlength=k)
    constrained_inertia = 0.0
    for cluster in range(k):
        pts = z[z_assignment == cluster]
        if len(pts):
            constrained_inertia += float(((pts - pts.mean(axis=0)) ** 2).sum())

    rows, chart_labels, chart_values = [], [], [[] for _ in range(4)]
    for cluster in order:
        cluster = int(cluster)
        idx = z_assignment == cluster
        n = int(idx.sum())
        rows.append({
            "cluster": f"工况族群{display[cluster]}",
            "count": n,
            "share_pct": round(n / len(records) * 100, 1),
            **{f: round(float(v), 2) for f, v in zip(
                ("temperature_c", "pressure_bar", "residence_min", "yield_pct"), centers_raw[cluster]
            )},
        })
        chart_labels.append(f"族群{display[cluster]}")
        for j in range(4):
            chart_values[j].append(round(float((centers_raw[cluster, j] - mean[j]) / std[j]), 3))

    best = max(rows, key=lambda row: row["yield_pct"])
    must_ok = sum(1 for a, b in must_links if batch_cluster[index_of[a]] == batch_cluster[index_of[b]])
    cannot_ok = sum(1 for a, b in cannot_links if batch_cluster[index_of[a]] != batch_cluster[index_of[b]])

    components_out = []
    for ci, members in enumerate(comps):
        edges = [[a, b] for a, b in must_links if comp_index.get(a) == ci and comp_index.get(b) == ci]
        components_out.append({
            "group": f"G{ci + 1}",
            "cluster": f"工况族群{display[assign[ci]]}",
            "size": len(members),
            "members": members,
            "must_edges": edges,
        })

    # 逐批归群依据：前端可按批次号直接查它在哪个族群、属于哪个同群组。
    batch_assignments = [
        {
            "batch_id": batch_id,
            "cluster": f"工况族群{display[batch_cluster[index_of[batch_id]]]}",
            "group": f"G{comp_index[batch_id] + 1}",
        }
        for batch_id in batch_ids
    ]

    version_label = f"v{version_meta.get('version')}" if version_meta.get("version") else "约束版本"
    warnings = ["分群用于探索，不表明某温压组合可安全放大。"]
    if score < 0.25:
        warnings.append("轮廓系数低于0.25，当前工况分离较弱。")
    if plain_inertia is not None and constrained_inertia > plain_inertia * 1.15:
        warnings.append("约束使簇内平方和明显高于无约束KMeans；这是满足同群/互斥关系的代价。")

    return {
        "summary": (
            f"{len(records)}批工况在{version_label}约束下分为{k}个族群；"
            f"同群{must_ok}/{len(must_links)}对、互斥{cannot_ok}/{len(cannot_links)}对全部满足，"
            f"轮廓系数{score:.3f}。"
        ),
        "status": "satisfied",
        "metrics": [
            {"label": "轮廓系数", "value": round(score, 4)},
            {"label": "族群数量", "value": k},
            {"label": "批次数", "value": len(records)},
            {"label": "约束簇内平方和", "value": round(constrained_inertia, 3)},
        ],
        "table": {
            "columns": [
                {"key": "cluster", "label": "工况族群"},
                {"key": "count", "label": "批次数"},
                {"key": "share_pct", "label": "占比(%)"},
                {"key": "temperature_c", "label": "反应温度(℃)"},
                {"key": "pressure_bar", "label": "釜压(bar)"},
                {"key": "residence_min", "label": "停留时间(min)"},
                {"key": "yield_pct", "label": "收率(%)"},
            ],
            "rows": rows,
        },
        "chart": {
            "type": "bar",
            "labels": chart_labels,
            "series": [
                {"name": f"{label} Z中心", "values": chart_values[j]}
                for j, label in enumerate(["反应温度(℃)", "釜压(bar)", "停留时间(min)", "收率(%)"])
            ],
        },
        "findings": [
            f"{best['cluster']}的中心收率最高，为{best['yield_pct']}%；应结合温压窗口安排独立小试复核。",
            f"最小族群{min(row['count'] for row in rows)}批、最大族群{max(row['count'] for row in rows)}批；每群均非空。",
            f"约束簇内平方和{constrained_inertia:.1f}" + (
                f"，无约束KMeans为{plain_inertia:.1f}（轮廓系数{plain_silhouette:.3f}）。"
                if plain_inertia is not None else "。"
            ),
        ],
        "method": (
            "对四个特征做Z分数标准化后，以同群关系的传递闭包为最小单位做带互斥的K种着色搜索；"
            "在可行解上按簇内平方和做确定性局部优化，并把中心反变换回业务单位。"
        ),
        "warnings": warnings,
        "constraint": {
            "version_id": version_meta.get("version_id"),
            "version": version_meta.get("version"),
            "constraint_hash": version_meta.get("constraint_hash"),
            "must_links": [list(p) for p in must_links],
            "cannot_links": [list(p) for p in cannot_links],
            "must_group_count": len(comps),
            "must_pair_count": len(must_links),
            "cannot_pair_count": len(cannot_links),
            "status": "satisfied",
            "verification": {
                "must_links_satisfied": must_ok,
                "must_links_total": len(must_links),
                "cannot_links_satisfied": cannot_ok,
                "cannot_links_total": len(cannot_links),
                "non_empty_clusters": int((counts > 0).sum()),
                "clusters": k,
            },
            "objective": {
                "constrained_inertia": round(constrained_inertia, 4),
                "constrained_silhouette": round(score, 4),
                "unconstrained_inertia": round(plain_inertia, 4) if plain_inertia is not None else None,
                "unconstrained_silhouette": round(plain_silhouette, 4) if plain_silhouette is not None else None,
            },
            "components": components_out,
            "batch_assignments": batch_assignments,
            "search": search,
        },
    }
