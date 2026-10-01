"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.db.models.analytics import Feedback, QueryRecord
from app.db.models.audit import AuditEvent
from app.db.models.auth import RefreshToken
from app.db.models.base import Base
from app.db.models.connections import ConnectionGrant, DatabaseConnection, EncryptedSecret
from app.db.models.exports import ExportJob
from app.db.models.metadata import MetadataObject, MetadataSnapshot
from app.db.models.semantic import SemanticVersion
from app.db.models.tenancy import Permission, Role, Tenant, User, UserRole, role_permissions

__all__ = [
    "AuditEvent",
    "Base",
    "ConnectionGrant",
    "DatabaseConnection",
    "EncryptedSecret",
    "ExportJob",
    "Feedback",
    "MetadataObject",
    "MetadataSnapshot",
    "Permission",
    "QueryRecord",
    "RefreshToken",
    "Role",
    "SemanticVersion",
    "Tenant",
    "User",
    "UserRole",
    "role_permissions",
]
