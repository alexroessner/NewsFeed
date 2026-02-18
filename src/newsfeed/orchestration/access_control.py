"""Access control for the NewsFeed Telegram bot.

Provides user allowlisting, admin role checks, and registration flow
to prevent unauthorized access to the bot and its API-consuming commands.

Configuration (in config/pipelines.json under "access_control"):
    allowed_users:   list of Telegram user_id strings that can use the bot
    admin_users:     list of Telegram user_id strings with admin privileges
    open_registration: if true, any user can register via /start (default: false)
    owner_user_id:   the primary owner (always has admin + access, loaded from
                     TELEGRAM_OWNER_ID env var or config)

When allowed_users is empty and open_registration is false, the bot operates
in "owner-only" mode — only the owner can use it.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)


class AccessControl:
    """Manages user authorization for the Telegram bot."""

    # Commands that are always allowed (even for unauthorized users)
    _PUBLIC_COMMANDS = frozenset({"start", "help"})

    # Commands restricted to admin users
    _ADMIN_COMMANDS = frozenset({
        "status", "config", "promote", "demote", "approve", "reject",
        "users", "admin_stats", "broadcast",
    })

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._allowed_users: set[str] = set()
        self._admin_users: set[str] = set()
        self._pending_users: dict[str, float] = {}  # user_id -> request timestamp
        self._open_registration: bool = cfg.get("open_registration", False)

        # Owner from env var or config — always has full access
        owner = os.environ.get("TELEGRAM_OWNER_ID", "") or cfg.get("owner_user_id", "")
        self._owner_id = str(owner).strip() if owner else ""

        # Load configured allowed users
        for uid in cfg.get("allowed_users", []):
            self._allowed_users.add(str(uid).strip())
        for uid in cfg.get("admin_users", []):
            uid_str = str(uid).strip()
            self._admin_users.add(uid_str)
            self._allowed_users.add(uid_str)  # admins are always allowed

        # Owner always in both sets
        if self._owner_id:
            self._allowed_users.add(self._owner_id)
            self._admin_users.add(self._owner_id)

        log.info("Access control initialized: %d allowed, %d admins, open_registration=%s",
                 len(self._allowed_users), len(self._admin_users), self._open_registration)

    def is_allowed(self, user_id: str) -> bool:
        """Check if a user is allowed to use the bot."""
        uid = str(user_id).strip()
        # If no users are configured at all, allow everyone (bootstrapping mode)
        if not self._allowed_users and not self._owner_id:
            return True
        return uid in self._allowed_users

    def is_admin(self, user_id: str) -> bool:
        """Check if a user has admin privileges."""
        uid = str(user_id).strip()
        return uid in self._admin_users

    def is_public_command(self, command: str) -> bool:
        """Check if a command is publicly accessible."""
        return command in self._PUBLIC_COMMANDS

    def is_admin_command(self, command: str) -> bool:
        """Check if a command requires admin privileges."""
        return command in self._ADMIN_COMMANDS

    def check_access(self, user_id: str, command: str) -> tuple[bool, str]:
        """Check if a user can execute a command.

        Returns (allowed, denial_message). If allowed is True, denial_message
        is empty. If False, denial_message explains why.
        """
        uid = str(user_id).strip()

        # Public commands always allowed
        if self.is_public_command(command):
            return True, ""

        # Check basic access
        if not self.is_allowed(uid):
            return False, (
                "You don't have access to this bot.\n"
                "Use /start to request access from the administrator."
            )

        # Check admin requirement
        if self.is_admin_command(command) and not self.is_admin(uid):
            return False, "This command requires admin privileges."

        return True, ""

    def request_access(self, user_id: str) -> str:
        """Handle an access request from a new user.

        Returns a message to send to the user.
        """
        uid = str(user_id).strip()

        if self.is_allowed(uid):
            return "You already have access."

        if self._open_registration:
            self._allowed_users.add(uid)
            log.info("Auto-approved user %s (open_registration=true)", uid)
            return "Welcome! You've been granted access."

        # Queue for admin approval
        self._pending_users[uid] = time.time()
        log.info("User %s requested access (pending admin approval)", uid)
        return (
            "Access request submitted.\n"
            "An administrator will review your request."
        )

    def approve_user(self, admin_id: str, target_id: str) -> str:
        """Admin approves a pending user."""
        if not self.is_admin(admin_id):
            return "You don't have permission to approve users."
        target = str(target_id).strip()
        self._allowed_users.add(target)
        self._pending_users.pop(target, None)
        log.info("Admin %s approved user %s", admin_id, target)
        return f"User {target} approved."

    def reject_user(self, admin_id: str, target_id: str) -> str:
        """Admin rejects a pending user."""
        if not self.is_admin(admin_id):
            return "You don't have permission to reject users."
        target = str(target_id).strip()
        self._pending_users.pop(target, None)
        log.info("Admin %s rejected user %s", admin_id, target)
        return f"User {target} rejected."

    def promote_to_admin(self, admin_id: str, target_id: str) -> str:
        """Promote a user to admin."""
        if not self.is_admin(admin_id):
            return "You don't have permission to promote users."
        target = str(target_id).strip()
        self._admin_users.add(target)
        self._allowed_users.add(target)
        log.info("Admin %s promoted user %s to admin", admin_id, target)
        return f"User {target} promoted to admin."

    def demote_from_admin(self, admin_id: str, target_id: str) -> str:
        """Remove admin privileges from a user."""
        if not self.is_admin(admin_id):
            return "You don't have permission to demote users."
        target = str(target_id).strip()
        if target == self._owner_id:
            return "Cannot demote the owner."
        self._admin_users.discard(target)
        log.info("Admin %s demoted user %s from admin", admin_id, target)
        return f"User {target} demoted from admin."

    def get_pending_users(self) -> list[str]:
        """Return list of user IDs pending approval."""
        return list(self._pending_users.keys())

    def get_user_count(self) -> dict[str, int]:
        """Return user counts for status reporting."""
        return {
            "allowed": len(self._allowed_users),
            "admin": len(self._admin_users),
            "pending": len(self._pending_users),
        }

    def snapshot(self) -> dict[str, Any]:
        """Serialize state for persistence."""
        return {
            "allowed_users": sorted(self._allowed_users),
            "admin_users": sorted(self._admin_users),
            "pending_users": {uid: ts for uid, ts in self._pending_users.items()},
        }

    def restore(self, data: dict[str, Any]) -> int:
        """Restore state from persisted snapshot. Returns count restored."""
        restored = 0
        for uid in data.get("allowed_users", []):
            self._allowed_users.add(str(uid))
            restored += 1
        for uid in data.get("admin_users", []):
            self._admin_users.add(str(uid))
        for uid, ts in data.get("pending_users", {}).items():
            self._pending_users[str(uid)] = ts
        # Ensure owner always has access
        if self._owner_id:
            self._allowed_users.add(self._owner_id)
            self._admin_users.add(self._owner_id)
        return restored
