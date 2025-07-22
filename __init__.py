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
    bl_label = "Bake PBR Textures"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(subtype="DIR_PATH")
    resolution: IntProperty(name="Resolution", default=2048, min=16, max=16384)
    margin: IntProperty(name="Margin", default=4, min=0, max=64)
    replace_nodes: BoolProperty(name="Replace Material Nodes", default=False)
    include_lighting: BoolProperty(name="Include Lighting", default=False, description="Include scene lighting information when baking")
    lighting_shadow_mode: EnumProperty(
        name="Shadow Mode",
        description="Shadow handling mode for lighting baking",
        items=[
            ('WITH_SHADOWS', 'With Shadows', 'Include shadows in lighting baking (complete lighting)'),
            ('NO_SHADOWS', 'No Shadows', 'Exclude shadows, only include direct lighting without shadows'),
        ],
        default='WITH_SHADOWS'
    )
    organize_folders: BoolProperty(name="Organize Folders", default=True, description="Create folders for each object/material/resolution")
    
    # Multi-resolution support
    enable_multi_resolution: BoolProperty(name="Multi-Resolution Export", default=False, description="Export textures at multiple resolutions simultaneously")
    res_512: BoolProperty(name="512×512", default=False)
    res_1024: BoolProperty(name="1024×1024", default=True)
    res_2048: BoolProperty(name="2048×2048", default=True)
    res_4096: BoolProperty(name="4096×4096", default=False)
    res_8192: BoolProperty(name="8192×8192", default=False)
    
    # Custom resolution (supports rectangular)
    enable_custom_resolution: BoolProperty(name="Custom Resolution", default=False, description="Enable custom resolution input")
    custom_width_1: IntProperty(name="Width 1", default=1536, min=16, max=16384, description="First custom resolution width")
    custom_height_1: IntProperty(name="Height 1", default=1536, min=16, max=16384, description="First custom resolution height")
    custom_width_2: IntProperty(name="Width 2", default=1920, min=16, max=16384, description="Second custom resolution width")
    custom_height_2: IntProperty(name="Height 2", default=1080, min=16, max=16384, description="Second custom resolution height")
    custom_width_3: IntProperty(name="Width 3", default=1280, min=16, max=16384, description="Third custom resolution width")
    custom_height_3: IntProperty(name="Height 3", default=720, min=16, max=16384, description="Third custom resolution height")
    use_custom_1: BoolProperty(name="Enable Custom 1", default=False)
    use_custom_2: BoolProperty(name="Enable Custom 2", default=False)
    use_custom_3: BoolProperty(name="Enable Custom 3", default=False)

    # Basic PBR channels
    include_basecolor: BoolProperty(name="Base Color", default=True)
    include_roughness: BoolProperty(name="Roughness", default=True)
    include_metallic: BoolProperty(name="Metallic", default=True)
    include_normal: BoolProperty(name="Normal", default=True)
    
    # Advanced PBR channels
    include_subsurface: BoolProperty(name="Subsurface", default=False)
    include_transmission: BoolProperty(name="Transmission", default=False)
    include_emission: BoolProperty(name="Emission", default=False)
    include_alpha: BoolProperty(name="Alpha", default=False)
    include_specular: BoolProperty(name="Specular", default=False)
    include_clearcoat: BoolProperty(name="Clearcoat", default=False)
    include_clearcoat_roughness: BoolProperty(name="Clearcoat Roughness", default=False)
    include_sheen: BoolProperty(name="Sheen", default=False)
    
    # Special channels
    include_displacement: BoolProperty(name="Displacement", default=False)
    include_ambient_occlusion: BoolProperty(name="Ambient Occlusion", default=False)
    
    # Custom shader baking
    include_custom_shader: BoolProperty(name="Custom Shader", default=False, description="Bake custom shader currently connected to Material Output")
    
    # Mixed shader processing strategy
    mixed_shader_strategy: EnumProperty(
        name="Mixed Shader Strategy",
        description="Processing strategy when material contains both Principled BSDF and custom shaders",
        items=[
            ('SURFACE_OUTPUT', 'Full Surface Output', 'Bake complete Material Output Surface result (recommended)'),
            ('PRINCIPLED_ONLY', 'Principled BSDF Only', 'Only bake Principled BSDF part, ignore custom shaders'),
            ('CUSTOM_ONLY', 'Custom Shader Only', 'Try to bake only custom shader part (experimental)'),
        ],
        default='SURFACE_OUTPUT'
    )
    
    # Multi-material slot merging functionality
    enable_material_atlas: BoolProperty(
        name="Material Atlas Merging",
        description="Bake multiple material slots to the same texture",
        default=False
    )
    atlas_layout_mode: EnumProperty(
        name="Atlas Layout",
        description="Layout mode for the atlas",
        items=[
            ('AUTO', 'Auto Layout', 'Automatically calculate optimal layout'),
            ('MANUAL', 'Manual Layout', 'Manually specify rows and columns'),
        ],
        default='AUTO'
    )
    atlas_cols: IntProperty(
        name="Columns",
        description="Number of columns in the atlas",
        default=2,
        min=1,
        max=8
    )
    atlas_rows: IntProperty(
        name="Rows", 
        description="Number of rows in the atlas",
        default=2,
        min=1,
        max=8
    )
    atlas_padding: FloatProperty(
        name="Padding",
        description="Padding between materials (UV space)",
        default=0.02,
        min=0.0,
        max=0.1
    )
    atlas_update_uv: BoolProperty(
        name="Update UV Mapping",
        description="Create new UV mapping for the atlas",
        default=True
    )
    
    # UDIM support
    enable_udim: BoolProperty(
        name="UDIM Support",
        description="Enable UDIM tile baking, generate separate textures for each UDIM tile",
        default=False
    )
    udim_auto_detect: BoolProperty(
        name="Auto Detect UDIM",
        description="Automatically detect UDIM tiles used by the model",
        default=True
    )
    udim_range_start: IntProperty(
        name="UDIM Start",
        description="Starting number for UDIM tile range",
        default=1001,
        min=1001,
        max=1100
    )
    udim_range_end: IntProperty(
        name="UDIM End",
        description="Ending number for UDIM tile range",
        default=1010,
        min=1001,
        max=1100
    )
    udim_naming_mode: EnumProperty(
        name="UDIM Naming Mode",
        description="Naming convention for UDIM files",
        items=[
            ('STANDARD', 'Standard Mode', 'material_name.1001.channel_name.png'),
            ('MARI', 'Mari Mode', 'material_name_1001_channel_name.png'),
            ('MUDBOX', 'Mudbox Mode', 'material_name.channel_name.1001.png'),
        ],
        default='STANDARD'
    )
    
    # Color Space Management
    colorspace_mode: EnumProperty(
        name="Color Space Mode",
        description="How to handle color space assignments",
        items=[
            ('AUTO', 'Auto Detection', 'Automatically assign appropriate color spaces based on channel type'),
            ('CUSTOM', 'Custom Settings', 'Use custom color space settings for each channel type'),
            ('MANUAL', 'Manual Override', 'Manually override color space for all textures'),
        ],
        default='AUTO'
    )
    
    # Color space assignments for different channel types
    colorspace_basecolor: EnumProperty(
        name="Base Color",
        description="Color space for Base Color/Diffuse textures",
        items=[
            ('sRGB', 'sRGB', 'Standard sRGB color space (gamma corrected)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB color space'),
            ('Non-Color', 'Non-Color', 'Non-color data'),
            ('ACEScg', 'ACEScg', 'ACES working color space'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020 color space'),
        ],
        default='sRGB'
    )
    
    colorspace_normal: EnumProperty(
        name="Normal Maps",
        description="Color space for Normal Map textures",
        items=[
            ('Non-Color', 'Non-Color', 'Non-color data (recommended for normal maps)'),
            ('sRGB', 'sRGB', 'sRGB color space'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Raw', 'Raw', 'Raw color data'),
        ],
        default='Non-Color'
    )
    
    colorspace_roughness: EnumProperty(
        name="Roughness/Metallic",
        description="Color space for Roughness, Metallic and other data textures",
        items=[
            ('Non-Color', 'Non-Color', 'Non-color data (recommended for data maps)'),
            ('sRGB', 'sRGB', 'sRGB color space'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Raw', 'Raw', 'Raw color data'),
        ],
        default='Non-Color'
    )
    
    colorspace_emission: EnumProperty(
        name="Emission",
        description="Color space for Emission textures",
        items=[
            ('sRGB', 'sRGB', 'sRGB color space (recommended for emission)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB color space'),
            ('ACEScg', 'ACEScg', 'ACES working color space'),
            ('Non-Color', 'Non-Color', 'Non-color data'),
        ],
        default='sRGB'
    )
    
    colorspace_manual_override: EnumProperty(
        name="Manual Override",
        description="Color space to use for all textures when using manual override mode",
        items=[
            ('sRGB', 'sRGB', 'sRGB color space'),
            ('Non-Color', 'Non-Color', 'Non-color data'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB color space'),
            ('ACEScg', 'ACEScg', 'ACES working color space'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020 color space'),
            ('Raw', 'Raw', 'Raw color data'),
            ('XYZ', 'XYZ', 'CIE XYZ color space'),
        ],
        default='sRGB'
    )

    def get_colorspace_for_channel(self, channel_suffix):
        """Determine the appropriate color space for a given channel based on user settings"""
        
        if self.colorspace_mode == 'MANUAL':
            return self.colorspace_manual_override
        
        # Define automatic color space assignments
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
            'CustomShader': 'sRGB',  # Default to sRGB for custom shaders
        }
        
        if self.colorspace_mode == 'AUTO':
            return auto_colorspace_mapping.get(channel_suffix, 'Non-Color')
        
        elif self.colorspace_mode == 'CUSTOM':
            # Map channels to user's custom settings
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
                # Default fallback for unknown channels
                return self.colorspace_roughness
        
        return 'Non-Color'  # Safe fallback

    def set_image_colorspace(self, img, channel_suffix):
        """Set the color space for an image based on channel type and user preferences"""
        try:
            target_colorspace = self.get_colorspace_for_channel(channel_suffix)
            
            # Validate that the color space exists in Blender
            available_colorspaces = []
            
            # Safe check for bpy availability
            try:
                if hasattr(bpy.types.ColorManagedViewSettings, 'bl_rna'):
                    # Try to get available color spaces
                    try:
                        # This is a safe way to check available color spaces
                        available_colorspaces = ['sRGB', 'Non-Color', 'Linear Rec.709', 'Linear sRGB', 'Raw']
                        # Extended list for newer Blender versions
                        extended_colorspaces = ['ACEScg', 'Rec.2020', 'XYZ', 'Linear', 'Filmic Log']
                        
                        # Try to access the color management to see what's available
                        try:
                            scene = bpy.context.scene
                            view_settings = scene.view_settings
                            available_colorspaces.extend(extended_colorspaces)
                        except:
                            pass
                            
                    except:
                        # Fallback to basic color spaces
                        available_colorspaces = ['sRGB', 'Non-Color', 'Linear Rec.709', 'Raw']
            except NameError:
                # bpy is not available in this scope
                available_colorspaces = ['sRGB', 'Non-Color', 'Linear Rec.709', 'Raw']
            
            # Apply color space if available, with fallbacks
            if target_colorspace in available_colorspaces or not available_colorspaces:
                img.colorspace_settings.name = target_colorspace
                self.report({'INFO'}, f"Set {channel_suffix} color space to {target_colorspace}")
            else:
                # Fallback logic
                if channel_suffix in ['BaseColor', 'Emission', 'CustomShader']:
                    fallback = 'sRGB'
                else:
                    fallback = 'Non-Color'
                
                img.colorspace_settings.name = fallback
                self.report({'WARNING'}, f"Color space '{target_colorspace}' not available, using '{fallback}' for {channel_suffix}")
                
        except (AttributeError, KeyError) as e:
            self.report({'WARNING'}, f"Cannot set color space for {channel_suffix}: {str(e)}")
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
                
                self.report({'INFO'}, f"Lighting baking settings enabled: Direct + Indirect + Color (Shadow mode: {shadow_mode})")
            
            if pass_filter:
                # 如果pass_filter是set类型，转换为Blender期望的格式
                if isinstance(pass_filter, set):
                    bpy.ops.object.bake(type=bake_type, pass_filter=pass_filter, margin=margin, use_clear=True)
                else:
                    bpy.ops.object.bake(type=bake_type, pass_filter={pass_filter}, margin=margin, use_clear=True)
            else:
                bpy.ops.object.bake(type=bake_type, margin=margin, use_clear=True)
        
        finally:
            # 恢复原始烘焙设置
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
                    self.report({'INFO'}, f"Created custom output directory: {directory}")
                except Exception as e:
                    self.report({'ERROR'}, f"Cannot create custom directory: {str(e)}, using default directory")
                    directory = bpy.path.abspath("//")
            else:
                self.report({'INFO'}, f"Using custom output directory: {directory}")
        else:
            directory = self.directory if self.directory else bpy.path.abspath("//")
            if not context.scene.mbnl_use_custom_directory:
                self.report({'INFO'}, f"Using default output directory: {directory}")
        
        # Check light sources settings (when lighting baking is enabled)
        if self.include_lighting:
            lights = [obj for obj in context.scene.objects if obj.type == 'LIGHT']
            if not lights:
                # Check world environment lighting
                world = context.scene.world
                has_world_light = False
                if world and world.use_nodes:
                    for node in world.node_tree.nodes:
                        if node.type in ['TEX_ENVIRONMENT', 'TEX_SKY'] or node.bl_idname == 'ShaderNodeBackground':
                            # Check if there's actual light intensity
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
                    self.report({'WARNING'}, "Lighting baking enabled but no light sources found in scene. Results may be dark. Consider adding lights or world environment lighting.")
                else:
                    self.report({'INFO'}, "Using world environment lighting for baking")
            else:
                light_types = {}
                for light in lights:
                    light_type = light.data.type
                    light_types[light_type] = light_types.get(light_type, 0) + 1
                
                light_info = ", ".join([f"{count} {ltype}" for ltype, count in light_types.items()])
                self.report({'INFO'}, f"Found light sources: {light_info}, proceeding with lighting baking")
        
        # Check if there are selected objects
        selected_objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not selected_objects:
            self.report({'WARNING'}, "Please select at least one mesh object")
            return {'CANCELLED'}

        total_materials = 0
        processed_materials = 0
        
        # Calculate total material count
        for obj in selected_objects:
            total_materials += len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
        
        if total_materials == 0:
            self.report({'WARNING'}, "Selected objects have no available materials")
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
            
            # Save original settings
            original_samples = scene.cycles.samples
            original_device = scene.cycles.device
            
            # Ensure sufficient samples for lighting baking
            min_samples = 128
            if hasattr(scene.cycles, 'samples'):
                if scene.cycles.samples < min_samples:
                    scene.cycles.samples = min_samples
                    self.report({'INFO'}, f"Lighting baking: Sample count adjusted to {min_samples} to ensure quality")
                else:
                    self.report({'INFO'}, f"Lighting baking: Current sample count {scene.cycles.samples}")
            
            # Check GPU acceleration status
            try:
                if scene.cycles.device == 'CPU':
                    # Try to check if GPU devices are available
                    cycles_prefs = bpy.context.preferences.addons.get('cycles')
                    if cycles_prefs and hasattr(cycles_prefs.preferences, 'devices'):
                        gpu_devices = [device for device in cycles_prefs.preferences.devices if device.type in ['CUDA', 'OPENCL', 'OPTIX', 'HIP']]
                        if gpu_devices:
                            scene.cycles.device = 'GPU'
                            self.report({'INFO'}, "Lighting baking: GPU acceleration enabled to improve baking speed")
                        else:
                            self.report({'INFO'}, "Lighting baking: Using CPU rendering (no available GPU detected)")
                    else:
                        self.report({'INFO'}, "Lighting baking: Using CPU rendering")
                else:
                    self.report({'INFO'}, "Lighting baking: GPU acceleration already enabled")
            except Exception as e:
                self.report({'INFO'}, f"Lighting baking: GPU detection failed, using current device: {scene.cycles.device}")
            
            # Set appropriate denoising options
            if hasattr(scene.cycles, 'use_denoising'):
                scene.cycles.use_denoising = True
                self.report({'INFO'}, "Lighting baking: Denoising enabled to improve quality")
            
            self.report({'INFO'}, "Note: Lighting baking quality depends on scene lighting setup and render sample count")
        
        # Folder organization information
        if self.organize_folders:
            self.report({'INFO'}, "Folder organization enabled - Files will be saved in Object/Material/Resolution structure")
        else:
            self.report({'INFO'}, "Using traditional file naming - All files saved in same directory")
        
        # Determine resolutions to bake
        if self.enable_multi_resolution:
            resolutions = []
            
            # Add preset resolutions
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
            
            # Add custom resolutions (supports rectangular)
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
            
            # Convert square resolutions to tuple format for unified processing
            square_resolutions = [(res, res) for res in resolutions]
            all_resolutions = square_resolutions + custom_resolutions
            
            # Remove duplicates and sort by area (more reasonable sorting)
            unique_resolutions = []
            for res in all_resolutions:
                if res not in unique_resolutions:
                    unique_resolutions.append(res)
            resolutions = sorted(unique_resolutions, key=lambda x: x[0] * x[1])
            
            if not resolutions:
                self.report({'WARNING'}, "Multi-resolution enabled but no resolutions selected, using default resolution")
                resolutions = [(self.resolution, self.resolution)]
            else:
                preset_info = []
                custom_info = []
                
                for width, height in resolutions:
                    if width == height and width in [512, 1024, 2048, 4096, 8192]:
                        preset_info.append(f'{width}×{height}')
                    else:
                        if width == height:
                            custom_info.append(f'{width}×{height}(custom)')
                        else:
                            custom_info.append(f'{width}×{height}(custom)')
                
                all_info = preset_info + custom_info
                self.report({'INFO'}, f"Will export the following resolutions: {', '.join(all_info)}")
        else:
            resolutions = [(self.resolution, self.resolution)]

        self.report({'INFO'}, f"Starting to process {len(selected_objects)} objects with {total_materials} materials")

        for obj in selected_objects:
            # Ensure object has UV layers
            if not obj.data.uv_layers:
                self.report({'INFO'}, f"Auto-creating UV mapping for object '{obj.name}'")
                smart_uv(obj)
            
            context.view_layer.objects.active = obj

            # UDIM detection and setup
            udim_tiles = []
            if self.enable_udim:
                if self.udim_auto_detect:
                    udim_tiles = detect_udim_tiles(obj)
                    if udim_tiles:
                        self.report({'INFO'}, f"Detected UDIM tiles: {udim_tiles}")
                    else:
                        self.report({'WARNING'}, f"Object '{obj.name}' has no UDIM tiles detected, using regular baking")
                        udim_tiles = [1001]  # Default to use 1001 tile
                else:
                    # Use specified range
                    udim_tiles = list(range(self.udim_range_start, self.udim_range_end + 1))
                    self.report({'INFO'}, f"Using specified UDIM range: {udim_tiles}")
            else:
                # Non-UDIM mode, use virtual tile 1001
                udim_tiles = [1001]

            # Process all material slots of the object
            material_slots = [slot for slot in obj.material_slots if slot.material and slot.material.use_nodes]
            
            if not material_slots:
                self.report({'INFO'}, f"Object '{obj.name}' has no available materials, skipping")
                continue
                
            self.report({'INFO'}, f"Processing object '{obj.name}' with {len(material_slots)} materials")

            for slot in material_slots:
                mat = slot.material
                processed_materials += 1
                
                # Analyze material type (with error handling)
                try:
                    analysis = analyze_material(mat)
                    material_type = analysis.get('material_type', 'unknown')
                except Exception as e:
                    self.report({'WARNING'}, f"Material '{mat.name}' analysis failed, using default processing: {str(e)}")
                    material_type = 'unknown'
                    analysis = {'material_type': 'unknown', 'has_image_textures': False}
                
                safe_mat_name = safe_encode_text(mat.name, "Unnamed Material")
                self.report({'INFO'}, f"Processing material '{safe_mat_name}' ({processed_materials}/{total_materials}) - Type: {material_type}")
                
                # Decide processing strategy based on material type
                if material_type == 'unknown':
                    self.report({'INFO'}, f"Material '{mat.name}' type unknown, attempting default processing")
                elif material_type == 'default':
                    self.report({'INFO'}, f"Material '{mat.name}' uses default settings, will bake default values")
                elif material_type == 'textured':
                    texture_count = len(analysis.get('texture_nodes', []))
                    self.report({'INFO'}, f"Material '{mat.name}' contains {texture_count} image textures")
                elif material_type == 'procedural':
                    pure_colors = analysis.get('pure_color_inputs', [])
                    if pure_colors:
                        self.report({'INFO'}, f"Material '{mat.name}' uses solid color values: {', '.join(pure_colors)}")
                    else:
                        self.report({'INFO'}, f"Material '{mat.name}' uses procedural nodes")
                elif material_type == 'mixed':
                    self.report({'INFO'}, f"Material '{mat.name}' mixes textures and solid colors")
                elif material_type == 'custom_shader':
                    custom_shaders = analysis.get('custom_shaders', [])
                    shader_names = [shader['label'] for shader in custom_shaders[:3]]  # Show first 3
                    if len(custom_shaders) <= 3:
                        self.report({'INFO'}, f"Material '{mat.name}' uses custom shaders: {', '.join(shader_names)}")
                    else:
                        self.report({'INFO'}, f"Material '{mat.name}' uses custom shaders: {', '.join(shader_names)} + {len(custom_shaders)} total")
                elif material_type == 'mixed_shader':
                    custom_count = len(analysis.get('custom_shaders', []))
                    self.report({'INFO'}, f"Material '{mat.name}' mixes Principled BSDF with {custom_count} custom shaders")
                elif material_type == 'mixed_shader_network':
                    mix_info = analysis.get('shader_network', {})
                    mix_node_name = mix_info.get('mix_node', {}).get('name', 'Unknown')
                    self.report({'INFO'}, f"Material '{mat.name}' uses mixed shader network (Mix node: {mix_node_name})")
                    self.report({'INFO'}, f"Mixed shader strategy: {self.mixed_shader_strategy}")
                elif material_type == 'principled_with_custom':
                    custom_count = len(analysis.get('custom_shaders', []))
                    self.report({'INFO'}, f"Material '{mat.name}' is Principled BSDF-based with {custom_count} custom shaders")
                elif material_type == 'custom_with_principled':
                    self.report({'INFO'}, f"Material '{mat.name}' is custom shader-based with Principled BSDF")

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
                
                # Basic PBR channels
                if self.include_basecolor:
                    if self.include_lighting:
                        # Include lighting: Use COMBINED baking to capture complete scene lighting
                        passes.append(('BaseColor', 'COMBINED', None, True, None))
                        self.report({'INFO'}, f"Material '{mat.name}' base color will include scene lighting (COMBINED method)")
                    else:
                        # No lighting: Use emission baking to ensure correct base color capture, regardless of metallic value
                        passes.append(('BaseColor', 'EMIT', None, True, input_mapping.get('BaseColor')))
                if self.include_roughness:
                    # For solid color materials, consider using emission baking as alternative
                    if material_type in ['procedural', 'default'] and not analysis.get('has_image_textures', False):
                        passes.append(('Roughness', 'EMIT', None, False, input_mapping.get('Roughness')))
                        self.report({'INFO'}, f"Material '{mat.name}' roughness will use Emission method baking (solid color material)")
                    else:
                        passes.append(('Roughness', 'ROUGHNESS', None, False, None))
                if self.include_metallic:
                    passes.append(('Metallic', 'EMIT', None, False, input_mapping.get('Metallic')))
                if self.include_normal:
                    passes.append(('Normal', 'NORMAL', None, False, None))
                
                # Advanced PBR channels
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
                
                # Special channels (not using emission method)
                if self.include_displacement:
                    passes.append(('Displacement', 'EMIT', None, False, None))  # Not through principled input
                if self.include_ambient_occlusion:
                    passes.append(('AO', 'AO', None, False, None))
                
                # Custom shader baking
                if self.include_custom_shader:
                    # Check if there's a shader connected to Material Output
                    output_node = analysis.get('output_node')
                    if output_node and output_node.inputs['Surface'].is_linked:
                        passes.append(('CustomShader', 'EMIT', None, True, None))  # Use special identifier for custom shader
                        self.report({'INFO'}, f"Material '{mat.name}' will bake custom shader output")
                    else:
                        self.report({'WARNING'}, f"Material '{mat.name}' Material Output has no connected shader, skipping custom shader baking")

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
                                    # Generate file names and paths
                                    if self.organize_folders:
                                        # Use folder organization: Object/Material/Resolution/Texture
                                        if width == height:
                                            res_folder = f"{width}x{height}"
                                        else:
                                            res_folder = f"{width}x{height}"
                                        
                                        # Clean folder names (remove illegal characters)
                                        encoded_obj_name = safe_encode_text(obj.name, "Unknown_Object")
                                        encoded_mat_name = safe_encode_text(mat.name, "Unknown_Material")
                                        safe_obj_name = "".join(c for c in encoded_obj_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                                        safe_mat_name = "".join(c for c in encoded_mat_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                                        
                                        # Ensure folder names are not empty
                                        if not safe_obj_name:
                                            safe_obj_name = "Object"
                                        if not safe_mat_name:
                                            safe_mat_name = "Material"
                                        
                                        folder_path = os.path.join(directory, safe_obj_name, safe_mat_name, res_folder)
                                        os.makedirs(folder_path, exist_ok=True)
                                        
                                        # UDIM file naming
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
                                        # Traditional naming method
                                        encoded_obj_name = safe_encode_text(obj.name, "Object")
                                        encoded_mat_name = safe_encode_text(mat.name, "Material")
                                        safe_obj_name = "".join(c for c in encoded_obj_name if c.isalnum() or c in ('_', '-')).strip()
                                        safe_mat_name = "".join(c for c in encoded_mat_name if c.isalnum() or c in ('_', '-')).strip()
                                        
                                        # Ensure names are not empty
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

                                    # Adjust baking strategy based on material type
                                    should_bake = True
                                    
                                    # For materials with only solid colors, check if certain channels need baking
                                    if material_type in ['procedural', 'default'] and not analysis['has_image_textures']:
                                        # Check if specific inputs are meaningful to bake
                                        if emission_input:
                                            principled = analysis.get('principled_node')
                                            if principled and emission_input in principled.inputs:
                                                input_socket = principled.inputs[emission_input]
                                                if not input_socket.is_linked:
                                                    # Check if it's a default value
                                                    try:
                                                        default_val = input_socket.default_value
                                                        if suffix == 'Metallic' and abs(default_val) < 0.01:
                                                            self.report({'INFO'}, f"Skipping {suffix} baking - using default value {default_val}")
                                                            should_bake = False
                                                        elif suffix == 'Roughness' and abs(default_val - 0.5) < 0.01:
                                                            self.report({'INFO'}, f"Skipping {suffix} baking - using default value {default_val}")
                                                            should_bake = False
                                                        elif suffix in ['Subsurface', 'Transmission', 'Specular', 'Clearcoat', 'Sheen'] and abs(default_val) < 0.01:
                                                            self.report({'INFO'}, f"Skipping {suffix} baking - using default value {default_val}")
                                                            should_bake = False
                                                    except (AttributeError, TypeError):
                                                        pass
                            
                                    if should_bake:
                                        try:
                                            # Check if it's lighting baking for base color
                                            is_lighting_basecolor = (suffix == 'BaseColor' and self.include_lighting)
                                            # Check if it's custom shader
                                            is_custom_shader = (suffix == 'CustomShader')
                                            
                                            if is_custom_shader:  # Custom shader baking
                                                self.report({'INFO'}, f"Using Emission method to bake custom shader output")
                                                
                                                # Check if it's a mixed shader material, if so apply strategy
                                                if material_type in ['mixed_shader_network', 'principled_with_custom', 'custom_with_principled']:
                                                    self.report({'INFO'}, f"Detected mixed shader material, applying strategy: {self.mixed_shader_strategy}")
                                                    
                                                    if self.mixed_shader_strategy == 'PRINCIPLED_ONLY':
                                                        # Try to bake only Principled BSDF part
                                                        principled_node = analysis.get('principled_node')
                                                        if principled_node:
                                                            self.report({'INFO'}, f"According to strategy, baking only Principled BSDF part")
                                                            with temporary_principled_only_surface(nt, principled_node) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                                else:
                                                                    self.report({'ERROR'}, f"Cannot set Principled BSDF only baking, falling back to full surface output")
                                                                    with temporary_emission_surface(nt) as temp_emit:
                                                                        if temp_emit:
                                                                            self.bake_generic(context, btype, img, self.margin)
                                                        else:
                                                            self.report({'WARNING'}, f"Principled BSDF node not found, using full surface output")
                                                            with temporary_emission_surface(nt) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                    elif self.mixed_shader_strategy == 'CUSTOM_ONLY':
                                                        # Try to bake only custom shader part
                                                        self.report({'INFO'}, f"According to strategy, attempting to bake only custom shader part (experimental)")
                                                        custom_shaders = analysis.get('custom_shaders', [])
                                                        if custom_shaders:
                                                            # Select first custom shader
                                                            first_custom = custom_shaders[0]['node']
                                                            with temporary_custom_shader_only_surface(nt, first_custom) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                                else:
                                                                    self.report({'ERROR'}, f"Cannot set custom shader only baking, falling back to full surface output")
                                                                    with temporary_emission_surface(nt) as temp_emit:
                                                                        if temp_emit:
                                                                            self.bake_generic(context, btype, img, self.margin)
                                                        else:
                                                            self.report({'WARNING'}, f"Custom shader node not found, using full surface output")
                                                            with temporary_emission_surface(nt) as temp_emit:
                                                                if temp_emit:
                                                                    self.bake_generic(context, btype, img, self.margin)
                                                    else:  # SURFACE_OUTPUT or default
                                                        self.report({'INFO'}, f"Using full surface output strategy")
                                                        with temporary_emission_surface(nt) as temp_emit:
                                                            if temp_emit:
                                                                self.bake_generic(context, btype, img, self.margin)
                                                            else:
                                                                self.report({'ERROR'}, f"Cannot set custom shader baking, skipping {suffix}")
                                                else:
                                                    # Non-mixed shader material, use original logic
                                                    # Get material output node and connected shader information
                                                    output_node = analysis.get('output_node')
                                                    if output_node and output_node.inputs['Surface'].is_linked:
                                                        shader_node = output_node.inputs['Surface'].links[0].from_node
                                                        shader_type = shader_node.bl_idname
                                                        shader_name = shader_node.name
                                                        self.report({'INFO'}, f"Detected shader type: {shader_type} ('{shader_name}')")
                                                        
                                                        # If it's a node group, show more information
                                                        if shader_type == 'ShaderNodeNodeGroup':
                                                            if hasattr(shader_node, 'node_tree') and shader_node.node_tree:
                                                                group_name = shader_node.node_tree.name
                                                                self.report({'INFO'}, f"Node group name: {group_name}")
                                                                # Show node group outputs
                                                                outputs = list(shader_node.outputs.keys())
                                                                self.report({'INFO'}, f"Node group outputs: {outputs}")
                                                
                                                    with temporary_emission_surface(nt) as temp_emit:
                                                        if temp_emit:
                                                            self.bake_generic(context, btype, img, self.margin)
                                                        else:
                                                            self.report({'ERROR'}, f"Cannot set custom shader baking, skipping {suffix}")
                                            elif emission_input and not is_lighting_basecolor:  # Channels that need emission baking (except lighting base color)
                                                if suffix == 'BaseColor' and not self.include_lighting:
                                                    self.report({'INFO'}, f"Using Emission method to bake base color to ensure correct solid color capture")
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
                                                        self.bake_generic(context, btype, img, self.margin, use_lighting=True, shadow_mode=self.lighting_shadow_mode)
                                                        
                                                    finally:
                                                        # 恢复原始材质设置
                                                        if original_metallic is not None:
                                                            principled.inputs['Metallic'].default_value = original_metallic
                                                else:
                                                    # 如果没有Principled BSDF，直接烘焙
                                                    self.bake_generic(context, btype, img, self.margin, use_lighting=True, shadow_mode=self.lighting_shadow_mode)
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
                                                self.report({'INFO'}, f"Baked {suffix} texture ({width}×{height}): {relative_path}")
                                            else:
                                                self.report({'INFO'}, f"Baked {suffix} texture ({width}×{height}): {img_name}.png")
                                                
                                        except Exception as e:
                                            self.report({'ERROR'}, f"Baked {suffix} texture failed ({width}×{height}): {str(e)}")
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
                                                        self.report({'INFO'}, f"Created solid {suffix} texture ({width}×{height}): {relative_path} (Value: {default_val})")
                                                    else:
                                                        self.report({'INFO'}, f"Created solid {suffix} texture ({width}×{height}): {img_name}.png (Value: {default_val})")
                                        except Exception as e:
                                            self.report({'ERROR'}, f"Created solid {suffix} texture failed ({width}×{height}): {str(e)}")
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

                # Rebuild material if requested (moved outside resolution loop)
                    primary_baked_images = all_baked_images.get(primary_resolution, {})
                    total_images_all_res = sum(len(images) for images in all_baked_images.values())
                    self.report({'INFO'}, f"Replace nodes setting: {self.replace_nodes}, Total baked images: {total_images_all_res}")
                    
                    if self.replace_nodes and primary_baked_images:
                        primary_width, primary_height = primary_resolution
                        self.report({'INFO'}, f"Rebuilding material '{mat.name}' nodes, using primary resolution {primary_width}×{primary_height}, baked {len(primary_baked_images)} channels")
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
                                        self.report({'WARNING'}, "Cannot create AO mix node, skipping AO mix")
                                
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
                                            self.report({'WARNING'}, f"Connection failed: {from_output} -> {to_input_name} (missing slot)")
                                except (KeyError, AttributeError, TypeError, RuntimeError) as e:
                                    self.report({'WARNING'}, f"Connection error: {str(e)}")
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
                                        self.report({'WARNING'}, f"AO mix failed, connecting directly to base color: {str(e)}")
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
                                    self.report({'WARNING'}, f"Normal connection failed: {str(e)}")

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
                                self.report({'ERROR'}, f"Main output connection failed: {str(e)}")
                            
                            # 连接位移
                            if 'Displacement' in tex_nodes and displacement_node:
                                try:
                                    nt.links.new(tex_nodes['Displacement'].outputs['Color'], displacement_node.inputs['Height'])
                                    nt.links.new(displacement_node.outputs['Displacement'], output.inputs['Displacement'])
                                except Exception as e:
                                    self.report({'WARNING'}, f"Displacement connection failed: {str(e)}")

                            connected_channels = list(primary_baked_images.keys())
                            self.report({'INFO'}, f"Successfully rebuilt material '{mat.name}' nodes, connected {len(connected_channels)} channels: {', '.join(connected_channels)}")
                            
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
                                self.report({'INFO'}, f"Restored basic nodes for material '{mat.name}'")
                            except Exception as restore_error:
                                self.report({'ERROR'}, f"Cannot restore basic nodes for material '{mat.name}': {str(restore_error)}")
                    elif not self.replace_nodes:
                        self.report({'INFO'}, f"Replace nodes feature disabled, keeping original nodes for material '{mat.name}'")
                    elif not primary_baked_images:
                        self.report({'WARNING'}, f"No successfully baked textures, skipping node reconstruction for material '{mat.name}'")

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
            self.report({'ERROR'}, "Please select at least one mesh object")
            return {'CANCELLED'}
        
        self.report({'INFO'}, "=== Custom Shader Diagnosis Report ===")
        
        for obj in selected_objects:
            obj_name = safe_encode_text(obj.name, "Unnamed Object")
            self.report({'INFO'}, f"Analyzing object: {obj_name}")
            
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    mat = slot.material
                    mat_name = safe_encode_text(mat.name, "Unnamed Material")
                    self.report({'INFO'}, f"  Material: {mat_name}")
                    
                    # Analyze material
                    analysis = analyze_material(mat)
                    material_type = analysis.get('material_type', 'unknown')
                    self.report({'INFO'}, f"    Type: {material_type}")
            
            # 检查Material Output连接
            output_node = analysis.get('output_node')
            if output_node:
                if output_node.inputs['Surface'].is_linked:
                    shader_link = output_node.inputs['Surface'].links[0]
                    shader_node = shader_link.from_node
                    shader_socket = shader_link.from_socket
                    
                    self.report({'INFO'}, f"    Shader Node: {shader_node.bl_idname} ('{shader_node.name}')")
                    self.report({'INFO'}, f"    Output Socket: '{shader_socket.name}'")
                    
                    # 如果是节点组，显示更多信息
                    if shader_node.bl_idname == 'ShaderNodeNodeGroup':
                        if hasattr(shader_node, 'node_tree') and shader_node.node_tree:
                            group_name = shader_node.node_tree.name
                            self.report({'INFO'}, f"    Node Group: {group_name}")
                            
                            # 显示所有输出
                            outputs = list(shader_node.outputs.keys())
                            self.report({'INFO'}, f"    Available Outputs: {outputs}")
                            
                            # 检查哪个输出正在被使用
                            used_output = shader_socket.name
                            self.report({'INFO'}, f"    Current Used Output: '{used_output}'")
                        else:
                            self.report({'WARNING'}, f"    Node Group lacks node_tree")
                else:
                    self.report({'WARNING'}, f"    Surface input of Material Output is not connected")
            else:
                self.report({'ERROR'}, f"    Material Output node not found")
            
            # 检查自定义着色器
            custom_shaders = analysis.get('custom_shaders', [])
            if custom_shaders:
                self.report({'INFO'}, f"    Detected {len(custom_shaders)} custom shaders:")
                for shader in custom_shaders[:3]:  # 只显示前3个
                    self.report({'INFO'}, f"      - {shader['type']} ('{shader['name']}')")
            else:
                self.report({'INFO'}, f"    No custom shaders detected")
            
            # 检查混合着色器网络
            if material_type in ['mixed_shader_network', 'principled_with_custom', 'custom_with_principled']:
                self.report({'INFO'}, f"    Mixed Shader Analysis:")
                self.report({'INFO'}, f"      Principled connected to output: {analysis.get('principled_connected_to_output')}")
                self.report({'INFO'}, f"      Custom shader connected to output: {analysis.get('custom_connected_to_output')}")
                
                shader_network = analysis.get('shader_network', {})
                if shader_network:
                    mix_node = shader_network.get('mix_node')
                    if mix_node:
                        self.report({'INFO'}, f"      Mix Node: {mix_node.bl_idname} ('{mix_node.name}')")
                        self.report({'INFO'}, f"      Contains Principled: {shader_network.get('has_principled')}")
                        self.report({'INFO'}, f"      Contains Custom: {shader_network.get('has_custom')}")
            
                mix_shaders = analysis.get('mix_shaders', [])
                if mix_shaders:
                    self.report({'INFO'}, f"    Detected {len(mix_shaders)} Mix/Add Shader nodes:")
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
            self.report({'ERROR'}, "Preset name cannot be empty")
            return {'CANCELLED'}
        
        # 清理预设名称（移除非法字符）
        safe_name = "".join(c for c in self.preset_name if c.isalnum() or c in (' ', '-', '_')).strip()
        if not safe_name:
            self.report({'ERROR'}, "Preset name contains illegal characters")
            return {'CANCELLED'}
        
        # 收集所有设置
        settings = {
            # 基本设置
            'resolution': scene.mbnl_resolution,
            'replace_nodes': scene.mbnl_replace_nodes,
            'include_lighting': scene.mbnl_include_lighting,
            'lighting_shadow_mode': scene.mbnl_lighting_shadow_mode,
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
            self.report({'INFO'}, f"Preset '{safe_name}' saved successfully")
            # 更新预设列表
            scene.mbnl_preset_list = safe_name
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Preset '{safe_name}' saved failed")
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
            self.report({'WARNING'}, "No preset selected")
            return {'CANCELLED'}
        
        # 加载预设
        settings = load_preset_from_file(scene.mbnl_preset_list)
        if not settings:
            self.report({'ERROR'}, f"Preset '{scene.mbnl_preset_list}' loaded failed")
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
            
            # 色彩空间管理
            if 'colorspace_mode' in settings:
                scene.mbnl_colorspace_mode = settings['colorspace_mode']
            if 'colorspace_basecolor' in settings:
                scene.mbnl_colorspace_basecolor = settings['colorspace_basecolor']
            if 'colorspace_normal' in settings:
                scene.mbnl_colorspace_normal = settings['colorspace_normal']
            if 'colorspace_roughness' in settings:
                scene.mbnl_colorspace_roughness = settings['colorspace_roughness']
            if 'colorspace_emission' in settings:
                scene.mbnl_colorspace_emission = settings['colorspace_emission']
            if 'colorspace_manual_override' in settings:
                scene.mbnl_colorspace_manual_override = settings['colorspace_manual_override']
            
            self.report({'INFO'}, f"Preset '{scene.mbnl_preset_list}' loaded successfully")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Error applying preset settings: {str(e)}")
            return {'CANCELLED'}


class MBNL_OT_delete_preset(Operator):
    bl_idname = "mbnl.delete_preset"
    bl_label = "Delete Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        
        if scene.mbnl_preset_list == 'NONE':
            self.report({'WARNING'}, "No preset selected")
            return {'CANCELLED'}
        
        preset_name = scene.mbnl_preset_list
        
        # 删除预设文件
        if delete_preset_file(preset_name):
            self.report({'INFO'}, f"Preset '{preset_name}' deleted successfully")
            # 重置预设选择
            scene.mbnl_preset_list = 'NONE'
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Preset '{preset_name}' deleted failed")
            return {'CANCELLED'}

    def invoke(self, context, event):
        scene = context.scene
        if scene.mbnl_preset_list == 'NONE':
            self.report({'WARNING'}, "No preset selected")
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
            self.report({'INFO'}, f"Preset list refreshed, found {len(presets)} presets")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to refresh preset list: {str(e)}")
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
    bl_label = "Set 3072"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        scene.mbnl_custom_width_2 = 3072
        scene.mbnl_custom_height_2 = 3072
        scene.mbnl_use_custom_2 = True
        return {'FINISHED'}


class MBNL_OT_set_custom_6144(Operator):
    bl_idname = "mbnl.set_custom_6144"
    bl_label = "Set 6144"
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
    bl_label = "Set 1920×1080"
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
    bl_label = "Set 1280×720"
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
    bl_label = "Set 2560×1440"
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
    bl_label = "Set 3840×2160"
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
    bl_label = "Clear Custom Resolution"
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
                            print(f"UI material analysis error: {e}")

        # =============================================================================
        # 1. 顶部：物体状态信息
        # =============================================================================
        status_box = layout.box()
        status_box.label(text="📊 Current Status", icon='INFO')
        
        if selected_objects:
            
            # 主要状态信息
            status_row = status_box.row()
            status_col1 = status_row.column()
            status_col2 = status_row.column()
            
            status_col1.label(text=f"✓ Objects: {len(selected_objects)}")
            status_col2.label(text=f"✓ Materials: {total_materials}")
            
            # Material type statistics (only show when multiple types exist)
            if total_materials > 0:
                type_count = sum(1 for count in material_stats.values() if count > 0)
                if type_count > 1:
                    stats_box = status_box.box()
                    stats_box.label(text="Material Type Distribution:", icon='MATERIAL')
                    stats_row = stats_box.row()
                    stats_col1 = stats_row.column()
                    stats_col2 = stats_row.column()
                    
                    if material_stats['textured'] > 0:
                        stats_col1.label(text=f"  Textured: {material_stats['textured']}")
                    if material_stats['procedural'] > 0:
                        stats_col1.label(text=f"  Solid Color: {material_stats['procedural']}")
                    if material_stats['mixed'] > 0:
                        stats_col2.label(text=f"  Mixed: {material_stats['mixed']}")
                    if material_stats['default'] > 0:
                        stats_col2.label(text=f"  Default: {material_stats['default']}")
                    if material_stats['custom_shader'] > 0:
                        stats_col1.label(text=f"  Custom Shader: {material_stats['custom_shader']}")
                    if material_stats['mixed_shader'] > 0:
                        stats_col2.label(text=f"  Mixed Shader: {material_stats['mixed_shader']}")
                    if material_stats['mixed_shader_network'] > 0:
                        stats_box.label(text=f"🔗 Mixed Network: {material_stats['mixed_shader_network']} (strategy needed)", icon='NODE_MATERIAL')
                    if material_stats['principled_with_custom'] > 0:
                        stats_col1.label(text=f"  PBR+Custom: {material_stats['principled_with_custom']}")
                    if material_stats['custom_with_principled'] > 0:
                        stats_col2.label(text=f"  Custom+PBR: {material_stats['custom_with_principled']}")
            
            # Object details (simplified display)
            if len(selected_objects) <= 2:
                for obj in selected_objects:
                    mat_count = len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
                    safe_obj_name = safe_encode_text(obj.name, "Unnamed Object")
                    status_box.label(text=f"  • {safe_obj_name} ({mat_count} materials)")
            elif len(selected_objects) <= 5:
                detail_row = status_box.row()
                obj_names = [safe_encode_text(obj.name, "Unnamed") for obj in selected_objects[:3]]
                detail_row.label(text=f"  • {', '.join(obj_names)} + {len(selected_objects) - 3} more objects")
            else:
                detail_row = status_box.row()
                detail_row.label(text=f"  • Batch processing: {len(selected_objects)} objects")
        else:
            status_box.alert = True
            status_box.label(text="⚠ Please select at least one mesh object", icon='ERROR')

        layout.separator()

        # =============================================================================
        # 2. Preset Management
        # =============================================================================
        preset_box = layout.box()
        preset_box.label(text="🎛️ Preset Management", icon='PRESET')
        
        # Preset selection and operations
        preset_row = preset_box.row(align=True)
        preset_row.prop(scene, "mbnl_preset_list", text="")
        preset_row.operator("mbnl.refresh_presets", text="", icon='FILE_REFRESH')
        
        # Preset operation buttons
        preset_ops_row = preset_box.row(align=True)
        preset_ops_row.operator("mbnl.save_preset", text="Save", icon='FILE_NEW')
        
        load_button = preset_ops_row.row()
        load_button.enabled = scene.mbnl_preset_list != 'NONE'
        load_button.operator("mbnl.load_preset", text="Load", icon='IMPORT')
        
        delete_button = preset_ops_row.row()
        delete_button.enabled = scene.mbnl_preset_list != 'NONE'
        delete_button.operator("mbnl.delete_preset", text="Delete", icon='TRASH')
        
        # Preset status
        if scene.mbnl_preset_list != 'NONE':
            preset_box.label(text=f"✓ Current: {scene.mbnl_preset_list}", icon='CHECKMARK')

        layout.separator()

        # =============================================================================
        # 3. Basic Settings
        # =============================================================================
        basic_box = layout.box()
        basic_box.label(text="⚙️ Basic Settings", icon='SETTINGS')
        
        # Resolution settings
        if not scene.mbnl_enable_multi_resolution:
            basic_box.prop(scene, "mbnl_resolution")
        
        basic_box.prop(scene, "mbnl_replace_nodes")
        basic_box.prop(scene, "mbnl_include_lighting")
        
        # Lighting baking explanation
        if scene.mbnl_include_lighting:
            light_info_box = basic_box.box()
            light_info_box.label(text="💡 Enhanced Lighting Baking:", icon='LIGHT_SUN')
            
            # Shadow mode selection
            shadow_row = light_info_box.row()
            shadow_row.prop(scene, "mbnl_lighting_shadow_mode", text="Shadow Mode")
            
            # Shadow mode explanation
            shadow_info_box = light_info_box.box()
            if scene.mbnl_lighting_shadow_mode == 'WITH_SHADOWS':
                shadow_info_box.label(text="✓ With Shadows: Complete lighting with shadows", icon='LIGHT_SUN')
                shadow_info_box.label(text="• Includes shadows cast by all light sources")
                shadow_info_box.label(text="• Most realistic lighting reproduction")
            else:  # NO_SHADOWS
                shadow_info_box.label(text="⚡ No Shadows: Direct lighting without shadows", icon='LIGHT_SUN')
                shadow_info_box.label(text="• Temporarily disables shadows from all light sources")
                shadow_info_box.label(text="• Useful for even lighting without dark areas")
            
            light_info_box.label(text="• Uses COMBINED method to capture complete lighting")
            light_info_box.label(text="• Auto-optimizes sample count and GPU acceleration")
            light_info_box.label(text="• Intelligently adjusts material settings to improve quality")
            light_info_box.label(text="• Includes direct lighting, indirect lighting and reflections")
            
            # Add performance tip
            perf_row = light_info_box.row()
            perf_row.label(text="⚡ Tip: Lighting baking takes longer, GPU recommended", icon='INFO')

        layout.separator()

        # =============================================================================
        # 4. Color Space Management
        # =============================================================================
        colorspace_box = layout.box()
        colorspace_box.label(text="🎨 Color Space Management", icon='COLOR')
        
        # Color space mode selection
        colorspace_box.prop(scene, "mbnl_colorspace_mode", text="Mode")
        
        # Show different options based on mode
        if scene.mbnl_colorspace_mode == 'AUTO':
            cs_info_box = colorspace_box.box()
            cs_info_box.label(text="🤖 Automatic Detection:", icon='AUTO')
            cs_info_box.label(text="• Color textures (Base Color, Emission): sRGB")
            cs_info_box.label(text="• Data textures (Normal, Roughness, etc.): Non-Color")
            cs_info_box.label(text="• Optimal for most workflows")
            
        elif scene.mbnl_colorspace_mode == 'CUSTOM':
            cs_custom_box = colorspace_box.box()
            cs_custom_box.label(text="⚙️ Custom Settings:", icon='PREFERENCES')
            
            # Custom color space settings in columns
            cs_row1 = cs_custom_box.row()
            cs_col1 = cs_row1.column()
            cs_col2 = cs_row1.column()
            
            cs_col1.prop(scene, "mbnl_colorspace_basecolor", text="Base Color")
            cs_col1.prop(scene, "mbnl_colorspace_emission", text="Emission")
            
            cs_col2.prop(scene, "mbnl_colorspace_normal", text="Normal Maps")  
            cs_col2.prop(scene, "mbnl_colorspace_roughness", text="Data Maps")
            
        elif scene.mbnl_colorspace_mode == 'MANUAL':
            cs_manual_box = colorspace_box.box()
            cs_manual_box.label(text="🎛️ Manual Override:", icon='PREFERENCES')
            cs_manual_box.prop(scene, "mbnl_colorspace_manual_override", text="All Textures")
            cs_manual_box.label(text="⚠️ Override applies to ALL baked textures", icon='ERROR')

        layout.separator()

        # =============================================================================
        # 5. Output Settings
        # =============================================================================
        output_box = layout.box()
        output_box.label(text="📁 Output Settings", icon='FILE_FOLDER')
        
        # Folder organization
        output_box.prop(scene, "mbnl_organize_folders")
        
        # Folder organization explanation
        if scene.mbnl_organize_folders:
            org_info_box = output_box.box()
            org_info_box.label(text="📁 Folder Structure:", icon='FILE_FOLDER')
            org_info_box.label(text="ObjectName/MaterialName/Resolution/texture.png")
            org_info_box.label(text="Example: Cube/Material/2048x2048/basecolor.png")
        else:
            org_info_box = output_box.box()
            org_info_box.label(text="📄 Traditional Naming:", icon='FILE_BLANK')
            org_info_box.label(text="Object_Material_texture_resolution.png")
            org_info_box.label(text="Example: Cube_Material_basecolor_2048.png")
        
        # Custom output directory
        output_box.prop(scene, "mbnl_use_custom_directory")
        
        # Custom directory settings
        if scene.mbnl_use_custom_directory:
            custom_dir_box = output_box.box()
            custom_dir_box.label(text="Custom Output Directory:", icon='FILE_FOLDER')
            custom_dir_box.prop(scene, "mbnl_custom_directory", text="Path")
            
            # Show current path information
            if scene.mbnl_custom_directory:
                try:
                    abs_path = bpy.path.abspath(scene.mbnl_custom_directory)
                    safe_path = safe_path_display(abs_path)
                    
                    if os.path.exists(abs_path):
                        info_row = custom_dir_box.row()
                        info_row.label(text=f"✓ Directory exists: {safe_path}", icon='CHECKMARK')
                    else:
                        warning_row = custom_dir_box.row()
                        warning_row.alert = True
                        warning_row.label(text=f"⚠ Directory doesn't exist, will be created: {safe_path}", icon='ERROR')
                except Exception as e:
                    error_row = custom_dir_box.row()
                    error_row.alert = True
                    error_row.label(text="⚠ Path display error, please check path format", icon='ERROR')
            else:
                placeholder_row = custom_dir_box.row()
                placeholder_row.label(text="💡 Please select output directory", icon='INFO')
        
        # Multi-resolution settings
        output_box.prop(scene, "mbnl_enable_multi_resolution")
        
        # Multi-resolution selection
        if scene.mbnl_enable_multi_resolution:
            multi_res_box = output_box.box()
            multi_res_box.label(text="Multi-Resolution Configuration:", icon='TEXTURE')
            
            # Preset resolutions
            preset_row = multi_res_box.row()
            preset_row.label(text="Preset Resolutions:")
            res_row = multi_res_box.row(align=True)
            col1 = res_row.column()
            col1.prop(scene, "mbnl_res_512")
            col1.prop(scene, "mbnl_res_2048")
            col2 = res_row.column()
            col2.prop(scene, "mbnl_res_1024")
            col2.prop(scene, "mbnl_res_4096")
            
            # 8K resolution on separate line
            multi_res_box.prop(scene, "mbnl_res_8192")
            
            # Quick selection buttons
            quick_row = multi_res_box.row(align=True)
            quick_row.operator("mbnl.select_res_game", text="Game")
            quick_row.operator("mbnl.select_res_film", text="Film")
            quick_row.operator("mbnl.select_res_all", text="All")
            quick_row.operator("mbnl.select_res_none", text="Clear")
            
            # Custom resolution area
            custom_box = multi_res_box.box()
            custom_box.prop(scene, "mbnl_enable_custom_resolution")
            
            if scene.mbnl_enable_custom_resolution:
                custom_box.label(text="Custom Resolutions:", icon='SETTINGS')
                
                # Custom resolution settings
                for i in range(1, 4):
                    custom_row = custom_box.row(align=True)
                    custom_row.prop(scene, f"mbnl_use_custom_{i}", text="")
                    sub_row = custom_row.row(align=True)
                    sub_row.enabled = getattr(scene, f"mbnl_use_custom_{i}")
                    sub_row.prop(scene, f"mbnl_custom_width_{i}", text="Width")
                    sub_row.prop(scene, f"mbnl_custom_height_{i}", text="Height")
                
                # Common resolution quick buttons
                preset_box = custom_box.box()
                preset_box.label(text="Common Resolutions:")
                
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
                clear_row.operator("mbnl.clear_custom_res", text="Clear Custom", icon='X')
            
            # Show selected resolutions
            selected_preset = []
            selected_custom = []
            
            # Preset resolutions
            if scene.mbnl_res_512: selected_preset.append("512")
            if scene.mbnl_res_1024: selected_preset.append("1024")
            if scene.mbnl_res_2048: selected_preset.append("2048")
            if scene.mbnl_res_4096: selected_preset.append("4096")
            if scene.mbnl_res_8192: selected_preset.append("8192")
            
            # Custom resolutions
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
                custom_info = [f'{r}×{r}(custom)' if r.isdigit() else f'{r}(custom)' for r in selected_custom]
                display_info = preset_info + custom_info
                
                summary_box.label(text=f"✓ Export resolutions: {', '.join(display_info)}", icon='CHECKMARK')
                
                # Performance tip
                if len(all_selected) > 2 or any(int(r.split('x')[0]) >= 4096 for r in all_selected):
                    summary_box.label(text="💡 High resolution/multi-resolution baking takes longer", icon='INFO')
            else:
                warning_box = multi_res_box.box()
                warning_box.alert = True
                warning_box.label(text="⚠ Please select at least one resolution", icon='ERROR')

        layout.separator()

        # =============================================================================
        # 5. Channel Selection
        # =============================================================================
        channels_box = layout.box()
        channels_box.label(text="🎨 Channel Selection", icon='MATERIAL')
        
        # Quick selection buttons
        quick_box = channels_box.box()
        quick_box.label(text="Quick Selection:", icon='PRESET')
        quick_row1 = quick_box.row(align=True)
        quick_row1.operator("mbnl.select_basic", text="Basic PBR")
        quick_row1.operator("mbnl.select_full", text="Full PBR")
        quick_row1.operator("mbnl.select_none", text="Deselect All")
        
        quick_row2 = quick_box.row(align=True)
        quick_row2.operator("mbnl.select_custom_shader", text="Custom Shader Only")
        quick_row2.operator("mbnl.diagnose_custom_shader", text="Diagnose", icon='CONSOLE')

        # Basic PBR channels
        basic_box = channels_box.box()
        basic_box.label(text="Basic PBR Channels:", icon='MATERIAL_DATA')
        basic_row = basic_box.row(align=True)
        basic_col1 = basic_row.column()
        basic_col1.prop(scene, "mbnl_include_basecolor")
        basic_col1.prop(scene, "mbnl_include_metallic")
        basic_col2 = basic_row.column()
        basic_col2.prop(scene, "mbnl_include_roughness")
        basic_col2.prop(scene, "mbnl_include_normal")

        # Advanced PBR channels
        advanced_box = channels_box.box()
        advanced_box.label(text="Advanced PBR Channels:", icon='NODE_MATERIAL')
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

        # Special channels
        special_box = channels_box.box()
        special_box.label(text="Special Channels:", icon='MODIFIER_DATA')
        special_row = special_box.row(align=True)
        special_row.prop(scene, "mbnl_include_displacement")
        special_row.prop(scene, "mbnl_include_ambient_occlusion")
        
        # Custom shader baking
        custom_shader_box = channels_box.box()
        custom_shader_box.label(text="Custom Shaders:", icon='NODE_MATERIAL')
        custom_shader_box.prop(scene, "mbnl_include_custom_shader")
        
        # Custom shader explanation
        if scene.mbnl_include_custom_shader:
            info_box = custom_shader_box.box()
            info_box.label(text="💡 Custom Shader Baking Info:", icon='INFO')
            info_box.label(text="• Bakes shader currently connected to Material Output")
            info_box.label(text="• Supports all types of shader nodes and node groups")
            info_box.label(text="• Includes Diffuse, Glossy, Emission, node groups, etc.")
            info_box.label(text="• Baking result is the final color output of the shader")
            
            # Mixed shader strategy selection
            mixed_count = (material_stats.get('mixed_shader_network', 0) + 
                          material_stats.get('principled_with_custom', 0) + 
                          material_stats.get('custom_with_principled', 0))
            
            if mixed_count > 0:
                strategy_box = custom_shader_box.box()
                strategy_box.label(text="🔀 Mixed Shader Strategy:", icon='NODE_MATERIAL')
                strategy_box.label(text=f"Detected {mixed_count} mixed shader materials")
                strategy_box.prop(scene, "mbnl_mixed_shader_strategy", text="Processing Strategy")
                
                # Strategy explanation
                strategy_info = strategy_box.box()
                if scene.mbnl_mixed_shader_strategy == 'SURFACE_OUTPUT':
                    strategy_info.label(text="✓ Full Surface Output: Bake final mixed result (recommended)", icon='CHECKMARK')
                elif scene.mbnl_mixed_shader_strategy == 'PRINCIPLED_ONLY':
                    strategy_info.label(text="⚠ Principled BSDF Only: Ignore custom shader parts", icon='ERROR')
                elif scene.mbnl_mixed_shader_strategy == 'CUSTOM_ONLY':
                    strategy_info.label(text="🧪 Custom Shader Only: Experimental feature, may be unstable", icon='EXPERIMENTAL')
            
            # Special notes for node groups
            warning_box = info_box.box()
            warning_box.label(text="🔧 Node Group Optimization:", icon='NODE_MATERIAL')
            warning_box.label(text="• Intelligently detects node group Shader/BSDF/Color outputs")
            warning_box.label(text="• Auto-resolves node group output connection issues")
            warning_box.label(text="• Detailed baking process logs help with debugging")

        layout.separator()

        # =============================================================================
        # 6. Material Atlas Baking
        # =============================================================================
        atlas_box = layout.box()
        atlas_box.label(text="🎯 Material Atlas Baking", icon='TEXTURE')
        
        # Check if suitable for atlas baking
        atlas_eligible = False
        if selected_objects and len(selected_objects) == 1:
            obj = selected_objects[0]
            material_count = len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
            if material_count >= 2:
                atlas_eligible = True
                safe_obj_name = safe_encode_text(obj.name, "Unnamed Object")
                atlas_box.label(text=f"✓ Object: {safe_obj_name} ({material_count} materials)", icon='CHECKMARK')
            else:
                atlas_box.label(text="Requires at least 2 material slots", icon='INFO')
        else:
            if len(selected_objects) > 1:
                atlas_box.label(text="Atlas baking only supports single object", icon='INFO')
            else:
                atlas_box.label(text="Please select an object", icon='INFO')
        
        if atlas_eligible:
            # Atlas settings
            atlas_settings_box = atlas_box.box()
            atlas_settings_box.label(text="Atlas Settings:", icon='SETTINGS')
            
            atlas_settings_box.prop(scene, "mbnl_atlas_layout_mode")
            
            if scene.mbnl_atlas_layout_mode == 'AUTO':
                obj = selected_objects[0]
                material_count = len([slot for slot in obj.material_slots if slot.material and slot.material.use_nodes])
                auto_cols, auto_rows = calculate_atlas_layout(material_count)
                atlas_settings_box.label(text=f"Auto Layout: {auto_cols}×{auto_rows}", icon='AUTO')
            else:
                manual_row = atlas_settings_box.row(align=True)
                manual_row.prop(scene, "mbnl_atlas_cols")
                manual_row.prop(scene, "mbnl_atlas_rows")
            
            atlas_settings_box.prop(scene, "mbnl_atlas_padding")
            atlas_settings_box.prop(scene, "mbnl_atlas_update_uv")
            
            # Channel selection
            atlas_channels_box = atlas_box.box()
            atlas_channels_box.label(text="Atlas Channels:", icon='MATERIAL')
            
            atlas_row = atlas_channels_box.row(align=True)
            atlas_col1 = atlas_row.column()
            atlas_col1.prop(scene, "mbnl_atlas_include_basecolor", text="Base Color")
            atlas_col1.prop(scene, "mbnl_atlas_include_metallic", text="Metallic")
            atlas_col2 = atlas_row.column()
            atlas_col2.prop(scene, "mbnl_atlas_include_roughness", text="Roughness")
            atlas_col2.prop(scene, "mbnl_atlas_include_normal", text="Normal")
            
            # Atlas baking button
            atlas_button_row = atlas_box.row()
            atlas_button_row.scale_y = 1.3
            
            atlas_op = atlas_button_row.operator("mbnl.bake_material_atlas", text="🎯 Bake Material Atlas", icon='TEXTURE')
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
            
            # Atlas explanation
            atlas_info_box = atlas_box.box()
            atlas_info_box.label(text="💡 Atlas Baking Info:", icon='INFO')
            atlas_info_box.label(text="• Merges multiple material slots into one texture")
            atlas_info_box.label(text="• Automatically remaps UV coordinates")
            atlas_info_box.label(text="• Suitable for game optimization and reducing Draw Calls")
            atlas_info_box.label(text="• Each material occupies one area of the atlas")
        else:
            atlas_box.enabled = False

        layout.separator()

        # =============================================================================
        # 7. Regular Baking Execution
        # =============================================================================
        bake_box = layout.box()
        bake_box.label(text="🚀 Start Baking", icon='RENDER_RESULT')
        
        # Check if baking can be executed
        can_bake = True
        bake_issues = []
        
        if not selected_objects:
            can_bake = False
            bake_issues.append("No objects selected")
        elif total_materials == 0:
            can_bake = False
            bake_issues.append("No available materials")
        
        # Check channel selection
        selected_channels = []
        if scene.mbnl_include_basecolor: selected_channels.append("Base Color")
        if scene.mbnl_include_roughness: selected_channels.append("Roughness")
        if scene.mbnl_include_metallic: selected_channels.append("Metallic")
        if scene.mbnl_include_normal: selected_channels.append("Normal")
        if scene.mbnl_include_subsurface: selected_channels.append("Subsurface")
        if scene.mbnl_include_transmission: selected_channels.append("Transmission")
        if scene.mbnl_include_emission: selected_channels.append("Emission")
        if scene.mbnl_include_alpha: selected_channels.append("Alpha")
        if scene.mbnl_include_specular: selected_channels.append("Specular")
        if scene.mbnl_include_clearcoat: selected_channels.append("Clearcoat")
        if scene.mbnl_include_clearcoat_roughness: selected_channels.append("Clearcoat Roughness")
        if scene.mbnl_include_sheen: selected_channels.append("Sheen")
        if scene.mbnl_include_displacement: selected_channels.append("Displacement")
        if scene.mbnl_include_ambient_occlusion: selected_channels.append("AO")
        if scene.mbnl_include_custom_shader: selected_channels.append("Custom Shader")
        
        if not selected_channels:
            can_bake = False
            bake_issues.append("No channels selected")
        
        # Check multi-resolution settings
        if scene.mbnl_enable_multi_resolution:
            has_resolution = (scene.mbnl_res_512 or scene.mbnl_res_1024 or scene.mbnl_res_2048 or 
                             scene.mbnl_res_4096 or scene.mbnl_res_8192)
            if scene.mbnl_enable_custom_resolution:
                has_resolution = has_resolution or scene.mbnl_use_custom_1 or scene.mbnl_use_custom_2 or scene.mbnl_use_custom_3
            
            if not has_resolution:
                can_bake = False
                bake_issues.append("Multi-resolution enabled but no resolutions selected")
        
        # Show baking readiness status
        if can_bake:
            status_box = bake_box.box()
            status_box.label(text="✓ Ready to Bake", icon='CHECKMARK')
            
            # Show baking summary
            if selected_channels:
                if len(selected_channels) <= 6:
                    status_box.label(text=f"Channels: {', '.join(selected_channels)}")
                else:
                    status_box.label(text=f"Channels: {len(selected_channels)} selected")
            
            if scene.mbnl_enable_multi_resolution:
                res_count = sum([scene.mbnl_res_512, scene.mbnl_res_1024, scene.mbnl_res_2048, 
                               scene.mbnl_res_4096, scene.mbnl_res_8192])
                if scene.mbnl_enable_custom_resolution:
                    res_count += sum([scene.mbnl_use_custom_1, scene.mbnl_use_custom_2, scene.mbnl_use_custom_3])
                status_box.label(text=f"Resolutions: {res_count}")
            else:
                status_box.label(text=f"Resolution: {scene.mbnl_resolution}×{scene.mbnl_resolution}")
        else:
            issues_box = bake_box.box()
            issues_box.alert = True
            issues_box.label(text="⚠ Cannot Execute Baking", icon='ERROR')
            for issue in bake_issues:
                issues_box.label(text=f"• {issue}")
        
        # Baking button
        button_layout = bake_box
        button_layout.enabled = can_bake
        
        button_row = button_layout.row()
        button_row.scale_y = 1.5  # Make button more prominent
        
        op = button_row.operator(MBNL_OT_bake.bl_idname, text="🎯 Start PBR Texture Baking", icon='RENDER_RESULT')
        
        # Set operator parameters
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
            tips_box.label(text="💡 Usage Tips:", icon='INFO')
            tips_box.label(text="• Do not operate other functions during baking")
            tips_box.label(text="• High resolution baking may take a long time")
            tips_box.label(text="• Baking progress will be shown in console")

        layout.separator()

        # =============================================================================
        # 8. UDIM Support
        # =============================================================================
        udim_box = layout.box()
        udim_box.label(text="🔸 UDIM Tile Baking", icon='UV')
        
        # UDIM basic settings
        udim_box.prop(scene, "mbnl_enable_udim")
        
        if scene.mbnl_enable_udim:
            udim_settings_box = udim_box.box()
            udim_settings_box.label(text="UDIM Settings:", icon='SETTINGS')
            
            udim_settings_box.prop(scene, "mbnl_udim_auto_detect")
            
            if not scene.mbnl_udim_auto_detect:
                # Manual range settings
                range_row = udim_settings_box.row(align=True)
                range_row.prop(scene, "mbnl_udim_range_start", text="Start")
                range_row.prop(scene, "mbnl_udim_range_end", text="End")
                
                # Show range information
                tile_count = scene.mbnl_udim_range_end - scene.mbnl_udim_range_start + 1
                range_info = udim_settings_box.row()
                range_info.label(text=f"Will bake {tile_count} UDIM tiles")
            
            udim_settings_box.prop(scene, "mbnl_udim_naming_mode")
            
            # UDIM naming examples
            naming_example_box = udim_settings_box.box()
            naming_example_box.label(text="File Naming Examples:", icon='FILE_TEXT')
            
            if scene.mbnl_udim_naming_mode == 'STANDARD':
                naming_example_box.label(text="material.1001.basecolor.png")
            elif scene.mbnl_udim_naming_mode == 'MARI':
                naming_example_box.label(text="material_1001_basecolor.png")
            elif scene.mbnl_udim_naming_mode == 'MUDBOX':
                naming_example_box.label(text="material.basecolor.1001.png")
            
            # UDIM detection preview
            if selected_objects and len(selected_objects) == 1:
                obj = selected_objects[0]
                if obj.data.uv_layers:
                    detected_tiles = detect_udim_tiles(obj)
                    
                    if detected_tiles:
                        preview_box = udim_box.box()
                        preview_box.label(text="✓ Detected UDIM Tiles:", icon='CHECKMARK')
                        
                        # Show detected tiles
                        tiles_text = ", ".join(str(tile) for tile in detected_tiles[:10])  # Show maximum 10
                        if len(detected_tiles) > 10:
                            tiles_text += f" ... (total {len(detected_tiles)})"
                        
                        preview_box.label(text=tiles_text)
                        
                        # Tip information
                        info_row = preview_box.row()
                        info_row.label(text=f"💡 Will generate independent textures for each tile", icon='INFO')
                    else:
                        warning_box = udim_box.box()
                        warning_box.alert = True
                        warning_box.label(text="⚠ No UDIM tiles detected", icon='ERROR')
                        warning_box.label(text="Model may use standard 0-1 UV layout")
                else:
                    warning_box = udim_box.box()
                    warning_box.alert = True
                    warning_box.label(text="⚠ Object has no UV layers", icon='ERROR')
            else:
                info_box = udim_box.box()
                info_box.label(text="💡 Please select single object to preview UDIM tiles", icon='INFO')
        else:
            udim_box.label(text="UDIM support disabled, will use standard UV baking")

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
            self.report({'WARNING'}, "Please select at least one mesh object")
            return {'CANCELLED'}
        
        if len(selected_objects) > 1:
            self.report({'WARNING'}, "Material atlas feature can only process one object at a time")
            return {'CANCELLED'}
        
        obj = selected_objects[0]
        
        # 检查材质槽
        material_slots = [slot for slot in obj.material_slots if slot.material and slot.material.use_nodes]
        if len(material_slots) < 2:
            self.report({'WARNING'}, "Object must have at least 2 material slots to create atlas")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"Starting to create atlas for '{obj.name}' with {len(material_slots)} materials")
        
        # 确定图集布局
        if self.atlas_layout_mode == 'AUTO':
            cols, rows = calculate_atlas_layout(len(material_slots))
        else:
            cols, rows = self.atlas_cols, self.atlas_rows
        
        if cols * rows < len(material_slots):
            self.report({'WARNING'}, f"Atlas layout {cols}×{rows} cannot accommodate {len(material_slots)} materials")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"Using atlas layout: {cols}×{rows}")
        
        # 保存原始UV层名称
        original_uv_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
        
        try:
            # 创建图集UV映射
            if self.atlas_update_uv:
                atlas_uv_name = create_atlas_uv_layer(obj, material_slots, (cols, rows), self.atlas_padding)
                self.report({'INFO'}, f"Created atlas UV layer: {atlas_uv_name}")
            
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
                        self.report({'ERROR'}, f"Baking material {mat.name} failed: {str(e)}")
                    
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
                
                self.report({'INFO'}, f"Saved atlas {suffix}: {img_path}")
            
            self.report({'INFO'}, f"Material atlas baking completed!")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Material atlas baking failed: {str(e)}")
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
        print(f"UDIM area setting error: {e}")
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
        print(f"UDIM UV normalization error: {e}")
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
        print(f"UDIM UV restoration error: {e}")
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

    # Scene properties
    bpy.types.Scene.mbnl_replace_nodes = BoolProperty(
        name="Replace Material Nodes",
        description="After baking, rebuild material using baked textures.",
        default=False,
    )
    bpy.types.Scene.mbnl_resolution = IntProperty(
        name="Resolution",
        description="Baking texture resolution (pixels)",
        default=2048,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_include_lighting = BoolProperty(
        name="Include Lighting",
        description="Include scene lighting information when baking, affects base color and other channels",
        default=False,
    )
    bpy.types.Scene.mbnl_lighting_shadow_mode = EnumProperty(
        name="Shadow Mode",
        description="Shadow handling mode for lighting baking",
        items=[
            ('WITH_SHADOWS', 'With Shadows', 'Include shadows in lighting baking (complete lighting)'),
            ('NO_SHADOWS', 'No Shadows', 'Exclude shadows, only include direct lighting without shadows'),
        ],
        default='WITH_SHADOWS'
    )
    bpy.types.Scene.mbnl_organize_folders = BoolProperty(
        name="Organize Folders",
        description="Create folders for each object/material/resolution, better organize output files",
        default=True,
    )
    bpy.types.Scene.mbnl_use_custom_directory = BoolProperty(
        name="Custom Output Directory",
        description="Use custom directory to save baked images",
        default=False,
    )
    bpy.types.Scene.mbnl_custom_directory = StringProperty(
        name="Custom Directory Path",
        description="Select custom output directory path",
        default="",
        subtype="DIR_PATH",
    )
    
    # Preset management
    bpy.types.Scene.mbnl_preset_list = EnumProperty(
        name="Preset List",
        description="Available baking presets",
        items=update_presets_enum,
        default=0
    )
    
    # Multi-resolution support
    bpy.types.Scene.mbnl_enable_multi_resolution = BoolProperty(
        name="Multi-Resolution Export",
        description="Export textures at multiple resolutions simultaneously",
        default=False,
    )
    bpy.types.Scene.mbnl_res_512 = BoolProperty(name="512×512", default=False)
    bpy.types.Scene.mbnl_res_1024 = BoolProperty(name="1024×1024", default=True)
    bpy.types.Scene.mbnl_res_2048 = BoolProperty(name="2048×2048", default=True)
    bpy.types.Scene.mbnl_res_4096 = BoolProperty(name="4096×4096", default=False)
    bpy.types.Scene.mbnl_res_8192 = BoolProperty(name="8192×8192", default=False)
    
    # Custom resolution support (supports rectangular)
    bpy.types.Scene.mbnl_enable_custom_resolution = BoolProperty(
        name="Custom Resolution",
        description="Enable custom resolution input",
        default=False,
    )
    bpy.types.Scene.mbnl_custom_width_1 = IntProperty(
        name="Width 1",
        description="First custom resolution width",
        default=1536,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_height_1 = IntProperty(
        name="Height 1",
        description="First custom resolution height",
        default=1536,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_width_2 = IntProperty(
        name="Width 2",
        description="Second custom resolution width",
        default=1920,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_height_2 = IntProperty(
        name="Height 2",
        description="Second custom resolution height",
        default=1080,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_width_3 = IntProperty(
        name="Width 3",
        description="Third custom resolution width",
        default=1280,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_custom_height_3 = IntProperty(
        name="Height 3",
        description="Third custom resolution height",
        default=720,
        min=16,
        max=16384,
    )
    bpy.types.Scene.mbnl_use_custom_1 = BoolProperty(name="Enable Custom 1", default=False)
    bpy.types.Scene.mbnl_use_custom_2 = BoolProperty(name="Enable Custom 2", default=False)
    bpy.types.Scene.mbnl_use_custom_3 = BoolProperty(name="Enable Custom 3", default=False)
    
    # Basic PBR channels
    bpy.types.Scene.mbnl_include_basecolor = BoolProperty(name="Base Color", default=True)
    bpy.types.Scene.mbnl_include_roughness = BoolProperty(name="Roughness", default=True)
    bpy.types.Scene.mbnl_include_metallic = BoolProperty(name="Metallic", default=True)
    bpy.types.Scene.mbnl_include_normal = BoolProperty(name="Normal", default=True)
    
    # Advanced PBR channels
    bpy.types.Scene.mbnl_include_subsurface = BoolProperty(name="Subsurface", default=False)
    bpy.types.Scene.mbnl_include_transmission = BoolProperty(name="Transmission", default=False)
    bpy.types.Scene.mbnl_include_emission = BoolProperty(name="Emission", default=False)
    bpy.types.Scene.mbnl_include_alpha = BoolProperty(name="Alpha", default=False)
    bpy.types.Scene.mbnl_include_specular = BoolProperty(name="Specular", default=False)
    bpy.types.Scene.mbnl_include_clearcoat = BoolProperty(name="Clearcoat", default=False)
    bpy.types.Scene.mbnl_include_clearcoat_roughness = BoolProperty(name="Clearcoat Roughness", default=False)
    bpy.types.Scene.mbnl_include_sheen = BoolProperty(name="Sheen", default=False)
    
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
        name="Material Atlas Merge",
        description="Merge multiple material slots into a single texture",
        default=False
    )
    bpy.types.Scene.mbnl_atlas_layout_mode = EnumProperty(
        name="Atlas Layout",
        description="Atlas layout mode",
        items=[
            ('AUTO', 'Auto Layout', 'Automatically calculate the best layout'),
            ('MANUAL', 'Manual Layout', 'Manually specify the number of columns and rows'),
        ],
        default='AUTO'
    )
    bpy.types.Scene.mbnl_atlas_cols = IntProperty(
        name="Columns",
        description="Number of columns in the atlas",
        default=2,
        min=1,
        max=8
    )
    bpy.types.Scene.mbnl_atlas_rows = IntProperty(
        name="Rows", 
        description="Number of rows in the atlas",
        default=2,
        min=1,
        max=8
    )
    bpy.types.Scene.mbnl_atlas_padding = FloatProperty(
        name="Padding",
        description="Padding between materials (UV space)",
        default=0.02,
        min=0.0,
        max=0.1
    )
    bpy.types.Scene.mbnl_atlas_update_uv = BoolProperty(
        name="Update UV Mapping",
        description="Create a new UV mapping for the atlas",
        default=True
    )
    bpy.types.Scene.mbnl_atlas_include_basecolor = BoolProperty(name="基础色", default=True)
    bpy.types.Scene.mbnl_atlas_include_roughness = BoolProperty(name="粗糙度", default=True)
    bpy.types.Scene.mbnl_atlas_include_metallic = BoolProperty(name="金属度", default=True)
    bpy.types.Scene.mbnl_atlas_include_normal = BoolProperty(name="法线", default=True)
    
    # UDIM支持属性
    bpy.types.Scene.mbnl_enable_udim = BoolProperty(
        name="UDIM Support",
        description="Enable UDIM tile baking, generate independent textures for each UDIM tile",
        default=False
    )
    bpy.types.Scene.mbnl_udim_auto_detect = BoolProperty(
        name="Auto Detect UDIM",
        description="Automatically detect UDIM tiles used by the model",
        default=True
    )
    bpy.types.Scene.mbnl_udim_range_start = IntProperty(
        name="UDIM Start",
        description="UDIM tile range start number",
        default=1001,
        min=1001,
        max=1100
    )
    bpy.types.Scene.mbnl_udim_range_end = IntProperty(
        name="UDIM End",
        description="UDIM tile range end number",
        default=1010,
        min=1001,
        max=1100
    )
    bpy.types.Scene.mbnl_udim_naming_mode = EnumProperty(
        name="UDIM Naming Mode",
        description="Naming mode for UDIM files",
        items=[
            ('STANDARD', 'Standard Mode', 'Material name.1001.Channel name.png'),
            ('MARI', 'Mari Mode', 'Material name_1001_Channel name.png'),
            ('MUDBOX', 'Mudbox Mode', 'Material name.Channel name.1001.png'),
        ],
        default='STANDARD'
    )
    
    # Color Space Management Properties
    bpy.types.Scene.mbnl_colorspace_mode = EnumProperty(
        name="Color Space Mode",
        description="How to handle color space assignments",
        items=[
            ('AUTO', 'Auto Detection', 'Automatically assign appropriate color spaces based on channel type'),
            ('CUSTOM', 'Custom Settings', 'Use custom color space settings for each channel type'),
            ('MANUAL', 'Manual Override', 'Manually override color space for all textures'),
        ],
        default='AUTO'
    )
    
    bpy.types.Scene.mbnl_colorspace_basecolor = EnumProperty(
        name="Base Color",
        description="Color space for Base Color/Diffuse textures",
        items=[
            ('sRGB', 'sRGB', 'Standard sRGB color space (gamma corrected)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB color space'),
            ('Non-Color', 'Non-Color', 'Non-color data'),
            ('ACEScg', 'ACEScg', 'ACES working color space'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020 color space'),
        ],
        default='sRGB'
    )
    
    bpy.types.Scene.mbnl_colorspace_normal = EnumProperty(
        name="Normal Maps",
        description="Color space for Normal Map textures",
        items=[
            ('Non-Color', 'Non-Color', 'Non-color data (recommended for normal maps)'),
            ('sRGB', 'sRGB', 'sRGB color space'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Raw', 'Raw', 'Raw color data'),
        ],
        default='Non-Color'
    )
    
    bpy.types.Scene.mbnl_colorspace_roughness = EnumProperty(
        name="Roughness/Metallic",
        description="Color space for Roughness, Metallic and other data textures",
        items=[
            ('Non-Color', 'Non-Color', 'Non-color data (recommended for data maps)'),
            ('sRGB', 'sRGB', 'sRGB color space'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Raw', 'Raw', 'Raw color data'),
        ],
        default='Non-Color'
    )
    
    bpy.types.Scene.mbnl_colorspace_emission = EnumProperty(
        name="Emission",
        description="Color space for Emission textures",
        items=[
            ('sRGB', 'sRGB', 'sRGB color space (recommended for emission)'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB color space'),
            ('ACEScg', 'ACEScg', 'ACES working color space'),
            ('Non-Color', 'Non-Color', 'Non-color data'),
        ],
        default='sRGB'
    )
    
    bpy.types.Scene.mbnl_colorspace_manual_override = EnumProperty(
        name="Manual Override",
        description="Color space to use for all textures when using manual override mode",
        items=[
            ('sRGB', 'sRGB', 'sRGB color space'),
            ('Non-Color', 'Non-Color', 'Non-color data'),
            ('Linear Rec.709', 'Linear Rec.709', 'Linear Rec.709 color space'),
            ('Linear sRGB', 'Linear sRGB', 'Linear sRGB color space'),
            ('ACEScg', 'ACEScg', 'ACES working color space'),
            ('Rec.2020', 'Rec.2020', 'ITU-R BT.2020 color space'),
            ('Raw', 'Raw', 'Raw color data'),
            ('XYZ', 'XYZ', 'CIE XYZ color space'),
        ],
        default='sRGB'
    )


def unregister():
    props = [
        "mbnl_replace_nodes",
        "mbnl_resolution",
        "mbnl_include_lighting",
        "mbnl_lighting_shadow_mode",
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
