"""add nces attendance boundaries

Revision ID: a2d9f4c8b6e1
Revises: d8f2c6a4b7e1
Create Date: 2026-08-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a2d9f4c8b6e1"
down_revision: Union[str, None] = "d8f2c6a4b7e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nces_attendance_boundaries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("school_id", sa.String(), nullable=False),
        sa.Column("school_name", sa.String(), nullable=False),
        sa.Column("lea_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("grade_low", sa.String(), nullable=True),
        sa.Column("grade_high", sa.String(), nullable=True),
        sa.Column("level", sa.String(), nullable=True),
        sa.Column("source_name", sa.String(), nullable=True),
        sa.Column("defacto", sa.String(), nullable=True),
        sa.Column("open_enroll", sa.String(), nullable=True),
        sa.Column("multi_boundary", sa.String(), nullable=True),
        sa.Column("bbox_xmin", sa.Float(), nullable=False),
        sa.Column("bbox_ymin", sa.Float(), nullable=False),
        sa.Column("bbox_xmax", sa.Float(), nullable=False),
        sa.Column("bbox_ymax", sa.Float(), nullable=False),
        sa.Column("geometry", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("school_id", "source_name", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax", name="uq_nces_boundary_shape"),
    )
    op.create_index(op.f("ix_nces_attendance_boundaries_school_id"), "nces_attendance_boundaries", ["school_id"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_school_name"), "nces_attendance_boundaries", ["school_name"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_lea_id"), "nces_attendance_boundaries", ["lea_id"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_state"), "nces_attendance_boundaries", ["state"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_level"), "nces_attendance_boundaries", ["level"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_bbox_xmin"), "nces_attendance_boundaries", ["bbox_xmin"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_bbox_ymin"), "nces_attendance_boundaries", ["bbox_ymin"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_bbox_xmax"), "nces_attendance_boundaries", ["bbox_xmax"], unique=False)
    op.create_index(op.f("ix_nces_attendance_boundaries_bbox_ymax"), "nces_attendance_boundaries", ["bbox_ymax"], unique=False)
    op.create_index(
        "ix_nces_boundaries_state_level_bbox",
        "nces_attendance_boundaries",
        ["state", "level", "bbox_xmin", "bbox_xmax", "bbox_ymin", "bbox_ymax"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_nces_boundaries_state_level_bbox", table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_bbox_ymax"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_bbox_xmax"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_bbox_ymin"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_bbox_xmin"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_level"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_state"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_lea_id"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_school_name"), table_name="nces_attendance_boundaries")
    op.drop_index(op.f("ix_nces_attendance_boundaries_school_id"), table_name="nces_attendance_boundaries")
    op.drop_table("nces_attendance_boundaries")
