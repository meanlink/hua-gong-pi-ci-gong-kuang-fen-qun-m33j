from sqlalchemy import String, JSON, Double, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(1000), default='')
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    # 绑定到提交时的约束版本；NULL 表示无约束分析，不受换版影响。
    constraint_version_id: Mapped[int | None] = mapped_column(
        ForeignKey('constraint_versions.id'), nullable=True, default=None
    )
    constraint_version: Mapped[int | None] = mapped_column(nullable=True, default=None)
    constraint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    invalidated_at: Mapped[float | None] = mapped_column(Double, nullable=True, default=None)
    invalidation_reason: Mapped[str] = mapped_column(String(1000), default='')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
