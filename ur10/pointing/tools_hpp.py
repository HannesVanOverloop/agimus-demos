# BSD 2-Clause License

# Copyright (c) 2021, Florent Lamiraux
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.

# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import rospy
from math import sqrt, pi
import hpp_idl
from pinocchio import XYZQUATToSE3, SE3ToXYZQUAT
from agimus_demos import InStatePlanner
from hpp.corbaserver import wrap_delete
from hpp.corbaserver import loadServerPlugin
from agimus_hpp.plugin import Client as AgimusHppClient
import numpy as np
from scipy.spatial.transform import Rotation as R
import tf2_ros, rospy
from hpp.gepetto import PathPlayer
from std_msgs.msg import Empty as EmptyMsg, Bool, Int32, UInt32, String as StringMsg
import time
import rosnode
from react_inria.srv import reset as ResetSrv

def concatenatePaths(paths):
    if len(paths) == 0: return None
    p = paths[0].asVector()
    for q in paths[1:]:
        assert(p.end() == q.initial())
        p.appendPath(q)
    return p

tool_gripper = 'ur10e/gripper'
rosNodeStarted = False

def initRosNode():
    if not rosNodeStarted:
        rospy.init_node("hpp", disable_signals=True)

def isYes(res):
    YES = ['y', 'yes']
    for y in YES:
        if y in res.lower():
            return True
    return False

class PathGenerator(object):
    def __init__(self, ps, graph, ri=None, v=None, qref=None):
        self.ps = ps
        self.robot = ps.robot
        self.graph = graph
        self.ri = ri
        self.v = v
        if self.v is not None:
            self.pp = PathPlayer(v)
        self.qref = qref # this configuration stores the default pose of the part
        # store corba object corresponding to constraint graph
        self.cgraph = ps.hppcorba.problem.getProblem().getConstraintGraph()
        # create Planner to solve path planning problems on manifolds
        self.inStatePlanner = InStatePlanner(ps, graph)
        self.inStatePlanner.maxIterPathPlanning = 100 #TODO ?
        self.inStatePlanner.timeOutPathPlanning = 15
        self.configs = {}
        self.isClogged = lambda x : False
        self.graphValidation = None
        self.setPointCloud()
        self.setPointCloudDistances()
        self.setPublishersAndSubscribers()
        self.removePointCloud()

    def setPointCloud(self):
        loadServerPlugin('corbaserver', 'agimus-hpp.so')
        cl = AgimusHppClient()
        self.pcl = cl.server.getPointCloud()
        self.pcl.initializeRosNode('agimus_hpp_pcl', False)

    def setPointCloudDistances(self, lower_distance=0.3, upper_distance=1):
        self.pcl.setDistanceBounds(lower_distance,upper_distance)

    def setObjectPlan(self):
        # Get 3 holes on the plaque plan, in the object frame (part/root_joint)
        hole_1 = self.robot.getHandlePositionInJoint('part/handle_40')[1]
        hole_2 = self.robot.getHandlePositionInJoint('part/handle_06')[1]
        hole_3 = self.robot.getHandlePositionInJoint('part/handle_31')[1]
        self.pcl.setObjectPlan(hole_1, hole_2, hole_3)

    def setObjectPlanMargin(self, margin):
        self.pcl.setObjectPlanMargin(margin)

    def buildPointCloud(self, res=0.005, qi=None, margin=0.015, timeout=30,
                  plan=True, new=True):
        qi = self.localizePart()
        self.robot.setCurrentConfig(qi)
        if plan:
            self.setObjectPlan()
            self.setObjectPlanMargin(margin)
        else:
            self.pcl.removeObjectPlan()
        print(f"Getting point cloud ... (timeout is {timeout})")
        result = self.pcl.buildPointCloud('part/base_link', '/camera/depth/color/points',
                        'ur10e/camera_depth_optical_frame', res,
                        qi, timeout, new)
        if result:
            self.graph.initialize()
            self.testGraph()
            print("Success")
            return True
        else:
            print("Failure")
            return False

    def eraseAllPaths(self, excepted=[]):
        for i in range(self.ps.numberPaths()-1,-1,-1):
            if i not in excepted:
                self.ps.erasePath(i)

    def testGraph(self, verbose=False):
        cproblem = self.ps.hppcorba.problem.getProblem()
        cgraph = cproblem.getConstraintGraph()
        self.graphValidation = self.ps.client.manipulation.problem.createGraphValidation()
        self.graphValidation.validate(cgraph)
        if verbose:
            print(self.graphValidation.str())

    # Create the constraint used to generate a new configuration
    # behind the current one in case of failure in delicate positions
    # (useful if the robot fails in a grasp position)
    def createBackwardConstraint(self):
        self.ps.client.basic.problem.resetConstraints()
        self.ps.client.basic.problem.createTransformationR3xSO3Constraint('behind-failure', '', 'ur10e/wrist_3_link',
                                        [0, 0, 0, 0, 0, 0, 1], [0, 0, 0, 0, 0, 0, 1],
                                        [True, True, True, True, True, True,])
        self.ps.setConstantRightHandSide('behind-failure', False)
        self.ps.addNumericalConstraints('config-projector', ['behind-failure'])

    # Generate a configuration with the camera looking
    # horizontally at the hole at a certain distance
    # (used to get a point cloud for the hole)
    def getLookAtHoleConfig(self, hole_id, qinit=None, distance=0.5):
        qinit = self.checkQInit(qinit)
        self.ps.client.basic.problem.resetConstraints()
        t_part = self.robot.hppcorba.robot.getJointPosition('part/root_joint')
        self.ps.client.basic.problem.createTransformationConstraint(
                            'part-fixed',
                            '',
                            'part/base_link',
                            t_part,
                            [True, True, True, True, True, True,])
        self.ps.setConstantRightHandSide('part-fixed', False)
        self.ps.addNumericalConstraints('config-projector', ['part-fixed'])
        self.ps.client.basic.problem.createTransformationR3xSO3Constraint(
                            'look-at-hole-'+str(hole_id),
                            'ur10e/ref_camera_link',
                            'part/hole_' + str(hole_id).zfill(2) + '_link',
                            [distance, 0, 0, 0.5, -0.5, -0.5, 0.5], [0, 0, 0, 0, 0, 0, 1],
                            [True, True, True, True, True, True,])
        self.ps.setConstantRightHandSide('look-at-hole-'+str(hole_id), False)
        self.ps.addNumericalConstraints('config-projector', ['look-at-hole-'+str(hole_id)])
        res, qgoal, err = self.ps.applyConstraints(qinit)
        return res, qgoal, err

    def planLookAtHole(self, hole_id, qinit=None, distance=0.5):
        qinit = self.checkQInit(qinit)
        res, qgoal, _ = self.getLookAtHoleConfig(hole_id, qinit, distance)
        if res:
            return self.planTo(qgoal)
        else:
            return None, qinit

    def wd(self, o):
        return wrap_delete(o, self.ps.client.basic._tools)

    def localizePart(self):
        self.resetVision()
        q1 = self.ri.getCurrentConfig(self.qref)
        ok = False
        try:
            new_q_init = self.ri.getObjectPose(q1, timeout=2)
            ok = True
        except RuntimeError as e:
            self.resetVision()
        if not ok:
            try:
                time.sleep(0.5)
                new_q_init = self.ri.getObjectPose(q1, timeout=2)
                ok = True
            except RuntimeError as e:
                self.resetVisionHard()
        if not ok:
            time.sleep(0.5)
            new_q_init = self.ri.getObjectPose(q1, timeout=2)
        norm = sqrt(sum([e*e for e in new_q_init[-4:]]))
        new_q_init[-4:] = [e/norm for e in new_q_init[-4:]]
        self.qref = new_q_init
        if self.v is not None:
            self.v(new_q_init)
        return new_q_init

    def setTablePose(self, qref):
        q = self.qref[:]
        q[-4:] = qref[-4:]
        return q

    def generateValidConfigForHandle(self, handle, qinit, qguesses = [],
                                     NrandomConfig=50, step=3):
        edge = tool_gripper + " > " + handle
        ok = False
        from itertools import chain
        def project_and_validate(e, qrhs, q):
            res, qres, err = self.graph.generateTargetConfig (e, qrhs, q)
            return res and not self.isClogged(qres) and self.robot.configIsValid(qres), qres
        qpg, qg = None, None
        res = False
        for qrand in chain(qguesses, (self.robot.shootRandomConfig()
                                      for _ in range(NrandomConfig))):
            res1, qpg = project_and_validate (edge+" | f_01", qinit, qrand)
            if not res1: continue
            if step >= 2:
                res2, qg = project_and_validate(edge+" | f_12", qpg, qpg)
                if not res2:
                    continue
            else:
                res2 = True
            res = True; break
        return res, qpg, qg

    def generateRotatingMotionForHandle(self, handle, qinit, loopEdge,
                                        qguesses = [],
                                        NrandomConfig=10):
        p0 = self.generatePathForHandle(handle, qinit,
                                       NrandomConfig=NrandomConfig, step=2)
        if not p0:
            print('failed to generate path to grasp')
            return None
        qg = p0.end()
        q = qg[:]
        q[5] = -pi
        configs = []
        res, q1, err = self.graph.generateTargetConfig(loopEdge, qg, q)
        if not res:
            print('Failed to generate first configuration')
            return None
        configs.append(q1[:])
        q[5] = -pi/2
        res, q1, err = self.graph.generateTargetConfig(loopEdge, qg, q)
        if not res:
            print('Failed to generate first configuration')
            return None
        configs.append(q1[:])
        q[5] = 0
        res, q1, err = self.graph.generateTargetConfig(loopEdge, qg, q)
        if not res:
            print('Failed to generate first configuration')
            return None
        configs.append(q1[:])
        q[5] = pi/2
        res, q1, err = self.graph.generateTargetConfig(loopEdge, qg, q)
        if not res:
            print('Failed to generate first configuration')
            return None
        configs.append(q1[:])
        q[5] = pi
        res, q1, err = self.graph.generateTargetConfig(loopEdge, qg, q)
        if not res:
            print('Failed to generate second configuration')
            return None
        configs.append(q1[:])
        self.inStatePlanner.setEdge(loopEdge)

        paths = []
        qi = qg
        for q1 in configs+[qg]:
            res, p1, msg = self.inStatePlanner.directPath(qi, q1, False)
            if not res:
                print(f'Failed to generate path from {qi} to {q1}')
                return None
            qi = q1[:]
            paths.append(p1)
        # res, p2, msg = self.inStatePlanner.directPath(q1, q2, False)
        # if not res:
        #     print('Failed to generate path from first to second configuration')
        # res, p3, msg = self.inStatePlanner.directPath(q2, qg, False)
        # if not res:
        #     print('Failed to generate path from second configuration to grasp')
        p_end = p0.pathAtRank(1).reverse()
        res = [p0] + paths + [p_end]

        path_ids = []
        for p in res:
            pid = self.addPath(p.asVector())
            path_ids.append(pid)
        return path_ids

    def generateValidConfig(self, constraint, qguesses = [], NrandomConfig=50):
        from itertools import chain
        for qrand in chain(qguesses, (self.robot.shootRandomConfig()
                                      for _ in range(NrandomConfig))):
            res, qres = constraint.apply (qrand)
            if res and self.robot.configIsValid(qres):
                return True, qres
        return False, None

    def checkQInit(self, qinit=None):
        if qinit is None:
            if self.ri is None or self.qref is None:
                raise ValueError('ri and qref must be defined')
            else:
                return self.ri.getCurrentConfig(self.qref)
        return qinit

    def generateLocalizationConfig(self, qinit, maxIter=1000):
        for _ in range(maxIter):
            q = self.robot.shootRandomConfig()
            res, q, e = self.graph.generateTargetConfig('go-look-at-part', qinit, q)
            if not res:
                continue
            res = self.robot.isConfigValid(q)
            if res:
                return q
        raise RuntimeError("Failed to generate target config for localization")

    def setLocalizationConfig(self):
        res = False
        while not res:
            try:
                q_local = self.generateLocalizationConfig(qinit=self.q_ref)
                self.setConfig("localization", q_local)
                pid2, q2 = self.planToConfig("localization", qinit=self.q_ref)
                res = True
            except:
                print("Failed to generate path to localization. Generating new localization configuration")


    # Generate a path from an initial configuration to a grasp
    #
    # param handle: name of the handle,
    # param qinit: initial configuration of the system,
    # param NrandomConfig: number of trials to generate a pre-grasp
    #                      configuration,
    # param step: final state of the motion:
    #              1 -> pregrasp,
    #              2 -> grasps,
    #              3 -> back to pregrap.
    # and going through
    # pregraps, grasp and pregrasp again for a given handle
    def generatePathForHandle(self, handle, qinit=None, NrandomConfig=100,
                              step=3):
        qinit = self.checkQInit(qinit)
        # generate configurations
        edge = tool_gripper + " > " + handle
        ok = False
        for nTrial in range(NrandomConfig):
            res, qpg, qg = self.generateValidConfigForHandle\
               (handle=handle, qinit=qinit, qguesses = [qinit],
                NrandomConfig=NrandomConfig, step=step)
            if not res:
                continue
            # build path
            # from qinit to pregrasp
            self.inStatePlanner.setEdge(edge + " | f_01")
            try:
                p1 = self.inStatePlanner.computePath(qinit, [qpg],
                                                     resetRoadmap = True)
            except hpp_idl.hpp.Error as exc:
                p1 = None
            if not p1: continue
            if step < 2:
                return p1
            # from pregrasp to grasp
            self.inStatePlanner.setEdge(edge + " | f_12")
            res, p2, msg = self.inStatePlanner.directPath(qpg, qg, True)
            if not res: p2 = None
            if not p2: continue
            # Return concatenation
            if step < 3:
                return concatenatePaths([p1, p2])
            # back to pregrasp
            p3 = self.wd(p2.reverse())
            return concatenatePaths([p1, p2, p3])
        raise RuntimeError('failed fo compute a path.')

    def planTo(self, qgoal, qinit=None):
        if isinstance(qgoal, str):
            return self.planToConfig(qgoal)
        qinit = self.checkQInit(qinit)
        self.inStatePlanner.setEdge("Loop | f")
        p1 = self.inStatePlanner.computePath(qinit, [qgoal],
                                             resetRoadmap = True)
        pid = self.addPath(p1)
        q_end = self.ps.configAtParam(pid, self.ps.pathLength(pid))
        print(f"Path to config, ID = {pid}")
        return pid, q_end

    def addPath(self, p, optimizePath=True):
        pid = self.robot.client.basic.problem.addPath(p)
        if optimizePath:
            self.ps.optimizePath(pid)
        return self.ps.numberPaths()-1

    def setConfig(self, name, q):
        self.configs[name] = q

    # param isClogged: function that checks whether the pregrasp and grasps
    # are clogged.
    def setIsClogged(self, isClogged):
        if isClogged is None:
            self.isClogged = lambda x : False
        else:
            self.isClogged = isClogged
        return True

    def planToConfig(self, name, qinit=None):
        if name not in self.configs:
            raise RuntimeError(f"{name} has not been set")
        # p = self.planTo(self.configs[name], qinit)
        # pid = self.addPath(p)
        # q_end = self.ps.configAtParam(pid, self.ps.pathLength(pid))
        return self.planTo(self.configs[name])

    def isHoleDoable(self, hole_id, qinit=None):
        if hole_id in [17,20]:
            return False
        if self.graphValidation is None:
            raise RuntimeError("You should validate the graph first")
        coll = self.graphValidation.getCollisionsForNode("ur10e/gripper grasps part/handle_"+str(hole_id))
        if len(coll) == 0:
            qinit = self.checkQInit(qinit)
            handle = 'part/handle_'+str(hole_id).zfill(2)
            res, qpg, qg = self.generateValidConfigForHandle(handle, qinit=qinit,
                    qguesses = [qinit], NrandomConfig=100)
            if not res or (qg is not None and not self.robot.isConfigValid(qg)[0]):
                print("Cannot generate valid grasp config")
                return False
            return True
        else:
            print("Cannot do hole " + str(hole_id) + " because of the following collisions:")
            for a,b in coll:
                print("Collision between", a, "and", b)
            return False

    def planDeburringPathForHole(self, hole_id, qinit=None, NrandomConfig=50):
        qinit = self.checkQInit(qinit)
        if not self.isHoleDoable(hole_id, qinit):
            print(f"Hole {hole_id} is not doable")
            return None, qinit
        handle = 'part/handle_'+str(hole_id).zfill(2)
        try:
            p = self.generatePathForHandle(handle, qinit, NrandomConfig)
        except:
            print(f"Failed to generate path for hole {hole_id}")
            return None, qinit
        if p:
            pid = self.addPath(p)
        q_end = self.ps.configAtParam(pid, self.ps.pathLength(pid))
        print(f"Path for hole: {hole_id}, ID = {pid}")
        return pid, q_end

    def planDeburringPaths(self, hole_ids, qinit=None):
        qi = self.checkQInit(qinit)
        path_ids = []
        for hole_id in hole_ids:
            pid, qi = self.planDeburringPathForHole(hole_id, qi)
            if pid is not None:
                path_ids.append(pid)
        if len(path_ids) == 0:
            return path_ids, None
        q_end = self.ps.configAtParam(path_ids[-1], self.ps.pathLength(path_ids[-1]))
        return path_ids, q_end

    def planBackwardsAfterFailure(self, qinit=None, distance=0.1):
        qinit = self.checkQInit(qinit)
        # Get current transform of wrist
        t = self.robot.getCurrentTransformation('ur10e/wrist_3_joint')
        trans = [t[i][3] for i in range(3)]
        rot_matrix = [t[i][:3] for i in range(3)]
        quat = list(R.from_matrix(rot_matrix).as_quat())
        # Set back 10cm
        wrist_x = np.matmul(np.array(rot_matrix), np.array([0,0,1]))
        new_trans = list(np.array(trans) - distance * wrist_x)
        # Create and set the right hand side of the y'behind-failure' constraint
        self.createBackwardConstraint()
        self.ps.setRightHandSideByName('behind-failure', new_trans+quat)
        res, qgoal, err = self.ps.applyConstraints(qinit)
        if not res:
            raise RuntimeError("Failed to project configuration")
        self.ps.directPath(qinit, qgoal, False)
        self.ps.optimizePath(self.ps.numberPaths()-1)
        pid = self.ps.numberPaths()-1
        return pid, self.ps.configAtParam(pid, self.ps.pathLength(pid))

    def setPublishersAndSubscribers(self):
        # Topic to publish which path to execute
        PathExecutionTopic = "/agimus/start_path"
        self.path_execution_publisher = rospy.Publisher(
                PathExecutionTopic, UInt32, queue_size=1)

        # Topic publishing the status of the path execution
        StatusRunningTopic = "/agimus/status/running"
        self.path_ready = False
        rospy.Subscriber (StatusRunningTopic,
                    Bool, self.callback_statusRunningTopics)

        # Topic publishing when a path has finished being executed
        PathSuccessTopic = "/agimus/status/path_success"
        self.path_success = False
        rospy.Subscriber (PathSuccessTopic, EmptyMsg, self.callback_pathSuccess)

        # Topic publishing in case of failed execution of the path
        PathFailedTopic = "/agimus/status/path_failed"
        self.path_failed = False
        rospy.Subscriber (PathFailedTopic, StringMsg, self.callback_pathFailed)

        # Topic publishing in case of failed localization of the part
        LocalizationFailedTopic = "/agimus/status/localization_failed"
        self.localization_failed = False
        rospy.Subscriber (LocalizationFailedTopic, StringMsg, self.callback_localizationFailed)

        # Parameter setting the level of steps for the path execution
        self.StepByStepParam = "/agimus/step_by_step"

        # Topic publishing the execution of the next step
        StepTopic = "/agimus/step"
        self.step_publisher = rospy.Publisher(
            StepTopic, EmptyMsg, queue_size=1)

        # Topic publishing the status of the steps execution
        StatusWaitStepByStepTopic = "/agimus/status/is_waiting_for_step_by_step"
        self.step_ready = False

        self.subs_status_stepbystep = rospy.Subscriber (StatusWaitStepByStepTopic,
                    Bool, self.callback_statusStepByStep)

    def callback_pathSuccess(self, msg):
        self.path_success = True

    def callback_pathFailed(self, msg):
        self.path_failed = True

    def callback_localizationFailed(self, msg):
        self.localization_failed = True

    def callback_statusStepByStep(self, msg):
        self.step_ready = msg.data

    def callback_statusRunningTopics(self, msg):
        self.path_running = msg.data

    def removePointCloud(self):
        self.pcl.removeOctree('part/base_link')

    def planToPregrasp(self, handle_id, qinit=None):
        qinit = self.checkQInit(qinit)
        try:
            p = self.generatePathForHandle('part/handle_'+str(handle_id).zfill(2), qinit, step=1)
        except Exception as e:
            print(e)
            return None, qinit
        pid = self.addPath(p)
        q_end = self.ps.configAtParam(pid, self.ps.pathLength(pid))
        return pid, q_end

    def demo_execute(self, pid, max_nb=10, steps=True, visualize=True):
        if self.ps.pathLength(pid) < 0.3:
            return True
        res = self.step(pid, steps, visualize=visualize)
        if not res:
            return False
        print(f"    executing path {pid}")
        # Set step level parameter to zero
        rospy.set_param(self.StepByStepParam, 0)
        self.path_success = False
        self.path_failed = False
        self.localization_failed = False
        # Execute path
        self.path_execution_publisher.publish(pid)
        while not self.path_success:
            # Wait for path to finish
            if self.localization_failed:
                return False
            if self.path_failed:
                if max_nb < 0:
                    print("Failed to play path")
                    return False
                # print("Retrying...")
                time.sleep(0.2)
                return self.demo_execute(pid, max_nb=max_nb-1, steps=False)
            time.sleep(0.1)
        return True

    def demo_planAndExecute(self, goal, qinit=None):
        qinit = self.checkQInit(qinit)
        pid, _ = self.planTo(goal, qinit=qinit)
        self.demo_execute(pid)

    def demo_pointcloud(self, configs=[], steps=False):
        for qpc in configs:
            print("Going to PC acquisition config...")
            try:
                pid, _ = self.planTo(qpc)
            except Exception as e:
                print(e)
                return False
            try:
                res = self.demo_execute(pid, steps=steps)
                if not res:
                    raise RuntimeError("Failed to demo_execute")
            except Exception as e:
                print(e)
                self.resetVision()
                res = self.demo_execute(pid, steps=steps)
            if not res:
                return False
            self.buildPointCloud(new=False)
        return True

    def checkNeedVisionReset(self, config):
        res, msg = self.robot.isConfigValid(config)
        if not res:
            if 'Joint part/root_joint' in msg and 'value out of range' in msg:
                return True
            if 'Collision between object ur10e/support_link' in msg and 'part/' in msg:
                return True
        return False

    def resetVision(self):
        print("Resetting localizer")
        rospy.wait_for_service('/reset_from_robot_position')
        self.reset_vision_client = rospy.ServiceProxy('/reset_from_robot_position', ResetSrv)
        res = self.reset_vision_client()
        if not res:
            raise RuntimeError("Could not reset vision from robot position")
        return True

    def resetVisionHard(self):
        rosnode.kill_nodes(['/react_camera_localizer'])
        rospy.wait_for_service('/reset_from_robot_position')
        return True

    def demo_handle_pointcloud(self, handles=[], qinit=None, steps=False):
        qinit = self.checkQInit(qinit)
        qi = qinit[:]
        ok = False
        for handle in handles:
            self.resetVision()
            print(f"\nPlanning point cloud acquisition in front of handle {handle}...")
            res, qpg, _ = self.generateValidConfigForHandle(
                'part/handle_'+str(handle).zfill(2),
                qinit = qi,
                qguesses = [qi],
                step = 1,
                NrandomConfig=50)
            if res:
                self.demo_pointcloud([qpg], steps=steps)
                qi = qpg[:]
                ok = True
            else:
                print("Failed.")
                print(res)
                print(qpg)
        return ok

    def demo_calib(self, steps=False):
        print("Going to calibration config 0...")
        try:
            pid, _ = self.planTo("calib")
        except Exception as e:
            print(e)
            return False
        res = self.demo_execute(pid, steps=steps)
        if not res:
            return False
        self.localizePart()
        print("Object localized")
        if steps:
            res = input("Place or move obstacle NOW")
        self.buildPointCloud(new=True)
        return True

    def step(self, pid, steps=True, visualize=True):
        if steps:
            if visualize:
                self.pp(pid)
            res = input("Execute ? (Y/n/reshow)")
            if 'r' in res.lower():
                return self.step(pid, steps=steps, visualize=True)
            if 'n' in res.lower():
                return False
            else:
                return True
        return True

    def demo_goToHolePointCloud(self, hole_id, steps=False, pointcloud=True):
        qinit = self.checkQInit()
        if not self.isHoleDoable(hole_id, qinit):
            print(f"Hole {hole_id} is not doable")
            return None, qinit
        print(f"\nGoing to hole {hole_id}")
        for i in range(3):
            try:
                print("Planning pregrasp")
                pid, _ = self.planToPregrasp(hole_id)
                if pid is not None:
                    break
            except:
                return False
        if pid is None:
            print(f"Failed to do hole {hole_id}")
            return True
        res = self.demo_execute(pid, steps=steps)
        if pointcloud:
            self.buildPointCloud(new=False)
        print("Executing pointing task...")
        try:
            pid, _ = self.planDeburringPathForHole(hole_id)
            if pid is None:
                return False
        except:
            return False
        return self.demo_execute(pid, steps=steps)

    def demo_goToHole(self, hole_id, steps=False, NbTries=3):
        i = 0
        while True:
            print(f"\nGoing to hole {hole_id}, try {i}")
            try:
                pid, _ = self.planDeburringPathForHole(hole_id)
                if pid is not None:
                    try:
                        res = self.demo_execute(pid, steps=steps)
                        if not res:
                            raise RuntimeError("Failed to demo_execute")
                    except Exception as e:
                        print(e)
                        self.resetVision()
                        res = self.demo_execute(pid, steps=steps)
                    return res
                else:
                    i += 1
                    if i > NbTries:
                        return None
            except:
                i += 1
                if i > NbTries:
                    return None

    def demo(self, hole_list=[7,8,9,42,43,13], pointcloud_handles=[1,5,19,15],
                steps=False, getPCatPreGrasp=False, execute=True):
        self.removePointCloud()
        res = self.demo_calib(steps=steps)
        if not res:
            print("Could not do calib.")
            return False

        res = self.demo_pointcloud(configs=["pointcloud", "pointcloud2"], steps=steps)
        # if not getPCatPreGrasp:
        #     res = self.demo_handle_pointcloud(handles=pointcloud_handles, steps=steps)
        if not res:
            print("Could not do point cloud acquisition.")
            return False

        for hole_id in hole_list:
            self.resetVision()
            if getPCatPreGrasp:
                res = self.demo_goToHolePointCloud(hole_id, steps=steps)
            else:
                res = self.demo_goToHolePointCloud(hole_id, steps=steps, pointcloud=False)
            time.sleep(0.2)

        print("\nGoing to calibration config...")
        for i in range(3):
            try:
                pid, _ = self.planTo("calib")
            except Exception as e :
                print(e)
                continue
            if pid is not None:
                break
        if pid is None:
            print("Failed to go back to calib")
            return False
        res = self.demo_execute(pid, steps=steps)
        print("Demo done.")
        return True

class RosInterface(object):
    nodeId = 0
    def __init__(self, robot):
        self.robot = robot
        self.robotPrefix = robot.robotNames[0] + "/"
        initRosNode()
        self.tfBuffer = tf2_ros.Buffer()
        self.listener = tf2_ros.TransformListener(self.tfBuffer)

    def getCurrentConfig(self, q0, timeout=5.):
        from sensor_msgs.msg import JointState
        q = q0[:]
        # Acquire robot state
        msg = rospy.wait_for_message("/joint_states", JointState)
        for ni, qi in zip(msg.name, msg.position):
            jni = self.robotPrefix + ni
            if self.robot.getJointConfigSize(jni) != 1:
                continue
            try:
                rk = self.robot.rankInConfiguration[jni]
            except KeyError:
                continue
            assert self.robot.getJointConfigSize(jni) == 1
            q[rk] = qi

        return q

    def getObjectPose(self, q0, timeout=5.):
        # the object should be placed wrt to the robot, as this is what the
        # sensor tells us.
        # Get pose of object wrt to the camera using TF
        cameraFrame = self.robot.opticalFrame
        qres = q0[:]

        for obj in self.robot.robotNames[1:]:
            objectFrame = obj + '/base_link_measured'
            wMc = XYZQUATToSE3(self.robot.hppcorba.robot.getJointsPosition\
                               (q0, [self.robotPrefix + cameraFrame])[0])
            try:
                _cMo = self.tfBuffer.lookup_transform(cameraFrame, objectFrame,
                        rospy.Time(), rospy.Duration(timeout))
                _cMo = _cMo.transform
                # renormalize quaternion
                x = _cMo.rotation.x
                y = _cMo.rotation.y
                z = _cMo.rotation.z
                w = _cMo.rotation.w
                n = sqrt(x*x+y*y+z*z+w*w)
            except (tf2_ros.LookupException, tf2_ros.ConnectivityException,
                    tf2_ros.ExtrapolationException) as e:
                print('could not get TF transform : ', e)
                raise RuntimeError(str(e))
            cMo = XYZQUATToSE3([_cMo.translation.x, _cMo.translation.y,
                                _cMo.translation.z, _cMo.rotation.x/n,
                                _cMo.rotation.y/n, _cMo.rotation.z/n,
                                _cMo.rotation.w/n])
            rk = self.robot.rankInConfiguration[obj + '/root_joint']
            assert self.robot.getJointConfigSize(obj + '/root_joint') == 7
            qres[rk:rk+7] = SE3ToXYZQUAT (wMc * cMo)

        return qres
