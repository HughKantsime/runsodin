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
