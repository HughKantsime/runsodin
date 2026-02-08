#!/usr/bin/env python3
"""
Add 3D Model Viewer to O.D.I.N.
- Extracts mesh geometry from .3mf files during upload
- Stores compressed vertex/triangle data in print_files.mesh_data
- Adds GET /api/print-files/{id}/mesh endpoint
- Adds GET /api/models/{id}/mesh endpoint (looks up via print_file_id)
"""

MAIN_PATH = "/opt/printfarm-scheduler/backend/main.py"
PARSER_PATH = "/opt/printfarm-scheduler/backend/threemf_parser.py"

# ============================================================
# 1. Add mesh extraction function to threemf_parser.py
# ============================================================

print("=" * 60)
print("  O.D.I.N. — 3D Model Viewer Backend")
print("=" * 60)
print()

# Read parser
with open(PARSER_PATH, "r") as f:
    parser_content = f.read()

mesh_extract_func = '''

def extract_mesh_from_3mf(file_path: str) -> Optional[dict]:
    """
    Extract mesh geometry (vertices + triangles) from a .3mf file.
    Returns a dict with 'vertices' (flat [x,y,z,x,y,z,...]) and
    'triangles' (flat [v1,v2,v3,v1,v2,v3,...]) for Three.js BufferGeometry.
    Keeps only every Nth vertex for large models to stay under ~500KB.
    """
    import xml.etree.ElementTree as ET
    
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Find the 3D model file
            model_path = None
            for name in zf.namelist():
                if name.lower().endswith('.model') and '3d/' in name.lower():
                    model_path = name
                    break
            
            if not model_path:
                # Try root-level .model file
                for name in zf.namelist():
                    if name.lower().endswith('.model'):
                        model_path = name
                        break
            
            if not model_path:
                return None
            
            xml_data = zf.read(model_path).decode('utf-8')
            
            # Parse XML — handle namespace
            # 3MF uses namespace: http://schemas.microsoft.com/3dmanufacturing/core/2015/02
            ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            root = ET.fromstring(xml_data)
            
            all_vertices = []
            all_triangles = []
            vertex_offset = 0
            
            # Find all mesh elements (could be multiple objects)
            meshes = root.findall('.//m:mesh', ns)
            if not meshes:
                # Try without namespace (some files don't use it)
                meshes = root.findall('.//mesh')
            
            for mesh in meshes:
                # Extract vertices
                vertices_elem = mesh.find('m:vertices', ns)
                if vertices_elem is None:
                    vertices_elem = mesh.find('vertices')
                if vertices_elem is None:
                    continue
                
                local_verts = []
                for v in vertices_elem.findall('m:vertex', ns):
                    local_verts.extend([
                        float(v.get('x', 0)),
                        float(v.get('y', 0)),
                        float(v.get('z', 0))
                    ])
                if not local_verts:
                    for v in vertices_elem.findall('vertex'):
                        local_verts.extend([
                            float(v.get('x', 0)),
                            float(v.get('y', 0)),
                            float(v.get('z', 0))
                        ])
                
                # Extract triangles
                triangles_elem = mesh.find('m:triangles', ns)
                if triangles_elem is None:
                    triangles_elem = mesh.find('triangles')
                if triangles_elem is None:
                    continue
                
                local_tris = []
                for t in triangles_elem.findall('m:triangle', ns):
                    local_tris.extend([
                        int(t.get('v1', 0)) + vertex_offset,
                        int(t.get('v2', 0)) + vertex_offset,
                        int(t.get('v3', 0)) + vertex_offset
                    ])
                if not local_tris:
                    for t in triangles_elem.findall('triangle'):
                        local_tris.extend([
                            int(t.get('v1', 0)) + vertex_offset,
                            int(t.get('v2', 0)) + vertex_offset,
                            int(t.get('v3', 0)) + vertex_offset
                        ])
                
                all_vertices.extend(local_verts)
                all_triangles.extend(local_tris)
                vertex_offset += len(local_verts) // 3
            
            if not all_vertices or not all_triangles:
                return None
            
            num_verts = len(all_vertices) // 3
            num_tris = len(all_triangles) // 3
            
            # Decimation for large models — keep mesh under ~500KB JSON
            MAX_TRIANGLES = 50000
            if num_tris > MAX_TRIANGLES:
                # Simple decimation: take every Nth triangle
                step = (num_tris // MAX_TRIANGLES) + 1
                decimated_tris = []
                used_verts = set()
                for i in range(0, len(all_triangles), step * 3):
                    if i + 2 < len(all_triangles):
                        v1, v2, v3 = all_triangles[i], all_triangles[i+1], all_triangles[i+2]
                        decimated_tris.extend([v1, v2, v3])
                        used_verts.update([v1, v2, v3])
                
                # Remap vertices to compact array
                vert_map = {}
                compact_verts = []
                new_idx = 0
                for old_idx in sorted(used_verts):
                    vert_map[old_idx] = new_idx
                    base = old_idx * 3
                    if base + 2 < len(all_vertices):
                        compact_verts.extend(all_vertices[base:base+3])
                        new_idx += 1
                
                remapped_tris = [vert_map.get(t, 0) for t in decimated_tris]
                all_vertices = compact_verts
                all_triangles = remapped_tris
                num_verts = len(all_vertices) // 3
                num_tris = len(all_triangles) // 3
            
            # Round floats to reduce JSON size
            all_vertices = [round(v, 3) for v in all_vertices]
            
            return {
                "vertices": all_vertices,
                "triangles": all_triangles,
                "vertex_count": num_verts,
                "triangle_count": num_tris
            }
    
    except Exception as e:
        print(f"Error extracting mesh: {e}")
        return None
'''

if "extract_mesh_from_3mf" not in parser_content:
    parser_content += mesh_extract_func
    with open(PARSER_PATH, "w") as f:
        f.write(parser_content)
    print("[1/4] ✅ Added extract_mesh_from_3mf to threemf_parser.py")
else:
    print("[1/4] ⚠️  extract_mesh_from_3mf already exists in parser")


# ============================================================
# 2. Patch upload endpoint to extract and store mesh
# ============================================================

with open(MAIN_PATH, "r") as f:
    main_content = f.read()

changes = []

# 2a. Update import to include extract_mesh_from_3mf
old_import = "from threemf_parser import parse_3mf, extract_objects_from_plate"
new_import = "from threemf_parser import parse_3mf, extract_objects_from_plate, extract_mesh_from_3mf"

if "extract_mesh_from_3mf" not in main_content:
    main_content = main_content.replace(old_import, new_import)
    changes.append("Updated threemf_parser import")

# 2b. Add mesh extraction after object extraction in upload handler
old_objects = """        # Extract objects for quantity counting
        import zipfile
        with zipfile.ZipFile(tmp_path, 'r') as zf:
            plate_objects = extract_objects_from_plate(zf)"""

new_objects = """        # Extract objects for quantity counting
        import zipfile
        with zipfile.ZipFile(tmp_path, 'r') as zf:
            plate_objects = extract_objects_from_plate(zf)
        
        # Extract 3D mesh for viewer
        mesh_data = extract_mesh_from_3mf(tmp_path)
        mesh_json = json_lib.dumps(mesh_data) if mesh_data else None"""

if "Extract 3D mesh for viewer" not in main_content:
    main_content = main_content.replace(old_objects, new_objects)
    changes.append("Added mesh extraction to upload handler")

# 2c. Add mesh_data to the INSERT statement
old_insert_cols = """                filename, project_name, print_time_seconds, total_weight_grams,
                layer_count, layer_height, nozzle_diameter, printer_model,
                supports_used, bed_type, filaments_json, thumbnail_b64"""
new_insert_cols = """                filename, project_name, print_time_seconds, total_weight_grams,
                layer_count, layer_height, nozzle_diameter, printer_model,
                supports_used, bed_type, filaments_json, thumbnail_b64, mesh_data"""

old_insert_vals = """                :filename, :project_name, :print_time_seconds, :total_weight_grams,
                :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                :supports_used, :bed_type, :filaments_json, :thumbnail_b64"""
new_insert_vals = """                :filename, :project_name, :print_time_seconds, :total_weight_grams,
                :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                :supports_used, :bed_type, :filaments_json, :thumbnail_b64, :mesh_json"""

if "mesh_data" not in main_content.split("INSERT INTO print_files")[1].split(")")[0] if "INSERT INTO print_files" in main_content else "":
    main_content = main_content.replace(old_insert_cols, new_insert_cols, 1)
    main_content = main_content.replace(old_insert_vals, new_insert_vals, 1)
    changes.append("Added mesh_data to print_files INSERT")

# 2d. Add mesh_json to the params dict
old_thumb_param = """            "thumbnail_b64": metadata.thumbnail_b64
        })"""
new_thumb_param = """            "thumbnail_b64": metadata.thumbnail_b64,
            "mesh_json": mesh_json
        })"""

if '"mesh_json": mesh_json' not in main_content:
    main_content = main_content.replace(old_thumb_param, new_thumb_param, 1)
    changes.append("Added mesh_json parameter to INSERT")

# 2e. Add has_mesh to upload response
old_response_objects = '''            "objects": plate_objects
        }'''
new_response_objects = '''            "objects": plate_objects,
            "has_mesh": mesh_data is not None
        }'''

if '"has_mesh"' not in main_content:
    main_content = main_content.replace(old_response_objects, new_response_objects, 1)
    changes.append("Added has_mesh to upload response")

# ============================================================
# 3. Add mesh API endpoints
# ============================================================

mesh_endpoints = '''

# ============== 3D Model Viewer ==============

@app.get("/api/print-files/{file_id}/mesh", tags=["3D Viewer"])
async def get_print_file_mesh(file_id: int, db: Session = Depends(get_db)):
    """Get mesh geometry data for 3D viewer from a print file."""
    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": file_id}).fetchone()
    
    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available for this file")
    
    import json as json_stdlib
    return json_stdlib.loads(result[0])


@app.get("/api/models/{model_id}/mesh", tags=["3D Viewer"])
async def get_model_mesh(model_id: int, db: Session = Depends(get_db)):
    """Get mesh geometry for a model (via its linked print_file)."""
    # Find print_file_id from model
    model = db.execute(text(
        "SELECT print_file_id FROM models WHERE id = :id"
    ), {"id": model_id}).fetchone()
    
    if not model or not model[0]:
        raise HTTPException(status_code=404, detail="Model has no linked print file")
    
    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": model[0]}).fetchone()
    
    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available")
    
    import json as json_stdlib
    return json_stdlib.loads(result[0])
'''

if "/api/models/{model_id}/mesh" not in main_content:
    # Add before the final section or at end
    # Find a good insertion point — after print-files section
    insert_marker = "# ============== Maintenance"
    if insert_marker in main_content:
        main_content = main_content.replace(insert_marker, mesh_endpoints + "\n" + insert_marker)
        changes.append("Added /api/print-files/{id}/mesh endpoint")
        changes.append("Added /api/models/{id}/mesh endpoint")
    else:
        # Fallback — append before last 100 chars
        main_content += mesh_endpoints
        changes.append("Added mesh API endpoints (appended)")

with open(MAIN_PATH, "w") as f:
    f.write(main_content)

print(f"[2/4] Patched main.py:")
for c in changes:
    print(f"  ✅ {c}")
if not changes:
    print("  ⚠️  No changes needed — already patched")

# ============================================================
# 4. Summary
# ============================================================
print()
print("[3/4] Run: python3 migrate_mesh_data.py")
print("[4/4] Build frontend + restart backend")
print()
print("=" * 60)
print("  Backend complete. Next: deploy ModelViewer.jsx")
print("=" * 60)
