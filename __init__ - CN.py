bl_info = {
    "name": "EasyBake",
    "author": "475519905",
    "version": (1, 7, 0),
    "blender": (4, 2, 0),
    "location": "Render ‣ EasyBake",
    "description": "Bake optional PBR channels (Base Color, Roughness, Metallic, Normal), supports multi-objects, multi-material slots, optional node replacement and custom resolutions. Now supports custom shader baking.",
    "category": "Material",
}

import bpy
import os
import json
from contextlib import contextmanager
from bpy.props import IntProperty, StringProperty, BoolProperty, EnumProperty, FloatProperty
from bpy.types import Operator, Panel

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

def safe_encode_text(text, fallback="Unknown"):
    """Safely handle text that might contain non-UTF-8 characters"""
    if not text:
        return fallback
    
    try:
        # Try to ensure text is string type
        if isinstance(text, bytes):
            # If it's bytes type, try to decode
            try:
                return text.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return text.decode('gbk')  # Try GBK encoding (Windows Chinese)
                except UnicodeDecodeError:
                    return text.decode('latin-1', errors='ignore')  # Final fallback
        else:
            # If it's string, ensure correct encoding
            return str(text).encode('utf-8', errors='ignore').decode('utf-8')
    except Exception:
        return fallback


def safe_path_display(path, max_length=50):
    """Safely display file path, avoiding encoding errors"""
    if not path:
        return "No path set"
    
    try:
        # Safe encoding handling
        safe_path = safe_encode_text(str(path))
        
        # If path is too long, truncate
        if len(safe_path) > max_length:
            return "..." + safe_path[-(max_length-3):]
        return safe_path
    except Exception:
        return "Path display error"


def calculate_atlas_layout(material_count):
    """Calculate optimal layout for material atlas"""
    if material_count <= 1:
        return (1, 1)
    elif material_count <= 2:
        return (2, 1)
    elif material_count <= 4:
        return (2, 2)
    elif material_count <= 6:
        return (3, 2)
    elif material_count <= 9:
        return (3, 3)
    elif material_count <= 12:
        return (4, 3)
    elif material_count <= 16:
        return (4, 4)
    else:
        # For more than 16 materials, use approximate square layout
        import math
        sqrt_count = math.ceil(math.sqrt(material_count))
        return (sqrt_count, sqrt_count)


def get_atlas_uv_bounds(material_index, cols, rows, padding=0.02):
    """Get UV bounds for material in atlas"""
    col = material_index % cols
    row = material_index // cols
    
    # Calculate base UV coordinates
    u_size = 1.0 / cols
    v_size = 1.0 / rows
    
    u_min = col * u_size
    v_min = row * v_size
    u_max = u_min + u_size
    v_max = v_min + v_size
    
    # Apply padding
    padding_u = padding / cols
    padding_v = padding / rows
    
    u_min += padding_u
    v_min += padding_v
    u_max -= padding_u
    v_max -= padding_v
    
    return (u_min, v_min, u_max, v_max)


def create_atlas_uv_layer(obj, material_slots, atlas_layout, padding=0.02):
    """Create atlas UV layer for object"""
    if not obj.data.uv_layers:
        # If no UV layer exists, create one first
        obj.data.uv_layers.new(name="UVMap")
    
    # Create new atlas UV layer
    atlas_uv_layer = obj.data.uv_layers.new(name="AtlasUV")
    obj.data.uv_layers.active = atlas_uv_layer
    
    cols, rows = atlas_layout
    
    import bmesh
    
    # Enter edit mode to modify UV
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    
    # Create bmesh instance
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    
    try:
        # Ensure face indices are valid
        bm.faces.ensure_lookup_table()
        
        # Get UV layer
        uv_layer = bm.loops.layers.uv.active
        if not uv_layer:
            uv_layer = bm.loops.layers.uv.new()
        
        # Assign each face to correct material area
        for face in bm.faces:
            material_index = face.material_index
            if material_index < len(material_slots):
                # Get UV bounds for this material in atlas
                u_min, v_min, u_max, v_max = get_atlas_uv_bounds(material_index, cols, rows, padding)
                
                # Get face's original UV coordinates
                original_uvs = []
                for loop in face.loops:
                    original_uvs.append(loop[uv_layer].uv.copy())
                
                # Remap UV coordinates to atlas area
                for i, loop in enumerate(face.loops):
                    orig_u, orig_v = original_uvs[i]
                    # Map 0-1 range UV to material's atlas area
                    new_u = u_min + orig_u * (u_max - u_min)
                    new_v = v_min + orig_v * (v_max - v_min)
                    loop[uv_layer].uv = (new_u, new_v)
        
        # Update mesh
        bmesh.update_edit_mesh(obj.data)
        
    except Exception as e:
        print(f"UV atlas creation error: {e}")
    finally:
        bm.free()
        bpy.ops.object.mode_set(mode='OBJECT')
    
    return atlas_uv_layer.name


def restore_original_uv_layer(obj, original_uv_name):
    """Restore original UV layer"""
    if original_uv_name in obj.data.uv_layers:
        obj.data.uv_layers.active = obj.data.uv_layers[original_uv_name]
        # Delete atlas UV layer
        if "AtlasUV" in obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers["AtlasUV"])


def get_presets_dir():
    """Get presets folder path"""
    presets_dir = os.path.join(bpy.utils.user_resource('SCRIPTS'), "presets", "mbnl_bake")
    os.makedirs(presets_dir, exist_ok=True)
    return presets_dir


def get_preset_filepath(preset_name):
    """Get full path of preset file"""
    return os.path.join(get_presets_dir(), f"{preset_name}.json")


def get_available_presets():
    """Get all available presets"""
    presets_dir = get_presets_dir()
    presets = []
    try:
        for filename in os.listdir(presets_dir):
            if filename.endswith('.json'):
                preset_name = filename[:-5]  # Remove .json extension
                presets.append((preset_name, preset_name, f"Preset: {preset_name}"))
    except OSError:
        pass
    
    if not presets:
        presets.append(('NONE', 'No Presets', 'No available presets'))
    
    return presets


def update_presets_enum(self, context):
    """Update preset enum list"""
    return get_available_presets()


def save_preset_to_file(preset_name, settings):
    """Save preset to file"""
    try:
        filepath = get_preset_filepath(preset_name)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Failed to save preset: {e}")
        return False


def load_preset_from_file(preset_name):
    """Load preset from file"""
    try:
        filepath = get_preset_filepath(preset_name)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"Failed to load preset: {e}")
    return None


def delete_preset_file(preset_name):
    """Delete preset file"""
    try:
        filepath = get_preset_filepath(preset_name)
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
    except Exception as e:
        print(f"Failed to delete preset: {e}")
    return False

def ensure_cycles(scene):
    if scene.render.engine != "CYCLES":
        scene.render.engine = "CYCLES"


def smart_uv(obj):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.uv.smart_project(angle_limit=66, island_margin=0.03)
    bpy.ops.object.mode_set(mode="OBJECT")


def get_principled_bsdf_inputs():
    """Get actual input names of Principled BSDF in current Blender version"""
    
    try:
        # Create temporary material to check input names
        temp_mat = bpy.data.materials.new("temp_check")
        temp_mat.use_nodes = True
        nt = temp_mat.node_tree
        nt.nodes.clear()
        
        principled = nt.nodes.new('ShaderNodeBsdfPrincipled')
        input_names = list(principled.inputs.keys())
        
        # Clean up temporary material
        bpy.data.materials.remove(temp_mat)
        
        return input_names
    except Exception:
        # If any error occurs, return basic input name list
        return [
            'Base Color', 'Metallic', 'Roughness', 'IOR', 'Alpha',
            'Normal', 'Subsurface', 'Transmission', 'Emission'
        ]


def create_input_mapping():
    """Create input name mapping suitable for current Blender version"""
    try:
        available_inputs = get_principled_bsdf_inputs()
    except Exception:
        # Use conservative default input list
        available_inputs = [
            'Base Color', 'Metallic', 'Roughness', 'IOR', 'Alpha',
            'Normal', 'Subsurface', 'Transmission', 'Emission'
        ]
    
    # Base mapping (try different possible names)
    mapping_candidates = {
        'BaseColor': ['Base Color', 'BaseColor'],
        'Metallic': ['Metallic'],
        'Roughness': ['Roughness'],
        'Subsurface': ['Subsurface', 'Subsurface Weight', 'Subsurface Radius'],
        'Transmission': ['Transmission', 'Transmission Weight'],
        'Emission': ['Emission', 'Emission Color'],
        'Alpha': ['Alpha'],
        'Specular': ['Specular', 'Specular IOR', 'IOR'],
        'Clearcoat': ['Clearcoat', 'Clearcoat Weight'],
        'ClearcoatRoughness': ['Clearcoat Roughness'],
        'Sheen': ['Sheen', 'Sheen Weight']
    }
    
    # Build actual mapping
    input_mapping = {}
    for key, candidates in mapping_candidates.items():
        found = False
        for candidate in candidates:
            if candidate in available_inputs:
                input_mapping[key] = candidate
                found = True
                break
        # If no match found, use first candidate name (but no guarantee it exists)
        if not found:
            input_mapping[key] = candidates[0]
    
    return input_mapping


def analyze_material(material):
    """Analyze material type and characteristics"""
    analysis = {
        'has_image_textures': False,
        'has_pure_colors': False,
        'texture_nodes': [],
        'principled_node': None,
        'material_type': 'unknown',
        'custom_shaders': [],
        'output_node': None,
        'has_custom_shaders': False,
        'mix_shaders': [],
        'shader_network': {},
        'principled_connected_to_output': False,
        'custom_connected_to_output': False
    }
    
    if not material or not material.use_nodes:
        return analysis
    
    nt = material.node_tree
    if not nt:
        return analysis
    
    try:
        # Find output node
        output_node = None
        for node in nt.nodes:
            if node.bl_idname == 'ShaderNodeOutputMaterial':
                output_node = node
                break
        analysis['output_node'] = output_node
        
        # Find Principled BSDF node
        principled = None
        for node in nt.nodes:
            if node.bl_idname == 'ShaderNodeBsdfPrincipled':
                principled = node
                break
        
        analysis['principled_node'] = principled
        
        # Detect custom shaders (shader nodes other than Principled BSDF)
        custom_shaders = []
        shader_types = [
            'ShaderNodeBsdfDiffuse', 'ShaderNodeBsdfGlossy', 'ShaderNodeBsdfTransparent',
            'ShaderNodeBsdfTranslucent', 'ShaderNodeBsdfGlass', 'ShaderNodeBsdfRefraction',
            'ShaderNodeBsdfAnisotropic', 'ShaderNodeBsdfVelvet', 'ShaderNodeBsdfToon',
            'ShaderNodeSubsurfaceScattering', 'ShaderNodeEmission', 'ShaderNodeBsdfHair',
            'ShaderNodeBsdfHairPrincipled', 'ShaderNodeBsdfSheen', 'ShaderNodeMixShader',
            'ShaderNodeAddShader', 'ShaderNodeNodeGroup'  # Node groups can also be custom shaders
        ]
        
        for node in nt.nodes:
            if node.bl_idname in shader_types:
                custom_shaders.append({
                    'node': node,
                    'type': node.bl_idname,
                    'name': node.name,
                    'label': node.label if node.label else node.name
                })
        
        analysis['custom_shaders'] = custom_shaders
        analysis['has_custom_shaders'] = len(custom_shaders) > 0
        
        # Detect Mix/Add Shader nodes
        mix_shaders = []
        for node in nt.nodes:
            if node.bl_idname in ['ShaderNodeMixShader', 'ShaderNodeAddShader']:
                mix_shaders.append({
                    'node': node,
                    'type': node.bl_idname,
                    'name': node.name
                })
        analysis['mix_shaders'] = mix_shaders
        
        # Analyze shader network connections
        if output_node and output_node.inputs['Surface'].is_linked:
            connected_node = output_node.inputs['Surface'].links[0].from_node
            
            # Check if directly connected to Principled BSDF
            if connected_node.bl_idname == 'ShaderNodeBsdfPrincipled':
                analysis['principled_connected_to_output'] = True
            # Check if directly connected to custom shader
            elif connected_node.bl_idname in [
                'ShaderNodeBsdfDiffuse', 'ShaderNodeBsdfGlossy', 'ShaderNodeBsdfTransparent',
                'ShaderNodeBsdfTranslucent', 'ShaderNodeBsdfGlass', 'ShaderNodeBsdfRefraction',
                'ShaderNodeBsdfAnisotropic', 'ShaderNodeBsdfVelvet', 'ShaderNodeBsdfToon',
                'ShaderNodeSubsurfaceScattering', 'ShaderNodeEmission', 'ShaderNodeBsdfHair',
                'ShaderNodeBsdfHairPrincipled', 'ShaderNodeBsdfSheen', 'ShaderNodeNodeGroup'
            ]:
                analysis['custom_connected_to_output'] = True
            # Check if connected through Mix/Add Shader
            elif connected_node.bl_idname in ['ShaderNodeMixShader', 'ShaderNodeAddShader']:
                # Analyze Mix/Add Shader inputs
                principled_in_mix = False
                custom_in_mix = False
                
                for input_socket in connected_node.inputs:
                    if input_socket.is_linked:
                        input_node = input_socket.links[0].from_node
                        if input_node.bl_idname == 'ShaderNodeBsdfPrincipled':
                            principled_in_mix = True
                        elif input_node.bl_idname in [
                            'ShaderNodeBsdfDiffuse', 'ShaderNodeBsdfGlossy', 'ShaderNodeBsdfTransparent',
                            'ShaderNodeBsdfTranslucent', 'ShaderNodeBsdfGlass', 'ShaderNodeBsdfRefraction',
                            'ShaderNodeBsdfAnisotropic', 'ShaderNodeBsdfVelvet', 'ShaderNodeBsdfToon',
                            'ShaderNodeSubsurfaceScattering', 'ShaderNodeEmission', 'ShaderNodeBsdfHair',
                            'ShaderNodeBsdfHairPrincipled', 'ShaderNodeBsdfSheen', 'ShaderNodeNodeGroup'
                        ]:
                            custom_in_mix = True
                
                analysis['principled_connected_to_output'] = principled_in_mix
                analysis['custom_connected_to_output'] = custom_in_mix
                
                # Store mix network info
                analysis['shader_network'] = {
                    'mix_node': connected_node,
                    'has_principled': principled_in_mix,
                    'has_custom': custom_in_mix
                }
        
        if not principled and not analysis['has_custom_shaders']:
            analysis['material_type'] = 'unknown'
            return analysis
        elif not principled and analysis['has_custom_shaders']:
            analysis['material_type'] = 'custom_shader'
            return analysis
        
        # Check if has image textures
        image_textures = [n for n in nt.nodes if n.bl_idname == 'ShaderNodeTexImage' and n.image]
        analysis['texture_nodes'] = image_textures
        analysis['has_image_textures'] = len(image_textures) > 0
        
        # Safely check if has connections to Principled BSDF inputs
        connected_inputs = []
        try:
            if principled:
                for input_name in principled.inputs.keys():
                    try:
                        if principled.inputs[input_name].is_linked:
                            connected_inputs.append(input_name)
                    except (KeyError, AttributeError):
                        continue
        except (AttributeError, TypeError):
            pass
        
        # Safely check if has pure color values (unconnected but non-default value inputs)
        pure_color_inputs = []
        try:
            if principled:
                for input_name in principled.inputs.keys():
                    try:
                        input_socket = principled.inputs[input_name]
                        if not input_socket.is_linked:
                            # Check if it's non-default value
                            try:
                                default_val = input_socket.default_value
                                if input_name == 'Base Color' and hasattr(default_val, '__len__'):
                                    # Base color is not pure white (0.8, 0.8, 0.8)
                                    if not (abs(default_val[0] - 0.8) < 0.01 and 
                                           abs(default_val[1] - 0.8) < 0.01 and 
                                           abs(default_val[2] - 0.8) < 0.01):
                                        pure_color_inputs.append(input_name)
                                elif input_name in ['Metallic', 'Roughness'] and isinstance(default_val, (int, float)):
                                    # Metallic is not 0, Roughness is not 0.5
                                    if ((input_name == 'Metallic' and abs(default_val) > 0.01) or
                                        (input_name == 'Roughness' and abs(default_val - 0.5) > 0.01)):
                                        pure_color_inputs.append(input_name)
                            except (AttributeError, TypeError):
                                pass
                    except (KeyError, AttributeError):
                        continue
        except (AttributeError, TypeError):
            pass
        
        analysis['has_pure_colors'] = len(pure_color_inputs) > 0
        analysis['pure_color_inputs'] = pure_color_inputs
        analysis['connected_inputs'] = connected_inputs
        
        # Determine material type
        if principled and analysis['has_custom_shaders']:
            # Check if mixed in same network
            if analysis['principled_connected_to_output'] and analysis['custom_connected_to_output']:
                analysis['material_type'] = 'mixed_shader_network'  # Mixed shader network
            elif analysis['principled_connected_to_output']:
                analysis['material_type'] = 'principled_with_custom'  # Principled main, contains custom shaders
            elif analysis['custom_connected_to_output']:
                analysis['material_type'] = 'custom_with_principled'  # Custom shader main, contains Principled
            else:
                analysis['material_type'] = 'mixed_shader'  # General mixed case
        elif analysis['has_custom_shaders'] and not principled:
            analysis['material_type'] = 'custom_shader'  # Only custom shaders
        elif principled and not analysis['has_custom_shaders']:
            # Principled BSDF material subdivision
            if analysis['has_image_textures'] and analysis['has_pure_colors']:
                analysis['material_type'] = 'mixed'
            elif analysis['has_image_textures']:
                analysis['material_type'] = 'textured'
            elif analysis['has_pure_colors'] or connected_inputs:
                analysis['material_type'] = 'procedural'
            else:
                analysis['material_type'] = 'default'
        else:
            analysis['material_type'] = 'unknown'
            
    except Exception as e:
        # If any error occurs during analysis, return basic info
        analysis['material_type'] = 'unknown'
        print(f"Material analysis error: {e}")
    
    return analysis


@contextmanager
def temporary_emission_surface(nt):
    """Temporarily replace shader connected to Material Output Surface with Emission for baking"""
    output = next((n for n in nt.nodes if n.bl_idname == 'ShaderNodeOutputMaterial'), None)
    if not output or not output.inputs['Surface'].is_linked:
        yield None
        return

    # Save original connections
    original_surface_link = output.inputs['Surface'].links[0]
    original_from_socket = original_surface_link.from_socket
    original_to_socket = original_surface_link.to_socket
    original_from_node = original_from_socket.node
    
    # Remove original connection
    nt.links.remove(original_surface_link)

    # Create emission node
    emit = nt.nodes.new('ShaderNodeEmission')
    emit.location = (-300, -600)
    emit.label = "Custom Shader Bake"
    
    try:
        # Intelligently handle different types of shader nodes
        connection_made = False
        
        # If it's a node group, needs special handling
        if original_from_node.bl_idname == 'ShaderNodeNodeGroup':
            # Node group may have multiple outputs, find correct shader output
            shader_output_names = ['Shader', 'BSDF', 'Surface', 'Color', 'Output']
            
            for output_name in shader_output_names:
                if output_name in original_from_node.outputs:
                    try:
                        nt.links.new(original_from_node.outputs[output_name], emit.inputs['Color'])
                        connection_made = True
                        print(f"Custom shader bake: connected node group output '{output_name}' to Emission")
                        break
                    except Exception as e:
                        print(f"Failed to connect node group output '{output_name}': {e}")
                        continue
            
            # If no standard output name found, use first output
            if not connection_made and len(original_from_node.outputs) > 0:
                try:
                    first_output = original_from_node.outputs[0]
                    nt.links.new(first_output, emit.inputs['Color'])
                    connection_made = True
                    print(f"Custom shader bake: using node group first output '{first_output.name}'")
                except Exception as e:
                    print(f"Failed to connect node group first output: {e}")
        
        # For other types of shader nodes
        elif original_from_node.bl_idname in [
            'ShaderNodeBsdfDiffuse', 'ShaderNodeBsdfGlossy', 'ShaderNodeBsdfTransparent',
            'ShaderNodeBsdfTranslucent', 'ShaderNodeBsdfGlass', 'ShaderNodeBsdfRefraction',
            'ShaderNodeBsdfAnisotropic', 'ShaderNodeBsdfVelvet', 'ShaderNodeBsdfToon',
            'ShaderNodeSubsurfaceScattering', 'ShaderNodeBsdfHair', 'ShaderNodeBsdfHairPrincipled',
            'ShaderNodeBsdfSheen'
        ]:
            # These nodes usually have BSDF output
            if 'BSDF' in original_from_node.outputs:
                try:
                    nt.links.new(original_from_node.outputs['BSDF'], emit.inputs['Color'])
                    connection_made = True
                    print(f"Custom shader bake: connected {original_from_node.bl_idname} BSDF output")
                except Exception as e:
                    print(f"Failed to connect BSDF output: {e}")
        
        # For Emission nodes
        elif original_from_node.bl_idname == 'ShaderNodeEmission':
            if 'Emission' in original_from_node.outputs:
                try:
                    nt.links.new(original_from_node.outputs['Emission'], emit.inputs['Color'])
                    connection_made = True
                    print(f"Custom shader bake: connected Emission node output")
                except Exception as e:
                    print(f"Failed to connect Emission output: {e}")
        
        # For Mix/Add Shader nodes
        elif original_from_node.bl_idname in ['ShaderNodeMixShader', 'ShaderNodeAddShader']:
            if 'Shader' in original_from_node.outputs:
                try:
                    nt.links.new(original_from_node.outputs['Shader'], emit.inputs['Color'])
                    connection_made = True
                    print(f"Custom shader bake: connected {original_from_node.bl_idname} Shader output")
                except Exception as e:
                    print(f"Failed to connect Shader output: {e}")
        
        # If still not connected successfully, try using original from_socket
        if not connection_made:
            try:
                nt.links.new(original_from_socket, emit.inputs['Color'])
                connection_made = True
                print(f"Custom shader bake: using original output '{original_from_socket.name}'")
            except Exception as e:
                print(f"Failed to connect using original output: {e}")
                # Last fallback: create a solid color output
                rgb = nt.nodes.new('ShaderNodeRGB')
                rgb.location = (-500, -600)
                rgb.label = "Temp RGB for Custom Shader"
                rgb.outputs['Color'].default_value = (0.8, 0.8, 0.8, 1.0)  # Light gray
                nt.links.new(rgb.outputs['Color'], emit.inputs['Color'])
                print("Custom shader bake: using fallback solid color output")
        
        # Connect emission to Material Output
        nt.links.new(emit.outputs['Emission'], output.inputs['Surface'])
        
        yield emit
        
    finally:
        # Cleanup and restore
        # Remove emission node connections
        for link in list(emit.outputs['Emission'].links):
            nt.links.remove(link)
        for link in list(emit.inputs['Color'].links):
            nt.links.remove(link)
        
        # Remove any temporary RGB nodes that might have been created
        temp_nodes_to_remove = [n for n in nt.nodes if n.label == "Temp RGB for Custom Shader"]
        for temp_node in temp_nodes_to_remove:
            nt.nodes.remove(temp_node)
        
        # Remove emission node
        nt.nodes.remove(emit)
        
        # Restore original connection
        nt.links.new(original_from_socket, original_to_socket)


@contextmanager
def temporary_principled_only_surface(nt, principled_node):
    """Temporarily connect Material Output only to Principled BSDF for baking"""
    output = next((n for n in nt.nodes if n.bl_idname == 'ShaderNodeOutputMaterial'), None)
    if not output or not principled_node:
        yield None
        return

    # Save original connections
    original_links = []
    if output.inputs['Surface'].is_linked:
        original_links = [(l.from_socket, l.to_socket) for l in output.inputs['Surface'].links]
        # Remove original connections
        for link in list(output.inputs['Surface'].links):
            nt.links.remove(link)

    # Create emission node
    emit = nt.nodes.new('ShaderNodeEmission')
    emit.location = (-300, -700)
    emit.label = "Principled Only Bake"
    
    try:
        # 直接连接Principled BSDF到emission
        if 'BSDF' in principled_node.outputs:
            nt.links.new(principled_node.outputs['BSDF'], emit.inputs['Color'])
        else:
            # 如果没有BSDF输出，使用第一个可用输出
            if len(principled_node.outputs) > 0:
                nt.links.new(principled_node.outputs[0], emit.inputs['Color'])
        
        # 将emission连接到Material Output
        nt.links.new(emit.outputs['Emission'], output.inputs['Surface'])
        
        print(f"Principled only bake: 成功连接Principled BSDF到Emission")
        yield emit
        
    finally:
        # 清理和恢复
        # 移除emission节点的连接
        for link in list(emit.outputs['Emission'].links):
            nt.links.remove(link)
        for link in list(emit.inputs['Color'].links):
            nt.links.remove(link)
        
        # 移除emission节点
        nt.nodes.remove(emit)
        
        # 恢复原始连接
        for frm, to in original_links:
            nt.links.new(frm, to)


@contextmanager
def temporary_custom_shader_only_surface(nt, custom_shader_node):
    """临时将Material Output只连接到指定自定义着色器进行烘焙"""
    output = next((n for n in nt.nodes if n.bl_idname == 'ShaderNodeOutputMaterial'), None)
    if not output or not custom_shader_node:
        yield None
        return

    # 保存原始连接
    original_links = []
    if output.inputs['Surface'].is_linked:
        original_links = [(l.from_socket, l.to_socket) for l in output.inputs['Surface'].links]
        # 移除原始连接
        for link in list(output.inputs['Surface'].links):
            nt.links.remove(link)

    # 创建emission节点
    emit = nt.nodes.new('ShaderNodeEmission')
    emit.location = (-300, -800)
    emit.label = "Custom Shader Only Bake"
    
    try:
        # 连接自定义着色器到emission
        connection_made = False
        
        # 根据节点类型选择合适的输出
        if custom_shader_node.bl_idname == 'ShaderNodeNodeGroup':
            # 节点组处理
            shader_output_names = ['Shader', 'BSDF', 'Surface', 'Color', 'Output']
            for output_name in shader_output_names:
                if output_name in custom_shader_node.outputs:
                    try:
                        nt.links.new(custom_shader_node.outputs[output_name], emit.inputs['Color'])
                        connection_made = True
                        print(f"Custom shader only bake: 连接节点组输出 '{output_name}'")
                        break
                    except Exception:
                        continue
        elif custom_shader_node.bl_idname == 'ShaderNodeEmission':
            if 'Emission' in custom_shader_node.outputs:
                nt.links.new(custom_shader_node.outputs['Emission'], emit.inputs['Color'])
                connection_made = True
                print(f"Custom shader only bake: 连接Emission节点")
        else:
            # 其他BSDF节点
            if 'BSDF' in custom_shader_node.outputs:
                nt.links.new(custom_shader_node.outputs['BSDF'], emit.inputs['Color'])
                connection_made = True
                print(f"Custom shader only bake: 连接BSDF输出")
        
        # 如果没有成功连接，使用第一个可用输出
        if not connection_made and len(custom_shader_node.outputs) > 0:
            nt.links.new(custom_shader_node.outputs[0], emit.inputs['Color'])
            connection_made = True
            print(f"Custom shader only bake: 使用第一个输出 '{custom_shader_node.outputs[0].name}'")
        
        if connection_made:
            # 将emission连接到Material Output
            nt.links.new(emit.outputs['Emission'], output.inputs['Surface'])
            yield emit
        else:
            print(f"Custom shader only bake: 无法连接自定义着色器")
            yield None
        
    finally:
        # 清理和恢复
        # 移除emission节点的连接
        for link in list(emit.outputs['Emission'].links):
            nt.links.remove(link)
        for link in list(emit.inputs['Color'].links):
            nt.links.remove(link)
        
        # 移除emission节点
        nt.nodes.remove(emit)
        
        # 恢复原始连接
        for frm, to in original_links:
            nt.links.new(frm, to)


@contextmanager
def temporary_emission_input(nt, input_name, default_value=0.0):
    """Route any Principled BSDF input to Emission temporarily for baking."""
    output = next((n for n in nt.nodes if n.bl_idname == 'ShaderNodeOutputMaterial'), None)
    principled = next((n for n in nt.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
    if not output or not principled or input_name not in principled.inputs:
        yield None
        return

    # Save original links
    orig_links = [(l.from_socket, l.to_socket) for l in output.inputs['Surface'].links]
    for frm, to in orig_links:
        if frm.links:
            nt.links.remove(frm.links[0])

    # Create emission setup
    emit = nt.nodes.new('ShaderNodeEmission')
    emit.location = (-300, -600)
    tmp_nodes = [emit]

    try:
        target_input = principled.inputs[input_name]
        if target_input.is_linked:
            nt.links.new(target_input.links[0].from_socket, emit.inputs['Color'])
        else:
            rgb = nt.nodes.new('ShaderNodeRGB')
            val = target_input.default_value
            # Handle different input types
            if hasattr(val, '__len__') and len(val) >= 3:  # Color or Vector
                rgb.outputs['Color'].default_value = val
            else:  # Float
                rgb.outputs['Color'].default_value = (val, val, val, 1)
            nt.links.new(rgb.outputs['Color'], emit.inputs['Color'])
            tmp_nodes.append(rgb)
    except (KeyError, AttributeError, TypeError):
        # 如果无法访问输入，使用默认值
        rgb = nt.nodes.new('ShaderNodeRGB')
        rgb.outputs['Color'].default_value = (default_value, default_value, default_value, 1)
        nt.links.new(rgb.outputs['Color'], emit.inputs['Color'])
        tmp_nodes.append(rgb)

    nt.links.new(emit.outputs['Emission'], output.inputs['Surface'])

    try:
        yield tmp_nodes
    finally:
        # Clean up and restore
        for n in tmp_nodes:
            if n.outputs[0].links:
                nt.links.remove(n.outputs[0].links[0])
            nt.nodes.remove(n)
        for frm, to in orig_links:
            nt.links.new(frm, to)


@contextmanager
def temporary_emission_metallic(nt):
    """Route Metallic to Emission temporarily for baking."""
    with temporary_emission_input(nt, 'Metallic', 0.0) as temp_nodes:
        yield temp_nodes


# -----------------------------------------------------------------------------
# Bake Operator
# -----------------------------------------------------------------------------

class MBNL_OT_bake(Operator):
    bl_idname = "mbnl.bake"
    bl_label = "烘焙PBR贴图"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(subtype="DIR_PATH")
    resolution: IntProperty(name="分辨率", default=2048, min=16, max=16384)
    margin: IntProperty(name="边距", default=4, min=0, max=64)
    replace_nodes: BoolProperty(name="替换材质节点", default=False)
    include_lighting: BoolProperty(name="包含光照", default=False, description="烘焙时包含场景光照信息")
    lighting_shadow_mode: EnumProperty(
        name="阴影模式",
        description="光照烘焙的阴影处理模式",
        items=[
            ('WITH_SHADOWS', '包含阴影', '光照烘焙包含阴影(完整光照)'),
            ('NO_SHADOWS', '无阴影', '不包含阴影,仅包含直接光照'),
        ],
        default='WITH_SHADOWS'
    )
    organize_folders: BoolProperty(name="整理文件夹", default=True, description="为每个物体/材质/分辨率创建文件夹")
    
    # 多分辨率支持
    enable_multi_resolution: BoolProperty(name="多分辨率导出", default=False, description="同时导出多个分辨率的贴图")
    res_512: BoolProperty(name="512×512", default=False)
    res_1024: BoolProperty(name="1024×1024", default=True)
    res_2048: BoolProperty(name="2048×2048", default=True)
    res_4096: BoolProperty(name="4096×4096", default=False)
    res_8192: BoolProperty(name="8192×8192", default=False)
    
    # 自定义分辨率(支持矩形)
    enable_custom_resolution: BoolProperty(name="自定义分辨率", default=False, description="启用自定义分辨率输入")
    custom_width_1: IntProperty(name="宽度 1", default=1536, min=16, max=16384, description="第一个自定义分辨率宽度")
    custom_height_1: IntProperty(name="高度 1", default=1536, min=16, max=16384, description="第一个自定义分辨率高度")
    custom_width_2: IntProperty(name="宽度 2", default=1920, min=16, max=16384, description="第二个自定义分辨率宽度")
    custom_height_2: IntProperty(name="高度 2", default=1080, min=16, max=16384, description="第二个自定义分辨率高度")
    custom_width_3: IntProperty(name="宽度 3", default=1280, min=16, max=16384, description="第三个自定义分辨率宽度")
    custom_height_3: IntProperty(name="高度 3", default=720, min=16, max=16384, description="第三个自定义分辨率高度")
    use_custom_1: BoolProperty(name="启用自定义 1", default=False)
    use_custom_2: BoolProperty(name="启用自定义 2", default=False)
    use_custom_3: BoolProperty(name="启用自定义 3", default=False)

    # 基础PBR通道
    include_basecolor: BoolProperty(name="基础色", default=True)
    include_roughness: BoolProperty(name="粗糙度", default=True)
    include_metallic: BoolProperty(name="金属度", default=True)
    include_normal: BoolProperty(name="法线", default=True)
    
    # 高级PBR通道
    include_subsurface: BoolProperty(name="次表面散射", default=False)
    include_transmission: BoolProperty(name="透射", default=False)
    include_emission: BoolProperty(name="自发光", default=False)
    include_alpha: BoolProperty(name="透明度", default=False)
    include_specular: BoolProperty(name="高光", default=False)
    include_clearcoat: BoolProperty(name="清漆", default=False)
    include_clearcoat_roughness: BoolProperty(name="清漆粗糙度", default=False)
    include_sheen: BoolProperty(name="光泽", default=False)
    
    # 特殊通道
    include_displacement: BoolProperty(name="置换", default=False)
    include_ambient_occlusion: BoolProperty(name="环境光遮蔽", default=False)
    
    # 自定义着色器烘焙
    include_custom_shader: BoolProperty(name="自定义着色器", default=False, description="烘焙当前连接到材质输出的自定义着色器")
    
    # 混合着色器处理策略
    mixed_shader_strategy: EnumProperty(
        name="混合着色器策略",
        description="当材质同时包含Principled BSDF和自定义着色器时的处理策略",
        items=[
            ('SURFACE_OUTPUT', '完整表面输出', '烘焙完整的材质输出表面结果(推荐)'),
            ('PRINCIPLED_ONLY', '仅Principled BSDF', '只烘焙Principled BSDF部分,忽略自定义着色器'),
            ('CUSTOM_ONLY', '仅自定义着色器', '尝试只烘焙自定义着色器部分(实验性)'),
        ],
        default='SURFACE_OUTPUT'
    )
    
    # 多材质槽合并功能
    enable_material_atlas: BoolProperty(
        name="材质图集合并",
        description="将多个材质槽烘焙到同一张贴图",
        default=False
    )
    atlas_layout_mode: EnumProperty(
        name="图集布局",
        description="图集的布局模式",
        items=[
            ('AUTO', '自动布局', '自动计算最佳布局'),
            ('MANUAL', '手动布局', '手动指定行列数'),
        ],
        default='AUTO'
    )
    atlas_cols: IntProperty(
        name="列数",
        description="图集的列数",
        default=2,
        min=1,
        max=8
    )
    atlas_rows: IntProperty(
        name="行数", 
        description="图集的行数",
        default=2,
        min=1,
        max=8
    )
    atlas_padding: FloatProperty(
        name="间距",
        description="材质之间的间距(UV空间)",
        default=0.02,
        min=0.0,
        max=0.1
    )
    atlas_update_uv: BoolProperty(
        name="更新UV映射",
        description="为图集创建新的UV映射",
        default=True
    )
    
    # UDIM支持
    enable_udim: BoolProperty(
        name="UDIM支持",
        description="启用UDIM贴图烘焙,为每个UDIM贴图生成单独的贴图",
        default=False
    )
    udim_auto_detect: BoolProperty(
        name="自动检测UDIM",
        description="自动检测模型使用的UDIM贴图",
        default=True
    )
    udim_range_start: IntProperty(
        name="UDIM起始",
        description="UDIM贴图范围的起始编号",
        default=1001,
        min=1001,
        max=1100
    )
    udim_range_end: IntProperty(
        name="UDIM结束",
        description="UDIM贴图范围的结束编号",
        default=1010,
        min=1001,
        max=1100
    )
    udim_naming_mode: EnumProperty(
        name="UDIM命名模式",
        description="UDIM文件的命名规则",
        items=[
            ('STANDARD', '标准模式', 'material_name.1001.channel_name.png'),
            ('MARI', 'Mari模式', 'material_name_1001_channel_name.png'),
            ('MUDBOX', 'Mudbox模式', 'material_name.channel_name.1001.png'),
        ],
        default='STANDARD'
    )
    
    # 色彩空间管理
    colorspace_mode: EnumProperty(
        name="色彩空间模式",
        description="如何处理色彩空间分配",
        items=[
            ('AUTO', '自动检测', '根据通道类型自动分配合适的色彩空间'),
            ('CUSTOM', '自定义设置', '使用每种通道类型的自定义色彩空间设置'),
            ('MANUAL', '手动覆盖', '手动覆盖所有贴图的色彩空间'),
        ],
        default='AUTO'
    )
    
    # 不同通道类型的色彩空间分配
    colorspace_basecolor: EnumProperty(
        name="基础色",
        description="基础色/漫反射贴图的色彩空间",
        items=[
            ('sRGB', 'sRGB', '标准sRGB色彩空间(伽马校正)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB色彩空间'),
            ('Non-Color', '非颜色', '非颜色数据'),
            ('ACEScg', 'ACEScg', 'ACES工作色彩空间'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020色彩空间'),
        ],
        default='sRGB'
    )
    
    colorspace_normal: EnumProperty(
        name="法线贴图",
        description="法线贴图的色彩空间",
        items=[
            ('Non-Color', '非颜色', '非颜色数据(推荐用于法线贴图)'),
            ('sRGB', 'sRGB', 'sRGB色彩空间'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Raw', 'Raw', '原始颜色数据'),
        ],
        default='Non-Color'
    )
    
    colorspace_roughness: EnumProperty(
        name="粗糙度/金属度",
        description="粗糙度、金属度和其他数据贴图的色彩空间",
        items=[
            ('Non-Color', '非颜色', '非颜色数据(推荐用于数据贴图)'),
            ('sRGB', 'sRGB', 'sRGB色彩空间'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Raw', 'Raw', '原始颜色数据'),
        ],
        default='Non-Color'
    )
    
    colorspace_emission: EnumProperty(
        name="自发光",
        description="自发光贴图的色彩空间",
        items=[
            ('sRGB', 'sRGB', 'sRGB色彩空间(推荐用于自发光)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB色彩空间'),
            ('ACEScg', 'ACEScg', 'ACES工作色彩空间'),
            ('Non-Color', '非颜色', '非颜色数据'),
        ],
        default='sRGB'
    )
    
    colorspace_manual_override: EnumProperty(
        name="手动覆盖",
        description="手动覆盖模式下用于所有贴图的色彩空间",
        items=[
            ('sRGB', 'sRGB', 'sRGB色彩空间'),
            ('Non-Color', '非颜色', '非颜色数据'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB色彩空间'),
            ('ACEScg', 'ACEScg', 'ACES工作色彩空间'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020色彩空间'),
            ('Raw', 'Raw', '原始颜色数据'),
            ('XYZ', 'XYZ', 'CIE XYZ色彩空间'),
        ],
        default='sRGB'
    )

    def get_colorspace_for_channel(self, channel_suffix):
        """根据用户设置确定给定通道的适当色彩空间"""
        
        if self.colorspace_mode == 'MANUAL':
            return self.colorspace_manual_override
        
        # 定义自动色彩空间分配
        auto_colorspace_mapping = {
            'BaseColor': 'sRGB',
            'Diffuse': 'sRGB',
            'Albedo': 'sRGB',
            'Color': 'sRGB',
            'Emission': 'sRGB',
            'EmissionColor': 'sRGB',
            'Normal': 'Non-Color',
            'NormalMap': 'Non-Color',
            'Bump': 'Non-Color',
            'Height': 'Non-Color',
            'Displacement': 'Non-Color',
            'Roughness': 'Non-Color',
            'Metallic': 'Non-Color',
            'Specular': 'Non-Color',
            'Glossiness': 'Non-Color',
            'Alpha': 'Non-Color',
            'Opacity': 'Non-Color',
            'Transmission': 'Non-Color',
            'Subsurface': 'Non-Color',
            'SubsurfaceColor': 'sRGB',
            'Clearcoat': 'Non-Color',
            'ClearcoatRoughness': 'Non-Color',
            'Sheen': 'Non-Color',
            'SheenTint': 'sRGB',
            'AO': 'Non-Color',
            'AmbientOcclusion': 'Non-Color',
            'CustomShader': 'sRGB',  # 自定义着色器默认使用sRGB
        }
        
        if self.colorspace_mode == 'AUTO':
            return auto_colorspace_mapping.get(channel_suffix, 'Non-Color')
        
        elif self.colorspace_mode == 'CUSTOM':
            # 将通道映射到用户的自定义设置
            if channel_suffix in ['BaseColor', 'Diffuse', 'Albedo', 'Color']:
                return self.colorspace_basecolor
            elif channel_suffix in ['Normal', 'NormalMap', 'Bump']:
                return self.colorspace_normal
            elif channel_suffix in ['Roughness', 'Metallic', 'Specular', 'Glossiness', 'Alpha', 'Opacity', 
                                  'Transmission', 'Subsurface', 'Clearcoat', 'ClearcoatRoughness', 'Sheen', 
                                  'AO', 'AmbientOcclusion', 'Height', 'Displacement']:
                return self.colorspace_roughness
            elif channel_suffix in ['Emission', 'EmissionColor']:
                return self.colorspace_emission
            else:
                # 未知通道的默认回退
                return self.colorspace_roughness
        
        return 'Non-Color'  # 安全回退

    def set_image_colorspace(self, img, channel_suffix):
        """根据通道类型和用户偏好设置图像的色彩空间"""
        try:
            target_colorspace = self.get_colorspace_for_channel(channel_suffix)
            
            # 验证色彩空间在Blender中是否存在
            available_colorspaces = []
            
            # 安全检查bpy可用性
            try:
                if hasattr(bpy.types.ColorManagedViewSettings, 'bl_rna'):
                    # 尝试获取可用的色彩空间
                    try:
                        # 这是一个安全的方式来检查可用的色彩空间
                        available_colorspaces = ['sRGB', 'Non-Color', 'Linear Rec.709', 'Linear sRGB', 'Raw']
                        # 新版Blender的扩展列表
                        extended_colorspaces = ['ACEScg', 'Rec.2020', 'XYZ', 'Linear', 'Filmic Log']
                        
                        # 尝试访问色彩管理以查看可用内容
                        try:
                            scene = bpy.context.scene
                            view_settings = scene.view_settings
                            available_colorspaces.extend(extended_colorspaces)
                        except:
                            pass
                            
                    except:
                        # 回退到基本色彩空间
                        available_colorspaces = ['sRGB', 'Non-Color', 'Linear Rec.709', 'Raw']
            except NameError:
                # bpy在此作用域中不可用
                available_colorspaces = ['sRGB', 'Non-Color', 'Linear Rec.709', 'Raw']
            
            # 如果可用则应用色彩空间,否则使用回退选项
            if target_colorspace in available_colorspaces or not available_colorspaces:
                img.colorspace_settings.name = target_colorspace
                self.report({'INFO'}, f"设置{channel_suffix}的色彩空间为{target_colorspace}")
            else:
                # 回退逻辑
                if channel_suffix in ['BaseColor', 'Emission', 'CustomShader']:
                    fallback = 'sRGB'
                else:
                    fallback = 'Non-Color'
                
                img.colorspace_settings.name = fallback
                self.report({'WARNING'}, f"色彩空间'{target_colorspace}'不可用,对{channel_suffix}使用'{fallback}'")
                
        except (AttributeError, KeyError) as e:
            self.report({'WARNING'}, f"无法为{channel_suffix}设置色彩空间: {str(e)}")
        except Exception as e:
            self.report({'ERROR'}, f"为{channel_suffix}设置色彩空间时发生意外错误: {str(e)}")

    def bake_generic(self, context, bake_type, img, margin, pass_filter=None, use_lighting=False, shadow_mode='WITH_SHADOWS'):
        scene = context.scene
        scene.cycles.bake_type = bake_type
        
        # 保存原始烘焙设置
        original_use_pass_direct = scene.render.bake.use_pass_direct
        original_use_pass_indirect = scene.render.bake.use_pass_indirect
        original_use_pass_color = scene.render.bake.use_pass_color
        
        # 保存法线烘焙相关设置
        original_normal_space = None
        original_normal_r = None
        original_normal_g = None
        original_normal_b = None
        
        if bake_type == 'NORMAL':
            # 保存原始法线设置
            original_normal_space = scene.render.bake.normal_space
            original_normal_r = scene.render.bake.normal_r
            original_normal_g = scene.render.bake.normal_g  
            original_normal_b = scene.render.bake.normal_b
            
            # 设置切线空间法线 (Tangent Space) - 这是游戏和实时渲染的标准
            scene.render.bake.normal_space = 'TANGENT'
            scene.render.bake.normal_r = 'POS_X'  # Red = +X
            scene.render.bake.normal_g = 'POS_Y'  # Green = +Y  
            scene.render.bake.normal_b = 'POS_Z'  # Blue = +Z
            
            # 报告法线烘焙设置
            print(f"法线烘焙设置: 切线空间 (Tangent Space), RGB=+X+Y+Z")
        
        # 保存原始光源设置 (用于阴影控制)
        original_light_settings = []
        
        try:
            # 配置光影烘焙设置
            if use_lighting and bake_type == 'COMBINED':
                # 根据阴影模式配置光源
                if shadow_mode == 'NO_SHADOWS':
                    # 无阴影模式：临时禁用所有光源的阴影
                    for obj in scene.objects:
                        if obj.type == 'LIGHT' and obj.data:
                            original_light_settings.append({
                                'object': obj,
                                'cast_shadow': obj.data.use_shadow if hasattr(obj.data, 'use_shadow') else True
                            })
                            if hasattr(obj.data, 'use_shadow'):
                                obj.data.use_shadow = False
                    
                    self.report({'INFO'}, f"No shadows mode: Disabled shadows for {len(original_light_settings)} light sources")
                else:
                    self.report({'INFO'}, "With shadows mode: Keeping all shadow settings")
                
                # 启用直接光照和间接光照
                scene.render.bake.use_pass_direct = True
                scene.render.bake.use_pass_indirect = True
                scene.render.bake.use_pass_color = True
                
                # 确保有足够的光线反弹次数
                if scene.cycles.max_bounces < 4:
                    scene.cycles.max_bounces = 4
                
                self.report({'INFO'}, "光影烘焙设置已启用：直接光照 + 间接光照 + 颜色传递")
            
            if pass_filter:
                # 如果pass_filter是set类型，转换为Blender期望的格式
                if isinstance(pass_filter, set):
                    bpy.ops.object.bake(type=bake_type, pass_filter=pass_filter, margin=margin, use_clear=True)
                else:
                    bpy.ops.object.bake(type=bake_type, pass_filter={pass_filter}, margin=margin, use_clear=True)
            else:
                bpy.ops.object.bake(type=bake_type, margin=margin, use_clear=True)
        
        finally:
            # 恢复原始设置
            scene.render.bake.use_pass_direct = original_use_pass_direct
            scene.render.bake.use_pass_indirect = original_use_pass_indirect
            scene.render.bake.use_pass_color = original_use_pass_color
            
            # 恢复法线烘焙设置
            if bake_type == 'NORMAL' and original_normal_space is not None:
                scene.render.bake.normal_space = original_normal_space
                scene.render.bake.normal_r = original_normal_r
                scene.render.bake.normal_g = original_normal_g
                scene.render.bake.normal_b = original_normal_b
            
            # 恢复原始光源设置
            for light_setting in original_light_settings:
                obj = light_setting['object']
                if obj and obj.data and hasattr(obj.data, 'use_shadow'):
                    obj.data.use_shadow = light_setting['cast_shadow']

    def execute(self, context):
        ensure_cycles(context.scene)
        
        # 确定输出目录
        if context.scene.mbnl_use_custom_directory and context.scene.mbnl_custom_directory:
            directory = bpy.path.abspath(context.scene.mbnl_custom_directory)
            if not os.path.exists(directory):
                try:
                    os.makedirs(directory, exist_ok=True)
                    self.report({'INFO'}, f"创建自定义输出目录: {directory}")
                except Exception as e:
                    self.report({'ERROR'}, f"无法创建自定义目录: {str(e)}，使用默认目录")
                    directory = bpy.path.abspath("//")
            else:
                self.report({'INFO'}, f"使用自定义输出目录: {directory}")
        else:
            directory = self.directory if self.directory else bpy.path.abspath("//")
            if not context.scene.mbnl_use_custom_directory:
                self.report({'INFO'}, f"使用默认输出目录: {directory}")
        
        # 检查光源设置（当启用光影烘焙时）
        if self.include_lighting:
            lights = [obj for obj in context.scene.objects if obj.type == 'LIGHT']
            if not lights:
                # 检查世界环境光
                world = context.scene.world
                has_world_light = False
                if world and world.use_nodes:
                    for node in world.node_tree.nodes:
                        if node.type in ['TEX_ENVIRONMENT', 'TEX_SKY'] or node.bl_idname == 'ShaderNodeBackground':
                            # 检查是否有实际的光强度
                            if node.bl_idname == 'ShaderNodeBackground':
                                try:
                                    strength = node.inputs['Strength'].default_value if 'Strength' in node.inputs else 1.0
                                    if strength > 0.01:
                                        has_world_light = True
                                        break
                                except:
                                    has_world_light = True
                                    break
                            else:
                                has_world_light = True
                                break
                
                if not has_world_light:
                    self.report({'WARNING'}, "启用光影烘焙但场景中没有光源，烘焙结果可能较暗。建议添加光源或世界环境光。")
                else:
                    self.report({'INFO'}, "使用世界环境光进行光影烘焙")
            else:
                light_types = {}
                for light in lights:
                    light_type = light.data.type
                    light_types[light_type] = light_types.get(light_type, 0) + 1
                
                light_info = ", ".join([f"{count}个{ltype}" for ltype, count in light_types.items()])
                self.report({'INFO'}, f"找到光源: {light_info}，将进行光影烘焙")
        
        # 检查是否有选中的物体
        selected_objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not selected_objects:
            self.report({'WARNING'}, "请选择至少一个网格物体")
            return {'CANCELLED'}

        total_materials = 0
        processed_materials = 0
        
        # 计算总材质数量
        for obj in selected_objects:
            total_materials += len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
        
        if total_materials == 0:
            self.report({'WARNING'}, "所选物体没有可用的材质")
            return {'CANCELLED'}

        # Normal baking pre-checks
        if self.include_normal:
            # Check UV mapping for normal baking
            uv_issues = []
            for obj in selected_objects:
                if not obj.data.uv_layers:
                    uv_issues.append(obj.name)
                else:
                    # Check if UV layer has valid data
                    uv_layer = obj.data.uv_layers.active
                    if not uv_layer:
                        uv_issues.append(f"{obj.name} (no active UV)")
            
            if uv_issues:
                self.report({'WARNING'}, f"Objects missing proper UV mapping for normal baking: {', '.join(uv_issues)}. Normal baking may fail.")
            
            # Check for auto smooth settings
            auto_smooth_info = []
            for obj in selected_objects:
                if hasattr(obj.data, 'use_auto_smooth'):
                    if not obj.data.use_auto_smooth:
                        auto_smooth_info.append(obj.name)
            
            if auto_smooth_info:
                self.report({'INFO'}, f"Consider enabling Auto Smooth for better normal baking results on: {', '.join(auto_smooth_info)}")
            
            self.report({'INFO'}, "Normal baking will use Tangent Space (standard for games/realtime rendering)")

        # Provide additional information and optimization settings for lighting baking
        if self.include_lighting:
            scene = context.scene
            
            # 保存原始设置
            original_samples = scene.cycles.samples
            original_device = scene.cycles.device
            
            # 确保有足够的采样数进行光影烘焙
            min_samples = 128
            if hasattr(scene.cycles, 'samples'):
                if scene.cycles.samples < min_samples:
                    scene.cycles.samples = min_samples
                    self.report({'INFO'}, f"光影烘焙：采样数已调整为 {min_samples} 以确保质量")
                else:
                    self.report({'INFO'}, f"光影烘焙：当前采样数 {scene.cycles.samples}")
            
            # 检查GPU加速状态
            try:
                if scene.cycles.device == 'CPU':
                    # 尝试检查是否有可用的GPU设备
                    cycles_prefs = bpy.context.preferences.addons.get('cycles')
                    if cycles_prefs and hasattr(cycles_prefs.preferences, 'devices'):
                        gpu_devices = [device for device in cycles_prefs.preferences.devices if device.type in ['CUDA', 'OPENCL', 'OPTIX', 'HIP']]
                        if gpu_devices:
                            scene.cycles.device = 'GPU'
                            self.report({'INFO'}, "光影烘焙：已启用GPU加速以提高烘焙速度")
                        else:
                            self.report({'INFO'}, "光影烘焙：使用CPU渲染（未检测到可用GPU）")
                    else:
                        self.report({'INFO'}, "光影烘焙：使用CPU渲染")
                else:
                    self.report({'INFO'}, "光影烘焙：已使用GPU加速")
            except Exception as e:
                self.report({'INFO'}, f"光影烘焙：GPU检测失败，使用当前设备: {scene.cycles.device}")
            
            # 设置适当的去噪选项
            if hasattr(scene.cycles, 'use_denoising'):
                scene.cycles.use_denoising = True
                self.report({'INFO'}, "光影烘焙：已启用去噪以改善质量")
            
            self.report({'INFO'}, "注意: 光影烘焙质量取决于场景光照设置和渲染采样数")
        
        # 文件夹组织信息
        if self.organize_folders:
            self.report({'INFO'}, "文件夹组织已启用 - 文件将按 物体/材质/分辨率 结构保存")
        else:
            self.report({'INFO'}, "使用传统文件命名 - 所有文件保存在同一目录")
        
        # 确定要烘焙的分辨率列表
        if self.enable_multi_resolution:
            resolutions = []
            
            # 添加预设分辨率
            if self.res_512:
                resolutions.append(512)
            if self.res_1024:
                resolutions.append(1024)
            if self.res_2048:
                resolutions.append(2048)
            if self.res_4096:
                resolutions.append(4096)
            if self.res_8192:
                resolutions.append(8192)
            
            # 添加自定义分辨率（支持矩形）
            custom_resolutions = []
            if self.enable_custom_resolution:
                if self.use_custom_1:
                    custom_res = (self.custom_width_1, self.custom_height_1)
                    if custom_res not in custom_resolutions:
                        custom_resolutions.append(custom_res)
                if self.use_custom_2:
                    custom_res = (self.custom_width_2, self.custom_height_2)
                    if custom_res not in custom_resolutions:
                        custom_resolutions.append(custom_res)
                if self.use_custom_3:
                    custom_res = (self.custom_width_3, self.custom_height_3)
                    if custom_res not in custom_resolutions:
                        custom_resolutions.append(custom_res)
            
            # 转换方形分辨率为元组格式，便于统一处理
            square_resolutions = [(res, res) for res in resolutions]
            all_resolutions = square_resolutions + custom_resolutions
            
            # 去除重复并按面积排序（更合理的排序方式）
            unique_resolutions = []
            for res in all_resolutions:
                if res not in unique_resolutions:
                    unique_resolutions.append(res)
            resolutions = sorted(unique_resolutions, key=lambda x: x[0] * x[1])
            
            if not resolutions:
                self.report({'WARNING'}, "启用多分辨率但未选择任何分辨率，使用默认分辨率")
                resolutions = [(self.resolution, self.resolution)]
            else:
                preset_info = []
                custom_info = []
                
                for width, height in resolutions:
                    if width == height and width in [512, 1024, 2048, 4096, 8192]:
                        preset_info.append(f'{width}×{height}')
                    else:
                        if width == height:
                            custom_info.append(f'{width}×{height}(自定义)')
                        else:
                            custom_info.append(f'{width}×{height}(自定义)')
                
                all_info = preset_info + custom_info
                self.report({'INFO'}, f"将导出以下分辨率: {', '.join(all_info)}")
        else:
            resolutions = [(self.resolution, self.resolution)]

        self.report({'INFO'}, f"开始处理 {len(selected_objects)} 个物体，共 {total_materials} 个材质")

        for obj in selected_objects:
            # 确保物体有UV层
            if not obj.data.uv_layers:
                self.report({'INFO'}, f"为物体 '{obj.name}' 自动创建UV映射")
                smart_uv(obj)
            
            context.view_layer.objects.active = obj

            # UDIM检测和设置
            udim_tiles = []
            if self.enable_udim:
                if self.udim_auto_detect:
                    udim_tiles = detect_udim_tiles(obj)
                    if udim_tiles:
                        self.report({'INFO'}, f"检测到UDIM瓦片: {udim_tiles}")
                    else:
                        self.report({'WARNING'}, f"物体 '{obj.name}' 未检测到UDIM瓦片，将使用常规烘焙")
                        udim_tiles = [1001]  # 默认使用1001瓦片
                else:
                    # 使用指定范围
                    udim_tiles = list(range(self.udim_range_start, self.udim_range_end + 1))
                    self.report({'INFO'}, f"使用指定UDIM范围: {udim_tiles}")
            else:
                # 非UDIM模式，使用虚拟瓦片1001
                udim_tiles = [1001]

            # 处理物体的所有材质槽
            material_slots = [slot for slot in obj.material_slots if slot.material and slot.material.use_nodes]
            
            if not material_slots:
                self.report({'INFO'}, f"物体 '{obj.name}' 没有可用的材质，跳过")
                continue
                
            self.report({'INFO'}, f"处理物体 '{obj.name}' 的 {len(material_slots)} 个材质")

            for slot in material_slots:
                mat = slot.material
                processed_materials += 1
                
                # 分析材质类型（带错误处理）
                try:
                    analysis = analyze_material(mat)
                    material_type = analysis.get('material_type', 'unknown')
                except Exception as e:
                    self.report({'WARNING'}, f"材质 '{mat.name}' 分析失败，使用默认处理: {str(e)}")
                    material_type = 'unknown'
                    analysis = {'material_type': 'unknown', 'has_image_textures': False}
                
                safe_mat_name = safe_encode_text(mat.name, "未命名材质")
                self.report({'INFO'}, f"正在处理材质 '{safe_mat_name}' ({processed_materials}/{total_materials}) - 类型: {material_type}")
                
                # 根据材质类型决定处理策略
                if material_type == 'unknown':
                    self.report({'INFO'}, f"材质 '{mat.name}' 类型未知，尝试默认处理")
                elif material_type == 'default':
                    self.report({'INFO'}, f"材质 '{mat.name}' 使用默认设置，将烘焙默认值")
                elif material_type == 'textured':
                    texture_count = len(analysis.get('texture_nodes', []))
                    self.report({'INFO'}, f"材质 '{mat.name}' 包含 {texture_count} 个图像纹理")
                elif material_type == 'procedural':
                    pure_colors = analysis.get('pure_color_inputs', [])
                    if pure_colors:
                        self.report({'INFO'}, f"材质 '{mat.name}' 使用纯色值: {', '.join(pure_colors)}")
                    else:
                        self.report({'INFO'}, f"材质 '{mat.name}' 使用程序化节点")
                elif material_type == 'mixed':
                    self.report({'INFO'}, f"材质 '{mat.name}' 混合了纹理和纯色值")
                elif material_type == 'custom_shader':
                    custom_shaders = analysis.get('custom_shaders', [])
                    shader_names = [shader['label'] for shader in custom_shaders[:3]]  # 显示前3个
                    if len(custom_shaders) <= 3:
                        self.report({'INFO'}, f"材质 '{mat.name}' 使用自定义着色器: {', '.join(shader_names)}")
                    else:
                        self.report({'INFO'}, f"材质 '{mat.name}' 使用自定义着色器: {', '.join(shader_names)} 等{len(custom_shaders)}个")
                elif material_type == 'mixed_shader':
                    custom_count = len(analysis.get('custom_shaders', []))
                    self.report({'INFO'}, f"材质 '{mat.name}' 混合了Principled BSDF和{custom_count}个自定义着色器")
                elif material_type == 'mixed_shader_network':
                    mix_info = analysis.get('shader_network', {})
                    mix_node_name = mix_info.get('mix_node', {}).get('name', '未知')
                    self.report({'INFO'}, f"材质 '{mat.name}' 使用混合着色器网络 (Mix节点: {mix_node_name})")
                    self.report({'INFO'}, f"混合着色器策略: {self.mixed_shader_strategy}")
                elif material_type == 'principled_with_custom':
                    custom_count = len(analysis.get('custom_shaders', []))
                    self.report({'INFO'}, f"材质 '{mat.name}' 以Principled BSDF为主，含{custom_count}个自定义着色器")
                elif material_type == 'custom_with_principled':
                    self.report({'INFO'}, f"材质 '{mat.name}' 以自定义着色器为主，含Principled BSDF")

                nt = mat.node_tree
                bake_node = nt.nodes.new("ShaderNodeTexImage")
                bake_node.select = True
                nt.nodes.active = bake_node

                # 为不同分辨率存储烘焙的图像
                all_baked_images = {}  # 格式: {(width, height): {suffix: image}}
                primary_resolution = max(resolutions, key=lambda x: x[0] * x[1])  # 用于节点重建的主分辨率（最大面积）

                # 创建适合当前Blender版本的输入名称映射
                input_mapping = create_input_mapping()

                # Build pass list based on selections
                passes = []
                
                # 基础PBR通道
                if self.include_basecolor:
                    if self.include_lighting:
                        # 包含光影：使用COMBINED烘焙来捕获完整的场景光照
                        passes.append(('BaseColor', 'COMBINED', None, True, None))
                        self.report({'INFO'}, f"材质 '{mat.name}' 的基础色将包含场景光照（COMBINED方法）")
                    else:
                        # 不包含光影：使用emission烘焙确保能正确捕获基础色，无论金属度如何
                        passes.append(('BaseColor', 'EMIT', None, True, input_mapping.get('BaseColor')))
                if self.include_roughness:
                    # 对于纯色材质，考虑使用emission烘焙作为备选
                    if material_type in ['procedural', 'default'] and not analysis.get('has_image_textures', False):
                        passes.append(('Roughness', 'EMIT', None, False, input_mapping.get('Roughness')))
                        self.report({'INFO'}, f"材质 '{mat.name}' 的粗糙度将使用Emission方法烘焙（纯色材质）")
                    else:
                        passes.append(('Roughness', 'ROUGHNESS', None, False, None))
                if self.include_metallic:
                    passes.append(('Metallic', 'EMIT', None, False, input_mapping.get('Metallic')))
                if self.include_normal:
                    passes.append(('Normal', 'NORMAL', None, False, None))
                
                # 高级PBR通道
                if self.include_subsurface:
                    passes.append(('Subsurface', 'EMIT', None, False, input_mapping.get('Subsurface')))
                if self.include_transmission:
                    passes.append(('Transmission', 'EMIT', None, False, input_mapping.get('Transmission')))
                if self.include_emission:
                    passes.append(('Emission', 'EMIT', None, False, input_mapping.get('Emission')))
                if self.include_alpha:
                    passes.append(('Alpha', 'EMIT', None, False, input_mapping.get('Alpha')))
                if self.include_specular:
                    passes.append(('Specular', 'EMIT', None, False, input_mapping.get('Specular')))
                if self.include_clearcoat:
                    passes.append(('Clearcoat', 'EMIT', None, False, input_mapping.get('Clearcoat')))
                if self.include_clearcoat_roughness:
                    passes.append(('ClearcoatRoughness', 'EMIT', None, False, input_mapping.get('ClearcoatRoughness')))
                if self.include_sheen:
                    passes.append(('Sheen', 'EMIT', None, False, input_mapping.get('Sheen')))
                
                # 特殊通道（不使用emission方式）
                if self.include_displacement:
                    passes.append(('Displacement', 'EMIT', None, False, None))  # 不通过principled输入
                if self.include_ambient_occlusion:
                    passes.append(('AO', 'AO', None, False, None))
                
                # 自定义着色器烘焙
                if self.include_custom_shader:
                    # 检查是否有连接到Material Output的着色器
                    output_node = analysis.get('output_node')
                    if output_node and output_node.inputs['Surface'].is_linked:
                        passes.append(('CustomShader', 'EMIT', None, True, None))  # 使用特殊的标识符来表示自定义着色器
                        self.report({'INFO'}, f"材质 '{mat.name}' 将烘焙自定义着色器输出")
                    else:
                        self.report({'WARNING'}, f"材质 '{mat.name}' 的Material Output未连接着色器，跳过自定义着色器烘焙")

                # Bake for each resolution
                for res_idx, (width, height) in enumerate(resolutions):
                    self.report({'INFO'}, f"Starting baking at resolution {width}×{height} ({res_idx + 1}/{len(resolutions)})")
                    
                    try:
                        resolution_key = (width, height)
                        if resolution_key not in all_baked_images:
                            all_baked_images[resolution_key] = {}
                        
                        # UDIM tile loop
                        for udim_tile in udim_tiles:
                            if self.enable_udim and len(udim_tiles) > 1:
                                self.report({'INFO'}, f"Processing UDIM tile {udim_tile}")
                                
                                # Set UDIM baking area
                                if udim_tile != 1001:  # Only non-default tiles need special handling
                                    original_uvs = normalize_udim_uvs_for_baking(obj, udim_tile)
                                else:
                                    original_uvs = {}
                            else:
                                original_uvs = {}
                            
                            try:
                                # Bake for each channel
                                for suffix, btype, pfilter, alpha, emission_input in passes:
                                    # 生成文件名和路径
                                    if self.organize_folders:
                                        # 使用文件夹组织: 物体/材质/分辨率/贴图
                                        if width == height:
                                            res_folder = f"{width}x{height}"
                                        else:
                                            res_folder = f"{width}x{height}"
                                        
                                        # 清理文件夹名称（移除非法字符）
                                        encoded_obj_name = safe_encode_text(obj.name, "Unknown_Object")
                                        encoded_mat_name = safe_encode_text(mat.name, "Unknown_Material")
                                        safe_obj_name = "".join(c for c in encoded_obj_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                                        safe_mat_name = "".join(c for c in encoded_mat_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                                        
                                        # 确保文件夹名称不为空
                                        if not safe_obj_name:
                                            safe_obj_name = "Object"
                                        if not safe_mat_name:
                                            safe_mat_name = "Material"
                                        
                                        folder_path = os.path.join(directory, safe_obj_name, safe_mat_name, res_folder)
                                        os.makedirs(folder_path, exist_ok=True)
                                        
                                        # UDIM文件命名
                                        if self.enable_udim and udim_tile != 1001:
                                            if self.udim_naming_mode == 'STANDARD':
                                                img_name = f"{safe_mat_name}.{udim_tile}.{suffix.lower()}"
                                            elif self.udim_naming_mode == 'MARI':
                                                img_name = f"{safe_mat_name}_{udim_tile}_{suffix.lower()}"
                                            elif self.udim_naming_mode == 'MUDBOX':
                                                img_name = f"{safe_mat_name}.{suffix.lower()}.{udim_tile}"
                                        else:
                                            img_name = f"{suffix.lower()}"
                                        
                                        blender_img_name = f"{safe_obj_name}_{safe_mat_name}_{suffix.lower()}_{res_folder}"
                                        if self.enable_udim and udim_tile != 1001:
                                            blender_img_name += f"_{udim_tile}"
                                        full_path = os.path.join(folder_path, img_name + ".png")
                                    else:
                                        # 传统命名方式
                                        encoded_obj_name = safe_encode_text(obj.name, "Object")
                                        encoded_mat_name = safe_encode_text(mat.name, "Material")
                                        safe_obj_name = "".join(c for c in encoded_obj_name if c.isalnum() or c in ('_', '-')).strip()
                                        safe_mat_name = "".join(c for c in encoded_mat_name if c.isalnum() or c in ('_', '-')).strip()
                                        
                                        # 确保名称不为空
                                        if not safe_obj_name:
                                            safe_obj_name = "Object"
                                        if not safe_mat_name:
                                            safe_mat_name = "Material"
                                        
                                        if len(resolutions) > 1:
                                            if width == height:
                                                img_name = f"{safe_obj_name}_{safe_mat_name}_{suffix.lower()}_{width}"
                                            else:
                                                img_name = f"{safe_obj_name}_{safe_mat_name}_{suffix.lower()}_{width}x{height}"
                                        else:
                                            img_name = f"{safe_obj_name}_{safe_mat_name}_{suffix.lower()}"
                                        
                                        full_path = os.path.join(directory, img_name + ".png")
                                    
                                    # 使用适当的Blender图像名称
                                    if self.organize_folders:
                                        img = bpy.data.images.new(blender_img_name, width=width, height=height, alpha=alpha)
                                    else:
                                        img = bpy.data.images.new(img_name, width=width, height=height, alpha=alpha)
                                    
                                    # Set color space using the new system
                                    self.set_image_colorspace(img, suffix)
                                    
                                    bake_node.image = img

                                    # 根据材质类型调整烘焙策略
                                    should_bake = True
                                    
                                    # 对于只有纯色的材质，检查是否有必要烘焙某些通道
                                    if material_type in ['procedural', 'default'] and not analysis['has_image_textures']:
                                        # 检查特定输入是否有意义烘焙
                                        if emission_input:
                                            principled = analysis.get('principled_node')
                                            if principled and emission_input in principled.inputs:
                                                input_socket = principled.inputs[emission_input]
                                                if not input_socket.is_linked:
                                                    # 检查是否是默认值
                                                    try:
                                                        default_val = input_socket.default_value
                                                        if suffix == 'Metallic' and abs(default_val) < 0.01:
                                                            self.report({'INFO'}, f"跳过 {suffix} 烘焙 - 使用默认值 {default_val}")
                                                            should_bake = False
                                                        elif suffix == 'Roughness' and abs(default_val - 0.5) < 0.01:
                                                            self.report({'INFO'}, f"跳过 {suffix} 烘焙 - 使用默认值 {default_val}")
                                                            should_bake = False
                                                        elif suffix in ['Subsurface', 'Transmission', 'Specular', 'Clearcoat', 'Sheen'] and abs(default_val) < 0.01:
                                                            self.report({'INFO'}, f"跳过 {suffix} 烘焙 - 使用默认值 {default_val}")
                                                            should_bake = False
                                                    except (AttributeError, TypeError):
                                                        pass
                            
                                    if should_bake:
                                        try:
                                            # 检查是否是光影烘焙的基础色
                                            is_lighting_basecolor = (suffix == 'BaseColor' and self.include_lighting)
                                            # 检查是否是自定义着色器
                                            is_custom_shader = (suffix == 'CustomShader')
                                            
                                            if is_custom_shader:  # 自定义着色器烘焙
                                                self.report({'INFO'}, f"使用Emission方法烘焙自定义着色器输出")
                                                
                                                # 检查是否为混合着色器材质，如果是则应用策略
                                                if material_type in ['mixed_shader_network', 'principled_with_custom', 'custom_with_principled']:
                                                    self.report({'INFO'}, f"检测到混合着色器材质，应用策略: {self.mixed_shader_strategy}")
                                                    
                                                    if self.mixed_shader_strategy == 'PRINCIPLED_ONLY':
                                                        # 尝试只烘焙Principled BSDF部分
                                                        principled_node = analysis.get('principled_node')
                                                        if principled_node:
                                                            self.report({'INFO'}, f"根据策略，只烘焙Principled BSDF部分")
                                                            with temporary_principled_only_surface(nt, principled_node) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                                else:
                                                                    self.report({'ERROR'}, f"无法设置仅Principled BSDF烘焙，回退到完整表面输出")
                                                                    with temporary_emission_surface(nt) as temp_emit:
                                                                        if temp_emit:
                                                                            self.bake_generic(context, btype, img, self.margin)
                                                        else:
                                                            self.report({'WARNING'}, f"未找到Principled BSDF节点，使用完整表面输出")
                                                            with temporary_emission_surface(nt) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                    elif self.mixed_shader_strategy == 'CUSTOM_ONLY':
                                                        # 尝试只烘焙自定义着色器部分
                                                        self.report({'INFO'}, f"根据策略，尝试只烘焙自定义着色器部分（实验性）")
                                                        custom_shaders = analysis.get('custom_shaders', [])
                                                        if custom_shaders:
                                                            # 选择第一个自定义着色器
                                                            first_custom = custom_shaders[0]['node']
                                                            with temporary_custom_shader_only_surface(nt, first_custom) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                                else:
                                                                    self.report({'ERROR'}, f"无法设置仅自定义着色器烘焙，回退到完整表面输出")
                                                                    with temporary_emission_surface(nt) as temp_emit:
                                                                        if temp_emit:
                                                                            self.bake_generic(context, btype, img, self.margin)
                                                        else:
                                                            self.report({'WARNING'}, f"未找到自定义着色器节点，使用完整表面输出")
                                                            with temporary_emission_surface(nt) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                    else:  # SURFACE_OUTPUT 或默认
                                                        self.report({'INFO'}, f"使用完整表面输出策略")
                                                        with temporary_emission_surface(nt) as temp_emit:
                                                            if temp_emit:
                                                                self.bake_generic(context, btype, img, self.margin)
                                                            else:
                                                                self.report({'ERROR'}, f"无法设置自定义着色器烘焙，跳过 {suffix}")
                                                else:
                                                    # 非混合着色器材质，使用原有逻辑
                                                    # 获取材质输出节点和连接的着色器信息
                                                    output_node = analysis.get('output_node')
                                                    if output_node and output_node.inputs['Surface'].is_linked:
                                                        shader_node = output_node.inputs['Surface'].links[0].from_node
                                                        shader_type = shader_node.bl_idname
                                                        shader_name = shader_node.name
                                                        self.report({'INFO'}, f"检测到着色器类型: {shader_type} ('{shader_name}')")
                                                        
                                                        # 如果是节点组，显示更多信息
                                                        if shader_type == 'ShaderNodeNodeGroup':
                                                            if hasattr(shader_node, 'node_tree') and shader_node.node_tree:
                                                                group_name = shader_node.node_tree.name
                                                                self.report({'INFO'}, f"节点组名称: {group_name}")
                                                                # 显示节点组的输出
                                                                outputs = list(shader_node.outputs.keys())
                                                                self.report({'INFO'}, f"节点组输出: {outputs}")
                                                
                                                    with temporary_emission_surface(nt) as temp_emit:
                                                        if temp_emit:
                                                            self.bake_generic(context, btype, img, self.margin)
                                                        else:
                                                            self.report({'ERROR'}, f"无法设置自定义着色器烘焙，跳过 {suffix}")
                                            elif emission_input and not is_lighting_basecolor:  # 需要通过emission烘焙的通道（除了光影基础色）
                                                if suffix == 'BaseColor' and not self.include_lighting:
                                                    self.report({'INFO'}, f"使用Emission方法烘焙基础色以确保正确捕获纯色值")
                                                with temporary_emission_input(nt, emission_input):
                                                    self.bake_generic(context, btype, img, self.margin)
                                            elif is_lighting_basecolor:  # 光影烘焙的基础色，保持原材质不变
                                                self.report({'INFO'}, f"使用COMBINED方法烘焙基础色，保持原材质设置以捕获光照")
                                                
                                                # 确保材质设置适合光影烘焙
                                                principled = analysis.get('principled_node')
                                                if principled:
                                                    # 临时调整一些设置以确保更好的光影捕获
                                                    original_metallic = None
                                                    original_roughness = None
                                                    
                                                    try:
                                                        # 如果金属度太高，临时降低以获得更好的漫反射信息
                                                        if 'Metallic' in principled.inputs and not principled.inputs['Metallic'].is_linked:
                                                            original_metallic = principled.inputs['Metallic'].default_value
                                                            if original_metallic > 0.8:
                                                                principled.inputs['Metallic'].default_value = 0.2
                                                                self.report({'INFO'}, f"光影烘焙：临时降低金属度从 {original_metallic:.2f} 到 0.2 以更好捕获光照")
                                                        
                                                        # 进行烘焙
                                                        self.bake_generic(context, btype, img, self.margin, use_lighting=True)
                                                        
                                                    finally:
                                                        # 恢复原始材质设置
                                                        if original_metallic is not None:
                                                            principled.inputs['Metallic'].default_value = original_metallic
                                                else:
                                                    # 如果没有Principled BSDF，直接烘焙
                                                    self.bake_generic(context, btype, img, self.margin, use_lighting=True)
                                            else:  # 直接烘焙的通道
                                                if pfilter:
                                                    # 使用特定的pass filter进行烘焙
                                                    self.bake_generic(context, btype, img, self.margin, pfilter)
                                                else:
                                                    self.bake_generic(context, btype, img, self.margin)

                                            img.filepath_raw = full_path
                                            img.file_format = 'PNG'
                                            img.save()
                                            all_baked_images[resolution_key][suffix] = img
                                            
                                            if self.organize_folders:
                                                relative_path = os.path.relpath(full_path, directory)
                                                self.report({'INFO'}, f"成功烘焙 {suffix} 贴图 ({width}×{height}): {relative_path}")
                                            else:
                                                self.report({'INFO'}, f"成功烘焙 {suffix} 贴图 ({width}×{height}): {img_name}.png")
                                                
                                        except Exception as e:
                                            self.report({'ERROR'}, f"烘焙 {suffix} 贴图失败 ({width}×{height}): {str(e)}")
                                            # 清理失败的图像
                                            if img.name in bpy.data.images:
                                                bpy.data.images.remove(img)
                                    else:
                                        # 对于跳过的通道，创建一个纯色图像
                                        try:
                                            principled = analysis.get('principled_node')
                                            if principled and emission_input and emission_input in principled.inputs:
                                                input_socket = principled.inputs[emission_input]
                                                if not input_socket.is_linked:
                                                    default_val = input_socket.default_value
                                                    # 创建纯色图像
                                                    pixels = []
                                                    total_pixels = width * height
                                                    
                                                    if hasattr(default_val, '__len__') and len(default_val) >= 3:
                                                        # 颜色值
                                                        for _ in range(total_pixels):
                                                            pixels.extend([default_val[0], default_val[1], default_val[2], 1.0])
                                                    else:
                                                        # 浮点值
                                                        for _ in range(total_pixels):
                                                            pixels.extend([default_val, default_val, default_val, 1.0])
                                                    
                                                    img.pixels = pixels
                                                    img.filepath_raw = full_path
                                                    img.file_format = 'PNG'
                                                    img.save()
                                                    all_baked_images[resolution_key][suffix] = img
                                                    
                                                    if self.organize_folders:
                                                        relative_path = os.path.relpath(full_path, directory)
                                                        self.report({'INFO'}, f"创建纯色 {suffix} 贴图 ({width}×{height}): {relative_path} (值: {default_val})")
                                                    else:
                                                        self.report({'INFO'}, f"创建纯色 {suffix} 贴图 ({width}×{height}): {img_name}.png (值: {default_val})")
                                        except Exception as e:
                                            self.report({'ERROR'}, f"创建纯色 {suffix} 贴图失败 ({width}×{height}): {str(e)}")
                                            # 清理失败的图像
                                            if img.name in bpy.data.images:
                                                bpy.data.images.remove(img)
                                            
                            except Exception as e:
                                self.report({'ERROR'}, f"UDIM tile {udim_tile} baking failed: {str(e)}")
                            finally:
                                # Restore UDIM UV coordinates
                                if original_uvs:
                                    restore_udim_uvs(obj, original_uvs)
                
                    except Exception as e:
                        self.report({'ERROR'}, f"Error during baking at resolution {width}×{height}: {str(e)}")

                    # Rebuild material if requested
                    primary_baked_images = all_baked_images.get(primary_resolution, {})
                    total_images_all_res = sum(len(images) for images in all_baked_images.values())
                    self.report({'INFO'}, f"替换节点设置: {self.replace_nodes}, 已烘焙图像总数: {total_images_all_res}")
                    
                    if self.replace_nodes and primary_baked_images:
                        primary_width, primary_height = primary_resolution
                        self.report({'INFO'}, f"开始重建材质 '{mat.name}' 的节点，使用主分辨率 {primary_width}×{primary_height}，已烘焙 {len(primary_baked_images)} 个通道")
                        try:
                            nt.nodes.clear()
                            tex_nodes = {}
                            
                            # 定义节点排列顺序和位置
                            node_order = [
                                'BaseColor', 'Roughness', 'Metallic', 'Normal',
                                'Subsurface', 'Transmission', 'Emission', 'Alpha',
                                'Specular', 'Clearcoat', 'ClearcoatRoughness', 'Sheen',
                                'Displacement', 'AO'
                            ]
                            
                            y = 400
                            for key in node_order:
                                if key in primary_baked_images:
                                    tex = nt.nodes.new('ShaderNodeTexImage')
                                    tex.image = primary_baked_images[key]
                                    tex.location = (-800, y)
                                    tex.label = key
                                    
                                    # 设置正确的颜色空间
                                    if tex.image:
                                        self.set_image_colorspace(tex.image, key)
                                    
                                    tex_nodes[key] = tex
                                    y -= 150

                            # 创建特殊节点
                            normal_map = None
                            if 'Normal' in tex_nodes:
                                normal_map = nt.nodes.new('ShaderNodeNormalMap')
                                normal_map.location = (-500, tex_nodes['Normal'].location.y)
                                normal_map.label = "Normal Map"

                            # 创建ColorRamp节点用于AO混合
                            ao_mix = None
                            if 'AO' in tex_nodes:
                                try:
                                    # 尝试新版本的Mix节点
                                    ao_mix = nt.nodes.new('ShaderNodeMix')
                                    ao_mix.data_type = 'RGBA'
                                    ao_mix.blend_type = 'MULTIPLY'
                                    if 'Fac' in ao_mix.inputs:
                                        ao_mix.inputs['Fac'].default_value = 0.5
                                    elif 'Factor' in ao_mix.inputs:
                                        ao_mix.inputs['Factor'].default_value = 0.5
                                except:
                                    try:
                                        # 回退到旧版本的MixRGB节点
                                        ao_mix = nt.nodes.new('ShaderNodeMixRGB')
                                        ao_mix.blend_type = 'MULTIPLY'
                                        ao_mix.inputs['Fac'].default_value = 0.5
                                    except:
                                        # 如果都失败了，不使用AO混合
                                        ao_mix = None
                                        self.report({'WARNING'}, "无法创建AO混合节点，跳过AO混合")
                                
                                if ao_mix:
                                    ao_mix.location = (-300, tex_nodes['AO'].location.y)
                                    ao_mix.label = "AO Mix"

                            # 创建位移节点
                            displacement_node = None
                            if 'Displacement' in tex_nodes:
                                displacement_node = nt.nodes.new('ShaderNodeDisplacement')
                                displacement_node.location = (0, -400)
                                displacement_node.label = "Displacement"

                            # 创建Principled BSDF
                            principled = nt.nodes.new('ShaderNodeBsdfPrincipled')
                            principled.location = (-100, 0)
                            principled.label = "Principled BSDF"

                            # 创建输出节点
                            output = nt.nodes.new('ShaderNodeOutputMaterial')
                            output.location = (300, 0)
                            output.label = "Material Output"

                            # 创建适合当前Blender版本的输入名称映射
                            input_mapping = create_input_mapping()

                            # 安全连接函数
                            def safe_connect(from_node, from_output, to_node, to_input_name):
                                try:
                                    # 检查所有参数都是有效的
                                    if (from_node and to_node and 
                                        hasattr(from_node, 'outputs') and hasattr(to_node, 'inputs') and
                                        isinstance(to_input_name, str) and to_input_name and
                                        isinstance(from_output, str) and from_output):
                                        
                                        # 检查输入是否存在
                                        if (to_input_name in to_node.inputs and 
                                            from_output in from_node.outputs):
                                            nt.links.new(from_node.outputs[from_output], to_node.inputs[to_input_name])
                                            return True
                                        else:
                                            self.report({'WARNING'}, f"连接失败: {from_output} -> {to_input_name} (不存在的插槽)")
                                except (KeyError, AttributeError, TypeError, RuntimeError) as e:
                                    self.report({'WARNING'}, f"连接错误: {str(e)}")
                                return False

                            # 连接基础通道
                            if 'BaseColor' in tex_nodes:
                                if 'AO' in tex_nodes and ao_mix:
                                    # 将BaseColor和AO混合
                                    try:
                                        # 尝试不同的输入名称
                                        color1_input = 'Color1' if 'Color1' in ao_mix.inputs else 'A'
                                        color2_input = 'Color2' if 'Color2' in ao_mix.inputs else 'B'
                                        color_output = 'Color' if 'Color' in ao_mix.outputs else 'Result'
                                        
                                        nt.links.new(tex_nodes['BaseColor'].outputs['Color'], ao_mix.inputs[color1_input])
                                        nt.links.new(tex_nodes['AO'].outputs['Color'], ao_mix.inputs[color2_input])
                                        
                                        basecolor_input = input_mapping.get('BaseColor', 'Base Color')
                                        safe_connect(ao_mix, color_output, principled, basecolor_input)
                                    except Exception as e:
                                        self.report({'WARNING'}, f"AO混合失败，直接连接基础色: {str(e)}")
                                        basecolor_input = input_mapping.get('BaseColor', 'Base Color')
                                        safe_connect(tex_nodes['BaseColor'], 'Color', principled, basecolor_input)
                                else:
                                    basecolor_input = input_mapping.get('BaseColor', 'Base Color')
                                    safe_connect(tex_nodes['BaseColor'], 'Color', principled, basecolor_input)
                            
                            if 'Roughness' in tex_nodes:
                                roughness_input = input_mapping.get('Roughness', 'Roughness')
                                safe_connect(tex_nodes['Roughness'], 'Color', principled, roughness_input)
                            
                            if 'Metallic' in tex_nodes:
                                metallic_input = input_mapping.get('Metallic', 'Metallic')
                                safe_connect(tex_nodes['Metallic'], 'Color', principled, metallic_input)
                            
                            if 'Normal' in tex_nodes and normal_map:
                                try:
                                    nt.links.new(tex_nodes['Normal'].outputs['Color'], normal_map.inputs['Color'])
                                    safe_connect(normal_map, 'Normal', principled, 'Normal')
                                except Exception as e:
                                    self.report({'WARNING'}, f"法线连接失败: {str(e)}")

                            # 连接高级PBR通道
                            advanced_channels = [
                                ('Subsurface', 'Subsurface'),
                                ('Transmission', 'Transmission'),
                                ('Emission', 'Emission'),
                                ('Alpha', 'Alpha'),
                                ('Specular', 'Specular'),
                                ('Clearcoat', 'Clearcoat'),
                                ('ClearcoatRoughness', 'ClearcoatRoughness'),
                                ('Sheen', 'Sheen')
                            ]
                            
                            for tex_key, mapping_key in advanced_channels:
                                if tex_key in tex_nodes:
                                    input_name = input_mapping.get(mapping_key, mapping_key)
                                    safe_connect(tex_nodes[tex_key], 'Color', principled, input_name)
                            
                            # 连接主要输出
                            try:
                                nt.links.new(principled.outputs['BSDF'], output.inputs['Surface'])
                            except Exception as e:
                                self.report({'ERROR'}, f"主要输出连接失败: {str(e)}")
                            
                            # 连接位移
                            if 'Displacement' in tex_nodes and displacement_node:
                                try:
                                    nt.links.new(tex_nodes['Displacement'].outputs['Color'], displacement_node.inputs['Height'])
                                    nt.links.new(displacement_node.outputs['Displacement'], output.inputs['Displacement'])
                                except Exception as e:
                                    self.report({'WARNING'}, f"位移连接失败: {str(e)}")

                            connected_channels = list(primary_baked_images.keys())
                            self.report({'INFO'}, f"成功重建材质 '{mat.name}' 的节点，连接了 {len(connected_channels)} 个通道: {', '.join(connected_channels)}")
                            
                        except Exception as e:
                            self.report({'ERROR'}, f"重建材质节点失败: {str(e)}")
                            # 如果重建失败，尝试恢复基本的Principled BSDF节点
                            try:
                                nt.nodes.clear()
                                principled = nt.nodes.new('ShaderNodeBsdfPrincipled')
                                principled.location = (0, 0)
                                output = nt.nodes.new('ShaderNodeOutputMaterial')
                                output.location = (300, 0)
                                nt.links.new(principled.outputs['BSDF'], output.inputs['Surface'])
                                self.report({'INFO'}, f"已为材质 '{mat.name}' 恢复基本节点设置")
                            except Exception as restore_error:
                                self.report({'ERROR'}, f"无法恢复材质 '{mat.name}' 的基本节点: {str(restore_error)}")
                    elif not self.replace_nodes:
                        self.report({'INFO'}, f"替换节点功能已禁用，保持材质 '{mat.name}' 的原始节点")
                    elif not primary_baked_images:
                        self.report({'WARNING'}, f"没有成功烘焙的贴图，跳过材质 '{mat.name}' 的节点重建")

                    # 清理烘焙节点
                    try:
                        if bake_node and hasattr(bake_node, 'bl_idname') and bake_node.bl_idname == 'ShaderNodeTexImage':
                            nt.nodes.remove(bake_node)
                    except (TypeError, AttributeError, ReferenceError, RuntimeError):
                        # 如果节点已经被删除、无效或无法移除，忽略错误
                        pass

        self.report({'INFO'}, f"烘焙完成！处理了 {len(selected_objects)} 个物体，{processed_materials} 个材质")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# 快捷选择操作器
# -----------------------------------------------------------------------------

class MBNL_OT_select_basic(Operator):
    bl_idname = "mbnl.select_basic"
    bl_label = "Select Basic PBR Channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # Basic PBR channels
        scene.mbnl_include_basecolor = True
        scene.mbnl_include_roughness = True
        scene.mbnl_include_metallic = True
        scene.mbnl_include_normal = True
        # Advanced PBR channels
        scene.mbnl_include_subsurface = False
        scene.mbnl_include_transmission = False
        scene.mbnl_include_emission = False
        scene.mbnl_include_alpha = False
        scene.mbnl_include_specular = False
        scene.mbnl_include_clearcoat = False
        scene.mbnl_include_clearcoat_roughness = False
        scene.mbnl_include_sheen = False
        # Special channels
        scene.mbnl_include_displacement = False
        scene.mbnl_include_ambient_occlusion = False
        # Custom shaders
        scene.mbnl_include_custom_shader = False
        return {'FINISHED'}


class MBNL_OT_select_full(Operator):
    bl_idname = "mbnl.select_full"
    bl_label = "Select All PBR Channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # Basic PBR channels
        scene.mbnl_include_basecolor = True
        scene.mbnl_include_roughness = True
        scene.mbnl_include_metallic = True
        scene.mbnl_include_normal = True
        # Advanced PBR channels
        scene.mbnl_include_subsurface = True
        scene.mbnl_include_transmission = True
        scene.mbnl_include_emission = True
        scene.mbnl_include_alpha = True
        scene.mbnl_include_specular = True
        scene.mbnl_include_clearcoat = True
        scene.mbnl_include_clearcoat_roughness = True
        scene.mbnl_include_sheen = True
        # Special channels
        scene.mbnl_include_displacement = True
        scene.mbnl_include_ambient_occlusion = True
        # Custom shaders
        scene.mbnl_include_custom_shader = True
        return {'FINISHED'}


class MBNL_OT_select_none(Operator):
    bl_idname = "mbnl.select_none"
    bl_label = "Deselect All Channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # Basic PBR channels
        scene.mbnl_include_basecolor = False
        scene.mbnl_include_roughness = False
        scene.mbnl_include_metallic = False
        scene.mbnl_include_normal = False
        # Advanced PBR channels
        scene.mbnl_include_subsurface = False
        scene.mbnl_include_transmission = False
        scene.mbnl_include_emission = False
        scene.mbnl_include_alpha = False
        scene.mbnl_include_specular = False
        scene.mbnl_include_clearcoat = False
        scene.mbnl_include_clearcoat_roughness = False
        scene.mbnl_include_sheen = False
        # Special channels
        scene.mbnl_include_displacement = False
        scene.mbnl_include_ambient_occlusion = False
        # Custom shaders
        scene.mbnl_include_custom_shader = False
        return {'FINISHED'}


class MBNL_OT_select_custom_shader(Operator):
    bl_idname = "mbnl.select_custom_shader"
    bl_label = "Select Custom Shader Only"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # Disable all other channels
        scene.mbnl_include_basecolor = False
        scene.mbnl_include_roughness = False
        scene.mbnl_include_metallic = False
        scene.mbnl_include_normal = False
        scene.mbnl_include_subsurface = False
        scene.mbnl_include_transmission = False
        scene.mbnl_include_emission = False
        scene.mbnl_include_alpha = False
        scene.mbnl_include_specular = False
        scene.mbnl_include_clearcoat = False
        scene.mbnl_include_clearcoat_roughness = False
        scene.mbnl_include_sheen = False
        scene.mbnl_include_displacement = False
        scene.mbnl_include_ambient_occlusion = False
        # Only enable custom shader
        scene.mbnl_include_custom_shader = True
        return {'FINISHED'}


class MBNL_OT_diagnose_custom_shader(Operator):
    bl_idname = "mbnl.diagnose_custom_shader"
    bl_label = "Diagnose Custom Shader"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        self.report({'INFO'}, "=== 自定义着色器诊断报告 ===")
        
        for obj in selected_objects:
            obj_name = safe_encode_text(obj.name, "未命名物体")
            self.report({'INFO'}, f"分析物体: {obj_name}")
            
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    mat = slot.material
                    mat_name = safe_encode_text(mat.name, "未命名材质")
                    self.report({'INFO'}, f"  材质: {mat_name}")
                    
                    # 分析材质
                    analysis = analyze_material(mat)
                    material_type = analysis.get('material_type', 'unknown')
                    self.report({'INFO'}, f"    类型: {material_type}")
                    
                    # 检查Material Output连接
                    output_node = analysis.get('output_node')
                    if output_node:
                        if output_node.inputs['Surface'].is_linked:
                            shader_link = output_node.inputs['Surface'].links[0]
                            shader_node = shader_link.from_node
                            shader_socket = shader_link.from_socket
                            
                            self.report({'INFO'}, f"    着色器节点: {shader_node.bl_idname} ('{shader_node.name}')")
                            self.report({'INFO'}, f"    输出插槽: '{shader_socket.name}'")
                            
                            # 如果是节点组，显示更多信息
                            if shader_node.bl_idname == 'ShaderNodeNodeGroup':
                                if hasattr(shader_node, 'node_tree') and shader_node.node_tree:
                                    group_name = shader_node.node_tree.name
                                    self.report({'INFO'}, f"    节点组: {group_name}")
                                    
                                    # 显示所有输出
                                    outputs = list(shader_node.outputs.keys())
                                    self.report({'INFO'}, f"    可用输出: {outputs}")
                                    
                                    # 检查哪个输出正在被使用
                                    used_output = shader_socket.name
                                    self.report({'INFO'}, f"    当前使用的输出: '{used_output}'")
                                else:
                                    self.report({'WARNING'}, f"    节点组缺少node_tree")
                        else:
                            self.report({'WARNING'}, f"    Material Output的Surface输入未连接")
                    else:
                        self.report({'ERROR'}, f"    未找到Material Output节点")
                    
                    # 检查自定义着色器
                    custom_shaders = analysis.get('custom_shaders', [])
                    if custom_shaders:
                        self.report({'INFO'}, f"    检测到{len(custom_shaders)}个自定义着色器:")
                        for shader in custom_shaders[:3]:  # 只显示前3个
                            self.report({'INFO'}, f"      - {shader['type']} ('{shader['name']}')")
                    else:
                        self.report({'INFO'}, f"    未检测到自定义着色器")
                    
                    # 检查混合着色器网络
                    if material_type in ['mixed_shader_network', 'principled_with_custom', 'custom_with_principled']:
                        self.report({'INFO'}, f"    混合着色器分析:")
                        self.report({'INFO'}, f"      Principled连接到输出: {analysis.get('principled_connected_to_output')}")
                        self.report({'INFO'}, f"      自定义着色器连接到输出: {analysis.get('custom_connected_to_output')}")
                        
                        shader_network = analysis.get('shader_network', {})
                        if shader_network:
                            mix_node = shader_network.get('mix_node')
                            if mix_node:
                                self.report({'INFO'}, f"      混合节点: {mix_node.bl_idname} ('{mix_node.name}')")
                                self.report({'INFO'}, f"      包含Principled: {shader_network.get('has_principled')}")
                                self.report({'INFO'}, f"      包含自定义: {shader_network.get('has_custom')}")
                        
                        mix_shaders = analysis.get('mix_shaders', [])
                        if mix_shaders:
                            self.report({'INFO'}, f"    检测到{len(mix_shaders)}个Mix/Add Shader节点:")
                            for mix_shader in mix_shaders[:2]:  # 只显示前2个
                                self.report({'INFO'}, f"      - {mix_shader['type']} ('{mix_shader['name']}')")
        
        self.report({'INFO'}, "=== 诊断完成 ===")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# 多分辨率快捷选择操作器
# -----------------------------------------------------------------------------

class MBNL_OT_select_res_game(Operator):
    bl_idname = "mbnl.select_res_game"
    bl_label = "Game Common"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_res_512 = True
        scene.mbnl_res_1024 = True
        scene.mbnl_res_2048 = True
        scene.mbnl_res_4096 = False
        scene.mbnl_res_8192 = False
        return {'FINISHED'}


class MBNL_OT_select_res_film(Operator):
    bl_idname = "mbnl.select_res_film"
    bl_label = "Film High Quality"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_res_512 = False
        scene.mbnl_res_1024 = False
        scene.mbnl_res_2048 = True
        scene.mbnl_res_4096 = True
        scene.mbnl_res_8192 = True
        return {'FINISHED'}


class MBNL_OT_select_res_all(Operator):
    bl_idname = "mbnl.select_res_all"
    bl_label = "All Resolutions"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_res_512 = True
        scene.mbnl_res_1024 = True
        scene.mbnl_res_2048 = True
        scene.mbnl_res_4096 = True
        scene.mbnl_res_8192 = True
        return {'FINISHED'}


class MBNL_OT_select_res_none(Operator):
    bl_idname = "mbnl.select_res_none"
    bl_label = "Clear All"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_res_512 = False
        scene.mbnl_res_1024 = False
        scene.mbnl_res_2048 = False
        scene.mbnl_res_4096 = False
        scene.mbnl_res_8192 = False
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# 预设管理操作器
# -----------------------------------------------------------------------------

class MBNL_OT_save_preset(Operator):
    bl_idname = "mbnl.save_preset"
    bl_label = "Save Preset"
    bl_options = {"REGISTER", "UNDO"}
    
    preset_name: StringProperty(
        name="Preset Name",
        description="Enter preset name",
        default="New Preset"
    )

    def execute(self, context):
        scene = context.scene
        
        if not self.preset_name.strip():
            self.report({'ERROR'}, "预设名称不能为空")
            return {'CANCELLED'}
        
        # 清理预设名称（移除非法字符）
        safe_name = "".join(c for c in self.preset_name if c.isalnum() or c in (' ', '-', '_')).strip()
        if not safe_name:
            self.report({'ERROR'}, "预设名称包含非法字符")
            return {'CANCELLED'}
        
        # 收集所有设置
        settings = {
            # 基本设置
            'resolution': scene.mbnl_resolution,
            'replace_nodes': scene.mbnl_replace_nodes,
            'include_lighting': scene.mbnl_include_lighting,
            'organize_folders': scene.mbnl_organize_folders,
            'use_custom_directory': scene.mbnl_use_custom_directory,
            'custom_directory': scene.mbnl_custom_directory,
            
            # 多分辨率设置
            'enable_multi_resolution': scene.mbnl_enable_multi_resolution,
            'res_512': scene.mbnl_res_512,
            'res_1024': scene.mbnl_res_1024,
            'res_2048': scene.mbnl_res_2048,
            'res_4096': scene.mbnl_res_4096,
            'res_8192': scene.mbnl_res_8192,
            
            # 自定义分辨率设置
            'enable_custom_resolution': scene.mbnl_enable_custom_resolution,
            'custom_width_1': scene.mbnl_custom_width_1,
            'custom_height_1': scene.mbnl_custom_height_1,
            'custom_width_2': scene.mbnl_custom_width_2,
            'custom_height_2': scene.mbnl_custom_height_2,
            'custom_width_3': scene.mbnl_custom_width_3,
            'custom_height_3': scene.mbnl_custom_height_3,
            'use_custom_1': scene.mbnl_use_custom_1,
            'use_custom_2': scene.mbnl_use_custom_2,
            'use_custom_3': scene.mbnl_use_custom_3,
            
            # 基础PBR通道
            'include_basecolor': scene.mbnl_include_basecolor,
            'include_roughness': scene.mbnl_include_roughness,
            'include_metallic': scene.mbnl_include_metallic,
            'include_normal': scene.mbnl_include_normal,
            
            # 高级PBR通道
            'include_subsurface': scene.mbnl_include_subsurface,
            'include_transmission': scene.mbnl_include_transmission,
            'include_emission': scene.mbnl_include_emission,
            'include_alpha': scene.mbnl_include_alpha,
            'include_specular': scene.mbnl_include_specular,
            'include_clearcoat': scene.mbnl_include_clearcoat,
            'include_clearcoat_roughness': scene.mbnl_include_clearcoat_roughness,
            'include_sheen': scene.mbnl_include_sheen,
            
            # 特殊通道
            'include_displacement': scene.mbnl_include_displacement,
            'include_ambient_occlusion': scene.mbnl_include_ambient_occlusion,
            
            # 自定义着色器
            'include_custom_shader': scene.mbnl_include_custom_shader,
            'mixed_shader_strategy': scene.mbnl_mixed_shader_strategy,
            
            # 色彩空间管理
            'colorspace_mode': scene.mbnl_colorspace_mode,
            'colorspace_basecolor': scene.mbnl_colorspace_basecolor,
            'colorspace_normal': scene.mbnl_colorspace_normal,
            'colorspace_roughness': scene.mbnl_colorspace_roughness,
            'colorspace_emission': scene.mbnl_colorspace_emission,
            'colorspace_manual_override': scene.mbnl_colorspace_manual_override,
        }
        
        # 保存预设
        if save_preset_to_file(safe_name, settings):
            self.report({'INFO'}, f"预设 '{safe_name}' 保存成功")
            # 更新预设列表
            scene.mbnl_preset_list = safe_name
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"预设 '{safe_name}' 保存失败")
            return {'CANCELLED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class MBNL_OT_load_preset(Operator):
    bl_idname = "mbnl.load_preset"
    bl_label = "Load Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        
        if scene.mbnl_preset_list == 'NONE':
            self.report({'WARNING'}, "没有选择预设")
            return {'CANCELLED'}
        
        # 加载预设
        settings = load_preset_from_file(scene.mbnl_preset_list)
        if not settings:
            self.report({'ERROR'}, f"预设 '{scene.mbnl_preset_list}' 加载失败")
            return {'CANCELLED'}
        
        # 应用设置
        try:
            # 基本设置
            if 'resolution' in settings:
                scene.mbnl_resolution = settings['resolution']
            if 'replace_nodes' in settings:
                scene.mbnl_replace_nodes = settings['replace_nodes']
            if 'include_lighting' in settings:
                scene.mbnl_include_lighting = settings['include_lighting']
            if 'lighting_shadow_mode' in settings:
                scene.mbnl_lighting_shadow_mode = settings['lighting_shadow_mode']
            if 'organize_folders' in settings:
                scene.mbnl_organize_folders = settings['organize_folders']
            if 'use_custom_directory' in settings:
                scene.mbnl_use_custom_directory = settings['use_custom_directory']
            if 'custom_directory' in settings:
                scene.mbnl_custom_directory = settings['custom_directory']
            
            # 多分辨率设置
            if 'enable_multi_resolution' in settings:
                scene.mbnl_enable_multi_resolution = settings['enable_multi_resolution']
            if 'res_512' in settings:
                scene.mbnl_res_512 = settings['res_512']
            if 'res_1024' in settings:
                scene.mbnl_res_1024 = settings['res_1024']
            if 'res_2048' in settings:
                scene.mbnl_res_2048 = settings['res_2048']
            if 'res_4096' in settings:
                scene.mbnl_res_4096 = settings['res_4096']
            if 'res_8192' in settings:
                scene.mbnl_res_8192 = settings['res_8192']
            
            # 自定义分辨率设置
            if 'enable_custom_resolution' in settings:
                scene.mbnl_enable_custom_resolution = settings['enable_custom_resolution']
            if 'custom_width_1' in settings:
                scene.mbnl_custom_width_1 = settings['custom_width_1']
            if 'custom_height_1' in settings:
                scene.mbnl_custom_height_1 = settings['custom_height_1']
            if 'custom_width_2' in settings:
                scene.mbnl_custom_width_2 = settings['custom_width_2']
            if 'custom_height_2' in settings:
                scene.mbnl_custom_height_2 = settings['custom_height_2']
            if 'custom_width_3' in settings:
                scene.mbnl_custom_width_3 = settings['custom_width_3']
            if 'custom_height_3' in settings:
                scene.mbnl_custom_height_3 = settings['custom_height_3']
            if 'use_custom_1' in settings:
                scene.mbnl_use_custom_1 = settings['use_custom_1']
            if 'use_custom_2' in settings:
                scene.mbnl_use_custom_2 = settings['use_custom_2']
            if 'use_custom_3' in settings:
                scene.mbnl_use_custom_3 = settings['use_custom_3']
            
            # 基础PBR通道
            if 'include_basecolor' in settings:
                scene.mbnl_include_basecolor = settings['include_basecolor']
            if 'include_roughness' in settings:
                scene.mbnl_include_roughness = settings['include_roughness']
            if 'include_metallic' in settings:
                scene.mbnl_include_metallic = settings['include_metallic']
            if 'include_normal' in settings:
                scene.mbnl_include_normal = settings['include_normal']
            
            # 高级PBR通道
            if 'include_subsurface' in settings:
                scene.mbnl_include_subsurface = settings['include_subsurface']
            if 'include_transmission' in settings:
                scene.mbnl_include_transmission = settings['include_transmission']
            if 'include_emission' in settings:
                scene.mbnl_include_emission = settings['include_emission']
            if 'include_alpha' in settings:
                scene.mbnl_include_alpha = settings['include_alpha']
            if 'include_specular' in settings:
                scene.mbnl_include_specular = settings['include_specular']
            if 'include_clearcoat' in settings:
                scene.mbnl_include_clearcoat = settings['include_clearcoat']
            if 'include_clearcoat_roughness' in settings:
                scene.mbnl_include_clearcoat_roughness = settings['include_clearcoat_roughness']
            if 'include_sheen' in settings:
                scene.mbnl_include_sheen = settings['include_sheen']
            
            # 特殊通道
            if 'include_displacement' in settings:
                scene.mbnl_include_displacement = settings['include_displacement']
            if 'include_ambient_occlusion' in settings:
                scene.mbnl_include_ambient_occlusion = settings['include_ambient_occlusion']
            
            # 自定义着色器
            if 'include_custom_shader' in settings:
                scene.mbnl_include_custom_shader = settings['include_custom_shader']
            if 'mixed_shader_strategy' in settings:
                scene.mbnl_mixed_shader_strategy = settings['mixed_shader_strategy']
            
            self.report({'INFO'}, f"预设 '{scene.mbnl_preset_list}' 加载成功")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"应用预设设置时出错: {str(e)}")
            return {'CANCELLED'}


class MBNL_OT_delete_preset(Operator):
    bl_idname = "mbnl.delete_preset"
    bl_label = "Delete Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        
        if scene.mbnl_preset_list == 'NONE':
            self.report({'WARNING'}, "没有选择预设")
            return {'CANCELLED'}
        
        preset_name = scene.mbnl_preset_list
        
        # 删除预设文件
        if delete_preset_file(preset_name):
            self.report({'INFO'}, f"预设 '{preset_name}' 删除成功")
            # 重置预设选择
            scene.mbnl_preset_list = 'NONE'
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"预设 '{preset_name}' 删除失败")
            return {'CANCELLED'}

    def invoke(self, context, event):
        scene = context.scene
        if scene.mbnl_preset_list == 'NONE':
            self.report({'WARNING'}, "没有选择预设")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)


class MBNL_OT_refresh_presets(Operator):
    bl_idname = "mbnl.refresh_presets"
    bl_label = "Refresh Preset List"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        # 强制刷新预设列表
        try:
            # 触发预设枚举的更新
            presets = get_available_presets()
            self.report({'INFO'}, f"预设列表已刷新，找到 {len(presets)} 个预设")
        except Exception as e:
            self.report({'ERROR'}, f"刷新预设列表失败: {str(e)}")
            return {'CANCELLED'}
        return {'FINISHED'}





# -----------------------------------------------------------------------------
# 自定义分辨率快捷设置操作器
# -----------------------------------------------------------------------------

class MBNL_OT_set_custom_1536(Operator):
    bl_idname = "mbnl.set_custom_1536"
    bl_label = "Set 1536"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_custom_width_1 = 1536
        scene.mbnl_custom_height_1 = 1536
        scene.mbnl_use_custom_1 = True
        return {'FINISHED'}


class MBNL_OT_set_custom_3072(Operator):
    bl_idname = "mbnl.set_custom_3072"
    bl_label = "设置3072"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_custom_width_2 = 3072
        scene.mbnl_custom_height_2 = 3072
        scene.mbnl_use_custom_2 = True
        return {'FINISHED'}


class MBNL_OT_set_custom_6144(Operator):
    bl_idname = "mbnl.set_custom_6144"
    bl_label = "设置6144"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_custom_width_3 = 6144
        scene.mbnl_custom_height_3 = 6144
        scene.mbnl_use_custom_3 = True
        return {'FINISHED'}


# 矩形分辨率快捷按钮
class MBNL_OT_set_custom_1920x1080(Operator):
    bl_idname = "mbnl.set_custom_1920x1080"
    bl_label = "设置1920×1080"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # 找到第一个未使用的自定义分辨率槽
        if not scene.mbnl_use_custom_1:
            scene.mbnl_custom_width_1 = 1920
            scene.mbnl_custom_height_1 = 1080
            scene.mbnl_use_custom_1 = True
        elif not scene.mbnl_use_custom_2:
            scene.mbnl_custom_width_2 = 1920
            scene.mbnl_custom_height_2 = 1080
            scene.mbnl_use_custom_2 = True
        elif not scene.mbnl_use_custom_3:
            scene.mbnl_custom_width_3 = 1920
            scene.mbnl_custom_height_3 = 1080
            scene.mbnl_use_custom_3 = True
        else:
            # 如果都被使用了，替换第一个
            scene.mbnl_custom_width_1 = 1920
            scene.mbnl_custom_height_1 = 1080
        return {'FINISHED'}


class MBNL_OT_set_custom_1280x720(Operator):
    bl_idname = "mbnl.set_custom_1280x720"
    bl_label = "设置1280×720"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # 找到第一个未使用的自定义分辨率槽
        if not scene.mbnl_use_custom_1:
            scene.mbnl_custom_width_1 = 1280
            scene.mbnl_custom_height_1 = 720
            scene.mbnl_use_custom_1 = True
        elif not scene.mbnl_use_custom_2:
            scene.mbnl_custom_width_2 = 1280
            scene.mbnl_custom_height_2 = 720
            scene.mbnl_use_custom_2 = True
        elif not scene.mbnl_use_custom_3:
            scene.mbnl_custom_width_3 = 1280
            scene.mbnl_custom_height_3 = 720
            scene.mbnl_use_custom_3 = True
        else:
            # 如果都被使用了，替换第二个
            scene.mbnl_custom_width_2 = 1280
            scene.mbnl_custom_height_2 = 720
        return {'FINISHED'}


class MBNL_OT_set_custom_2560x1440(Operator):
    bl_idname = "mbnl.set_custom_2560x1440"
    bl_label = "设置2560×1440"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # 找到第一个未使用的自定义分辨率槽
        if not scene.mbnl_use_custom_1:
            scene.mbnl_custom_width_1 = 2560
            scene.mbnl_custom_height_1 = 1440
            scene.mbnl_use_custom_1 = True
        elif not scene.mbnl_use_custom_2:
            scene.mbnl_custom_width_2 = 2560
            scene.mbnl_custom_height_2 = 1440
            scene.mbnl_use_custom_2 = True
        elif not scene.mbnl_use_custom_3:
            scene.mbnl_custom_width_3 = 2560
            scene.mbnl_custom_height_3 = 1440
            scene.mbnl_use_custom_3 = True
        else:
            # 如果都被使用了，替换第三个
            scene.mbnl_custom_width_3 = 2560
            scene.mbnl_custom_height_3 = 1440
        return {'FINISHED'}


class MBNL_OT_set_custom_3840x2160(Operator):
    bl_idname = "mbnl.set_custom_3840x2160"
    bl_label = "设置3840×2160"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        # 找到第一个未使用的自定义分辨率槽
        if not scene.mbnl_use_custom_1:
            scene.mbnl_custom_width_1 = 3840
            scene.mbnl_custom_height_1 = 2160
            scene.mbnl_use_custom_1 = True
        elif not scene.mbnl_use_custom_2:
            scene.mbnl_custom_width_2 = 3840
            scene.mbnl_custom_height_2 = 2160
            scene.mbnl_use_custom_2 = True
        elif not scene.mbnl_use_custom_3:
            scene.mbnl_custom_width_3 = 3840
            scene.mbnl_custom_height_3 = 2160
            scene.mbnl_use_custom_3 = True
        else:
            # 如果都被使用了，替换第一个
            scene.mbnl_custom_width_1 = 3840
            scene.mbnl_custom_height_1 = 2160
        return {'FINISHED'}


class MBNL_OT_clear_custom_res(Operator):
    bl_idname = "mbnl.clear_custom_res"
    bl_label = "清空自定义分辨率"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_use_custom_1 = False
        scene.mbnl_use_custom_2 = False
        scene.mbnl_use_custom_3 = False
        # 重置为默认值
        scene.mbnl_custom_width_1 = 1536
        scene.mbnl_custom_height_1 = 1536
        scene.mbnl_custom_width_2 = 1920
        scene.mbnl_custom_height_2 = 1080
        scene.mbnl_custom_width_3 = 1280
        scene.mbnl_custom_height_3 = 720
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# UI Panel
# -----------------------------------------------------------------------------

class MBNL_PT_panel(Panel):
    bl_label = "EasyBake"
    bl_idname = "MBNL_PT_panel"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Pre-calculate object and material information for entire UI
        selected_objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        total_materials = 0
        material_stats = {
            'textured': 0,
            'procedural': 0,
            'mixed': 0,
            'default': 0,
            'custom_shader': 0,
            'mixed_shader': 0,
            'mixed_shader_network': 0,
            'principled_with_custom': 0,
            'custom_with_principled': 0
        }
        
        if selected_objects:
            for obj in selected_objects:
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes:
                        total_materials += 1
                        try:
                            analysis = analyze_material(slot.material)
                            mat_type = analysis.get('material_type', 'unknown')
                            if mat_type in material_stats:
                                material_stats[mat_type] += 1
                            else:
                                material_stats['default'] += 1
                        except Exception as e:
                            # 如果材质分析失败，计为默认材质
                            material_stats['default'] += 1
                            print(f"UI材质分析错误: {e}")

        # =============================================================================
        # 1. 顶部：物体状态信息
        # =============================================================================
        status_box = layout.box()
        status_box.label(text="📊 当前状态", icon='INFO')
        
        if selected_objects:
            
            # 主要状态信息
            status_row = status_box.row()
            status_col1 = status_row.column()
            status_col2 = status_row.column()
            
            status_col1.label(text=f"✓ 物体: {len(selected_objects)} 个")
            status_col2.label(text=f"✓ 材质: {total_materials} 个")
            
            # 材质类型统计（仅在有多种类型时显示）
            if total_materials > 0:
                type_count = sum(1 for count in material_stats.values() if count > 0)
                if type_count > 1:
                    stats_box = status_box.box()
                    stats_box.label(text="材质类型分布:", icon='MATERIAL')
                    stats_row = stats_box.row()
                    stats_col1 = stats_row.column()
                    stats_col2 = stats_row.column()
                    
                    if material_stats['textured'] > 0:
                        stats_col1.label(text=f"  纹理材质: {material_stats['textured']}")
                    if material_stats['procedural'] > 0:
                        stats_col1.label(text=f"  纯色材质: {material_stats['procedural']}")
                    if material_stats['mixed'] > 0:
                        stats_col2.label(text=f"  混合材质: {material_stats['mixed']}")
                    if material_stats['default'] > 0:
                        stats_col2.label(text=f"  默认材质: {material_stats['default']}")
                    if material_stats['custom_shader'] > 0:
                        stats_col1.label(text=f"  自定义着色器: {material_stats['custom_shader']}")
                    if material_stats['mixed_shader'] > 0:
                        stats_col2.label(text=f"  混合着色器: {material_stats['mixed_shader']}")
                    if material_stats['mixed_shader_network'] > 0:
                        stats_box.label(text=f"🔗 混合网络: {material_stats['mixed_shader_network']} (需选择策略)", icon='NODE_MATERIAL')
                    if material_stats['principled_with_custom'] > 0:
                        stats_col1.label(text=f"  PBR+自定义: {material_stats['principled_with_custom']}")
                    if material_stats['custom_with_principled'] > 0:
                        stats_col2.label(text=f"  自定义+PBR: {material_stats['custom_with_principled']}")
            
            # 物体详情（简化显示）
            if len(selected_objects) <= 2:
                for obj in selected_objects:
                    mat_count = len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
                    safe_obj_name = safe_encode_text(obj.name, "未命名物体")
                    status_box.label(text=f"  • {safe_obj_name} ({mat_count} 材质)")
            elif len(selected_objects) <= 5:
                detail_row = status_box.row()
                obj_names = [safe_encode_text(obj.name, "未命名") for obj in selected_objects[:3]]
                detail_row.label(text=f"  • {', '.join(obj_names)} + {len(selected_objects) - 3} 个物体")
            else:
                detail_row = status_box.row()
                detail_row.label(text=f"  • 大批量处理: {len(selected_objects)} 个物体")
        else:
            status_box.alert = True
            status_box.label(text="⚠ 请选择至少一个网格物体", icon='ERROR')

        layout.separator()

        # =============================================================================
        # 2. 预设管理
        # =============================================================================
        preset_box = layout.box()
        preset_box.label(text="🎛️ 预设管理", icon='PRESET')
        
        # 预设选择和操作
        preset_row = preset_box.row(align=True)
        preset_row.prop(scene, "mbnl_preset_list", text="")
        preset_row.operator("mbnl.refresh_presets", text="", icon='FILE_REFRESH')
        
        # 预设操作按钮
        preset_ops_row = preset_box.row(align=True)
        preset_ops_row.operator("mbnl.save_preset", text="保存", icon='FILE_NEW')
        
        load_button = preset_ops_row.row()
        load_button.enabled = scene.mbnl_preset_list != 'NONE'
        load_button.operator("mbnl.load_preset", text="加载", icon='IMPORT')
        
        delete_button = preset_ops_row.row()
        delete_button.enabled = scene.mbnl_preset_list != 'NONE'
        delete_button.operator("mbnl.delete_preset", text="删除", icon='TRASH')
        
        # 预设状态
        if scene.mbnl_preset_list != 'NONE':
            preset_box.label(text=f"✓ 当前: {scene.mbnl_preset_list}", icon='CHECKMARK')
        else:
            # 检查是否有预设，如果没有显示创建默认预设按钮
            presets = get_available_presets()
            if len(presets) == 1 and presets[0][0] == 'NONE':
                default_row = preset_box.row()
                default_row.operator("mbnl.create_default_presets", text="创建默认预设", icon='ADD')

        layout.separator()

        # =============================================================================
        # 3. 基本设置
        # =============================================================================
        basic_box = layout.box()
        basic_box.label(text="⚙️ 基本设置", icon='SETTINGS')
        
        # 分辨率设置
        if not scene.mbnl_enable_multi_resolution:
            basic_box.prop(scene, "mbnl_resolution")
        
        basic_box.prop(scene, "mbnl_replace_nodes")
        basic_box.prop(scene, "mbnl_include_lighting")
        
        # 光影烘焙说明
        if scene.mbnl_include_lighting:
            light_info_box = basic_box.box()
            light_info_box.label(text="💡 增强光影烘焙:", icon='LIGHT_SUN')
            
            # 阴影模式选择
            shadow_row = light_info_box.row()
            shadow_row.prop(scene, "mbnl_lighting_shadow_mode", text="阴影模式")
            
            # 阴影模式说明
            shadow_info_box = light_info_box.box()
            if scene.mbnl_lighting_shadow_mode == 'WITH_SHADOWS':
                shadow_info_box.label(text="✓ 包含阴影: 完整光影和阴影", icon='LIGHT_SUN')
                shadow_info_box.label(text="• 包含所有光源投射的阴影")
                shadow_info_box.label(text="• 最真实的光影重现")
            else:  # NO_SHADOWS
                shadow_info_box.label(text="⚡ 无阴影: 直接光照无阴影", icon='LIGHT_SUN')
                shadow_info_box.label(text="• 临时禁用所有光源的阴影")
                shadow_info_box.label(text="• 适用于需要均匀光照无暗部的场景")
            
            light_info_box.label(text="• 使用组合方法捕获完整光照")
            light_info_box.label(text="• 自动优化采样数量和GPU加速")
            light_info_box.label(text="• 智能调整材质设置以提高质量")
            light_info_box.label(text="• 包含直接光照、间接光照和反射")
            
            # 添加性能提示
            perf_row = light_info_box.row()
            perf_row.label(text="⚡ 提示：光影烘焙耗时较长，建议使用GPU", icon='INFO')

        layout.separator()

        # =============================================================================
        # 4. 色彩空间管理
        # =============================================================================
        colorspace_box = layout.box()
        colorspace_box.label(text="🎨 色彩空间管理", icon='COLOR')
        
        # 色彩空间模式选择
        colorspace_box.prop(scene, "mbnl_colorspace_mode", text="模式")
        
        # 根据模式显示不同选项
        if scene.mbnl_colorspace_mode == 'AUTO':
            cs_info_box = colorspace_box.box()
            cs_info_box.label(text="🤖 自动检测:", icon='AUTO')
            cs_info_box.label(text="• 颜色贴图(基础色、自发光): sRGB")
            cs_info_box.label(text="• 数据贴图(法线、粗糙度等): Non-Color")
            cs_info_box.label(text="• 适用于大多数工作流程")
            
        elif scene.mbnl_colorspace_mode == 'CUSTOM':
            cs_custom_box = colorspace_box.box()
            cs_custom_box.label(text="⚙️ 自定义设置:", icon='PREFERENCES')
            
            # 自定义色彩空间设置列
            cs_row1 = cs_custom_box.row()
            cs_col1 = cs_row1.column()
            cs_col2 = cs_row1.column()
            
            cs_col1.prop(scene, "mbnl_colorspace_basecolor", text="基础色")
            cs_col1.prop(scene, "mbnl_colorspace_emission", text="自发光")
            
            cs_col2.prop(scene, "mbnl_colorspace_normal", text="法线贴图")  
            cs_col2.prop(scene, "mbnl_colorspace_roughness", text="数据贴图")
            
        elif scene.mbnl_colorspace_mode == 'MANUAL':
            cs_manual_box = colorspace_box.box()
            cs_manual_box.label(text="🎛️ 手动覆盖:", icon='PREFERENCES')
            cs_manual_box.prop(scene, "mbnl_colorspace_manual_override", text="所有贴图")
            cs_manual_box.label(text="⚠️ 覆盖将应用于所有烘焙贴图", icon='ERROR')

        layout.separator()

        # =============================================================================
        # 5. Output Settings
        # =============================================================================
        output_box = layout.box()
        output_box.label(text="📁 输出设置", icon='FILE_FOLDER')
        
        # 文件夹组织
        output_box.prop(scene, "mbnl_organize_folders")
        
        # 文件夹组织说明
        if scene.mbnl_organize_folders:
            org_info_box = output_box.box()
            org_info_box.label(text="📁 文件夹结构:", icon='FILE_FOLDER')
            org_info_box.label(text="物体名/材质名/分辨率/贴图.png")
            org_info_box.label(text="例: Cube/Material/2048x2048/basecolor.png")
        else:
            org_info_box = output_box.box()
            org_info_box.label(text="📄 传统命名:", icon='FILE_BLANK')
            org_info_box.label(text="物体_材质_贴图_分辨率.png")
            org_info_box.label(text="例: Cube_Material_basecolor_2048.png")
        
        # 自定义输出目录
        output_box.prop(scene, "mbnl_use_custom_directory")
        
        # 自定义目录设置
        if scene.mbnl_use_custom_directory:
            custom_dir_box = output_box.box()
            custom_dir_box.label(text="自定义输出目录:", icon='FILE_FOLDER')
            custom_dir_box.prop(scene, "mbnl_custom_directory", text="路径")
            
            # 显示当前设置的路径信息
            if scene.mbnl_custom_directory:
                try:
                    abs_path = bpy.path.abspath(scene.mbnl_custom_directory)
                    safe_path = safe_path_display(abs_path)
                    
                    if os.path.exists(abs_path):
                        info_row = custom_dir_box.row()
                        info_row.label(text=f"✓ 目录存在: {safe_path}", icon='CHECKMARK')
                    else:
                        warning_row = custom_dir_box.row()
                        warning_row.alert = True
                        warning_row.label(text=f"⚠ 目录不存在，将自动创建: {safe_path}", icon='ERROR')
                except Exception as e:
                    error_row = custom_dir_box.row()
                    error_row.alert = True
                    error_row.label(text="⚠ 路径显示错误，请检查路径格式", icon='ERROR')
            else:
                placeholder_row = custom_dir_box.row()
                placeholder_row.label(text="💡 请选择输出目录", icon='INFO')
        
        # 多分辨率设置
        output_box.prop(scene, "mbnl_enable_multi_resolution")
        
        # 多分辨率选择
        if scene.mbnl_enable_multi_resolution:
            multi_res_box = output_box.box()
            multi_res_box.label(text="多分辨率配置:", icon='TEXTURE')
            
            # 预设分辨率
            preset_row = multi_res_box.row()
            preset_row.label(text="预设分辨率:")
            res_row = multi_res_box.row(align=True)
            col1 = res_row.column()
            col1.prop(scene, "mbnl_res_512")
            col1.prop(scene, "mbnl_res_2048")
            col2 = res_row.column()
            col2.prop(scene, "mbnl_res_1024")
            col2.prop(scene, "mbnl_res_4096")
            
            # 8K分辨率单独一行
            multi_res_box.prop(scene, "mbnl_res_8192")
            
            # 快捷选择按钮
            quick_row = multi_res_box.row(align=True)
            quick_row.operator("mbnl.select_res_game", text="游戏")
            quick_row.operator("mbnl.select_res_film", text="影视")
            quick_row.operator("mbnl.select_res_all", text="全部")
            quick_row.operator("mbnl.select_res_none", text="清空")
            
            # 自定义分辨率区域
            custom_box = multi_res_box.box()
            custom_box.prop(scene, "mbnl_enable_custom_resolution")
            
            if scene.mbnl_enable_custom_resolution:
                custom_box.label(text="自定义分辨率:", icon='SETTINGS')
                
                # 自定义分辨率设置
                for i in range(1, 4):
                    custom_row = custom_box.row(align=True)
                    custom_row.prop(scene, f"mbnl_use_custom_{i}", text="")
                    sub_row = custom_row.row(align=True)
                    sub_row.enabled = getattr(scene, f"mbnl_use_custom_{i}")
                    sub_row.prop(scene, f"mbnl_custom_width_{i}", text="宽")
                    sub_row.prop(scene, f"mbnl_custom_height_{i}", text="高")
                
                # 常用分辨率快捷按钮
                preset_box = custom_box.box()
                preset_box.label(text="常用分辨率:")
                
                square_row = preset_box.row(align=True)
                square_row.operator("mbnl.set_custom_1536", text="1536²")
                square_row.operator("mbnl.set_custom_3072", text="3072²")
                square_row.operator("mbnl.set_custom_6144", text="6144²")
                
                rect_row1 = preset_box.row(align=True)
                rect_row1.operator("mbnl.set_custom_1920x1080", text="1920×1080")
                rect_row1.operator("mbnl.set_custom_1280x720", text="1280×720")
                
                rect_row2 = preset_box.row(align=True)
                rect_row2.operator("mbnl.set_custom_2560x1440", text="2560×1440")
                rect_row2.operator("mbnl.set_custom_3840x2160", text="3840×2160")
                
                clear_row = preset_box.row()
                clear_row.operator("mbnl.clear_custom_res", text="清空自定义", icon='X')
            
            # 显示已选择的分辨率
            selected_preset = []
            selected_custom = []
            
            # 预设分辨率
            if scene.mbnl_res_512: selected_preset.append("512")
            if scene.mbnl_res_1024: selected_preset.append("1024")
            if scene.mbnl_res_2048: selected_preset.append("2048")
            if scene.mbnl_res_4096: selected_preset.append("4096")
            if scene.mbnl_res_8192: selected_preset.append("8192")
            
            # 自定义分辨率
            if scene.mbnl_enable_custom_resolution:
                for i in range(1, 4):
                    if getattr(scene, f"mbnl_use_custom_{i}"):
                        w = getattr(scene, f"mbnl_custom_width_{i}")
                        h = getattr(scene, f"mbnl_custom_height_{i}")
                        if w == h:
                            selected_custom.append(f"{w}")
                        else:
                            selected_custom.append(f"{w}x{h}")
            
            all_selected = selected_preset + selected_custom
            
            if all_selected:
                summary_box = multi_res_box.box()
                preset_info = [f'{r}×{r}' for r in selected_preset]
                custom_info = [f'{r}×{r}(自定义)' if r.isdigit() else f'{r}(自定义)' for r in selected_custom]
                display_info = preset_info + custom_info
                
                summary_box.label(text=f"✓ 导出分辨率: {', '.join(display_info)}", icon='CHECKMARK')
                
                # 性能提示
                if len(all_selected) > 2 or any(int(r.split('x')[0]) >= 4096 for r in all_selected):
                    summary_box.label(text="💡 高分辨率/多分辨率烘焙耗时较长", icon='INFO')
            else:
                warning_box = multi_res_box.box()
                warning_box.alert = True
                warning_box.label(text="⚠ 请至少选择一个分辨率", icon='ERROR')

        layout.separator()

        # =============================================================================
        # 5. 通道选择
        # =============================================================================
        channels_box = layout.box()
        channels_box.label(text="🎨 通道选择", icon='MATERIAL')
        
        # 快捷选择按钮
        quick_box = channels_box.box()
        quick_box.label(text="快捷选择:", icon='PRESET')
        quick_row1 = quick_box.row(align=True)
        quick_row1.operator("mbnl.select_basic", text="基础PBR")
        quick_row1.operator("mbnl.select_full", text="完整PBR")
        quick_row1.operator("mbnl.select_none", text="全部取消")
        
        quick_row2 = quick_box.row(align=True)
        quick_row2.operator("mbnl.select_custom_shader", text="仅自定义着色器")
        quick_row2.operator("mbnl.diagnose_custom_shader", text="诊断", icon='CONSOLE')

        # 基础PBR通道
        basic_box = channels_box.box()
        basic_box.label(text="基础PBR通道:", icon='MATERIAL_DATA')
        basic_row = basic_box.row(align=True)
        basic_col1 = basic_row.column()
        basic_col1.prop(scene, "mbnl_include_basecolor")
        basic_col1.prop(scene, "mbnl_include_metallic")
        basic_col2 = basic_row.column()
        basic_col2.prop(scene, "mbnl_include_roughness")
        basic_col2.prop(scene, "mbnl_include_normal")

        # 高级PBR通道
        advanced_box = channels_box.box()
        advanced_box.label(text="高级PBR通道:", icon='NODE_MATERIAL')
        advanced_row = advanced_box.row(align=True)
        advanced_col1 = advanced_row.column()
        advanced_col1.prop(scene, "mbnl_include_subsurface")
        advanced_col1.prop(scene, "mbnl_include_emission")
        advanced_col1.prop(scene, "mbnl_include_specular")
        advanced_col1.prop(scene, "mbnl_include_sheen")
        advanced_col2 = advanced_row.column()
        advanced_col2.prop(scene, "mbnl_include_transmission")
        advanced_col2.prop(scene, "mbnl_include_alpha")
        advanced_col2.prop(scene, "mbnl_include_clearcoat")
        advanced_col2.prop(scene, "mbnl_include_clearcoat_roughness")

        # 特殊通道
        special_box = channels_box.box()
        special_box.label(text="特殊通道:", icon='MODIFIER_DATA')
        special_row = special_box.row(align=True)
        special_row.prop(scene, "mbnl_include_displacement")
        special_row.prop(scene, "mbnl_include_ambient_occlusion")
        
        # 自定义着色器烘焙
        custom_shader_box = channels_box.box()
        custom_shader_box.label(text="自定义着色器:", icon='NODE_MATERIAL')
        custom_shader_box.prop(scene, "mbnl_include_custom_shader")
        
        # 自定义着色器说明
        if scene.mbnl_include_custom_shader:
            info_box = custom_shader_box.box()
            info_box.label(text="💡 自定义着色器烘焙说明:", icon='INFO')
            info_box.label(text="• 将烘焙当前连接到Material Output的着色器")
            info_box.label(text="• 支持所有类型的着色器节点和节点组")
            info_box.label(text="• 包括Diffuse、Glossy、Emission、节点组等")
            info_box.label(text="• 烘焙结果为着色器的最终颜色输出")
            
            # 混合着色器策略选择
            mixed_count = (material_stats.get('mixed_shader_network', 0) + 
                          material_stats.get('principled_with_custom', 0) + 
                          material_stats.get('custom_with_principled', 0))
            
            if mixed_count > 0:
                strategy_box = custom_shader_box.box()
                strategy_box.label(text="🔀 混合着色器策略:", icon='NODE_MATERIAL')
                strategy_box.label(text=f"检测到 {mixed_count} 个混合着色器材质")
                strategy_box.prop(scene, "mbnl_mixed_shader_strategy", text="处理策略")
                
                # 策略说明
                strategy_info = strategy_box.box()
                if scene.mbnl_mixed_shader_strategy == 'SURFACE_OUTPUT':
                    strategy_info.label(text="✓ 完整表面输出：烘焙最终混合结果（推荐）", icon='CHECKMARK')
                elif scene.mbnl_mixed_shader_strategy == 'PRINCIPLED_ONLY':
                    strategy_info.label(text="⚠ 仅Principled BSDF：忽略自定义着色器部分", icon='ERROR')
                elif scene.mbnl_mixed_shader_strategy == 'CUSTOM_ONLY':
                    strategy_info.label(text="🧪 仅自定义着色器：实验性功能，可能不稳定", icon='EXPERIMENTAL')
            
            # 针对节点组的特殊说明
            warning_box = info_box.box()
            warning_box.label(text="🔧 节点组优化:", icon='NODE_MATERIAL')
            warning_box.label(text="• 智能检测节点组的Shader/BSDF/Color输出")
            warning_box.label(text="• 自动解决节点组输出连接问题")
            warning_box.label(text="• 详细的烘焙过程日志帮助调试")

        layout.separator()

        # =============================================================================
        # 6. 材质图集烘焙
        # =============================================================================
        atlas_box = layout.box()
        atlas_box.label(text="🎯 材质图集烘焙", icon='TEXTURE')
        
        # 检查是否适合图集烘焙
        atlas_eligible = False
        if selected_objects and len(selected_objects) == 1:
            obj = selected_objects[0]
            material_count = len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
            if material_count >= 2:
                atlas_eligible = True
                safe_obj_name = safe_encode_text(obj.name, "未命名物体")
                atlas_box.label(text=f"✓ 物体: {safe_obj_name} ({material_count} 个材质)", icon='CHECKMARK')
            else:
                atlas_box.label(text="需要至少2个材质槽", icon='INFO')
        else:
            if len(selected_objects) > 1:
                atlas_box.label(text="图集烘焙只支持单个物体", icon='INFO')
            else:
                atlas_box.label(text="请选择一个物体", icon='INFO')
        
        if atlas_eligible:
            # 图集设置
            atlas_settings_box = atlas_box.box()
            atlas_settings_box.label(text="图集设置:", icon='SETTINGS')
            
            atlas_settings_box.prop(scene, "mbnl_atlas_layout_mode")
            
            if scene.mbnl_atlas_layout_mode == 'AUTO':
                obj = selected_objects[0]
                material_count = len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
                auto_cols, auto_rows = calculate_atlas_layout(material_count)
                atlas_settings_box.label(text=f"自动布局: {auto_cols}×{auto_rows}", icon='AUTO')
            else:
                manual_row = atlas_settings_box.row(align=True)
                manual_row.prop(scene, "mbnl_atlas_cols")
                manual_row.prop(scene, "mbnl_atlas_rows")
            
            atlas_settings_box.prop(scene, "mbnl_atlas_padding")
            atlas_settings_box.prop(scene, "mbnl_atlas_update_uv")
            
            # 通道选择
            atlas_channels_box = atlas_box.box()
            atlas_channels_box.label(text="图集通道:", icon='MATERIAL')
            
            atlas_row = atlas_channels_box.row(align=True)
            atlas_col1 = atlas_row.column()
            atlas_col1.prop(scene, "mbnl_atlas_include_basecolor", text="基础色")
            atlas_col1.prop(scene, "mbnl_atlas_include_metallic", text="金属度")
            atlas_col2 = atlas_row.column()
            atlas_col2.prop(scene, "mbnl_atlas_include_roughness", text="粗糙度")
            atlas_col2.prop(scene, "mbnl_atlas_include_normal", text="法线")
            
            # 图集烘焙按钮
            atlas_button_row = atlas_box.row()
            atlas_button_row.scale_y = 1.3
            
            atlas_op = atlas_button_row.operator("mbnl.bake_material_atlas", text="🎯 烘焙材质图集", icon='TEXTURE')
            atlas_op.resolution = scene.mbnl_resolution
            atlas_op.atlas_layout_mode = scene.mbnl_atlas_layout_mode
            atlas_op.atlas_cols = scene.mbnl_atlas_cols
            atlas_op.atlas_rows = scene.mbnl_atlas_rows
            atlas_op.atlas_padding = scene.mbnl_atlas_padding
            atlas_op.atlas_update_uv = scene.mbnl_atlas_update_uv
            atlas_op.include_basecolor = scene.mbnl_atlas_include_basecolor
            atlas_op.include_roughness = scene.mbnl_atlas_include_roughness
            atlas_op.include_metallic = scene.mbnl_atlas_include_metallic
            atlas_op.include_normal = scene.mbnl_atlas_include_normal
            
            # 图集说明
            atlas_info_box = atlas_box.box()
            atlas_info_box.label(text="💡 图集烘焙说明:", icon='INFO')
            atlas_info_box.label(text="• 将多个材质槽合并到一张纹理")
            atlas_info_box.label(text="• 自动重新映射UV坐标")
            atlas_info_box.label(text="• 适用于游戏优化和减少Draw Call")
            atlas_info_box.label(text="• 每个材质占用图集的一个区域")
        else:
            atlas_box.enabled = False

        layout.separator()

        # =============================================================================
        # 7. 常规烘焙执行
        # =============================================================================
        bake_box = layout.box()
        bake_box.label(text="🚀 开始烘焙", icon='RENDER_RESULT')
        
        # 检查是否可以执行烘焙
        can_bake = True
        bake_issues = []
        
        if not selected_objects:
            can_bake = False
            bake_issues.append("未选择物体")
        elif total_materials == 0:
            can_bake = False
            bake_issues.append("没有可用材质")
        
        # 检查通道选择
        selected_channels = []
        if scene.mbnl_include_basecolor: selected_channels.append("基础色")
        if scene.mbnl_include_roughness: selected_channels.append("粗糙度")
        if scene.mbnl_include_metallic: selected_channels.append("金属度")
        if scene.mbnl_include_normal: selected_channels.append("法线")
        if scene.mbnl_include_subsurface: selected_channels.append("次表面")
        if scene.mbnl_include_transmission: selected_channels.append("透射")
        if scene.mbnl_include_emission: selected_channels.append("自发光")
        if scene.mbnl_include_alpha: selected_channels.append("透明度")
        if scene.mbnl_include_specular: selected_channels.append("镜面")
        if scene.mbnl_include_clearcoat: selected_channels.append("清漆")
        if scene.mbnl_include_clearcoat_roughness: selected_channels.append("清漆粗糙度")
        if scene.mbnl_include_sheen: selected_channels.append("光泽")
        if scene.mbnl_include_displacement: selected_channels.append("置换")
        if scene.mbnl_include_ambient_occlusion: selected_channels.append("AO")
        if scene.mbnl_include_custom_shader: selected_channels.append("自定义着色器")
        
        if not selected_channels:
            can_bake = False
            bake_issues.append("未选择任何通道")
        
        # 检查多分辨率设置
        if scene.mbnl_enable_multi_resolution:
            has_resolution = (scene.mbnl_res_512 or scene.mbnl_res_1024 or scene.mbnl_res_2048 or 
                             scene.mbnl_res_4096 or scene.mbnl_res_8192)
            if scene.mbnl_enable_custom_resolution:
                has_resolution = has_resolution or scene.mbnl_use_custom_1 or scene.mbnl_use_custom_2 or scene.mbnl_use_custom_3
            
            if not has_resolution:
                can_bake = False
                bake_issues.append("多分辨率已启用但未选择分辨率")
        
        # 显示烘焙准备状态
        if can_bake:
            status_box = bake_box.box()
            status_box.label(text="✓ 准备就绪", icon='CHECKMARK')
            
            # 显示烘焙摘要
            if selected_channels:
                if len(selected_channels) <= 6:
                    status_box.label(text=f"通道: {', '.join(selected_channels)}")
                else:
                    status_box.label(text=f"通道: {len(selected_channels)} 个已选择")
            
            if scene.mbnl_enable_multi_resolution:
                res_count = sum([scene.mbnl_res_512, scene.mbnl_res_1024, scene.mbnl_res_2048, 
                               scene.mbnl_res_4096, scene.mbnl_res_8192])
                if scene.mbnl_enable_custom_resolution:
                    res_count += sum([scene.mbnl_use_custom_1, scene.mbnl_use_custom_2, scene.mbnl_use_custom_3])
                status_box.label(text=f"分辨率: {res_count} 个")
            else:
                status_box.label(text=f"分辨率: {scene.mbnl_resolution}×{scene.mbnl_resolution}")
        else:
            issues_box = bake_box.box()
            issues_box.alert = True
            issues_box.label(text="⚠ 无法执行烘焙", icon='ERROR')
            for issue in bake_issues:
                issues_box.label(text=f"• {issue}")
        
        # 烘焙按钮
        button_layout = bake_box
        button_layout.enabled = can_bake
        
        button_row = button_layout.row()
        button_row.scale_y = 1.5  # 让按钮更显眼
        
        op = button_row.operator(MBNL_OT_bake.bl_idname, text="🎯 开始烘焙PBR贴图", icon='RENDER_RESULT')
        
        # 设置操作器参数
        op.replace_nodes = scene.mbnl_replace_nodes
        op.resolution = scene.mbnl_resolution
        op.include_lighting = scene.mbnl_include_lighting
        op.lighting_shadow_mode = scene.mbnl_lighting_shadow_mode
        op.organize_folders = scene.mbnl_organize_folders
        op.enable_multi_resolution = scene.mbnl_enable_multi_resolution
        op.res_512 = scene.mbnl_res_512
        op.res_1024 = scene.mbnl_res_1024
        op.res_2048 = scene.mbnl_res_2048
        op.res_4096 = scene.mbnl_res_4096
        op.res_8192 = scene.mbnl_res_8192
        op.enable_custom_resolution = scene.mbnl_enable_custom_resolution
        op.custom_width_1 = scene.mbnl_custom_width_1
        op.custom_height_1 = scene.mbnl_custom_height_1
        op.custom_width_2 = scene.mbnl_custom_width_2
        op.custom_height_2 = scene.mbnl_custom_height_2
        op.custom_width_3 = scene.mbnl_custom_width_3
        op.custom_height_3 = scene.mbnl_custom_height_3
        op.use_custom_1 = scene.mbnl_use_custom_1
        op.use_custom_2 = scene.mbnl_use_custom_2
        op.use_custom_3 = scene.mbnl_use_custom_3
        op.include_basecolor = scene.mbnl_include_basecolor
        op.include_roughness = scene.mbnl_include_roughness
        op.include_metallic = scene.mbnl_include_metallic
        op.include_normal = scene.mbnl_include_normal
        op.include_subsurface = scene.mbnl_include_subsurface
        op.include_transmission = scene.mbnl_include_transmission
        op.include_emission = scene.mbnl_include_emission
        op.include_alpha = scene.mbnl_include_alpha
        op.include_specular = scene.mbnl_include_specular
        op.include_clearcoat = scene.mbnl_include_clearcoat
        op.include_clearcoat_roughness = scene.mbnl_include_clearcoat_roughness
        op.include_sheen = scene.mbnl_include_sheen
        op.include_displacement = scene.mbnl_include_displacement
        op.include_ambient_occlusion = scene.mbnl_include_ambient_occlusion
        op.include_custom_shader = scene.mbnl_include_custom_shader
        op.mixed_shader_strategy = scene.mbnl_mixed_shader_strategy
        
        # Color space settings
        op.colorspace_mode = scene.mbnl_colorspace_mode
        op.colorspace_basecolor = scene.mbnl_colorspace_basecolor
        op.colorspace_normal = scene.mbnl_colorspace_normal
        op.colorspace_roughness = scene.mbnl_colorspace_roughness
        op.colorspace_emission = scene.mbnl_colorspace_emission
        op.colorspace_manual_override = scene.mbnl_colorspace_manual_override
        
        # Add usage tips
        if can_bake:
            tips_box = bake_box.box()
            tips_box.label(text="💡 使用提示:", icon='INFO')
            tips_box.label(text="• 烘焙过程中请勿操作其他功能")
            tips_box.label(text="• 高分辨率烘焙可能需要较长时间")
            tips_box.label(text="• 烘焙进度将在控制台显示")

        layout.separator()

        # =============================================================================
        # 8. UDIM支持
        # =============================================================================
        udim_box = layout.box()
        udim_box.label(text="🔸 UDIM瓦片烘焙", icon='UV')
        
        # UDIM基本设置
        udim_box.prop(scene, "mbnl_enable_udim")
        
        if scene.mbnl_enable_udim:
            udim_settings_box = udim_box.box()
            udim_settings_box.label(text="UDIM设置:", icon='SETTINGS')
            
            udim_settings_box.prop(scene, "mbnl_udim_auto_detect")
            
            if not scene.mbnl_udim_auto_detect:
                # 手动范围设置
                range_row = udim_settings_box.row(align=True)
                range_row.prop(scene, "mbnl_udim_range_start", text="起始")
                range_row.prop(scene, "mbnl_udim_range_end", text="结束")
                
                # 显示范围信息
                tile_count = scene.mbnl_udim_range_end - scene.mbnl_udim_range_start + 1
                range_info = udim_settings_box.row()
                range_info.label(text=f"将烘焙 {tile_count} 个UDIM瓦片")
            
            udim_settings_box.prop(scene, "mbnl_udim_naming_mode")
            
            # UDIM命名示例
            naming_example_box = udim_settings_box.box()
            naming_example_box.label(text="文件命名示例:", icon='FILE_TEXT')
            
            if scene.mbnl_udim_naming_mode == 'STANDARD':
                naming_example_box.label(text="material.1001.basecolor.png")
            elif scene.mbnl_udim_naming_mode == 'MARI':
                naming_example_box.label(text="material_1001_basecolor.png")
            elif scene.mbnl_udim_naming_mode == 'MUDBOX':
                naming_example_box.label(text="material.basecolor.1001.png")
            
            # UDIM检测预览
            if selected_objects and len(selected_objects) == 1:
                obj = selected_objects[0]
                if obj.data.uv_layers:
                    detected_tiles = detect_udim_tiles(obj)
                    
                    if detected_tiles:
                        preview_box = udim_box.box()
                        preview_box.label(text="✓ 检测到的UDIM瓦片:", icon='CHECKMARK')
                        
                        # 显示检测到的瓦片
                        tiles_text = ", ".join(str(tile) for tile in detected_tiles[:10])  # 最多显示10个
                        if len(detected_tiles) > 10:
                            tiles_text += f" ... (共{len(detected_tiles)}个)"
                        
                        preview_box.label(text=tiles_text)
                        
                        # 提示信息
                        info_row = preview_box.row()
                        info_row.label(text=f"💡 将为每个瓦片生成独立纹理", icon='INFO')
                    else:
                        warning_box = udim_box.box()
                        warning_box.alert = True
                        warning_box.label(text="⚠ 未检测到UDIM瓦片", icon='ERROR')
                        warning_box.label(text="模型可能使用标准0-1 UV布局")
                else:
                    warning_box = udim_box.box()
                    warning_box.alert = True
                    warning_box.label(text="⚠ 物体没有UV层", icon='ERROR')
            else:
                info_box = udim_box.box()
                info_box.label(text="💡 请选择单个物体以预览UDIM瓦片", icon='INFO')
        else:
            udim_box.label(text="UDIM支持已禁用，将使用标准UV烘焙")

        layout.separator()


# -----------------------------------------------------------------------------
# 材质图集烘焙操作器
# -----------------------------------------------------------------------------

class MBNL_OT_bake_material_atlas(Operator):
    bl_idname = "mbnl.bake_material_atlas"
    bl_label = "Bake Material Atlas"
    bl_description = "Bake multiple material slots to the same texture"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(subtype="DIR_PATH")
    resolution: IntProperty(name="Resolution", default=2048, min=16, max=16384)
    atlas_layout_mode: EnumProperty(
        name="Atlas Layout",
        items=[
            ('AUTO', 'Auto Layout', 'Automatically calculate optimal layout'),
            ('MANUAL', 'Manual Layout', 'Manually specify rows and columns'),
        ],
        default='AUTO'
    )
    atlas_cols: IntProperty(name="Columns", default=2, min=1, max=8)
    atlas_rows: IntProperty(name="Rows", default=2, min=1, max=8)
    atlas_padding: FloatProperty(name="Padding", default=0.02, min=0.0, max=0.1)
    atlas_update_uv: BoolProperty(name="Update UV Mapping", default=True)
    
    # Channel selection
    include_basecolor: BoolProperty(name="Base Color", default=True)
    include_roughness: BoolProperty(name="Roughness", default=True)
    include_metallic: BoolProperty(name="Metallic", default=True)
    include_normal: BoolProperty(name="Normal", default=True)

    def execute(self, context):
        ensure_cycles(context.scene)
        
        # 检查选中的物体
        selected_objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not selected_objects:
            self.report({'WARNING'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        if len(selected_objects) > 1:
            self.report({'WARNING'}, "材质图集功能一次只能处理一个物体")
            return {'CANCELLED'}
        
        obj = selected_objects[0]
        
        # 检查材质槽
        material_slots = [slot for slot in obj.material_slots if slot.material and slot.material.use_nodes]
        if len(material_slots) < 2:
            self.report({'WARNING'}, "物体至少需要2个材质槽才能创建图集")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"开始为物体 '{obj.name}' 创建 {len(material_slots)} 个材质的图集")
        
        # 确定图集布局
        if self.atlas_layout_mode == 'AUTO':
            cols, rows = calculate_atlas_layout(len(material_slots))
        else:
            cols, rows = self.atlas_cols, self.atlas_rows
        
        if cols * rows < len(material_slots):
            self.report({'WARNING'}, f"图集布局 {cols}×{rows} 无法容纳 {len(material_slots)} 个材质")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"使用图集布局: {cols}×{rows}")
        
        # 保存原始UV层名称
        original_uv_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
        
        try:
            # 创建图集UV映射
            if self.atlas_update_uv:
                atlas_uv_name = create_atlas_uv_layer(obj, material_slots, (cols, rows), self.atlas_padding)
                self.report({'INFO'}, f"创建图集UV层: {atlas_uv_name}")
            
            # 确定输出目录
            directory = self.directory if self.directory else bpy.path.abspath("//")
            
            # 准备通道列表
            passes = []
            if self.include_basecolor:
                passes.append(('BaseColor', 'EMIT', True))
            if self.include_roughness:
                passes.append(('Roughness', 'ROUGHNESS', False))
            if self.include_metallic:
                passes.append(('Metallic', 'EMIT', False))
            if self.include_normal:
                passes.append(('Normal', 'NORMAL', False))
            
            # 为每个通道创建图集纹理
            context.view_layer.objects.active = obj
            
            for suffix, btype, alpha in passes:
                # 创建图集图像
                img_name = f"{obj.name}_Atlas_{suffix}"
                img = bpy.data.images.new(img_name, width=self.resolution, height=self.resolution, alpha=alpha)
                
                # 设置颜色空间 - 使用智能检测
                try:
                    if suffix in ['BaseColor', 'Diffuse', 'Albedo', 'Color']:
                        img.colorspace_settings.name = 'sRGB'
                    elif suffix in ['Emission', 'EmissionColor']:
                        img.colorspace_settings.name = 'sRGB'
                    else:
                        # Normal, Roughness, Metallic, etc.
                        img.colorspace_settings.name = 'Non-Color'
                except Exception as e:
                    print(f"Warning: Cannot set color space for atlas {suffix}: {e}")
                
                # 为每个材质槽烘焙到图集的对应区域
                for mat_idx, slot in enumerate(material_slots):
                    mat = slot.material
                    
                    # 获取材质在图集中的UV边界
                    u_min, v_min, u_max, v_max = get_atlas_uv_bounds(mat_idx, cols, rows, self.atlas_padding)
                    
                    # 创建临时材质用于烘焙
                    temp_mat = mat.copy()
                    temp_mat.name = f"TEMP_{mat.name}"
                    obj.material_slots[mat_idx].material = temp_mat
                    
                    try:
                        nt = temp_mat.node_tree
                        
                        # 创建烘焙节点
                        bake_node = nt.nodes.new("ShaderNodeTexImage")
                        bake_node.image = img
                        bake_node.select = True
                        nt.nodes.active = bake_node
                        
                        # 选择当前材质的面
                        bpy.ops.object.mode_set(mode='EDIT')
                        bpy.ops.mesh.select_all(action='DESELECT')
                        bpy.ops.object.material_slot_select()
                        bpy.ops.object.mode_set(mode='OBJECT')
                        
                        # 执行烘焙
                        if btype == 'EMIT':
                            # 使用emission烘焙
                            if suffix == 'BaseColor':
                                input_mapping = create_input_mapping()
                                basecolor_input = input_mapping.get('BaseColor', 'Base Color')
                                with temporary_emission_input(nt, basecolor_input):
                                    bpy.ops.object.bake(type=btype, margin=4, use_clear=False)
                            elif suffix == 'Metallic':
                                with temporary_emission_metallic(nt):
                                    bpy.ops.object.bake(type=btype, margin=4, use_clear=False)
                        else:
                            # 直接烘焙
                            bpy.ops.object.bake(type=btype, margin=4, use_clear=False)
                        
                        # 清理烘焙节点
                        nt.nodes.remove(bake_node)
                        
                    except Exception as e:
                        self.report({'ERROR'}, f"烘焙材质 {mat.name} 失败: {str(e)}")
                    
                    finally:
                        # 恢复原始材质
                        obj.material_slots[mat_idx].material = mat
                        # 删除临时材质
                        bpy.data.materials.remove(temp_mat)
                
                # 保存图集图像
                img_path = os.path.join(directory, f"{img_name}.png")
                img.filepath_raw = img_path
                img.file_format = 'PNG'
                img.save()
                
                self.report({'INFO'}, f"保存图集 {suffix}: {img_path}")
            
            self.report({'INFO'}, f"材质图集烘焙完成！")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"材质图集烘焙失败: {str(e)}")
            return {'CANCELLED'}
        
        finally:
            # 恢复原始UV层
            if self.atlas_update_uv and original_uv_name:
                restore_original_uv_layer(obj, original_uv_name)


# -----------------------------------------------------------------------------
# UDIM Helper Functions
# -----------------------------------------------------------------------------

def detect_udim_tiles(obj):
    """检测物体使用的UDIM瓦片"""
    if not obj.data.uv_layers:
        return []
    
    tiles = set()
    uv_layer = obj.data.uv_layers.active
    
    if not uv_layer:
        return []
    
    # 遍历所有UV坐标，确定使用的瓦片
    for polygon in obj.data.polygons:
        for loop_index in polygon.loop_indices:
            uv = uv_layer.data[loop_index].uv
            # 计算UDIM瓦片编号
            tile_u = int(uv.x)
            tile_v = int(uv.y) 
            
            # UDIM编号计算：1001 + tile_u + (tile_v * 10)
            udim_number = 1001 + tile_u + (tile_v * 10)
            
            # 只添加有效的UDIM瓦片（通常在1001-1100范围内）
            if 1001 <= udim_number <= 1100:
                tiles.add(udim_number)
    
    return sorted(list(tiles))


def get_udim_tile_bounds(udim_number):
    """获取UDIM瓦片的UV边界"""
    # 从UDIM编号计算瓦片坐标
    tile_index = udim_number - 1001
    tile_u = tile_index % 10
    tile_v = tile_index // 10
    
    return {
        'u_min': float(tile_u),
        'v_min': float(tile_v),
        'u_max': float(tile_u + 1),
        'v_max': float(tile_v + 1),
        'tile_u': tile_u,
        'tile_v': tile_v
    }


def create_udim_filename(base_name, udim_number, suffix, extension="png"):
    """创建UDIM文件名"""
    # 标准UDIM命名：basename.udim_number.suffix.extension
    return f"{base_name}.{udim_number}.{suffix}.{extension}"


def setup_udim_baking_area(obj, udim_number):
    """设置指定UDIM瓦片的烘焙区域"""
    import bmesh
    
    # 获取瓦片边界
    bounds = get_udim_tile_bounds(udim_number)
    
    # 进入编辑模式
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    
    # 创建bmesh实例
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    
    try:
        # 确保面索引有效
        bm.faces.ensure_lookup_table()
        
        # 获取UV层
        uv_layer = bm.loops.layers.uv.active
        if not uv_layer:
            return False
        
        # 取消选择所有面
        for face in bm.faces:
            face.select = False
        
        # 选择属于当前UDIM瓦片的面
        selected_faces = 0
        for face in bm.faces:
            face_in_tile = False
            for loop in face.loops:
                uv = loop[uv_layer].uv
                if (bounds['u_min'] <= uv.x < bounds['u_max'] and 
                    bounds['v_min'] <= uv.y < bounds['v_max']):
                    face_in_tile = True
                    break
            
            if face_in_tile:
                face.select = True
                selected_faces += 1
        
        # 更新网格
        bmesh.update_edit_mesh(obj.data)
        
        return selected_faces > 0
        
    except Exception as e:
        print(f"UDIM区域设置错误: {e}")
        return False
    finally:
        bm.free()
        bpy.ops.object.mode_set(mode='OBJECT')


def normalize_udim_uvs_for_baking(obj, udim_number):
    """将UDIM瓦片的UV坐标临时归一化到0-1范围进行烘焙"""
    import bmesh
    
    bounds = get_udim_tile_bounds(udim_number)
    
    # 进入编辑模式
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    
    # 创建bmesh实例
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    original_uvs = {}
    
    try:
        # 获取UV层
        uv_layer = bm.loops.layers.uv.active
        if not uv_layer:
            return original_uvs
        
        # 保存原始UV坐标并归一化
        for face in bm.faces:
            for loop in face.loops:
                loop_index = loop.index
                uv = loop[uv_layer].uv.copy()
                original_uvs[loop_index] = uv
                
                # 检查是否在当前UDIM瓦片内
                if (bounds['u_min'] <= uv.x < bounds['u_max'] and 
                    bounds['v_min'] <= uv.y < bounds['v_max']):
                    # 归一化到0-1范围
                    normalized_u = uv.x - bounds['tile_u']
                    normalized_v = uv.y - bounds['tile_v']
                    loop[uv_layer].uv = (normalized_u, normalized_v)
        
        # 更新网格
        bmesh.update_edit_mesh(obj.data)
        
        return original_uvs
        
    except Exception as e:
        print(f"UDIM UV归一化错误: {e}")
        return original_uvs
    finally:
        bm.free()
        bpy.ops.object.mode_set(mode='OBJECT')


def restore_udim_uvs(obj, original_uvs):
    """恢复原始UDIM UV坐标"""
    import bmesh
    
    if not original_uvs:
        return
    
    # 进入编辑模式
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    
    # 创建bmesh实例
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    
    try:
        # 获取UV层
        uv_layer = bm.loops.layers.uv.active
        if not uv_layer:
            return
        
        # 恢复原始UV坐标
        for face in bm.faces:
            for loop in face.loops:
                loop_index = loop.index
                if loop_index in original_uvs:
                    loop[uv_layer].uv = original_uvs[loop_index]
        
        # 更新网格
        bmesh.update_edit_mesh(obj.data)
        
    except Exception as e:
        print(f"UDIM UV恢复错误: {e}")
    finally:
        bm.free()
        bpy.ops.object.mode_set(mode='OBJECT')


# -----------------------------------------------------------------------------
# Helper Functions (continuing existing functions)
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

classes = (
    MBNL_OT_bake, 
    MBNL_OT_select_basic, 
    MBNL_OT_select_full, 
    MBNL_OT_select_none,
    MBNL_OT_select_custom_shader,
    MBNL_OT_diagnose_custom_shader,
    MBNL_OT_select_res_game,
    MBNL_OT_select_res_film,
    MBNL_OT_select_res_all,
    MBNL_OT_select_res_none,
    MBNL_OT_save_preset,
    MBNL_OT_load_preset,
    MBNL_OT_delete_preset,
    MBNL_OT_refresh_presets,
    MBNL_OT_set_custom_1536,
    MBNL_OT_set_custom_3072,
    MBNL_OT_set_custom_6144,
    MBNL_OT_set_custom_1920x1080,
    MBNL_OT_set_custom_1280x720,
    MBNL_OT_set_custom_2560x1440,
    MBNL_OT_set_custom_3840x2160,
    MBNL_OT_clear_custom_res,
    MBNL_PT_panel,
    MBNL_OT_bake_material_atlas
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # 场景属性
    bpy.types.Scene.mbnl_replace_nodes = BoolProperty(
        name="替换材质节点",
        description="烘焙完成后，使用烘焙的纹理重建材质。",
        default=False,
    )
    bpy.types.Scene.mbnl_resolution = IntProperty(
        name="分辨率",
        description="烘焙纹理的分辨率（像素）",
        default=2048,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_include_lighting = BoolProperty(
        name="包含光照",
        description="烘焙时包含场景光照信息，会影响基础色和其他通道",
        default=False,
    )
    bpy.types.Scene.mbnl_lighting_shadow_mode = EnumProperty(
        name="阴影模式",
        description="光照烘焙的阴影处理模式",
        items=[
            ('WITH_SHADOWS', '包含阴影', '光照烘焙包含阴影（完整光照）'),
            ('NO_SHADOWS', '无阴影', '不包含阴影，仅包含直接光照'),
        ],
        default='WITH_SHADOWS'
    )
    bpy.types.Scene.mbnl_organize_folders = BoolProperty(
        name="整理文件夹",
        description="为每个物体/材质/分辨率创建文件夹，更好地组织输出文件",
        default=True,
    )
    bpy.types.Scene.mbnl_use_custom_directory = BoolProperty(
        name="自定义输出目录",
        description="使用自定义目录保存烘焙图像",
        default=False,
    )
    bpy.types.Scene.mbnl_custom_directory = StringProperty(
        name="自定义目录路径",
        description="选择自定义输出目录路径",
        default="",
        subtype="DIR_PATH",
    )
    
    # 预设管理
    bpy.types.Scene.mbnl_preset_list = EnumProperty(
        name="预设列表",
        description="可用的烘焙预设",
        items=update_presets_enum,
        default=0
    )
    
    # 多分辨率支持
    bpy.types.Scene.mbnl_enable_multi_resolution = BoolProperty(
        name="多分辨率导出",
        description="同时导出多个分辨率的纹理",
        default=False,
    )
    bpy.types.Scene.mbnl_res_512 = BoolProperty(name="512×512", default=False)
    bpy.types.Scene.mbnl_res_1024 = BoolProperty(name="1024×1024", default=True)
    bpy.types.Scene.mbnl_res_2048 = BoolProperty(name="2048×2048", default=True)
    bpy.types.Scene.mbnl_res_4096 = BoolProperty(name="4096×4096", default=False)
    bpy.types.Scene.mbnl_res_8192 = BoolProperty(name="8192×8192", default=False)
    
    # 自定义分辨率支持（支持矩形）
    bpy.types.Scene.mbnl_enable_custom_resolution = BoolProperty(
        name="自定义分辨率",
        description="启用自定义分辨率输入",
        default=False,
    )
    bpy.types.Scene.mbnl_custom_width_1 = IntProperty(
        name="宽度 1",
        description="第一个自定义分辨率宽度",
        default=1536,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_height_1 = IntProperty(
        name="高度 1",
        description="第一个自定义分辨率高度",
        default=1536,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_width_2 = IntProperty(
        name="宽度 2",
        description="第二个自定义分辨率宽度",
        default=1920,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_height_2 = IntProperty(
        name="高度 2",
        description="第二个自定义分辨率高度",
        default=1080,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_width_3 = IntProperty(
        name="宽度 3",
        description="第三个自定义分辨率宽度",
        default=1280,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_height_3 = IntProperty(
        name="高度 3",
        description="第三个自定义分辨率高度",
        default=720,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_use_custom_1 = BoolProperty(name="启用自定义 1", default=False)
    bpy.types.Scene.mbnl_use_custom_2 = BoolProperty(name="启用自定义 2", default=False)
    bpy.types.Scene.mbnl_use_custom_3 = BoolProperty(name="启用自定义 3", default=False)
    
    # 基础 PBR 通道
    bpy.types.Scene.mbnl_include_basecolor = BoolProperty(name="基础色", default=True)
    bpy.types.Scene.mbnl_include_roughness = BoolProperty(name="粗糙度", default=True)
    bpy.types.Scene.mbnl_include_metallic = BoolProperty(name="金属度", default=True)
    bpy.types.Scene.mbnl_include_normal = BoolProperty(name="法线", default=True)
    
    # 高级 PBR 通道
    bpy.types.Scene.mbnl_include_subsurface = BoolProperty(name="次表面散射", default=False)
    bpy.types.Scene.mbnl_include_transmission = BoolProperty(name="透射", default=False)
    bpy.types.Scene.mbnl_include_emission = BoolProperty(name="自发光", default=False)
    bpy.types.Scene.mbnl_include_alpha = BoolProperty(name="透明度", default=False)
    bpy.types.Scene.mbnl_include_specular = BoolProperty(name="高光", default=False)
    bpy.types.Scene.mbnl_include_clearcoat = BoolProperty(name="清漆", default=False)
    bpy.types.Scene.mbnl_include_clearcoat_roughness = BoolProperty(name="清漆粗糙度", default=False)
    bpy.types.Scene.mbnl_include_sheen = BoolProperty(name="光泽", default=False)
    # Special channels
    bpy.types.Scene.mbnl_include_displacement = BoolProperty(name="Displacement", default=False)
    bpy.types.Scene.mbnl_include_ambient_occlusion = BoolProperty(name="Ambient Occlusion", default=False)
    
    # Custom shaders
    bpy.types.Scene.mbnl_include_custom_shader = BoolProperty(name="Custom Shader", default=False, description="Bake custom shader currently connected to Material Output")
    
    # Mixed shader strategy
    bpy.types.Scene.mbnl_mixed_shader_strategy = EnumProperty(
        name="Mixed Shader Strategy",
        description="Processing strategy when material contains both Principled BSDF and custom shaders",
        items=[
            ('SURFACE_OUTPUT', 'Full Surface Output', 'Bake complete Material Output Surface result (recommended)'),
            ('PRINCIPLED_ONLY', 'Principled BSDF Only', 'Only bake Principled BSDF part, ignore custom shaders'),
            ('CUSTOM_ONLY', 'Custom Shader Only', 'Try to bake only custom shader part (experimental)'),
        ],
        default='SURFACE_OUTPUT'
    )
    
    # 多材质槽合并功能
    bpy.types.Scene.mbnl_enable_material_atlas = BoolProperty(
        name="材质图集合并",
        description="将多个材质槽烘焙到同一张纹理上",
        default=False
    )
    bpy.types.Scene.mbnl_atlas_layout_mode = EnumProperty(
        name="图集布局",
        description="图集的布局方式",
        items=[
            ('AUTO', '自动布局', '自动计算最佳布局'),
            ('MANUAL', '手动布局', '手动指定行列数'),
        ],
        default='AUTO'
    )
    bpy.types.Scene.mbnl_atlas_cols = IntProperty(
        name="列数",
        description="图集的列数",
        default=2,
        min=1,
        max=8
    )
    bpy.types.Scene.mbnl_atlas_rows = IntProperty(
        name="行数", 
        description="图集的行数",
        default=2,
        min=1,
        max=8
    )
    bpy.types.Scene.mbnl_atlas_padding = FloatProperty(
        name="边距",
        description="材质间的边距（UV空间）",
        default=0.02,
        min=0.0,
        max=0.1
    )
    bpy.types.Scene.mbnl_atlas_update_uv = BoolProperty(
        name="更新UV映射",
        description="为图集创建新的UV映射",
        default=True
    )
    bpy.types.Scene.mbnl_atlas_include_basecolor = BoolProperty(name="基础色", default=True)
    bpy.types.Scene.mbnl_atlas_include_roughness = BoolProperty(name="粗糙度", default=True)
    bpy.types.Scene.mbnl_atlas_include_metallic = BoolProperty(name="金属度", default=True)
    bpy.types.Scene.mbnl_atlas_include_normal = BoolProperty(name="法线", default=True)
    
    # UDIM支持属性
    bpy.types.Scene.mbnl_enable_udim = BoolProperty(
        name="UDIM支持",
        description="启用UDIM瓦片烘焙，为每个UDIM瓦片生成独立纹理",
        default=False
    )
    bpy.types.Scene.mbnl_udim_auto_detect = BoolProperty(
        name="自动检测UDIM",
        description="自动检测模型使用的UDIM瓦片",
        default=True
    )
    bpy.types.Scene.mbnl_udim_range_start = IntProperty(
        name="UDIM起始",
        description="UDIM瓦片范围起始编号",
        default=1001,
        min=1001,
        max=1100
    )
    bpy.types.Scene.mbnl_udim_range_end = IntProperty(
        name="UDIM结束",
        description="UDIM瓦片范围结束编号",
        default=1010,
        min=1001,
        max=1100
    )
    bpy.types.Scene.mbnl_udim_naming_mode = EnumProperty(
        name="UDIM命名模式",
        description="UDIM文件的命名方式",
        items=[
            ('STANDARD', '标准模式', '材质名.1001.通道名.png'),
            ('MARI', 'Mari模式', '材质名_1001_通道名.png'),
            ('MUDBOX', 'Mudbox模式', '材质名.通道名.1001.png'),
        ],
        default='STANDARD'
    )
    
    # Color Space Management Properties
    bpy.types.Scene.mbnl_colorspace_mode = EnumProperty(
        name="色彩空间模式",
        description="如何处理色彩空间分配",
        items=[
            ('AUTO', '自动检测', '根据通道类型自动分配适当的色彩空间'),
            ('CUSTOM', '自定义设置', '为每种通道类型使用自定义色彩空间设置'),
            ('MANUAL', '手动覆盖', '手动覆盖所有纹理的色彩空间'),
        ],
        default='AUTO'
    )
    
    bpy.types.Scene.mbnl_colorspace_basecolor = EnumProperty(
        name="基础色",
        description="基础色/漫反射纹理的色彩空间",
        items=[
            ('sRGB', 'sRGB', '标准sRGB色彩空间(伽马校正)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB色彩空间'),
            ('Non-Color', '无色彩', '非色彩数据'),
            ('ACEScg', 'ACEScg', 'ACES工作色彩空间'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020色彩空间'),
        ],
        default='sRGB'
    )
    
    bpy.types.Scene.mbnl_colorspace_normal = EnumProperty(
        name="法线贴图",
        description="法线贴图纹理的色彩空间",
        items=[
            ('Non-Color', '无色彩', '非色彩数据(推荐用于法线贴图)'),
            ('sRGB', 'sRGB', 'sRGB色彩空间'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Raw', 'Raw', '原始色彩数据'),
        ],
        default='Non-Color'
    )
    
    bpy.types.Scene.mbnl_colorspace_roughness = EnumProperty(
        name="粗糙度/金属度",
        description="粗糙度、金属度和其他数据纹理的色彩空间",
        items=[
            ('Non-Color', '无色彩', '非色彩数据(推荐用于数据贴图)'),
            ('sRGB', 'sRGB', 'sRGB色彩空间'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Raw', 'Raw', '原始色彩数据'),
        ],
        default='Non-Color'
    )
    
    bpy.types.Scene.mbnl_colorspace_emission = EnumProperty(
        name="自发光",
        description="自发光纹理的色彩空间",
        items=[
            ('sRGB', 'sRGB', 'sRGB色彩空间(推荐用于自发光)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB色彩空间'),
            ('ACEScg', 'ACEScg', 'ACES工作色彩空间'),
            ('Non-Color', '无色彩', '非色彩数据'),
        ],
        default='sRGB'
    )
    
    bpy.types.Scene.mbnl_colorspace_manual_override = EnumProperty(
        name="手动覆盖",
        description="手动覆盖模式下用于所有纹理的色彩空间",
        items=[
            ('sRGB', 'sRGB', 'sRGB色彩空间'),
            ('Non-Color', '无色彩', '非色彩数据'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709色彩空间'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB色彩空间'),
            ('ACEScg', 'ACEScg', 'ACES工作色彩空间'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020色彩空间'),
            ('Raw', 'Raw', '原始色彩数据'),
            ('XYZ', 'XYZ', 'CIE XYZ色彩空间'),
        ],
        default='sRGB'
    )


def unregister():
    props = [
        "mbnl_replace_nodes",
        "mbnl_resolution",
        "mbnl_include_lighting",
        "mbnl_organize_folders",
        "mbnl_use_custom_directory",
        "mbnl_custom_directory",
        "mbnl_preset_list",
        # 多分辨率支持
        "mbnl_enable_multi_resolution",
        "mbnl_res_512",
        "mbnl_res_1024",
        "mbnl_res_2048",
        "mbnl_res_4096",
        "mbnl_res_8192",
        # 自定义分辨率支持
        "mbnl_enable_custom_resolution",
        "mbnl_custom_width_1",
        "mbnl_custom_height_1",
        "mbnl_custom_width_2",
        "mbnl_custom_height_2",
        "mbnl_custom_width_3",
        "mbnl_custom_height_3",
        "mbnl_use_custom_1",
        "mbnl_use_custom_2",
        "mbnl_use_custom_3",
        # 基础PBR通道
        "mbnl_include_basecolor",
        "mbnl_include_roughness",
        "mbnl_include_metallic",
        "mbnl_include_normal",
        # 高级PBR通道
        "mbnl_include_subsurface",
        "mbnl_include_transmission",
        "mbnl_include_emission",
        "mbnl_include_alpha",
        "mbnl_include_specular",
        "mbnl_include_clearcoat",
        "mbnl_include_clearcoat_roughness",
        "mbnl_include_sheen",
        # 特殊通道
        "mbnl_include_displacement",
        "mbnl_include_ambient_occlusion",
        # 自定义着色器
        "mbnl_include_custom_shader",
        "mbnl_mixed_shader_strategy",
        "mbnl_enable_material_atlas",
        "mbnl_atlas_layout_mode",
        "mbnl_atlas_cols",
        "mbnl_atlas_rows",
        "mbnl_atlas_padding",
        "mbnl_atlas_update_uv",
        "mbnl_atlas_include_basecolor",
        "mbnl_atlas_include_roughness", 
        "mbnl_atlas_include_metallic",
        "mbnl_atlas_include_normal",
        "mbnl_enable_udim",
        "mbnl_udim_auto_detect",
        "mbnl_udim_range_start",
        "mbnl_udim_range_end",
        "mbnl_udim_naming_mode",
        # 色彩空间管理
        "mbnl_colorspace_mode",
        "mbnl_colorspace_basecolor",
        "mbnl_colorspace_normal",
        "mbnl_colorspace_roughness",
        "mbnl_colorspace_emission",
        "mbnl_colorspace_manual_override",
    ]
    for p in props:
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
