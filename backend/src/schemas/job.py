from pydantic import BaseModel, Field
class JobInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    dataset_id: int
    params: dict = Field(default_factory=dict)
    # 省略或 null 表示原有无约束分析；指定版本时绑定该约束版本快照。
    constraint_version_id: int | None = None
