"""At most one unfinished sync run per source

Source synchronization became an orchestrated pipeline (`workflow = 'source-sync'` in
`pipeline_runs`) rather than work done inside the HTTP request that asked for it. The
request now only reserves a run, which makes "is this source already syncing?" a question
the database has to be able to answer under concurrency: two clicks, or a click racing
the folder watcher's timer, must not open two crawls of the same estate. Two concurrent
scans interleave their observations and their tombstone decisions, and the one that
decides first deletes what the other has not written yet.

A partial unique index rather than a constraint: finished runs are the run history and
have to be allowed to accumulate per source. Nothing matches the predicate when this runs
— the workflow name did not exist before it — so it can never fail on existing data.

Revision ID: cc9ef5e3d206
Revises: f5320edaf89a
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "cc9ef5e3d206"
down_revision: Union[str, Sequence[str], None] = "f5320edaf89a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must stay identical to models._ACTIVE_SYNC_PREDICATE.
ACTIVE_SYNC_PREDICATE = "workflow = 'source-sync' AND status IN ('queued', 'running')"


def upgrade() -> None:
    op.create_index(
        "uq_pipeline_runs_active_sync",
        "pipeline_runs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_SYNC_PREDICATE),
        sqlite_where=sa.text(ACTIVE_SYNC_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pipeline_runs_active_sync",
        table_name="pipeline_runs",
        postgresql_where=sa.text(ACTIVE_SYNC_PREDICATE),
        sqlite_where=sa.text(ACTIVE_SYNC_PREDICATE),
    )
