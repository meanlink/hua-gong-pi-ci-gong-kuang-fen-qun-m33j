from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
    # 约束版本被替换后，旧版分析（含运行中结果）统一拒收，不能批准或分发。
    invalidated = 'invalidated'
class ConstraintFailureKind(str, Enum):
    # 已在有限搜索内证明无解。
    infeasible = 'infeasible'
    # 搜索预算耗尽，不能据此断言无解，必须与无解分开呈现。
    timeout = 'timeout'
