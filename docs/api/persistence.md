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

## Detecting stale drafts

Atomic replacement prevents readers from seeing a partly written file, but it does not stop an editor from overwriting changes saved after it read the file. An editor that holds a draft between reading and saving can pass a revision token to refuse that.

1. Read the config and its token together with `load_stack_config_with_revision()`. Both come from the same bytes, so the token cannot describe a newer file than the config.
2. Edit the draft. Nothing is locked while the draft is open.
3. Save with `save_stack_config(draft, expected_revision=token)`. If the file changed since step 1, it raises `StaleConfigRevisionError` and writes nothing. Reload and retry.

For a file that is missing or cannot be parsed, `stack_config_revision()` returns a token directly. A missing file has a stable token, so a save that expects an absent file is refused if the file appears first. Tokens are opaque: compare them for equality only, and do not store them with the config.

`omop-config configure` and the `add` commands use this, so they refuse to save over changes made while they were running.

::: oa_configurator.loader.load_stack_config_with_revision

::: oa_configurator.loader.stack_config_revision

::: oa_configurator.io.StaleConfigRevisionError

## Concurrent writers

`save_stack_config()` takes an exclusive lock on `config.toml.lock` while it rereads the destination, checks the revision, refreshes the backup, replaces the file and verifies it. Two saves therefore never interleave, and exactly one of several saves that expect the same revision succeeds. A save waits up to `lock_timeout` seconds (10 by default) for another writer and then raises `ConfigSaveError`. The lock is held for the save only, never while a draft is being edited.

`config.toml.lock` is an empty file with mode `0600` that stays beside the config. It is safe to delete when no save is running. The operating system releases the lock if a process dies, so a leftover file never blocks a save.

The guarantee covers writers that use `save_stack_config()`. A text editor or script that writes `config.toml` directly is not excluded.
