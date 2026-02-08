#!/usr/bin/env python3
"""
Edit models.py to add job approval workflow fields.
Run on server: python3 edit_models_approval.py

Changes:
1. Add JOB_SUBMITTED, JOB_APPROVED, JOB_REJECTED to AlertType enum
2. Add submitted_by, approved_by, approved_at, rejected_reason to Job model

IMPORTANT: This script needs to find the exact strings in your models.py.
If the replacements fail, the script will tell you what to add manually.
"""

import sys

MODELS_PY = "/opt/printfarm-scheduler/backend/models.py"

with open(MODELS_PY, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Add new AlertType enum values
# ============================================================

# Look for the last AlertType value and add after it
# Common pattern: MAINTENANCE_OVERDUE or HMS_ERROR is the last value
for last_value in ["HMS_ERROR", "MAINTENANCE_OVERDUE", "PRINTER_OFFLINE", "SPOOL_LOW"]:
    marker = f'{last_value} = "{last_value.lower()}"'
    if marker in content:
        new_values = f'''{marker}
    JOB_SUBMITTED = "job_submitted"
    JOB_APPROVED = "job_approved"
    JOB_REJECTED = "job_rejected"'''
        if "JOB_SUBMITTED" not in content:
            content = content.replace(marker, new_values)
            changes += 1
            print(f"✓ Added JOB_SUBMITTED/APPROVED/REJECTED to AlertType (after {last_value})")
        else:
            print("· AlertType already has JOB_SUBMITTED")
        break
else:
    # Try alternate enum format
    if "class AlertType" in content and "JOB_SUBMITTED" not in content:
        print("✗ Found AlertType class but couldn't match enum value format.")
        print("  MANUAL EDIT: Add these values to the AlertType enum:")
        print('    JOB_SUBMITTED = "job_submitted"')
        print('    JOB_APPROVED = "job_approved"')
        print('    JOB_REJECTED = "job_rejected"')
    elif "JOB_SUBMITTED" in content:
        print("· AlertType already has JOB_SUBMITTED")
    else:
        print("✗ Could not find AlertType enum in models.py")

# ============================================================
# 2. Add approval columns to Job model
# ============================================================

# Look for existing Job columns to find insertion point
# We'll add after order_item_id or quantity_on_bed (the last added columns)
if "submitted_by" in content:
    print("· Job model already has submitted_by column")
else:
    # Try to find a good insertion point - look for common late-added columns
    for marker_col in [
        "quantity_on_bed = Column(Integer",
        "order_item_id = Column(Integer",
        "suggested_price = Column(Float",
        "estimated_cost = Column(Float",
    ]:
        if marker_col in content:
            # Find the full line
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if marker_col in line:
                    # Insert after this line
                    indent = "    "  # standard SQLAlchemy model indent
                    new_cols = f'''
{indent}# Job approval workflow (v0.18.0)
{indent}submitted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
{indent}approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
{indent}approved_at = Column(DateTime, nullable=True)
{indent}rejected_reason = Column(Text, nullable=True)'''
                    lines.insert(i + 1, new_cols)
                    content = "\n".join(lines)
                    changes += 1
                    print(f"✓ Added approval columns to Job model (after {marker_col[:30]}...)")
                    break
            break
    else:
        print("✗ Could not find insertion point in Job model.")
        print("  MANUAL EDIT: Add these columns to the Job class:")
        print('    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=True)')
        print('    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)')
        print("    approved_at = Column(DateTime, nullable=True)")
        print("    rejected_reason = Column(Text, nullable=True)")

# ============================================================
# 3. Write the file
# ============================================================

if changes > 0:
    with open(MODELS_PY, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes to models.py")
else:
    print("\n⚠ No changes applied. Check the errors above and apply manually if needed.")
