"""Byte limits for print-file storage and inspection."""

MIB = 1024**2
GIB = 1024**3
MAX_COMPRESSED_BYTES = 512 * MIB
MAX_MEMBER_BYTES = GIB
MAX_GCODE_BYTES = 512 * MIB
MAX_TOTAL_BYTES = 2 * GIB
MAX_PLATE_JSON_BYTES = 4 * MIB
EDUCATION_UPLOAD_BYTES = 160 * MIB
EDUCATION_MEMBER_BYTES = 400 * MIB
EDUCATION_TOTAL_BYTES = 500 * MIB
SINGLE_SOURCE_APPROVAL_BYTES = 64 * MIB
DISK_CHECK_THRESHOLD = EDUCATION_UPLOAD_BYTES

UPLOAD_EXPANSION_REFUSALS = {
    "too_large": f"File rejected: decompressed size exceeds the {MAX_TOTAL_BYTES // GIB} GiB total limit.",
    "member_too_large": f"File rejected: decompressed size exceeds the limit of {MAX_MEMBER_BYTES // GIB} GiB per member.",
    "gcode_too_large": f"File rejected: a plate exceeds the {MAX_GCODE_BYTES // MIB} MiB sliced G-code limit.",
}
