export function fixtureResponse(values, key, role, defaultPermissions) {
  if (!Object.hasOwn(values, key)) {
    return { declared: false, status: 599, body: { error: 'undeclared accessibility fixture endpoint' } }
  }
  const declared = JSON.parse(JSON.stringify(values[key]), (_key, value) => value === '$now' ? new Date().toISOString() : value)
  if (declared?.$dynamic === 'current_user') {
    return { declared: true, status: 200, body: { id: 1, username: `${role}@school.test`, email: `${role}@school.test`, role, group_id: 1 } }
  }
  if (declared?.$dynamic === 'permissions') {
    // The server's default matrix (core/rbac_policy.py); 2.0 pages enforce every page key,
    // so a partial matrix would redirect personas away from the pages under audit.
    return { declared: true, status: 200, body: defaultPermissions }
  }
  return { declared: true, status: declared?.$status ?? 200, body: declared != null && Object.hasOwn(declared, '$body') ? declared.$body : declared }
}
