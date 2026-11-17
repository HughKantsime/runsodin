# Settle filament

Open **Print Archive**, filter by **Needs filament settled**, open a print and choose **Settle filament**. Operators and administrators have this permission by default; it can be withheld separately in Settings.

Enter the spool or spools that fed the print and the total grams used. The suggested spools come from the saved start record, never today's loaded spools. Measured grams are preferred to a sliced estimate. Review every manual use recorded against the same job: say whether it was for this print. When counted, that use is subtracted from the amount deducted now. Enter the full total even if all of it was already recorded by hand; a zero additional deduction still closes the print.

Choose **No further deduction** when nothing was extruded, the use was recorded by hand without a matching job row, the spool is not tracked, or its balance already includes the print. A counted manual row requires a spool and total grams, so use **Deduct filament** for that case. If ODIN already deducted this print, the only available action is **Keep the existing deduction**; additional manual use cannot be counted in that action.

If a spool was weighed or measured after the print, ODIN defaults to no further deduction. Explicitly confirm **Deduct anyway** only when that balance does not already include the use you are recording. Unknown balances remain unknown: settling records use but does not invent a balance. A known balance below the amount used becomes zero and the record asks you to weigh the spool.

Settlements show who recorded them and when, subject to the reader's access. They do not change print status, times, ownership or printer-reported grams. The archive's cost calculation uses the full entered grams, with the purchase evidence saved at print start. The separately retained measured-cost records are not recalculated.

Imported history without a physical print, Education-marked records, and ownership conflicts on an installation with organizations cannot be settled here. On an installation without organizations, unresolved ownership can be settled when no Education marker exists. Automatic accounting for new printer-started prints is unchanged.

The dashboard summarizes unresolved visible prints finished in the last 30 days. Unresolved does not necessarily mean nothing was deducted: another accounting path may already have recorded the use. The settlement preview checks that evidence.

Spoolman receives only grams newly deducted by the settlement, through the existing delivery queue. Previously recorded manual use is not sent again. No new network integration is involved. Migration 061 retains filament evidence with the physical print. Database setup checks past usage before ODIN starts; records it cannot establish require review. Job, spool and archive deletion do not erase that evidence.

Historical weighing that increased, preserved, or established an unknown balance did not retain a timestamp in older versions. Those readings cannot be detected retrospectively. New weigh-ins retain a timestamp, and existing downward weigh adjustments and AMS measurement timestamps are checked.

After running an older version on an upgraded database, review unresolved prints from that period before settling them.
