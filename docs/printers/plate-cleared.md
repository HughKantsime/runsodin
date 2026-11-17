# Plate cleared

After removing a finished print, open its printer card or printer detail and choose **Plate cleared**. This works in Classic and Foundry, including on phones, for every printer type.

The confirmation says whether a job is waiting. If nothing is waiting, nothing will start. If a job is waiting for the plate, confirming releases that job to start. If you cannot see the job, the confirmation says “A job” and keeps its name private.

A waiting job exists only where the installation's queue puts jobs into the `waiting_plate_clear` state. The standard PostgreSQL job enum does not include that state; clearing a plate there records the finished print's confirmation without releasing a job.

The control appears when the plate needs clearing and you have permission to edit printers. Viewers can see **Plate not cleared yet**. While the printer is printing or paused, the button is hidden. After ODIN records the confirmation, the card shows **Plate cleared** and the time. A later print makes the earlier confirmation no longer current.

ODIN shows a refusal as returned by the server and does not retry automatically. **Plate is clear, start next** remains a separate control; use **Start next** for an attended Kobra S1 start.

Home Assistant also has a **Plate cleared** button and **Plate needs clearing** binary sensor. If a job is waiting for the plate, pressing this releases it to start. The button needs the `write:printers` token scope and `printers.edit` permission. A successful press refreshes the status immediately.

## Known limits

A job row carrying the retired status `waiting_plate_clear` (possible only in data from old versions) is not supported by the jobs list.
