"""Windows notification adapter for the shared autonomous contact service."""

from datetime import UTC, datetime, timedelta
from .autonomous_intent_state import _iso
from .autonomous_social import AutonomousContact
from .model_budget import BudgetRejected


class DesktopContact(AutonomousContact):
    def capability_status(self, character: str) -> dict:
        preferences = self.runtime.budget.preferences()
        enabled = preferences["contact_enabled"] and not preferences["autonomous_paused"]
        configured = enabled and self.runtime.status()["configured"]
        return {
            "enabled": enabled,
            "policy_allows_now": configured and self.contact._allowed_now(character, datetime.now(UTC)),
            "background_checks_enabled": configured,
            "model_generated_contact_enabled": configured,
            "browser_display_and_confirmation_required": False,
            "native_notifications_depend_on_windows_settings": True,
            "runs_after_app_exit": False,
        }

    def _after_delivery(self, db, message_id, now):
        db.execute("INSERT INTO desktop_contact_notifications(message_id,expires_at) VALUES(?,?)",
                   (message_id, _iso(now + timedelta(minutes=2))))

    def consume_notification(self, *, foreground: bool) -> str | None:
        """At most one native display attempt. Chat delivery is already committed."""
        now = datetime.now(UTC)
        with self.database.atomic() as db:
            pending = db.execute("""SELECT message_id FROM desktop_contact_notifications
                WHERE consumed_at IS NULL AND expires_at>? ORDER BY expires_at DESC LIMIT 1""",
                                 (_iso(now),)).fetchone()
            if not pending:
                return None
            db.execute("UPDATE desktop_contact_notifications SET consumed_at=? WHERE message_id=?", (_iso(now), pending["message_id"]))
            if foreground:
                return None
            try:
                self.runtime.budget.require_autonomous_allowed("autonomous")
            except BudgetRejected:
                return None
            return pending["message_id"] if self.contact._allowed_now("big_fish", now) else None
