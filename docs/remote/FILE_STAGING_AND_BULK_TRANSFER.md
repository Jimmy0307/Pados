# File Staging and Bulk Transfer

## Remote upload

`POST /remote/v1/uploads?project_id=&name=&classification=` with the raw bytes as the body.

* Names must be plain file names. `..`, `/`, `\`, absolute paths, drive letters,
  and Windows device names (`CON`, `NUL`, `COM1`, and so on) are rejected.
* Bytes are stored under a fixed name inside a PADOS-owned staging directory,
  never in a project workspace. The client-chosen name is metadata only.
* Each file records SHA-256, size, classification, project binding, and a Source
  Identity (channel, actor, original name, received time).
* State is `STAGED` or `QUARANTINED`. A file is quarantined when its classification
  is above the project ceiling, it has an executable content type, or the malware
  scan fails.
* When no scanner is configured, the scan status is `MALWARE_SCAN_NOT_CONFIGURED`.
  This is visible in the record and in `/remote/v1/home-base`.
* Staged files are referenced from intents as `staged:<id>`. Quarantined files are refused.

## Bulk transfer boundary

**SFTP (or any external transport) moves bytes; PADOS assigns meaning.**
PADOS embeds no FTP or SFTP server. Operators run a standard SFTP service on the
Home Base that drops files into `inbox_root`. `pados.remote.staging.BulkInbox.register`
then admits a named inbox file (regular files only, no symlinks, no traversal)
through the same staging pipeline with channel `BULK_EXTERNAL_TRANSFER`.

Status: `BULK_SFTP_BOUNDARY=CONTRACT_READY_EXTERNAL_TRANSPORT`.
