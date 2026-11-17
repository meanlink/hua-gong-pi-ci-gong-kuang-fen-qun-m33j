from pydantic import BaseModel, Field


class ConstraintVersionInput(BaseModel):
    dataset_id: int
    must_links: list = Field(default_factory=list, max_length=4000)
    cannot_links: list = Field(default_factory=list, max_length=4000)
    note: str = Field(default='', max_length=500)
    # 乐观并发：客户端带上它看到的现行版本，不匹配即拒绝且不新增版本。
    base_version_id: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)


class ConstraintValidateInput(BaseModel):
    dataset_id: int
    must_links: list = Field(default_factory=list, max_length=4000)
    cannot_links: list = Field(default_factory=list, max_length=4000)
