#!/usr/bin/env
# An action server to execute a task plan.

from __future__ import print_function, division

import pickle

import rospy
import actionlib

from sound_interface import SoundClient
from task_executor.abstract_step import AbstractStep
from task_executor.actions import get_default_actions
from task_executor.tasks import Task, TaskContext

from actionlib_msgs.msg import GoalID, GoalStatus
from task_executor.msg import ExecuteAction
from assistance_msgs.msg import (RequestAssistanceAction, RequestAssistanceGoal,
                                 RequestAssistanceResult)
from std_srvs.srv import Trigger, TriggerResponse


# The actual action server to execute the tasks

class TaskServer(object):
    """
    Given the task to perform, this server yields control to sub clients. When
    the clients are done, it moves on to the next task. If the task fails, the
    server sends context to an arbitrator. The arbitrator yields control to a
    recovery interface, which then returns a signal back up the stack to this
    server with hints on how execution should proceed.
    """

    ASSISTANCE_ARBITRATOR_ACTION_SERVER = "arbitrator"

    def __init__(self, actions=None, connect_arbitrator=True):
        # Instantiate the action clients
        if actions is None:
            self.actions = get_default_actions()
        else:
            self.actions = actions

        # Provide a service to reload, and then reload
        self._reload_service = rospy.Service('~reload', Trigger, self.reload)
        self.reload(None)

        # Connect to the arbitrator only if needed
        if connect_arbitrator:
            # Instantiate a connection to the arbitration server
            self._arbitration_client = actionlib.SimpleActionClient(
                TaskServer.ASSISTANCE_ARBITRATOR_ACTION_SERVER,
                RequestAssistanceAction
            )
        else:
            self._arbitration_client = None

        # Instantiate the action server
        self._server = actionlib.SimpleActionServer(
            rospy.get_name(),
            ExecuteAction,
            self.execute,
            auto_start=False
        )

    def start(self):
        if self._arbitration_client is not None:
            rospy.loginfo("Connecting to assistance arbitrator...")
            self._arbitration_client.wait_for_server()
            rospy.loginfo("...assistance arbitrator connected")

        self._server.start()
        rospy.loginfo("Executor node ready...")
        self.actions.beep(beep=SoundClient.BEEP_PROUD)

    def reload(self, req):
        # Get the task configs
        tasks_config = self._validate_tasks(rospy.get_param('~tasks'))
        self.tasks = { key: Task() for key, _ in tasks_config.iteritems() }

        # Instantiate the registry of actions
        self.actions.init()

        # Instantiate the registry of tasks
        for key, task in self.tasks.iteritems():
            task.init(
                name=key,
                tasks=self.tasks,
                actions=self.actions,
                **tasks_config[key]
            )

        return TriggerResponse(success=True)

    def execute(self, goal):
        """
        Execute the given task name. Has a spec of ExecuteGoal.
        """
        result = self._server.get_default_result()
        if goal.name not in self.tasks:
            rospy.logerr("Task {}: UNRECOGNIZED.".format(goal.name))
            self._server.set_aborted(result)
            return

        # Prepare the task. Main tasks cannot take parameters or return values
        task = self.tasks[goal.name]
        task.set_running()
        variables = {}
        execution_context = TaskContext()
        request_assistance = True

        # Execute the task unless we have been told not to seek assistance or
        # unless the underlying task has been preempted.
        while not task.is_succeeded() and request_assistance:
            # The task execution portion of the while loop
            try:
                rospy.loginfo("Task {}: EXECUTING.".format(task.name))
                request_assistance = False  # We don't want to request
                                            # assistance until there's an error
                for variables in task.run(execution_context):
                    # First check to see if we've been preempted. If we have, then
                    # set the preempt flag and wait for the task to return
                    if self._server.is_preempt_requested() or not self._server.is_active():
                        task.stop()
                        continue

                    # Check to see if something has stopped the task. If so, then
                    # exit out of this for loop
                    if task.is_preempted() or task.is_aborted():
                        break

                # If the task has been preempted, then stop executing it
                if task.is_preempted():
                    rospy.logwarn("Task {}: PREEMPTED. Context Keys: {}".format(task.name, variables.keys()))
                    self._server.set_preempted(result)
                    return

                # If the task has failed, request assistance and resume based
                if task.is_aborted():
                    rospy.logerr("Task {}: FAIL. Context Keys: {}".format(task.name, variables.keys()))
                    request_assistance = True

            except Exception as e:
                # There was some unexpected error in the underlying code.
                # Capture it and send it to the recovery mechanism.
                rospy.logerr("Exception in task execution: {}".format(e))
                variables = {
                    'task': task.name,
                    'step_idx': task.step_idx,
                    'exception': e
                }
                request_assistance = True

            # The value of request assistance depends on the arbitration client
            if request_assistance and self._arbitration_client is None:
                request_assistance = False

            # The request assistance portion of the while loop
            if request_assistance:
                # Create the assistance goal
                assistance_goal = RequestAssistanceGoal(stamp=rospy.Time.now())
                executor = task.get_executor()
                assistance_goal.component = executor.name
                assistance_goal.component_status = executor.status
                assistance_goal.priority = RequestAssistanceGoal.PRIORITY_NORMAL
                assistance_goal.context = pickle.dumps(variables)

                # Send the goal and wait. Preempt if a preempt request has also
                # appeared
                self._arbitration_client.send_goal(assistance_goal)
                while not self._arbitration_client.wait_for_result(rospy.Duration(0.5)):
                    if self._server.is_preempt_requested():
                        self._arbitration_client.cancel_goal()

                # Get the result from the arbitration server and proceed
                # accordingly
                assist_status = self._arbitration_client.get_state()
                assist_result = self._arbitration_client.get_result()

                if assist_status == GoalStatus.PREEMPTED:
                    rospy.logwarn("Assistance request PREEMPTED. Exiting.")
                    self._server.set_preempted(result)
                    return
                elif assist_status != GoalStatus.SUCCEEDED:  # Most likely ABORTED
                    rospy.logerr("Assistance request ABORTED. Exiting.")
                    self._server.set_aborted(result)
                    return
                else:  # GoalStatus.SUCCEEDED
                    rospy.loginfo("Assistance request COMPLETED. Resume Hint: {}"
                                  .format(assist_result.resume_hint))
                    if assist_result.resume_hint == RequestAssistanceResult.RESUME_NEXT:
                        # Set the execution context to the next step
                        execution_context = TaskContext(start_idx=task.step_idx + 1, restart_child=True)
                    elif assist_result.resume_hint == RequestAssistanceResult.RESUME_PREVIOUS:
                        # Set the execution context to the previous step
                        execution_context = TaskContext(start_idx=task.step_idx - 1, restart_child=True)
                    elif assist_result.resume_hint == RequestAssistanceResult.RESUME_RETRY:
                        # Reset the execution context
                        execution_context = TaskContext(restart_child=True)
                    elif assist_result.resume_hint == RequestAssistanceResult.RESUME_CONTINUE:
                        # Set execution context to current step
                        execution_context = TaskContext(start_idx=task.step_idx, restart_child=False)
                    else:  # RequestAssistanceResult.RESUME_NONE
                        request_assistance = False
                        result_context = (
                            pickle.loads(assist_result.context)
                            if assist_result.context != ''
                            else {}
                        )
                        variables = task.set_aborted(**result_context)

            # End while

        # Check to see if the task aborted
        if task.is_aborted():
            rospy.logerr("Task {}: FAIL. Context Keys: {}".format(task.name, variables.keys()))
            self._server.set_aborted(result)
            return

        # Otherwise, signal complete
        result.success = True
        rospy.loginfo("Task {}: SUCCESS.".format(task.name))
        self._server.set_succeeded(result)

    def stop(self):
        # Cancel all current goals
        cancel_pub = rospy.Publisher(rospy.get_name() + '/cancel', GoalID)
        cancel_pub.publish(GoalID(stamp=rospy.Time.now()))

        # Wait a bit
        rospy.sleep(0.5)

    def _validate_tasks(self, tasks):
        # We don't need to validate yet. But perhaps soon
        return tasks
