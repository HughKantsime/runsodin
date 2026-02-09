"""
3MF Parser for Bambu Studio sliced files.
Extracts print metadata, filament info, and thumbnails.
"""

import zipfile
import xml.etree.ElementTree as ET
import json
import base64
from pathlib import Path

def _friendly_printer_name(model_id: str) -> str:
    """Convert Bambu internal codes to friendly names."""
    mappings = {
        'BL-P001': 'P1S', 'BL-P002': 'P1P',
        'BL-A001': 'A1', 'BL-A003': 'A1 Mini',
        'C11': 'X1C', 'C12': 'X1E', 'C13': 'X1',
        'N1': 'A1', 'N2S': 'A1 Mini',
        'O1D': 'X1C',
    }
    for code, name in mappings.items():
        if code.lower() in model_id.lower():
            return name
    return model_id


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


from typing import Optional, List
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
    nozzle_diameter: float
    printer_model: str
    supports_used: bool
    bed_type: str
    filaments: List[FilamentInfo]
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


def parse_3mf(file_path: str) -> Optional[PrintFileMetadata]:
    """
    Parse a Bambu Studio sliced .3mf file and extract metadata.
    
    Args:
        file_path: Path to the .3mf file
        
    Returns:
        PrintFileMetadata object or None if parsing fails
    """
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Check if this is a sliced file (has gcode)
            file_list = zf.namelist()
            has_gcode = any('plate_1.gcode' in f for f in file_list)
            
            if not has_gcode:
                # Try to parse as unsliced project
                return parse_unsliced_3mf(zf, file_path)
            
            # Parse slice_info.config for main metadata
            slice_info = parse_slice_info(zf)
            if not slice_info:
                return None
            
            # Parse plate_1.json for additional info
            plate_info = parse_plate_json(zf)
            
            # Extract thumbnail
            thumbnail_b64 = extract_thumbnail(zf)
            
            # Get project name from 3dmodel.model
            project_name = extract_project_name(zf) or Path(file_path).stem
            
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
                filename=Path(file_path).name,
                project_name=project_name,
                print_time_seconds=int(slice_info.get('prediction', 0)),
                total_weight_grams=float(slice_info.get('weight', 0)),
                layer_count=layer_count,
                layer_height=plate_info.get('layer_height', 0.2) if plate_info else 0.2,
                nozzle_diameter=float(str(slice_info.get('nozzle_diameters', 0.4)).split(',')[0]),
                printer_model=extract_printer_model_from_settings(zf) or slice_info.get('printer_model_id', 'Unknown'),
                supports_used=slice_info.get('support_used', 'false').lower() == 'true',
                bed_type=plate_info.get('bed_type', 'Unknown') if plate_info else 'Unknown',
                filaments=filaments,
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
                'nozzle_diameter': data.get('nozzle_diameter', 0.4)
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


def parse_unsliced_3mf(zf: zipfile.ZipFile, file_path: str) -> Optional[PrintFileMetadata]:
    """Parse an unsliced .3mf project file (limited data)."""
    try:
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
        
        project_name = extract_project_name(zf) or Path(file_path).stem
        thumbnail_b64 = extract_thumbnail(zf)
        
        return PrintFileMetadata(
            filename=Path(file_path).name,
            project_name=project_name,
            print_time_seconds=0,  # Unknown - needs manual entry
            total_weight_grams=0,
            layer_count=0,
            layer_height=0.2,
            nozzle_diameter=0.4,
            printer_model='Unknown',
            supports_used=False,
            bed_type='Unknown',
            filaments=filaments,
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


def extract_objects_from_plate(zf: zipfile.ZipFile) -> list:
    """Extract object names from plate_1.json for quantity counting."""
    try:
        with zf.open('Metadata/plate_1.json') as f:
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
