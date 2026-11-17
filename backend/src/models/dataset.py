from sqlalchemy import String, JSON, Double, Integer
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class Dataset(Base):
    __tablename__ = 'datasets'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    source: Mapped[str] = mapped_column(String(200), default='manual.json')
    records: Mapped[list] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    # 现行约束版本指针；NULL 表示尚无任何版本（等价于无约束分析）。
    # 不声明外键，避免 datasets 与 constraint_versions 相互引用造成建表成环。
    active_constraint_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[float] = mapped_column(Double)
