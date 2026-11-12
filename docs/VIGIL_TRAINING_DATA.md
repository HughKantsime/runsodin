# Vigil training frames

Vigil can save camera frames from your own prints so that a detection model can later be tuned on your printers. This page says what is saved, what is never saved, and where the files live. Collection is off until you turn on **Collect training data** for a printer in the Vigil settings.

## What is saved

- One frame every 120 seconds while a print runs (not every detection tick), plus up to three frames around each detection: the frame before it, the detection frame and the frame after it.
- If a crop is set for the printer, training frames are saved cropped to it (for example, to the bed only). The crop is four fractions of the picture (left, top, width, height) in the printer's Vigil settings. Detection frames kept for review stay full size.
- Frames are files under `/data/vision_frames` inside the container, with a row per frame in the database.

## What is never saved

- **Controlled work.** No training frame is saved while a controlled job prints. If O.D.I.N. cannot tell, it saves nothing.
- **Models you exclude.** Give a model the category `no-training` and its prints are left out. The category is re-read once a minute, so a change takes up to a minute to apply. A print started at the printer, with no O.D.I.N. job or model, has no category and is collected (controlled work is still refused).
- **When the disk is nearly full.** Collection stops when the volume holding the frames has less than 2 GB free, platform administrators get one alert, and it resumes when space returns. Detection and alerts are not affected.

## Who can see the frames

Platform administrators only, and for controlled work only those inside its audience. Frames never leave your installation: O.D.I.N. does not upload them anywhere.

## How long they are kept

Unlabelled frames follow the Vigil retention setting (30 days by default). Frames you have labelled are kept until you delete them.

## Keeping the frames on another disk or a NAS

Frames add up (a printer that prints all day saves several hundred a day at the default interval). To keep them off the main data volume, mount the other disk or network share **on the host** and bind it over the frames directory in your compose file:

```yaml
services:
  odin:
    volumes:
      - odin-data:/data
      - /mnt/nas/odin-vision-frames:/data/vision_frames
```

- Mount the share on the host with the host's own tools. O.D.I.N. never stores share credentials and never mounts anything itself.
- The directory must be writable by the user the container runs as.
- Move the existing contents of `vision_frames` into the new location before you restart, or the frames already recorded will show as missing.
- The low-disk guard measures the volume the frames are actually on, so it follows the mount.
- If the share is unavailable when the container starts, frames are written to the empty mount point on the host instead. Use your host's mount ordering (for example a systemd mount unit that the Docker service requires) to prevent that.

## Settings that have no screen yet

The sampling interval and the free-space minimum can be changed through the Vigil settings API (`training_capture_interval_sec`, 1 to 86400; `training_min_free_gb`, 0 turns the guard off). The per-printer interval override is API only.
