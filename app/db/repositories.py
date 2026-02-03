"""
Database repositories for data access.

This module implements the repository pattern, separating data access
logic from business logic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from .connection import PSYCOPG2_AVAILABLE, get_connection

if TYPE_CHECKING:
    from psycopg2.extensions import connection as PgConnection

if PSYCOPG2_AVAILABLE:
    import psycopg2

logger = logging.getLogger("device-management.db.repositories")


class ProvisioningRepository:
    """
    Repository for provisioning records.

    Handles all CRUD operations for the provisioning table.
    """

    @staticmethod
    def upsert(
        *,
        email: str,
        client_uuid: str,
        device_name: str,
        encryption_key: str,
        comments: str = "enroll",
        conn: PgConnection | None = None,
    ) -> bool:
        """
        Insert or update a provisioning record.

        Uses INSERT with conflict handling for idempotent enrollment.
        Updates only if the existing record is PENDING or ENROLLED.

        Args:
            email: User email
            client_uuid: Device/client UUID
            device_name: Device/plugin name
            encryption_key: Encryption key or fingerprint
            comments: Optional comments
            conn: Optional existing connection (for transactions)

        Returns:
            True if operation succeeded, False otherwise
        """
        if not PSYCOPG2_AVAILABLE:
            logger.warning("psycopg2 not available, skipping upsert")
            return False

        def _execute(connection: PgConnection) -> bool:
            connection.autocommit = True
            with connection.cursor() as cur:
                try:
                    cur.execute(
                        """
                        INSERT INTO provisioning (
                            email, device_name, client_uuid, status, encryption_key, comments
                        ) VALUES (%s, %s, %s, 'ENROLLED', %s, %s)
                        """,
                        (email, device_name, client_uuid, encryption_key, comments),
                    )
                    return True
                except psycopg2.Error as exc:
                    # Handle unique constraint violation (23505)
                    if getattr(exc, "pgcode", None) != "23505":
                        raise
                    cur.execute(
                        """
                        UPDATE provisioning
                        SET email = %s,
                            device_name = %s,
                            status = 'ENROLLED',
                            encryption_key = %s,
                            updated_at = now()
                        WHERE client_uuid = %s
                          AND status IN ('PENDING', 'ENROLLED')
                        """,
                        (email, device_name, encryption_key, client_uuid),
                    )
                    return cur.rowcount > 0

        try:
            if conn:
                return _execute(conn)
            with get_connection() as connection:
                return _execute(connection)
        except Exception:
            logger.exception("Failed to upsert provisioning")
            return False

    @staticmethod
    def get_by_client_uuid(
        client_uuid: str | UUID,
        *,
        conn: PgConnection | None = None,
    ) -> dict | None:
        """
        Get provisioning record by client UUID.

        Args:
            client_uuid: Device/client UUID
            conn: Optional existing connection

        Returns:
            Provisioning record as dict, or None if not found
        """
        if not PSYCOPG2_AVAILABLE:
            return None

        def _execute(connection: PgConnection) -> dict | None:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, created_at, updated_at, email, device_name,
                           client_uuid, status, encryption_key, comments
                    FROM provisioning
                    WHERE client_uuid = %s
                      AND status IN ('PENDING', 'ENROLLED')
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (str(client_uuid),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "created_at": row[1],
                    "updated_at": row[2],
                    "email": row[3],
                    "device_name": row[4],
                    "client_uuid": row[5],
                    "status": row[6],
                    "encryption_key": row[7],
                    "comments": row[8],
                }

        try:
            if conn:
                return _execute(conn)
            with get_connection() as connection:
                return _execute(connection)
        except Exception:
            logger.exception("Failed to get provisioning by client_uuid")
            return None

    @staticmethod
    def revoke(
        client_uuid: str | UUID,
        *,
        conn: PgConnection | None = None,
    ) -> bool:
        """
        Revoke a provisioning (set status to REVOKED).

        Args:
            client_uuid: Device/client UUID
            conn: Optional existing connection

        Returns:
            True if a record was revoked, False otherwise
        """
        if not PSYCOPG2_AVAILABLE:
            return False

        def _execute(connection: PgConnection) -> bool:
            connection.autocommit = True
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE provisioning
                    SET status = 'REVOKED', updated_at = now()
                    WHERE client_uuid = %s
                      AND status IN ('PENDING', 'ENROLLED')
                    """,
                    (str(client_uuid),),
                )
                return cur.rowcount > 0

        try:
            if conn:
                return _execute(conn)
            with get_connection() as connection:
                return _execute(connection)
        except Exception:
            logger.exception("Failed to revoke provisioning")
            return False


class DeviceConnectionRepository:
    """
    Repository for device connection audit logs.

    Handles logging of device connection events.
    """

    # Actions that should not be logged
    SKIP_ACTIONS = {"HEALTHZ"}

    @staticmethod
    def log(
        *,
        action: str,
        email: str,
        client_uuid: str,
        encryption_key_fingerprint: str,
        source_ip: str | None = None,
        user_agent: str | None = None,
        connected_at: datetime | None = None,
        conn: PgConnection | None = None,
    ) -> bool:
        """
        Log a device connection event.

        Args:
            action: Action type (ENROLL, CONFIG_GET, BINARY_GET, etc.)
            email: User email
            client_uuid: Device/client UUID
            encryption_key_fingerprint: Key fingerprint for audit
            source_ip: Client source IP
            user_agent: Client user agent string
            connected_at: Connection timestamp (defaults to now())
            conn: Optional existing connection

        Returns:
            True if log was created, False otherwise
        """
        if action in DeviceConnectionRepository.SKIP_ACTIONS:
            return True

        if not PSYCOPG2_AVAILABLE:
            logger.debug("psycopg2 not available, skipping connection log")
            return False

        def _execute(connection: PgConnection) -> bool:
            connection.autocommit = True
            with connection.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO device_connections (
                        email, client_uuid, action, encryption_key_fingerprint,
                        connected_at, source_ip, user_agent
                    ) VALUES (%s, %s, %s, %s, COALESCE(%s, now()), %s, %s)
                    """,
                    (
                        email,
                        client_uuid,
                        action,
                        encryption_key_fingerprint,
                        connected_at,
                        source_ip,
                        user_agent,
                    ),
                )
                return True

        try:
            if conn:
                return _execute(conn)
            with get_connection() as connection:
                return _execute(connection)
        except Exception:
            logger.exception("Failed to log device connection")
            return False

    @staticmethod
    def get_last_connections(
        client_uuid: str | UUID,
        limit: int = 10,
        *,
        conn: PgConnection | None = None,
    ) -> list[dict]:
        """
        Get the last N connections for a device.

        Args:
            client_uuid: Device/client UUID
            limit: Maximum number of records to return
            conn: Optional existing connection

        Returns:
            List of connection records as dicts
        """
        if not PSYCOPG2_AVAILABLE:
            return []

        def _execute(connection: PgConnection) -> list[dict]:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, created_at, email, client_uuid, action,
                           encryption_key_fingerprint, connected_at,
                           disconnected_at, source_ip, user_agent
                    FROM device_connections
                    WHERE client_uuid = %s
                    ORDER BY connected_at DESC
                    LIMIT %s
                    """,
                    (str(client_uuid), limit),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": row[0],
                        "created_at": row[1],
                        "email": row[2],
                        "client_uuid": row[3],
                        "action": row[4],
                        "encryption_key_fingerprint": row[5],
                        "connected_at": row[6],
                        "disconnected_at": row[7],
                        "source_ip": row[8],
                        "user_agent": row[9],
                    }
                    for row in rows
                ]

        try:
            if conn:
                return _execute(conn)
            with get_connection() as connection:
                return _execute(connection)
        except Exception:
            logger.exception("Failed to get last connections")
            return []


# Singleton instances for convenience
provisioning_repo = ProvisioningRepository()
device_connection_repo = DeviceConnectionRepository()
