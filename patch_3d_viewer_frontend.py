#!/usr/bin/env python3
"""
Patch Models.jsx to add 3D viewer button on model cards.
Adds a Box icon button that opens ModelViewer modal.
"""

MODELS_PATH = "/opt/printfarm-scheduler/frontend/src/pages/Models.jsx"

with open(MODELS_PATH, "r") as f:
    content = f.read()

changes = []

# 1. Add import for ModelViewer and Box icon
# Find existing lucide import and add Box if not there
if "Box" not in content.split("from 'lucide-react'")[0].split("import")[-1]:
    # Add Box to the lucide imports
    old_lucide = content.split("from 'lucide-react'")[0].split("import {")[-1]
    if "Box" not in old_lucide:
        # Find the lucide import line and add Box
        import re
        lucide_match = re.search(r"(import \{[^}]+)(} from 'lucide-react')", content)
        if lucide_match:
            old_import = lucide_match.group(0)
            icons = lucide_match.group(1)
            if "Box" not in icons:
                new_import = icons.rstrip().rstrip(',') + ", Box } from 'lucide-react'"
                content = content.replace(old_import, new_import, 1)
                changes.append("Added Box to lucide imports")

# 2. Add ModelViewer import
if "ModelViewer" not in content:
    # Add after the last import
    import_marker = "from 'lucide-react'"
    idx = content.index(import_marker) + len(import_marker)
    # Find end of that line
    next_newline = content.index('\n', idx)
    content = content[:next_newline+1] + "import ModelViewer from '../components/ModelViewer'\n" + content[next_newline+1:]
    changes.append("Added ModelViewer import")

# 3. Add viewer state to the Models page component
# Find the main component function and add state
if "viewerModelId" not in content:
    # Find where other useState calls are in the main component
    # Look for the first useState in the default export or main function
    state_patterns = [
        "const [editModel, setEditModel] = useState",
        "const [deleteModel, setDeleteModel] = useState",
        "const [scheduleModel, setScheduleModel] = useState",
        "const [filter, setFilter] = useState",
        "const [search, setSearch] = useState",
    ]
    
    inserted = False
    for pattern in state_patterns:
        if pattern in content:
            idx = content.index(pattern)
            line_end = content.index('\n', idx)
            content = content[:line_end+1] + "  const [viewerModelId, setViewerModelId] = useState(null)\n  const [viewerModelName, setViewerModelName] = useState('')\n" + content[line_end+1:]
            changes.append("Added viewerModelId state")
            inserted = True
            break
    
    if not inserted:
        print("  ❌ Could not find state insertion point")

# 4. Add 3D View button to ModelCard
# Look for the existing button row in ModelCard (edit/delete/schedule buttons)
if "View 3D" not in content and "view3d" not in content.lower():
    # Find the schedule/edit/delete buttons area in ModelCard
    # Look for onSchedule button
    schedule_patterns = [
        "onSchedule(model)",
        "onSchedule && ",
        "Schedule",
    ]
    
    # Try to find a button row — look for the trash/edit icons area
    # Pattern: find where action buttons are rendered in ModelCard
    import re
    
    # Look for the function signature to understand the props
    card_match = re.search(r'function ModelCard\(\{([^}]+)\}\)', content)
    if card_match:
        props = card_match.group(1)
        if 'onView3D' not in props:
            new_props = props.rstrip() + ', onView3D'
            content = content.replace(card_match.group(0), f'function ModelCard({{ {new_props} }})', 1)
            changes.append("Added onView3D prop to ModelCard")
    
    # Find the button area — look for delete button with Trash icon
    trash_match = re.search(r'(onClick=\{.*?onDelete.*?\}[^>]*>.*?(?:Trash|trash).*?</button>)', content, re.DOTALL)
    if trash_match:
        # Insert 3D view button before the delete button's parent or after edit
        # Simpler: find the div containing action buttons
        pass
    
    # Alternative approach: find the model card action buttons and add our button
    # Look for a pattern like: className="flex ... gap-
    # that contains onEdit or onDelete
    
    # Let's find where onSchedule is called and add the 3D button nearby
    if 'onSchedule(model)' in content:
        # Add a 3D view button near the schedule button
        old_schedule = None
        # Find the schedule button JSX
        schedule_btn_match = re.search(
            r'(<button[^>]*onClick=\{[^}]*onSchedule\(model\)[^}]*\}[^>]*>.*?</button>)',
            content, re.DOTALL
        )
        if schedule_btn_match:
            old_btn = schedule_btn_match.group(0)
            view3d_btn = '''<button onClick={() => onView3D && onView3D(model)} className="p-1.5 bg-farm-700 hover:bg-farm-600 rounded text-amber-400 hover:text-amber-300" title="View 3D model"><Box size={14} /></button>
            '''
            content = content.replace(old_btn, view3d_btn + old_btn, 1)
            changes.append("Added 3D view button to ModelCard")

# 5. Pass onView3D to ModelCard instances
if "onView3D" in content and "onView3D={" not in content.split("<ModelCard")[1] if "<ModelCard" in content else "":
    # Find ModelCard usage and add prop
    modelcard_usage = re.search(r'(<ModelCard\s+key=\{[^}]+\}[^/]*?)(/\s*>)', content)
    if modelcard_usage:
        old_usage = modelcard_usage.group(0)
        props_part = modelcard_usage.group(1)
        if 'onView3D' not in props_part:
            new_usage = props_part + ' onView3D={(m) => { setViewerModelId(m.id); setViewerModelName(m.name) }} ' + modelcard_usage.group(2)
            content = content.replace(old_usage, new_usage, 1)
            changes.append("Passed onView3D to ModelCard instance")

# 6. Add ModelViewer modal render at bottom of component return
if "<ModelViewer" not in content:
    # Find the closing of the main return — look for the last </div> before the export or end
    # Add the modal just before the last closing fragment or div
    
    # Find the component's return statement end
    # Look for viewerModelId conditional render spot
    # Safest: add before the final closing tag of the return
    
    viewer_jsx = '''
      {viewerModelId && (
        <ModelViewer
          modelId={viewerModelId}
          modelName={viewerModelName}
          onClose={() => { setViewerModelId(null); setViewerModelName('') }}
        />
      )}'''
    
    # Find the last </div> that closes the page
    # Count from end
    last_return_close = content.rfind('</div>')
    if last_return_close > 0:
        # Find the line
        content = content[:last_return_close] + viewer_jsx + '\n    ' + content[last_return_close:]
        changes.append("Added ModelViewer modal render")

with open(MODELS_PATH, "w") as f:
    f.write(content)

print("=" * 60)
print("  O.D.I.N. — 3D Viewer Frontend Patch")
print("=" * 60)
for c in changes:
    print(f"  ✅ {c}")
if not changes:
    print("  ⚠️  No changes applied")
print()
print("  Build: cd /opt/printfarm-scheduler/frontend && npm run build")
print("=" * 60)
