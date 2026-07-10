import os
import bpy
import bmesh
import colorsys
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.props import *


bl_info = {
    "name":         "ob2blender",
    "author":       "stone-temple-pilot",
    "blender": (4, 5, 2),
    "version": (2, 0, 0),
    "location":     "File > Import-Export",
    "description":  "Import and Export Runescape 3D model format (.ob2)",
    "category":     "Import-Export",
}

# Module reloading logic for proper addon updates
if "bpy" in locals():
    import importlib
    if "import_model" in locals():
        importlib.reload(import_model)
    if "export_model" in locals():
        importlib.reload(export_model)
    if "byte_buffer" in locals():
        importlib.reload(byte_buffer)
    if "runescape_mesh" in locals():
        importlib.reload(runescape_mesh)
    if "pmn_uv" in locals():
        importlib.reload(pmn_uv)

from . import import_model, export_model, byte_buffer, runescape_mesh, pmn_uv

from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.types import Operator

# ################################################################
# Import / Export Model
# ################################################################

class ImportOB2(Operator, ImportHelper):
    bl_idname = "import.model"
    bl_label = "Import Model"
    bl_description = "Import Runescape .ob2 model file(s). Maximum of 50."

    filename_ext = ".ob2"
    filter_glob = StringProperty(default="*.ob2", options={"HIDDEN"})
    
    # Enable multiple file selection
    files: bpy.props.CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'}
    ) # type: ignore
    
    directory: StringProperty(
        subtype='DIR_PATH',
        options={'HIDDEN', 'SKIP_SAVE'}
    ) # type: ignore
    
    def execute(self, context):
        import_count = 0
        failed_count = 0
        
        # If multiple files selected, use files collection
        if self.files:
            # Limit to 50 files maximum to prevent catastrophe
            files_to_import = self.files[:50]  # Take only first 50
            if len(self.files) > 50:
                self.report({'WARNING'}, f"Selected {len(self.files)} files. Importing first 50 only.")
            
            for file_elem in files_to_import:
                filepath = os.path.join(self.directory, file_elem.name)
                try:
                    mesh = import_model.read_mesh(filepath)
                    import_model.create_blender_mesh(mesh, filepath)
                    import_count += 1
                except Exception as e:
                    print(f"Failed to import {file_elem.name}: {e}")
                    failed_count += 1
        else:
            # Single file selection (fallback)
            try:
                mesh = import_model.read_mesh(self.filepath)
                import_model.create_blender_mesh(mesh, self.filepath)
                import_count += 1
            except Exception as e:
                print(f"Failed to import: {e}")
                failed_count += 1
        
        if import_count > 0:
            bpy.ops.ed.undo_push(message=f"Import {import_count} model(s)")
            self.report({'INFO'}, f"Imported {import_count} model(s)")
        
        if failed_count > 0:
            self.report({'WARNING'}, f"{failed_count} file(s) failed to import")
        
        return {'FINISHED'}

class ExportOB2Confirm(Operator):
    """Confirmation dialog for overwriting existing .ob2 files"""
    bl_idname = "export.model_confirm"
    bl_label = "Overwrite Existing Files?"
    bl_options = {'INTERNAL'}

    directory: StringProperty(options={'HIDDEN'}) # type: ignore

    _files = []

    def execute(self, context):
        export_model.export_to_ob2(self.directory, False)
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        total = len(ExportOB2Confirm._files)
        layout.label(text="The following files already exist:", icon='ERROR')
        for f in ExportOB2Confirm._files[:50]:
            layout.label(text=f"    {f}")
        if total > 50:
            layout.label(text=f"    ... plus {total - 50} more files")
        layout.separator()
        layout.label(text="Press OK to overwrite, or cancel to abort.")

class ExportOB2(Operator, ExportHelper):
    bl_idname = "export.model"
    bl_label = "Export Model"
    bl_description = "Export selected objects individually as objectname.ob2"

    filename_ext = ".ob2"
    filter_glob = StringProperty( default="*.ob2", options={"HIDDEN"})
    
    def execute( self, context ):
        directory = os.path.dirname(self.filepath)
        self.directory = directory

        # Check for existing files that would be overwritten
        selected_objects = bpy.context.selected_objects
        if selected_objects:
            conflicting = []
            for obj in selected_objects:
                export_path = os.path.join(directory, f"{obj.name}.ob2")
                if os.path.exists(export_path):
                    conflicting.append(f"{obj.name}.ob2")

            if conflicting:
                ExportOB2Confirm._files = conflicting
                bpy.ops.export.model_confirm('INVOKE_DEFAULT', directory=directory)
                return {'FINISHED'}

        export_model.export_to_ob2(self.directory, False)
        return {'FINISHED'}

# ################################################################
# Common
# ################################################################
def menu_func_import( self, context ):
    self.layout.operator(ImportOB2.bl_idname, text="Runescape Model (.ob2)")

def menu_func_export( self, context ):
    self.layout.operator(ExportOB2.bl_idname, text="Runescape Model (.ob2)")


## Timer-based attribute picker - polls selection while any picker is enabled ##
_picker_timer_running = False
_picker_updating_value = False  # Flag to indicate picker is updating the value (not user)
_color_syncing = False  # Prevent recursive updates between packed value and HSL sliders
_pmn_draw_handler = None
_PMN_STABLE_ID_LAYER = "OB2_PMN_STABLE_ID"
_PMN_COLORS = {
    "P": (1.0, 0.2, 0.2),
    "M": (0.2, 0.5, 1.0),
    "N": (0.2, 1.0, 0.2),
}

def _get_addon_preferences(context=None):
    ctx = context if context is not None else bpy.context
    prefs = getattr(ctx, "preferences", None)
    if prefs is None:
        return None

    # Try likely keys across normal/module reload scenarios.
    keys = []
    if __package__:
        keys.append(__package__)
    if __name__:
        keys.append(__name__)
        keys.append(__name__.split(".")[0])
    keys.append("ob2blender")

    for key in keys:
        addon = prefs.addons.get(key)
        if addon is not None and getattr(addon, "preferences", None) is not None:
            return addon.preferences

    return None

def _get_pmn_display_colors(context=None):
    prefs = _get_addon_preferences(context)
    if prefs is None:
        return _PMN_COLORS

    return {
        "P": tuple(float(v) for v in prefs.pmn_color_p),
        "M": tuple(float(v) for v in prefs.pmn_color_m),
        "N": tuple(float(v) for v in prefs.pmn_color_n),
    }

def _iter_available_scenes():
    scenes = getattr(bpy.data, "scenes", None)
    if scenes is None:
        return ()
    return scenes

def _sync_scene_pmn_colors_from_preferences(context=None):
    colors = _get_pmn_display_colors(context)
    for scene in _iter_available_scenes():
        if hasattr(scene, "ob2_pmn_color_p"):
            scene.ob2_pmn_color_p = colors["P"]
        if hasattr(scene, "ob2_pmn_color_m"):
            scene.ob2_pmn_color_m = colors["M"]
        if hasattr(scene, "ob2_pmn_color_n"):
            scene.ob2_pmn_color_n = colors["N"]

def _tag_view3d_redraw_all_windows(context=None):
    ctx = context if context is not None else bpy.context
    wm = getattr(ctx, "window_manager", None)
    if wm is None:
        return

    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

def _update_pmn_preference_colors(self, context):
    _sync_scene_pmn_colors_from_preferences(context)
    _tag_view3d_redraw_all_windows(context)

class OB2_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ if __package__ else __name__

    pmn_color_p: FloatVectorProperty(
        name="PMN P",
        description="Display color for PMN P markers and swatches",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=_PMN_COLORS["P"],
        update=_update_pmn_preference_colors,
    ) # type: ignore

    pmn_color_m: FloatVectorProperty(
        name="PMN M",
        description="Display color for PMN M markers and swatches",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=_PMN_COLORS["M"],
        update=_update_pmn_preference_colors,
    ) # type: ignore

    pmn_color_n: FloatVectorProperty(
        name="PMN N",
        description="Display color for PMN N markers and swatches",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=_PMN_COLORS["N"],
        update=_update_pmn_preference_colors,
    ) # type: ignore

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="PMN Marker Colors")
        col.prop(self, "pmn_color_p")
        col.prop(self, "pmn_color_m")
        col.prop(self, "pmn_color_n")
        col.separator()
        col.operator("ob2.reset_pmn_preference_colors", text="Reset PMN Colors to Defaults")

class OB2_OT_reset_pmn_preference_colors(Operator):
    bl_idname = "ob2.reset_pmn_preference_colors"
    bl_label = "Reset PMN Colors"
    bl_description = "Reset PMN P/M/N preference colors to default values"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prefs = _get_addon_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "Could not access addon preferences")
            return {'CANCELLED'}

        prefs.pmn_color_p = _PMN_COLORS["P"]
        prefs.pmn_color_m = _PMN_COLORS["M"]
        prefs.pmn_color_n = _PMN_COLORS["N"]
        _sync_scene_pmn_colors_from_preferences(context)
        _tag_view3d_redraw_all_windows(context)
        self.report({'INFO'}, "Reset PMN colors to defaults")
        return {'FINISHED'}

def _enable_vertex_index_overlay():
    wm = bpy.context.window_manager
    if wm is None:
        return

    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                overlay = getattr(space, "overlay", None)
                if overlay is None:
                    continue
                if hasattr(overlay, "show_extra_indices"):
                    overlay.show_extra_indices = True
                elif hasattr(overlay, "show_indices"):
                    overlay.show_indices = True
                elif hasattr(overlay, "show_vertex_indices"):
                    overlay.show_vertex_indices = True

def _get_selected_textured_face_pmn_indices(obj, bm):
    bm.faces.ensure_lookup_table()

    candidate = None

    # Prefer active face first so UI readback matches what was just clicked.
    active_face = bm.faces.active
    if active_face is not None and active_face.select:
        candidate = active_face

    if candidate is None and bm.select_history:
        for elem in reversed(bm.select_history):
            if isinstance(elem, bmesh.types.BMFace) and elem.select:
                candidate = elem
                break

    if candidate is None:
        for face in bm.faces:
            if face.select:
                candidate = face
                break

    if candidate is None:
        return None

    pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
    if pmn_layer is None:
        return None

    vec = candidate[pmn_layer]
    return int(round(vec[0])), int(round(vec[1])), int(round(vec[2]))

def _ensure_pmn_layer_in_edit_mesh(obj, bm):
    pmn_layer = bm.faces.layers.float_vector.get("PMN")
    if pmn_layer is not None:
        return pmn_layer

    if not obj or obj.type != 'MESH':
        return None

    mesh_attr = obj.data.attributes.get("PMN")
    if mesh_attr is None or mesh_attr.domain != 'FACE' or mesh_attr.data_type != 'FLOAT_VECTOR':
        return None

    pmn_layer = bm.faces.layers.float_vector.new("PMN")
    bm.faces.ensure_lookup_table()

    face_count = min(len(bm.faces), len(mesh_attr.data))
    for i in range(face_count):
        vec = mesh_attr.data[i].vector
        bm.faces[i][pmn_layer] = (float(vec[0]), float(vec[1]), float(vec[2]))

    return pmn_layer

def _ensure_pmn_stable_ids(bm):
    bm.verts.ensure_lookup_table()
    stable_layer = bm.verts.layers.int.get(_PMN_STABLE_ID_LAYER)
    if stable_layer is None:
        stable_layer = bm.verts.layers.int.new(_PMN_STABLE_ID_LAYER)
        for v in bm.verts:
            v[stable_layer] = int(v.index)
    return stable_layer

def _remap_mesh_pmn_indices_from_stable_ids(obj, bm):
    pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
    if pmn_layer is None:
        return 0

    stable_layer = _ensure_pmn_stable_ids(bm)
    id_to_index = {}
    for v in bm.verts:
        sid = int(v[stable_layer])
        if sid not in id_to_index:
            id_to_index[sid] = int(v.index)

    remapped_count = 0
    for face in bm.faces:
        vec = face[pmn_layer]
        old_p = int(round(vec[0]))
        old_m = int(round(vec[1]))
        old_n = int(round(vec[2]))

        new_p = id_to_index.get(old_p)
        new_m = id_to_index.get(old_m)
        new_n = id_to_index.get(old_n)

        if new_p is None or new_m is None or new_n is None:
            continue

        if old_p != new_p or old_m != new_m or old_n != new_n:
            face[pmn_layer] = (float(new_p), float(new_m), float(new_n))
            remapped_count += 1

    if remapped_count:
        bmesh.update_edit_mesh(obj.data)

    return remapped_count

def _draw_pmn_vertices_overlay():
    context = bpy.context
    if context is None or context.scene is None:
        return

    scene = context.scene
    if not getattr(scene, "ob2_pmn_show_vertices", False):
        return

    obj = context.active_object
    if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return

    mesh = obj.data
    bm = bmesh.from_edit_mesh(mesh)
    bm.verts.ensure_lookup_table()

    max_idx = len(bm.verts) - 1
    indices = [
        int(scene.ob2_pmn_p),
        int(scene.ob2_pmn_m),
        int(scene.ob2_pmn_n),
    ]
    for idx in indices:
        if idx < 0 or idx > max_idx:
            return

    coords = [
        obj.matrix_world @ bm.verts[indices[0]].co,
        obj.matrix_world @ bm.verts[indices[1]].co,
        obj.matrix_world @ bm.verts[indices[2]].co,
    ]
    colors = [
        (*_get_pmn_display_colors(context)["P"], 0.65),
        (*_get_pmn_display_colors(context)["M"], 0.65),
        (*_get_pmn_display_colors(context)["N"], 0.65),
    ]

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('ALWAYS')
    gpu.state.point_size_set(7.0)

    for co, color in zip(coords, colors):
        batch = batch_for_shader(shader, 'POINTS', {"pos": [co]})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    gpu.state.point_size_set(1.0)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')

def _picker_timer():
    """Timer callback that reads selection attributes while pickers are active."""
    global _picker_timer_running
    if not _picker_timer_running:
        return None

    context = bpy.context
    scene = context.scene
    if scene is None:
        return 0.25

    pickers_active = (
        getattr(scene, "ob2_vskin_pick", False)
        or getattr(scene, "ob2_tskin_pick", False)
        or getattr(scene, "ob2_pri_pick", False)
        or getattr(scene, "ob2_alpha_pick", False)
        or getattr(scene, "ob2_pmn_pick", False)
    )

    # Keep polling in face select mode so Selected PMN stays in sync even when
    # live copy to Input is toggled off.
    track_selected_pmn = False
    obj = context.active_object
    if obj and obj.type == 'MESH' and context.mode == 'EDIT_MESH':
        select_mode = context.tool_settings.mesh_select_mode
        track_selected_pmn = bool(select_mode[2])

    if pickers_active or track_selected_pmn:
        _read_active_attributes()
        return 0.1

    return 0.25

def _is_picker_timer_registered():
    is_registered_fn = getattr(bpy.app.timers, "is_registered", None)
    if callable(is_registered_fn):
        try:
            return bool(is_registered_fn(_picker_timer))
        except Exception:
            return False
    return bool(_picker_timer_running)

def _start_picker_timer():
    """Start the picker timer if not already running."""
    global _picker_timer_running
    if (not _picker_timer_running) or (not _is_picker_timer_registered()):
        _picker_timer_running = True
        if not _is_picker_timer_registered():
            bpy.app.timers.register(_picker_timer, first_interval=0.0)

def _read_active_attributes():
    """Read attributes from active vertex/face and update scene labels."""
    global _picker_updating_value
    scene = bpy.context.scene
    obj = bpy.context.active_object

    if not obj or obj.type != 'MESH' or bpy.context.mode != 'EDIT_MESH':
        return

    mesh = obj.data
    bm = bmesh.from_edit_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # Keep PMN indices aligned to current vertex indices after topology operations
    # that reindex vertices (for example duplicate/separate workflows).
    _remap_mesh_pmn_indices_from_stable_ids(obj, bm)

    # Check selection mode: (vert, edge, face)
    select_mode = bpy.context.tool_settings.mesh_select_mode
    vert_mode = select_mode[0]
    face_mode = select_mode[2]

    # Get bmesh attribute layers
    vskin_layer = bm.verts.layers.int.get("VSKIN")
    tskin_layer = bm.faces.layers.int.get("TSKIN")
    pri_layer = bm.faces.layers.int.get("PRI")
    alpha_layer = bm.faces.layers.int.get("ALPHA")

    # Find active vertex
    v_elem = None
    if vert_mode and (getattr(scene, "ob2_vskin_pick", False)):
        if bm.select_history:
            for e in reversed(bm.select_history):
                if isinstance(e, bmesh.types.BMVert):
                    v_elem = e
                    break
        if v_elem is None:
            for vert in bm.verts:
                if vert.select:
                    v_elem = vert
                    break
        
        if v_elem is not None:
            _picker_updating_value = True  # Set flag before updating
            scene.ob2_vskin_label = 0 if not vskin_layer else v_elem[vskin_layer]
            _picker_updating_value = False  # Clear flag after updating
            # 0 (Default) if no vskin attribute, otherwise assign vskin of active vertex.

    # Find active face
    f_elem = None
    if face_mode and (getattr(scene, "ob2_tskin_pick", False)
        or getattr(scene, "ob2_pri_pick", False) or getattr(scene, "ob2_alpha_pick", False)
        or getattr(scene, "ob2_pmn_pick", False)):
        #enable face picking if any face picker is on (3 of them).
        if bm.select_history:
            for e in reversed(bm.select_history):
                if isinstance(e, bmesh.types.BMFace):
                    f_elem = e
                    break
        if f_elem is None:
            for face in bm.faces:
                if face.select:
                    f_elem = face
                    break   

    # Update face attrs from face
    if f_elem is not None:
        _picker_updating_value = True  # Set flag before updating
        if getattr(scene, "ob2_tskin_pick", False):
            scene.ob2_tskin_label = 0 if not tskin_layer else f_elem[tskin_layer]
        if getattr(scene, "ob2_pri_pick", False):
            scene.ob2_pri_label = 0 if not pri_layer else f_elem[pri_layer]
        if getattr(scene, "ob2_alpha_pick", False):
            scene.ob2_alpha_label = 0 if not alpha_layer else f_elem[alpha_layer]
        _picker_updating_value = False  # Clear flag after updating

    # Selected PMN should always track selected faces while in face select mode.
    if face_mode:
        _picker_updating_value = True
        pmn = _selected_textured_face_pmn(bpy.context)
        if pmn is not None:
            scene.ob2_pmn_selected_p = pmn[0]
            scene.ob2_pmn_selected_m = pmn[1]
            scene.ob2_pmn_selected_n = pmn[2]

            # Live toggle controls Selected -> Input copying only.
            if getattr(scene, "ob2_pmn_pick", False):
                scene.ob2_pmn_p = pmn[0]
                scene.ob2_pmn_m = pmn[1]
                scene.ob2_pmn_n = pmn[2]
        else:
            scene.ob2_pmn_selected_p = 0
            scene.ob2_pmn_selected_m = 0
            scene.ob2_pmn_selected_n = 0
        _picker_updating_value = False

    _update_pmn_vectors_from_input(bpy.context)
    
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()

def _update_pmn_vectors_from_input(context):
    scene = context.scene
    obj = context.active_object

    if not obj or obj.type != 'MESH':
        scene.ob2_pmn_vector_p = (0.0, 0.0, 0.0)
        scene.ob2_pmn_vector_m = (0.0, 0.0, 0.0)
        scene.ob2_pmn_vector_n = (0.0, 0.0, 0.0)
        return

    p_idx = int(scene.ob2_pmn_p)
    m_idx = int(scene.ob2_pmn_m)
    n_idx = int(scene.ob2_pmn_n)

    if context.mode == 'EDIT_MESH':
        bm = bmesh.from_edit_mesh(obj.data)
        _set_pmn_vectors_from_indices(scene, obj, bm, p_idx, m_idx, n_idx)
        return

    verts = obj.data.vertices
    max_idx = len(verts) - 1

    def world_coord(index):
        if index < 0 or index > max_idx:
            return (0.0, 0.0, 0.0)
        co = obj.matrix_world @ verts[index].co
        return (float(co.x), float(co.y), float(co.z))

    scene.ob2_pmn_vector_p = world_coord(p_idx)
    scene.ob2_pmn_vector_m = world_coord(m_idx)
    scene.ob2_pmn_vector_n = world_coord(n_idx)

def _selected_textured_face_pmn(context):
    obj = context.active_object
    if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return None

    mesh = obj.data
    bm = bmesh.from_edit_mesh(mesh)
    return _get_selected_textured_face_pmn_indices(obj, bm)

def _set_pmn_vectors_from_indices(scene, obj, bm, p_idx, m_idx, n_idx):
    bm.verts.ensure_lookup_table()
    max_idx = len(bm.verts) - 1

    def world_coord(index):
        if index < 0 or index > max_idx:
            return (0.0, 0.0, 0.0)
        co = obj.matrix_world @ bm.verts[index].co
        return (float(co.x), float(co.y), float(co.z))

    scene.ob2_pmn_vector_p = world_coord(p_idx)
    scene.ob2_pmn_vector_m = world_coord(m_idx)
    scene.ob2_pmn_vector_n = world_coord(n_idx)

def _uvs_from_pmn(mesh, face, p_idx, m_idx, n_idx):
    verts = mesh.vertices
    max_idx = len(verts) - 1
    if p_idx < 0 or m_idx < 0 or n_idx < 0:
        return None
    if p_idx > max_idx or m_idx > max_idx or n_idx > max_idx:
        return None

    a = verts[face.verts[0].index].co
    b = verts[face.verts[1].index].co
    c = verts[face.verts[2].index].co

    p = verts[p_idx].co
    m = verts[m_idx].co
    n = verts[n_idx].co

    return pmn_uv.project_triangle_uv_from_pmn(a, b, c, p, m, n)

## toolbar specific to OB2 operations laid out here ##
class OB2_PT_import_export_panel(bpy.types.Panel):
    bl_label = "Import / Export"
    bl_idname = "OB2_PT_import_export_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OB2"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("import.model", text="Import .ob2")
        col.operator("export.model", text="Export Individual .ob2")


class OB2_PT_attributes_panel(bpy.types.Panel):
    bl_label = "Attributes / Labels"
    bl_idname = "OB2_PT_attributes_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OB2"
    bl_order = 2
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)

        row = col.row(align=True)
        op = row.operator("ob2.add_attribute", text="Add VSKIN")
        op.type = "VSKIN"
        op = row.operator("ob2.add_attribute", text="Add TSKIN")
        op.type = "TSKIN"

        row = col.row(align=True)
        op = row.operator("ob2.add_attribute", text="Add PRI")
        op.type = "PRI"
        op = row.operator("ob2.add_attribute", text="Add ALPHA")
        op.type = "ALPHA"

        col.separator()

        # Vertex foldout header
        vskin_section = col.box()
        row = vskin_section.row(align=True)
        row.prop(
            scene,
            "ob2_vskin_foldout",
            icon='TRIA_DOWN' if scene.ob2_vskin_foldout else 'TRIA_RIGHT',
            emboss=False,
            text="Vertex Label",
        )

        if scene.ob2_vskin_foldout:
            row = vskin_section.row(align=True)
            row.prop(scene, "ob2_vskin_label", text="VSKIN")
            row.prop(scene, "ob2_vskin_pick", text="", icon='EYEDROPPER')
            row = vskin_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Select Only")
            op.type = "VSKIN"
            op.add = False
            op.deselect = False

            op = row.operator("ob2.select_labeled", text="Select More")
            op.type = "VSKIN"
            op.add = True
            op.deselect = False

            row = vskin_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Deselect")
            op.type = "VSKIN"
            op.add = True
            op.deselect = True

            op = row.operator("ob2.apply_label", text="Apply Label")
            op.type = "VSKIN"

        # Face foldout header
        tskin_section = col.box()
        row = tskin_section.row(align=True)
        row.prop(
            scene,
            "ob2_tskin_foldout",
            icon='TRIA_DOWN' if scene.ob2_tskin_foldout else 'TRIA_RIGHT',
            emboss=False,
            text="Face Label",
        )

        if scene.ob2_tskin_foldout:
            row = tskin_section.row(align=True)
            row.prop(scene, "ob2_tskin_label", text="TSKIN")
            row.prop(scene, "ob2_tskin_pick", text="", icon='EYEDROPPER')
            row = tskin_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Select Only")
            op.type = "TSKIN"
            op.add = False
            op.deselect = False

            op = row.operator("ob2.select_labeled", text="Select More")
            op.type = "TSKIN"
            op.add = True
            op.deselect = False

            row = tskin_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Deselect")
            op.type = "TSKIN"
            op.add = True
            op.deselect = True

            op = row.operator("ob2.apply_label", text="Apply Label")
            op.type = "TSKIN"

        pri_section = col.box()
        row = pri_section.row(align=True)
        row.prop(
            scene,
            "ob2_pri_foldout",
            icon='TRIA_DOWN' if scene.ob2_pri_foldout else 'TRIA_RIGHT',
            emboss=False,
            text="Priority Label",
        )

        if scene.ob2_pri_foldout:
            row = pri_section.row(align=True)
            row.prop(scene, "ob2_pri_label", text="PRI")
            row.prop(scene, "ob2_pri_pick", text="", icon='EYEDROPPER')
            row = pri_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Select Only")
            op.type = "PRI"
            op.add = False
            op.deselect = False

            op = row.operator("ob2.select_labeled", text="Select More")
            op.type = "PRI"
            op.add = True
            op.deselect = False

            row = pri_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Deselect")
            op.type = "PRI"
            op.add = True
            op.deselect = True

            op = row.operator("ob2.apply_label", text="Apply Label")
            op.type = "PRI"

        alpha_section = col.box()
        row = alpha_section.row(align=True)
        row.prop(
            scene,
            "ob2_alpha_foldout",
            icon='TRIA_DOWN' if scene.ob2_alpha_foldout else 'TRIA_RIGHT',
            emboss=False,
            text="Alpha Label",
        )
        if scene.ob2_alpha_foldout:
            row = alpha_section.row(align=True)
            row.prop(scene, "ob2_alpha_label", text="ALPHA")
            row.prop(scene, "ob2_alpha_pick", text="", icon='EYEDROPPER')
            row = alpha_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Select Only")
            op.type = "ALPHA"
            op.add = False
            op.deselect = False

            op = row.operator("ob2.select_labeled", text="Select More")
            op.type = "ALPHA"
            op.add = True
            op.deselect = False

            row = alpha_section.row(align=True)

            op = row.operator("ob2.select_labeled", text="Deselect")
            op.type = "ALPHA"
            op.add = True
            op.deselect = True

            op = row.operator("ob2.apply_label", text="Apply Label")
            op.type = "ALPHA"


class OB2_PT_color_panel(bpy.types.Panel):
    bl_label = "Color Picker"
    bl_idname = "OB2_PT_color_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OB2"
    bl_order = 1
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)

        row = col.row(align=True)
        row.prop(scene, "ob2_color_value", text="HSL16 Value")
        row = col.row(align=True)
        row.prop(scene, "ob2_color_rgb15", text="RGB15 Value")
        mat_to_hsl_box = col.box()
        row = mat_to_hsl_box.row(align=True)
        row.scale_y = 1.15
        row.operator("ob2.material_to_hsl16", text="Load Material")
        row = col.row(align=True)
        row.operator("ob2.hsl16_to_material_overwrite", text="Overwrite Sel. Material")
        row = col.row(align=True)
        row.operator("ob2.hsl16_to_material_new", text="Create New Material")
        row = col.row(align=True)
        row.enabled = False
        row.prop(scene, "ob2_color_preview", text="Preview")
        row = col.row(align=True)
        row.prop(scene, "ob2_color_h", text="Hue", slider=True)
        row = col.row(align=True)
        row.prop(scene, "ob2_color_s", text="Saturation", slider=True)
        row = col.row(align=True)
        row.prop(scene, "ob2_color_l", text="Lightness", slider=True)


class OB2_PT_pmn_panel(bpy.types.Panel):
    bl_label = "PMN texturing"
    bl_idname = "OB2_PT_pmn_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OB2"
    bl_order = 3
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)

        pmn_box = col.box()
        row = pmn_box.row(align=True)
        row.operator("ob2.add_pmn_attribute", text="Add PMN Attribute (Active Mesh)")
        row = pmn_box.row(align=True)
        row.prop(scene, "ob2_pmn_show_vertices", text="Highlight PMN In Viewport")
        row = pmn_box.row(align=True)
        row.operator("ob2.pmn_to_uv", text="Refresh UV from PMN")
        row = pmn_box.row(align=True)
        row.prop(scene, "ob2_pmn_pick", text="", icon='EYEDROPPER')
        row.label(text="Live assign Selected -> Input")
        row = pmn_box.row(align=True)
        row.operator("ob2.pmn_copy_selected_to_input", text="Copy Selected -> Input (P/M/N)")
        table = pmn_box.box()
        header = table.row(align=True)
        swatch_header = header.row(align=True)
        swatch_header.scale_x = 0.72
        swatch_header.label(text="")
        selected_header = header.row(align=True)
        selected_header.scale_x = 1.0
        op = selected_header.operator("ob2.pmn_select_triplet_vertices", text="Selected")
        op.source = "SELECTED"
        input_header = header.row(align=True)
        input_header.scale_x = 1.0
        op = input_header.operator("ob2.pmn_select_triplet_vertices", text="Input", depress=True)
        op.source = "INPUT"
        apply_header = header.row(align=True)
        apply_header.scale_x = 1.0
        apply_header.operator("ob2.pmn_apply_to_faces", text="Apply", depress=True)

        row = table.row(align=True)
        swatch = row.row(align=True)
        swatch.scale_x = 0.72
        swatch.enabled = False
        swatch.prop(scene, "ob2_pmn_color_p", text="")
        selected = row.row(align=True)
        selected.scale_x = 1.0
        op = selected.operator("ob2.pmn_select_vertex", text=f"P ({scene.ob2_pmn_selected_p})")
        op.index_name = "P"
        op.use_selected = True
        input_col = row.row(align=True)
        input_col.scale_x = 1.0
        op = input_col.operator("ob2.pmn_select_vertex", text=f"P ({scene.ob2_pmn_p})", depress=True)
        op.index_name = "P"
        op.use_selected = False
        apply_col = row.row(align=True)
        apply_col.scale_x = 1.0
        op = apply_col.operator("ob2.pmn_set_input_from_active_vertex", text="Set", depress=True)
        op.index_name = "P"

        row = table.row(align=True)
        swatch = row.row(align=True)
        swatch.scale_x = 0.72
        swatch.enabled = False
        swatch.prop(scene, "ob2_pmn_color_m", text="")
        selected = row.row(align=True)
        selected.scale_x = 1.0
        op = selected.operator("ob2.pmn_select_vertex", text=f"M ({scene.ob2_pmn_selected_m})")
        op.index_name = "M"
        op.use_selected = True
        input_col = row.row(align=True)
        input_col.scale_x = 1.0
        op = input_col.operator("ob2.pmn_select_vertex", text=f"M ({scene.ob2_pmn_m})", depress=True)
        op.index_name = "M"
        op.use_selected = False
        apply_col = row.row(align=True)
        apply_col.scale_x = 1.0
        op = apply_col.operator("ob2.pmn_set_input_from_active_vertex", text="Set", depress=True)
        op.index_name = "M"

        row = table.row(align=True)
        swatch = row.row(align=True)
        swatch.scale_x = 0.72
        swatch.enabled = False
        swatch.prop(scene, "ob2_pmn_color_n", text="")
        selected = row.row(align=True)
        selected.scale_x = 1.0
        op = selected.operator("ob2.pmn_select_vertex", text=f"N ({scene.ob2_pmn_selected_n})")
        op.index_name = "N"
        op.use_selected = True
        input_col = row.row(align=True)
        input_col.scale_x = 1.0
        op = input_col.operator("ob2.pmn_select_vertex", text=f"N ({scene.ob2_pmn_n})", depress=True)
        op.index_name = "N"
        op.use_selected = False
        apply_col = row.row(align=True)
        apply_col.scale_x = 1.0
        op = apply_col.operator("ob2.pmn_set_input_from_active_vertex", text="Set", depress=True)
        op.index_name = "N"

        row = pmn_box.row(align=True)
        row.operator("ob2.pmn_apply_input_from_three_verts", text="Triangle to PMN Input")

        row = pmn_box.row(align=True)
        row.label(text="Select Faces")
        op = row.operator("ob2.pmn_select_faces_by_pmn", text="Selected")
        op.source = "SELECTED"
        op = row.operator("ob2.pmn_select_faces_by_pmn", text="Input")
        op.source = "INPUT"

        row = pmn_box.row(align=True)
        row.enabled = False
        row.prop(scene, "ob2_pmn_vector_p", text="VectorP")
        row = pmn_box.row(align=True)
        row.enabled = False
        row.prop(scene, "ob2_pmn_vector_m", text="VectorM")
        row = pmn_box.row(align=True)
        row.enabled = False
        row.prop(scene, "ob2_pmn_vector_n", text="VectorN")
        row = pmn_box.row(align=True)
        op = row.operator("ob2.uv_to_pmn", text="PMN from UV")
        op.generate_new = False
        op = row.operator("ob2.uv_to_pmn", text="Generate Vertices from UV")
        op.generate_new = True

        mapping_tools = pmn_box.box()
        row = mapping_tools.row(align=True)
        icon = 'TRIA_DOWN' if scene.ob2_pmn_mapping_tools_foldout else 'TRIA_RIGHT'
        row.prop(scene, "ob2_pmn_mapping_tools_foldout", text="Mapping Tools", emboss=False, icon=icon)

        if scene.ob2_pmn_mapping_tools_foldout:
            body = mapping_tools.column(align=True)
            body.operator("ob2.pmn_assign_quads_from_selected", text="Assign PMN Quads from Selected")
            body.operator("ob2.pmn_assign_triangles_from_selected", text="Assign PMN to Triangles")

            op = body.operator("ob2.pmn_swap_indices_on_selected_faces", text="Swap P and M")
            op.swap_mode = 'P_M'
            op = body.operator("ob2.pmn_swap_indices_on_selected_faces", text="Swap P and N")
            op.swap_mode = 'P_N'
            op = body.operator("ob2.pmn_swap_indices_on_selected_faces", text="Swap M and N")
            op.swap_mode = 'M_N'



class OB2_OT_pmn_select_vertex(Operator):
    bl_idname = "ob2.pmn_select_vertex"
    bl_label = "Select PMN Vertex"
    bl_description = "Select the vertex at this PMN index (Shift-click toggles multi select/deselect)"
    bl_options = {'REGISTER', 'UNDO'}

    index_name: StringProperty(name="Index Name", default="P") # type: ignore
    use_selected: BoolProperty(name="Use Selected Column", default=False) # type: ignore
    shift_toggle: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False) # type: ignore

    def invoke(self, context, event):
        self.shift_toggle = bool(event.shift)
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        # Manual PMN table interactions should disable live Selected->Input updates.
        context.scene.ob2_pmn_pick = False

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        # Ensure clicks operate on vertices, not edges/faces.
        context.tool_settings.mesh_select_mode = (True, False, False)

        scene = context.scene
        if self.index_name == "P":
            idx = scene.ob2_pmn_selected_p if self.use_selected else scene.ob2_pmn_p
        elif self.index_name == "M":
            idx = scene.ob2_pmn_selected_m if self.use_selected else scene.ob2_pmn_m
        elif self.index_name == "N":
            idx = scene.ob2_pmn_selected_n if self.use_selected else scene.ob2_pmn_n
        else:
            self.report({'ERROR'}, f"Unknown index name: {self.index_name}")
            return {'CANCELLED'}

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()

        if idx < 0 or idx >= len(bm.verts):
            self.report({'ERROR'}, f"Vertex index {idx} out of range")
            return {'CANCELLED'}

        target_vert = bm.verts[idx]
        if self.shift_toggle:
            # Shift-click toggles this vertex while keeping existing selection.
            target_vert.select = not target_vert.select
        else:
            for v in bm.verts:
                v.select = False
            target_vert.select = True

        bm.select_flush_mode()
        bmesh.update_edit_mesh(mesh)

        source_name = "Selected" if self.use_selected else "Input"
        action = "Toggled" if self.shift_toggle else "Selected"
        self.report({'INFO'}, f"{action} vertex {idx} ({source_name} PMN {self.index_name})")
        return {'FINISHED'}

class OB2_OT_add_pmn_attribute(Operator):
    bl_idname = "ob2.add_pmn_attribute"
    bl_label = "Add PMN Attribute"
    bl_description = "Create or correct PMN as a FACE FLOAT_VECTOR attribute on the active mesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        start_mode = context.mode
        switched_mode = False
        if start_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
            switched_mode = True

        try:
            mesh = obj.data
            existing = mesh.attributes.get("PMN")
            if existing is not None and existing.domain == 'FACE' and existing.data_type == 'FLOAT_VECTOR':
                self.report({'INFO'}, f"PMN attribute is already correct on '{obj.name}'")
                return {'FINISHED'}

            if existing is not None:
                mesh.attributes.remove(existing)
                status = "Corrected"
            else:
                status = "Created"

            mesh.attributes.new(name="PMN", type='FLOAT_VECTOR', domain='FACE')
            self.report({'INFO'}, f"{status} PMN attribute on '{obj.name}'")
            return {'FINISHED'}
        finally:
            if switched_mode and context.active_object == obj and context.mode == 'OBJECT':
                bpy.ops.object.mode_set(mode='EDIT')

class OB2_OT_pmn_apply_from_selection(Operator):
    bl_idname = "ob2.pmn_apply_from_selection"
    bl_label = "Apply From Selected Vertex"
    bl_description = "Set this PMN index to the selected vertex"
    bl_options = {'REGISTER', 'UNDO'}

    index_name: StringProperty(name="Index Name", default="P") # type: ignore

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()

        # Find selected vertex
        selected_verts = [v for v in bm.verts if v.select]
        if len(selected_verts) == 0:
            self.report({'ERROR'}, "No vertex selected")
            return {'CANCELLED'}
        if len(selected_verts) > 1:
            self.report({'WARNING'}, "Multiple vertices selected, using first")

        vertex_idx = selected_verts[0].index
        scene = context.scene

        if self.index_name == "P":
            scene.ob2_pmn_p = vertex_idx
        elif self.index_name == "M":
            scene.ob2_pmn_m = vertex_idx
        elif self.index_name == "N":
            scene.ob2_pmn_n = vertex_idx
        else:
            self.report({'ERROR'}, f"Unknown index name: {self.index_name}")
            return {'CANCELLED'}

        # Disable the picker to prevent overwriting with active face PMN
        scene.ob2_pmn_pick = False

        self.report({'INFO'}, f"Set PMN {self.index_name} to vertex {vertex_idx}")
        return {'FINISHED'}

class OB2_OT_pmn_set_input_from_active_vertex(Operator):
    bl_idname = "ob2.pmn_set_input_from_active_vertex"
    bl_label = "Set Input PMN From Active Vertex"
    bl_description = "Set Input P/M/N to the active selected vertex index"
    bl_options = {'REGISTER', 'UNDO'}

    index_name: StringProperty(name="Index Name", default="P") # type: ignore

    def execute(self, context):
        context.scene.ob2_pmn_pick = False

        obj = context.active_object
        if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
            return {'FINISHED'}

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()

        active_vert = None
        if bm.select_history:
            for elem in reversed(bm.select_history):
                if isinstance(elem, bmesh.types.BMVert) and elem.select:
                    active_vert = elem
                    break

        if active_vert is None:
            return {'FINISHED'}

        scene = context.scene
        vertex_idx = int(active_vert.index)

        if self.index_name == "P":
            scene.ob2_pmn_p = vertex_idx
        elif self.index_name == "M":
            scene.ob2_pmn_m = vertex_idx
        elif self.index_name == "N":
            scene.ob2_pmn_n = vertex_idx
        else:
            return {'CANCELLED'}

        _update_pmn_vectors_from_input(context)
        bpy.ops.ed.undo_push(message=f"Set PMN Input {self.index_name}={vertex_idx}")
        return {'FINISHED'}

class OB2_OT_pmn_select_triplet_vertices(Operator):
    bl_idname = "ob2.pmn_select_triplet_vertices"
    bl_label = "Select PMN Triplet Vertices"
    bl_description = "Select all vertices for Selected or Input PMN (P, M, N)"
    bl_options = {'REGISTER', 'UNDO'}

    source: EnumProperty(
        name="Source",
        items=(
            ('SELECTED', "Selected", "Use Selected PMN"),
            ('INPUT', "Input", "Use Input PMN"),
        ),
        default='SELECTED',
    ) # type: ignore

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        context.scene.ob2_pmn_pick = False

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        context.tool_settings.mesh_select_mode = (True, False, False)

        scene = context.scene
        if self.source == 'INPUT':
            indices = [
                int(scene.ob2_pmn_p),
                int(scene.ob2_pmn_m),
                int(scene.ob2_pmn_n),
            ]
        else:
            indices = [
                int(scene.ob2_pmn_selected_p),
                int(scene.ob2_pmn_selected_m),
                int(scene.ob2_pmn_selected_n),
            ]

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        max_idx = len(bm.verts) - 1

        unique_indices = []
        seen = set()
        for idx in indices:
            if idx in seen:
                continue
            seen.add(idx)
            unique_indices.append(idx)

        if any(idx < 0 or idx > max_idx for idx in unique_indices):
            self.report({'ERROR'}, f"PMN index out of range for active mesh (0..{max_idx})")
            return {'CANCELLED'}

        for v in bm.verts:
            v.select = False

        bm.select_history.clear()
        for idx in unique_indices:
            v = bm.verts[idx]
            v.select = True
            bm.select_history.add(v)

        bm.select_flush_mode()
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)

        source_name = "Input" if self.source == 'INPUT' else "Selected"
        self.report({'INFO'}, f"Selected {len(unique_indices)} {source_name} PMN vert(s): {tuple(unique_indices)}")
        return {'FINISHED'}

class OB2_OT_pmn_apply_input_from_three_verts(Operator):
    bl_idname = "ob2.pmn_apply_input_from_three_verts"
    bl_label = "Triangle to PMN Input"
    bl_description = "Select 3 vertices in click order and press Space to set Input P/M/N"
    bl_options = {'REGISTER', 'UNDO'}

    def _read_ordered_selected_verts(self, bm):
        selected_verts = []
        seen = set()
        for elem in bm.select_history:
            if isinstance(elem, bmesh.types.BMVert) and elem.select and elem.index not in seen:
                selected_verts.append(elem)
                seen.add(elem.index)

        if len(selected_verts) < 3:
            for v in bm.verts:
                if v.select and v.index not in seen:
                    selected_verts.append(v)
                    seen.add(v.index)

        return selected_verts

    def invoke(self, context, event):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        context.scene.ob2_pmn_pick = False

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        context.tool_settings.mesh_select_mode = (True, False, False)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set("Triangle to PMN Input: Select 3 verts in P/M/N order, press Space to confirm, Esc to cancel")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            context.workspace.status_text_set(None)
            self.report({'INFO'}, "PMN Apply cancelled")
            return {'CANCELLED'}

        if event.type == 'SPACE' and event.value == 'PRESS':
            obj = context.active_object
            if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
                context.workspace.status_text_set(None)
                self.report({'ERROR'}, "Active mesh in Edit Mode required")
                return {'CANCELLED'}
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            selected_verts = self._read_ordered_selected_verts(bm)

            if len(selected_verts) != 3:
                self.report({'WARNING'}, f"Need exactly 3 selected vertices (currently {len(selected_verts)}).")
                return {'RUNNING_MODAL'}

            scene = context.scene
            scene.ob2_pmn_p = int(selected_verts[0].index)
            scene.ob2_pmn_m = int(selected_verts[1].index)
            scene.ob2_pmn_n = int(selected_verts[2].index)
            _update_pmn_vectors_from_input(context)
            bpy.ops.ed.undo_push(message=f"Triangle to PMN Input P={scene.ob2_pmn_p}, M={scene.ob2_pmn_m}, N={scene.ob2_pmn_n}")
            context.workspace.status_text_set(None)
            self.report({'INFO'}, f"Input PMN set: P={scene.ob2_pmn_p}, M={scene.ob2_pmn_m}, N={scene.ob2_pmn_n}")
            return {'FINISHED'}

        return {'PASS_THROUGH'}

class OB2_OT_pmn_select_faces_by_pmn(Operator):
    bl_idname = "ob2.pmn_select_faces_by_pmn"
    bl_label = "Select Faces By PMN"
    bl_description = "Select all faces whose PMN exactly matches Selected or Input PMN"
    bl_options = {'REGISTER', 'UNDO'}

    source: EnumProperty(
        name="Source",
        items=(
            ('SELECTED', "Selected", "Use PMN from Selected column"),
            ('INPUT', "Input", "Use PMN from Input column"),
        ),
        default='SELECTED',
    ) # type: ignore
    shift_add: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False) # type: ignore

    def invoke(self, context, event):
        self.shift_add = bool(event.shift)
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        context.tool_settings.mesh_select_mode = (False, False, True)

        scene = context.scene
        if self.source == 'INPUT':
            target = (
                int(scene.ob2_pmn_p),
                int(scene.ob2_pmn_m),
                int(scene.ob2_pmn_n),
            )
        else:
            target = (
                int(scene.ob2_pmn_selected_p),
                int(scene.ob2_pmn_selected_m),
                int(scene.ob2_pmn_selected_n),
            )

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        matched_count = 0
        first_match = None
        for face in bm.faces:
            vec = face[pmn_layer]
            pmn = (
                int(round(vec[0])),
                int(round(vec[1])),
                int(round(vec[2])),
            )
            is_match = (pmn == target)
            if self.shift_add:
                if is_match:
                    face.select = True
            else:
                face.select = is_match
            if is_match:
                matched_count += 1
                if first_match is None:
                    first_match = face

        if first_match is not None:
            bm.faces.active = first_match

        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)

        source_name = "Input" if self.source == 'INPUT' else "Selected"
        if matched_count == 0:
            self.report({'WARNING'}, f"No faces matched {source_name} PMN {target}")
        else:
            if self.shift_add:
                self.report({'INFO'}, f"Added {matched_count} face(s) matching {source_name} PMN {target}")
            else:
                self.report({'INFO'}, f"Selected {matched_count} face(s) matching {source_name} PMN {target}")
        return {'FINISHED'}

class OB2_OT_pmn_assign_from_three_verts(Operator):
    bl_idname = "ob2.pmn_assign_from_three_verts"
    bl_label = "Assign P/M/N From 3 Selected Verts"
    bl_description = "Assign first/second/third selected vertices to P/M/N"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()

        # Build selected vertices in click order (oldest -> newest).
        selected_verts = []
        seen = set()
        for elem in bm.select_history:
            if isinstance(elem, bmesh.types.BMVert) and elem.select and elem.index not in seen:
                selected_verts.append(elem)
                seen.add(elem.index)

        # Fallback: include selected verts missing from history.
        if len(selected_verts) < 3:
            for v in bm.verts:
                if v.select and v.index not in seen:
                    selected_verts.append(v)
                    seen.add(v.index)

        if len(selected_verts) > 3:
            self.report({'WARNING'}, f"Too many vertices selected ({len(selected_verts)}). Select exactly 3.")
            return {'CANCELLED'}
        elif len(selected_verts) < 3:
            self.report({'WARNING'}, f"Not enough vertices selected ({len(selected_verts)}). Select exactly 3.")
            return {'CANCELLED'}

        # Assign in order
        scene = context.scene
        scene.ob2_pmn_p = selected_verts[0].index
        scene.ob2_pmn_m = selected_verts[1].index
        scene.ob2_pmn_n = selected_verts[2].index

        # Disable the picker to prevent overwriting with active face PMN
        scene.ob2_pmn_pick = False

        self.report({'INFO'}, f"Assigned P={selected_verts[0].index}, M={selected_verts[1].index}, N={selected_verts[2].index}")
        return {'FINISHED'}

class OB2_OT_pmn_assign_quads_from_selected(Operator):
    bl_idname = "ob2.pmn_assign_quads_from_selected"
    bl_label = "Assign PMN Quads from Selected"
    bl_description = "Pair adjacent selected triangles as quads and assign matching PMN to both triangles"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        selected_tris = [f for f in bm.faces if f.select and len(f.verts) == 3]
        if len(selected_tris) < 2:
            self.report({'WARNING'}, "Select at least two triangle faces")
            return {'CANCELLED'}

        selected_set = set(selected_tris)
        edge_to_faces = {}
        for f in selected_tris:
            for e in f.edges:
                edge_to_faces.setdefault(e, []).append(f)

        candidates = []
        for edge, faces in edge_to_faces.items():
            if len(faces) != 2:
                continue
            f1, f2 = faces
            if f1 not in selected_set or f2 not in selected_set:
                continue

            edge_verts = set(edge.verts)
            unique_1 = [v for v in f1.verts if v not in edge_verts]
            unique_2 = [v for v in f2.verts if v not in edge_verts]
            if len(unique_1) != 1 or len(unique_2) != 1:
                continue

            u1 = unique_1[0]
            u2 = unique_2[0]
            ev0, ev1 = edge.verts[0], edge.verts[1]

            # Prefer planar and similarly oriented pairs.
            n1 = f1.normal
            n2 = f2.normal
            normal_penalty = 1.0 - max(-1.0, min(1.0, n1.dot(n2)))
            plane_penalty = abs((u2.co - f1.verts[0].co).dot(n1))
            score = float(normal_penalty + plane_penalty)

            candidates.append((score, f1, f2, u1, u2, ev0, ev1))

        if not candidates:
            self.report({'WARNING'}, "No adjacent triangle pairs found in selection")
            return {'CANCELLED'}

        candidates.sort(key=lambda item: item[0])
        used_faces = set()
        paired_count = 0

        for _, f1, f2, u1, u2, ev0, ev1 in candidates:
            if f1 in used_faces or f2 in used_faces:
                continue

            # Deterministic PMN triplet for both tris in this inferred quad.
            if int(ev0.index) <= int(ev1.index):
                n_vert = ev0
            else:
                n_vert = ev1

            p_idx = float(u1.index)
            m_idx = float(u2.index)
            n_idx = float(n_vert.index)

            f1[pmn_layer] = (p_idx, m_idx, n_idx)
            f2[pmn_layer] = (p_idx, m_idx, n_idx)

            used_faces.add(f1)
            used_faces.add(f2)
            paired_count += 1

        if paired_count == 0:
            self.report({'WARNING'}, "No non-overlapping triangle pairs could be assigned")
            return {'CANCELLED'}

        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)

        # Refresh UVs from newly written PMN values for selected faces.
        bpy.ops.ob2.pmn_to_uv()

        total_faces = paired_count * 2
        self.report({'INFO'}, f"Assigned PMN for {paired_count} inferred quad(s) ({total_faces} triangle faces)")
        return {'FINISHED'}


class OB2_OT_pmn_assign_triangles_from_selected(Operator):
    bl_idname = "ob2.pmn_assign_triangles_from_selected"
    bl_label = "Assign PMN to Triangles"
    bl_description = "Assign each selected triangle face its own PMN values using that face's vertex order"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        selected_tris = [f for f in bm.faces if f.select and len(f.verts) == 3]
        if not selected_tris:
            self.report({'WARNING'}, "No selected triangle faces")
            return {'CANCELLED'}

        for face in selected_tris:
            face[pmn_layer] = (
                float(face.verts[0].index),
                float(face.verts[1].index),
                float(face.verts[2].index),
            )

        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        bpy.ops.ob2.pmn_to_uv()
        self.report({'INFO'}, f"Assigned PMN for {len(selected_tris)} selected triangle face(s)")
        return {'FINISHED'}


class OB2_OT_pmn_swap_indices_on_selected_faces(Operator):
    bl_idname = "ob2.pmn_swap_indices_on_selected_faces"
    bl_label = "Swap PMN Indices on Selected Faces"
    bl_description = "Swap two PMN indices on selected faces"
    bl_options = {'REGISTER', 'UNDO'}

    swap_mode: EnumProperty(
        name="Swap Mode",
        description="Which PMN indices to swap",
        items=(
            ('P_M', "P and M", "Swap P and M"),
            ('P_N', "P and N", "Swap P and N"),
            ('M_N', "M and N", "Swap M and N"),
        ),
        default='P_M',
    ) # type: ignore

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        selected_faces = [face for face in bm.faces if face.select]
        if not selected_faces:
            self.report({'WARNING'}, "No selected faces")
            return {'CANCELLED'}

        for face in selected_faces:
            p, m, n = (
                int(round(face[pmn_layer][0])),
                int(round(face[pmn_layer][1])),
                int(round(face[pmn_layer][2])),
            )

            if self.swap_mode == 'P_M':
                p, m = m, p
            elif self.swap_mode == 'P_N':
                p, n = n, p
            else:
                m, n = n, m

            face[pmn_layer] = (float(p), float(m), float(n))

        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        bpy.ops.ob2.pmn_to_uv()

        if self.swap_mode == 'P_M':
            swap_text = "P and M"
        elif self.swap_mode == 'P_N':
            swap_text = "P and N"
        else:
            swap_text = "M and N"

        self.report({'INFO'}, f"Swapped {swap_text} on {len(selected_faces)} selected face(s)")
        return {'FINISHED'}

class OB2_OT_pmn_copy_selected_to_input(Operator):
    bl_idname = "ob2.pmn_copy_selected_to_input"
    bl_label = "Copy Selected PMN To Input"
    bl_description = "Copy active-face PMN indices from Selected column into Input column"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        scene.ob2_pmn_p = int(scene.ob2_pmn_selected_p)
        scene.ob2_pmn_m = int(scene.ob2_pmn_selected_m)
        scene.ob2_pmn_n = int(scene.ob2_pmn_selected_n)
        _update_pmn_vectors_from_input(context)
        bpy.ops.ed.undo_push(message="Copy Selected PMN to Input")
        self.report({'INFO'}, f"Copied Selected PMN to Input: P={scene.ob2_pmn_p}, M={scene.ob2_pmn_m}, N={scene.ob2_pmn_n}")
        return {'FINISHED'}

class OB2_OT_pmn_apply_to_faces(Operator):
    bl_idname = "ob2.pmn_apply_to_faces"
    bl_label = "Apply PMN to Faces"
    bl_description = "Switch to face select and press Space to apply Input PMN to selected faces"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        context.scene.ob2_pmn_pick = False

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        context.tool_settings.mesh_select_mode = (False, False, True)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set("Apply PMN: Select faces, press Space to apply, Esc to cancel")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            context.workspace.status_text_set(None)
            self.report({'INFO'}, "PMN Apply cancelled")
            return {'CANCELLED'}

        if event.type == 'SPACE' and event.value == 'PRESS':
            result = self.execute(context)
            if result == {'FINISHED'}:
                context.workspace.status_text_set(None)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        scene = context.scene
        p_val = float(scene.ob2_pmn_p)
        m_val = float(scene.ob2_pmn_m)
        n_val = float(scene.ob2_pmn_n)

        applied_count = 0
        for face in bm.faces:
            if not face.select:
                continue
            face[pmn_layer] = (p_val, m_val, n_val)
            applied_count += 1

        bmesh.update_edit_mesh(mesh)

        if applied_count == 0:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        # Now update UVs for those faces
        bpy.ops.ob2.pmn_to_uv()

        self.report({'INFO'}, f"Applied PMN to {applied_count} face(s) and updated UVs")
        return {'FINISHED'}

class OB2_OT_pmn_to_uv(Operator):
    bl_idname = "ob2.pmn_to_uv"
    bl_label = "Refresh UV from PMN"
    bl_description = "Refresh selected faces' UV coordinates using PMN"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        uv_layer = bm.loops.layers.uv.get("UVMap")
        if uv_layer is None:
            uv_layer = bm.loops.layers.uv.new("UVMap")

        converted_count = 0
        skipped_count = 0

        for face in bm.faces:
            if not face.select:
                continue

            pmn_vec = face[pmn_layer]
            p_idx = int(round(pmn_vec[0]))
            m_idx = int(round(pmn_vec[1]))
            n_idx = int(round(pmn_vec[2]))

            # Non-textured faces are expected to use 0,0,0 PMN.
            if p_idx == 0 and m_idx == 0 and n_idx == 0:
                skipped_count += 1
                continue

            uvs = _uvs_from_pmn(mesh, face, p_idx, m_idx, n_idx)
            if uvs is None:
                skipped_count += 1
                continue

            for loop, uv in zip(face.loops, uvs):
                loop[uv_layer].uv = uv

            converted_count += 1

        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)

        if converted_count == 0:
            self.report({'WARNING'}, f"Converted 0 faces. Skipped {skipped_count} face(s).")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Converted {converted_count} face(s) from PMN to UV (skipped {skipped_count}).")
        return {'FINISHED'}

class OB2_OT_uv_to_pmn(Operator):
    bl_idname = "ob2.uv_to_pmn"
    bl_label = "Convert UV To PMN"
    bl_description = "Estimate PMN indices from UVs for selected faces"
    bl_options = {'REGISTER', 'UNDO'}

    generate_new: BoolProperty(name="Generate New Vertices", default=False) # type: ignore

    def invoke(self, context, event):
        if not self.generate_new:
            return self.execute(context)

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return self.execute(context)

        if context.mode != 'EDIT_MESH':
            return self.execute(context)

        bm = bmesh.from_edit_mesh(obj.data)
        selected_face_count = sum(1 for f in bm.faces if f.select)
        if selected_face_count > 1:
            return context.window_manager.invoke_props_dialog(self, width=420)

        return self.execute(context)

    def draw(self, context):
        if self.generate_new:
            layout = self.layout
            layout.label(text="You are generating vertices from UV for multiple faces.", icon='ERROR')
            layout.label(text="This may create many vertices. Continue?")

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        scene = context.scene
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()

        pmn_layer = _ensure_pmn_layer_in_edit_mesh(obj, bm)
        if pmn_layer is None:
            self.report({'ERROR'}, "Mesh has no FACE vector attribute named PMN")
            return {'CANCELLED'}

        uv_layer = bm.loops.layers.uv.get("UVMap")
        if uv_layer is None:
            self.report({'ERROR'}, "Mesh has no UVMap layer")
            return {'CANCELLED'}

        generate_new = bool(self.generate_new)
        selected_faces = [f for f in bm.faces if f.select]
        if not selected_faces:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        stable_layer = _ensure_pmn_stable_ids(bm)
        max_stable_id = -1
        for v in bm.verts:
            sid = int(v[stable_layer])
            if sid > max_stable_id:
                max_stable_id = sid
        next_stable_id = max_stable_id + 1

        def closest_vertex_index(target):
            tx, ty, tz = float(target[0]), float(target[1]), float(target[2])
            best_idx = 0
            best_d2 = None
            for v in bm.verts:
                dx = float(v.co.x) - tx
                dy = float(v.co.y) - ty
                dz = float(v.co.z) - tz
                d2 = dx * dx + dy * dy + dz * dz
                if best_d2 is None or d2 < best_d2:
                    best_d2 = d2
                    best_idx = int(v.index)
            return best_idx

        def find_vertex_at_position(target, epsilon=1e-6):
            tx, ty, tz = float(target[0]), float(target[1]), float(target[2])
            for v in bm.verts:
                if (
                    abs(float(v.co.x) - tx) <= epsilon
                    and abs(float(v.co.y) - ty) <= epsilon
                    and abs(float(v.co.z) - tz) <= epsilon
                ):
                    return v
            return None

        # Reuse vertices by snapped integer coordinate to avoid duplicates across faces.
        coord_to_vert = {}
        for v in bm.verts:
            ix = round(float(v.co.x))
            iy = round(float(v.co.y))
            iz = round(float(v.co.z))
            if (
                abs(float(v.co.x) - ix) <= 1e-6
                and abs(float(v.co.y) - iy) <= 1e-6
                and abs(float(v.co.z) - iz) <= 1e-6
            ):
                key = (int(ix), int(iy), int(iz))
                if key not in coord_to_vert:
                    coord_to_vert[key] = v

        converted_count = 0
        skipped_count = 0
        generated_count = 0
        for face in selected_faces:
            if len(face.verts) != 3 or len(face.loops) != 3:
                skipped_count += 1
                continue

            a = face.verts[0].co
            b = face.verts[1].co
            c = face.verts[2].co

            uv_a = face.loops[0][uv_layer].uv
            uv_b = face.loops[1][uv_layer].uv
            uv_c = face.loops[2][uv_layer].uv

            solved = pmn_uv.solve_pmn_from_triangle_uv(a, b, c, uv_a, uv_b, uv_c)
            if solved is None:
                skipped_count += 1
                continue

            targets = {
                "P": solved[0],
                "M": solved[1],
                "N": solved[2],
            }
            pmn_indices = {}

            for name, target in targets.items():
                snapped = (
                    int(round(float(target[0]))),
                    int(round(float(target[1]))),
                    int(round(float(target[2]))),
                )

                if generate_new:
                    existing_snapped = coord_to_vert.get(snapped)
                    if existing_snapped is not None:
                        pmn_indices[name] = int(existing_snapped.index)
                    else:
                        new_vert = bm.verts.new(snapped)
                        new_vert[stable_layer] = next_stable_id
                        next_stable_id += 1
                        bm.verts.index_update()
                        bm.verts.ensure_lookup_table()
                        coord_to_vert[snapped] = new_vert
                        pmn_indices[name] = int(new_vert.index)
                        generated_count += 1
                else:
                    existing_vert = find_vertex_at_position(target)
                    if existing_vert is not None:
                        pmn_indices[name] = int(existing_vert.index)
                        continue

                    pmn_indices[name] = closest_vertex_index(target)

            face[pmn_layer] = (
                float(pmn_indices["P"]),
                float(pmn_indices["M"]),
                float(pmn_indices["N"]),
            )
            converted_count += 1

        # Persist PMN and optional vertex additions before UV refresh.
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=generate_new)

        # If we generated snapped vertices, recompute UVs only once at the end.
        if generate_new and converted_count > 0:
            bm = bmesh.from_edit_mesh(mesh)
            bm.faces.ensure_lookup_table()
            uv_layer = bm.loops.layers.uv.get("UVMap")
            for face in bm.faces:
                if not face.select:
                    continue
                pmn_vec = face[pmn_layer]
                p_idx = int(round(pmn_vec[0]))
                m_idx = int(round(pmn_vec[1]))
                n_idx = int(round(pmn_vec[2]))
                uvs = _uvs_from_pmn(mesh, face, p_idx, m_idx, n_idx)
                if uvs is None:
                    continue
                for loop, uv in zip(face.loops, uvs):
                    loop[uv_layer].uv = uv
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)

        if converted_count == 0:
            self.report({'WARNING'}, f"Converted 0 faces. Skipped {skipped_count} face(s).")
            return {'CANCELLED'}

        if generate_new:
            self.report({'INFO'}, f"Converted {converted_count} face(s), generated {generated_count} vertex/vertices, skipped {skipped_count}.")
        else:
            self.report({'INFO'}, f"Converted {converted_count} face(s), skipped {skipped_count}.")
        return {'FINISHED'}

class OB2_OT_material_to_hsl16(Operator):
    bl_idname = "ob2.material_to_hsl16"
    bl_label = "Load Material"
    bl_description = "Read active material diffuse color into HSL16"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if not obj.active_material:
            self.report({'ERROR'}, "No active material on object")
            return {'CANCELLED'}

        mat = obj.active_material
        r, g, b = mat.diffuse_color[:3]

        h, l, s = colorsys.rgb_to_hls(r, g, b)
        hi = round(h * 63.0)
        si = round(s * 7.0)
        li = round(l * 127.0)
        value = ((hi & 0x3F) << 10) | ((si & 0x07) << 7) | (li & 0x7F)
        context.scene.ob2_color_value = value

        rq, gq, bq = colorsys.hls_to_rgb(hi / 63.0, li / 127.0, si / 7.0)
        if max(abs(r - rq), abs(g - gq), abs(b - bq)) > 1e-5:
            self.report({'WARNING'}, f"Loaded HSL16 {value} from '{mat.name}' (rounded to nearest integer step)")
        else:
            self.report({'INFO'}, f"Loaded HSL16 {value} from '{mat.name}'")

        return {'FINISHED'}

class OB2_OT_hsl16_to_material_overwrite(Operator):
    bl_idname = "ob2.hsl16_to_material_overwrite"
    bl_label = "HSL16 to Material (Overwrite)"
    bl_description = "Overwrite active material diffuse color from current HSL16 value"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        start_mode = context.mode
        result = {'CANCELLED'}

        if start_mode == 'EDIT_MESH':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                self.report({'ERROR'}, "Could not switch to Object Mode for material edit")
                return {'CANCELLED'}

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        if not obj.active_material:
            self.report({'ERROR'}, "No active material on object")
            return {'CANCELLED'}

        value = max(0, min(int(context.scene.ob2_color_value), 65535))
        h = ((value >> 10) & 0x3F) / 63.0
        s = ((value >> 7) & 0x07) / 7.0
        l = (value & 0x7F) / 127.0
        r, g, b = colorsys.hls_to_rgb(h, l, s)

        mat = obj.active_material
        alpha = mat.diffuse_color[3] if len(mat.diffuse_color) > 3 else 1.0
        mat.diffuse_color = (r, g, b, alpha)
        self.report({'INFO'}, f"Overwrote '{mat.name}' diffuse from HSL16 {value}")
        result = {'FINISHED'}

        return result

class OB2_OT_hsl16_to_material_new(Operator):
    bl_idname = "ob2.hsl16_to_material_new"
    bl_label = "HSL16 to NEW Material"
    bl_description = "Create a new material from current HSL16 value and assign it"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        start_mode = context.mode
        result = {'CANCELLED'}

        if start_mode == 'EDIT_MESH':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                self.report({'ERROR'}, "Could not switch to Object Mode for material edit")
                return {'CANCELLED'}

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return {'CANCELLED'}

        value = max(0, min(int(context.scene.ob2_color_value), 65535))
        h = ((value >> 10) & 0x3F) / 63.0
        s = ((value >> 7) & 0x07) / 7.0
        l = (value & 0x7F) / 127.0
        r, g, b = colorsys.hls_to_rgb(h, l, s)

        base_name = f"HSL16_{value}"
        mat_name = base_name
        suffix = 1
        while bpy.data.materials.get(mat_name) is not None:
            mat_name = f"{base_name}_{suffix}"
            suffix += 1

        mat = bpy.data.materials.new(name=mat_name)
        mat.diffuse_color = (r, g, b, 1.0)

        obj.data.materials.append(mat)
        obj.active_material_index = len(obj.data.materials) - 1
        self.report({'INFO'}, f"Created and assigned new material '{mat.name}' from HSL16 {value}")
        result = {'FINISHED'}

        return result

class OB2_OT_select_labeled(Operator):
    bl_idname = "ob2.select_labeled"
    bl_label = "Select Labeled"
    bl_description = "Select items whose label matches the given label"

    # If True: add to selection. If False: replace selection (only).
    add: BoolProperty(
        name="Add",
        description="Add to existing selection instead of replacing it",
        default=False,
    ) # type: ignore

    deselect: BoolProperty(
        name="Deselect",
        description="Deselect items whose label matches the given label",
        default=False,
    ) # type: ignore

    type: StringProperty(
        name="Attribute Type",
        description="The attribute to select by",
        default="VSKIN",
    ) # type: ignore

    def execute(self, context):
        if self.type == "VSKIN":
            target = context.scene.ob2_vskin_label
        elif self.type == "TSKIN":
            target = context.scene.ob2_tskin_label
        elif self.type == "PRI":
            target = context.scene.ob2_pri_label
        elif self.type == "ALPHA":
            target = context.scene.ob2_alpha_label
        else:
            self.report({'ERROR'}, f"Unknown attribute type: {self.type}")
            return {'CANCELLED'}
        start_mode = context.mode

        # Which objects to affect?
        objs = []
        if start_mode == 'EDIT_MESH':
            # All meshes currently in edit mode (multi-object edit supported)
            objs = [o for o in getattr(context, "objects_in_mode", []) if o.type == 'MESH']
        else:
            # From object mode: use selected meshes, or fallback to active mesh
            if context.selected_objects:
                objs = [o for o in context.selected_objects if o.type == 'MESH']
            elif context.active_object and context.active_object.type == 'MESH':
                objs = [context.active_object]

        if not objs:
            self.report({'ERROR'}, "No mesh object(s) to operate on")
            return {'CANCELLED'}

        # Go to object mode to edit vertex/face selection flags
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        try:
            if self.type == "VSKIN":
                # Vertex-based selection
                context.tool_settings.mesh_select_mode = (True, False, False)
            else:
                # Face-based selection (TSKIN, PRI, ALPHA, etc.)
                context.tool_settings.mesh_select_mode = (False, False, True)
        except AttributeError:
            pass

        has_attr = False

        for obj in objs:
            mesh = obj.data
            attr = mesh.attributes.get(self.type)
            #print(f"Checking object '{obj.name}' for attribute '{self.type}': {'Found' if attr else 'Not Found'}")
            if not attr or attr.domain not in {'POINT', 'FACE'}:
                continue

            has_attr = True

            # Clear everything in "Select Only" mode
            if not self.add:
                for v in mesh.vertices:
                    v.select = False
                for e in mesh.edges:
                    e.select = False
                for p in mesh.polygons:
                    p.select = False

            data = attr.data
            
            
            if self.type == "VSKIN":
                deselect_list = [] #gotta do something different for vertices - force unselect faces and edges as well.
                for i, v in enumerate(mesh.vertices):
                    if data[i].value != target:
                        continue

                    if not self.deselect:
                        v.select = True
                    else:
                        deselect_list.append(i)
                if self.deselect and deselect_list:
                    deselect_set = set(deselect_list)
                    for i in deselect_set:
                        mesh.vertices[i].select = False
                    for e in mesh.edges:
                        if any(vidx in deselect_set for vidx in e.vertices):
                            e.select = False
                    for p in mesh.polygons:
                        if any(vidx in deselect_set for vidx in p.vertices):
                            p.select = False    
                        
            else: # because everything else is a face attribute.
                for i, p in enumerate(mesh.polygons):
                    if data[i].value != target:
                        continue

                    if not self.deselect:
                        p.select = True
                    else:
                        p.select = False
                        for vidx in p.vertices:
                                mesh.vertices[vidx].select = False
                        for edgx in p.edge_keys:
                            edge = mesh.edge_keys.index(edgx)
                            mesh.edges[edge].select = False

            # Ensure these objects stay selected for edit mode
            obj.select_set(True)

        if not has_attr:
            self.report({'ERROR'}, f"No {self.type} attribute found on selected mesh(es).")
            return {'CANCELLED'}

        # Return to edit mode (or stay in edit mode) as requested
        if start_mode in {'EDIT_MESH', 'OBJECT'}:
            bpy.ops.object.mode_set(mode='EDIT')

        bpy.ops.ed.undo_push(message=f"Select {self.type}={target}")
        return {'FINISHED'}

class OB2_OT_apply_label(Operator):
    bl_idname = "ob2.apply_label"
    bl_label = "Apply Label"
    bl_description = "Apply label to selected geometry"

    type: StringProperty(
        name="Attribute Type",
        description="The attribute to apply",
        default="VSKIN",
    ) # type: ignore

    def execute(self, context):
        if self.type == "VSKIN":
            target = context.scene.ob2_vskin_label
        elif self.type == "TSKIN":
            target = context.scene.ob2_tskin_label
        elif self.type == "PRI":
            target = context.scene.ob2_pri_label
        elif self.type == "ALPHA":
            target = context.scene.ob2_alpha_label
        else:
            self.report({'ERROR'}, f"Unknown attribute type: {self.type}")
            return {'CANCELLED'}
    
        # Which objects to affect?
        objs = []
        start_mode = context.mode
        if start_mode == 'EDIT_MESH':
            objs = [o for o in getattr(context, "objects_in_mode", []) if o.type == 'MESH']
        else:
            if context.selected_objects:
                objs = [o for o in context.selected_objects if o.type == 'MESH']
            elif context.active_object and context.active_object.type == 'MESH':
                objs = [context.active_object]

        if not objs:
            self.report({'ERROR'}, "No mesh object(s) to operate on")
            return {'CANCELLED'}

        # Go to edit mode
        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        try:
            if self.type == "VSKIN":
                context.tool_settings.mesh_select_mode = (True, False, False)
            else:
                context.tool_settings.mesh_select_mode = (False, False, True)
        except AttributeError:
            pass

        # Apply the attribute value to selected geometry using bmesh
        for obj in objs:
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)
            
            layer = None
            if self.type == "VSKIN":
                layer = bm.verts.layers.int.get(self.type)
                if not layer:
                    self.report({'ERROR'}, f"No {self.type} attribute found on mesh '{obj.name}'.")
                    continue
                for vert in bm.verts:
                    if vert.select:
                        vert[layer] = target
            else:
                layer = bm.faces.layers.int.get(self.type)
                if not layer:
                    self.report({'ERROR'}, f"No {self.type} attribute found on mesh '{obj.name}'.")
                    continue
                for face in bm.faces:
                    if face.select:
                        face[layer] = target
            
            bmesh.update_edit_mesh(mesh)
        
        bpy.ops.ed.undo_push(message=f"Apply {self.type}={target}")
        return {'FINISHED'}

class OB2_OT_add_attribute(Operator):
    bl_idname = "ob2.add_attribute"
    bl_label = "Add OB2 Attribute"
    bl_description = "Add an OB2 integer attribute to selected mesh object(s)"
    bl_options = {'REGISTER', 'UNDO'}

    type: StringProperty(
        name="Attribute Type",
        description="The attribute to create",
        default="VSKIN",
    ) # type: ignore

    def execute(self, context):
        attribute_specs = {
            "VSKIN": ("POINT", "INT"),
            "TSKIN": ("FACE", "INT"),
            "PRI": ("FACE", "INT"),
            "ALPHA": ("FACE", "INT"),
        }

        if self.type not in attribute_specs:
            self.report({'ERROR'}, f"Unknown attribute type: {self.type}")
            return {'CANCELLED'}

        start_mode = context.mode
        switched_mode = False

        if start_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
            switched_mode = True

        try:
            objs = [o for o in context.selected_objects if o.type == 'MESH']
            if not objs and context.active_object and context.active_object.type == 'MESH':
                objs = [context.active_object]

            if not objs:
                self.report({'ERROR'}, "No selected mesh object(s)")
                return {'CANCELLED'}

            domain, attr_type = attribute_specs[self.type]
            created_count = 0
            already_count = 0
            corrected_converted_count = 0
            corrected_reset_count = 0

            for obj in objs:
                mesh = obj.data
                existing = mesh.attributes.get(self.type)
                if existing is None:
                    mesh.attributes.new(name=self.type, type=attr_type, domain=domain)
                    created_count += 1
                    continue

                existing_type = getattr(existing, "data_type", None)
                if existing.domain == domain and existing_type == attr_type:
                    already_count += 1
                    continue

                copied_values = None
                if existing.domain == domain:
                    try:
                        copied_values = [int(round(item.value)) for item in existing.data]
                    except AttributeError:
                        copied_values = None

                mesh.attributes.remove(existing)
                replacement = mesh.attributes.new(name=self.type, type=attr_type, domain=domain)

                if copied_values is not None and len(copied_values) == len(replacement.data):
                    replacement.data.foreach_set("value", copied_values)
                    corrected_converted_count += 1
                else:
                    corrected_reset_count += 1

            summary_parts = []
            if created_count:
                summary_parts.append(f"created {created_count}")
            if corrected_converted_count:
                summary_parts.append(f"corrected+converted {corrected_converted_count}")
            if corrected_reset_count:
                summary_parts.append(f"corrected+reset {corrected_reset_count}")
            if already_count:
                summary_parts.append(f"already correct {already_count}")

            if not summary_parts:
                self.report({'INFO'}, f"No changes needed for {self.type}")
            else:
                self.report({'INFO'}, f"{self.type}: " + ", ".join(summary_parts))

            return {'FINISHED'}
        finally:
            if switched_mode and context.active_object and context.active_object.type == 'MESH' and context.mode == 'OBJECT':
                bpy.ops.object.mode_set(mode='EDIT')


# Register classes
classes = (
    ImportOB2,
    ExportOB2Confirm,
    ExportOB2,
    OB2_AddonPreferences,
    OB2_OT_reset_pmn_preference_colors,
    OB2_PT_import_export_panel,
    OB2_PT_attributes_panel,
    OB2_PT_color_panel,
    OB2_PT_pmn_panel,
    OB2_OT_select_labeled,
    OB2_OT_apply_label,
    OB2_OT_add_attribute,
    OB2_OT_material_to_hsl16,
    OB2_OT_hsl16_to_material_overwrite,
    OB2_OT_hsl16_to_material_new,
    OB2_OT_add_pmn_attribute,
    OB2_OT_pmn_select_vertex,
    OB2_OT_pmn_apply_from_selection,
    OB2_OT_pmn_set_input_from_active_vertex,
    OB2_OT_pmn_select_triplet_vertices,
    OB2_OT_pmn_apply_input_from_three_verts,
    OB2_OT_pmn_select_faces_by_pmn,
    OB2_OT_pmn_assign_from_three_verts,
    OB2_OT_pmn_assign_quads_from_selected,
    OB2_OT_pmn_assign_triangles_from_selected,
    OB2_OT_pmn_swap_indices_on_selected_faces,
    OB2_OT_pmn_copy_selected_to_input,
    OB2_OT_pmn_apply_to_faces,
    OB2_OT_uv_to_pmn,
    OB2_OT_pmn_to_uv,
)

# Update callbacks start the picker timer when any picker is enabled
def _update_timer_pick(self, context):
    if (context.scene.ob2_vskin_pick
        or context.scene.ob2_tskin_pick
        or context.scene.ob2_pri_pick
        or context.scene.ob2_alpha_pick
        or context.scene.ob2_pmn_pick):
        _start_picker_timer()

def _update_ob2_vskin_foldout(self, context):
    # Turn off VSKIN picker when foldout is collapsed.
    if not context.scene.ob2_vskin_foldout:
        context.scene.ob2_vskin_pick = False
    # Turn back on when expanded.
    else:
        context.scene.ob2_vskin_pick = True

def _update_ob2_tskin_foldout(self, context):
    # Turn off TSKIN pickers when foldout is collapsed.
    if not context.scene.ob2_tskin_foldout:
        context.scene.ob2_tskin_pick = False
    # Turn back on when expanded.
    else:
        context.scene.ob2_tskin_pick = True

def _update_ob2_pri_foldout(self, context):
    # Turn off PRI pickers when foldout is collapsed.
    if not context.scene.ob2_pri_foldout:
        context.scene.ob2_pri_pick = False
    # Turn back on when expanded.
    else:
        context.scene.ob2_pri_pick = True

def _update_ob2_alpha_foldout(self, context):
    # Turn off ALPHA pickers when foldout is collapsed.
    if not context.scene.ob2_alpha_foldout:
        context.scene.ob2_alpha_pick = False
    # Turn back on when expanded.
    else:
        context.scene.ob2_alpha_pick = True

def _update_vskin_label(self, context):
    # Turn off VSKIN picker when manually changing the value
    # Don't turn off if the picker itself is updating the value
    global _picker_updating_value
    if not _picker_updating_value:
        context.scene.ob2_vskin_pick = False

def _update_tskin_label(self, context):
    # Turn off TSKIN picker when manually changing the value
    # Don't turn off if the picker itself is updating the value
    global _picker_updating_value
    if not _picker_updating_value:
        context.scene.ob2_tskin_pick = False

def _update_pri_label(self, context):
    # Turn off PRI picker when manually changing the value
    # Don't turn off if the picker itself is updating the value
    global _picker_updating_value
    if not _picker_updating_value:
        context.scene.ob2_pri_pick = False

def _update_alpha_label(self, context):
    # Turn off ALPHA picker when manually changing the value
    # Don't turn off if the picker itself is updating the value
    global _picker_updating_value
    if not _picker_updating_value:
        context.scene.ob2_alpha_pick = False

def _update_pmn_p_label(self, context):
    _update_pmn_vectors_from_input(context)

def _update_pmn_m_label(self, context):
    _update_pmn_vectors_from_input(context)

def _update_pmn_n_label(self, context):
    _update_pmn_vectors_from_input(context)

def _set_ob2_preview_from_hsl(scene, h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 63.0, l / 127.0, s / 7.0)
    scene.ob2_color_preview = (r, g, b)

def _rgb15_to_rgb(value):
    v = max(0, min(int(value), 32767))
    r = ((v >> 10) & 0x1F) / 31.0
    g = ((v >> 5) & 0x1F) / 31.0
    b = (v & 0x1F) / 31.0
    return r, g, b

def _rgb_to_rgb15(r, g, b):
    return ((round(r * 31.0) & 0x1F) << 10) | ((round(g * 31.0) & 0x1F) << 5) | (round(b * 31.0) & 0x1F)

def _rgb_to_hsl16(r, g, b):
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return ((round(h * 63.0) & 0x3F) << 10) | ((round(s * 7.0) & 0x07) << 7) | (round(l * 127.0) & 0x7F)

def _hsl16_to_rgb(value):
    v = max(0, min(int(value), 65535))
    h = ((v >> 10) & 0x3F) / 63.0
    s = ((v >> 7) & 0x07) / 7.0
    l = (v & 0x7F) / 127.0
    return colorsys.hls_to_rgb(h, l, s)

def _update_ob2_color_value(self, context):
    global _color_syncing
    if _color_syncing:
        return

    _color_syncing = True
    scene = context.scene
    value = max(0, min(int(scene.ob2_color_value), 65535))
    scene.ob2_color_h = (value >> 10) & 0x3F
    scene.ob2_color_s = (value >> 7) & 0x07
    scene.ob2_color_l = value & 0x7F
    scene.ob2_color_rgb15 = _rgb_to_rgb15(*_hsl16_to_rgb(value))
    _set_ob2_preview_from_hsl(scene, scene.ob2_color_h, scene.ob2_color_s, scene.ob2_color_l)
    _color_syncing = False

def _update_ob2_hsl_components(self, context):
    global _color_syncing
    if _color_syncing:
        return

    _color_syncing = True
    scene = context.scene
    h = max(0, min(int(scene.ob2_color_h), 63))
    s = max(0, min(int(scene.ob2_color_s), 7))
    # HSL16 stores lightness in 7 bits, so 128 is clamped to 127 for packing.
    l = max(0, min(int(scene.ob2_color_l), 127))
    scene.ob2_color_value = ((h & 0x3F) << 10) | ((s & 0x07) << 7) | (l & 0x7F)
    scene.ob2_color_rgb15 = _rgb_to_rgb15(*_hsl16_to_rgb(scene.ob2_color_value))
    _set_ob2_preview_from_hsl(scene, h, s, l)
    _color_syncing = False

def _update_ob2_color_rgb15(self, context):
    global _color_syncing
    if _color_syncing:
        return

    _color_syncing = True
    scene = context.scene
    rgb15 = max(0, min(int(scene.ob2_color_rgb15), 32767))
    scene.ob2_color_rgb15 = rgb15
    r, g, b = _rgb15_to_rgb(rgb15)
    scene.ob2_color_value = _rgb_to_hsl16(r, g, b)
    scene.ob2_color_h = (scene.ob2_color_value >> 10) & 0x3F
    scene.ob2_color_s = (scene.ob2_color_value >> 7) & 0x07
    scene.ob2_color_l = scene.ob2_color_value & 0x7F
    _set_ob2_preview_from_hsl(scene, scene.ob2_color_h, scene.ob2_color_s, scene.ob2_color_l)
    _color_syncing = False

def register():
    global _pmn_draw_handler

    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

    if _pmn_draw_handler is None:
        _pmn_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_pmn_vertices_overlay,
            (),
            'WINDOW',
            'POST_VIEW',
        )

    _start_picker_timer()

    bpy.types.Scene.ob2_vskin_label = IntProperty(
        name="VSKIN",
        description="VSKIN label to select",
        default=0,
        soft_min=0,
        soft_max=255,
        update=_update_vskin_label
    )

    bpy.types.Scene.ob2_tskin_label = IntProperty(
        name="TSKIN",
        description="TSKIN label to select",
        default=0,
        soft_min=0,
        soft_max=255,
        update=_update_tskin_label
    )

    bpy.types.Scene.ob2_pri_label = IntProperty(
        name="PRI",
        description="PRI label to select",
        default=0,
        soft_min=0,
        soft_max=255,
        update=_update_pri_label
    )

    bpy.types.Scene.ob2_alpha_label = IntProperty(
        name="ALPHA",
        description="ALPHA label to select",
        default=0,
        soft_min=0,
        soft_max=255,
        update=_update_alpha_label
    )

    bpy.types.Scene.ob2_vskin_pick = BoolProperty(
        name="VSKIN Picker",
        description="Click a vertex to set VSKIN label",
        default=False,
        update=_update_timer_pick
    )

    bpy.types.Scene.ob2_tskin_pick = BoolProperty(
        name="TSKIN Picker",
        description="Click a face to set TSKIN label",
        default=False,
        update=_update_timer_pick
    )

    bpy.types.Scene.ob2_pri_pick = BoolProperty(
        name="PRI Picker",
        description="Click a face to set PRI label",
        default=False,
        update=_update_timer_pick
    )

    bpy.types.Scene.ob2_alpha_pick = BoolProperty(
        name="ALPHA Picker",
        description="Click a face to set ALPHA label",
        default=False,
        update=_update_timer_pick
    )

    bpy.types.Scene.ob2_pmn_pick = BoolProperty(
        name="PMN Picker",
        description="Read PMN from selected textured face in edit mode",
        default=False,
        update=_update_timer_pick
    )

    bpy.types.Scene.ob2_pmn_show_vertices = BoolProperty(
        name="Highlight PMN",
        description="Draw P/M/N vertex highlights in the 3D viewport",
        default=True,
    )

    bpy.types.Scene.ob2_pmn_mapping_tools_foldout = BoolProperty(
        name="Mapping Tools",
        default=False,
    )

    bpy.types.Scene.ob2_vskin_foldout = BoolProperty(
        name="VSKIN Foldout",
        default=False,
        update=_update_ob2_vskin_foldout
    )

    bpy.types.Scene.ob2_tskin_foldout = BoolProperty(
        name="TSKIN Foldout",
        default=False,
        update=_update_ob2_tskin_foldout
    )

    bpy.types.Scene.ob2_pri_foldout = BoolProperty(
        name="PRI Foldout",
        default=False,
        update=_update_ob2_pri_foldout
    )

    bpy.types.Scene.ob2_alpha_foldout = BoolProperty(
        name="ALPHA Foldout",
        default=False,
        update=_update_ob2_alpha_foldout
    )

    bpy.types.Scene.ob2_pmn_p = IntProperty(
        name="P",
        description="PMN P vertex index",
        default=0,
        min=0,
        update=_update_pmn_p_label,
    )

    bpy.types.Scene.ob2_pmn_selected_p = IntProperty(
        name="Selected P",
        description="PMN P vertex index from active textured face",
        default=0,
        min=0,
    )

    bpy.types.Scene.ob2_pmn_m = IntProperty(
        name="M",
        description="PMN M vertex index",
        default=0,
        min=0,
        update=_update_pmn_m_label,
    )

    bpy.types.Scene.ob2_pmn_selected_m = IntProperty(
        name="Selected M",
        description="PMN M vertex index from active textured face",
        default=0,
        min=0,
    )

    bpy.types.Scene.ob2_pmn_n = IntProperty(
        name="N",
        description="PMN N vertex index",
        default=0,
        min=0,
        update=_update_pmn_n_label,
    )

    bpy.types.Scene.ob2_pmn_selected_n = IntProperty(
        name="Selected N",
        description="PMN N vertex index from active textured face",
        default=0,
        min=0,
    )

    bpy.types.Scene.ob2_pmn_vector_p = FloatVectorProperty(
        name="VectorP",
        description="World-space position of vertex at PMN index P",
        subtype='XYZ',
        size=3,
        default=(0.0, 0.0, 0.0),
    )

    bpy.types.Scene.ob2_pmn_vector_m = FloatVectorProperty(
        name="VectorM",
        description="World-space position of vertex at PMN index M",
        subtype='XYZ',
        size=3,
        default=(0.0, 0.0, 0.0),
    )

    bpy.types.Scene.ob2_pmn_vector_n = FloatVectorProperty(
        name="VectorN",
        description="World-space position of vertex at PMN index N",
        subtype='XYZ',
        size=3,
        default=(0.0, 0.0, 0.0),
    )

    bpy.types.Scene.ob2_pmn_color_p = FloatVectorProperty(
        name="P Color",
        description="Display color for PMN P",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=_PMN_COLORS["P"],
    )

    bpy.types.Scene.ob2_pmn_color_m = FloatVectorProperty(
        name="M Color",
        description="Display color for PMN M",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=_PMN_COLORS["M"],
    )

    bpy.types.Scene.ob2_pmn_color_n = FloatVectorProperty(
        name="N Color",
        description="Display color for PMN N",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=_PMN_COLORS["N"],
    )

    bpy.types.Scene.ob2_color_value = IntProperty(
        name="Color Value",
        description="Color value to convert (0-65535)",
        default=0,
        min=0,
        max=65535,
        update=_update_ob2_color_value,
    )

    bpy.types.Scene.ob2_color_rgb15 = IntProperty(
        name="RGB15 Value",
        description="RGB15 value (0-32767) synchronized with HSL16",
        default=0,
        min=0,
        max=32767,
        update=_update_ob2_color_rgb15,
    )

    bpy.types.Scene.ob2_color_h = IntProperty(
        name="Hue",
        description="Hue step in HSL16 (0-63)",
        default=0,
        min=0,
        max=63,
        update=_update_ob2_hsl_components,
    )

    bpy.types.Scene.ob2_color_s = IntProperty(
        name="Saturation",
        description="Saturation step in HSL16 (0-7)",
        default=0,
        min=0,
        max=7,
        update=_update_ob2_hsl_components,
    )

    bpy.types.Scene.ob2_color_l = IntProperty(
        name="Lightness",
        description="Lightness step in HSL16 (0-128, packed as 0-127)",
        default=0,
        min=0,
        max=128,
        update=_update_ob2_hsl_components,
    )

    bpy.types.Scene.ob2_color_preview = FloatVectorProperty(
        name="Preview",
        description="Preview color for the current HSL16 value",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.0, 0.0, 0.0),
    )

    for scene in _iter_available_scenes():
        value = max(0, min(int(scene.ob2_color_value), 65535))
        h = (value >> 10) & 0x3F
        s = (value >> 7) & 0x07
        l = value & 0x7F
        scene.ob2_color_rgb15 = _rgb_to_rgb15(*_hsl16_to_rgb(value))
        _set_ob2_preview_from_hsl(scene, h, s, l)

    _sync_scene_pmn_colors_from_preferences()


def unregister():
    global _pmn_draw_handler, _picker_timer_running

    unregister_timer_fn = getattr(bpy.app.timers, "unregister", None)
    if callable(unregister_timer_fn) and _is_picker_timer_registered():
        try:
            unregister_timer_fn(_picker_timer)
        except Exception:
            pass

    _picker_timer_running = False
    del bpy.types.Scene.ob2_pmn_p
    del bpy.types.Scene.ob2_pmn_selected_p
    del bpy.types.Scene.ob2_pmn_m
    del bpy.types.Scene.ob2_pmn_selected_m
    del bpy.types.Scene.ob2_pmn_n
    del bpy.types.Scene.ob2_pmn_selected_n
    del bpy.types.Scene.ob2_pmn_vector_p
    del bpy.types.Scene.ob2_pmn_vector_m
    del bpy.types.Scene.ob2_pmn_vector_n
    del bpy.types.Scene.ob2_pmn_color_p
    del bpy.types.Scene.ob2_pmn_color_m
    del bpy.types.Scene.ob2_pmn_color_n

    del bpy.types.Scene.ob2_color_value
    del bpy.types.Scene.ob2_color_rgb15
    del bpy.types.Scene.ob2_color_h
    del bpy.types.Scene.ob2_color_s
    del bpy.types.Scene.ob2_color_l
    del bpy.types.Scene.ob2_color_preview

    del bpy.types.Scene.ob2_vskin_foldout
    del bpy.types.Scene.ob2_tskin_foldout
    del bpy.types.Scene.ob2_pri_foldout
    del bpy.types.Scene.ob2_alpha_foldout

    del bpy.types.Scene.ob2_vskin_pick
    del bpy.types.Scene.ob2_tskin_pick
    del bpy.types.Scene.ob2_pri_pick
    del bpy.types.Scene.ob2_alpha_pick
    del bpy.types.Scene.ob2_pmn_pick
    del bpy.types.Scene.ob2_pmn_show_vertices
    del bpy.types.Scene.ob2_pmn_mapping_tools_foldout

    del bpy.types.Scene.ob2_vskin_label
    del bpy.types.Scene.ob2_tskin_label
    del bpy.types.Scene.ob2_pri_label
    del bpy.types.Scene.ob2_alpha_label

    for cls in classes:
        bpy.utils.unregister_class(cls)

    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)

    if _pmn_draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_pmn_draw_handler, 'WINDOW')
        _pmn_draw_handler = None

if __name__ == "__main__":
    register()