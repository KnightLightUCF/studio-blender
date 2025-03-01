from math import ceil, floor

import bpy

from bpy.props import FloatProperty, IntProperty
from bpy.types import Context

from sbstudio.math.nearest_neighbors import find_nearest_neighbors
from sbstudio.plugin.api import get_api
from sbstudio.plugin.constants import Collections
from sbstudio.plugin.model.formation import create_formation
from sbstudio.plugin.model.safety_check import get_proximity_warning_threshold
from sbstudio.plugin.utils.evaluator import create_position_evaluator

from .base import StoryboardOperator

__all__ = ("LandOperator",)


class LandOperator(StoryboardOperator):
    """Blender operator that adds a landing transition to the show, starting at
    the current frame.
    """

    bl_idname = "skybrush.land"
    bl_label = "Land Drones"
    bl_description = "Add a landing maneuver to all the drones"
    bl_options = {"REGISTER", "UNDO"}

    only_with_valid_storyboard = True

    start_frame = IntProperty(
        name="at frame", description="Start frame of the landing maneuver"
    )

    velocity = FloatProperty(
        name="with velocity",
        description="Average vertical velocity during the landing maneuver",
        default=1,
        min=0.1,
        soft_min=0.1,
        soft_max=10,
        unit="VELOCITY",
    )

    altitude = FloatProperty(
        name="to altitude",
        description="Altitude to land to",
        default=0,
        soft_min=-50,
        soft_max=50,
        unit="LENGTH",
    )

    spindown_time = FloatProperty(
        name="Motor spindown delay (sec)",
        description=(
            "Time it takes for the motors to spin down after a successful landing"
        ),
        default=5,
        min=0,
        soft_min=0,
        soft_max=10,
        unit="TIME",
    )

    @classmethod
    def poll(cls, context):
        if not super().poll(context):
            return False

        drones = Collections.find_drones(create=False)
        return drones is not None and len(drones.objects) > 0

    def invoke(self, context, event):
        storyboard = context.scene.skybrush.storyboard
        self.start_frame = max(context.scene.frame_current, storyboard.frame_end)
        return context.window_manager.invoke_props_dialog(self)

    def execute_on_storyboard(self, storyboard, entries, context):
        return {"FINISHED"} if self._run(storyboard, context=context) else {"CANCELLED"}

    def _run(self, storyboard, *, context) -> bool:
        bpy.ops.skybrush.prepare()

        if not self._validate_start_frame(context):
            return False

        drones = Collections.find_drones().objects
        if not drones:
            return False

        # Get the current positions of the drones to land
        with create_position_evaluator() as get_positions_of:
            source = get_positions_of(drones, frame=self.start_frame)

        # Construct the target formation as well
        target = [(x, y, self.altitude) for x, y, _ in source]

        # Calculate the Z distance to travel for each drone
        diffs = [s[2] - t[2] for s, t in zip(source, target)]
        if min(diffs) < 0:
            dist = abs(min(diffs))
            self.report(
                {"ERROR"},
                f"At least one drone would have to land upwards by {dist}m",
            )
            return False

        # Ask the API to figure out the start times and durations for each drone
        fps = context.scene.render.fps
        min_distance = get_proximity_warning_threshold(context)
        _, _, dist = find_nearest_neighbors(target)
        if dist < min_distance:
            delays, durations = get_api().plan_landing(
                source,
                min_distance=min_distance,
                velocity=self.velocity,
                target_altitude=self.altitude,
                spindown_time=self.spindown_time,
            )
        else:
            # We can save an API call here
            delays = [0] * len(source)
            durations = [diff / self.velocity for diff in diffs]

        delays = [int(ceil(delay * fps)) for delay in delays]
        durations = [int(floor(duration * fps)) for duration in durations]
        max_duration = max(
            delay + duration for delay, duration in zip(delays, durations)
        )
        post_delays = [
            max_duration - delay - duration
            for delay, duration in zip(delays, durations)
        ]

        # Extend the duration of the last formation to the frame where we want
        # to start the landing
        if len(storyboard.entries) > 0:
            last_entry = storyboard.entries[-1]
            last_entry.extend_until(self.start_frame)

        # Calculate when the landing should end
        end_of_landing = self.start_frame + max_duration

        # Add a new storyboard entry with the given formation
        entry = storyboard.add_new_entry(
            formation=create_formation("Landing", target),
            frame_start=end_of_landing,
            duration=0,
            select=True,
            context=context,
        )
        assert entry is not None
        entry.transition_type = "MANUAL"

        # Set up the custom departure delays for the drones
        if max(delays) > 0 or max(post_delays) > 0:
            entry.schedule_overrides_enabled = True
            for index, (delay, post_delay) in enumerate(zip(delays, post_delays)):
                if delay > 0 or post_delay > 0:
                    override = entry.add_new_schedule_override()
                    override.index = index
                    override.pre_delay = delay
                    override.post_delay = post_delay

        # Recalculate the transition leading to the target formation
        bpy.ops.skybrush.recalculate_transitions(scope="TO_SELECTED")
        return True

    def _validate_start_frame(self, context: Context) -> bool:
        """Returns whether the takeoff time chosen by the user is valid."""
        storyboard = context.scene.skybrush.storyboard
        if storyboard.last_entry is not None:
            last_frame = context.scene.skybrush.storyboard.frame_end
        else:
            last_frame = None

        # TODO(ntamas): what if the last entry in the storyboard _is_ the
        # landing, what shall we do then? Probably we should ignore it and look
        # at the penultimate entry.

        if last_frame is not None and self.start_frame < last_frame:
            self.report(
                {"ERROR"},
                (
                    f"Landing maneuver must not start before the last entry of "
                    f"the storyboard (frame {last_frame})"
                ),
            )
            return False

        return True
