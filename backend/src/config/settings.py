import os
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./local.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ.get('JWT_SECRET', 'local-development-change-before-deploy')
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', '/artifacts')
# 约束聚类搜索预算：超时与节点上限都必须与“已证明无解”区分。
CONSTRAINT_SEARCH_TIMEOUT_SECONDS = float(os.getenv('CONSTRAINT_SEARCH_TIMEOUT_SECONDS', '8'))
CONSTRAINT_SEARCH_NODE_LIMIT = int(os.getenv('CONSTRAINT_SEARCH_NODE_LIMIT', '200000'))
