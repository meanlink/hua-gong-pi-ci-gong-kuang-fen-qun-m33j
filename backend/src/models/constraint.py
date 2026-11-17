from sqlalchemy import String, JSON, Double, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class ConstraintVersion(Base):
    """按批次号保存的不可变约束版本。

    每次换版新增一行，旧版本永不原地修改；Dataset.active_constraint_version_id
    指向唯一现行版本。分析任务保存版本快照，规则更新后旧版任务统一失效。
    """

    __tablename__ = 'constraint_versions'
    __table_args__ = (
        UniqueConstraint('dataset_id', 'version', name='uq_constraint_dataset_version'),
        UniqueConstraint('dataset_id', 'idempotency_key', name='uq_constraint_dataset_idempotency'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    version: Mapped[int] = mapped_column(Integer)
    # 规范化后的批次号对，元素为 [batch_a, batch_b]（字典序）。
    must_links: Mapped[list] = mapped_column(JSON, default=list)
    cannot_links: Mapped[list] = mapped_column(JSON, default=list)
    constraint_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(500), default='')
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    base_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
