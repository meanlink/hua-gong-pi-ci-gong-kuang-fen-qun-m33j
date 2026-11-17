from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models

# create_all 只建新表；对已有演示库补齐新增列，避免升级后 500。
_ADDED_COLUMNS = {
    'datasets': {
        'active_constraint_version_id': 'INTEGER NULL',
    },
    'jobs': {
        'constraint_version_id': 'INTEGER NULL',
        'constraint_version': 'INTEGER NULL',
        'constraint_hash': 'VARCHAR(64) NULL',
        'invalidated_at': 'DOUBLE NULL',
        'invalidation_reason': 'VARCHAR(1000) NOT NULL DEFAULT \'\'',
    },
    'reports': {
        'disabled_reason': 'VARCHAR(1000) NOT NULL DEFAULT \'\'',
    },
}


def _add_columns():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {col['name'] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in present:
                    continue
                if engine.dialect.name == 'sqlite':
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
                else:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


def migrate():
    Base.metadata.create_all(engine)
    _add_columns()


if __name__ == '__main__':
    migrate()
