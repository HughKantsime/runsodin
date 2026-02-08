#!/usr/bin/env python3
"""
Update App.jsx NavItem to use industrial left-border active state.
Run on server: python3 forge_sidebar.py
"""

APP_JSX = "/opt/printfarm-scheduler/frontend/src/App.jsx"

with open(APP_JSX, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Update NavItem active style — left amber border instead of bg fill
# ============================================================

old_navitem = """function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  return (
    <NavLink 
      to={to}
      onClick={onClick}
      className={({ isActive }) => clsx(
        collapsed ? 'flex items-center justify-center py-2.5 rounded-lg transition-colors' 
                  : 'flex items-center gap-3 px-4 py-2.5 rounded-lg transition-colors',
      )}
      style={({ isActive }) => isActive
        ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-active-text)' }
        : { color: 'var(--brand-sidebar-text)' }
      }
    >
      <Icon size={20} className="flex-shrink-0" />
      {!collapsed && <span className="font-medium">{children}</span>}
    </NavLink>
  )
}"""

new_navitem = """function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  return (
    <NavLink 
      to={to}
      onClick={onClick}
      className={({ isActive }) => clsx(
        'transition-colors border-l-3',
        collapsed ? 'flex items-center justify-center py-2 rounded' 
                  : 'flex items-center gap-3 px-4 py-2 rounded text-sm',
        isActive ? 'border-l-amber-500' : 'border-l-transparent',
      )}
      style={({ isActive }) => isActive
        ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-active-text)' }
        : { color: 'var(--brand-sidebar-text)' }
      }
    >
      <Icon size={18} className="flex-shrink-0" />
      {!collapsed && <span className="font-medium">{children}</span>}
    </NavLink>
  )
}"""

if old_navitem in content:
    content = content.replace(old_navitem, new_navitem)
    changes += 1
    print("✓ Updated NavItem with left-border active state")

# ============================================================
# 2. Update NavGroup to be more industrial — tighter, uppercase
# ============================================================

old_navgroup_label = """        <span className="text-[10px] uppercase tracking-widest font-semibold"
            style={{ color: 'var(--brand-text-muted)' }}>
            {label}
          </span>"""

new_navgroup_label = """        <span className="text-[9px] uppercase font-mono font-medium" 
            style={{ color: 'var(--brand-text-muted)', letterSpacing: '0.2em' }}>
            {label}
          </span>"""

if old_navgroup_label in content:
    content = content.replace(old_navgroup_label, new_navgroup_label)
    changes += 1
    print("✓ Updated NavGroup label styling")

# Write
if changes > 0:
    with open(APP_JSX, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes to App.jsx")
else:
    print("\n⚠ No changes applied — check if NavItem structure has changed")
