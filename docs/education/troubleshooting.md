# Guided troubleshooting

Open **Settings > System > Troubleshooting** as a server administrator (admin without an organization restriction).

1. **Check server** checks the database, application version, supervised services, the actual Education upload filesystem and reserve, and a temporary write/read when the directory exists. A missing directory or unavailable supervisor status is shown as unknown, not a pass.
2. Choose a saved printer and **Check printer**. Bambu checks authenticated status and FTPS TLS/authentication/protected transfer separately. Moonraker and PrusaLink query their status endpoint using the saved credentials. Unsupported protocols are labeled explicitly.
3. For a Bambu LAN printer, authorize **Test file transfer**. ODIN uploads a uniquely named small `.txt`, retrieves and compares its bytes, then deletes only that file using a fresh authenticated connection. It never sends a print command. A cleanup failure names the diagnostic file to remove manually. Existing self-signed Bambu LAN certificate policy is preserved.
4. Describe the problem without personal information or credentials. Recent logs are optional. **Preview support report**, review all included content, then **Download report**.
5. **Open email draft** opens the user's mail app addressed to ODIN Support. Attach the downloaded JSON yourself. ODIN does not send an email, attach files automatically, or upload reports to an external service.

Report results carry timestamps and diagnostic IDs. Earlier printer checks keep their own timestamps and are cleared when selecting a different printer. Changing the problem description or log choice clears the preview so it must be regenerated.

Checks do not establish that a physical print started or completed. Log filtering covers common credential patterns but cannot guarantee removal of arbitrary personal details; review before sharing. Preserve existing data, volumes and encryption keys during support.
