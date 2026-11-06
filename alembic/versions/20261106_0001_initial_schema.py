"""Initial schema: all tables, pgvector + pg_trgm, row level security.

Revision ID: 0001
Revises:
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 768

# Creation order matters for foreign keys; drop order is the reverse.
TABLES = [
    "users",
    "oauth_tokens",
    "sync_state",
    "emails",
    "email_insights",
    "attachments",
    "sent_embeddings",
    "preferences",
    "preference_notes",
    "approvals",
    "scheduling_actions",
    "notifications",
    "activity_log",
    "chat_messages",
    "digests",
]


def standard_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    """id, created_at, updated_at — present on every table."""
    return [
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def user_column(unique: bool = False) -> sa.Column:  # type: ignore[type-arg]
    """user_id referencing users; rows are deleted together with the user."""
    return sa.Column(
        "user_id",
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=unique,
        index=not unique,
    )


def email_column(nullable: bool, ondelete: str = "CASCADE", index: bool = True) -> sa.Column:  # type: ignore[type-arg]
    """email_id referencing emails."""
    return sa.Column(
        "email_id", sa.Uuid(), sa.ForeignKey("emails.id", ondelete=ondelete), nullable=nullable, index=index
    )


def text_array(name: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, ARRAY(sa.Text()), nullable=False, server_default="{}")


def jsonb(name: str, nullable: bool = False, default: str = "{}") -> sa.Column:  # type: ignore[type-arg]
    server_default = None if nullable else sa.text(f"'{default}'::jsonb")
    return sa.Column(name, JSONB(), nullable=nullable, server_default=server_default)


def upgrade() -> None:
    # NOTE: extensions are created in the default schema so this works on Supabase and plain Postgres.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "users",
        *standard_columns(),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.Text()),
        sa.Column("picture_url", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("onboarded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "oauth_tokens",
        *standard_columns(),
        user_column(unique=True),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        text_array("scopes"),
    )
    op.create_table(
        "sync_state",
        *standard_columns(),
        user_column(unique=True),
        sa.Column("history_id", sa.String(32)),
        sa.Column("watch_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "emails",
        *standard_columns(),
        user_column(),
        sa.Column("gmail_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column("from_name", sa.Text()),
        text_array("to"),
        text_array("cc"),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_text", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_unread", sa.Boolean(), nullable=False, server_default=sa.false()),
        jsonb("invite", nullable=True),
        sa.Column("message_id_header", sa.Text()),
        sa.Column("references_header", sa.Text()),
        sa.UniqueConstraint("user_id", "gmail_id", name="uq_emails_user_gmail_id"),
    )
    op.create_index("ix_emails_user_received", "emails", ["user_id", "received_at"])
    op.create_index("ix_emails_user_thread", "emails", ["user_id", "thread_id"])

    op.create_table(
        "email_insights",
        *standard_columns(),
        user_column(),
        sa.Column("email_id", sa.Uuid(), sa.ForeignKey("emails.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("category", sa.String(32), nullable=False, index=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("sentiment", sa.String(32), nullable=False),
        sa.Column("sentiment_score", sa.Double(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("thread_summary", sa.Text()),
        jsonb("action_items", default="[]"),
        jsonb("meeting_intent", nullable=True),
        sa.Column("needs_reply", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suspicious", sa.Boolean(), nullable=False, server_default=sa.false()),
        jsonb("suspicious_reasons", default="[]"),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
    )
    op.create_table(
        "attachments",
        *standard_columns(),
        user_column(),
        email_column(nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("gmail_attachment_id", sa.Text()),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("sender_name", sa.Text()),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gmail_thread_id", sa.String(64), nullable=False),
        sa.Column("gmail_message_id", sa.String(64), nullable=False),
        sa.Column(
            "search",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(filename, '') || ' ' || coalesce(sender_name, '')"
                " || ' ' || coalesce(sender_email, '') || ' ' || coalesce(subject, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.create_index("ix_attachments_search", "attachments", ["search"], postgresql_using="gin")
    op.create_index(
        "ix_attachments_filename_trgm",
        "attachments",
        ["filename"],
        postgresql_using="gin",
        postgresql_ops={"filename": "gin_trgm_ops"},
    )

    op.create_table(
        "sent_embeddings",
        *standard_columns(),
        user_column(),
        email_column(nullable=False),
        sa.Column("chunk", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    )
    op.create_index(
        "ix_sent_embeddings_hnsw",
        "sent_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "preferences",
        *standard_columns(),
        user_column(unique=True),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("sign_off", sa.Text(), nullable=False, server_default="Best regards"),
        sa.Column("tone", sa.Text(), nullable=False, server_default="friendly and professional"),
        sa.Column("language", sa.String(64), nullable=False, server_default="English"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        text_array("vip_senders"),
        jsonb("meeting_preferences"),
    )
    op.create_table(
        "preference_notes",
        *standard_columns(),
        user_column(),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="user"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
    )
    op.create_table(
        "approvals",
        *standard_columns(),
        user_column(),
        email_column(nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        jsonb("payload"),
        jsonb("warnings", default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        jsonb("result", nullable=True),
    )
    op.create_index("ix_approvals_user_status", "approvals", ["user_id", "status"])

    op.create_table(
        "scheduling_actions",
        *standard_columns(),
        user_column(),
        email_column(nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("calendar_event_id", sa.Text()),
        sa.Column("mode", sa.String(16), nullable=False),
        jsonb("decision"),
        sa.Column("undo_until", sa.DateTime(timezone=True)),
        sa.Column("undone", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "notifications",
        *standard_columns(),
        user_column(),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("link", sa.Text()),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("undo_action_id", sa.Uuid(), sa.ForeignKey("scheduling_actions.id", ondelete="SET NULL")),
    )
    op.create_table(
        "activity_log",
        *standard_columns(),
        user_column(),
        email_column(nullable=True, ondelete="SET NULL", index=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(64)),
        jsonb("detail"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
    )
    op.create_index("ix_activity_user_created", "activity_log", ["user_id", "created_at"])

    op.create_table(
        "chat_messages",
        *standard_columns(),
        user_column(),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        jsonb("steps", default="[]"),
    )
    op.create_index("ix_chat_messages_user_session", "chat_messages", ["user_id", "session_id"])

    op.create_table(
        "digests",
        *standard_columns(),
        user_column(),
        sa.Column("date", sa.Date(), nullable=False),
        jsonb("content"),
        sa.UniqueConstraint("user_id", "date", name="uq_digests_user_date"),
    )

    # Row level security on with no policies: only the backend (table owner) can read or write.
    for table in [*TABLES, "alembic_version"]:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
