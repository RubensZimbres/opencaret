import time

import rclpy
from controls.PI import PI
from opencaret_msgs.msg import LongitudinalPlan
from rclpy.node import Node
from std_msgs.msg import Float32, Bool
import math
from util import util
import numpy as np

PLAN_LOOKAHEAD_INDEX = 3
TIME_STEP = 0.2
MAX_THROTTLE = 0.4
MAX_BRAKE = 0.5
MAX_PLANNER_DELAY = 1.0  # after 1.0s of no plan, consider the planner dead.
THROTTLE_FILTER = 0.9
BRAKE_FILTER = 0.9

class CONTROL_MODE:
    ACCELERATE = 1,
    BRAKE = 2


class LongitudinalController(Node):
    kP = 0.1
    kI = 0.01
    kF = 0.13
    DEADBAND_ACCEL = 0.05
    DEADBAND_BRAKE = -0.05

    def __init__(self):
        super().__init__('lateral_controler')
        self.ego_accel = 0
        self.ego_velocity = 0
        self.brake_output = 0.0
        self.throttle_output = 0.0
        self.target_throttle = 0.0
        self.target_brake = 0.0

        self.mode = CONTROL_MODE.BRAKE
        self.pi = PI(self.kP, self.kI, self.kF, -MAX_BRAKE, MAX_THROTTLE)
        self.create_subscription(LongitudinalPlan, 'longitudinal_plan', self.on_plan)
        self.create_subscription(Float32, "debug_target_speed", self.on_debug_target_speed)
        self.plan_deviation_pub = self.create_publisher(Float32, '/plan_deviation')
        self.target_speed_pub = self.create_publisher(Float32, 'pid_target_speed')
        self.target_acc = self.create_publisher(Float32, 'pid_target_accel')
        self.p_pub = self.create_publisher(Float32, 'pid_p')
        self.ff_pub = self.create_publisher(Float32, 'pid_ff')
        self.i_pub = self.create_publisher(Float32, 'pid_i')
        self.create_subscription(Float32, "wheel_speed", self.on_speed)
        self.throttle_pub = self.create_publisher(Float32, '/throttle_command')
        self.brake_pub = self.create_publisher(Float32, '/brake_command')
        self.last_plan_time = None
        self.controls_enabled = False
        self.plan = None
        self.acceleration_plan = None
        self.velocity_plan = None
        self.controls_enabled_sub = self.create_subscription(Bool, 'controls_enable', self.on_controls_enable)

        self.pid_timer = self.create_timer(1.0 / 50.0, self.pid_spin)

    def set_target_throttle(self, target, force=False):
        self.target_throttle = target
        if force:
            self.throttle_output = target

    def set_target_brake(self, target, force=False):
        self.target_brake = target
        if force:
            self.brake_output = target

    def on_controls_enable(self, msg):
        self.controls_enabled = msg.data

    def on_speed(self, msg):
        self.ego_velocity = msg.data

    def on_plan(self, msg):
        self.plan = msg
        self.velocity_plan = np.array(self.plan.velocity).astype(np.float32)[PLAN_LOOKAHEAD_INDEX:]
        self.acceleration_plan = np.array(self.plan.accel).astype(np.float32)[PLAN_LOOKAHEAD_INDEX:]
        self.last_plan_time = time.time()

    def on_debug_target_speed(self, msg):
        self.set_target_speed(msg.data)

    def set_target_speed(self, target_vel):
        target_vel = max(0, target_vel)
        self.target_speed_pub.publish(Float32(data=target_vel))

    def on_imu(self, msg):
        self.ego_accel = msg.linear_acceleration.x

    def find_current_position_in_plan(self):
        dt = time.time() - self.last_plan_time
        closest_plan_index = min(len(self.velocity_plan) - 1, math.floor(dt / TIME_STEP))
        time_since_closest_plan_index = dt - closest_plan_index * TIME_STEP
        current_plan_deviation = self.velocity_plan[closest_plan_index].item() - self.ego_velocity
        return closest_plan_index,  time_since_closest_plan_index, current_plan_deviation

    def is_plan_stale(self):
        dt = time.time() - self.last_plan_time
        return dt > MAX_PLANNER_DELAY

    def pid_spin(self):
        if not self.controls_enabled:
            return

        deviation = 0.0
        if self.plan:
            closest_plan_index, time_since_closest_plan_index, deviation = self.find_current_position_in_plan()
            acceleration = self.acceleration_plan[closest_plan_index].item()
            if not self.is_plan_stale():
                velocity = self.velocity_plan[closest_plan_index] + acceleration * time_since_closest_plan_index
            else:
                velocity = self.velocity_plan[-1].item()  # Try just going at the last velocity and hope for the best!
        else:
            acceleration = 0.0
            velocity = 0.0

        self.plan_deviation_pub.publish(Float32(data=deviation))
        output = self.pi.update(velocity, self.ego_velocity, acceleration)

        #  PI debug
        self.target_speed_pub.publish(Float32(data=velocity))
        self.target_acc.publish(Float32(data=acceleration))
        self.p_pub.publish(Float32(data=self.pi.P))
        self.ff_pub.publish(Float32(data=self.pi.FF))
        self.i_pub.publish(Float32(data=self.pi.I))

        if self.mode == CONTROL_MODE.BRAKE and self.ego_velocity <= 0.5 and velocity <= 0.5 and acceleration <= 0.1:
            self.get_logger().warn("Brake clamping for stop")
            output = -0.25

        if output > self.DEADBAND_ACCEL:
            # print("Accelerating: {}".format(self.pid.output))
            if self.mode == CONTROL_MODE.BRAKE:
                self.pi.clear()
                self.mode = CONTROL_MODE.ACCELERATE
            self.set_target_brake(0.0)
            self.set_target_throttle(output)

        elif output < self.DEADBAND_BRAKE:
            if self.mode == CONTROL_MODE.ACCELERATE:
                self.pi.clear()
                self.mode = CONTROL_MODE.BRAKE
            # print("Braking: {}".format(self.pid.output))
            self.set_target_brake(-output + 0.05)
            self.set_target_throttle(0.0)
        else:
            if self.mode == CONTROL_MODE.BRAKE:
                self.set_target_brake(-min(0.0, output - 0.05))
            else:
                self.set_target_throttle(max(0.0, output))

        self.throttle_output = THROTTLE_FILTER * self.throttle_output + (1.0 - THROTTLE_FILTER) * self.target_throttle
        self.brake_output = BRAKE_FILTER * self.brake_output + (1.0 - BRAKE_FILTER) * self.target_brake
        self.throttle_pub.publish(Float32(data=self.throttle_output))
        self.brake_pub.publish(Float32(data=self.brake_output))


def main():
    rclpy.init()
    controls = LongitudinalController()
    rclpy.spin(controls)
    controls.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
