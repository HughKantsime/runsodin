#!/usr/bin/env python3
"""
Edit frontend/src/api.js to add approval workflow API calls.
Run on server: python3 edit_api_js_approval.py
"""

API_JS = "/opt/printfarm-scheduler/frontend/src/api.js"

with open(API_JS, "r") as f:
    content = f.read()

# Add before the last line or at the end of the file
# Find a good insertion point - look for the last export function

APPROVAL_API = '''

// === Job Approval Workflow (v0.18.0) ===

export async function approveJob(jobId) {
  return fetchAPI(`/api/jobs/${jobId}/approve`, { method: 'POST' });
}

export async function rejectJob(jobId, reason) {
  return fetchAPI(`/api/jobs/${jobId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export async function resubmitJob(jobId) {
  return fetchAPI(`/api/jobs/${jobId}/resubmit`, { method: 'POST' });
}

export async function getApprovalSetting() {
  return fetchAPI('/api/config/require-job-approval');
}

export async function setApprovalSetting(enabled) {
  return fetchAPI('/api/config/require-job-approval', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
}
'''

if "approveJob" in content:
    print("· api.js already has approveJob function")
else:
    content = content.rstrip() + "\n" + APPROVAL_API
    with open(API_JS, "w") as f:
        f.write(content)
    print("✓ Added approval API functions to api.js")
