# Isolated Education printer pilot

Updated October 9, 2026. The owner’s isolated Kubernetes pilot is deployed in odin-edu-pilot on kube3. The pinned v1.9.24 image passed all ten release gates; separate Education activation, initial admin setup and physical printer acceptance remain pending. The Compose instructions below remain an alternative setup. Do not copy production data or credentials.

Use a separate Linux Docker VM on the owner's infrastructure, with its own IP and storage. A separate Compose project on a chosen existing Docker host is possible if approved, with enforceable CPU/memory limits and verified disk capacity before startup. Suggested starting allocation: 2 vCPU, 4 GiB RAM, and 40 GiB disk with at least 15 GiB free on the actual /data filesystem. This is a rehearsal allocation, not a measured capacity guarantee. The default upload headroom reserve is 10 GiB and a 10% filesystem floor, so verify available space after pulling the image and creating volumes.

## Isolation and access

Before starting, inspect Docker Compose projects and confirm odin-edu-pilot is unused. Stop if that name or its volumes already exist until ownership is established. Copy pilot-compose.yml into a new pilot-only directory. Do not copy production .env, database, identity files, license, printer credentials, notifications or volumes. Container startup creates separate persisted encryption/JWT keys. Docker project name odin-edu-pilot keeps volume/network names separate. No container_name override or Watchtower deployment is introduced. Do not run any volume deletion command during upgrade or rollback.

The UI binds loopback on port 18000 by default. Use an SSH tunnel from the MacBook, or set PILOT_BIND_IP to the VM's LAN IP, PILOT_ORIGIN to its exact HTTP origin and PILOT_TRUSTED_HOSTS to its IP/hostname. Permit UI access only from the test client network; do not expose it publicly. Camera streaming ports are deliberately not published for the first print exercise. For any access beyond trusted local test networks, use HTTPS and secure cookies before entering real credentials.

From the chosen VM, allow outgoing printer MQTT TCP 8883, implicit FTPS TCP 990 and the passive data ports advertised by that printer. Obtain the actual passive range from device/network evidence rather than assume one. Verify from the pilot container network, not solely from a laptop. Online image pull and license activation also need DNS and HTTPS access to GHCR and runsodin.com respectively. Offline license activation is available through the existing supported proof-of-possession flow if required. No firewall change is implied by this guide.

## Bring up and activate

On the selected Docker host, after confirming this is the pilot directory:

```sh
docker compose -f pilot-compose.yml config --quiet
docker compose -f pilot-compose.yml pull
docker compose -f pilot-compose.yml up -d
```

Check /health and /health/ready and the exact running image identity. Complete the normal first-run setup with a new pilot administrator. Issue a separate internal Education evaluation entitlement using the existing ODIN license service and activate it through the supported application flow. It must report Education plus education_workflows. Do not reuse CTEC's entitlement or patch the deployed license verifier. Keep license keys/access codes in the normal secure UI or secret files; never in this guide, recordings, chat or reports.

Enable Education mode as the installation superadmin. Create a fictional school organization, a class/club, and separate student, teacher and operator accounts. Give the student a student grant and the teacher a manager grant on that class. Assign only the designated printer to the pilot organization and grant it to the class. Match its model, nozzle, bed and loaded material to a genuine sliced file. A teacher's review permission and an operator's scheduler/dispatch permission are distinct.

## Owner's physical preparation

1. Designate the printer and record model/firmware. Make it idle, load appropriate filament and ensure its bed is clear. Keep it supervised during active tests.
2. Stop production dispatch to this printer for the test window. If production ODIN manages it, remove it from automatic scheduling/dispatch using supported controls and verify no queued jobs can target it. We will determine the precise action with the owner rather than change production blindly.
3. Confirm printer LAN access and enter IP, serial and access code directly into the pilot. Do not send its access code in chat.
4. Slice a small printable model in OrcaSlicer for this exact printer/nozzle/material; export sliced .3mf. Use separate browser profiles or private contexts for student, teacher and operator.

## Acceptance record

Record image/version, printer model/firmware, slicer version, case, timestamps and actual results. Use synthetic users; keep secrets out of screenshots/recordings.

- Student uploads and selects the class; teacher sees the submission; another student cannot see it.
- Teacher previews compatible printer and approves. Student cannot schedule or dispatch. Operator schedules; only authorized printer is assigned.
- Operator dispatches once. Printer receives exact file, starts, and ODIN shows correct submission progress and completion.
- Printer unavailable produces a truthful error. Restore it and explicitly retry; one physical print, no duplicate commands.
- Cancel through supported controls; both printer and submission settle correctly. Test supervised interruption/reconnect on a disposable job.
- Restart pilot while idle, then during a supervised test print. Existing users/settings/submission links survive; no automatic duplicate start.
- Back up stopped pilot volumes including database, uploads, installation identity and keys; restore to an isolated stopped copy before any upgrade rehearsal. Keep the restored copy disconnected from both the printer and license service while verifying its preserved installation/device identity. Never run both copies against the printer concurrently. Verify rollback and restore instead of deleting data.

A passing local rehearsal validates this build and printer configuration. School firmware, VM and VLAN confirmation remains separate. Do not call the whole application or every supported printer certified based on this checklist.
