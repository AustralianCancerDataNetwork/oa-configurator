# Saving configuration safely

Analysts using `omop-config` do not need to call this API directly; the CLI uses it whenever a command changes `config.toml`. Application developers should call `save_stack_config()` only after a candidate has been validated, presented, and approved. The function validates and serializes the whole candidate before touching the current file, and any temporary or backup file containing credentials is created with mode `0600` before those credentials are written.

::: oa_configurator.io.save_stack_config

::: oa_configurator.io.ConfigSaveError

## What remains after a failed save

When `config.toml` already exists, oa-configurator copies its complete contents to `config.toml.bak` before replacing it. It then reloads the new file to make sure the saved configuration is usable. If anything fails after replacement, it restores the previous file; if the failed operation was the first save, it removes the unusable new file.

The table below is useful when an application catches `ConfigSaveError` or an analyst sees “Could not save configuration” in the CLI:

| Failure point | Authoritative state |
|---|---|
| Validation, serialization, candidate write, or candidate sync | Existing destination is untouched. |
| Backup creation, backup replacement, or first directory sync | Existing destination is untouched; an older backup may remain. |
| Destination replacement | Existing destination remains authoritative; the refreshed backup contains the same pre-save bytes. |
| Directory sync or verification after replacement | The writer restores the pre-save bytes from the backup. |
| First save fails after replacement | The writer removes the new destination. |
| Recovery itself fails | `ConfigSaveError` reports both save and recovery failure; inspect the destination and backup before retrying. |

Temporary files are removed on handled success and failure paths. The destination and backup are replaced separately, so a failure can leave an older backup while the current destination remains untouched; the table above identifies which file is authoritative.

After a successful update, `config.toml.bak` remains available as the previous complete configuration. It may contain plaintext credentials, so analysts and applications must protect it just like the active file. See [Secrets](../secrets.md#backup-copies) before resetting credentials or saving through a symbolic link.

## Concurrency boundary

Atomic replacement prevents readers from seeing a partly written file, but it does not prevent two editors from overwriting each other's complete changes. If your application can have more than one active editor or worker, allow only one save at a time and reject stale drafts before calling `save_stack_config()`; successful overlapping saves are otherwise last-writer-wins in this release.
