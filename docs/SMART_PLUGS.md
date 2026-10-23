# Smart plug power commands

Direct Tasmota HTTP manual commands and automatic ON/OFF use the durable 027 power fence. Active or uncertain fences block dispatch until reconciled; they do not expire automatically.

Home Assistant and MQTT plugs retain their existing manual ON/OFF/toggle responses and automatic power behavior under the per-process printer power lock. They are **not cross-process fenced** because their protocols cannot prove command completion. A Home Assistant response or MQTT publication receipt cannot establish that a remote command has finished or cannot execute later. Frontend request nonces are accepted for these plugs without creating a fence or providing durable deduplication.

Dispatch remains available for HA/MQTT printers with no fence rows. Existing active or uncertain fences still block their target, including after a plug configuration change.
