"""
3MF Parser for Bambu Studio sliced files.
Extracts print metadata, filament info, and thumbnails.
"""

import math
import re
import zipfile
import json
import base64
from pathlib import Path

# Use defusedxml to prevent XXE and billion-laughs attacks when parsing 3MF XML.
# defusedxml is a hard requirement (in requirements.txt) — no fallback to stdlib.
from defusedxml import ElementTree as ET

def _friendly_printer_name(model_id: str) -> str:
    """Convert Bambu model ids / product names to friendly names.

    Delegates to the single Bambu model table in
    modules.printers.printer_models; unknown values pass through.
    """
    from modules.printers.printer_models import bambu_model_name
    return bambu_model_name(model_id) or model_id


def extract_printer_model_from_settings(zf: zipfile.ZipFile) -> str:
    """Extract printer_model from project_settings.config (more reliable than slice_info)."""
    try:
        if 'Metadata/project_settings.config' in zf.namelist():
            with zf.open('Metadata/project_settings.config') as f:
                content = f.read().decode('utf-8')
                import json
                data = json.loads(content)
                # Look for printer_model key
                pm = data.get('printer_model', '')
                if pm:
                    # Clean up "Bambu Lab X1C" -> "X1C", "Bambu Lab H2D" -> "H2D"
                    if 'Bambu Lab ' in pm:
                        return pm.replace('Bambu Lab ', '').strip()
                    return pm
    except Exception as e:
        print(f"Error extracting printer_model from settings: {e}")
    return None


from typing import Any, Optional, List
from dataclasses import dataclass, asdict


@dataclass
class FilamentInfo:
    slot: int
    type: str
    color: str  # hex color like #FFFFFF
    used_meters: float
    used_grams: float


@dataclass
class PrintFileMetadata:
    filename: str
    project_name: str
    print_time_seconds: int
    total_weight_grams: float
    layer_count: int
    layer_height: float
    nozzle_diameter: Optional[float]
    printer_model: Optional[str]
    supports_used: bool
    bed_type: str
    filaments: List[FilamentInfo]
    safety_facts: dict[str, Any]
    thumbnail_b64: Optional[str] = None
    
    def print_time_formatted(self) -> str:
        """Return print time as 'Xh Ym' format."""
        hours = self.print_time_seconds // 3600
        minutes = (self.print_time_seconds % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    
    def to_dict(self):
        d = asdict(self)
        d['print_time_formatted'] = self.print_time_formatted()
        return d


def metadata_from_dict(data: dict) -> PrintFileMetadata:
    """Rebuild metadata sent by the isolated 3MF worker (plain data, no parsing)."""
    values = dict(data)
    values["filaments"] = [FilamentInfo(**item) for item in values.get("filaments") or []]
    return PrintFileMetadata(**values)


def analyze_education_3mf(file_path, ticket):
    """Education upload analysis in the bounded 3MF child (never in the calling process):
    returns (status, metadata). ``ticket`` is a mesh_isolation admission taken on the
    event loop; status is ok, too_large, too_many_members, bad_zip, no_plate, unparsed
    or failed. Raises mesh_isolation.Busy when no run slot frees up before the deadline."""
    from modules.models_library.mesh_isolation import run_admitted
    analysis = run_admitted(str(file_path), ticket, mode="education")
    if analysis.status != "ok" or not analysis.metadata:
        return analysis.status, None
    return "ok", metadata_from_dict(analysis.metadata)


def __getattr__(name):
    # Lazy: the isolation module is only needed by callers of the isolated entry points.
    if name in ("ThreeMFBusy", "reserve_3mf_analysis", "run_on_3mf_executor"):
        from modules.models_library import mesh_isolation
        return {"ThreeMFBusy": mesh_isolation.Busy, "reserve_3mf_analysis": mesh_isolation.reserve,
                "run_on_3mf_executor": mesh_isolation.run_on_executor}[name]
    raise AttributeError(name)


def parse_3mf(file_path: str, name: Optional[str] = None) -> Optional[PrintFileMetadata]:
    """
    Parse a Bambu Studio sliced .3mf file and extract metadata.
    
    Args:
        file_path: Path to the .3mf file
        name: The user's original filename. When the 3MF carries no
            Title, the project name falls back to this name rather than the
            server-side temp file stem.
        
    Returns:
        PrintFileMetadata object or None if parsing fails
    """
    try:
        from modules.models_library.print_file_meta import extract_print_file_meta

        safety_meta = extract_print_file_meta(file_path, ".3mf")
        safety_facts = safety_meta["safety_facts"]
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Check if this is a sliced file (has gcode)
            file_list = zf.namelist()
            has_gcode = any('plate_1.gcode' in f for f in file_list)
            
            if not has_gcode:
                # Try to parse as unsliced project
                return parse_unsliced_3mf(zf, file_path, name)
            
            # Parse slice_info.config for main metadata
            slice_info = parse_slice_info(zf)
            if not slice_info:
                return None
            
            # Parse plate_1.json for additional info
            plate_info = parse_plate_json(zf)
            
            # Extract thumbnail
            thumbnail_b64 = extract_thumbnail(zf)
            
            # Get project name from 3dmodel.model
            project_name = extract_project_name(zf) or Path(name or file_path).stem
            
            # Build filament list
            filaments = []
            for fil in slice_info.get('filaments', []):
                filaments.append(FilamentInfo(
                    slot=fil.get('id', 1),
                    type=fil.get('type', 'Unknown'),
                    color=fil.get('color', '#888888'),
                    used_meters=float(fil.get('used_m', 0)),
                    used_grams=float(fil.get('used_g', 0))
                ))
            
            # Parse layer count from layer_ranges
            layer_count = 0
            layer_ranges = slice_info.get('layer_ranges', '')
            if layer_ranges:
                parts = layer_ranges.split()
                if len(parts) >= 2:
                    layer_count = int(parts[-1]) + 1  # 0-indexed, so add 1
            
            return PrintFileMetadata(
                filename=Path(name or file_path).name,
                project_name=project_name,
                print_time_seconds=int(slice_info.get('prediction', 0)),
                total_weight_grams=float(slice_info.get('weight', 0)),
                layer_count=layer_count,
                layer_height=plate_info.get('layer_height', 0.2) if plate_info else 0.2,
                nozzle_diameter=safety_facts["nozzle"].get("value"),
                printer_model=safety_facts["machine"].get("value"),
                supports_used=slice_info.get('support_used', 'false').lower() == 'true',
                bed_type=plate_info.get('bed_type', 'Unknown') if plate_info else 'Unknown',
                filaments=filaments,
                safety_facts=safety_facts,
                thumbnail_b64=thumbnail_b64
            )
            
    except Exception as e:
        print(f"Error parsing 3mf: {e}")
        return None


def parse_slice_info(zf: zipfile.ZipFile) -> Optional[dict]:
    """Parse Metadata/slice_info.config XML file."""
    try:
        with zf.open('Metadata/slice_info.config') as f:
            tree = ET.parse(f)
            root = tree.getroot()
            
            result = {}
            filaments = []
            
            # Find plate element
            plate = root.find('plate')
            if plate is None:
                return None
            
            # Extract metadata
            for meta in plate.findall('metadata'):
                key = meta.get('key')
                value = meta.get('value')
                if key and value:
                    result[key] = value
            
            # Extract filament info
            for fil in plate.findall('filament'):
                filaments.append({
                    'id': int(fil.get('id', 1)),
                    'type': fil.get('type', 'Unknown'),
                    'color': fil.get('color', '#888888'),
                    'used_m': fil.get('used_m', '0'),
                    'used_g': fil.get('used_g', '0')
                })
            
            # Extract layer ranges
            layer_lists = plate.find('layer_filament_lists')
            if layer_lists is not None:
                layer_fil = layer_lists.find('layer_filament_list')
                if layer_fil is not None:
                    result['layer_ranges'] = layer_fil.get('layer_ranges', '')
            
            result['filaments'] = filaments
            return result
            
    except Exception as e:
        print(f"Error parsing slice_info: {e}")
        return None


def parse_plate_json(zf: zipfile.ZipFile) -> Optional[dict]:
    """Parse Metadata/plate_1.json file."""
    if not _plate_json_member_ok(zf, 'Metadata/plate_1.json'):
        return None
    try:
        with zf.open('Metadata/plate_1.json') as f:
            data = json.load(f)
            
            # Extract layer height from bbox_objects
            layer_height = 0.2
            if 'bbox_objects' in data and len(data['bbox_objects']) > 0:
                layer_height = data['bbox_objects'][0].get('layer_height', 0.2)
            
            return {
                'bed_type': data.get('bed_type', 'Unknown'),
                'layer_height': layer_height,
                'filament_colors': data.get('filament_colors', []),
                'nozzle_diameter': data.get('nozzle_diameter')
            }
    except Exception as e:
        print(f"Error parsing plate_1.json: {e}")
        return None


def extract_thumbnail(zf: zipfile.ZipFile) -> Optional[str]:
    """Extract and base64 encode the plate thumbnail."""
    try:
        # Try plate_1.png first, then fallback to other thumbnails
        thumbnail_paths = [
            'Metadata/plate_1.png',
            'Auxiliaries/.thumbnails/thumbnail_small.png',
            'Auxiliaries/.thumbnails/thumbnail_3mf.png'
        ]
        
        for path in thumbnail_paths:
            if path in zf.namelist():
                with zf.open(path) as f:
                    data = f.read()
                    return base64.b64encode(data).decode('utf-8')
        
        return None
    except Exception as e:
        print(f"Error extracting thumbnail: {e}")
        return None


def extract_project_name(zf: zipfile.ZipFile) -> Optional[str]:
    """Extract project name from 3D/3dmodel.model."""
    try:
        with zf.open('3D/3dmodel.model') as f:
            content = f.read().decode('utf-8')
            # Look for Title metadata
            tree = ET.fromstring(content)
            for meta in tree.findall('.//{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}metadata'):
                if meta.get('name') == 'Title':
                    return meta.text
            return None
    except Exception as e:
        print(f"Error extracting project name: {e}")
        return None


def parse_unsliced_3mf(zf: zipfile.ZipFile, file_path: str, name: Optional[str] = None) -> Optional[PrintFileMetadata]:
    """Parse an unsliced .3mf project file (limited data)."""
    try:
        from modules.models_library.print_file_meta import extract_print_file_meta

        safety_facts = extract_print_file_meta(file_path, ".3mf")["safety_facts"]
        # Extract what we can from project_settings.config
        filaments = []
        filament_type = 'Unknown'
        filament_color = '#888888'
        
        if 'Metadata/project_settings.config' in zf.namelist():
            with zf.open('Metadata/project_settings.config') as f:
                content = f.read().decode('utf-8')
                data = json.loads(content)
                
                if 'filament_type' in data:
                    types = data['filament_type']
                    if isinstance(types, list) and len(types) > 0:
                        filament_type = types[0]
                
                if 'filament_colour' in data:
                    colors = data['filament_colour']
                    if isinstance(colors, list) and len(colors) > 0:
                        filament_color = colors[0]
        
        filaments.append(FilamentInfo(
            slot=1,
            type=filament_type,
            color=filament_color,
            used_meters=0,
            used_grams=0
        ))
        
        project_name = extract_project_name(zf) or Path(name or file_path).stem
        thumbnail_b64 = extract_thumbnail(zf)
        
        return PrintFileMetadata(
            filename=Path(name or file_path).name,
            project_name=project_name,
            print_time_seconds=0,  # Unknown - needs manual entry
            total_weight_grams=0,
            layer_count=0,
            layer_height=0.2,
            nozzle_diameter=safety_facts["nozzle"].get("value"),
            printer_model=safety_facts["machine"].get("value"),
            supports_used=False,
            bed_type='Unknown',
            filaments=filaments,
            safety_facts=safety_facts,
            thumbnail_b64=thumbnail_b64
        )
        
    except Exception as e:
        print(f"Error parsing unsliced 3mf: {e}")
        return None


# CLI test
if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        result = parse_3mf(sys.argv[1])
        if result:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("Failed to parse file")


# A plate_N.json is a few KB in real Bambu Studio / Orca exports. json.load of a large
# member expands to many times its size in Python objects (a ~60 MB member exceeds 4 GB),
# so a plate JSON larger than this is never parsed.
MAX_PLATE_JSON_BYTES = 4 * 2**20


def plate_json_too_large(zf: zipfile.ZipFile) -> bool:
    """True when any Metadata/plate_N.json declares more than MAX_PLATE_JSON_BYTES."""
    return any(re.fullmatch(r"Metadata/plate_[1-9][0-9]*\.json", info.filename)
               and info.file_size > MAX_PLATE_JSON_BYTES for info in zf.infolist())


def _plate_json_member_ok(zf: zipfile.ZipFile, name: str) -> bool:
    try:
        return zf.getinfo(name).file_size <= MAX_PLATE_JSON_BYTES
    except KeyError:
        return False


def extract_objects_from_plate(zf: zipfile.ZipFile, plate_number: int = 1) -> list:
    """Extract object names from plate_N.json (default plate 1) for quantity counting."""
    if not _plate_json_member_ok(zf, f'Metadata/plate_{plate_number}.json'):
        return []
    try:
        with zf.open(f'Metadata/plate_{plate_number}.json') as f:
            data = json.load(f)
            objects = []
            
            # Objects can be in 'objects' or 'bbox_objects' depending on version
            obj_list = data.get('objects', data.get('bbox_objects', []))
            
            for obj in obj_list:
                name = obj.get('name', 'Unknown')
                # Auto-detect wipe tower
                is_wipe_tower = 'wipe_tower' in name.lower() or 'wipe tower' in name.lower()
                objects.append({
                    'name': name,
                    'is_wipe_tower': is_wipe_tower,
                    'checked': not is_wipe_tower  # Uncheck wipe towers by default
                })
            
            return objects
    except Exception as e:
        print(f"Error extracting objects: {e}")
        return []


_CORE_NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
_PROD_NS = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
_MAX_COMPONENT_DEPTH = 16
# Budgets for one preview. Components multiply: a few hundred bytes can describe
# billions of instances, so emission is bounded globally, not per level.
_MAX_EMITTED_VERTICES = 1_000_000
_MAX_EMITTED_TRIANGLES = 2_000_000
_MAX_MESH_INSTANCES = 10_000
_MAX_COMPONENT_VISITS = 100_000
# Decompressed size of one .model part (zip members are tiny when repetitive).
_MAX_MODEL_PART_BYTES = 64 * 2**20


class MeshBudgetExceeded(Exception):
    """The package describes more geometry than a preview may expand (or a cycle)."""


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _children(elem, name: str):
    return [child for child in list(elem) if _local(child.tag) == name]


def _child(elem, name: str):
    found = _children(elem, name)
    return found[0] if found else None


def _finite(value) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise MeshBudgetExceeded("non-finite geometry")
    return number


def _matrix(value):
    """3MF transform 'm00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32' (row vectors)."""
    if not value:
        return _IDENTITY
    try:
        parts = tuple(_finite(v) for v in value.split())
    except ValueError:
        raise MeshBudgetExceeded("invalid transform") from None
    if len(parts) != 12:
        raise MeshBudgetExceeded("invalid transform")
    return parts


def _compose(inner, outer):
    """Matrix applying `inner` first, then `outer`."""
    a, b = inner, outer
    rot = []
    for r in range(3):
        for c in range(3):
            rot.append(sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)))
    trans = [sum(a[9 + k] * b[k * 3 + c] for k in range(3)) + b[9 + c] for c in range(3)]
    return tuple(rot + trans)


def _root_model_path(zf: zipfile.ZipFile):
    """The package's start part (OPC relationship), else 3D/3dmodel.model, else any .model."""
    names = zf.namelist()
    lower = {n.lower(): n for n in names}
    try:
        rels = ET.fromstring(zf.read(lower['_rels/.rels']))
        for rel in rels.iter():
            if _local(rel.tag) == 'Relationship' and (rel.get('Type') or '').endswith('/3dmodel'):
                target = (rel.get('Target') or '').lstrip('/')
                if target.lower() in lower:
                    return lower[target.lower()]
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        pass
    if '3d/3dmodel.model' in lower:
        return lower['3d/3dmodel.model']
    for name in names:
        if name.lower().endswith('.model') and '3d/' in name.lower():
            return name
    for name in names:
        if name.lower().endswith('.model'):
            return name
    return None


def _collect_mesh_geometry(zf: zipfile.ZipFile):
    """Flattened vertices/triangles of every built object.

    Follows build items -> objects -> components, including 3MF Production
    extension components whose geometry lives in another part
    (``<component p:path="/3D/Objects/object_1.model">``, as written by
    Bambu Studio and OrcaSlicer projects), applying each transform.
    """
    root_path = _root_model_path(zf)
    if not root_path:
        return [], []
    lower = {n.lower(): n for n in zf.namelist()}
    documents = {}

    def objects_of(path):
        if path not in documents:
            try:
                with zf.open(path) as member:
                    data = member.read(_MAX_MODEL_PART_BYTES + 1)
                if len(data) > _MAX_MODEL_PART_BYTES:
                    raise MeshBudgetExceeded("model part too large")
                tree = ET.fromstring(data)
            except (KeyError, ET.ParseError):
                documents[path] = ({}, None)
                return documents[path]
            resources = _child(tree, 'resources')
            objects = {o.get('id'): o for o in _children(resources, 'object')} if resources is not None else {}
            documents[path] = (objects, tree)
        return documents[path]

    vertices, triangles = [], []
    budget = {'instances': 0, 'visits': 0}

    def emit_mesh(mesh, transform):
        verts_elem, tris_elem = _child(mesh, 'vertices'), _child(mesh, 'triangles')
        if verts_elem is None or tris_elem is None:
            return
        budget['instances'] += 1
        source = _children(verts_elem, 'vertex')
        faces = _children(tris_elem, 'triangle')
        if (budget['instances'] > _MAX_MESH_INSTANCES or len(vertices) // 3 + len(source) > _MAX_EMITTED_VERTICES
                or len(triangles) // 3 + len(faces) > _MAX_EMITTED_TRIANGLES):
            raise MeshBudgetExceeded("mesh budget exceeded")
        offset = len(vertices) // 3
        m = transform
        count = 0
        for v in source:
            x, y, z = _finite(v.get('x', 0)), _finite(v.get('y', 0)), _finite(v.get('z', 0))
            point = (x * m[0] + y * m[3] + z * m[6] + m[9],
                     x * m[1] + y * m[4] + z * m[7] + m[10],
                     x * m[2] + y * m[5] + z * m[8] + m[11])
            if not all(math.isfinite(c) for c in point):
                raise MeshBudgetExceeded("non-finite geometry")
            vertices.extend(point)
            count += 1
        for t in faces:
            ids = [int(t.get(k, 0)) for k in ('v1', 'v2', 'v3')]
            if all(0 <= i < count for i in ids):
                triangles.extend(i + offset for i in ids)

    def emit_object(path, object_id, transform, stack):
        key = (path, object_id)
        if key in stack:
            raise MeshBudgetExceeded("component cycle")
        if len(stack) >= _MAX_COMPONENT_DEPTH:
            raise MeshBudgetExceeded("components nested too deep")
        budget['visits'] += 1
        if budget['visits'] > _MAX_COMPONENT_VISITS:
            raise MeshBudgetExceeded("too many components")
        objects, _ = objects_of(path)
        obj = objects.get(object_id)
        if obj is None:
            return
        mesh = _child(obj, 'mesh')
        if mesh is not None:
            emit_mesh(mesh, transform)
        components = _child(obj, 'components')
        for comp in _children(components, 'component') if components is not None else []:
            ref = comp.get('{%s}path' % _PROD_NS) or comp.get('path')
            target = path
            if ref:
                target = lower.get(ref.lstrip('/').lower())
                if target is None:
                    continue
            emit_object(target, comp.get('objectid'), _compose(_matrix(comp.get('transform')), transform), stack | {key})

    objects, tree = objects_of(root_path)
    build = _child(tree, 'build') if tree is not None else None
    items = _children(build, 'item') if build is not None else []
    if items:
        for item in items:
            emit_object(root_path, item.get('objectid'), _matrix(item.get('transform')), frozenset())
    else:
        # No build section: every object of the root part.
        for object_id in objects:
            emit_object(root_path, object_id, _IDENTITY, frozenset())
    return vertices, triangles


def extract_mesh_from_3mf(file_path: str) -> Optional[dict]:
    """
    Extract mesh geometry (vertices + triangles) from a .3mf file.
    Returns a dict with 'vertices' (flat [x,y,z,x,y,z,...]) and
    'triangles' (flat [v1,v2,v3,v1,v2,v3,...]) for Three.js BufferGeometry.
    Keeps only every Nth vertex for large models to stay under ~500KB.
    """
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            try:
                all_vertices, all_triangles = _collect_mesh_geometry(zf)
            except MeshBudgetExceeded as refused:
                # No preview rather than an unbounded or invalid one.
                print(f"Mesh preview refused: {refused}")
                return None
            
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


def plate_summaries(zf: zipfile.ZipFile) -> list:
    """Per-plate sellable pieces, filament grams and print time for every sliced plate.

    Model-level upload fields (objects, quantity_per_bed, filament grams, build time) describe
    plate 1 only; scheduling a different plate uses that plate's entry from here.
    Returns [{plate_index (0-based), sellable_pieces, filament_grams, print_time_seconds}] for each
    Metadata/plate_N.gcode member; a value is None when the archive does not record it.
    """
    numbers = sorted({int(m.group(1)) for name in zf.namelist()
                      if (m := re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.gcode", name))})
    sliced_numbers = set(numbers)
    numbers = sorted(sliced_numbers | {int(m.group(1)) for name in zf.namelist()
                     if (m := re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.json", name))})
    sliced = {}
    try:
        with zf.open('Metadata/slice_info.config') as f:
            root = ET.parse(f).getroot()
        for plate in root.findall('plate'):
            meta = {m.get('key'): m.get('value') for m in plate.findall('metadata')}
            try:
                number = int(meta.get('index', ''))
            except ValueError:
                continue
            grams = None
            used = [fil.get('used_g') for fil in plate.findall('filament') if fil.get('used_g') is not None]
            try:
                grams = round(sum(float(value) for value in used), 2) if used else (float(meta['weight']) if meta.get('weight') else None)
                seconds = int(float(meta['prediction'])) if meta.get('prediction') else None
            except ValueError:
                grams, seconds = None, None
            sliced[number] = (grams, seconds)
    except (KeyError, ValueError, ET.ParseError):
        pass  # unsliced or foreign slicer: per-plate values stay None
    summaries = []
    for number in numbers:
        objects = extract_objects_from_plate(zf, number) if f'Metadata/plate_{number}.json' in zf.namelist() else None
        grams, seconds = sliced.get(number, (None, None))
        summaries.append({
            'plate_index': number - 1,
            'is_sliced': number in sliced_numbers,
            'sellable_pieces': sum(1 for obj in objects if obj.get('checked')) if objects is not None else None,
            'filament_grams': grams,
            'print_time_seconds': seconds,
        })
    return summaries
