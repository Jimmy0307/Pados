# BACKUP_RESTORE

## Backup

```powershell
.\scripts\backup.ps1
```

The script writes a PostgreSQL custom-format dump under `runtime\artifacts\backups` and records the latest path in `runtime\logs\last_backup_path.txt`.

## Restore Smoke

```powershell
.\scripts\restore.ps1 -BackupPath runtime\artifacts\backups\<dump>.dump
```

By default restore targets `pados_restore_smoke`. The script refuses to restore over the main `pados` database unless `-AllowOverwriteProduction` is provided.

