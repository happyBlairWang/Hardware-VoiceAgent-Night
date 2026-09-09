# Builds a schematic 3D view of the vintage-phone wiring.
# Boards are labelled blocks, not photo-accurate PCBs -- the point is which
# part feeds which, and which pin each wire lands on.
import bpy, math
from mathutils import Vector

# ---------------------------------------------------------------- reset
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

def mat(name, rgb, rough=0.55, emit=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1)
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = (*rgb, 1)
        b.inputs["Emission Strength"].default_value = emit
    return m

M = {
    "pcb_esp":  mat("pcb_esp",  (0.06, 0.09, 0.13)),
    "pcb_mic":  mat("pcb_mic",  (0.13, 0.35, 0.24)),
    "pcb_amp":  mat("pcb_amp",  (0.16, 0.28, 0.45)),
    "pad":      mat("pad",      (0.72, 0.55, 0.20), 0.35),
    "ink":      mat("ink",      (0.05, 0.09, 0.13), 0.9),
    "ink_hi":   mat("ink_hi",   (0.96, 0.97, 0.98), 0.9),
    "dim":      mat("dim",      (0.34, 0.40, 0.47), 0.9),
    "dim_hi":   mat("dim_hi",   (0.66, 0.72, 0.78), 0.9),
    "w_pwr":    mat("w_pwr",    (0.78, 0.16, 0.13), 0.4),   # red   3V3 / 5V
    "w_gnd":    mat("w_gnd",    (0.10, 0.10, 0.12), 0.5),   # black GND
    "w_mic":    mat("w_mic",    (0.25, 0.70, 0.40), 0.4),   # green mic signal
    "w_aud":    mat("w_aud",    (0.30, 0.60, 0.90), 0.4),   # blue  audio out
    "w_key":    mat("w_key",    (0.85, 0.62, 0.20), 0.4),   # amber keypad
    "spk":      mat("spk",      (0.14, 0.14, 0.16), 0.7),
    "cone":     mat("cone",     (0.35, 0.30, 0.26), 0.85),
    "ground":   mat("ground",   (0.80, 0.79, 0.76), 0.98),
}

def board(name, loc, size, material, z=0.18):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(loc[0], loc[1], z/2))
    o = bpy.context.object; o.name = name
    o.scale = (size[0], size[1], z)
    o.data.materials.append(M[material])
    bpy.ops.object.shade_smooth_by_angle()
    return o

def pad(loc, label=None, material="pad", r=0.16, above=False):
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=0.22,
                                        location=(loc[0], loc[1], 0.24), vertices=16)
    bpy.context.object.data.materials.append(M[material])
    if label:
        text(label, (loc[0], loc[1] + (0.78 if above else -0.72)), size=0.44, material="ink")

def text(body, loc, size=0.4, material="ink", align="CENTER", z=0.02):
    bpy.ops.object.text_add(location=(loc[0], loc[1], z + size * 0.62))
    t = bpy.context.object
    t.data.body = body
    t.data.size = size
    t.data.align_x = align
    t.data.align_y = "CENTER"
    t.data.extrude = 0.012
    t.rotation_euler = (math.radians(48), 0, 0)     # lean toward the camera
    t.data.materials.append(M[material])
    return t

def wire(p_from, p_to, material, arc=1.5, thick=0.075):
    """A cable that lifts off the bench, arcs over, and lands."""
    a, b = Vector((*p_from, 0.28)), Vector((*p_to, 0.28))
    mid = (a + b) / 2 + Vector((0, 0, arc))
    cu = bpy.data.curves.new("wire", "CURVE"); cu.dimensions = "3D"
    cu.bevel_depth = thick; cu.bevel_resolution = 4; cu.resolution_u = 24
    sp = cu.splines.new("BEZIER"); sp.bezier_points.add(2)
    for i, co in enumerate((a, mid, b)):
        bp = sp.bezier_points[i]; bp.co = co; bp.handle_left_type = bp.handle_right_type = "AUTO"
    ob = bpy.data.objects.new("wire", cu); scene.collection.objects.link(ob)
    ob.data.materials.append(M[material])
    return ob

def zone(name, cx, cy, w, h, rgb):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1); b.inputs["Roughness"].default_value = 1.0
    bpy.ops.mesh.primitive_plane_add(size=1, location=(cx, cy, 0.006))
    o = bpy.context.object; o.scale = (w, h, 1); o.data.materials.append(m)

def flow_arrow(x0, x1, y, material, label):
    bpy.ops.mesh.primitive_cube_add(size=1, location=((x0 + x1)/2 - 0.5, y, 0.05))
    sh = bpy.context.object; sh.scale = (abs(x1 - x0) - 1.4, 0.20, 0.10)
    sh.data.materials.append(M[material])
    bpy.ops.mesh.primitive_cone_add(radius1=0.62, radius2=0, depth=1.5,
                                    location=(x1 - 0.4, y, 0.05), vertices=32)
    hd = bpy.context.object
    hd.rotation_euler = (0, math.radians(90), 0)      # point along +X
    hd.scale = (0.20, 1, 1)                           # flatten against the bench
    hd.data.materials.append(M[material])
    text(label, ((x0 + x1)/2, y + 0.95), size=0.60, material="ink")

# ---------------------------------------------------------------- bench
bpy.ops.mesh.primitive_plane_add(size=90, location=(0, 0, -0.02))
bpy.context.object.data.materials.append(M["ground"])

zone("z_in",  (-9.3), 4.0, 11.5, 7.4, (0.80, 0.85, 0.79))
zone("z_out", ( 13.6), 5.0, 16.5, 8.6, (0.78, 0.82, 0.88))
flow_arrow(-14.0, -6.6, 8.6, "w_mic", "YOUR VOICE  —  IN")
flow_arrow(  6.6, 21.0, 8.6, "w_aud", "AGENT'S VOICE  —  OUT")

# ---------------------------------------------------------------- ESP32
board("ESP32", (0, 0), (11.5, 5.2), "pcb_esp")
text("ESP32", (0, 1.4), size=0.95, material="ink_hi", z=0.24)
text("the brain", (0, 0.35), size=0.42, material="dim_hi", z=0.24)

esp = {
    "3V3":  (-5.0,  2.6),
    "GND1": (-3.5,  2.6),
    "34":   (-2.0,  2.6),
    "25":   ( 2.0,  2.6),
    "GND2": ( 3.5,  2.6),
    "5V":   ( 5.0,  2.6),
}
for k, v in esp.items():
    pad(v, {"GND1": "GND", "GND2": "GND", "34": "34",
            "25": "25", "3V3": "3V3", "5V": "5V"}[k], above=True)

key_pins = {}
for i, g in enumerate(["13", "14", "16", "17", "18", "19", "21", "22"]):
    x = -3.85 + i * 1.1
    key_pins[g] = (x, -2.6)
    pad((x, -2.6), g, material="pad", r=0.14)

# ---------------------------------------------------------------- microphone
board("MIC", (-11.5, 4.6), (4.2, 3.4), "pcb_mic")
bpy.ops.mesh.primitive_cylinder_add(radius=0.75, depth=0.5,
                                    location=(-11.5, 5.4, 0.4), vertices=32)
bpy.context.object.data.materials.append(M["spk"])
text("MAX9814", (-11.5, 3.95), size=0.62, material="ink_hi", z=0.24)
text("microphone  ·  INPUT", (-11.5, 1.6), size=0.46, material="dim")

mic = {"VDD": (-13.0, 3.15), "GND": (-11.5, 3.15), "OUT": (-10.0, 3.15)}
for k, v in mic.items():
    pad(v, k, r=0.15)

# ---------------------------------------------------------------- amplifier
board("AMP", (11.0, 4.6), (5.0, 3.4), "pcb_amp")
text("HW-104", (11.0, 5.25), size=0.62, material="ink_hi", z=0.24)
text("PAM8403 amplifier  ·  OUTPUT", (11.0, 1.6), size=0.46, material="dim")

amp = {"L": (8.9, 3.15), "GND": (10.6, 3.15), "VCC": (12.3, 3.15)}
for k, v in amp.items():
    pad(v, {"L": "L in", "GND": "GND", "VCC": "+5V"}[k], r=0.15)
out_p = (12.6, 6.0); out_n = (13.6, 6.0)
pad(out_p, "L+", r=0.15); pad(out_n, "L-", r=0.15)

# ---------------------------------------------------------------- speaker
bpy.ops.mesh.primitive_cylinder_add(radius=2.1, depth=0.9, location=(18.5, 6.2, 0.45), vertices=48)
bpy.context.object.data.materials.append(M["spk"])
bpy.ops.mesh.primitive_cone_add(radius1=1.5, radius2=0.35, depth=0.5,
                                location=(18.5, 6.2, 1.05), vertices=48)
bpy.context.object.data.materials.append(M["cone"])
text("DW-EII  4Ω 3W", (18.5, 3.3), size=0.58, material="ink")
text("speaker", (18.5, 2.35), size=0.46, material="dim")

# ---------------------------------------------------------------- keypad
board("KEYPAD", (0, -9.5), (8.0, 8.0), "pcb_esp", z=0.14)
for r in range(4):
    for c in range(4):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(-2.7 + c*1.8, -6.8 - r*1.8, 0.22))
        k = bpy.context.object; k.scale = (0.72, 0.72, 0.1)
        k.data.materials.append(M["dim"])
text("4x4 KEYPAD", (0, -14.3), size=0.72, material="ink")

# ---------------------------------------------------------------- wiring
# microphone -> ESP32   (three wires, never touches the amplifier)
wire(mic["VDD"], esp["3V3"], "w_pwr", arc=2.6)
wire(mic["GND"], esp["GND1"], "w_gnd", arc=2.2)
wire(mic["OUT"], esp["34"],  "w_mic", arc=1.8)

# ESP32 -> amplifier
wire(esp["25"],   amp["L"],   "w_aud", arc=2.4)
wire(esp["GND2"], amp["GND"], "w_gnd", arc=2.8)
wire(esp["5V"],   amp["VCC"], "w_pwr", arc=3.2)

# amplifier -> speaker (the only two-wire part in the whole build)
wire(out_p, (17.2, 6.6), "w_aud", arc=0.9, thick=0.09)
wire(out_n, (17.2, 5.8), "w_aud", arc=0.6, thick=0.09)

# keypad -> ESP32
for i, g in enumerate(["13", "14", "16", "17", "18", "19", "21", "22"]):
    wire(key_pins[g], (-3.85 + i*1.1, -5.6), "w_key", arc=1.0, thick=0.055)

# ---------------------------------------------------------------- legend
text("RED = power      BLACK = ground      GREEN = mic signal      BLUE = audio out      AMBER = keypad",
     (3.5, -16.8), size=0.62, material="ink")
text("the microphone goes ONLY to the ESP32  —  it never touches the amplifier",
     (3.5, 11.6), size=0.78, material="ink")

# ---------------------------------------------------------------- camera + light
bpy.ops.object.camera_add(location=(3.5, -32.2, 26.8))
cam = bpy.context.object
cam.rotation_euler = (math.radians(48), 0, 0)
cam.data.type = "ORTHO"; cam.data.ortho_scale = 39
scene.camera = cam

bpy.ops.object.light_add(type="AREA", location=(-10, -14, 22))
bpy.context.object.data.energy = 3800; bpy.context.object.data.size = 22
bpy.ops.object.light_add(type="AREA", location=(14, 6, 18))
bpy.context.object.data.energy = 2000; bpy.context.object.data.size = 18
scene.world = bpy.data.worlds.new("w")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.86, 0.87, 0.89, 1)
scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.35

scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 128
scene.cycles.use_denoising = True
scene.render.resolution_x = 1800
scene.render.resolution_y = 1350
scene.render.film_transparent = False
scene.render.filepath = "/private/tmp/claude-501/-Users-harnoor-Developer-ESp32-Vintage-Phone/39870d22-31fa-4491-b773-5325bd1b070e/scratchpad/wiring_3d.png"
bpy.ops.render.render(write_still=True)
print("RENDER OK")
