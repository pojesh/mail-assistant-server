"""All database tables. Import from here so Alembic sees every model."""

from app.models.actions import ActivityLog, Approval, Notification, SchedulingAction
from app.models.base import Base
from app.models.chat import ChatMessage, Digest
from app.models.email import Attachment, Email, EmailInsight, SentEmbedding
from app.models.preferences import PreferenceNote, Preferences
from app.models.user import OAuthToken, SyncState, User

__all__ = [
    "ActivityLog",
    "Approval",
    "Attachment",
    "Base",
    "ChatMessage",
    "Digest",
    "Email",
    "EmailInsight",
    "Notification",
    "OAuthToken",
    "PreferenceNote",
    "Preferences",
    "SchedulingAction",
    "SentEmbedding",
    "SyncState",
    "User",
]
