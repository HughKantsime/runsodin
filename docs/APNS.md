# Native iOS alert delivery

ODIN uses APNs environment configuration. Configure `APNS_KEY_ID` (Apple key ID),
`APNS_TEAM_ID` (Apple Developer team ID), and `APNS_KEY_PATH` (a readable mounted
`.p8` signing-key file). `APNS_KEY_CONTENT` is an alternative inline key source;
when supplied, it takes precedence over the file. Keep private key material out
of the repository and logs.

Set `APNS_BUNDLE_ID` to the installed app's bundle identifier. The default for the
ODIN iOS app is `com.jlls.odin`. A mismatched topic prevents delivery. Production
registrations use Apple's production endpoint; `apns-sandbox` registrations use
its sandbox endpoint. ITAR mode disables APNs.

Restart the backend after configuration. Startup logs one warning if registered
devices exist and APNs is unconfigured. Alert dispatch remains best effort after
commit; it does not retry failed APNs requests. Terminal outbox alerts use that
same delivery hook once, including replay of previously unclaimed outbox rows.

## Diagnostic push

An authenticated admin can `POST /api/push/test` (also `/api/v1/push/test`). No body
is required. This sends one generic diagnostic push to each of the caller's own
registered devices, bypassing quiet hours and category preferences. It contains
no alert or Education data. A caller with no devices receives empty results.

Example response:

```json
{"sent":1,"total_devices":2,"devices":[{"device_id":1,"status":"sent"},{"device_id":2,"status":"topic_mismatch"}]}
```

`device_id` is the server registration row ID, not the token or client-supplied
identifier. Status is `sent`, `not_configured`, `bad_device_token`,
`topic_mismatch`, `auth_error`, or `other`. Raw APNs responses and signing material
are never returned or logged. A `sent` response means Apple accepted the request;
confirm receipt on the phone after release and configuration.

## Operational alerts

Push recipients must be active, within resource scope, selected for the alert's
in-app delivery, and currently opted into in-app notifications. Device category
settings and quiet hours still apply. Education mode, Education-owned jobs, and
active Education printer jobs suppress native alert delivery. The diagnostic
endpoint always uses generic copy.

Titles use the alert title; bodies use the first 180 characters of the alert
message after credential redaction. Categories match the iOS app: printer errors
use `HMS_ERROR`, detachment uses `SPAGHETTI_DETECTED`, and submissions use
`JOB_APPROVAL_REQUIRED`. Types without an iOS category are allowed by default.

Unread, undismissed spool alerts suppress subsequent alerts for that user and
spool without a time limit. Printer errors suppress the same `[source:code]`
identity (or full message without a prefix) for six hours per user/printer.
Reading or dismissing the old alert permits another. Fully deduplicated events
send nothing externally. Legacy uppercase alert/preference rows are supported;
canonical lowercase preferences take precedence if both exist. No data cleanup
or schema migration is required.
