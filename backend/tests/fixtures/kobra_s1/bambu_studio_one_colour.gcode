; HEADER_BLOCK_START
; BambuStudio 02.08.02.61
; estimated printing time (normal mode) = 43m 41s
; total layer number: 240
; total filament length [mm] : 3851.81
; total filament volume [cm^3] : 9264.69
; total filament weight [g] : 11.49
; filament_density: 1.24
; filament_diameter: 1.75
; max_z_height: 48.00
; filament: 1
; support_material_on_wipe_tower: 0
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; accel_to_decel_enable = 1
; accel_to_decel_factor = 50%
; activate_air_filtration = 1
; additional_cooling_fan_speed = 60
; additional_fan_full_speed_layer = 0
; alternate_extra_wall = 0
; ams_filament_load_time_ams = 0
; ams_filament_load_time_ams_lite = 0
; ams_filament_load_time_n3f_s = 0
; ams_filament_unload_time_ams = 0
; ams_filament_unload_time_ams_lite = 0
; ams_filament_unload_time_n3f_s = 0
; apply_scarf_seam_on_circles = 1
; auxiliary_fan = 1
; avoid_crossing_wall_includes_support = 0
; bed_custom_model = 
; bed_custom_texture = 
; bed_exclude_area = 
; bed_heat_soak_area = 
; bed_temperature_formula = by_first_filament
; before_layer_change_gcode = ; BEFORE_LAYER_CHANGE [layer_num] @ [layer_z]mm
; best_object_pos = 0.5,0.5
; bottom_color_penetration_layers = 3
; bottom_shell_layers = 3
; bottom_shell_thickness = 0
; bottom_surface_density = 100%
; bottom_surface_pattern = monotonic
; bridge_angle = 0
; bridge_flow = 0.9
; bridge_no_support = 0
; bridge_speed = 30
; brim_object_gap = 0.1
; brim_type = auto_brim
; brim_width = 5
; chamber_temperatures = 0
; change_filament_gcode = ; FLUSH_START\n;;; M400 P0\nT[next_extruder] ; change extruder\n; 1\n;;; G90\n;;; G1 Z{toolchange_z+2} F480\n;;; G1 X261 Y25 F12000\n;;; G1 Y1 F600\n;;; M400 P2730\n;;; G1 Y25 F3000\n;;; M400 P76250\n;;; M400 P35780\n; 2.1\n;;; G90\n;;; G1 Z{toolchange_z+2} F480\n;;; G1 X47 Y230 F12000\n;;; M400 P0\n; 2.2\n;;; G1 X47 Y276 F600\n;;; G1 X47 Y230 F12000\n;;; G1 X47 Y276 F600\n;;; G1 X47 Y230 F12000\n; 3.1\n;;; G1 F36000\n;;; G1 Y250\n;;; G1 F8000\n;;; G1 X81\n;;; G1 Y273\n; 3.2\n;;; G1 F8000\n;;; G1 X96\n;;; G1 X81\n;;; G1 F8000\n;;; G1 X96\n;;; G1 X81\n;;; G1 F8000\n;;; G1 X96\n;;; G1 X81\n;;; G1 X96\n;;; G1 X81\n;;; G1 X96\n;;; G1 X81\n;;; G1 X96\n;;; G1 X81\n; 3.3\n;;; G1 X72\n;;; G1 X77\n;;; G1 Z{toolchange_z}\n;;; M400 P0\n; FLUSH_END
; circle_compensation_manual_offset = 0
; circle_compensation_speed = 200
; close_additional_fan_first_x_layers = 1
; close_fan_the_first_x_layers = 1
; compatible_machine_expression_group = ;;
; compatible_printers_condition = 
; complete_print_exhaust_fan_speed = 80
; cool_plate_temp = 35
; cool_plate_temp_initial_layer = 35
; cooling_filter_enabled = 0
; cooling_perimeter_transition_distance = 10
; cooling_slowdown_logic = uniform_cooling
; counter_coef_1 = 0
; counter_coef_2 = 0.025
; counter_coef_3 = -0.11
; counter_limit_max = 0.05
; counter_limit_min = -0.04
; counterbore_hole_bridging = none
; curr_bed_type = Textured PEI Plate
; default_acceleration = 10000
; default_ams_type = -1
; default_filament_colour = ""
; default_filament_profile = "Anycubic PLA @Kobra S1 (ODIN)"
; default_jerk = 9
; default_print_profile = 0.20mm Standard @Kobra S1 (ODIN)
; deretraction_speed = 0
; detect_floating_vertical_shell = 1
; detect_narrow_internal_solid_infill = 1
; detect_overhang_wall = 1
; detect_thin_wall = 1
; diameter_limit = 50
; different_settings_to_system = ;;
; draft_shield = disabled
; during_print_exhaust_fan_speed = 60
; elefant_foot_compensation = 0.075
; embedding_wall_into_infill = 0
; enable_arc_fitting = 0
; enable_circle_compensation = 0
; enable_filament_dynamic_map = 0
; enable_height_slowdown = 0
; enable_long_retraction_when_cut = 0
; enable_mixed_color_sublayer = 0
; enable_order_independent_overlap_carving = 0
; enable_overhang_bridge_fan = 1
; enable_overhang_speed = 1
; enable_pre_heating = 0
; enable_pressure_advance = 1
; enable_prime_tower = 1
; enable_support = 0
; enable_support_ironing = 0
; enable_tower_interface_features = 0
; enable_wrapping_detection = 0
; enforce_support_layers = 0
; eng_plate_temp = 0
; eng_plate_temp_initial_layer = 0
; ensure_vertical_shell_thickness = enabled
; exclude_object = 1
; extruder_ams_count = 
; extruder_clearance_dist_to_rod = 33
; extruder_clearance_height_to_lid = 240
; extruder_clearance_height_to_rod = 48
; extruder_clearance_max_radius = 65
; extruder_colour = #FF4D4F
; extruder_max_nozzle_count = 1
; extruder_nozzle_stats = 
; extruder_offset = 0x0
; extruder_printable_area = 
; extruder_printable_height = 0
; extruder_type = Direct Drive
; fan_cooling_layer_time = 100
; fan_direction = undefine
; fan_max_speed = 100
; fan_min_speed = 100
; farthest_point_timelapse = 0
; filament_adaptive_volumetric_speed = 0
; filament_adhesiveness_category = 0
; filament_bridge_speed = 25
; filament_change_length = 10
; filament_change_length_nc = 10
; filament_colour = #00AE42
; filament_cooling_before_tower = 10
; filament_cost = 20
; filament_density = 1.24
; filament_dev_ams_drying_ams_limitations = ""
; filament_dev_ams_drying_heat_distortion_temperature = 0
; filament_dev_ams_drying_temperature = 0
; filament_dev_ams_drying_time = 0
; filament_dev_chamber_drying_bed_temperature = 0
; filament_dev_chamber_drying_time = 0
; filament_dev_drying_cooling_temperature = 0
; filament_dev_drying_softening_temperature = 0
; filament_diameter = 1.75
; filament_enable_overhang_speed = 1
; filament_end_gcode = "; filament end gcode\n"
; filament_extruder_compatibility = 0
; filament_extruder_variant = "Direct Drive Standard"
; filament_flow_ratio = 0.98
; filament_flush_temp = 0
; filament_flush_temp_fast = 0
; filament_flush_volumetric_speed = 0
; filament_ids = OFmjN2bc
; filament_is_mixed = 0
; filament_is_support = 0
; filament_map = 1
; filament_map_2 = 0
; filament_map_mode = Auto For Flush
; filament_max_volumetric_speed = 12
; filament_metal_stickiness = None
; filament_minimal_purge_on_wipe_tower = 15
; filament_mixed_components = ""
; filament_mixed_gradient = 0
; filament_mixed_gradient_curve = ""
; filament_mixed_gradient_per_part = 0
; filament_mixed_gradient_range = ""
; filament_mixed_sublayer_ratios = ""
; filament_notes = 
; filament_nozzle_map = 0
; filament_overhang_1_4_speed = 0
; filament_overhang_2_4_speed = 0
; filament_overhang_3_4_speed = 0
; filament_overhang_4_4_speed = 0
; filament_overhang_totally_speed = 10
; filament_pre_cooling_temperature = 0
; filament_pre_cooling_temperature_nc = 0
; filament_preheat_temperature_delta = 0
; filament_prime_volume = 45
; filament_prime_volume_nc = 60
; filament_printable = 3
; filament_ramming_travel_time = 0
; filament_ramming_travel_time_nc = 0
; filament_ramming_volumetric_speed = -1
; filament_ramming_volumetric_speed_nc = -1
; filament_retract_length_nc = 10
; filament_scarf_gap = 0
; filament_scarf_height = 10%
; filament_scarf_length = 10
; filament_scarf_seam_type = none
; filament_self_index = 1
; filament_settings_id = "Anycubic PLA @Kobra S1 (ODIN)"
; filament_shrink = 100%
; filament_soluble = 0
; filament_start_gcode = "; filament start gcode"
; filament_tower_interface_pre_extrusion_dist = 10
; filament_tower_interface_pre_extrusion_length = 0
; filament_tower_interface_print_temp = -1
; filament_tower_interface_purge_volume = 20
; filament_tower_ironing_area = 4
; filament_type = PLA
; filament_velocity_adaptation_factor = 1
; filament_vendor = Anycubic
; filament_volume_map = 0
; filename_format = {input_filename_base}_{filament_type[0]}_{layer_height}_{print_time}.gcode
; fill_multiline = 1
; filter_out_gap_fill = 0
; first_layer_print_sequence = 0
; first_x_layer_fan_speed = 0
; first_x_layer_part_fan_speed = 0
; flush_into_infill = 0
; flush_into_objects = 0
; flush_into_support = 1
; flush_multiplier = 1
; flush_multiplier_fast = 1.2
; flush_volumes_matrix = 0
; flush_volumes_vector = 140,140
; full_fan_speed_layer = 0
; fuzzy_skin = none
; fuzzy_skin_first_layer = 0
; fuzzy_skin_mode = displacement
; fuzzy_skin_noise_type = classic
; fuzzy_skin_octaves = 4
; fuzzy_skin_persistence = 0.5
; fuzzy_skin_point_distance = 0.8
; fuzzy_skin_scale = 1
; fuzzy_skin_thickness = 0.3
; gap_infill_speed = 250
; gcode_add_line_number = 0
; gcode_flavor = klipper
; grab_length = 0
; group_algo_with_time = 0
; has_filament_switcher = 0
; has_scarf_joint_seam = 0
; head_wrap_detect_zone = 
; hole_coef_1 = 0
; hole_coef_2 = -0.025
; hole_coef_3 = 0.28
; hole_limit_max = 0.25
; hole_limit_min = 0.08
; hot_plate_temp = 55
; hot_plate_temp_initial_layer = 55
; hotend_cooling_rate = 2
; hotend_heating_rate = 2
; impact_strength_z = 0
; independent_support_layer_height = 1
; infill_combination = 0
; infill_direction = 45
; infill_instead_top_bottom_surfaces = 0
; infill_jerk = 9
; infill_lock_depth = 1
; infill_rotate_step = 0
; infill_shift_step = 0.4
; infill_wall_overlap = 15%
; inherits_group = ;;fdm_machine_common
; initial_layer_acceleration = 500
; initial_layer_flow_ratio = 1
; initial_layer_infill_speed = 80
; initial_layer_jerk = 9
; initial_layer_line_width = 0.5
; initial_layer_print_height = 0.2
; initial_layer_speed = 50
; initial_layer_travel_acceleration = 500
; inner_wall_acceleration = 5000
; inner_wall_jerk = 9
; inner_wall_line_width = 0.45
; inner_wall_speed = 300
; interface_shells = 0
; interlocking_beam = 0
; interlocking_beam_layer_count = 2
; interlocking_beam_width = 0.8
; interlocking_boundary_avoidance = 2
; interlocking_depth = 2
; interlocking_orientation = 22.5
; internal_bridge_support_thickness = 0
; internal_solid_infill_line_width = 0.42
; internal_solid_infill_pattern = monotonic
; internal_solid_infill_speed = 250
; ironing_direction = 45
; ironing_fan_speed = -1
; ironing_flow = 10%
; ironing_inset = 0
; ironing_pattern = zig-zag
; ironing_spacing = 0.15
; ironing_speed = 30
; ironing_type = no ironing
; is_infill_first = 0
; layer_change_gcode = ; AFTER_LAYER_CHANGE [layer_num] @ [layer_z]mm
; layer_height = 0.2
; line_width = 0.42
; locked_skeleton_infill_pattern = zigzag
; locked_skin_infill_pattern = crosszag
; long_retractions_when_cut = 0
; long_retractions_when_ec = 0
; machine_bed_mass_Y = 0
; machine_end_gcode = ; ODIN_END_GCODE\nG92 E0\nG1 E-2 F3000\n{if max_layer_z < max_print_height-1}G1 Z{z_offset+min(max_layer_z+2, max_print_height)} F900 ; Move print head further up{endif} \nG1 F12000; present print\nG1 X44;  throw_position_x\nG1 Y270; throw_position_y\nM140 S0 ; turn off heatbed\nM104 S0 ; turn off temperature\nM106 P1 S0 ; turn off fan\nM106 P2 S0\nM106 P3 S0\nM84; disable motors                  \n; disable stepper motors
; machine_hotend_change_time = 0
; machine_load_filament_time = 126.423
; machine_max_acceleration_e = 5000,5000
; machine_max_acceleration_extruding = 20000,20000
; machine_max_acceleration_retracting = 5000,5000
; machine_max_acceleration_travel = 20000,20000
; machine_max_acceleration_x = 10000,10000
; machine_max_acceleration_y = 10000,10000
; machine_max_acceleration_z = 500,500
; machine_max_force_Y = 0
; machine_max_jerk_e = 2.5,2.5
; machine_max_jerk_x = 9,9
; machine_max_jerk_y = 9,9
; machine_max_jerk_z = 3,3
; machine_max_printed_mass = 0
; machine_max_speed_e = 80,80
; machine_max_speed_x = 600,600
; machine_max_speed_y = 600,600
; machine_max_speed_z = 15,15
; machine_min_extruding_rate = 0,0
; machine_min_travel_rate = 0,0
; machine_pause_gcode = M601
; machine_prepare_compensation_time = 260
; machine_start_gcode = M140 S[bed_temperature_initial_layer_single]\nM104 S140\nG9111 bedTemp=[bed_temperature_initial_layer_single] extruderTemp=[nozzle_temperature_initial_layer[initial_tool]]\nT[initial_tool]\nM117\nG90\nM83
; machine_switch_extruder_time = 5
; machine_unload_filament_time = 0
; master_extruder_id = 1
; max_bridge_length = 10
; max_layer_height = 0.28
; max_travel_detour_distance = 0
; min_bead_width = 85%
; min_feature_size = 25%
; min_layer_height = 0.08
; minimum_sparse_infill_area = 15
; mmu_segmented_region_interlocking_depth = 0
; mmu_segmented_region_max_width = 0
; monotonic_travel_into_wall = 0%
; no_slow_down_for_cooling_on_outwalls = 0
; nozzle_diameter = 0.4
; nozzle_flush_dataset = 0
; nozzle_height = 4
; nozzle_temperature = 205
; nozzle_temperature_initial_layer = 215
; nozzle_temperature_range_high = 240
; nozzle_temperature_range_low = 190
; nozzle_type = brass
; nozzle_volume = 71.6
; nozzle_volume_type = Standard
; only_one_wall_first_layer = 0
; ooze_prevention = 0
; other_layers_print_sequence = 0
; other_layers_print_sequence_nums = 0
; outer_wall_acceleration = 5000
; outer_wall_jerk = 9
; outer_wall_line_width = 0.42
; outer_wall_speed = 200
; overhang_1_4_speed = 0
; overhang_2_4_speed = 50
; overhang_3_4_speed = 30
; overhang_4_4_speed = 10
; overhang_fan_speed = 100
; overhang_fan_threshold = 50%
; overhang_threshold_participating_cooling = 95%
; overhang_totally_speed = 10
; override_filament_scarf_seam_setting = 0
; override_process_overhang_speed = 0
; physical_extruder_map = 0
; post_process = 
; pre_start_fan_time = 0
; precise_outer_wall = 1
; precise_z_height = 0
; pressure_advance = 0.035
; prime_tower_brim_width = 5
; prime_tower_enable_framework = 0
; prime_tower_extra_rib_length = 0
; prime_tower_fillet_wall = 1
; prime_tower_flat_ironing = 0
; prime_tower_infill_gap = 150%
; prime_tower_lift_height = -1
; prime_tower_lift_speed = 90
; prime_tower_max_speed = 90
; prime_tower_rib_wall = 1
; prime_tower_rib_width = 8
; prime_tower_skip_points = 1
; prime_tower_width = 35
; prime_volume_mode = Default
; print_compatible_printers = fdm_machine_common
; print_extruder_id = 1
; print_extruder_variant = "Direct Drive Standard"
; print_flow_ratio = 1
; print_in_clockwise = 0
; print_sequence = by layer
; print_settings_id = 0.20mm Standard @Kobra S1 (ODIN)
; printable_area = 0x0,250x0,250x250,0x250
; printable_height = 250
; printer_extruder_id = 1
; printer_extruder_variant = "Direct Drive Standard"
; printer_model = Anycubic Kobra 2
; printer_notes = 
; printer_settings_id = Kobra S1 0.4 (ODIN)
; printer_structure = corexy
; printer_technology = FFF
; printer_variant = 0.4
; printing_by_object_gcode = 
; process_notes = 
; raft_contact_distance = 0.1
; raft_expansion = 1.5
; raft_first_layer_density = 90%
; raft_first_layer_expansion = 5
; raft_layers = 0
; reduce_crossing_wall = 0
; reduce_fan_stop_start_freq = 1
; reduce_infill_retraction_mode = Auto
; required_nozzle_HRC = 3
; resolution = 0.012
; retract_before_wipe = 0%
; retract_length_toolchange = 0
; retract_lift_above = 0.3
; retract_lift_below = 249
; retract_restart_extra = 0
; retract_restart_extra_toolchange = 0
; retract_when_changing_layer = 1
; retraction_distances_when_cut = 18
; retraction_distances_when_ec = 10
; retraction_length = 0.8
; retraction_minimum_travel = 1
; retraction_speed = 40
; role_base_wipe_speed = 1
; scan_first_layer = 0
; scarf_angle_threshold = 155
; seam_gap = 10%
; seam_placement_away_from_overhangs = 0
; seam_position = aligned
; seam_slope_conditional = 1
; seam_slope_entire_loop = 0
; seam_slope_gap = 0
; seam_slope_inner_walls = 0
; seam_slope_min_length = 10
; seam_slope_start_height = 0
; seam_slope_steps = 10
; seam_slope_type = none
; silent_mode = 0
; single_extruder_multi_material = 1
; skeleton_infill_density = 15%
; skeleton_infill_line_width = 0.4
; skin_infill_density = 15%
; skin_infill_depth = 2
; skin_infill_line_width = 0.4
; skirt_distance = 2
; skirt_height = 1
; skirt_loops = 0
; skirt_per_object = 1
; slice_closing_radius = 0.049
; slicing_mode = regular
; slow_down_for_layer_cooling = 1
; slow_down_layer_time = 8
; slow_down_min_speed = 20
; slowdown_end_acc = 100000
; slowdown_end_height = 400
; slowdown_end_speed = 1000
; slowdown_start_acc = 100000
; slowdown_start_height = 0
; slowdown_start_speed = 1000
; small_perimeter_speed = 50%
; small_perimeter_threshold = 0
; smooth_coefficient = 80
; smooth_speed_discontinuity_area = 1
; solid_infill_filament = 0
; sparse_infill_acceleration = 100%
; sparse_infill_anchor = 400%
; sparse_infill_anchor_max = 20
; sparse_infill_density = 15%
; sparse_infill_filament = 0
; sparse_infill_lattice_angle_1 = -45
; sparse_infill_lattice_angle_2 = 45
; sparse_infill_line_width = 0.45
; sparse_infill_pattern = crosshatch
; sparse_infill_speed = 270
; spiral_mode = 0
; spiral_mode_max_xy_smoothing = 200%
; spiral_mode_smooth = 0
; standby_temperature_delta = -5
; start_end_points = 30x-3,54x245
; supertack_plate_temp = 35
; supertack_plate_temp_initial_layer = 35
; support_air_filtration = 1
; support_angle = 0
; support_base_pattern = default
; support_base_pattern_spacing = 2.5
; support_bottom_interface_spacing = 0.5
; support_bottom_z_distance = 0.2
; support_chamber_temp_control = 0
; support_cooling_filter = 0
; support_critical_regions_only = 0
; support_expansion = 0
; support_fast_purge_mode = 0
; support_filament = 0
; support_interface_bottom_layers = 2
; support_interface_filament = 0
; support_interface_loop_pattern = 0
; support_interface_not_for_body = 1
; support_interface_pattern = auto
; support_interface_spacing = 0.5
; support_interface_speed = 80
; support_interface_top_layers = 2
; support_ironing_direction = 0
; support_ironing_flow = 10%
; support_ironing_inset = 0
; support_ironing_pattern = zig-zag
; support_ironing_spacing = 0.1
; support_ironing_speed = 20
; support_line_width = 0.42
; support_object_first_layer_gap = 0.2
; support_object_skip_flush = 0
; support_object_xy_distance = 0.35
; support_on_build_plate_only = 1
; support_remove_small_overhang = 1
; support_speed = 150
; support_style = default
; support_threshold_angle = 30
; support_top_z_distance = 0.18
; support_type = tree(auto)
; symmetric_infill_y_axis = 0
; temperature_vitrification = 55
; template_custom_gcode = 
; textured_plate_temp = 55
; textured_plate_temp_initial_layer = 55
; thick_bridges = 0
; thumbnail_size = 230x110
; time_lapse_gcode = 
; timelapse_type = 0
; top_area_threshold = 200%
; top_color_penetration_layers = 4
; top_one_wall_type = all top
; top_shell_layers = 5
; top_shell_thickness = 1
; top_solid_infill_flow_ratio = 0.97
; top_surface_acceleration = 2000
; top_surface_density = 100%
; top_surface_jerk = 9
; top_surface_line_width = 0.42
; top_surface_pattern = monotonicline
; top_surface_speed = 200
; top_z_overrides_xy_distance = 0
; travel_acceleration = 10000
; travel_jerk = 9
; travel_short_distance_acceleration = 250
; travel_speed = 300
; travel_speed_z = 0
; tree_support_branch_angle = 45
; tree_support_branch_diameter = 2
; tree_support_branch_diameter_angle = 5
; tree_support_branch_distance = 5
; tree_support_wall_count = 0
; use_firmware_retraction = 0
; use_relative_e_distances = 1
; vertical_shell_speed = 80%
; volumetric_speed_coefficients = ""
; wall_distribution_count = 1
; wall_filament = 0
; wall_generator = classic
; wall_loops = 2
; wall_sequence = outer wall/inner wall
; wall_transition_angle = 10
; wall_transition_filter_deviation = 25%
; wall_transition_length = 100%
; wipe = 1
; wipe_distance = 1
; wipe_speed = 80%
; wipe_tower_no_sparse_layers = 0
; wipe_tower_rotation_angle = 0
; wipe_tower_x = 165
; wipe_tower_y = 230
; wrapping_detection_gcode = 
; wrapping_detection_layers = 20
; wrapping_exclude_area = 
; xy_contour_compensation = 0
; xy_hole_compensation = 0
; z_direction_outwall_speed_continuous = 0
; z_hop = 0.4
; z_hop_types = Normal Lift
; CONFIG_BLOCK_END

; THUMBNAIL_BLOCK_START
; thumbnail begin 230x110 3480
; iVBORw0KGgoAAAANSUhEUgAAAOYAAABuCAYAAAAziW8OAAAJ+UlEQVR4Ae2d63dU1RmH92Ri6wdbW4
; uri7uFEJIQyI1ACBcDqyCWsoqt7bICSlFQQCpaoEi5DBejaGsago2AmCjNh67+EfnTtvs3++xz9hxm
; MklEcK15nrX2ymUmyacn77vf9937GAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
; AAAAAAAAAAAAAAAAAAAAAAAAAA8Aj49aKS2besZNqeHjIA8APgpeUl8/VWm649S0oGAB4j76+ZrpAy
; rN+56AkAjxilrdWEjJciKQA8IvKpK3ICPGRWu2LN3qUl85slvnATVmuVIs4e976vttSXcTL39SmX7g
; LALPmtE23CiTbTurvZmr+vnTbv1dhLzrQmtmSinls3TcUWYDZIOMlT6rHmYpdb3dZcdmu4z5rPNljz
; 743WfL7Jmntb7JylDEJqXe215uaA/3wfqS1AbZY9NZRGxZMd1hxpfXBd652fkOVomUh5cKU1b6yy5k
; R7JupO1/MEgCpoDykptWc81GLNW6v9ejv5eP07SKmlKCsJL7kI/P4aaz7s81/r+zddJCatBajC7sV+
; f/kfl6oechHteFuy2n1q+12k1FJKHOQMS/8Ezq3zKbLeoz0uAETsSsTUXlIRU+ms1l/detetG+ttcW
; po/mLqdzjRi+e7fdT8wAl5wKW1r7dkYjKIAJDjWNt0hZinO1V99eusW++028KFbts8sW1+Yn7U5yOw
; 0uI3W72QElPC0usEqMGJdi/miBPzLy6VHXVR7EvXGvkyaZEonXVyuh6kbR4btOabbfMTU0Wk/Su8lL
; XERE6AhLdWZ2IedVHtzubK/uWHvV4syanoqeLNfMWU+PsTMWvtX2+5dgpAw7N7id9jKkqqanovJ6Yq
; pyfakuXlVIpb+GLw4YupCrBSXYCGZ/vCdOqn6dYmXzGtKWYi5zEfQQufbrDNU9vry5kXUz3NuPCjv3
; O+ywu77pmSAWh4tv6yVAhyqGVSbRwvL2Z5eTmbr/TZ4mSdfefYgC8qScyT7b6FEl5T0UmiS8qdi5il
; BSiz/KkhM75pDmI+KKdS4OKtORaG1M/U6F8oBmntWoyYACn/7M/S1mpinlv7oJyhrXIiKgwNr7eF+8
; /Xl1Jzt9rPHkjS2rAQEyCjcLJjOpXwq1mKqSUxVbX9YG0qpyZ6mu7MMOx+xRV9DrZkUVJ7z1RMUlmA
; DLfPLKehoTqbF1PyxUJKJg0LHF7lJ3kkZipve3kf2Tw6WDkxdC8Zw8tHyXh1UfgByBh4tmQ+7a99Fj
; Mv5hvJBM+7HbagveLZREx9lLRJ9Gy+3m8LGlTQ8TGJHKTUcIGkDtNAf16BmADVKHwyBzGPtvrxPQmn
; n5OkElLV1uNt2b5T0VOV2FdX+OXkK5xye8tTHT56ap+qiPt2G2ICVEVD6+OD1cXM9zKDmBJR8klMfZ
; Sc+X3oay1p77Kgv6HjZBL6TKffb3603n2+FjEBqtK/oFQc3TQ3MRX5JGW89HpyoiRNeyXdO37vWZbx
; hpPxbKctaPxPPc5jScQc2cjNBgAVdLtopTRzfBa9TEU9iSn5YikVQbXPDD3OWMzQWpGI/9pgn/hmu3
; 1iaoctuM9D8af51mbf39RRNABIOLraFkcGMhljSfMpqsR8bWUmpaQOe02Jm4q5younQo8rMBWnttum
; yW22+L8d9kf/32mbTnVmVdmP12eHqQEgoW9BqZyGfrLetzf+0TWzmIdaHkxl8/tMiRmi5u1B3ydVa2
; bqeVvQXUJxu0SFoNEB3145t5aeJkCKItvfOv25TFVjPx+oXpmdi5j6vsTTtI+Ol90Z9Glvvo95octf
; /DW6kbOZABX0/qJULtQoel3t8ZJITB3zygs3GzEPr6qUT+LXGjBQKqvznpLzJnICVKKmv/aMElNLUe
; 5aT1bAqSdmvMfMi1lrqZWiO2cl57CXs+nuFuQESOlxUVMtjHPrMjnDyrdMYil1+FkRr1rxp9ZSShvf
; oDcWyamD0+EW931c1gVgyhM7OrwsGUuRmCFqHsmJqfdIrLmIGUs5NpB9rtMnklO3H7hKbuF+cpyMNg
; o0PLpJQPvBC13Vo2Y8ZKDbCIJUsxFTaetI8jOXe/x0kCL0+XWZoBo+uOHlLHy2MTtOxgXR0PDsXjyd
; iqnUMo6aYcggRMrZihlHySBlEPNkMh0UR0/dcCA5JX84iI2c0NAseHKoXLzRXk/DBprSkZgqBAUx42
; hZT8wgZSxkNTG1hvuy36m7iJTWjkb3BCEnNDTdrhCkMbvwmAPt/9Q6iaNaPTGVuobXlcLmpawnpoTW
; P4AgJ5ETwPg52tOdtjgePYPk9ubZi6lCUYVodSJm/r069ykZNRUkOdW+CXLuWFgyAA2L5NRwuvZ8Em
; Zi6+zFVDqsoYW4uKMIqPG/WMz49ZHk9YPJ81Tc91QAKowNJAWhDbbw9Tb/3r08lAgamY6flcwR/2i+
; 4n+H5iamKrzH26pHRK04NQ4RNdwNdCkrGJXl1LnRRM70HwTHxaChaXu6VJbG7TuL4zXS2au91cXUSi
; 6LroiO8UqljK61HKksMJXlVDvl4z4/cH93M3ICmFYn5x+f8/f1XOutvI09Hz0lpsb7gphh5aOnCjxx
; lDxQW8xUTt0ndCOZElJBSq+d4VQKNDItPy2ZPUu9OC56Nn+xpbqcoZ2iOVhdIRLLqegpQd8MV1jmpD
; zZUVXKdN3f5i8D0724w0nFdpLxPWh0nvnxkNm92Jo/POfPXF7psU3xhc/VZNLJkVjOI8nteftzUfJS
; d20h4+UKQOUikNo44XsHVxI1AUz/gmnz4mL//MvTndkgQEgv8yuOnkeS6yv3z0PKVM6kSqy/p54rl3
; oBJCh6vrDImpeW+X2lBuD1SL/4wue8UJJTVd4gZj0hJV/5loON/sG6FUWjZI+KlABVUPR8wUXPV1aU
; L+UqX7Y1U2qr6Kl95sU6UoYHHr3yK/ferqxNoo/62Ved2J0/LxkAqIGiZxD0cKttvtTn94AzCVpvhe
; F5ia7BevUyVfU9tMpLObSQfSXArFj5k5LZpfR2eVocSg8+T9ZIb2utM522SZdD64Iw9UAvJ4/wC2kw
; AMyBOHrqMQnvrcmeJj2XiHknGSLQ/jJcHq0JI/VTn31yyADAPJCgip77lpWjZ/Fib2WLo96aiAo8qv
; z+ye01X0ZKgIeDomec3io1rdVWmYxSXlV5y1FStyes9lJKdgB4SMTprVJRpabX+7LTIiFCxm0QpcGK
; knq/hukB4HsiFIdeXOILOG7/WX5+iYbSbw9mBR6N7rkIWbjgvu5fUDIA8D0TR0+lt+Ekyuu5KEnqCv
; AYCNFTgurj3qVexpdJXQEeP6E4pPT29y6CSlgA+AGglJW0FQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
; AAAAAAAAAAAAAAAAGpRvAX9fXLEYb+SjAAAAAElFTkSuQmCC
; thumbnail end
; THUMBNAIL_BLOCK_END

; EXECUTABLE_BLOCK_START
M73 P0 R1
G90
M83
G28
M73 P100 R0
; EXECUTABLE_BLOCK_END
