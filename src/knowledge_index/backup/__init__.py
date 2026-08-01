"""Full-appliance backup: every store, one archive, one manifest, verified on the way out.

``runs`` is the entry point for anything that wants to take, list, verify or prune a
backup; ``restore`` is the entry point for getting one back. Everything else here is the
machinery those two stand on.
"""

from knowledge_index.backup.runs import (
    BackupNotConfigured,
    BackupNotFound,
    BackupRunFailed,
    enqueue_backup,
    execute_backup_run,
    list_backups,
    load_manifest,
    perform_backup,
    preflight,
    prune_backups,
    verify_backup,
    wait_for_run,
)

__all__ = [
    "BackupNotConfigured",
    "BackupNotFound",
    "BackupRunFailed",
    "enqueue_backup",
    "execute_backup_run",
    "list_backups",
    "load_manifest",
    "perform_backup",
    "preflight",
    "prune_backups",
    "verify_backup",
    "wait_for_run",
]
