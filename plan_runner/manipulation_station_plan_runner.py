import numpy as np
import sys

from pydrake.all import (
    BasicVector,
    LeafSystem,
    PortDataType,
)
from pydrake.trajectories import (
    PiecewisePolynomial
)
from pydrake.math import RollPitchYaw

from plan_runner.robot_plans import *
from plan_runner.open_left_door_plans import *


class ManipStationPlanRunner(LeafSystem):
    """
    The plan runner is constructed with a list of Plans (kuka_plans) and a list of gripper
    setpoints (gripper_setpoint_list).
    In its constructor, it adds two additional plans to kuka_plans for safety reasons:
    - The first plan holds the robot's current position for 3 seconds.
    - The second plan moves the robot from its current position, to the position at the beginning of the first
        plan in the Plans list.

    Plans in the modified kuka_plans are then activated in sequence.
    Each plan is active for (plan.duration * 1.1) seconds.

    By default, the plan runner sends position and torque commands to iiwa at 200Hz,
    and gripper setpoint commands to the schunk WSG50 at a lower rate.
    At every update event, the commands are generated by evaluating the currently active plan.
    """
    def __init__(self, station, kuka_plans, gripper_setpoint_list,
                 control_period=0.005, print_period=0.5):
        LeafSystem.__init__(self)
        self.set_name("Manipulation Plan Runner")
        # Append plan_list with a plan that moves the robot from its current position to
        # plan_list[0].traj.value(0)
        kuka_plans.insert(0, JointSpacePlan())
        gripper_setpoint_list.insert(0, 0.055)
        self.move_to_home_duration_sec = 1.0  # 6.0
        kuka_plans[0].duration = self.move_to_home_duration_sec

        # Add a five-second zero order hold to hold the current position of the robot
        kuka_plans.insert(0, JointSpacePlan())
        gripper_setpoint_list.insert(0, 0.055)
        self.zero_order_hold_duration_sec = 1.0  # 3.0
        kuka_plans[0].duration = self.zero_order_hold_duration_sec

        assert len(kuka_plans) == len(gripper_setpoint_list)
        self.gripper_setpoint_list = gripper_setpoint_list
        self.kuka_plans_list = kuka_plans

        # calculate starting time for all plans
        self.num_plans = len(kuka_plans)
        self.t_plan = np.zeros(self.num_plans + 1)
        self.t_plan[1] = self.zero_order_hold_duration_sec
        self.t_plan[2] = self.move_to_home_duration_sec + self.t_plan[1]
        for i in range(2, self.num_plans):
            self.t_plan[i + 1] = \
                self.t_plan[i] + kuka_plans[i].get_duration() * 1.1
        print "Plan starting time\n", self.t_plan

        # Stuff for iiwa control
        self.nu = 7
        self.print_period = print_period
        self.last_print_time = -print_period
        self.control_period = control_period

        # create a multibodyplant containing the robot only, which is used for
        # jacobian calculations.
        self.plant_iiwa = station.get_controller_plant()
        self.tree_iiwa = self.plant_iiwa.tree()
        self.context_iiwa = self.plant_iiwa.CreateDefaultContext()
        self.l7_frame = self.plant_iiwa.GetFrameByName('iiwa_link_7')

        # Declare iiwa_position/torque_command publishing rate
        self._DeclarePeriodicPublish(control_period)

        # iiwa position input port
        self.iiwa_position_input_port = \
            self._DeclareInputPort(
                "iiwa_position", PortDataType.kVectorValued, 7)

        # iiwa velocity input port
        self.iiwa_velocity_input_port = \
            self._DeclareInputPort(
                "iiwa_velocity", PortDataType.kVectorValued, 7)

        # position and torque command output port
        # first 7 elements are position commands.
        # last 7 elements are torque commands.
        self.iiwa_position_command_output_port = \
            self._DeclareVectorOutputPort("iiwa_position_and_torque_command",
                BasicVector(self.nu*2), self.CalcIiwaCommand)

        # gripper control
        self._DeclareDiscreteState(1)
        self._DeclarePeriodicDiscreteUpdate(period_sec=0.1)
        self.hand_setpoint_output_port = \
            self._DeclareVectorOutputPort(
                "gripper_setpoint", BasicVector(1), self.CalcHandSetpointOutput)
        self.gripper_force_limit_output_port = \
            self._DeclareVectorOutputPort(
                "force_limit", BasicVector(1), self.CalcForceLimitOutput)


    def CalcIiwaCommand(self, context, y_data):
        t= context.get_time()
        self.GetCurrentPlan(context)
        q_iiwa = self.EvalVectorInput(
            context, self.iiwa_position_input_port.get_index()).get_value()

        t_plan = t - self.current_plan.start_time
        new_position_command = np.zeros(7)
        new_position_command[:] = q_iiwa
        new_torque_command = np.zeros(7)

        if self.current_plan.type == PlanTypes["JointSpacePlan"]:
            new_position_command[:] = self.current_plan.traj.value(t_plan).flatten()

        elif self.current_plan.type == PlanTypes["JointSpacePlanRelative"] or \
            self.current_plan.type == PlanTypes["JointSpacePlanGoToTarget"]:
            if self.current_plan.traj is None:
                self.current_plan.UpdateTrajectory(q_start=q_iiwa)
            new_position_command[:] = self.current_plan.traj.value(t_plan).flatten()

        elif self.current_plan.type == PlanTypes["IiwaTaskSpacePlan"]:
            if self.current_plan.xyz_offset is None:
                # update self.context_iiwa
                x_iiwa_mutable = \
                    self.tree_iiwa.GetMutablePositionsAndVelocities(self.context_iiwa)
                x_iiwa_mutable[:7] = q_iiwa

                # Pose of frame L7 in world frame
                X_WL7 = self.tree_iiwa.CalcRelativeTransform(
                    self.context_iiwa, frame_A=self.plant_iiwa.world_frame(),
                    frame_B=self.l7_frame)

                # Position of Q in world frame
                p_L7Q = X_L7E.multiply(p_EQ)
                p_WQ = X_WL7.multiply(p_L7Q)

                self.current_plan.UpdateXyzOffset(p_WQ)

            new_position_command[:] = self.current_plan.CalcPositionCommand(
                t_plan, q_iiwa, self.control_period)

        elif self.current_plan.type == PlanTypes["OpenLeftDoorImpedancePlan"] or \
                self.current_plan.type == PlanTypes["OpenLeftDoorPositionPlan"]:
            # update self.context_iiwa
            x_iiwa_mutable = \
                self.tree_iiwa.GetMutablePositionsAndVelocities(self.context_iiwa)
            x_iiwa_mutable[:7] = q_iiwa

            Jv_WL7q, p_HrQ, R_L7L7r, R_WL7 = self.current_plan.CalcKinematics(
                l7_frame=self.l7_frame,
                world_frame=self.plant_iiwa.world_frame(),
                tree_iiwa=self.tree_iiwa, context_iiwa=self.context_iiwa,
                t_plan=t_plan)

            # compute commands
            if self.current_plan.type == PlanTypes["OpenLeftDoorPositionPlan"]:
                new_position_command[:] = self.current_plan.CalcPositionCommand(
                    t_plan, q_iiwa, Jv_WL7q, p_HrQ, R_L7L7r, R_WL7, self.control_period)
                new_torque_command[:] = self.current_plan.CalcTorqueCommand()
            elif self.current_plan.type == PlanTypes["OpenLeftDoorImpedancePlan"]:
                new_position_command[:] = self.current_plan.CalcPositionCommand(t_plan, q_iiwa)
                new_torque_command[:] = self.current_plan.CalcTorqueCommand(
                    t_plan, Jv_WL7q, p_HrQ, R_L7L7r, R_WL7)

        y = y_data.get_mutable_value()
        y[:self.nu] = new_position_command[:]
        y[self.nu:] = new_torque_command[:]

        # print current simulation time
        if (self.print_period and
                t - self.last_print_time >= self.print_period):
            print "t: ", context.get_time()
            self.last_print_time = context.get_time()


    def GetCurrentPlan(self, context):
        t = context.get_time()
        if self.kuka_plans_list[0].traj is None:
            q_current = self.EvalVectorInput(
                context, self.iiwa_position_input_port.get_index()).get_value()
            q0 = self.kuka_plans_list[2].traj.value(0).flatten()

            # zero order hold
            q_knots_kuka = np.zeros((2,7))
            q_knots_kuka[0] = q_current
            qtraj = PiecewisePolynomial.ZeroOrderHold(
                [0, self.zero_order_hold_duration_sec], q_knots_kuka.T)
            self.kuka_plans_list[0] = JointSpacePlan(qtraj)

            # move to the starting position of trajectory plans
            t_knots = np.array(
                [0., self.move_to_home_duration_sec / 2, self.move_to_home_duration_sec])
            q_knots_kuka = np.zeros((3, 7))
            q_knots_kuka[0] = q_current
            q_knots_kuka[2] = q0
            q_knots_kuka[1] = (q_knots_kuka[0] + q_knots_kuka[2]) / 2
            qtraj = PiecewisePolynomial.Cubic(
                t_knots, q_knots_kuka.T, np.zeros(7), np.zeros(7))

            self.kuka_plans_list[1] = JointSpacePlan(qtraj)

            # set current plan
            self.current_plan_idx = 0
            self.current_plan = self.kuka_plans_list[0]
            self.current_plan.set_start_time(0.)

        new_plan_idx = 0
        for i in range(self.num_plans):
            if t >= self.t_plan[i] and t < self.t_plan[i + 1]:
                new_plan_idx = i
                break
        if self.current_plan_idx < new_plan_idx:
            self.current_plan_idx = new_plan_idx
            self.current_plan = self.kuka_plans_list[new_plan_idx]
            self.current_plan.set_start_time(t)


    def _DoCalcDiscreteVariableUpdates(self, context, events, discrete_state):
        # Call base method to ensure we do not get recursion.
        LeafSystem._DoCalcDiscreteVariableUpdates(self, context, events, discrete_state)

        new_state = discrete_state.get_mutable_vector().get_mutable_value()
        # Close gripper after plan has been executed
        new_state[:] = self.gripper_setpoint_list[self.current_plan_idx]

    def CalcHandSetpointOutput(self, context, y_data):
        state = context.get_discrete_state_vector().get_value()
        y = y_data.get_mutable_value()
        # Get the ith finger control output
        y[:] = state[0]

    def CalcForceLimitOutput(self, context, output):
        output.SetAtIndex(0, 15.0)

