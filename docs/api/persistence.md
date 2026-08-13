# Persistence

`save_stack_config()` is the authoritative file-write path. It validates and
serializes the complete candidate before touching the destination, writes secret
bytes only to mode-`0600` files, and installs files with `os.replace()`.

::: oa_configurator.io.save_stack_config

::: oa_configurator.io.ConfigSaveError

## Recovery contract

For an existing `config.toml`, the writer first refreshes
`config.toml.bak` through a restrictive temporary file and atomic rename. It
then replaces `config.toml`, synchronizes the directory where the platform
supports it, invalidates loader caches, and reloads the result for verification.

The authoritative state after each failure is:

| Failure point | Authoritative state |
|---|---|
| Validation, serialization, candidate write, or candidate sync | Existing destination is untouched. |
| Backup creation, backup replacement, or first directory sync | Existing destination is untouched; an older backup may remain. |
| Destination replacement | Existing destination remains authoritative; the refreshed backup contains the same pre-save bytes. |
| Directory sync or verification after replacement | The writer restores the pre-save bytes from the backup. |
| First save fails after replacement | The writer removes the new destination. |
| Recovery itself fails | `ConfigSaveError` reports both save and recovery failure; inspect the destination and backup before retrying. |

Temporary files are removed on all handled success and failure paths. The
destination and backup are two distinct atomic replacements, not a single
multi-file transaction.

The backup is intentionally retained after successful verification as the
previous complete configuration. It may contain plaintext credentials; see
[Secrets](../secrets.md#backup-copies) for its lifecycle and symlink-location
semantics.

## Concurrency boundary

Atomic replacement prevents partial-file visibility, but this release does not
lock concurrent writers or compare revisions. Two overlapping successful saves
still use last-writer-wins semantics. Applications requiring stale-draft
protection must coordinate writers until the transactional persistence API is
available.
