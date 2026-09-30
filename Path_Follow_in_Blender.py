# -*- coding: utf-8 -*-
"""
路径跟随（PathFollow）Blender 插件
作者: Xbman
描述: 选中路径和截面，路径为活动物体，选中后执行放样
"""

bl_info = {
    'name': 'Path_Follow_in_Blender',
    'author': 'Xbman',
    'description': '选中路径和截面，路径为活动物体，选中后执行放样',
    'blender': (2, 80, 0),
    'version': (1, 1, 0),
    'location': '3D视图 > 侧边栏 > 路径跟随标签页',
    'category': '网格',
}

import gpu
import bpy
import bmesh
import math
import json
import functools
import mathutils
from mathutils import Vector, Matrix
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

# ═══════════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════════
IS_UPDATING = False
IS_PROP_UPDATE = False
PENDING_UPDATE_NAMES = set()
PENDING_OBJECT_TRANSFORM_NAMES = set()
FORCE_REALIGN_EDITABLE_ON_UPDATE = False
FORCE_MAPPING_REALIGN_ON_UPDATE = False
SUPPRESS_PATH_MAPPING_UPDATE = False
PENDING_MAPPING_TARGET_NAMES = set()
LAST_MAPPING_UI_TARGET_NAME = None
RAIL_CALC_WORLD_SPACE = False
IS_MULTI_GENERATING = False
UPDATE_DELAY = 0.6
_internal_resolution = 24
RAIL_OPERATOR_TRANSACTION_DEPTH = 0
RAIL_FORCE_NO_UNDO = False
RAIL_UNDO_REDO_GUARD = False
RAIL_UNDO_REDO_RELEASE_TIMER = False
RAIL_POST_UNDO_REBUILD_REQUESTED = False

_RAIL_EXEC_CONTEXTS = {
    'INVOKE_REGION_CHANNELS', 'INVOKE_REGION_WIN', 'INVOKE_AREA',
    'INVOKE_REGION_PREVIEW', 'EXEC_AREA', 'EXEC_DEFAULT',
    'INVOKE_DEFAULT', 'EXEC_REGION_WIN', 'EXEC_SCREEN',
    'EXEC_REGION_CHANNELS', 'INVOKE_SCREEN', 'EXEC_REGION_PREVIEW',
}

# ═══════════════════════════════════════════════════════════
# 事务 / 无撤销调用
# ═══════════════════════════════════════════════════════════
def _rail_op(op, *args, **kwargs):
    no_undo = bool(RAIL_FORCE_NO_UNDO or RAIL_OPERATOR_TRANSACTION_DEPTH > 0)
    if not no_undo:
        return op(*args, **kwargs)
    call_args = list(args)
    execution_context = 'EXEC_DEFAULT'
    if call_args and isinstance(call_args[0], str) and call_args[0] in _RAIL_EXEC_CONTEXTS:
        execution_context = call_args.pop(0)
    if call_args and isinstance(call_args[0], bool):
        call_args.pop(0)
    return op(execution_context, False, *call_args, **kwargs)

def _rail_call_no_undo(op, *args, **kwargs):
    global RAIL_FORCE_NO_UNDO
    previous = RAIL_FORCE_NO_UNDO
    RAIL_FORCE_NO_UNDO = True
    try:
        return _rail_op(op, *args, **kwargs)
    finally:
        RAIL_FORCE_NO_UNDO = previous

def _rail_undo_transaction(func):
    @functools.wraps(func)
    def wrapped(self, context, *args, **kwargs):
        global RAIL_OPERATOR_TRANSACTION_DEPTH
        RAIL_OPERATOR_TRANSACTION_DEPTH += 1
        try:
            return func(self, context, *args, **kwargs)
        finally:
            RAIL_OPERATOR_TRANSACTION_DEPTH = max(0, RAIL_OPERATOR_TRANSACTION_DEPTH - 1)
    return wrapped

# ═══════════════════════════════════════════════════════════
# 撤销/重做守卫
# ═══════════════════════════════════════════════════════════
def _rail_cancel_pending_auto_update(clear_pending=True):
    global FORCE_REALIGN_EDITABLE_ON_UPDATE, IS_PROP_UPDATE, IS_UPDATING, FORCE_MAPPING_REALIGN_ON_UPDATE
    try:
        if bpy.app.timers.is_registered(run_update_operator):
            bpy.app.timers.unregister(run_update_operator)
    except Exception:
        pass
    IS_UPDATING = False
    IS_PROP_UPDATE = False
    FORCE_REALIGN_EDITABLE_ON_UPDATE = False
    FORCE_MAPPING_REALIGN_ON_UPDATE = False
    if clear_pending:
        try:
            PENDING_UPDATE_NAMES.clear()
            PENDING_OBJECT_TRANSFORM_NAMES.clear()
            PENDING_MAPPING_TARGET_NAMES.clear()
        except Exception:
            pass

def _rail_collect_all_rebuild_source_names(scene):
    names = set()
    if not scene:
        return names
    try:
        for obj in scene.objects:
            if not _is_rebuildable_generated_object(obj):
                continue
            p_name = obj.get('gen_profile_name', '')
            r_name = obj.get('gen_rail_name', '')
            if p_name: names.add(str(p_name))
            if r_name: names.add(str(r_name))
    except Exception:
        pass
    return names

def _rail_release_undo_redo_guard():
    global RAIL_UNDO_REDO_RELEASE_TIMER, RAIL_UNDO_REDO_GUARD
    global RAIL_POST_UNDO_REBUILD_REQUESTED, SUPPRESS_PATH_MAPPING_UPDATE
    RAIL_UNDO_REDO_RELEASE_TIMER = False
    RAIL_UNDO_REDO_GUARD = False
    SUPPRESS_PATH_MAPPING_UPDATE = False
    if not RAIL_POST_UNDO_REBUILD_REQUESTED:
        return
    RAIL_POST_UNDO_REBUILD_REQUESTED = False
    context = bpy.context
    scene = context.scene if context else None
    if not scene or not getattr(scene, 'rail_auto_update', False):
        return
    try:
        names = _rail_collect_all_rebuild_source_names(scene)
        if names:
            PENDING_UPDATE_NAMES.update(names)
            PENDING_OBJECT_TRANSFORM_NAMES.clear()
            PENDING_MAPPING_TARGET_NAMES.clear()
            trigger_auto_update_delayed(context, delay=0.01)
    except Exception as e:
        print(f'Undo/Redo 后路径跟随同步失败: {e}')

def _rail_finish_undo_redo(scene=None):
    global RAIL_UNDO_REDO_RELEASE_TIMER, RAIL_POST_UNDO_REBUILD_REQUESTED
    RAIL_POST_UNDO_REBUILD_REQUESTED = True
    try:
        if bpy.app.timers.is_registered(_rail_release_undo_redo_guard):
            bpy.app.timers.unregister(_rail_release_undo_redo_guard)
    except Exception:
        pass
    try:
        bpy.app.timers.register(_rail_release_undo_redo_guard, first_interval=0.08)
        RAIL_UNDO_REDO_RELEASE_TIMER = True
    except Exception:
        RAIL_UNDO_REDO_RELEASE_TIMER = False
        _rail_release_undo_redo_guard()

@persistent
def rail_undo_pre(*_args):
    global RAIL_UNDO_REDO_GUARD, SUPPRESS_PATH_MAPPING_UPDATE
    RAIL_UNDO_REDO_GUARD = True
    SUPPRESS_PATH_MAPPING_UPDATE = True
    _rail_cancel_pending_auto_update(clear_pending=True)

@persistent
def rail_undo_post(*_args):
    try: _SCENE_RUNTIME_CACHE.clear()
    except Exception: pass
    _rail_finish_undo_redo(_args[0] if _args else None)

@persistent
def rail_redo_pre(*_args):
    global RAIL_UNDO_REDO_GUARD, SUPPRESS_PATH_MAPPING_UPDATE
    RAIL_UNDO_REDO_GUARD = True
    SUPPRESS_PATH_MAPPING_UPDATE = True
    _rail_cancel_pending_auto_update(clear_pending=True)

@persistent
def rail_redo_post(*_args):
    try: _SCENE_RUNTIME_CACHE.clear()
    except Exception: pass
    _rail_finish_undo_redo(_args[0] if _args else None)

# ═══════════════════════════════════════════════════════════
# 运行时场景属性缓存
# ═══════════════════════════════════════════════════════════
_RUNTIME_SCENE_KEYS = {'punten_lijst', 'loc_oorsprong', 'start_tangent',
                       'richt_lijnen', 'norm_lijst', 'is_loop_calc', 'eindig',
                       'corner_rot_steps'}
_SCENE_RUNTIME_CACHE = {}

def _runtime_scene_id(scene):
    try: return int(scene.as_pointer())
    except Exception: return id(scene)

def _copy_runtime_value(value):
    if isinstance(value, Vector): return value.copy()
    if isinstance(value, Matrix): return value.copy()
    if isinstance(value, (list, tuple)):
        return [_copy_runtime_value(v) for v in value]
    return value

def _set_runtime_scene_prop(scene, key, value):
    if scene is None: return
    cache = _SCENE_RUNTIME_CACHE.setdefault(_runtime_scene_id(scene), {})
    cache[key] = _copy_runtime_value(value)
    try:
        if key in scene: del scene[key]
    except Exception:
        pass

def _get_runtime_scene_prop(scene, key, default=None):
    if scene is None: return default
    cache = _SCENE_RUNTIME_CACHE.get(_runtime_scene_id(scene), {})
    if key in cache:
        return _copy_runtime_value(cache[key])
    try: return _copy_runtime_value(scene.get(key, default))
    except Exception: return default

def _has_runtime_scene_prop(scene, key):
    if scene is None: return False
    cache = _SCENE_RUNTIME_CACHE.get(_runtime_scene_id(scene), {})
    if key in cache: return True
    try: return key in scene
    except Exception: return False

def _del_runtime_scene_prop(scene, key):
    if scene is None: return
    try: _SCENE_RUNTIME_CACHE.get(_runtime_scene_id(scene), {}).pop(key, None)
    except Exception: pass
    try:
        if key in scene: del scene[key]
    except Exception: pass

def _purge_unsafe_runtime_idprops(scene=None):
    try: scenes = [scene] if scene else list(bpy.data.scenes)
    except Exception: scenes = []
    for sc in scenes:
        if not sc: continue
        for key in _RUNTIME_SCENE_KEYS:
            try:
                if key in sc: del sc[key]
            except Exception:
                continue

# ═══════════════════════════════════════════════════════════
# 生成设置序列化
# ═══════════════════════════════════════════════════════════
_GEN_SETTINGS_DEFAULTS = {
    'mirror_x': False, 'mirror_y': False, 'rotate': 0, 'z_up': False,
    'flip_dir': True, 'cap_start': True, 'cap_end': True,
    'make_loop': False, 'flatten_start': False, 'flatten_end': False,
    'map_start': 0.0, 'map_end': 1.0,
    'corner_sharp': True, 'corner_angle': 30.0, 'corner_segments': 0,
    'corner_radius': 0.35,
}

def _coerce_gen_settings(raw=None):
    data = {}
    if raw:
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            data = {}
    out = dict(_GEN_SETTINGS_DEFAULTS)
    for key, default in _GEN_SETTINGS_DEFAULTS.items():
        value = data.get(key, default)
        try:
            if isinstance(default, bool): out[key] = bool(value)
            elif isinstance(default, int): out[key] = int(value)
            elif isinstance(default, float): out[key] = float(value)
            else: out[key] = value
        except Exception:
            out[key] = default
    out['map_start'] = max(0.0, min(1.0, float(out.get('map_start', 0.0))))
    out['map_end'] = max(0.0, min(1.0, float(out.get('map_end', 1.0))))
    return out

def _current_gen_settings_from_scene(scene):
    return _coerce_gen_settings({
        'mirror_x': scene.get('rail_mirror_x', False),
        'mirror_y': scene.get('rail_mirror_y', False),
        'rotate': scene.get('rail_profile_rotation', 0),
        'z_up': scene.get('rail_z_up', False),
        'flip_dir': scene.get('rail_flip_dir', True),
        'cap_start': scene.get('rail_cap_start', True),
        'cap_end': scene.get('rail_cap_end', True),
        'make_loop': scene.get('rail_make_loop', False),
        'flatten_start': scene.get('rail_flatten_start', False),
        'flatten_end': scene.get('rail_flatten_end', False),
        'map_start': _scene_float_prop(scene, 'rail_map_start', 0.0),
        'map_end': _scene_float_prop(scene, 'rail_map_end', 1.0),
        'corner_sharp': bool(getattr(scene, 'rail_corner_sharp', True)),
        'corner_angle': round(math.degrees(_scene_float_prop(
            scene, 'rail_corner_angle', math.radians(30.0))), 2),
        'corner_segments': int(getattr(scene, 'rail_corner_segments', 0) or 0),
        'corner_radius': _scene_float_prop(scene, 'rail_corner_radius', 0.35),
    })

def _has_gen_settings(obj):
    if not obj: return False
    try: return 'gen_settings_json' in obj or 'gen_settings' in obj
    except Exception: return False

def _get_gen_settings(obj):
    if not obj: return dict(_GEN_SETTINGS_DEFAULTS)
    raw = None
    try: raw = obj.get('gen_settings_json', None)
    except Exception: raw = None
    if raw is None:
        try: raw = obj.get('gen_settings', None)
        except Exception: raw = None
    return _coerce_gen_settings(raw)

def _set_gen_settings(obj, settings):
    if not obj: return
    safe = _coerce_gen_settings(settings)
    try:
        if 'gen_settings' in obj: del obj['gen_settings']
    except Exception: pass
    try:
        obj['gen_settings_json'] = json.dumps(safe, ensure_ascii=False, separators=(',', ':'))
    except Exception: pass

def _migrate_legacy_gen_settings():
    try: objects = list(bpy.data.objects)
    except Exception: objects = []
    for obj in objects:
        try:
            if 'gen_settings' in obj:
                settings = _get_gen_settings(obj)
                _set_gen_settings(obj, settings)
        except Exception:
            continue

def _direct_backup_available(obj):
    if not obj: return False
    backup_name = obj.get('gen_direct_backup_mesh')
    return bool(backup_name and bpy.data.meshes.get(backup_name))

def _is_editable_profile_source(obj):
    return bool(obj and obj.get('gen_editable_profile_source'))

def _remove_custom_props(obj, keys):
    if not obj: return
    expanded_keys = []
    for key in keys:
        expanded_keys.append(key)
        if key == 'gen_settings': expanded_keys.append('gen_settings_json')
    for key in expanded_keys:
        try:
            if key in obj: del obj[key]
        except Exception:
            continue

_STALE_EDITABLE_PROFILE_KEYS = [
    'gen_editable_profile_source', 'gen_source_generated_name', 'gen_rail_name',
    'gen_align_pos', 'gen_settings', 'gen_profile_anchor_world',
    'gen_profile_anchor_align_pos', 'gen_last_rail_matrix_world',
]

# ═══════════════════════════════════════════════════════════
# 物体关系判断
# ═══════════════════════════════════════════════════════════
def _find_live_generated_owner_for_profile(profile_obj):
    if not profile_obj: return None
    gen_name = profile_obj.get('gen_source_generated_name', '')
    if gen_name:
        try:
            gen_ob = bpy.data.objects.get(gen_name)
            if (gen_ob and gen_ob.type == 'MESH'
                    and gen_ob.get('gen_profile_name') == profile_obj.name
                    and gen_ob.get('gen_rail_name')):
                return gen_ob
        except Exception:
            pass
    try:
        scene = bpy.context.scene if bpy.context else None
        objects = scene.objects if scene else bpy.data.objects
        for obj in objects:
            if (obj and obj.type == 'MESH'
                    and obj.get('gen_profile_name') == profile_obj.name
                    and obj.get('gen_rail_name')):
                return obj
    except Exception:
        pass
    return None

def _cleanup_stale_editable_profile_state(obj):
    if not obj or getattr(obj, 'type', None) != 'MESH': return False
    if not obj.get('gen_editable_profile_source'): return False
    if _find_live_generated_owner_for_profile(obj): return False
    _remove_custom_props(obj, _STALE_EDITABLE_PROFILE_KEYS)
    try:
        scene = bpy.context.scene if bpy.context else None
        if scene:
            last_gen = scene.get('pre_last_generated', '')
            if last_gen and not bpy.data.objects.get(last_gen):
                scene['pre_last_generated'] = ''
                scene['pre_last_source'] = ''
                scene['pre_last_rail'] = ''
            if scene.get('pre_editable_profile_after_generate', '') == obj.name:
                del scene['pre_editable_profile_after_generate']
    except Exception:
        pass
    return True

def _is_generated_or_runtime_object(obj):
    if not obj: return True
    _cleanup_stale_editable_profile_state(obj)
    try:
        if obj.get('gen_profile_name'): return True
        if (obj.get('gen_direct_preview') or obj.get('gen_direct_inplace')
                or obj.get('gen_profile_consumed')): return True
        if obj.get('gen_editable_profile_source'): return True
        return False
    except Exception:
        return True

def _is_usable_profile_candidate(obj):
    if not obj or obj.type != 'MESH': return False
    if _is_generated_or_runtime_object(obj): return False
    if obj.get('gen_rail_name'): return False
    return True

def _is_usable_rail_candidate(obj, profile_obj=None):
    if not obj or obj == profile_obj or obj.type not in {'CURVE', 'MESH'}:
        return False
    if _is_generated_or_runtime_object(obj): return False
    return True

def _profile_candidate_score(obj):
    score = 0
    try:
        score += len(obj.data.polygons) * 10000
        score += len(obj.data.vertices)
    except Exception:
        pass
    return score

def _is_rebuildable_generated_object(obj):
    if not obj or obj.type != 'MESH': return False
    gen_p_name = obj.get('gen_profile_name')
    gen_r_name = obj.get('gen_rail_name')
    if not gen_p_name or not gen_r_name: return False
    if obj.get('gen_direct_preview'): return False
    if obj.get('gen_direct_inplace') or obj.get('gen_profile_consumed'):
        if not _direct_backup_available(obj): return False
    return True

def _collect_rebuild_targets(scene, changed_names):
    if not changed_names: return []
    changed_names = set(changed_names)
    targets = []
    for obj in scene.objects:
        if not _is_rebuildable_generated_object(obj): continue
        if (obj.get('gen_profile_name') in changed_names
                or obj.get('gen_rail_name') in changed_names):
            targets.append(obj)
    return targets

def _object_is_alive(obj):
    try: return bool(obj and obj.name and bpy.data.objects.get(obj.name) is obj)
    except Exception: return False

# ═══════════════════════════════════════════════════════════
# 选择解析
# ═══════════════════════════════════════════════════════════
def _get_path_active_selection(context, allow_multi=False):
    active = context.active_object
    selected_objs = [o for o in context.selected_objects if o.type in {'CURVE', 'MESH'}]
    for obj in list(selected_objs):
        _cleanup_stale_editable_profile_state(obj)

    if not active or active not in selected_objs or active.type not in {'CURVE', 'MESH'}:
        if allow_multi:
            return None, [], '多路径模式：请选中一个截面和多个路径，并把截面设为活动物体'
        return None, [], '请同时选择截面和路径，并把路径设为活动物体'

    if allow_multi and _is_usable_profile_candidate(active):
        profile_obj = active
        rail_objs = []
        for obj in selected_objs:
            if obj == profile_obj: continue
            if _is_usable_rail_candidate(obj, profile_obj=profile_obj):
                rail_objs.append(obj)
        if len(rail_objs) >= 2: return profile_obj, rail_objs, ''
        if len(rail_objs) == 1:
            return None, [], '多路径模式至少需要选择 2 条路径；单路径请把路径设为活动物体'
        return None, [], '多路径模式：请把截面设为活动物体，并同时选中多个路径 Mesh/Curve'

    if not _is_usable_rail_candidate(active):
        if allow_multi:
            return None, [], '多路径模式：活动物体应为截面 Mesh；或按旧用法把活动物体设为原始路径'
        return None, [], '活动物体必须是原始路径 Mesh/Curve，不能是放样物体或可编辑截面'

    profile_candidates = [o for o in selected_objs
                          if o != active and _is_usable_profile_candidate(o)]
    if not profile_candidates:
        if allow_multi:
            return None, [], '多路径模式：请把截面设为活动物体，并同时选中多个路径；或选中一个截面 Mesh 并把路径设为活动物体'
        return None, [], '请选中一个截面 Mesh，并把路径设为活动物体'

    profile_obj = max(profile_candidates, key=_profile_candidate_score)
    rails = [active]
    if allow_multi:
        for obj in selected_objs:
            if obj == active or obj == profile_obj: continue
            if _is_usable_rail_candidate(obj, profile_obj=profile_obj):
                rails.append(obj)
    return profile_obj, rails, ''

def _restore_user_active_object(context, obj, edit_mode=False):
    if not _object_is_alive(obj): return False
    try:
        if context.mode != 'OBJECT':
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
        _rail_op(bpy.ops.object.select_all, action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        if edit_mode:
            _rail_op(bpy.ops.object.mode_set, mode='EDIT')
    except Exception:
        return False
    return True

def _capture_edit_selection_state(obj):
    if not obj or obj.mode != 'EDIT': return None
    state = {'type': obj.type}
    if obj.type == 'MESH':
        try: state['mesh_select_mode'] = tuple(bpy.context.tool_settings.mesh_select_mode)
        except Exception: state['mesh_select_mode'] = None
        try:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()
            state['verts'] = [v.index for v in bm.verts if v.select]
            state['edges'] = [e.index for e in bm.edges if e.select]
            state['faces'] = [f.index for f in bm.faces if f.select]
        except Exception: pass
        return state
    if obj.type == 'CURVE':
        splines_state = []
        try:
            for sp in obj.data.splines:
                if sp.type == 'BEZIER':
                    pts = [(bool(p.select_left_handle), bool(p.select_control_point),
                            bool(p.select_right_handle)) for p in sp.bezier_points]
                    splines_state.append((sp.type, pts))
                else:
                    pts = [bool(p.select) for p in sp.points]
                    splines_state.append((sp.type, pts))
            state['splines'] = splines_state
        except Exception: pass
        return state
    return state

def _restore_edit_selection_state(context, obj, state):
    if not state or not _object_is_alive(obj): return False
    try:
        if context.mode == 'OBJECT' or context.active_object != obj:
            _restore_user_active_object(context, obj, edit_mode=True)
    except Exception:
        return False

    if obj.type == 'MESH' and state.get('type') == 'MESH':
        try:
            if context.mode != 'EDIT_MESH':
                if context.mode != 'OBJECT':
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                context.view_layer.objects.active = obj
                obj.select_set(True)
                _rail_op(bpy.ops.object.mode_set, mode='EDIT')
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()
            for f in bm.faces: f.select_set(False)
            for e in bm.edges: e.select_set(False)
            for v in bm.verts: v.select_set(False)
            for idx in state.get('verts', []):
                if idx < len(bm.verts): bm.verts[idx].select_set(True)
            for idx in state.get('edges', []):
                if idx < len(bm.edges): bm.edges[idx].select_set(True)
            for idx in state.get('faces', []):
                if idx < len(bm.faces): bm.faces[idx].select_set(True)
            try:
                if state.get('mesh_select_mode') is not None:
                    bpy.context.tool_settings.mesh_select_mode = state['mesh_select_mode']
            except Exception: pass
            try: bm.select_flush_mode()
            except Exception: pass
            bmesh.update_edit_mesh(obj.data)
        except Exception as e:
            print(f'恢复网格编辑选择状态失败: {e}'); return False
        return True

    if obj.type == 'CURVE' and state.get('type') == 'CURVE':
        try:
            if context.mode != 'EDIT_CURVE':
                if context.mode != 'OBJECT':
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                context.view_layer.objects.active = obj
                obj.select_set(True)
                _rail_op(bpy.ops.object.mode_set, mode='EDIT')
            saved_splines = state.get('splines', [])
            for sp_i, sp in enumerate(obj.data.splines):
                saved = saved_splines[sp_i][1] if sp_i < len(saved_splines) else []
                if sp.type == 'BEZIER':
                    for p_i, p in enumerate(sp.bezier_points):
                        vals = saved[p_i] if p_i < len(saved) else (False, False, False)
                        p.select_left_handle = bool(vals[0])
                        p.select_control_point = bool(vals[1])
                        p.select_right_handle = bool(vals[2])
                else:
                    for p_i, p in enumerate(sp.points):
                        p.select = bool(saved[p_i]) if p_i < len(saved) else False
            obj.data.update_tag()
        except Exception as e:
            print(f'恢复曲线编辑选择状态失败: {e}'); return False
        return True
    return False

# ═══════════════════════════════════════════════════════════
# 路径 / 修改器工具
# ═══════════════════════════════════════════════════════════
def find_active_rail(context):
    active = context.active_object
    if not active: return None
    if active.get('gen_rail_name'):
        rail_name = active['gen_rail_name']
        rail = bpy.data.objects.get(rail_name)
        if rail and rail.type == 'CURVE': return rail
    if active.type == 'CURVE': return active
    return None

def _get_modifier_identifier(modifier, input_name):
    if not modifier or not modifier.node_group: return None
    node_group = modifier.node_group
    if hasattr(node_group, 'interface'):
        for item in node_group.interface.items_tree:
            if item.name == input_name: return item.identifier
    elif hasattr(node_group, 'inputs'):
        for item in node_group.inputs:
            if item.name == input_name: return item.identifier
    return None

def _modifier_supports_idprops(modifier):
    try:
        modifier.keys()
    except TypeError:
        return False
    except Exception:
        return True
    return True

def _modifier_input_keys(modifier):
    props = getattr(modifier, 'properties', None)
    inputs = getattr(props, 'inputs', None) if props else None
    if inputs is not None:
        try: return list(inputs.keys())
        except Exception: pass
    if _modifier_supports_idprops(modifier):
        try: return list(modifier.keys())
        except Exception: pass
    return []

def get_modifier_input_value(modifier, identifier, default=None):
    if not modifier or not identifier: return default
    props = getattr(modifier, 'properties', None)
    inputs = getattr(props, 'inputs', None) if props else None
    if inputs is not None and identifier in inputs.keys():
        try: return getattr(inputs, identifier).value
        except Exception: return default
    if _modifier_supports_idprops(modifier) and identifier in modifier:
        try: return modifier[identifier]
        except Exception: return default
    return default

def set_modifier_input_value(modifier, identifier, value):
    if not modifier or not identifier: return False
    props = getattr(modifier, 'properties', None)
    inputs = getattr(props, 'inputs', None) if props else None
    if inputs is not None and identifier in inputs.keys():
        try:
            getattr(inputs, identifier).value = value
            return True
        except Exception:
            return False
    if _modifier_supports_idprops(modifier):
        try:
            modifier[identifier] = value
            return True
        except Exception:
            pass
    return False

def get_resolution_proxy(self):
    rail = find_active_rail(bpy.context)
    if rail:
        mod = rail.modifiers.get('Path_Resample')
        if mod and mod.node_group:
            identifier = _get_modifier_identifier(mod, 'Count')
            for key in (identifier, 'Count', 'Input_2'):
                value = get_modifier_input_value(mod, key)
                if value is not None: return value
    return _internal_resolution

def set_resolution_proxy(self, value):
    global _internal_resolution
    _internal_resolution = value
    if RAIL_UNDO_REDO_GUARD: return
    rail = find_active_rail(bpy.context)
    if rail:
        apply_resample_modifier(rail, value)
        if bpy.context.view_layer:
            bpy.context.view_layer.update()
        scene = bpy.context.scene
        has_linked_gen = False
        for obj in scene.objects:
            if obj.get('gen_rail_name') == rail.name:
                has_linked_gen = True; break
        if has_linked_gen:
            # 实时更新开启时，修改器变化本身会经依赖图处理器排一次重建，
            # 这里再排一次会导致每次改数值都重建两遍（表现为模型抖动/抽搐）
            auto_handled = bool(getattr(scene, 'rail_auto_update', False))
            if not auto_handled:
                trigger_auto_update_delayed(bpy.context, delay=0.01)

# ═══════════════════════════════════════════════════════════
# 核心自动更新
# ═══════════════════════════════════════════════════════════
def run_update_operator():
    global FORCE_REALIGN_EDITABLE_ON_UPDATE, IS_PROP_UPDATE, IS_UPDATING, FORCE_MAPPING_REALIGN_ON_UPDATE
    if RAIL_UNDO_REDO_GUARD:
        IS_UPDATING = False; return
    if not bpy.context or not bpy.context.view_layer:
        IS_UPDATING = False; return

    scene = bpy.context.scene
    active_ob = bpy.context.active_object

    trigger_names = set(PENDING_UPDATE_NAMES)
    transform_names = set(PENDING_OBJECT_TRANSFORM_NAMES)
    explicit_mapping_target_names = set(PENDING_MAPPING_TARGET_NAMES)

    PENDING_UPDATE_NAMES.clear()
    PENDING_OBJECT_TRANSFORM_NAMES.clear()
    PENDING_MAPPING_TARGET_NAMES.clear()

    if active_ob:
        try: trigger_names.add(active_ob.name)
        except Exception: pass

    IS_UPDATING = True

    if explicit_mapping_target_names:
        targets_to_update = []
        for name in explicit_mapping_target_names:
            obj = bpy.data.objects.get(name)
            if _is_rebuildable_generated_object(obj):
                targets_to_update.append(obj)
    else:
        targets_to_update = _collect_rebuild_targets(scene, trigger_names)
        if not targets_to_update and active_ob and _is_rebuildable_generated_object(active_ob):
            targets_to_update.append(active_ob)

    unique_targets = list(set(targets_to_update))
    if not unique_targets:
        IS_UPDATING = False; return

    for target_obj in unique_targets:
        try:
            r_name = target_obj.get('gen_rail_name')
            p_name = target_obj.get('gen_profile_name')
            rail_ob = bpy.data.objects.get(r_name)
            profile_ob = bpy.data.objects.get(p_name)
            if not (rail_ob and profile_ob): continue

            scene['pre_last_rail'] = r_name
            scene['pre_last_source'] = p_name
            scene['pre_last_generated'] = target_obj.name
            saved_align = target_obj.get('gen_align_pos', 'MM')

            if not IS_PROP_UPDATE and _has_gen_settings(target_obj):
                sett = _get_gen_settings(target_obj)
                scene['rail_mirror_x'] = sett.get('mirror_x', False)
                scene['rail_mirror_y'] = sett.get('mirror_y', False)
                scene['rail_profile_rotation'] = sett.get('rotate', 0)
                scene['rail_z_up'] = sett.get('z_up', False)
                scene['rail_flip_dir'] = sett.get('flip_dir', True)
                scene['rail_cap_start'] = sett.get('cap_start', True)
                scene['rail_cap_end'] = sett.get('cap_end', True)
                scene['rail_make_loop'] = sett.get('make_loop', False)
                try: scene.rail_flatten_start = bool(sett.get('flatten_start', False))
                except Exception: pass
                try: scene.rail_flatten_end = bool(sett.get('flatten_end', False))
                except Exception: pass
                try: scene.rail_corner_sharp = bool(sett.get('corner_sharp', True))
                except Exception: pass
                try:
                    scene.rail_corner_angle = math.radians(
                        float(sett.get('corner_angle', 30.0)))
                except Exception:
                    pass
                try: scene.rail_corner_segments = int(sett.get('corner_segments', 0) or 0)
                except Exception: pass
                try: scene.rail_corner_radius = float(sett.get('corner_radius', 0.35))
                except Exception: pass
                _set_scene_mapping_no_update(
                    scene,
                    sett.get('map_start', _scene_float_prop(scene, 'rail_map_start', 0.0)),
                    sett.get('map_end', _scene_float_prop(scene, 'rail_map_end', 1.0)))
                scene['stored_align_pos'] = saved_align

            path_object_transform_update = r_name in transform_names
            rail_is_currently_in_edit_mode = (
                active_ob is not None
                and getattr(active_ob, 'name', None) == r_name
                and bpy.context.mode in {'EDIT_CURVE', 'EDIT_MESH'})
            editable_copy_align_to_path = bool(
                (r_name in trigger_names and p_name not in trigger_names)
                or FORCE_MAPPING_REALIGN_ON_UPDATE)
            if path_object_transform_update:
                editable_copy_align_to_path = False

            if _is_editable_profile_source(profile_ob):
                if path_object_transform_update and not rail_is_currently_in_edit_mode:
                    _apply_rail_transform_delta_to_profile(profile_ob, rail_ob, target_obj)
                FORCE_REALIGN_EDITABLE_ON_UPDATE = False
            else:
                FORCE_REALIGN_EDITABLE_ON_UPDATE = r_name in trigger_names

            try:
                _rail_call_no_undo(bpy.ops.mesh.profiel_vlak,
                                   align_pos=saved_align,
                                   target_name=target_obj.name,
                                   align_editable_copy_to_path=editable_copy_align_to_path)
            except RuntimeError:
                pass
        except Exception as e:
            print(f'自动更新错误: {e}')
        finally:
            IS_UPDATING = False
            IS_PROP_UPDATE = False
            FORCE_MAPPING_REALIGN_ON_UPDATE = False

def trigger_auto_update_delayed(context, delay=0.5):
    if RAIL_UNDO_REDO_GUARD: return
    if IS_UPDATING: return
    if bpy.app.timers.is_registered(run_update_operator):
        bpy.app.timers.unregister(run_update_operator)
    bpy.app.timers.register(run_update_operator, first_interval=delay)

def rail_mapping_active_watch_timer():
    if RAIL_UNDO_REDO_GUARD: return 0.25
    try: _sync_mapping_panel_to_active(bpy.context)
    except Exception: pass
    return 0.25

@persistent
def rail_depsgraph_handler(scene, depsgraph):
    if RAIL_UNDO_REDO_GUARD: return
    try: _sync_mapping_panel_to_active(bpy.context)
    except Exception: pass
    if not scene.rail_auto_update: return
    if IS_UPDATING: return

    changed_names = set()
    transform_names = set()
    active = bpy.context.active_object
    active_changed = False

    for update in depsgraph.updates:
        try: original = update.id.original
        except Exception: original = update.id
        is_real_change = bool(update.is_updated_geometry or update.is_updated_transform)
        if isinstance(original, bpy.types.Object) and is_real_change:
            changed_names.add(original.name)
            if update.is_updated_transform:
                transform_names.add(original.name)
            if active and original == active:
                active_changed = True
        else:
            if active and hasattr(active, 'data') and original == active.data and is_real_change:
                active_changed = True
            if is_real_change and isinstance(original, (bpy.types.Mesh, bpy.types.Curve)):
                for obj in scene.objects:
                    try:
                        if obj.data == original:
                            changed_names.add(obj.name)
                    except Exception:
                        pass

    if active_changed and active:
        try: changed_names.add(active.name)
        except Exception: pass

    if not changed_names: return
    if _collect_rebuild_targets(scene, changed_names):
        PENDING_UPDATE_NAMES.update(changed_names)
        PENDING_OBJECT_TRANSFORM_NAMES.update(transform_names)
        trigger_auto_update_delayed(bpy.context, delay=UPDATE_DELAY)

# ═══════════════════════════════════════════════════════════
# 属性更新目标解析
# ═══════════════════════════════════════════════════════════
def _resolve_property_update_target(context):
    try:
        scene = context.scene
        active = context.active_object
    except Exception:
        return None
    if active:
        if _is_rebuildable_generated_object(active): return active
        gen_name = active.get('gen_source_generated_name', '')
        gen_ob = bpy.data.objects.get(gen_name) if gen_name else None
        if _is_rebuildable_generated_object(gen_ob): return gen_ob
    try:
        pre = bpy.data.objects.get(scene.get('pre_last_generated', ''))
        if _is_rebuildable_generated_object(pre):
            if pre.name == active.name: return pre
            if (pre.get('gen_profile_name') == active.name
                    or pre.get('gen_rail_name') == active.name):
                return pre
    except Exception: pass
    try:
        candidates = []
        for obj in scene.objects:
            if not _is_rebuildable_generated_object(obj): continue
            if (obj.get('gen_profile_name') == active.name
                    or obj.get('gen_rail_name') == active.name):
                candidates.append(obj)
        if len(candidates) == 1: return candidates[0]
    except Exception: pass
    return None

def update_gen_mesh(self, context):
    global IS_PROP_UPDATE
    if RAIL_UNDO_REDO_GUARD: return
    scene = context.scene
    target = _resolve_property_update_target(context)
    if scene.rail_auto_update:
        IS_PROP_UPDATE = True
        if target:
            try:
                PENDING_MAPPING_TARGET_NAMES.add(target.name)
                PENDING_UPDATE_NAMES.add(target.name)
            except Exception: pass
        trigger_auto_update_delayed(context, delay=0.1)
        return
    if target:
        last_align = target.get('gen_align_pos', scene.get('stored_align_pos', 'MM'))
        scene['pre_last_generated'] = target.name
        scene['pre_last_source'] = target.get('gen_profile_name', scene.get('pre_last_source', ''))
        scene['pre_last_rail'] = target.get('gen_rail_name', scene.get('pre_last_rail', ''))
        try:
            _rail_call_no_undo(bpy.ops.mesh.profiel_vlak,
                               align_pos=last_align,
                               target_name=target.name,
                               align_editable_copy_to_path=False)
        except Exception as e:
            print(f'属性更新重建失败: {e}')

def _scene_float_prop(scene, prop_name, default=0.0):
    try: return float(getattr(scene, prop_name))
    except Exception:
        try: return float(scene.get(prop_name, default))
        except Exception: return float(default)

# ═══════════════════════════════════════════════════════════
# 映射（mapping）逻辑
# ═══════════════════════════════════════════════════════════
def _is_mapping_controlled_generated_object(obj):
    if not obj or obj.type != 'MESH': return False
    if obj.get('gen_direct_preview'): return False
    try: return bool(obj.get('gen_rail_name') and obj.get('gen_profile_name'))
    except Exception: return False

def _mapping_settings_from_target(target):
    start_v, end_v = 0.0, 1.0
    try:
        sett = _get_gen_settings(target) if target else {}
        start_v = float(sett.get('map_start', 0.0))
        end_v = float(sett.get('map_end', 1.0))
    except Exception:
        start_v, end_v = 0.0, 1.0
    start_v = max(0.0, min(1.0, start_v))
    end_v = max(0.0, min(1.0, end_v))
    if end_v < start_v: start_v, end_v = end_v, start_v
    if end_v - start_v < 0.001:
        end_v = min(1.0, start_v + 0.001)
        if end_v > 1.0: start_v = max(0.0, end_v - 0.001)
    return start_v, end_v

def _set_scene_mapping_no_update(scene, start_v=0.0, end_v=1.0):
    global SUPPRESS_PATH_MAPPING_UPDATE
    SUPPRESS_PATH_MAPPING_UPDATE = True
    try:
        scene.rail_map_start = max(0.0, min(1.0, float(start_v)))
        scene.rail_map_end = max(0.0, min(1.0, float(end_v)))
    except Exception:
        try:
            scene['rail_map_start'] = max(0.0, min(1.0, float(start_v)))
            scene['rail_map_end'] = max(0.0, min(1.0, float(end_v)))
        except Exception: pass
    finally:
        SUPPRESS_PATH_MAPPING_UPDATE = False

def _reset_scene_mapping_for_new_loft(scene):
    _set_scene_mapping_no_update(scene, 0.0, 1.0)

def _resolve_mapping_target_from_object(scene, obj):
    if not obj: return None
    if _is_mapping_controlled_generated_object(obj): return obj
    gen_name = obj.get('gen_source_generated_name', '')
    gen_ob = bpy.data.objects.get(gen_name) if gen_name else None
    if _is_mapping_controlled_generated_object(gen_ob): return gen_ob
    try:
        pre = bpy.data.objects.get(scene.get('pre_last_generated', ''))
        if (_is_mapping_controlled_generated_object(pre)
                and (pre.get('gen_profile_name') == obj.name
                     or pre.get('gen_rail_name') == obj.name)):
            return pre
    except Exception: pass
    try:
        candidates = []
        for candidate in scene.objects:
            if not _is_mapping_controlled_generated_object(candidate): continue
            if (candidate.get('gen_profile_name') == obj.name
                    or candidate.get('gen_rail_name') == obj.name):
                candidates.append(candidate)
        if len(candidates) == 1: return candidates[0]
    except Exception: pass
    return None

def _resolve_active_mapping_target(context):
    scene = context.scene
    active = context.active_object
    target = _resolve_mapping_target_from_object(scene, active)
    if target: return target
    try:
        pre = bpy.data.objects.get(scene.get('pre_last_generated', ''))
        if _is_mapping_controlled_generated_object(pre): return pre
    except Exception: pass
    return None

def _sync_mapping_panel_to_active(context, force=False):
    global LAST_MAPPING_UI_TARGET_NAME
    if not context or not getattr(context, 'scene', None): return
    if (RAIL_UNDO_REDO_GUARD or IS_UPDATING or IS_PROP_UPDATE
            or SUPPRESS_PATH_MAPPING_UPDATE): return
    scene = context.scene
    target = _resolve_active_mapping_target(context)
    target_name = target.name if target else ''
    if not force and target_name == LAST_MAPPING_UI_TARGET_NAME: return
    LAST_MAPPING_UI_TARGET_NAME = target_name
    if target:
        start_v, end_v = _mapping_settings_from_target(target)
        _set_scene_mapping_no_update(scene, start_v, end_v)
    else:
        _set_scene_mapping_no_update(scene, 0.0, 1.0)

def _find_mapping_rebuild_targets(context):
    target = _resolve_active_mapping_target(context)
    if target and _is_rebuildable_generated_object(target): return [target]
    return []

def update_path_mapping(self, context):
    global IS_PROP_UPDATE, FORCE_MAPPING_REALIGN_ON_UPDATE, LAST_MAPPING_UI_TARGET_NAME
    if RAIL_UNDO_REDO_GUARD or SUPPRESS_PATH_MAPPING_UPDATE: return
    scene = context.scene
    targets = _find_mapping_rebuild_targets(context)
    if not targets: return
    start_v = max(0.0, min(1.0, _scene_float_prop(scene, 'rail_map_start', 0.0)))
    end_v = max(0.0, min(1.0, _scene_float_prop(scene, 'rail_map_end', 1.0)))
    min_span = 0.001
    if end_v <= start_v + min_span:
        if start_v < 1.0 - min_span:
            end_v = start_v + min_span
        else:
            start_v = max(0.0, end_v - min_span)
        _set_scene_mapping_no_update(scene, start_v, end_v)
    for obj in targets:
        try:
            sett = _get_gen_settings(obj)
            sett['map_start'] = float(start_v)
            sett['map_end'] = float(end_v)
            _set_gen_settings(obj, sett)
            LAST_MAPPING_UI_TARGET_NAME = obj.name
        except Exception: pass
    FORCE_MAPPING_REALIGN_ON_UPDATE = True
    IS_PROP_UPDATE = True
    for obj in targets:
        try:
            PENDING_MAPPING_TARGET_NAMES.add(obj.name)
            PENDING_UPDATE_NAMES.add(obj.name)
        except Exception: pass
    if scene.rail_auto_update:
        trigger_auto_update_delayed(context, delay=0.01)
    else:
        for obj in targets:
            try:
                scene['pre_last_rail'] = obj.get('gen_rail_name', '')
                scene['pre_last_source'] = obj.get('gen_profile_name', '')
                scene['pre_last_generated'] = obj.name
                _rail_call_no_undo(bpy.ops.mesh.profiel_vlak,
                                   align_pos=obj.get('gen_align_pos', scene.get('stored_align_pos', 'MM')),
                                   target_name=obj.name,
                                   align_editable_copy_to_path=True)
            except Exception as e:
                print(f'路径映射更新失败: {e}')
        IS_PROP_UPDATE = False
        FORCE_MAPPING_REALIGN_ON_UPDATE = False
        PENDING_MAPPING_TARGET_NAMES.clear()

# ═══════════════════════════════════════════════════════════
# 路径映射几何
# ═══════════════════════════════════════════════════════════
def _append_unique_point(points, point, eps=1e-07):
    p = Vector(point)
    if not points or (p - points[-1]).length > eps:
        points.append(p)

def _calc_open_path_normals(points):
    if len(points) < 2: return [], []
    clean_points = [Vector(points[0])]
    for p in points[1:]:
        p = Vector(p)
        if (p - clean_points[-1]).length > 1e-07:
            clean_points.append(p)
    if len(clean_points) < 2: return [], []
    segs = []
    for i in range(len(clean_points) - 1):
        seg = clean_points[i + 1] - clean_points[i]
        if seg.length > 1e-07: segs.append(seg)
    if not segs: return [], []
    normals = [segs[0].normalized()]
    for i in range(len(segs) - 1):
        v1 = segs[i].normalized()
        v2 = segs[i + 1].normalized()
        avg = v1 + v2
        if avg.length < 1e-07: avg = v2
        normals.append(avg.normalized())
    normals.append(segs[-1].normalized())
    return clean_points, normals

def _sharpen_corners(points, normals, vecs, angle_threshold_rad, is_looping=False):
    """拐角锐化：拐角重建（snap 已在上游完成）后，拐角处使用平分斜接
    （标准弯头，左右对称、水密无缺口）。此函数只需透传，
    因为对称性由"弦平分法向 + 纯平移投影"机制保证。
    返回 (points, normals, vecs, None)。"""
    return points, normals, vecs, None

def _round_path_corners(points, normals, vecs, angle_threshold_rad,
                        arc_segments, radius_factor=0.35, is_looping=False):
    """拐角圆角：把尖角替换为一段圆弧（圆角/倒角）。
    arc_segments 为圆弧分段数，radius_factor 为圆角半径占较短邻段长度的比例。
    只处理转角局部峰值点，且圆角之间保持最小间距，避免连续多点各自触发
    圆角导致弧段堆叠（扇形堆积）。
    返回 (points, normals, vecs, None)。"""
    try: arc_segments = int(arc_segments)
    except Exception: return points, normals, vecs, None
    try: radius_factor = float(radius_factor)
    except Exception: radius_factor = 0.35
    if arc_segments <= 0:
        return points, normals, vecs, None
    n_pts = len(points)
    if n_pts < 3 or len(vecs) < 2 or len(normals) != n_pts:
        return points, normals, vecs, None
    try: thr = float(angle_threshold_rad)
    except Exception: thr = math.radians(30.0)
    radius_factor = max(0.02, min(0.45, radius_factor))
    # 1) 计算每个内部点的转角
    turns = []
    for i in range(1, n_pts - 1):
        v_in = Vector(vecs[i - 1])
        v_out = Vector(vecs[i])
        l_in = v_in.length
        l_out = v_out.length
        t = -1.0
        if l_in > 1e-09 and l_out > 1e-09:
            try: t = (v_in / l_in).angle(v_out / l_out, 0.0)
            except Exception: t = -1.0
        turns.append(t)
    # 2) 选出拐角点：局部峰值 + 最小间距（避免相邻多点连续触发）。
    #    圆角切点最多吃掉邻段的 49%，因此相距 >=2 的拐角不会重叠，
    #    间距只需 2（防止紧邻两点重复触发）。
    corner_indices = []
    handled_until = -1
    min_gap = 2
    for idx, i in enumerate(range(1, n_pts - 1)):
        if i <= handled_until:
            continue
        t = turns[idx]
        if t < thr or t <= 1e-06 or t >= math.pi - 1e-06:
            continue
        t_prev = turns[idx - 1] if idx > 0 else -1.0
        t_next = turns[idx + 1] if idx + 1 < len(turns) else -1.0
        if t < t_prev or t < t_next:
            continue  # 不是局部峰值，交给邻近峰值点处理
        corner_indices.append(i)
        handled_until = i + min_gap
    if not corner_indices:
        return points, normals, vecs, None
    corner_set = set(corner_indices)
    # 3) 生成圆弧
    new_points = [Vector(points[0])]
    new_normals = [Vector(normals[0])]
    for i in range(1, n_pts - 1):
        if i not in corner_set:
            new_points.append(Vector(points[i]))
            new_normals.append(Vector(normals[i]))
            continue
        v_in = Vector(vecs[i - 1])
        v_out = Vector(vecs[i])
        l_in = v_in.length
        l_out = v_out.length
        d_in = v_in / l_in
        d_out = v_out / l_out
        turn = (d_in).angle(d_out, 0.0)
        half = turn * 0.5
        r = radius_factor * min(l_in, l_out)
        t = r / math.tan(half) if half > 1e-09 else 0.0
        t = min(t, l_in * 0.49, l_out * 0.49)
        r_eff = t * math.tan(half)
        if r_eff <= 1e-09:
            new_points.append(Vector(points[i]))
            new_normals.append(Vector(normals[i]))
            continue
        corner = Vector(points[i])
        t1 = corner - d_in * t          # 圆弧起点（进入侧切点）
        # 圆心位于弯折内侧：内角平分方向 = normalize(d_out - d_in)
        center_dir = d_out - d_in
        cl = center_dir.length
        if cl < 1e-07:
            new_points.append(Vector(points[i]))
            new_normals.append(Vector(normals[i]))
            continue
        center = corner + (center_dir / cl) * (r_eff / math.sin(half) if math.sin(half) > 1e-09 else 0.0)
        axis = d_in.cross(d_out)
        al = axis.length
        if al < 1e-09:
            new_points.append(Vector(points[i]))
            new_normals.append(Vector(normals[i]))
            continue
        axis = axis / al
        start_rel = t1 - center
        for k in range(arc_segments + 1):
            phi = turn * k / arc_segments
            rot = Matrix.Rotation(phi, 3, axis)
            p_k = center + rot @ start_rel
            new_points.append(Vector(p_k))
            new_normals.append(Vector(normals[i]))
    new_points.append(Vector(points[-1]))
    new_normals.append(Vector(normals[-1]))
    new_vecs = [new_points[j + 1] - new_points[j] for j in range(len(new_points) - 1)]
    # 内部法向统一按"相邻弦平分"重算（与基础放样规则一致）：
    # 保证扫掠投影为纯平移，无剪切累积，左右镜像对称
    for idx in range(1, len(new_points) - 1):
        a = new_vecs[idx - 1]
        b = new_vecs[idx]
        la = a.length
        lb = b.length
        if la <= 1e-09 and lb <= 1e-09:
            continue
        if la <= 1e-09:
            new_normals[idx] = Vector(b / lb)
            continue
        if lb <= 1e-09:
            new_normals[idx] = Vector(a / la)
            continue
        avg = (a / la) + (b / lb)
        if avg.length < 1e-07:
            avg = b / lb
        new_normals[idx] = Vector(avg.normalized())
    if is_looping and len(new_normals) >= 2:
        new_normals[-1] = Vector(new_normals[0])
    return new_points, new_normals, new_vecs, None

def _snap_corners_to_intersections(points, vecs, angle_threshold_rad):
    """拐角重建：重采样点几乎不会恰好落在真实拐角上（折线在拐角处是斜切的），
    导致锐化/圆角的基准点偏离真实拐角、左右不对称。
    这里用拐角点相邻"直线段"的延长线交点把拐角点校正回真实拐角位置。
    参考方向优先取更远的相邻段（拐角跨越段的方向被采样斜化，不可靠）。
    返回 (points, vecs)。"""
    n = len(points)
    if n < 5 or len(vecs) != n - 1:
        return points, vecs
    try: thr = float(angle_threshold_rad)
    except Exception: thr = math.radians(30.0)
    new_points = [Vector(p) for p in points]
    changed = False
    for i in range(1, n - 1):
        v_in = Vector(vecs[i - 1])
        v_out = Vector(vecs[i])
        l_in = v_in.length
        l_out = v_out.length
        if l_in <= 1e-09 or l_out <= 1e-09:
            continue
        d_prev = v_in / l_in
        d_next = v_out / l_out
        try: turn = d_prev.angle(d_next, 0.0)
        except Exception: turn = 0.0
        if turn < thr or turn <= 1e-06:
            continue
        # 参考方向：优先取相邻的更远段（若与近段近似共线，说明是直线延续）
        ref_lim = min(math.radians(30.0), turn * 0.6)
        d_in_ref = d_prev
        if i >= 2:
            d2 = Vector(vecs[i - 2])
            if d2.length > 1e-09:
                d2 = d2 / d2.length
                try:
                    if d2.angle(d_prev, 0.0) < ref_lim: d_in_ref = d2
                except Exception: pass
        d_out_ref = d_next
        if i <= n - 3:
            d2 = Vector(vecs[i + 1])
            if d2.length > 1e-09:
                d2 = d2 / d2.length
                try:
                    if d2.angle(d_next, 0.0) < ref_lim: d_out_ref = d2
                except Exception: pass
        try:
            ref_turn = d_in_ref.angle(d_out_ref, 0.0)
        except Exception:
            continue
        if ref_turn < thr or ref_turn >= math.pi - 1e-06:
            continue
        p_a = Vector(points[i - 1])
        p_b = Vector(points[i + 1])
        far = (l_in + l_out) * 10.0 + 1.0
        try:
            hits = mathutils.geometry.intersect_line_line(
                p_a, p_a + d_in_ref * far, p_b - d_out_ref * far, p_b)
        except Exception:
            hits = None
        if not hits:
            continue
        x_pt = (Vector(hits[0]) + Vector(hits[1])) * 0.5
        # 合理性：交点不能偏离原拐角点太远
        if (x_pt - Vector(points[i])).length > (l_in + l_out) * 1.5 + 1e-06:
            continue
        new_points[i] = x_pt
        changed = True
    if not changed:
        return points, vecs
    new_vecs = [new_points[j + 1] - new_points[j] for j in range(len(new_points) - 1)]
    return new_points, new_vecs

def _apply_corner_treatment(points, normals, vecs, angle_threshold_rad=math.radians(30.0),
                           extra_segments=0, do_miter=True, radius_factor=0.35, is_looping=False):
    """拐角处理总入口：
    1) 拐角重建：把偏离真实拐角的采样点校正到直线段延长线交点上
    2) 拐角分段 > 0：拐角变为圆弧过渡（分段数即圆弧精度）
    3) 拐角分段 = 0 且开启锐化：拐角为对称旋转斜切锐边
    返回 (points, normals, vecs, rot_steps)"""
    points, vecs = _snap_corners_to_intersections(points, vecs, angle_threshold_rad)
    if extra_segments and int(extra_segments or 0) > 0:
        return _round_path_corners(
            points, normals, vecs, angle_threshold_rad,
            int(extra_segments or 0), radius_factor, is_looping)
    if do_miter:
        return _sharpen_corners(points, normals, vecs, angle_threshold_rad, is_looping)
    return points, normals, vecs, None

def _estimate_profile_mapping_safe_distance(profile_obj):
    if not profile_obj or getattr(profile_obj, 'type', None) != 'MESH': return 0.0
    try:
        coords = [profile_obj.matrix_world @ v.co for v in profile_obj.data.vertices]
        if not coords: return 0.0
        min_v = Vector((min(p.x for p in coords), min(p.y for p in coords), min(p.z for p in coords)))
        max_v = Vector((max(p.x for p in coords), max(p.y for p in coords), max(p.z for p in coords)))
        diag = (max_v - min_v).length
        if diag <= 1e-08: return 0.0
        return diag * 0.55
    except Exception:
        return 0.0

def _angle_between_segments(a, b):
    if a.length <= 1e-08 or b.length <= 1e-08: return 0.0
    try: return a.normalized().angle(b.normalized(), 0.0)
    except Exception: return 0.0

def _sanitize_mapped_points_for_corner_overlap(mapped_points, total_len, profile_safe_distance=0.0):
    pts = [Vector(p) for p in mapped_points]
    if len(pts) < 3: return pts
    base_tol = max(total_len * 0.0008, 1e-06)
    safe = max(base_tol, float(profile_safe_distance or 0.0))
    max_abs_safe = max(total_len * 0.08, base_tol)
    safe = min(safe, max_abs_safe)
    angle_eps = math.radians(3.0)
    changed = True
    loop_guard = 0
    while changed and loop_guard < 4 and len(pts) >= 3:
        loop_guard += 1
        changed = False
        first_len = (pts[1] - pts[0]).length
        first_turn = _angle_between_segments(pts[1] - pts[0], pts[2] - pts[1])
        first_limit = min(safe, max(base_tol, (pts[2] - pts[1]).length * 0.45))
        if first_turn > angle_eps and first_len <= first_limit:
            pts.pop(0); changed = True; break
        last_len = (pts[-1] - pts[-2]).length
        last_turn = _angle_between_segments(pts[-2] - pts[-3], pts[-1] - pts[-2])
        last_limit = min(safe, max(base_tol, (pts[-2] - pts[-3]).length * 0.45))
        if last_turn > angle_eps and last_len <= last_limit:
            pts.pop(); changed = True
    return pts

def _apply_path_mapping_to_scene(scene, profile_obj=None):
    try:
        start_factor = _scene_float_prop(scene, 'rail_map_start', 0.0)
        end_factor = _scene_float_prop(scene, 'rail_map_end', 1.0)
    except Exception:
        return False
    start_factor = max(0.0, min(1.0, start_factor))
    end_factor = max(0.0, min(1.0, end_factor))
    min_span = 0.001
    if end_factor <= start_factor + min_span:
        if start_factor < 1.0 - min_span:
            end_factor = start_factor + min_span
        else:
            start_factor = max(0.0, end_factor - min_span)
    if start_factor <= 1e-06 and end_factor >= 0.999999:
        return False
    try:
        original_points = [Vector(p) for p in _get_runtime_scene_prop(scene, 'punten_lijst', [])]
    except Exception:
        return False
    if len(original_points) < 2: return False
    points = [original_points[0]]
    for p in original_points[1:]:
        if (p - points[-1]).length > 1e-07:
            points.append(p)
    if len(points) < 2: return False
    seg_lengths = []
    total_len = 0.0
    for i in range(len(points) - 1):
        length = (points[i + 1] - points[i]).length
        seg_lengths.append(length)
        total_len += length
    if total_len <= 1e-07: return False
    start_dist = total_len * start_factor
    end_dist = total_len * end_factor
    cumulative = [0.0]
    acc = 0.0
    for length in seg_lengths:
        acc += length
        cumulative.append(acc)

    def point_at_distance(distance):
        distance = max(0.0, min(total_len, distance))
        acc_local = 0.0
        for i, length in enumerate(seg_lengths):
            next_acc = acc_local + length
            if distance <= next_acc or i == len(seg_lengths) - 1:
                if length <= 1e-07:
                    return points[i].copy(), i
                t = (distance - acc_local) / length
                return points[i].lerp(points[i + 1], max(0.0, min(1.0, t))), i
            acc_local = next_acc
        return points[-1].copy(), len(seg_lengths) - 1

    start_point, start_seg_index = point_at_distance(start_dist)
    end_point, end_seg_index = point_at_distance(end_dist)
    mapped_points = []
    _append_unique_point(mapped_points, start_point)
    for point_index in range(start_seg_index + 1, end_seg_index + 1):
        if 0 <= point_index < len(points):
            point_distance = cumulative[point_index]
            if start_dist + 1e-07 < point_distance < end_dist - 1e-07:
                _append_unique_point(mapped_points, points[point_index])
    _append_unique_point(mapped_points, end_point)
    mapped_points = _sanitize_mapped_points_for_corner_overlap(
        mapped_points, total_len, _estimate_profile_mapping_safe_distance(profile_obj))
    mapped_points, mapped_normals = _calc_open_path_normals(mapped_points)
    if len(mapped_points) < 2 or len(mapped_normals) != len(mapped_points):
        return False
    mapped_vecs = [mapped_points[i + 1] - mapped_points[i] for i in range(len(mapped_points) - 1)]
    if not mapped_vecs: return False
    try:
        flatten_start = bool(getattr(scene, 'rail_flatten_start', False))
        flatten_end = bool(getattr(scene, 'rail_flatten_end', False))

        def get_snapped_normal(tangent_vec):
            x, y, z = abs(tangent_vec.x), abs(tangent_vec.y), abs(tangent_vec.z)
            if x >= y and x >= z:
                return Vector((1, 0, 0)) if tangent_vec.x > 0 else Vector((-1, 0, 0))
            if y >= x and y >= z:
                return Vector((0, 1, 0)) if tangent_vec.y > 0 else Vector((0, -1, 0))
            return Vector((0, 0, 1)) if tangent_vec.z > 0 else Vector((0, 0, -1))

        if flatten_start and len(mapped_vecs) >= 1:
            mapped_normals[0] = get_snapped_normal(mapped_vecs[0].normalized())
        if flatten_end and len(mapped_vecs) >= 1:
            mapped_normals[-1] = get_snapped_normal(mapped_vecs[-1].normalized())
    except Exception: pass
    mapped_rot_steps = None
    if getattr(scene, 'rail_corner_sharp', True) or int(getattr(scene, 'rail_corner_segments', 0) or 0) > 0:
        try:
            mapped_points, mapped_normals, mapped_vecs, mapped_rot_steps = _apply_corner_treatment(
                mapped_points, mapped_normals, mapped_vecs,
                _scene_float_prop(scene, 'rail_corner_angle', math.radians(30.0)),
                int(getattr(scene, 'rail_corner_segments', 0) or 0),
                bool(getattr(scene, 'rail_corner_sharp', True)),
                _scene_float_prop(scene, 'rail_corner_radius', 0.35),
                False)
        except Exception:
            mapped_rot_steps = None
    _set_runtime_scene_prop(scene, 'corner_rot_steps', mapped_rot_steps)
    _set_runtime_scene_prop(scene, 'punten_lijst', mapped_points)
    _set_runtime_scene_prop(scene, 'norm_lijst', mapped_normals)
    _set_runtime_scene_prop(scene, 'richt_lijnen', mapped_vecs)
    _set_runtime_scene_prop(scene, 'eindig', True)
    _set_runtime_scene_prop(scene, 'is_loop_calc', False)
    _set_runtime_scene_prop(scene, 'start_tangent', mapped_vecs[0].normalized())
    return True

# ═══════════════════════════════════════════════════════════
# 重采样 / 备份
# ═══════════════════════════════════════════════════════════
def ensure_resample_node_group():
    group_name = 'Rail_Resample_Pro'
    if group_name in bpy.data.node_groups:
        return bpy.data.node_groups[group_name]
    group = bpy.data.node_groups.new(name=group_name, type='GeometryNodeTree')
    nodes = group.nodes
    links = group.links
    if hasattr(group, 'interface'):
        group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
        group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
        in_count = group.interface.new_socket(name='Count', in_out='INPUT', socket_type='NodeSocketInt')
        in_count.default_value = 24
        in_count.min_value = 2
        in_count.max_value = 100000
    else:
        group.outputs.new('NodeSocketGeometry', 'Geometry')
        group.inputs.new('NodeSocketGeometry', 'Geometry')
        in_count = group.inputs.new('NodeSocketInt', 'Count')
        in_count.default_value = 24
        in_count.min_value = 1
        in_count.max_value = 100000
    node_group_input = nodes.new('NodeGroupInput')
    node_group_input.location = (-340.0, 0.0)
    node_group_output = nodes.new('NodeGroupOutput')
    node_group_output.location = (200.0, 0.0)
    node_resample = nodes.new('GeometryNodeResampleCurve')
    node_resample.location = (-100.0, 0.0)
    if hasattr(node_resample, 'mode'):
        try: node_resample.mode = 'COUNT'
        except Exception: pass
    links.new(node_group_input.outputs[0], node_resample.inputs[0])
    count_socket = None
    for socket in node_resample.inputs:
        if socket.identifier == 'Count' or socket.name == 'Count':
            count_socket = socket; break
    if not count_socket and len(node_resample.inputs) > 2:
        count_socket = node_resample.inputs[2]
    if not count_socket:
        for socket in node_resample.inputs:
            if socket.type == 'INT':
                count_socket = socket; break
    if count_socket:
        links.new(node_group_input.outputs[1], count_socket)
    links.new(node_resample.outputs[0], node_group_output.inputs[0])
    return group

def apply_resample_modifier(obj, resolution_count):
    if obj.type != 'CURVE': return
    mod_name = 'Path_Resample'
    mod = obj.modifiers.get(mod_name)
    if not mod:
        mod = obj.modifiers.new(name=mod_name, type='NODES')
    node_group = ensure_resample_node_group()
    mod.node_group = node_group
    try:
        identifier = _get_modifier_identifier(mod, 'Count')
        # 数值未变化时不写入：写一次会标记依赖图，导致"重建→再触发重建"的抖动回环
        if identifier:
            current = get_modifier_input_value(mod, identifier)
            if current is not None and int(current) == int(resolution_count):
                return
        updated = False
        if identifier and set_modifier_input_value(mod, identifier, resolution_count):
            updated = True
        if not updated:
            for key in _modifier_input_keys(mod):
                if (key not in ('name', 'show_viewport', 'show_render')
                        and (key == 'Count' or (key.startswith('Input') and isinstance(get_modifier_input_value(mod, key), int)))):
                    if set_modifier_input_value(mod, key, resolution_count):
                        updated = True; break
        if not updated:
            set_modifier_input_value(mod, 'Count', resolution_count)
    except Exception as e:
        print(f'设置采样数值出错: {e}')
    obj.update_tag()

def ensure_direct_profile_backup(obj):
    if not obj or obj.type != 'MESH': return
    backup_name = obj.get('gen_direct_backup_mesh')
    if backup_name and bpy.data.meshes.get(backup_name): return
    backup = obj.data.copy()
    backup.name = f'{obj.name}_直接定位_原始截面备份'
    obj['gen_direct_backup_mesh'] = backup.name
    obj['gen_direct_backup_matrix'] = _matrix_to_flat_list(obj.matrix_world.copy())

def restore_direct_profile_backup(obj):
    if not obj or obj.type != 'MESH': return
    backup_name = obj.get('gen_direct_backup_mesh')
    backup = bpy.data.meshes.get(backup_name) if backup_name else None
    if backup:
        old_data = obj.data
        restored = backup.copy()
        restored.name = f'{obj.name}_直接定位_工作网格'
        obj.data = restored
        try:
            if old_data and old_data.users == 0 and old_data.name != backup.name:
                bpy.data.meshes.remove(old_data)
        except Exception: pass
    mat_vals = obj.get('gen_direct_backup_matrix')
    if mat_vals:
        try: obj.matrix_world = _flat_list_to_matrix(mat_vals)
        except Exception: pass

def clear_direct_profile_backup(obj, remove_backup_mesh=True):
    if not obj: return
    backup_name = obj.get('gen_direct_backup_mesh')
    if remove_backup_mesh and backup_name:
        backup = bpy.data.meshes.get(backup_name)
        if backup:
            try: bpy.data.meshes.remove(backup)
            except Exception: pass
    if 'gen_direct_preview' in obj: del obj['gen_direct_preview']
    if remove_backup_mesh:
        for key in ['gen_direct_backup_mesh', 'gen_direct_backup_matrix']:
            if key in obj: del obj[key]

# ═══════════════════════════════════════════════════════════
# 矩阵 / 锚点
# ═══════════════════════════════════════════════════════════
def _matrix_to_flat_list(mat):
    return [float(mat[r][c]) for r in range(4) for c in range(4)]

def _flat_list_to_matrix(values):
    vals = list(values)
    if len(vals) != 16: return Matrix.Identity(4)
    return Matrix((vals[0:4], vals[4:8], vals[8:12], vals[12:16]))

def _store_rail_matrix_state(profile_ob, generated_ob, rail_ob):
    if not rail_ob: return
    try: mat_values = _matrix_to_flat_list(rail_ob.matrix_world.copy())
    except Exception: return
    for obj in (profile_ob, generated_ob):
        if obj:
            try: obj['gen_last_rail_matrix_world'] = mat_values
            except Exception: pass

def _apply_rail_transform_delta_to_profile(profile_ob, rail_ob, generated_ob=None):
    if not (_is_editable_profile_source(profile_ob) and rail_ob and profile_ob.type == 'MESH'):
        return False
    prev_values = None
    try:
        if generated_ob: prev_values = generated_ob.get('gen_last_rail_matrix_world')
    except Exception: prev_values = None
    if not prev_values:
        try: prev_values = profile_ob.get('gen_last_rail_matrix_world')
        except Exception: prev_values = None
    if not prev_values:
        _store_rail_matrix_state(profile_ob, generated_ob, rail_ob); return False
    try:
        prev_mat = _flat_list_to_matrix(prev_values)
        curr_mat = rail_ob.matrix_world.copy()
        delta = curr_mat @ prev_mat.inverted_safe()
    except Exception:
        _store_rail_matrix_state(profile_ob, generated_ob, rail_ob); return False
    max_diff = max(abs(delta[r][c] - Matrix.Identity(4)[r][c]) for r in range(4) for c in range(4))
    if max_diff < 1e-07:
        _store_rail_matrix_state(profile_ob, generated_ob, rail_ob); return False
    try:
        old_world = profile_ob.matrix_world.copy()
        bake_to_world = delta @ old_world
        mesh = profile_ob.data
        for v in mesh.vertices:
            v.co = bake_to_world @ v.co
        mesh.update()
        profile_ob.matrix_world = Matrix.Identity(4)
        anchor = _prop_list_to_vec(profile_ob.get('gen_profile_anchor_world'), None)
        if anchor is not None:
            try:
                new_anchor = delta @ anchor
                align = generated_ob.get('gen_align_pos', 'MM') if generated_ob else 'MM'
                _store_profile_anchor_state(profile_ob, new_anchor, align)
            except Exception: pass
        profile_ob.update_tag()
        _set_mesh_origin_to_bounds_center_keep_world(profile_ob)
        _store_rail_matrix_state(profile_ob, generated_ob, rail_ob)
    except Exception as e:
        print(f'同步路径变换到可编辑截面失败: {e}')
        _store_rail_matrix_state(profile_ob, generated_ob, rail_ob)
        return False
    return True

def _force_mesh_object_world_space_identity(obj):
    if not obj or obj.type != 'MESH': return False
    mat = obj.matrix_world.copy()
    ident = Matrix.Identity(4)
    max_diff = max(abs(mat[r][c] - ident[r][c]) for r in range(4) for c in range(4))
    if max_diff < 1e-08: return True
    try:
        for v in obj.data.vertices:
            v.co = mat @ v.co
        obj.data.update()
        obj.matrix_world = ident
        obj.update_tag()
        return True
    except Exception as e:
        print(f'烘焙物体世界矩阵失败: {e}')
        return False

def _set_mesh_origin_to_bounds_center_keep_world(obj):
    if not obj or obj.type != 'MESH': return False
    mesh = obj.data
    if not mesh or len(mesh.vertices) == 0: return False
    try:
        if bpy.context.active_object != obj:
            bpy.context.view_layer.objects.active = obj
        if bpy.context.mode != 'OBJECT':
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
        old_world = obj.matrix_world.copy()
        world_coords = [old_world @ v.co for v in mesh.vertices]
        min_v = Vector((min(p.x for p in world_coords), min(p.y for p in world_coords), min(p.z for p in world_coords)))
        max_v = Vector((max(p.x for p in world_coords), max(p.y for p in world_coords), max(p.z for p in world_coords)))
        center_world = (min_v + max_v) * 0.5
        new_world = old_world.copy()
        new_world.translation = center_world
        to_new_local = new_world.inverted_safe() @ old_world
        for v in mesh.vertices:
            v.co = to_new_local @ v.co
        obj.matrix_world = new_world
        mesh.update()
        obj.update_tag()
        return True
    except Exception as e:
        print(f'截面原点移动到边界框中心失败: {e}')
        return False

def _move_editable_profile_anchor_to_current_path_start(profile_ob, scene, align_pos='MM'):
    if not (_is_editable_profile_source(profile_ob) and profile_ob.type == 'MESH'):
        return False
    try:
        begin_punt = Vector(_get_runtime_scene_prop(scene, 'punten_lijst', [Vector((0, 0, 0))])[0])
    except Exception:
        return False
    _force_mesh_object_world_space_identity(profile_ob)
    anchor = _prop_list_to_vec(profile_ob.get('gen_profile_anchor_world'), None)
    if anchor is None:
        _store_profile_anchor_state(profile_ob, begin_punt, align_pos); return False
    delta = begin_punt - anchor
    if delta.length < 1e-08:
        _store_profile_anchor_state(profile_ob, begin_punt, align_pos); return False
    try:
        mesh = profile_ob.data
        for v in mesh.vertices:
            v.co = v.co + delta
        mesh.update()
        profile_ob.matrix_world = Matrix.Identity(4)
        _store_profile_anchor_state(profile_ob, begin_punt, align_pos)
        profile_ob.update_tag()
        _set_mesh_origin_to_bounds_center_keep_world(profile_ob)
        return True
    except Exception as e:
        print(f'同步可编辑截面到新路径起点失败: {e}')
        return False

def _vec_to_prop_list(vec):
    try: return [float(vec.x), float(vec.y), float(vec.z)]
    except Exception: return [0.0, 0.0, 0.0]

def _prop_list_to_vec(values, fallback=None):
    try:
        vals = list(values)
        if len(vals) >= 3:
            return Vector((float(vals[0]), float(vals[1]), float(vals[2])))
    except Exception: pass
    return fallback.copy() if isinstance(fallback, Vector) else fallback

def _store_profile_anchor_state(obj, anchor_point, align_pos='MM'):
    if not obj: return
    try:
        obj['gen_profile_anchor_world'] = _vec_to_prop_list(Vector(anchor_point))
        obj['gen_profile_anchor_align_pos'] = align_pos
    except Exception: pass

def _copy_profile_anchor_state(src_obj, dst_obj):
    if not src_obj or not dst_obj: return
    try:
        if 'gen_profile_anchor_world' in src_obj:
            dst_obj['gen_profile_anchor_world'] = list(src_obj['gen_profile_anchor_world'])
        if 'gen_profile_anchor_align_pos' in src_obj:
            dst_obj['gen_profile_anchor_align_pos'] = src_obj['gen_profile_anchor_align_pos']
    except Exception: pass

def _align_mesh_copy_to_current_path_start(obj, scene, align_pos='MM'):
    if not obj or obj.type != 'MESH': return False
    try:
        norm_lijst = _get_runtime_scene_prop(scene, 'norm_lijst', [])
        is_loop_calc = _get_runtime_scene_prop(scene, 'is_loop_calc', False)
        if is_loop_calc:
            v_rail = Vector(_get_runtime_scene_prop(scene, 'start_tangent', norm_lijst[0]))
        else:
            v_rail = Vector(norm_lijst[0])
        begin_punt = Vector(_get_runtime_scene_prop(scene, 'punten_lijst', [Vector((0, 0, 0))])[0])
    except Exception:
        return False
    if v_rail.length < 1e-08: return False

    _force_mesh_object_world_space_identity(obj)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()
    bm.normal_update()
    verts = list(bm.verts); faces = list(bm.faces)
    if not verts:
        bm.free(); return False

    normaal = Vector((0, 0, 1)); midden = Vector((0, 0, 0))
    if faces:
        total_normal = Vector((0, 0, 0)); total_center = Vector((0, 0, 0))
        for f in faces:
            total_normal += f.normal
            total_center += f.calc_center_median()
        if total_normal.length > 1e-08:
            normaal = total_normal.normalized()
        midden = total_center / max(1, len(faces))
    else:
        if len(verts) > 1:
            coords = [v.co.copy() for v in verts]
            try:
                calc_normal = mathutils.geometry.normal(coords)
                if calc_normal.length > 1e-08:
                    normaal = calc_normal.normalized()
            except Exception:
                normaal = Vector((0, 0, 1))
            min_v = Vector((float('inf'),) * 3)
            max_v = Vector((float('-inf'),) * 3)
            for v in verts:
                min_v.x = min(min_v.x, v.co.x); min_v.y = min(min_v.y, v.co.y); min_v.z = min(min_v.z, v.co.z)
                max_v.x = max(max_v.x, v.co.x); max_v.y = max(max_v.y, v.co.y); max_v.z = max(max_v.z, v.co.z)
            midden = (min_v + max_v) / 2
        else:
            midden = verts[0].co.copy()

    ref_up = Vector((0, 0, 1))
    if abs(normaal.dot(ref_up)) > 0.95:
        ref_up = Vector((0, 1, 0))
    tangent_x = ref_up.cross(normaal)
    if tangent_x.length < 1e-08: tangent_x = Vector((1, 0, 0))
    else: tangent_x.normalize()
    tangent_y = normaal.cross(tangent_x)
    if tangent_y.length < 1e-08: tangent_y = Vector((0, 1, 0))
    else: tangent_y.normalize()

    min_x, max_x = float('inf'), float('-inf')
    min_y, max_y = float('inf'), float('-inf')
    for v in verts:
        vec = v.co - midden
        loc_x = vec.dot(tangent_x); loc_y = vec.dot(tangent_y)
        min_x = min(min_x, loc_x); max_x = max(max_x, loc_x)
        min_y = min(min_y, loc_y); max_y = max(max_y, loc_y)
    geo_center_x = (min_x + max_x) / 2
    geo_center_y = (min_y + max_y) / 2

    if 'L' in align_pos: off_x = min_x
    elif 'R' in align_pos: off_x = max_x
    else: off_x = geo_center_x
    if 'T' in align_pos: off_y = max_y
    elif 'B' in align_pos: off_y = min_y
    else: off_y = geo_center_y

    stored_anchor = _prop_list_to_vec(obj.get('gen_profile_anchor_world'), None)
    if stored_anchor is not None:
        anchor_point = stored_anchor
    else:
        anchor_point = midden + tangent_x * off_x + tangent_y * off_y

    target_fwd = v_rail.normalized()
    if scene.get('rail_z_up', False):
        source_mat = Matrix.Identity(3)
        source_mat[0] = tangent_x; source_mat[1] = tangent_y; source_mat[2] = normaal
        source_mat = source_mat.transposed()
        world_up = Vector((0, 0, 1))
        if abs(target_fwd.dot(world_up)) > 0.99:
            target_right = Vector((1, 0, 0))
        else:
            target_right = target_fwd.cross(world_up).normalized()
        target_up = target_right.cross(target_fwd).normalized()
        target_mat = Matrix.Identity(3)
        target_mat[0] = target_right; target_mat[1] = target_up; target_mat[2] = target_fwd
        target_mat = target_mat.transposed()
        rot_mat = (target_mat @ source_mat.inverted()).to_4x4()
    else:
        try: rot_mat = normaal.rotation_difference(target_fwd).to_matrix().to_4x4()
        except Exception: rot_mat = Matrix.Identity(4)

    for v in verts:
        v.co = rot_mat @ v.co
    rotated_anchor = rot_mat @ anchor_point
    move_vec = begin_punt - rotated_anchor
    for v in verts:
        v.co = v.co + move_vec
    bm.normal_update()
    bm.to_mesh(obj.data)
    obj.data.update()
    obj.matrix_world = Matrix.Identity(4)
    obj.update_tag()
    _store_profile_anchor_state(obj, begin_punt, align_pos)
    _set_mesh_origin_to_bounds_center_keep_world(obj)
    bm.free()
    return True

# ═══════════════════════════════════════════════════════════
# Operator: 计算路径点列表
# ═══════════════════════════════════════════════════════════
class MESH_OT_puntenlijst(bpy.types.Operator):
    bl_idname = 'mesh.puntenlijst'
    bl_label = '计算路径点列表'
    bl_options = {'UNDO'}
    wissel: bpy.props.BoolProperty(name='反转路径方向', default=True)
    use_all_geometry: bpy.props.BoolProperty(default=False, options={'HIDDEN'})

    @_rail_undo_transaction
    def execute(self, context):
        wissel = self.wissel
        ob = bpy.context.object
        scene = context.scene
        make_loop = scene.rail_make_loop if hasattr(scene, 'rail_make_loop') else scene.get('rail_make_loop', False)

        if ob.mode != 'EDIT':
            _rail_op(bpy.ops.object.mode_set, mode='EDIT')
        me = ob.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()

        if self.use_all_geometry:
            for v in bm.verts: v.select_set(True)
            for e in bm.edges: e.select_set(True)
            for f in bm.faces: f.select_set(True)
            try: bm.select_flush(True)
            except Exception: pass

        selected_faces = [f for f in bm.faces if f.select]
        if len(selected_faces) > 0:
            candidate_edges = set()
            for f in selected_faces:
                for e in f.edges: candidate_edges.add(e)
            boundary_edges = []
            for e in candidate_edges:
                linked_sel_faces = [f for f in e.link_faces if f.select]
                if len(linked_sel_faces) == 1:
                    boundary_edges.append(e)
            if boundary_edges:
                _rail_op(bpy.ops.mesh.select_all, action='DESELECT')
                for e in boundary_edges:
                    e.select = True
                for e in boundary_edges:
                    for v in e.verts: v.select = True
                bm.select_flush(True)

        if len([v for v in bm.verts if v.select]) == 0:
            _rail_op(bpy.ops.mesh.select_all, action='SELECT')

        def volgorde(bm):
            selected_verts = [v for v in bm.verts if v.select]
            if not selected_verts:
                return [], True, False
            selected_edges = [e for e in bm.edges if e.select]
            vert_edge_count = {v.index: 0 for v in selected_verts}
            for e in selected_edges:
                for v in e.verts:
                    if v.index in vert_edge_count:
                        vert_edge_count[v.index] += 1
            endpoints = [v_idx for v_idx, count in vert_edge_count.items() if count == 1]
            junctions = [v_idx for v_idx, count in vert_edge_count.items() if count > 2]
            if len(junctions) > 0:
                return (-1), True, False
            is_physically_closed = len(endpoints) == 0 and len(selected_verts) > 0
            if is_physically_closed:
                if (bm.select_history.active
                        and isinstance(bm.select_history.active, bmesh.types.BMVert)
                        and bm.select_history.active.select):
                    start_idx = bm.select_history.active.index
                else:
                    start_idx = selected_verts[0].index
            else:
                start_idx = endpoints[0] if len(endpoints) >= 2 else selected_verts[0].index
            final_indices = [start_idx]
            used_edges = set()
            current_v = bm.verts[start_idx]
            loop_safety = 0
            while loop_safety < len(selected_verts) + 5:
                loop_safety += 1
                next_v = None
                for e in current_v.link_edges:
                    if e.select and e.index not in used_edges:
                        other_v = e.other_vert(current_v)
                        used_edges.add(e.index)
                        final_indices.append(other_v.index)
                        next_v = other_v
                        break
                if next_v is None: break
                current_v = next_v
                if is_physically_closed and current_v.index == start_idx:
                    break
            return final_indices, False, not is_physically_closed

        vert_indices, error, is_open_ended = volgorde(bm)
        if error:
            self.report({'ERROR'}, '检测到交点或复杂网格，无法识别为单一路径')
            return {'CANCELLED'}
        if len(vert_indices) < 2:
            self.report({'ERROR'}, '路径过短')
            return {'CANCELLED'}

        use_world_space = bool(globals().get('RAIL_CALC_WORLD_SPACE', False))
        if use_world_space:
            world_mat = ob.matrix_world.copy()
            coords = [world_mat @ bm.verts[i].co.copy() for i in vert_indices]
        else:
            coords = [bm.verts[i].co.copy() for i in vert_indices]

        if wissel:
            coords.reverse()
            vert_indices.reverse()
        is_looping = make_loop or not is_open_ended
        if make_loop and is_open_ended:
            dist = (coords[-1] - coords[0]).length
            if dist > 0.0001: coords.append(coords[0])
            is_looping = True

        vecs = []
        for i in range(len(coords) - 1):
            vecs.append(coords[i + 1] - coords[i])

        normals = []
        if is_looping:
            num_segments = len(vecs)
            for i in range(num_segments):
                v_prev = vecs[i - 1].normalized()
                v_curr = vecs[i].normalized()
                avg_normal = (v_prev + v_curr).normalized()
                if avg_normal.length < 0.0001:
                    avg_normal = v_curr
                normals.append(avg_normal)
            normals.append(normals[0])
        else:
            normals.append(vecs[0].normalized())
            for i in range(len(vecs) - 1):
                v1 = vecs[i].normalized(); v2 = vecs[i + 1].normalized()
                avg = (v1 + v2).normalized()
                normals.append(avg)
            normals.append(vecs[-1].normalized())

        flatten_start = scene.rail_flatten_start
        flatten_end = scene.rail_flatten_end
        if not is_looping and len(coords) >= 2:
            def get_snapped_normal(tangent_vec):
                x, y, z = abs(tangent_vec.x), abs(tangent_vec.y), abs(tangent_vec.z)
                if x >= y and x >= z:
                    return Vector((1, 0, 0)) if tangent_vec.x > 0 else Vector((-1, 0, 0))
                if y >= x and y >= z:
                    return Vector((0, 1, 0)) if tangent_vec.y > 0 else Vector((0, -1, 0))
                return Vector((0, 0, 1)) if tangent_vec.z > 0 else Vector((0, 0, -1))
            if flatten_start:
                t_start = (coords[1] - coords[0]).normalized()
                normals[0] = get_snapped_normal(t_start)
            if flatten_end:
                t_end = (coords[-1] - coords[-2]).normalized()
                normals[-1] = get_snapped_normal(t_end)

        rot_steps = None
        if getattr(scene, 'rail_corner_sharp', True) or int(getattr(scene, 'rail_corner_segments', 0) or 0) > 0:
            try:
                coords, normals, vecs, rot_steps = _apply_corner_treatment(
                    coords, normals, vecs,
                    _scene_float_prop(scene, 'rail_corner_angle', math.radians(30.0)),
                    int(getattr(scene, 'rail_corner_segments', 0) or 0),
                    bool(getattr(scene, 'rail_corner_sharp', True)),
                    _scene_float_prop(scene, 'rail_corner_radius', 0.35),
                    is_looping)
            except Exception:
                rot_steps = None

        _set_runtime_scene_prop(bpy.context.scene, 'corner_rot_steps', rot_steps)
        _set_runtime_scene_prop(bpy.context.scene, 'loc_oorsprong',
                                Vector((0, 0, 0)) if use_world_space else ob.location)
        _set_runtime_scene_prop(bpy.context.scene, 'punten_lijst', coords)
        _set_runtime_scene_prop(bpy.context.scene, 'norm_lijst', normals)
        _set_runtime_scene_prop(bpy.context.scene, 'richt_lijnen', vecs)
        _set_runtime_scene_prop(bpy.context.scene, 'eindig', not is_looping)
        if len(vecs) > 0:
            _set_runtime_scene_prop(bpy.context.scene, 'start_tangent', vecs[0].normalized())
        else:
            _set_runtime_scene_prop(bpy.context.scene, 'start_tangent', Vector((0, 0, 1)))
        _set_runtime_scene_prop(bpy.context.scene, 'is_loop_calc', is_looping)
        return {'FINISHED'}

# ═══════════════════════════════════════════════════════════
# 镜像 bmesh
# ═══════════════════════════════════════════════════════════
def apply_mirror_to_bmesh(bm, axis='X'):
    selected_verts = [v for v in bm.verts if v.select]
    if not selected_verts: selected_verts = bm.verts
    if len(selected_verts) == 0: return
    v_co = selected_verts[0].co.copy()
    min_v = v_co.copy(); max_v = v_co.copy()
    for v in selected_verts:
        v_now = v.co
        min_v.x = min(min_v.x, v_now.x); min_v.y = min(min_v.y, v_now.y); min_v.z = min(min_v.z, v_now.z)
        max_v.x = max(max_v.x, v_now.x); max_v.y = max(max_v.y, v_now.y); max_v.z = max(max_v.z, v_now.z)
    center = (min_v + max_v) / 2
    mat_to_origin = Matrix.Translation(-center)
    mat_from_origin = Matrix.Translation(center)
    if axis == 'X':
        mat_scale = Matrix.Scale((-1), 4, (1, 0, 0))
    else:
        mat_scale = Matrix.Scale((-1), 4, (0, 1, 0))
    final_mat = mat_from_origin @ mat_scale @ mat_to_origin
    bmesh.ops.transform(bm, matrix=final_mat, verts=bm.verts)
    for f in bm.faces:
        f.normal_flip()

# ═══════════════════════════════════════════════════════════
# Operator: 轮廓对齐
# ═══════════════════════════════════════════════════════════
class MESH_OT_profiel_vlak(bpy.types.Operator):
    bl_idname = 'mesh.profiel_vlak'
    bl_label = '轮廓对齐'
    bl_options = {'UNDO'}

    align_pos: bpy.props.EnumProperty(items=[
        ('TL', '左上', ''), ('TM', '上中', ''), ('TR', '右上', ''),
        ('ML', '左中', ''), ('MM', '居中', ''), ('MR', '右中', ''),
        ('BL', '左下', ''), ('BM', '下中', ''), ('BR', '右下', ''),
    ], default='MM')
    target_name: bpy.props.StringProperty(options={'HIDDEN'})
    align_editable_copy_to_path: bpy.props.BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.mode in {'EDIT_MESH', 'OBJECT', 'EDIT_CURVE'}

    @_rail_undo_transaction
    def execute(self, context):
        global RAIL_CALC_WORLD_SPACE
        scene = context.scene

        previous_path_start = None
        try:
            previous_path_start = Vector(_get_runtime_scene_prop(scene, 'punten_lijst', [Vector((0, 0, 0))])[0])
        except Exception:
            previous_path_start = None

        force_realign_editable_on_update = bool(FORCE_REALIGN_EDITABLE_ON_UPDATE and self.target_name)
        align_editable_copy_to_path = bool(getattr(self, 'align_editable_copy_to_path', False) and self.target_name)
        mapping_full_realign_on_update = bool(FORCE_MAPPING_REALIGN_ON_UPDATE and self.target_name)

        rail_ob = None
        profile_ob = None
        initial_mode = context.mode
        was_in_edit_mode = initial_mode in {'EDIT_MESH', 'EDIT_CURVE'}
        active_ob_initial = context.active_object
        edit_selection_state = _capture_edit_selection_state(active_ob_initial) if was_in_edit_mode else None
        if was_in_edit_mode:
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')

        is_update_mode = False
        was_already_extruded = scene.get('is_already_extruded', False)
        active_ob = active_ob_initial if active_ob_initial else context.active_object

        direct_rebuild_mode = False
        direct_rebuild_target = None
        direct_preview_active = (active_ob is not None
                                 and active_ob.get('gen_direct_preview')
                                 and (not scene.get('is_already_extruded', False))
                                 and (not self.target_name))

        if active_ob and active_ob.get('gen_profile_consumed') and (not self.target_name):
            self.report({'WARNING'}, '该物体已由原截面直接生成，没有独立截面源；如需重新定位，请重新选择路径和截面。')
            return {'CANCELLED'}

        last_gen_name = ''
        last_src_name = ''
        last_rail_name = ''

        if self.target_name:
            last_gen_name = self.target_name
            temp_target = bpy.data.objects.get(last_gen_name)
            if temp_target:
                last_src_name = temp_target.get('gen_profile_name', scene.get('pre_last_source', ''))
                last_rail_name = temp_target.get('gen_rail_name', scene.get('pre_last_rail', ''))
                if temp_target.get('gen_direct_inplace') or temp_target.get('gen_profile_consumed'):
                    if _direct_backup_available(temp_target):
                        direct_rebuild_mode = True
                        direct_rebuild_target = temp_target
                        last_src_name = temp_target.name
                    else:
                        self.report({'WARNING'}, '该直接生成物没有原始截面备份，已跳过实时更新以避免物体消失')
                        return {'CANCELLED'}
            else:
                last_src_name = scene.get('pre_last_source', '')
                last_rail_name = scene.get('pre_last_rail', '')
        else:
            last_gen_name = scene.get('pre_last_generated', '')
            last_src_name = scene.get('pre_last_source', '')
            last_rail_name = scene.get('pre_last_rail', '')
            if active_ob and 'gen_profile_name' in active_ob and ('gen_rail_name' in active_ob):
                last_src_name = active_ob['gen_profile_name']
                last_rail_name = active_ob['gen_rail_name']
                last_gen_name = active_ob.name

        possible_update = False
        existing_generated_ob = bpy.data.objects.get(last_gen_name) if last_gen_name else None
        if existing_generated_ob:
            if direct_rebuild_mode:
                possible_update = False
            elif direct_preview_active or (existing_generated_ob.get('gen_direct_preview')
                                          and (not scene.get('is_already_extruded', False))):
                possible_update = False
            elif self.target_name:
                possible_update = True
            elif active_ob:
                if active_ob.name == last_gen_name and active_ob.get('gen_rail_name'):
                    possible_update = True
                if active_ob.name == last_rail_name and active_ob.get('gen_rail_name') is None and existing_generated_ob.get('gen_rail_name'):
                    possible_update = True
                if active_ob.name == last_src_name and existing_generated_ob.get('gen_rail_name'):
                    possible_update = True
        else:
            possible_update = False
            if not self.target_name:
                scene['pre_last_generated'] = ''
                scene['is_already_extruded'] = False
                was_already_extruded = False

        if possible_update:
            profile_found = bpy.data.objects.get(last_src_name)
            rail_found = bpy.data.objects.get(last_rail_name)
            if profile_found and rail_found:
                is_update_mode = True
                if (_is_editable_profile_source(profile_found)
                        and 'gen_profile_anchor_world' not in profile_found
                        and previous_path_start is not None):
                    align = (existing_generated_ob.get('gen_align_pos', scene.get('stored_align_pos', 'MM'))
                             if existing_generated_ob else scene.get('stored_align_pos', 'MM'))
                    _store_profile_anchor_state(profile_found, previous_path_start, align)
                old_gen = bpy.data.objects.get(last_gen_name)
                if old_gen:
                    try: bpy.data.objects.remove(old_gen, do_unlink=True)
                    except Exception: pass
                profile_ob = profile_found
                rail_ob = rail_found
                if context.view_layer.objects.active != rail_ob:
                    context.view_layer.objects.active = rail_ob
                    rail_ob.select_set(True)
                scene['pre_last_source'] = profile_ob.name
                scene['pre_last_rail'] = rail_ob.name
            else:
                is_update_mode = False

        if not is_update_mode:
            if direct_rebuild_mode:
                profile_ob = direct_rebuild_target
                rail_ob = bpy.data.objects.get(profile_ob.get('gen_rail_name', ''))
                if not rail_ob:
                    self.report({'ERROR'}, '找不到原截面关联的路径，无法实时更新')
                    return {'CANCELLED'}
                was_already_extruded = True
            else:
                scene['is_already_extruded'] = False
                was_already_extruded = False
            if not direct_rebuild_mode and direct_preview_active:
                profile_ob = active_ob
                rail_ob = bpy.data.objects.get(profile_ob.get('gen_rail_name', ''))
                if not rail_ob:
                    self.report({'ERROR'}, '找不到原截面关联的路径，请重新选择路径和截面')
                    return {'CANCELLED'}
            elif not direct_rebuild_mode:
                profile_ob, rail_list, selection_error = _get_path_active_selection(context, allow_multi=False)
                if not profile_ob or not rail_list:
                    self.report({'ERROR'}, selection_error or '请同时选择截面和路径，并把路径设为活动物体')
                    return {'CANCELLED'}
                rail_ob = rail_list[0]
            if (not is_update_mode) and (not direct_rebuild_mode) and (not direct_preview_active):
                _reset_scene_mapping_for_new_loft(scene)
            if not profile_ob or not rail_ob:
                self.report({'ERROR'}, '请选择轮廓物体和路径物体')
                return {'CANCELLED'}
            scene['pre_last_source'] = profile_ob.name
            scene['pre_last_rail'] = rail_ob.name

        if rail_ob and rail_ob.type == 'CURVE':
            res_count = get_resolution_proxy(None)
            apply_resample_modifier(rail_ob, res_count)
            context.view_layer.update()

        if rail_ob:
            prev_mode = rail_ob.mode
            if prev_mode != 'OBJECT':
                _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            rail_ob.select_set(True)
            context.view_layer.objects.active = rail_ob
            if profile_ob and profile_ob != rail_ob:
                profile_ob.select_set(True)

        flip_dir = scene.get('rail_flip_dir', True)
        temp_ob = None
        rail_edit_selection_requested = bool(was_in_edit_mode
                                             and active_ob_initial is not None
                                             and active_ob_initial == rail_ob
                                             and initial_mode == 'EDIT_MESH')

        if rail_ob.type == 'CURVE':
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()
            rail_eval = rail_ob.evaluated_get(depsgraph)
            temp_mesh = bpy.data.meshes.new_from_object(rail_eval)
            temp_ob = bpy.data.objects.new(rail_ob.name + '_临时计算', temp_mesh)
            context.collection.objects.link(temp_ob)
            temp_ob.matrix_world = rail_ob.matrix_world
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            temp_ob.select_set(True)
            context.view_layer.objects.active = temp_ob
            _rail_op(bpy.ops.object.transform_apply, location=True, rotation=True, scale=True)
            try:
                _rail_op(bpy.ops.mesh.puntenlijst, wissel=flip_dir, use_all_geometry=True)
                _set_runtime_scene_prop(scene, 'loc_oorsprong', Vector((0, 0, 0)))
            except Exception as e:
                bpy.data.objects.remove(temp_ob, do_unlink=True)
                self.report({'ERROR'}, f'曲线路径计算错误: {str(e)}')
                return {'CANCELLED'}
            bpy.data.objects.remove(temp_ob, do_unlink=True)
            if profile_ob:
                profile_ob.select_set(True)

        if rail_ob.type == 'MESH':
            context.view_layer.objects.active = rail_ob
            if not is_update_mode and rail_ob.mode == 'OBJECT' and len(rail_ob.data.polygons) > 5000:
                self.report({'WARNING'}, '目标网格面数过多，请进入编辑模式选中特定路径/面')
                return {'CANCELLED'}
            was_obj_mode = rail_ob.mode == 'OBJECT'
            if rail_ob.mode != 'EDIT':
                _rail_op(bpy.ops.object.editmode_toggle)
            if is_update_mode:
                _rail_op(bpy.ops.mesh.select_all, action='SELECT')
            prev_world_calc = RAIL_CALC_WORLD_SPACE
            RAIL_CALC_WORLD_SPACE = True
            try:
                res = _rail_op(bpy.ops.mesh.puntenlijst,
                               wissel=flip_dir,
                               use_all_geometry=not rail_edit_selection_requested)
                if 'CANCELLED' in res:
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                    return {'CANCELLED'}
            except RuntimeError:
                if context.object.mode != 'OBJECT':
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                if not is_update_mode:
                    return {'CANCELLED'}
                RAIL_CALC_WORLD_SPACE = prev_world_calc
                self.report({'ERROR'}, '路径无效：请选中单条路径线或单个/连通的面')
                return {'CANCELLED'}
            RAIL_CALC_WORLD_SPACE = prev_world_calc
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')

        _apply_path_mapping_to_scene(scene, profile_ob)

        def naar_collectie(ob):
            col_name = '挤出物'
            col = bpy.data.collections.get(col_name)
            if not col:
                col = bpy.data.collections.new(col_name)
                context.scene.collection.children.link(col)
            try: ob.users_collection[0].objects.unlink(ob)
            except Exception: pass
            col.objects.link(ob)

        try:
            norm_lijst = _get_runtime_scene_prop(bpy.context.scene, 'norm_lijst', [])
            is_loop_calc = _get_runtime_scene_prop(bpy.context.scene, 'is_loop_calc', False)
            if is_loop_calc:
                v_rail = Vector(_get_runtime_scene_prop(bpy.context.scene, 'start_tangent', norm_lijst[0]))
            else:
                v_rail = Vector(norm_lijst[0])
            oorsprong = Vector(_get_runtime_scene_prop(bpy.context.scene, 'loc_oorsprong', Vector((0, 0, 0))))
            begin_punt = Vector(_get_runtime_scene_prop(bpy.context.scene, 'punten_lijst', [Vector((0, 0, 0))])[0])
        except KeyError:
            return {'CANCELLED'}

        extrusion_ob = None
        is_separate_object = profile_ob != rail_ob
        editable_realign_mode = (is_update_mode and _is_editable_profile_source(profile_ob)
                                 and (not self.target_name or force_realign_editable_on_update))

        # 读取"截面吸附到路径"开关：
        #   True  = 吸附模式（原截面被移动到路径起点，兼容旧行为）
        #   False = 复制模式（原截面保留在原地，放样物使用其副本）
        snap_profile_to_path = bool(getattr(scene, 'rail_snap_profile_to_path', True))
        use_direct_profile_object = (
            (not is_update_mode and is_separate_object and snap_profile_to_path)
            or direct_rebuild_mode
            or (editable_realign_mode and snap_profile_to_path)
        )

        if (is_update_mode and _is_editable_profile_source(profile_ob)
                and self.target_name and (not force_realign_editable_on_update)):
            src_profile = profile_ob
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            src_profile.select_set(True)
            context.view_layer.objects.active = src_profile
            if context.mode != 'OBJECT':
                _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
            realtime_align_pos = scene.get('stored_align_pos', self.align_pos)
            # 吸附模式：移动原截面到路径起点
            if align_editable_copy_to_path and snap_profile_to_path:
                if mapping_full_realign_on_update:
                    _align_mesh_copy_to_current_path_start(src_profile, scene, realtime_align_pos)
                else:
                    _move_editable_profile_anchor_to_current_path_start(src_profile, scene, realtime_align_pos)
            _rail_op(bpy.ops.object.duplicate)
            extrusion_ob = context.object
            extrusion_ob.name = '挤出物'
            naar_collectie(extrusion_ob)
            _force_mesh_object_world_space_identity(extrusion_ob)
            _copy_profile_anchor_state(src_profile, extrusion_ob)
            # 非吸附模式：改为移动副本到路径起点（原截面保持原地不动）
            if align_editable_copy_to_path and not snap_profile_to_path:
                if mapping_full_realign_on_update:
                    _align_mesh_copy_to_current_path_start(extrusion_ob, scene, realtime_align_pos)
                else:
                    _move_editable_profile_anchor_to_current_path_start(extrusion_ob, scene, realtime_align_pos)
            _remove_custom_props(extrusion_ob, [
                'gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed',
                'gen_direct_backup_mesh', 'gen_direct_backup_matrix',
                'gen_editable_profile_source', 'gen_source_generated_name'])
            extrusion_ob['gen_profile_name'] = src_profile.name
            extrusion_ob['gen_rail_name'] = rail_ob.name
            extrusion_ob['gen_align_pos'] = scene.get('stored_align_pos', self.align_pos)
            _set_gen_settings(extrusion_ob, _current_gen_settings_from_scene(scene))
            _store_rail_matrix_state(src_profile, extrusion_ob, rail_ob)
            scene['pre_last_source'] = src_profile.name
            scene['pre_last_rail'] = rail_ob.name
            scene['pre_last_generated'] = extrusion_ob.name
            src_profile['gen_source_generated_name'] = extrusion_ob.name
            context.view_layer.objects.active = extrusion_ob
            extrusion_ob.select_set(True)
            _rail_op(bpy.ops.object.mode_set, mode='EDIT')
            _rail_op(bpy.ops.mesh.select_all, action='SELECT')
            _rail_op(bpy.ops.mesh.punten_naar_mesh)
            restored = False
            if active_ob_initial:
                restored = _restore_user_active_object(context, active_ob_initial, edit_mode=was_in_edit_mode)
                if restored and was_in_edit_mode:
                    _restore_edit_selection_state(context, active_ob_initial, edit_selection_state)
            if not restored:
                _restore_user_active_object(context, src_profile, edit_mode=False)
            return {'FINISHED'}

        if is_separate_object:
            if use_direct_profile_object:
                _rail_op(bpy.ops.object.select_all, action='DESELECT')
                profile_ob.select_set(True)
                context.view_layer.objects.active = profile_ob
                if profile_ob.type == 'CURVE':
                    try:
                        _rail_op(bpy.ops.object.convert, target='MESH')
                        profile_ob = context.object
                        scene['pre_last_source'] = profile_ob.name
                    except Exception as e:
                        self.report({'ERROR'}, f'截面曲线转网格失败: {e}')
                        return {'CANCELLED'}
                if profile_ob.type != 'MESH':
                    self.report({'ERROR'}, '截面必须是网格或可转换为网格的曲线')
                    return {'CANCELLED'}
                if not editable_realign_mode and (profile_ob.get('gen_direct_preview')
                                                  or profile_ob.get('gen_profile_consumed')
                                                  or direct_rebuild_mode):
                    restore_direct_profile_backup(profile_ob)
                else:
                    ensure_direct_profile_backup(profile_ob)
                _rail_op(bpy.ops.object.select_all, action='DESELECT')
                profile_ob.select_set(True)
                context.view_layer.objects.active = profile_ob
                _rail_op(bpy.ops.object.transform_apply, location=False, rotation=True, scale=True)
                extrusion_ob = profile_ob
                if extrusion_ob.mode != 'EDIT':
                    _rail_op(bpy.ops.object.mode_set, mode='EDIT')
                _rail_op(bpy.ops.mesh.select_all, action='SELECT')
            else:
                _rail_op(bpy.ops.object.select_all, action='DESELECT')
                profile_ob.select_set(True)
                context.view_layer.objects.active = profile_ob
                _rail_op(bpy.ops.object.duplicate)
                extrusion_ob = context.object
                extrusion_ob.name = '挤出物'
                if extrusion_ob.type == 'CURVE':
                    try: _rail_op(bpy.ops.object.convert, target='MESH')
                    except Exception as e:
                        self.report({'ERROR'}, f'截面曲线转网格失败: {e}')
                        return {'CANCELLED'}
                _rail_op(bpy.ops.object.transform_apply, location=False, rotation=True, scale=True)
                naar_collectie(extrusion_ob)
                _rail_op(bpy.ops.object.editmode_toggle)
                _rail_op(bpy.ops.mesh.select_all, action='SELECT')
        else:
            _rail_op(bpy.ops.object.editmode_toggle)
            _rail_op(bpy.ops.mesh.duplicate)
            _rail_op(bpy.ops.mesh.separate, type='SELECTED')
            _rail_op(bpy.ops.object.editmode_toggle)
            rail_ob.select_set(False)
            extrusion_ob = context.selected_objects[-1]
            extrusion_ob.name = '挤出物'
            context.view_layer.objects.active = extrusion_ob
            naar_collectie(extrusion_ob)
            _rail_op(bpy.ops.object.editmode_toggle)
            _rail_op(bpy.ops.mesh.select_all, action='SELECT')

        scene['pre_last_generated'] = extrusion_ob.name
        extrusion_ob['gen_profile_name'] = profile_ob.name
        extrusion_ob['gen_rail_name'] = rail_ob.name

        if use_direct_profile_object:
            if editable_realign_mode:
                extrusion_ob['gen_editable_profile_source'] = True
                extrusion_ob['gen_rail_name'] = rail_ob.name
                for key in ['gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed']:
                    if key in extrusion_ob: del extrusion_ob[key]
            else:
                extrusion_ob['gen_direct_preview'] = True
                extrusion_ob['gen_direct_inplace'] = True
        else:
            for key in ['gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed']:
                if key in extrusion_ob: del extrusion_ob[key]

        ob = context.object
        ob.location = oorsprong
        me = ob.data
        bm = bmesh.from_edit_mesh(me)

        is_mirrored_x = scene.get('rail_mirror_x', False)
        is_mirrored_y = scene.get('rail_mirror_y', False)
        if is_mirrored_x: apply_mirror_to_bmesh(bm, axis='X')
        if is_mirrored_y: apply_mirror_to_bmesh(bm, axis='Y')

        bm.faces.ensure_lookup_table(); bm.verts.ensure_lookup_table(); bm.normal_update()
        selected_verts = [v for v in bm.verts if v.select]
        selected_faces = [f for f in bm.faces if f.select]
        face_count = len(selected_faces)

        normaal = Vector((0, 0, 1)); midden = Vector((0, 0, 0))
        if face_count > 0:
            total_normal = Vector((0, 0, 0)); total_center = Vector((0, 0, 0))
            for vl in selected_faces:
                total_normal += vl.normal
                total_center += vl.calc_center_median()
            normaal = (total_normal / face_count).normalized()
            midden = total_center / face_count
        else:
            if len(selected_verts) > 1:
                try:
                    coords = [v.co for v in selected_verts]
                    calc_normal = mathutils.geometry.normal(coords)
                    if calc_normal.length_squared < 0.0001:
                        normaal = Vector((0, 0, 1))
                    else:
                        normaal = calc_normal
                except Exception:
                    normaal = Vector((0, 0, 1))
                min_v = Vector((float('inf'),) * 3)
                max_v = Vector((float('-inf'),) * 3)
                for v in selected_verts:
                    min_v.x = min(min_v.x, v.co.x); min_v.y = min(min_v.y, v.co.y); min_v.z = min(min_v.z, v.co.z)
                    max_v.x = max(max_v.x, v.co.x); max_v.y = max(max_v.y, v.co.y); max_v.z = max(max_v.z, v.co.z)
                midden = (min_v + max_v) / 2
            elif len(selected_verts) == 1:
                midden = selected_verts[0].co
                normaal = Vector((0, 0, 1))

        rot_steps = scene.get('rail_profile_rotation', 0)
        if rot_steps != 0:
            rot_angle = math.radians(90 * rot_steps)
            rot_mat = Matrix.Rotation(rot_angle, 4, normaal)
            mat_trans_to = Matrix.Translation(-midden)
            mat_trans_from = Matrix.Translation(midden)
            final_rot = mat_trans_from @ rot_mat @ mat_trans_to
            bmesh.ops.transform(bm, matrix=final_rot, verts=selected_verts)
            bm.normal_update()

        ref_up = Vector((0, 0, 1))
        if abs(normaal.dot(ref_up)) > 0.95:
            ref_up = Vector((0, 1, 0))
        tangent_x = ref_up.cross(normaal).normalized()
        tangent_y = normaal.cross(tangent_x).normalized()

        min_x, max_x = 99999.0, -99999.0
        min_y, max_y = 99999.0, -99999.0
        for v in selected_verts:
            vec = v.co - midden
            loc_x = vec.dot(tangent_x); loc_y = vec.dot(tangent_y)
            if loc_x < min_x: min_x = loc_x
            if loc_x > max_x: max_x = loc_x
            if loc_y < min_y: min_y = loc_y
            if loc_y > max_y: max_y = loc_y
        geo_center_x = (min_x + max_x) / 2
        geo_center_y = (min_y + max_y) / 2

        align = self.align_pos
        if 'L' in align: off_x = min_x
        elif 'R' in align: off_x = max_x
        else: off_x = geo_center_x
        if 'T' in align: off_y = max_y
        elif 'B' in align: off_y = min_y
        else: off_y = geo_center_y

        anchor_offset = tangent_x * off_x + tangent_y * off_y
        anchor_point = midden + anchor_offset
        target_fwd = v_rail.normalized()

        is_z_up = scene.get('rail_z_up', False)
        if is_z_up:
            source_mat = Matrix.Identity(3)
            source_mat[0] = tangent_x; source_mat[1] = tangent_y; source_mat[2] = normaal
            source_mat = source_mat.transposed()
            world_up = Vector((0, 0, 1))
            if abs(target_fwd.dot(world_up)) > 0.99:
                target_right = Vector((1, 0, 0))
            else:
                target_right = target_fwd.cross(world_up).normalized()
            target_up = target_right.cross(target_fwd).normalized()
            target_mat = Matrix.Identity(3)
            target_mat[0] = target_right; target_mat[1] = target_up; target_mat[2] = target_fwd
            target_mat = target_mat.transposed()
            mat = (target_mat @ source_mat.inverted()).to_4x4()
        else:
            rot_dif = normaal.rotation_difference(target_fwd)
            mat = rot_dif.to_matrix().to_4x4()

        for p in bm.verts:
            if p.select:
                p.co = mat @ p.co
        rotated_anchor = mat @ anchor_point
        transf = begin_punt - rotated_anchor
        for p in bm.verts:
            if p.select:
                p.co = p.co + transf

        bm.faces.ensure_lookup_table()
        bm.normal_update()
        bmesh.update_edit_mesh(me)
        context.view_layer.objects.active = ob
        scene['stored_align_pos'] = self.align_pos
        _store_profile_anchor_state(extrusion_ob, begin_punt, self.align_pos)
        if use_direct_profile_object and (not direct_rebuild_mode):
            _set_mesh_origin_to_bounds_center_keep_world(extrusion_ob)
        extrusion_ob['gen_align_pos'] = self.align_pos
        _set_gen_settings(extrusion_ob, _current_gen_settings_from_scene(scene))
        _store_rail_matrix_state(profile_ob, extrusion_ob, rail_ob)

        if editable_realign_mode and snap_profile_to_path:
            src_profile = extrusion_ob
            try: _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
            except Exception: pass
            _remove_custom_props(src_profile, ['gen_profile_name', 'gen_direct_preview',
                                               'gen_direct_inplace', 'gen_profile_consumed'])
            src_profile['gen_editable_profile_source'] = True
            src_profile['gen_rail_name'] = rail_ob.name
            _store_profile_anchor_state(src_profile, begin_punt, self.align_pos)
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            src_profile.select_set(True)
            context.view_layer.objects.active = src_profile
            _rail_op(bpy.ops.object.duplicate)
            new_loft = context.object
            new_loft.name = '挤出物'
            naar_collectie(new_loft)
            _force_mesh_object_world_space_identity(new_loft)
            _copy_profile_anchor_state(src_profile, new_loft)
            _remove_custom_props(new_loft, [
                'gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed',
                'gen_direct_backup_mesh', 'gen_direct_backup_matrix',
                'gen_editable_profile_source', 'gen_source_generated_name'])
            new_loft['gen_profile_name'] = src_profile.name
            new_loft['gen_rail_name'] = rail_ob.name
            new_loft['gen_align_pos'] = self.align_pos
            if _has_gen_settings(src_profile):
                _set_gen_settings(new_loft, _get_gen_settings(src_profile))
            else:
                _set_gen_settings(new_loft, _current_gen_settings_from_scene(scene))
            _store_rail_matrix_state(src_profile, new_loft, rail_ob)
            src_profile['gen_editable_profile_source'] = True
            src_profile['gen_rail_name'] = rail_ob.name
            src_profile['gen_source_generated_name'] = new_loft.name
            scene['pre_last_source'] = src_profile.name
            scene['pre_last_rail'] = rail_ob.name
            scene['pre_last_generated'] = new_loft.name
            scene['is_already_extruded'] = True
            context.view_layer.objects.active = new_loft
            new_loft.select_set(True)
            _rail_op(bpy.ops.object.mode_set, mode='EDIT')
            _rail_op(bpy.ops.mesh.select_all, action='SELECT')
            _rail_op(bpy.ops.mesh.punten_naar_mesh)
            restored = False
            if active_ob_initial:
                restored = _restore_user_active_object(context, active_ob_initial, edit_mode=was_in_edit_mode)
                if restored and was_in_edit_mode:
                    _restore_edit_selection_state(context, active_ob_initial, edit_selection_state)
            if not restored:
                _restore_user_active_object(context, src_profile, edit_mode=False)
            return {'FINISHED'}

        if (is_update_mode and was_already_extruded) or direct_rebuild_mode:
            _rail_op(bpy.ops.mesh.punten_naar_mesh)

        if (not was_already_extruded) or (was_in_edit_mode and active_ob_initial):
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            context.view_layer.objects.active = active_ob_initial
            active_ob_initial.select_set(True)
            _rail_op(bpy.ops.object.mode_set, mode='EDIT')
            _restore_edit_selection_state(context, active_ob_initial, edit_selection_state)
            if not was_in_edit_mode:
                _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                _rail_op(bpy.ops.object.select_all, action='DESELECT')
                target_to_select = active_ob_initial if active_ob_initial else rail_ob
                selected_success = False
                try:
                    if target_to_select and target_to_select.name:
                        context.view_layer.objects.active = target_to_select
                        target_to_select.select_set(True)
                        selected_success = True
                except (ReferenceError, AttributeError, TypeError):
                    pass
                if not selected_success and extrusion_ob:
                    try:
                        context.view_layer.objects.active = extrusion_ob
                        extrusion_ob.select_set(True)
                    except Exception: pass
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            context.view_layer.objects.active = extrusion_ob
            extrusion_ob.select_set(True)
            _rail_op(bpy.ops.object.mode_set, mode='EDIT')
            _rail_op(bpy.ops.mesh.select_all, action='SELECT')

        return {'FINISHED'}

# ═══════════════════════════════════════════════════════════
# 安全调用辅助
# ═══════════════════════════════════════════════════════════
def _clean_nested_operator_error(exc, fallback='操作失败'):
    msg = str(exc).strip()
    for prefix in ['错误:', 'Error:']:
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
    return msg or fallback

def _safe_call_profiel_vlak(report_owner, **kwargs):
    try:
        return _rail_op(bpy.ops.mesh.profiel_vlak, **kwargs)
    except RuntimeError as e:
        msg = _clean_nested_operator_error(e, '请选中一个截面 Mesh，并把路径设为活动物体')
        try: report_owner.report({'WARNING'}, msg)
        except Exception: pass
        return {'CANCELLED'}

# ═══════════════════════════════════════════════════════════
# 各类小 Operator
# ═══════════════════════════════════════════════════════════
class MESH_OT_spiegel_profiel(bpy.types.Operator):
    bl_idname = 'mesh.spiegel_profiel'
    bl_label = '轮廓镜像'
    bl_options = {'UNDO'}
    axis: bpy.props.EnumProperty(items=[('X', 'X轴', ''), ('Y', 'Y轴', '')], default='X')

    @_rail_undo_transaction
    def execute(self, context):
        scene = context.scene
        prop_name = f'rail_mirror_{self.axis.lower()}'
        scene[prop_name] = not scene.get(prop_name, False)
        res = _safe_call_profiel_vlak(self, align_pos=scene.get('stored_align_pos', 'MM'))
        if 'CANCELLED' in res:
            scene[prop_name] = not scene.get(prop_name, False)
            return {'CANCELLED'}
        return {'FINISHED'}

class MESH_OT_rotate_profile_step(bpy.types.Operator):
    bl_idname = 'mesh.rotate_profile_step'
    bl_label = '轮廓旋转90度'
    bl_options = {'UNDO'}
    direction: bpy.props.EnumProperty(items=[('CW', '顺时针', ''), ('CCW', '逆时针', '')], default='CW')

    @_rail_undo_transaction
    def execute(self, context):
        scene = context.scene
        current = scene.get('rail_profile_rotation', 0)
        scene['rail_profile_rotation'] = current + 1 if self.direction == 'CCW' else current - 1
        res = _safe_call_profiel_vlak(self, align_pos=scene.get('stored_align_pos', 'MM'))
        if 'CANCELLED' in res:
            scene['rail_profile_rotation'] = current
            return {'CANCELLED'}
        return {'FINISHED'}

class MESH_OT_wissel_richting(bpy.types.Operator):
    bl_idname = 'mesh.wissel_richting'
    bl_label = '切换路径首尾'
    bl_options = {'UNDO'}

    @_rail_undo_transaction
    def execute(self, context):
        scene = context.scene
        old_flip_dir = scene.get('rail_flip_dir', True)
        scene['rail_flip_dir'] = not old_flip_dir
        res = _safe_call_profiel_vlak(self, align_pos=scene.get('stored_align_pos', 'MM'))
        if 'CANCELLED' in res:
            scene['rail_flip_dir'] = old_flip_dir
            return {'CANCELLED'}
        return {'FINISHED'}

class MESH_OT_toggle_z_up(bpy.types.Operator):
    bl_idname = 'mesh.toggle_z_up'
    bl_label = '切换Z轴向上模式'
    bl_options = {'UNDO'}

    @_rail_undo_transaction
    def execute(self, context):
        scene = context.scene
        old_z_up = scene.get('rail_z_up', False)
        scene['rail_z_up'] = not old_z_up
        res = _safe_call_profiel_vlak(self, align_pos=scene.get('stored_align_pos', 'MM'))
        if 'CANCELLED' in res:
            scene['rail_z_up'] = old_z_up
            return {'CANCELLED'}
        return {'FINISHED'}

class MESH_OT_select_rail(bpy.types.Operator):
    bl_idname = 'mesh.select_rail'
    bl_label = '选中路径'
    bl_description = '选中并激活当前物体关联的路径对象'
    bl_options = {'UNDO'}

    @_rail_undo_transaction
    def execute(self, context):
        active = context.active_object
        if not active: return {'CANCELLED'}
        rail_name = active.get('gen_rail_name')
        if rail_name:
            rail_ob = bpy.data.objects.get(rail_name)
            if rail_ob:
                if context.mode != 'OBJECT':
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                _rail_op(bpy.ops.object.select_all, action='DESELECT')
                rail_ob.select_set(True)
                context.view_layer.objects.active = rail_ob
                self.report({'INFO'}, f'已选中路径: {rail_name}')
            else:
                self.report({'WARNING'}, f'找不到路径物体: {rail_name}')
        return {'FINISHED'}

class MESH_OT_select_profile(bpy.types.Operator):
    bl_idname = 'mesh.select_profile'
    bl_label = '选中截面'
    bl_description = '选中并激活当前物体关联的截面轮廓对象'
    bl_options = {'UNDO'}

    @_rail_undo_transaction
    def execute(self, context):
        active = context.active_object
        if not active: return {'CANCELLED'}
        profile_name = active.get('gen_profile_name')
        if profile_name:
            profile_ob = bpy.data.objects.get(profile_name)
            if profile_ob:
                if context.mode != 'OBJECT':
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                _rail_op(bpy.ops.object.select_all, action='DESELECT')
                profile_ob.select_set(True)
                context.view_layer.objects.active = profile_ob
                self.report({'INFO'}, f'已选中截面: {profile_name}')
            else:
                self.report({'WARNING'}, f'找不到截面物体: {profile_name}')
        return {'FINISHED'}

class MESH_OT_apply_rail_follow(bpy.types.Operator):
    bl_idname = 'mesh.apply_rail_follow'
    bl_label = '应用路径跟随'
    bl_description = '断开与路径和截面的关联，停止自动更新，将模型固定为普通网格'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        active = context.active_object
        return active and active.get('gen_rail_name') and active.get('gen_profile_name')

    @_rail_undo_transaction
    def execute(self, context):
        ob = context.active_object
        scene = context.scene
        if ob.get('gen_direct_backup_mesh'):
            clear_direct_profile_backup(ob)
        keys_to_remove = ['gen_rail_name', 'gen_profile_name', 'gen_settings', 'gen_align_pos',
                          'gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed',
                          'gen_direct_backup_mesh', 'gen_direct_backup_matrix',
                          'gen_last_rail_matrix_world']
        count = 0
        for key in keys_to_remove:
            if key in ob:
                del ob[key]
                count += 1
        if scene.get('pre_last_generated') == ob.name:
            scene['pre_last_generated'] = ''
            scene['pre_last_source'] = ''
            scene['pre_last_rail'] = ''
            if 'stored_align_pos' in scene:
                del scene['stored_align_pos']
        if count > 0:
            self.report({'INFO'}, '已应用：关联已断开，物体不再受控')
        else:
            self.report({'WARNING'}, '未找到关联属性')
        return {'FINISHED'}

# ═══════════════════════════════════════════════════════════
# Operator: 挤出主入口
# ═══════════════════════════════════════════════════════════
class MESH_OT_punten_naar_mesh(bpy.types.Operator):
    bl_idname = 'mesh.punten_naar_mesh'
    bl_label = '轮廓路径挤出'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'}

    @_rail_undo_transaction
    def execute(self, context):
        global IS_MULTI_GENERATING
        scene = context.scene
        active = context.active_object
        force_select_all_profile_before_loft = False

        if (not IS_MULTI_GENERATING and context.mode == 'OBJECT' and active
                and active.type in {'CURVE', 'MESH'}
                and (not active.get('gen_rail_name'))
                and (not active.get('gen_profile_name'))):
            profile_src, rail_objs, selection_error = _get_path_active_selection(context, allow_multi=True)
            if profile_src and len(rail_objs) > 1:
                generated_objs = []
                failed_names = []
                align = scene.get('stored_align_pos', 'MM')
                old_auto_update = bool(scene.get('rail_auto_update', False))
                IS_MULTI_GENERATING = True
                scene['rail_auto_update'] = False
                if context.mode != 'OBJECT':
                    _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                for rail_ob in rail_objs:
                    try:
                        _rail_op(bpy.ops.object.select_all, action='DESELECT')
                        profile_src.select_set(True)
                        context.view_layer.objects.active = profile_src
                        _rail_op(bpy.ops.object.duplicate)
                        profile_copy = context.object
                        profile_copy.name = f'{profile_src.name}_截面源_{rail_ob.name}'
                        _remove_custom_props(profile_copy, [
                            'gen_rail_name', 'gen_profile_name', 'gen_settings', 'gen_align_pos',
                            'gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed',
                            'gen_direct_backup_mesh', 'gen_direct_backup_matrix',
                            'gen_editable_profile_source', 'gen_source_generated_name',
                            'gen_last_rail_matrix_world', 'gen_profile_anchor_world',
                            'gen_profile_anchor_align_pos'])
                        _rail_op(bpy.ops.object.select_all, action='DESELECT')
                        rail_ob.select_set(True)
                        profile_copy.select_set(True)
                        context.view_layer.objects.active = rail_ob
                        res = _rail_op(bpy.ops.mesh.profiel_vlak, align_pos=align)
                        if 'CANCELLED' in res:
                            failed_names.append(rail_ob.name)
                            try:
                                if bpy.data.objects.get(profile_copy.name):
                                    bpy.data.objects.remove(profile_copy, do_unlink=True)
                            except Exception: pass
                            continue
                        res = _rail_op(bpy.ops.mesh.punten_naar_mesh)
                        if 'CANCELLED' in res:
                            failed_names.append(rail_ob.name)
                            continue
                        gen_name = scene.get('pre_last_generated', '')
                        gen_ob = bpy.data.objects.get(gen_name) if gen_name else None
                        if gen_ob:
                            generated_objs.append(gen_ob)
                        else:
                            failed_names.append(rail_ob.name)
                    except Exception as e:
                        failed_names.append(f'{rail_ob.name}({e})')
                        try:
                            if context.mode != 'OBJECT':
                                _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                        except Exception: pass
                scene['rail_auto_update'] = old_auto_update
                IS_MULTI_GENERATING = False
                if generated_objs:
                    try:
                        if context.mode != 'OBJECT':
                            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                        _rail_op(bpy.ops.object.select_all, action='DESELECT')
                        for obj in generated_objs:
                            if obj and obj.name in bpy.data.objects:
                                obj.select_set(True)
                        context.view_layer.objects.active = generated_objs[-1]
                    except Exception: pass
                    msg = f'已为 {len(generated_objs)} 条路径生成放样物体；每个放样物体都有独立可编辑截面'
                    if failed_names:
                        msg += f"；失败 {len(failed_names)} 条路径：{', '.join(failed_names[:5])}"
                    self.report({'INFO'}, msg)
                    return {'FINISHED'}
                else:
                    self.report({'ERROR'}, '多路径生成失败：请确认每个路径都是可识别的单一路径线/曲线')
                    return {'CANCELLED'}

        if active and _is_editable_profile_source(active):
            target_obj = None
            target_name = active.get('gen_source_generated_name', scene.get('pre_last_generated', ''))
            if target_name:
                target_obj = bpy.data.objects.get(target_name)
            if not target_obj:
                for obj in scene.objects:
                    if (obj.type == 'MESH'
                            and obj.get('gen_profile_name') == active.name
                            and obj.get('gen_rail_name')):
                        target_obj = obj; break
            if target_obj:
                align = target_obj.get('gen_align_pos', scene.get('stored_align_pos', 'MM'))
                return _safe_call_profiel_vlak(self, align_pos=align, target_name=target_obj.name)
            else:
                self.report({'ERROR'}, '找不到该截面关联的放样物体')
                return {'CANCELLED'}

        is_generated_object = active and active.get('gen_rail_name')
        if not is_generated_object:
            res = _safe_call_profiel_vlak(self, align_pos='MM')
            if 'CANCELLED' in res:
                return {'CANCELLED'}
        active = context.active_object

        if (active and active.get('gen_direct_preview')
                and active.get('gen_direct_inplace')
                and (not active.get('gen_profile_consumed'))):
            if context.mode == 'EDIT_MESH' and active.type == 'MESH':
                try: bmesh.update_edit_mesh(active.data)
                except Exception: pass
            if context.mode != 'OBJECT':
                _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
            force_select_all_profile_before_loft = True
            src_profile = active
            _set_mesh_origin_to_bounds_center_keep_world(src_profile)
            rail_name = src_profile.get('gen_rail_name', scene.get('pre_last_rail', ''))
            if not rail_name or not bpy.data.objects.get(rail_name):
                self.report({'ERROR'}, '找不到截面关联的路径，无法生成')
                return {'CANCELLED'}
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            src_profile.select_set(True)
            context.view_layer.objects.active = src_profile
            _rail_op(bpy.ops.object.duplicate)
            loft_ob = context.object
            loft_ob.name = '挤出物'
            _force_mesh_object_world_space_identity(loft_ob)
            col_name = '挤出物'
            col = bpy.data.collections.get(col_name)
            if not col:
                col = bpy.data.collections.new(col_name)
                context.scene.collection.children.link(col)
            try:
                if loft_ob.users_collection:
                    loft_ob.users_collection[0].objects.unlink(loft_ob)
            except Exception: pass
            try: col.objects.link(loft_ob)
            except Exception: pass
            clear_direct_profile_backup(src_profile, remove_backup_mesh=True)
            _remove_custom_props(src_profile, [
                'gen_profile_name', 'gen_profile_consumed', 'gen_direct_preview',
                'gen_direct_inplace', 'gen_direct_backup_mesh', 'gen_direct_backup_matrix'])
            src_profile['gen_editable_profile_source'] = True
            src_profile['gen_rail_name'] = rail_name
            src_profile['gen_source_generated_name'] = loft_ob.name
            if 'gen_profile_anchor_world' not in src_profile:
                try:
                    _store_profile_anchor_state(
                        src_profile,
                        Vector(_get_runtime_scene_prop(scene, 'punten_lijst', [Vector((0, 0, 0))])[0]),
                        src_profile.get('gen_align_pos', scene.get('stored_align_pos', 'MM')))
                except Exception: pass
            scene['pre_editable_profile_after_generate'] = src_profile.name
            _remove_custom_props(loft_ob, [
                'gen_direct_preview', 'gen_direct_inplace', 'gen_profile_consumed',
                'gen_direct_backup_mesh', 'gen_direct_backup_matrix',
                'gen_editable_profile_source', 'gen_source_generated_name'])
            loft_ob['gen_profile_name'] = src_profile.name
            loft_ob['gen_rail_name'] = rail_name
            loft_ob['gen_align_pos'] = src_profile.get('gen_align_pos', scene.get('stored_align_pos', 'MM'))
            _copy_profile_anchor_state(src_profile, loft_ob)
            if _has_gen_settings(src_profile):
                _set_gen_settings(loft_ob, _get_gen_settings(src_profile))
            _store_rail_matrix_state(src_profile, loft_ob, bpy.data.objects.get(rail_name))
            scene['pre_last_source'] = src_profile.name
            scene['pre_last_rail'] = rail_name
            scene['pre_last_generated'] = loft_ob.name
            _rail_op(bpy.ops.object.select_all, action='DESELECT')
            loft_ob.select_set(True)
            context.view_layer.objects.active = loft_ob

        was_extruded_before = scene.get('is_already_extruded', False)
        if context.mode == 'OBJECT':
            if context.object and context.object.type == 'MESH':
                _rail_op(bpy.ops.object.editmode_toggle)
            else:
                return {'CANCELLED'}
        if force_select_all_profile_before_loft and context.mode == 'EDIT_MESH':
            try:
                _rail_op(bpy.ops.mesh.select_mode, type='VERT')
                _rail_op(bpy.ops.mesh.select_all, action='SELECT')
            except Exception: pass

        ob = bpy.context.object
        me = ob.data
        bm = bmesh.from_edit_mesh(me)

        is_z_up_mode = scene.get('rail_z_up', False)
        is_loop_calc = _get_runtime_scene_prop(scene, 'is_loop_calc', False)

        def get_selected_verts():
            return [v for v in bm.verts if v.select]

        def verplaatsen(lijst):
            for elm in lijst:
                if elm[1] is not None:
                    bm.verts[elm[0].index].co = elm[1]

        def punt_snijpunt(punten, richtvector, vp, vn):
            punten_snijpunten = []
            bm.verts.ensure_lookup_table()
            for p in punten:
                p_1 = bm.verts[p.index].co
                p_2 = p_1 + richtvector
                snijpp = mathutils.geometry.intersect_line_plane(p_1, p_2, vp, vn)
                punten_snijpunten.append([p, snijpp])
            return punten_snijpunten

        def correct_twist(verts, center, plane_normal, ref_up_vector):
            world_z = Vector((0, 0, 1))
            ideal_up = world_z - plane_normal * world_z.dot(plane_normal)
            if ideal_up.length < 0.001:
                return ref_up_vector
            ideal_up.normalize()
            current_up_proj = ref_up_vector - plane_normal * ref_up_vector.dot(plane_normal)
            if current_up_proj.length < 0.001:
                current_up_proj = ref_up_vector
            current_up_proj.normalize()
            rot_quat = current_up_proj.rotation_difference(ideal_up)
            rot_mat = rot_quat.to_matrix().to_4x4()
            trans_from_origin = Matrix.Translation(center) @ rot_mat @ Matrix.Translation(-center)
            for v in verts:
                v.co = trans_from_origin @ v.co
            return ideal_up

        initial_verts = get_selected_verts()
        initial_vert_indices = {v.index for v in initial_verts}
        if not initial_verts:
            return {'CANCELLED'}

        try:
            seg_vecs = [Vector(v) for v in _get_runtime_scene_prop(bpy.context.scene, 'richt_lijnen', [])]
            points_list = [Vector(p) for p in _get_runtime_scene_prop(bpy.context.scene, 'punten_lijst', [])]
            bisector_normals = [Vector(n) for n in _get_runtime_scene_prop(bpy.context.scene, 'norm_lijst', [])]
            eindig = _get_runtime_scene_prop(bpy.context.scene, 'eindig', True)
        except Exception:
            return {'CANCELLED'}
        try:
            corner_rot_steps = _get_runtime_scene_prop(bpy.context.scene, 'corner_rot_steps', None) or {}
        except Exception:
            corner_rot_steps = {}

        track_up = Vector((0, 0, 1))

        if is_loop_calc:
            start_tangent = Vector(_get_runtime_scene_prop(bpy.context.scene, 'start_tangent', seg_vecs[0]))
            start_bisector_normal = bisector_normals[0]
            start_point = points_list[0]
            current_verts = get_selected_verts()
            verplaatsen(punt_snijpunt(current_verts, start_tangent, start_point, start_bisector_normal))
            bm.normal_update()
            bm.verts.ensure_lookup_table()

        num_steps = len(seg_vecs)
        last_step_verts = []
        for t in range(num_steps):
            _rail_op(bpy.ops.mesh.extrude_context)
            punten = get_selected_verts()
            rot_info = corner_rot_steps.get(t) if corner_rot_steps else None
            if rot_info:
                # 拐角旋转步：绕拐角轴做刚体旋转（对称斜切，无剪切无缺口）
                try:
                    rot_origin = Vector(rot_info[0])
                    rot_axis = Vector(rot_info[1])
                    rot_ang = float(rot_info[2])
                    rot_mat = Matrix.Rotation(rot_ang, 4, rot_axis)
                    rot_final = Matrix.Translation(rot_origin) @ rot_mat @ Matrix.Translation(-rot_origin)
                    bm.verts.ensure_lookup_table()
                    for elm in punten:
                        bm.verts[elm[0].index].co = rot_final @ bm.verts[elm[0].index].co
                except Exception:
                    pass
            else:
                rv = seg_vecs[t]
                vp = points_list[t + 1]
                vn = bisector_normals[t + 1]
                verplaatsen(punt_snijpunt(punten, rv, vp, vn))
                if is_z_up_mode:
                    track_up = correct_twist(punten, vp, vn, track_up)
            last_step_verts = punten

        if is_loop_calc:
            end_verts = get_selected_verts()
            end_vert_indices = {v.index for v in end_verts}
            faces_to_kill = []
            bm.faces.ensure_lookup_table()
            for f in bm.faces:
                is_start_face = all((v.index in initial_vert_indices for v in f.verts))
                is_end_face = all((v.index in end_vert_indices for v in f.verts))
                if is_start_face or is_end_face:
                    faces_to_kill.append(f)
            if faces_to_kill:
                bmesh.ops.delete(bm, geom=faces_to_kill, context='FACES')
            _rail_op(bpy.ops.mesh.select_all, action='SELECT')
            _rail_op(bpy.ops.mesh.remove_doubles, threshold=0.001)
            _rail_op(bpy.ops.mesh.select_linked, delimit={'SEAM'})
        else:
            _rail_op(bpy.ops.mesh.select_linked, delimit={'SEAM'})
            _rail_op(bpy.ops.mesh.normals_make_consistent, inside=False)
            _rail_op(bpy.ops.mesh.select_all, action='DESELECT')
            bm.faces.ensure_lookup_table()
            start_faces = set()
            for v_idx in initial_vert_indices:
                if v_idx < len(bm.verts):
                    for f in bm.verts[v_idx].link_faces:
                        if all((v.index in initial_vert_indices for v in f.verts)):
                            start_faces.add(f)
            end_indices = {v.index for v in last_step_verts}
            end_faces = set()
            for v in last_step_verts:
                if v.index < len(bm.verts):
                    bm_v = bm.verts[v.index]
                    for f in bm_v.link_faces:
                        if all((vv.index in end_indices for vv in f.verts)):
                            end_faces.add(f)
            del_faces = []
            if not scene.rail_cap_start:
                del_faces.extend(list(start_faces))
            else:
                for f in start_faces: f.select = True
            if not scene.rail_cap_end:
                del_faces.extend(list(end_faces))
            else:
                for f in end_faces: f.select = True
            if del_faces:
                bmesh.ops.delete(bm, geom=del_faces, context='FACES')

        if not is_loop_calc:
            bm.select_flush(True)
        # 清理拐角旋转步产生的退化几何（零长度边/零面积面）
        try:
            bmesh.ops.dissolve_degenerate(bm, dist=1e-5, edges=list(bm.edges))
        except Exception:
            pass
        bmesh.update_edit_mesh(me)
        if is_loop_calc:
            _rail_op(bpy.ops.mesh.normals_make_consistent, inside=False)

        context.scene['is_already_extruded'] = True
        if context.mode != 'OBJECT':
            _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
        if context.object and context.object.get('gen_direct_inplace'):
            clear_direct_profile_backup(context.object, remove_backup_mesh=False)
            context.object['gen_profile_consumed'] = True
        if context.object:
            context.view_layer.objects.active = context.object
            context.object.select_set(True)
            try: _rail_op(bpy.ops.object.shade_auto_smooth)
            except Exception:
                try:
                    _rail_op(bpy.ops.object.shade_smooth)
                    context.object.data.use_auto_smooth = True
                except Exception: pass

        if (not context.scene.get('rail_auto_update', False)
                and _has_runtime_scene_prop(context.scene, 'start_tangent')):
            _del_runtime_scene_prop(context.scene, 'start_tangent')

        editable_profile_name = context.scene.get('pre_editable_profile_after_generate', '')
        if editable_profile_name:
            try: del context.scene['pre_editable_profile_after_generate']
            except Exception: pass
            editable_profile = bpy.data.objects.get(editable_profile_name)
            if editable_profile:
                try:
                    if context.mode != 'OBJECT':
                        _rail_op(bpy.ops.object.mode_set, mode='OBJECT')
                    _set_mesh_origin_to_bounds_center_keep_world(editable_profile)
                    _rail_op(bpy.ops.object.select_all, action='DESELECT')
                    editable_profile.select_set(True)
                    context.view_layer.objects.active = editable_profile
                except Exception: pass

        return {'FINISHED'}

class MESH_OT_reset_path_mapping(bpy.types.Operator):
    bl_idname = 'mesh.reset_path_mapping'
    bl_label = '重置路径映射'
    bl_options = {'UNDO'}

    @_rail_undo_transaction
    def execute(self, context):
        scene = context.scene
        scene.rail_map_start = 0.0
        scene.rail_map_end = 1.0
        return {'FINISHED'}

# ═══════════════════════════════════════════════════════════
# 视图绘制
# ═══════════════════════════════════════════════════════════
shader = gpu.shader.from_builtin('UNIFORM_COLOR')

def draw_direction_arrow():
    if bpy.context.mode not in {'EDIT_MESH', 'EDIT_CURVE'}:
        return
    scene = bpy.context.scene
    if not _has_runtime_scene_prop(scene, 'start_tangent'):
        return
    last_gen_name = scene.get('pre_last_generated', '')
    last_rail_name = scene.get('pre_last_rail', '')
    if last_gen_name and last_gen_name not in bpy.data.objects:
        if _has_runtime_scene_prop(scene, 'start_tangent'):
            _del_runtime_scene_prop(scene, 'start_tangent')
        return
    if last_rail_name and last_rail_name not in bpy.data.objects:
        if _has_runtime_scene_prop(scene, 'start_tangent'):
            _del_runtime_scene_prop(scene, 'start_tangent')
        return
    if (not _has_runtime_scene_prop(scene, 'punten_lijst')
            or len(_get_runtime_scene_prop(scene, 'punten_lijst', [])) == 0):
        return
    try:
        offset = Vector(_get_runtime_scene_prop(scene, 'loc_oorsprong', (0, 0, 0)))
        start_pos_local = Vector(_get_runtime_scene_prop(scene, 'punten_lijst', [Vector((0, 0, 0))])[0])
        origin = offset + start_pos_local
        tangent = Vector(_get_runtime_scene_prop(scene, 'start_tangent', Vector((0, 0, 1)))).normalized()
        res = scene.get('rail_gen_resolution', 24)
        base_scale = 5.0 / res if res > 0 else 1.0
        scale = max(0.3, min(base_scale, 2.0))
        end_pos = origin + tangent * scale
        arrow_len = scale * 0.25
        arrow_width = scale * 0.08
        ref_up = Vector((0, 0, 1))
        if abs(tangent.dot(ref_up)) > 0.95:
            ref_up = Vector((0, 1, 0))
        right = tangent.cross(ref_up).normalized() * arrow_width
        ortho = tangent.cross(right).normalized() * arrow_width
        head_base = origin + tangent * (scale - arrow_len)
        coords = [origin, end_pos,
                  end_pos, head_base + right,
                  end_pos, head_base - right,
                  end_pos, head_base + ortho,
                  end_pos, head_base - ortho]
        batch = batch_for_shader(shader, 'LINES', {'pos': coords})
        shader.bind()
        shader.uniform_float('color', (1.0, 0.6, 0.0, 0.9))
        gpu.state.line_width_set(2.0)
        batch.draw(shader)
        gpu.state.line_width_set(1.0)
    except Exception:
        return

_draw_handler = None

# ═══════════════════════════════════════════════════════════
# 界面语言（中文 / English）
# ═══════════════════════════════════════════════════════════
_UI_TEXTS = {
    'zh': {
        'lang': '界面语言',
        'select_rail': '选中路径', 'select_profile': '选中截面',
        'apply_final': '应用物体（不再实时更新）',
        'segments': '分段', 'auto_update': '实时更新',
        'mirror_x': 'X轴镜像', 'mirror_y': 'Y轴镜像',
        'reverse_path': '⇄ 切换路径首尾', 'keep_z_up': '⬆ 保持Z轴向上',
        'mapping': '路径映射', 'map_start': '开始映射', 'map_end': '结束映射',
        'map_reset': '重置映射',
        'cap_start': '起始封口', 'cap_end': '结束封口',
        'squared_start': '起始正交', 'squared_end': '结束正交',
        'closed_path': '闭合路径', 'snap_profile': '截面吸附到路径',
        'corner_sharp': '拐角锐化', 'corner_angle': '角度',
        'corner_segments': '拐角分段', 'corner_radius': '圆角半径',
        'generate': '执行路径跟随', 'update_btn': '路径跟随',
    },
    'en': {
        'lang': 'Interface Language',
        'select_rail': 'Select Rail', 'select_profile': 'Select Profile',
        'apply_final': 'Apply Final Object',
        'segments': 'Segments', 'auto_update': 'Live Update',
        'mirror_x': 'Mirror X', 'mirror_y': 'Mirror Y',
        'reverse_path': '⇄ Reverse Path', 'keep_z_up': '⬆ Keep Z Up',
        'mapping': 'Path Mapping', 'map_start': 'Start', 'map_end': 'End',
        'map_reset': 'Reset Mapping',
        'cap_start': 'Cap Start', 'cap_end': 'Cap End',
        'squared_start': 'Square Start', 'squared_end': 'Square End',
        'closed_path': 'Closed Path', 'snap_profile': 'Snap Profile to Path',
        'corner_sharp': 'Sharp Corners', 'corner_angle': 'Angle',
        'corner_segments': 'Corner Segments', 'corner_radius': 'Radius',
        'generate': 'Generate Path Follow', 'update_btn': 'Update Path Follow',
    },
}

def _ui_preferences():
    try:
        import bpy as _bpy
        for key in (globals().get('__name__'), 'Path_Follow_in_Blender'):
            if not key: continue
            try: return _bpy.context.preferences.addons[key].preferences
            except Exception: continue
    except Exception:
        pass
    return None

def _ui_lang():
    prefs = _ui_preferences()
    if prefs is not None:
        try: return str(getattr(prefs, 'language', 'zh') or 'zh')
        except Exception: return 'zh'
    return 'zh'

def _t(key):
    lang = 'en' if _ui_lang() == 'en' else 'zh'
    try:
        return _UI_TEXTS[lang].get(key) or _UI_TEXTS['zh'].get(key) or key
    except Exception:
        return key

class PathFollowPreferences(bpy.types.AddonPreferences):
    bl_idname = 'Path_Follow_in_Blender'

    language: bpy.props.EnumProperty(
        name='Language / 界面语言',
        description='选择面板界面语言 / Choose UI language for the panel',
        items=[('zh', '中文 (Chinese)', '使用中文界面'),
               ('en', 'English', 'Use English interface')],
        default='zh')

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.label(text='Language / 界面语言')
        row.prop(self, 'language', expand=True)

class PATHFOLLOW_OT_toggle_language(bpy.types.Operator):
    bl_idname = 'pathfollow.toggle_language'
    bl_label = '切换语言 / Toggle Language'
    bl_description = '切换面板界面语言 / Switch panel UI language'
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _ui_preferences() is not None

    def execute(self, context):
        prefs = _ui_preferences()
        if prefs is None:
            return {'CANCELLED'}
        try:
            prefs.language = 'en' if _ui_lang() == 'zh' else 'zh'
        except Exception:
            return {'CANCELLED'}
        try: _apply_operator_labels()
        except Exception: pass
        return {'FINISHED'}

# ═══════════════════════════════════════════════════════════
# 侧边栏 UI
# ═══════════════════════════════════════════════════════════
class VIEW_PT_etrude_mesh(bpy.types.Panel):
    bl_category = 'Path Follow'
    bl_label = 'Path Follow'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        # 面板右上角：仅图标的语言切换按钮
        try:
            if _ui_preferences() is not None:
                self.layout.operator('pathfollow.toggle_language',
                                     text='', icon='WORLD')
        except Exception:
            pass

    def draw(self, context):
        try: _sync_mapping_panel_to_active(context)
        except Exception: pass
        layout = self.layout
        scene = context.scene
        active_ob = context.active_object
        is_generated = (active_ob and active_ob.get('gen_rail_name')
                        and active_ob.get('gen_profile_name'))
        if is_generated:
            box_sel = layout.box()
            col = box_sel.column(align=True)
            row = col.row(align=True)
            row.operator('mesh.select_rail', text=_t('select_rail'), icon='CURVE_DATA')
            row.operator('mesh.select_profile', text=_t('select_profile'), icon='MESH_DATA')
            row = col.row(align=True)
            row.scale_y = 1.3
            row.operator('mesh.apply_rail_follow', text=_t('apply_final'), icon='CHECKMARK')

        target_rail_ob = find_active_rail(context)
        if not target_rail_ob and scene.get('pre_last_rail'):
            target_rail_ob = bpy.data.objects.get(scene['pre_last_rail'])
        if target_rail_ob and target_rail_ob.type == 'CURVE':
            box = layout.box()
            row = box.row(align=True)
            icon_style = 'LINKED' if is_generated else 'CURVE_DATA'
            split = row.split(factor=0.6)
            split.label(text=f'{target_rail_ob.name}', icon=icon_style)
            split.prop(scene, 'rail_gen_resolution', text=_t('segments'))

        box_ops = layout.box()
        col = box_ops.column(align=True)
        row = col.row(align=True)
        row.operator('mesh.profiel_vlak', text='↖').align_pos = 'TL'
        row.operator('mesh.profiel_vlak', text='↑').align_pos = 'TM'
        row.operator('mesh.profiel_vlak', text='↗').align_pos = 'TR'
        row = col.row(align=True)
        row.operator('mesh.profiel_vlak', text='←').align_pos = 'ML'
        row.operator('mesh.profiel_vlak', text='⊙').align_pos = 'MM'
        row.operator('mesh.profiel_vlak', text='→').align_pos = 'MR'
        row = col.row(align=True)
        row.operator('mesh.profiel_vlak', text='↙').align_pos = 'BL'
        row.operator('mesh.profiel_vlak', text='↓').align_pos = 'BM'
        row.operator('mesh.profiel_vlak', text='↘').align_pos = 'BR'
        col.separator()
        row = col.row()
        row.prop(scene, 'rail_auto_update', text=_t('auto_update'), icon='PLAY')
        col.separator()
        row = col.row(align=True)
        is_mx = context.scene.get('rail_mirror_x', False)
        row.operator('mesh.spiegel_profiel', text=_t('mirror_x'),
                     icon='CHECKBOX_HLT' if is_mx else 'CHECKBOX_DEHLT').axis = 'X'
        is_my = context.scene.get('rail_mirror_y', False)
        row.operator('mesh.spiegel_profiel', text=_t('mirror_y'),
                     icon='CHECKBOX_HLT' if is_my else 'CHECKBOX_DEHLT').axis = 'Y'
        row = col.row(align=True)
        row.operator('mesh.rotate_profile_step', text='⟲ 90°').direction = 'CCW'
        row.operator('mesh.rotate_profile_step', text='90° ⟳').direction = 'CW'
        row = col.row(align=True)
        row.scale_y = 1.2
        row.operator('mesh.wissel_richting', text=_t('reverse_path'), icon='FILE_REFRESH')
        row = col.row(align=True)
        row.scale_y = 1.2
        is_z = context.scene.get('rail_z_up', False)
        row.operator('mesh.toggle_z_up', text=_t('keep_z_up'),
                     icon='CHECKBOX_HLT' if is_z else 'CHECKBOX_DEHLT')
        col.separator()
        map_box = col.box()
        map_col = map_box.column(align=True)
        map_col.label(text=_t('mapping'))
        row = map_col.row(align=True)
        row.prop(scene, 'rail_map_start', text=_t('map_start'), slider=True)
        row = map_col.row(align=True)
        row.prop(scene, 'rail_map_end', text=_t('map_end'), slider=True)
        row = map_col.row(align=True)
        row.operator('mesh.reset_path_mapping', text=_t('map_reset'), icon='FILE_REFRESH')
        layout.separator()
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(scene, 'rail_cap_start', text=_t('cap_start'), toggle=True)
        row.prop(scene, 'rail_cap_end', text=_t('cap_end'), toggle=True)
        row = col.row(align=True)
        row.prop(scene, 'rail_flatten_start', text=_t('squared_start'), toggle=True)
        row.prop(scene, 'rail_flatten_end', text=_t('squared_end'), toggle=True)
        row = col.row(align=True)
        corner_on = bool(getattr(scene, 'rail_corner_sharp', True))
        row.prop(scene, 'rail_corner_sharp', text=_t('corner_sharp'),
                 toggle=True,
                 icon='CHECKBOX_HLT' if corner_on else 'CHECKBOX_DEHLT')
        row.prop(scene, 'rail_corner_angle', text=_t('corner_angle'))
        row = col.row(align=True)
        row.prop(scene, 'rail_corner_segments', text=_t('corner_segments'))
        row.prop(scene, 'rail_corner_radius', text=_t('corner_radius'), slider=True)
        row = col.row(align=True)
        row.scale_y = 1.2
        row.prop(scene, 'rail_make_loop', text=_t('closed_path'), toggle=True, icon='MESH_CIRCLE')
        # 截面吸附开关
        snap = bool(getattr(scene, 'rail_snap_profile_to_path', False))
        row = col.row(align=True)
        row.scale_y = 1.2
        row.prop(scene, 'rail_snap_profile_to_path',
                 text=_t('snap_profile'),
                 toggle=True,
                 icon='CHECKBOX_HLT' if snap else 'CHECKBOX_DEHLT')
        col = layout.column()
        col.scale_y = 2.2
        btn_text = _t('generate')
        if context.scene.get('is_already_extruded', False):
            btn_text = _t('update_btn')
        col.operator('mesh.punten_naar_mesh', text=btn_text, icon='MOD_SCREW')

# ═══════════════════════════════════════════════════════════
# 注册 / 注销
# ═══════════════════════════════════════════════════════════
classes = [
    MESH_OT_select_rail, MESH_OT_select_profile, MESH_OT_apply_rail_follow,
    MESH_OT_puntenlijst, MESH_OT_punten_naar_mesh, MESH_OT_profiel_vlak,
    MESH_OT_spiegel_profiel, MESH_OT_rotate_profile_step,
    MESH_OT_wissel_richting, MESH_OT_toggle_z_up,
    MESH_OT_reset_path_mapping, VIEW_PT_etrude_mesh,
    PATHFOLLOW_OT_toggle_language, PathFollowPreferences,
]

# 算子在搜索菜单里的英文名称（英文界面时生效）
_OPERATOR_LABELS_EN = {
    MESH_OT_select_rail: 'Select Rail',
    MESH_OT_select_profile: 'Select Profile',
    MESH_OT_apply_rail_follow: 'Apply Final Object',
    MESH_OT_puntenlijst: 'Compute Path Points',
    MESH_OT_punten_naar_mesh: 'Path Follow Generate',
    MESH_OT_profiel_vlak: 'Align Profile',
    MESH_OT_spiegel_profiel: 'Mirror Profile',
    MESH_OT_rotate_profile_step: 'Rotate Profile 90°',
    MESH_OT_wissel_richting: 'Reverse Path',
    MESH_OT_toggle_z_up: 'Toggle Keep Z Up',
    MESH_OT_reset_path_mapping: 'Reset Path Mapping',
}

def _apply_operator_labels():
    English = _ui_lang() == 'en'
    for cls, en_label in _OPERATOR_LABELS_EN.items():
        try:
            if not hasattr(cls, '_original_bl_label'):
                cls._original_bl_label = cls.bl_label
            cls.bl_label = en_label if English else cls._original_bl_label
        except Exception:
            pass

def register():
    global RAIL_OPERATOR_TRANSACTION_DEPTH, RAIL_UNDO_REDO_RELEASE_TIMER
    global _draw_handler, RAIL_UNDO_REDO_GUARD, RAIL_FORCE_NO_UNDO
    global RAIL_POST_UNDO_REBUILD_REQUESTED

    RAIL_UNDO_REDO_GUARD = False
    RAIL_FORCE_NO_UNDO = False
    RAIL_OPERATOR_TRANSACTION_DEPTH = 0
    RAIL_UNDO_REDO_RELEASE_TIMER = False
    RAIL_POST_UNDO_REBUILD_REQUESTED = False

    _purge_unsafe_runtime_idprops()
    _migrate_legacy_gen_settings()

    for c in classes:
        bpy.utils.register_class(c)
    try: _apply_operator_labels()
    except Exception: pass

    bpy.types.Scene.rail_gen_resolution = bpy.props.IntProperty(
        name='Segments', default=24, min=2,
        get=get_resolution_proxy, set=set_resolution_proxy)
    bpy.types.Scene.rail_cap_start = bpy.props.BoolProperty(
        name='Cap Start', default=True, update=update_gen_mesh)
    bpy.types.Scene.rail_cap_end = bpy.props.BoolProperty(
        name='Cap End', default=True, update=update_gen_mesh)
    bpy.types.Scene.rail_make_loop = bpy.props.BoolProperty(
        name='Closed Path', default=False, update=update_gen_mesh)
    bpy.types.Scene.rail_flatten_start = bpy.props.BoolProperty(
        name='Square Start', default=False,
        description='强制起始截面平行于最接近的全局平面 / Force start section onto nearest global plane',
        update=update_gen_mesh)
    bpy.types.Scene.rail_flatten_end = bpy.props.BoolProperty(
        name='Square End', default=False,
        description='强制结束截面平行于最接近的全局平面 / Force end section onto nearest global plane',
        update=update_gen_mesh)
    # 新增：拐角锐化
    bpy.types.Scene.rail_corner_sharp = bpy.props.BoolProperty(
        name='Sharp Corners', default=True,
        description='在急拐角处让拐角截面垂直于进入段，生成水密的锐边直角 / '
                    'At sharp corners the section is perpendicular to the incoming segment (watertight edge)',
        update=update_gen_mesh)
    bpy.types.Scene.rail_corner_angle = bpy.props.FloatProperty(
        name='Corner Angle',
        description='相邻路径段之间的夹角超过该角度时视为拐角 / Turn angle above which a corner is detected',
        default=math.radians(30.0), min=math.radians(5.0), max=math.radians(89.0),
        subtype='ANGLE', update=update_gen_mesh)
    bpy.types.Scene.rail_corner_segments = bpy.props.IntProperty(
        name='Corner Segments', default=0, min=0, max=24,
        description='拐角圆弧的分段数：大于 0 时拐角生成圆弧过渡 / Arc resolution for rounded corners; 0 = off',
        update=update_gen_mesh)
    bpy.types.Scene.rail_corner_radius = bpy.props.FloatProperty(
        name='Corner Radius', default=0.35, min=0.05, max=0.45, subtype='FACTOR',
        description='圆角半径占较短邻段长度的比例 / Fillet radius as a fraction of the shorter adjacent segment',
        update=update_gen_mesh)
    bpy.types.Scene.rail_map_start = bpy.props.FloatProperty(
        name='Mapping Start', description='控制截面从路径长度的哪个百分比位置开始生成',
        default=0.0, min=0.0, max=1.0, subtype='FACTOR', update=update_path_mapping)
    bpy.types.Scene.rail_map_end = bpy.props.FloatProperty(
        name='Mapping End', description='控制截面生成到路径长度的哪个百分比位置结束',
        default=1.0, min=0.0, max=1.0, subtype='FACTOR', update=update_path_mapping)
    bpy.types.Scene.rail_auto_update = bpy.props.BoolProperty(
        name='Live Update', description='编辑路径或轮廓时，自动更新挤出模型',
        default=False, update=update_gen_mesh)
    # 新增：截面吸附开关
    bpy.types.Scene.rail_snap_profile_to_path = bpy.props.BoolProperty(
        name='Snap Profile to Path',
        description='执行路径跟随时，将原截面物体吸附到路径起点；'
                    '关闭时原截面保留在原位，放样使用其副本',
        default=False)

    if rail_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(rail_depsgraph_handler)
    if rail_undo_pre not in bpy.app.handlers.undo_pre:
        bpy.app.handlers.undo_pre.append(rail_undo_pre)
    if rail_undo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(rail_undo_post)
    if rail_redo_pre not in bpy.app.handlers.redo_pre:
        bpy.app.handlers.redo_pre.append(rail_redo_pre)
    if rail_redo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(rail_redo_post)
    if not bpy.app.timers.is_registered(rail_mapping_active_watch_timer):
        bpy.app.timers.register(rail_mapping_active_watch_timer, first_interval=0.25)

    bpy.types.Scene.show_rail_arrow = bpy.props.BoolProperty(
        name='显示方向', default=True,
        description='在视图中绘制路径放样方向箭头')

    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            draw_direction_arrow, (), 'WINDOW', 'POST_VIEW')

def unregister():
    global _draw_handler
    _purge_unsafe_runtime_idprops()
    try: _SCENE_RUNTIME_CACHE.clear()
    except Exception: pass

    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None

    if rail_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(rail_depsgraph_handler)
    if rail_undo_pre in bpy.app.handlers.undo_pre:
        bpy.app.handlers.undo_pre.remove(rail_undo_pre)
    if rail_undo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(rail_undo_post)
    if rail_redo_pre in bpy.app.handlers.redo_pre:
        bpy.app.handlers.redo_pre.remove(rail_redo_pre)
    if rail_redo_post in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(rail_redo_post)
    if bpy.app.timers.is_registered(run_update_operator):
        bpy.app.timers.unregister(run_update_operator)
    if bpy.app.timers.is_registered(_rail_release_undo_redo_guard):
        bpy.app.timers.unregister(_rail_release_undo_redo_guard)
    if bpy.app.timers.is_registered(rail_mapping_active_watch_timer):
        bpy.app.timers.unregister(rail_mapping_active_watch_timer)

    for c in classes:
        bpy.utils.unregister_class(c)

    del bpy.types.Scene.rail_gen_resolution
    del bpy.types.Scene.rail_cap_start
    del bpy.types.Scene.rail_cap_end
    del bpy.types.Scene.rail_make_loop
    del bpy.types.Scene.rail_flatten_start
    del bpy.types.Scene.rail_flatten_end
    if hasattr(bpy.types.Scene, 'rail_corner_sharp'):
        del bpy.types.Scene.rail_corner_sharp
    if hasattr(bpy.types.Scene, 'rail_corner_angle'):
        del bpy.types.Scene.rail_corner_angle
    if hasattr(bpy.types.Scene, 'rail_corner_segments'):
        del bpy.types.Scene.rail_corner_segments
    if hasattr(bpy.types.Scene, 'rail_corner_radius'):
        del bpy.types.Scene.rail_corner_radius
    del bpy.types.Scene.rail_map_start
    del bpy.types.Scene.rail_map_end
    if hasattr(bpy.types.Scene, 'rail_auto_update'):
        del bpy.types.Scene.rail_auto_update
    if hasattr(bpy.types.Scene, 'rail_snap_profile_to_path'):
        del bpy.types.Scene.rail_snap_profile_to_path

if __name__ == '__main__':
    register()
