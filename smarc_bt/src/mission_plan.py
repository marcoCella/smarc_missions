#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
# Ozer Ozkahraman (ozero@kth.se)
    
import rospy
import tf
import time
import math
import numpy as np

import common_globals
import imc_enums
import bb_enums

from tf.transformations import quaternion_from_euler

from geometry_msgs.msg import Point, PointStamped, Pose, PoseArray
from geographic_msgs.msg import GeoPoint
from smarc_msgs.srv import LatLonToUTM, UTMToLatLon
from smarc_msgs.msg import GotoWaypointGoal, GotoWaypoint, MissionControl

from coverage_planner import create_coverage_path
from dubins import calc_dubins_path, dubins_traj, waypoints_with_yaw

class Waypoint:
    def __init__(self,
                 goto_waypoint = None,
                 imc_man_id = None,
                 extra_data = None):
        """
        goto_waypoint message reference:
            uint8 Z_CONTROL_NONE=0
            uint8 Z_CONTROL_DEPTH=1
            uint8 Z_CONTROL_ALTITUDE=2
            uint8 SPEED_CONTROL_NONE=0
            uint8 SPEED_CONTROL_RPM=1
            uint8 SPEED_CONTROL_SPEED=2
            geometry_msgs/PoseStamped pose
              std_msgs/Header header
                uint32 seq
                time stamp
                string frame_id
              geometry_msgs/Pose pose
                geometry_msgs/Point position
                  float64 x
                  float64 y
                  float64 z
                geometry_msgs/Quaternion orientation
                  float64 x
                  float64 y
                  float64 z
                  float64 w
            float64 goal_tolerance
            uint8 z_control_mode
            float64 travel_altitude
            float64 travel_depth
            uint8 speed_control_mode
            float64 travel_rpm
            float64 travel_speed
            float64 lat
            float64 lon
            string name
        """


        # this is a numerical "type" of maneuver identifier used
        # only for imc/neptus
        self.imc_man_id = imc_man_id
        # the GotoWaypoint object from smarc_msgs.msg
        self.wp = goto_waypoint
        self.extra_data = extra_data


    def set_utm_from_latlon(self, lat_lon_to_utm_serv, set_frame=False):
        gp = GeoPoint()
        gp.latitude = self.wp.lat
        gp.longitude = self.wp.lon
        gp.altitude = 0
        res = lat_lon_to_utm_serv(gp)
        self.wp.pose.pose.position.x = res.utm_point.x
        self.wp.pose.pose.position.y = res.utm_point.y
        if set_frame:
            self.wp.pose.header.frame_id = 'utm'

    def set_latlon_from_utm(self, utm_to_lat_lon_serv, set_frame=False):
        p = Point()
        p.x = self.x
        p.y = self.y
        p.z = -self.wp.travel_depth
        res = utm_to_lat_lon_serv(p)
        self.wp.lat = res.lat_lon_point.latitude
        self.wp.lon = res.lat_lon_point.longitude
        if set_frame:
            self.wp.pose.header.frame_id = 'latlon'

    def is_too_similar_to_other(self, other_wp):
        """
        other_wp is a smarc_msgs GotoWaypoint
        """
        xy_tolerance = min(self.wp.goal_tolerance, other_wp.goal_tolerance)
        z_tolerance = 0.6 # hardcoded... i dont know what harsha is doing here?
        rpm_tolerance = 50 #rpm
        speed_tolerance = 0.1 #m/s

        xy_too_close = False
        z_too_close = False
        speed_too_close = False

        # if the control mode is different, they _are_ different wps
        if self.wp.z_control_mode != other_wp.z_control_mode:
            return False
        # otherwise, we gotta see if the _amounts_ are different enough
        else:
            if self.wp.z_control_mode == GotoWaypoint.Z_CONTROL_DEPTH:
                z_too_close = abs(self.wp.travel_depth - other_wp.travel_depth) < z_tolerance

            if self.wp.z_control_mode == GotoWaypoint.Z_CONTROL_ALTITUDE:
                z_too_close = abs(self.wp.travel_altitude - other_wp.travel_altitude) < z_tolerance

        # same with speed
        if self.wp.speed_control_mode != other_wp.speed_control_mode:
            return False
        else:
            if self.wp.speed_control_mode == GotoWaypoint.SPEED_CONTROL_RPM:
                speed_too_close = abs(self.wp.travel_rpm - other_wp.travel_rpm) < rpm_tolerance

            if self.wp.speed_control_mode == GotoWaypoint.SPEED_CONTROL_SPEED:
                speed_too_close = abs(self.wp.travel_speed - other_wp.travel_speed) < speed_tolerance

        xy_dist = math.sqrt( (self.x - other_wp.pose.pose.position.x)**2 + (self.y - other_wp.pose.pose.position.y)**2 )
        xy_too_close = xy_dist < xy_tolerance

        # if all of them are too close, wps are similar
        # otherwise they are different enough
        # if the control modes are different, that was handled above already
        return all((xy_too_close, z_too_close, speed_too_close))





    @property
    def x(self):
        return self.wp.pose.pose.position.x

    @property
    def y(self):
        return self.wp.pose.pose.position.y

    @property
    def depth(self):
        return self.wp.travel_depth

    @property
    def frame_id(self):
        return self.wp.pose.header.frame_id

    @property
    def is_actionable(self):
        if (self.x == 0 and self.y == 0) and self.frame_id == 'utm':
            return False

        return True

    def read_imc_maneuver(self, maneuver, utm_x, utm_y, extra_data=None):
        gwp  = GotoWaypoint()
        gwp.pose.header.frame_id = 'utm'
        gwp.pose.pose.position.x = utm_x
        gwp.pose.pose.position.y = utm_y
        gwp.lat = np.degrees(maneuver.lat) # because neptus uses radians, we use degrees
        gwp.lon = np.degrees(maneuver.lon)
        gwp.name = maneuver.maneuver_name
        gwp.goal_tolerance = 2 # to make this reactive, whoever sends the WP should set it

        # convert the IMC enums into SMaRC enums
        if maneuver.speed_units == imc_enums.SPEED_UNIT_RPM:
            gwp.speed_control_mode = GotoWaypoint.SPEED_CONTROL_RPM
            gwp.travel_rpm = maneuver.speed
        elif maneuver.speed_units == imc_enums.SPEED_UNIT_MPS:
            gwp.speed_control_mode = GotoWaypoint.SPEED_CONTROL_SPEED
            gwp.travel_speed = maneuver.speed
        else:
            gwp.speed_control_mode = GotoWaypointGoal.SPEED_CONTROL_NONE
            rospy.logwarn("Speed control of the waypoint is NONE!")

        gwp.z_control_mode = maneuver.z_units # same in smarc and imc
        if maneuver.z_units == imc_enums.Z_DEPTH:
            gwp.travel_depth = maneuver.z
        elif maneuver.z_units == imc_enums.Z_ALTITUDE:
            gwp.travel_altitude = maneuver.z
        else:
            rospy.logwarn("Z control mode not depth or alt, defaulting to 0 depth!")
            gwp.travel_depth = 0
            gwp.z_control_mode = GotoWaypoint.Z_CONTROL_DEPTH

        self.imc_man_id = maneuver.maneuver_imc_id
        self.wp = gwp
        self.extra_data = extra_data


    def __str__(self):
        s = 'Man: {}'.format(self.wp)
        return s



class MissionPlan:
    def __init__(self,
                 auv_config,
                 mission_control_msg = None,
                 plandb_msg = None,
                 plan_id = None,
                 coverage_swath = None,
                 vehicle_localization_error_growth = None,
                 waypoints=None
                 ):
        """
        A container object to keep things related to the mission plan.
        """
        # used to report when the mission was received
        self.creation_time = time.time()

        self.plandb_msg = plandb_msg
        if plandb_msg is not None:
            self.plan_id = plandb_msg.plan_id
        else:
            if plan_id is None:
                plan_id = "Unnamed - self.creation_time"

            self.plan_id = plan_id

        self.auv_conf = auv_config
        self.plan_frame = auv_config.UTM_LINK
        self.coverage_swath = coverage_swath
        self.vehicle_localization_error_growth = vehicle_localization_error_growth

        # test if the service is usable!
        # if not, test the backup
        # if that fails too, raise exception
        self.no_service = False
        self.latlontoutm_service_name = auv_config.LATLONTOUTM_SERVICE
        try:
            rospy.loginfo("Waiting (0.5s) lat_lon_to_utm service:{}".format(self.latlontoutm_service_name))
            rospy.wait_for_service(self.latlontoutm_service_name, timeout=0.5)
        except:
            rospy.logwarn(str(self.latlontoutm_service_name)+" service could be connected to!")
            self.latlontoutm_service_name = auv_config.LATLONTOUTM_SERVICE_ALTERNATIVE
            rospy.logwarn("Setting the service to the alternative:{}".format(self.latlontoutm_service_name))
            try:
                rospy.loginfo("Waiting (10s) lat_lon_to_utm service alternative:{}".format(self.latlontoutm_service_name))
                rospy.wait_for_service(self.latlontoutm_service_name, timeout=10)
            except:
                rospy.logerr("No lat_lon_to_utm service could be reached! The BT can not accept missions in this state!")
                rospy.logerr("The BT received a mission, tried to convert it to UTM coordinates using {} service and then {} as the backup and neither of them could be reached! Check the navigation/DR stack, the TF tree and the services!".format(latlontoutm_service_name, latlontoutm_service_name_alternative))
                self.no_service = True

        self.compute_dubins = True
        # self.utmtolatlon_service_name = auv_config.UTM_TO_LATLON_SERVICE
        # try:
        #     rospy.loginfo("Waiting (0.5s) utm_to_lat_lon service:{}".format(self.utmtolatlon_service_name))
        #     rospy.wait_for_service(self.utmtolatlon_service_name, timeout=0.5)
        # except:
        #     rospy.logwarn(str(self.utmtolatlon_service_name)+" service could be connected to!")
        #     self.utmtolatlon_service_name = auv_config.UTM_TO_LATLON_SERVICE_ALTERNATIVE
        #     rospy.logwarn("Setting the service to the alternative:{}".format(self.utmtolatlon_service_name))
        #     try:
        #         rospy.loginfo("Waiting (10s) lat_lon_to_utm service alternative:{}".format(self.utmtolatlon_service_name))
        #         rospy.wait_for_service(self.utmtolatlon_service_name, timeout=10)
        #     except:
        #         rospy.logwarn("Can't reach the utm_to_lat_lon service, the dubins path won't be computed.")
        #         self.compute_dubins = False


        # a list of names for each maneuver
        # good for feedback
        self.waypoint_man_ids = []

        # if waypoints are given directly, then skip reading the plandb message
        if waypoints is None and plandb_msg is not None:
            self.waypoints = self.read_plandb(plandb_msg)
        elif waypoints is None and mission_control_msg is not None:
            if self.compute_dubins:
                dubins_mission = self.dubins_mission_planner(mission_control_msg)
                self.waypoints = self.read_mission_control(dubins_mission, is_in_utm=True)
            else:
                self.waypoints = self.read_mission_control(mission_control_msg)
        elif waypoints is not None:
            self.waypoints = waypoints
        else:
            self.waypoints = []

        for wp in self.waypoints:
            self.waypoint_man_ids.append(wp.wp.name)

        # keep track of which waypoint we are going to
        # start at -1 to indicate that _we are not going to any yet_
        self.current_wp_index = -1

        # state of this plan
        self.plan_is_go = False


    def _get_latlon_to_utm_service(self):
        try:
            rospy.wait_for_service(self.latlontoutm_service_name, timeout=1)
        except:
            rospy.logwarn(str(self.latlontoutm_service_name)+" service not found!")
            return (None, None)

        try:
            latlontoutm_service = rospy.ServiceProxy(self.latlontoutm_service_name,
                                                     LatLonToUTM)
        except rospy.service.ServiceException:
            rospy.logerr_throttle_identical(5, "LatLon to UTM service failed! namespace:{}".format(self.latlontoutm_service_name))
            return None
        return latlontoutm_service

    def _get_utm_to_latlon_service(self):
        try:
            rospy.wait_for_service(self.utmtolatlon_service_name, timeout=5)
        except:
            rospy.logwarn(str(self.utmtolatlon_service_name)+" service not found!")
            return (None, None)

        try:
            utmtolatlon_service = rospy.ServiceProxy(self.utmtolatlon_service_name,
                                                     UTMToLatLon)
        except rospy.service.ServiceException:
            rospy.logerr_throttle_identical(5, "UTM to LatLon service failed! namespace:{}".format(self.utmtolatlon_service_name))
            return None
        return utmtolatlon_service


    def latlon_to_utm(self,
                      lat,
                      lon,
                      z,
                      in_degrees=False,
                      serv=None):

        if serv is None:
            serv = self._get_latlon_to_utm_service()
            if serv is None:
                return (None, None)

        gp = GeoPoint()
        if in_degrees:
            gp.latitude = lat
            gp.longitude = lon
        else:
            gp.latitude = np.degrees(lat)
            gp.longitude = np.degrees(lon)
        gp.altitude = z
        res = serv(gp)
        return (res.utm_point.x, res.utm_point.y)

    def utm_to_latlon(self,
                      utm_x,
                      utm_y,
                      serv=None):

        if serv is None:
            serv = self._get_utm_to_latlon_service()
            if serv is None:
                return (None, None)

        point = Point()
        point.x = utm_x
        point.y = utm_y
        
        res = serv(point)

        return (res.lat_lon_point.latitude, res.lat_lon_point.longitude)


    def read_mission_control(self, msg, is_in_utm=False):
        """
        read the waypoints off a smarc_msgs/MissionControl message
        and set our plan_id from its name
        """
        self.plan_id = msg.name
        waypoints = []
        serv = self._get_latlon_to_utm_service()
        for wp_msg in msg.waypoints:
            wp = Waypoint(goto_waypoint = wp_msg,
                          imc_man_id = imc_enums.MANEUVER_GOTO)
            # also make sure they are in utm
            if not is_in_utm:
                wp.set_utm_from_latlon(serv, set_frame=True)
            waypoints.append(wp)

        return waypoints



    # XXX could use a cleanup... 
    def read_plandb(self, plandb):
        """
        planddb message is a bunch of nested objects,
        we want a list of waypoints in the local frame,
        """
        if self.no_service:
            rospy.logerr("The BT can not reach the latlon_to_utm service!")
            return []

        waypoints = []
        request_id = plandb.request_id
        plan_id = plandb.plan_id
        plan_spec = plandb.plan_spec

        if len(plan_spec.maneuvers) <= 0:
            rospy.logwarn("THERE WERE NO MANEUVERS IN THE PLAN! plan_id:{} (Does this vehicle know of your plan's maneuvers?)".format(plan_id))

        for plan_man in plan_spec.maneuvers:
            man_id = plan_man.maneuver_id
            maneuver = plan_man.maneuver
            man_name = maneuver.maneuver_name
            man_imc_id = maneuver.maneuver_imc_id
            # probably every maneuver has lat lon z in them, but just in case...
            # goto and sample are identical, with sample having extra "syringe" booleans...
            # cover_area is also the same, with extra Polygon field, that we can 
            # straight translate to more goto waypoints

            # GOTO, SAMPLE
            if man_imc_id in [imc_enums.MANEUVER_GOTO, imc_enums.MANEUVER_SAMPLE]:
                utm_x, utm_y = self.latlon_to_utm(maneuver.lat,
                                                  maneuver.lon,
                                                  -maneuver.z)
                if utm_x is None:
                    rospy.loginfo("Could not convert LATLON to UTM! Skipping point:{}".format((maneuver.lat, maneuver.lon, man_name)))
                    continue

                # EXTRA STUFF FOR SAMPLE
                extra_data = {}
                if man_imc_id == imc_enums.MANEUVER_SAMPLE:
                    extra_data = {'syringe0':maneuver.syringe0,
                                  'syringe1':maneuver.syringe1,
                                  'syringe2':maneuver.syringe2}


                # construct the waypoint object
                wp = Waypoint()
                wp.read_imc_maneuver(maneuver, utm_x, utm_y, extra_data)
                waypoints.append(wp)


            # COVER AREA
            elif man_imc_id == imc_enums.MANEUVER_COVER_AREA:
                if self.vehicle_localization_error_growth is None or \
                   self.coverage_swath is None:
                    rospy.loginfo("I do not know either the error growth or the swath, so I can not plan for coverage! Skipping the CoverArea maneuver!")
                    continue

                # always go to the point given in map as the first move.
                utm_x, utm_y = self.latlon_to_utm(maneuver.lat,
                                                  maneuver.lon,
                                                  -maneuver.z)


                utm_poly_points = [(utm_x, utm_y)]
                # this maneuver has an extra polygon with it
                # that we want to generate waypoints inside of
                # generate the waypoints here and add them as goto waypoints
                if len(maneuver.polygon) > 2:
                    rospy.loginfo("Generating rectangular coverage pattern")
                    utm_poly_points += [self.latlon_to_utm(polyvert.lat, polyvert.lon, -maneuver.z) for polyvert in maneuver.polygon]
                    coverage_points = self.generate_coverage_pattern(utm_poly_points)
                else:
                    rospy.loginfo("This polygon ({}) has too few polygons for a coverarea, it will be used as a simple waypoint!".format(man_id))
                    coverage_points = utm_poly_points


                for i,point in enumerate(coverage_points):
                    wp = Waypoint()
                    wp.read_imc_maneuver(maneuver, point[0], point[1], {"poly":maneuver.polygon})
                    wp.wp.name = str(man_id) + "_{}/{}".format(i+1, len(coverage_points))
                    wp.imc_man_id = imc_enums.MANEUVER_GOTO
                    waypoints.append(wp)


            # UNIMPLEMENTED MANEUVER
            else:
                rospy.logwarn("SKIPPING UNIMPLEMENTED MANEUVER: id:{}, name:{}".format(man_imc_id, man_name))


        # sanity check
        if len(waypoints) <= 0:
            rospy.logerr("NO MANEUVERS IN MISSION PLAN!")

        return waypoints



    def generate_coverage_pattern(self, polygon):
        return create_coverage_path(polygon,
                                    self.coverage_swath,
                                    self.vehicle_localization_error_growth)


    def get_pose_array(self, flip_z=False):
        pa = PoseArray()
        pa.header.frame_id = self.plan_frame

        # add the rest of the waypoints
        for wp in self.waypoints:
            p = Pose()
            p.position.x = wp.x
            p.position.y = wp.y
            if flip_z:
                p.position.z = -wp.travel_depth
            else:
                p.position.z = wp.travel_depth
            pa.poses.append(p)

        return pa


    def path_to_list(self, path_msg):
        frame = path_msg.header.frame_id
        if frame != '' and frame != self.plan_frame:
            rospy.logerr_throttle_identical(5, "Waypoints are not in "+self.plan_frame+" they are in "+frame+" !")
            return []

        wps = []
        for pose_stamped in path_msg.poses:
            wp = (
                pose_stamped.pose.position.x,
                pose_stamped.pose.position.y,
                pose_stamped.pose.position.z
            )
            wps.append(wp)
        return wps


    def dubins_mission_planner(self, mission, turn_radius=10, num_points=2):
        '''
        Reads the waypoints from a MissionControl message and generates a sampled dubins 
        path between them. It returns a new MissionControl message equal to the input one
        except for the waypoints attribute.
        num_points represents the amount of waypoints to keep on each curve of the trajectory
        '''
        

        rospy.loginfo("Computing dubins path")

        ll_to_utm_serv = self._get_latlon_to_utm_service()

        # Wait for LoLo's first position before generating the path
        # XXX Check gps fix before converting lat/lon to utm
        # latlon_topic = rospy.get_param('latlon_topic', "/dr/lat_lon")
        latlon_topic = self.auv_conf.LATLON_TOPIC
        robot_name = self.auv_conf.robot_name
        latlon_topic = "/" + robot_name + "/" + latlon_topic
        gp = rospy.wait_for_message(latlon_topic, GeoPoint)
        utm_x_lolo, utm_y_lolo = self.latlon_to_utm(gp.latitude, gp.longitude, gp.altitude, serv=ll_to_utm_serv)
        lolo_utm = np.array([utm_x_lolo, utm_y_lolo])

        # Get x, y of waypoints from original mission lat/lon
        waypoints = []
        for wp in mission.waypoints:
            utm_x, utm_y = self.latlon_to_utm(wp.lat, wp.lon, wp.pose.pose.position.z, serv=ll_to_utm_serv)
            waypoints.append([utm_x, utm_y])
        waypoints_np = np.array(waypoints)

        # Generate path starting from LoLo's location
        waypoints_np = np.vstack((lolo_utm, waypoints_np))

        # Keep the other waypoints' parameters equal
        mwp = mission.waypoints[0]
        goal_tolerance = mwp.goal_tolerance
        z_control_mode = mwp.z_control_mode
        travel_altitude = mwp.travel_altitude
        travel_depth = mwp.travel_depth
        speed_control_mode = mwp.speed_control_mode
        travel_rpm = mwp.travel_rpm
        travel_speed = mwp.travel_speed

        # Compute angle between waypoints and get the new wps array
        waypoints_yaw, _ = waypoints_with_yaw(waypoints_np)

        path = []
        for j in range(len(waypoints_yaw)-1):
            param = calc_dubins_path(waypoints_yaw[j], waypoints_yaw[j+1], turn_radius)
            path.append(dubins_traj(param,1))

        ####
        ## This approach creates too many waypoints
        # dubins_waypoints = []
        # for el in path:
        #     for e in el:
        #         dubins_waypoints.append([e[0], e[1], e[2]]) # x, y, yaw[deg]
        ## Sample thw whole path
        # final_points_np = np.array(final_points)
        # N = float(len(final_points_np))
        # n = float(int(N/num_points_coeff)) # amount of waypoints to set along the path
        # mask = np.random.choice([False, True], len(final_points_np), p=[1.0-(n/N), n/N])
        # dubins_waypoints = final_points_np[mask]
        ####

        dubins_waypoints = []
        # Only define the new waypoints on the original waypoint, in the middle of each segment 
        # and on the curve
        for i, el in enumerate(path):
            dubins_waypoints.append([waypoints_yaw[i].x, waypoints_yaw[i].y, waypoints_yaw[i].psi]) # include original waypoints
            dubins_waypoints.append([el[int(len(el)/2), 0], el[int(len(el)/2), 1], el[int(len(el)/2), 2]]) # include point in the middle
            # Compute the differnece between orientation 
            # The maxima represent the points on the curve
            delta_angles = abs(np.diff(el[:, 2]))
            delta_angles = [0] + delta_angles
            max_idxs = np.argpartition(delta_angles, -num_points)[-num_points:] # Keep two points in each curve
            max_idxs = np.sort(max_idxs)
            for ind in max_idxs:
                dubins_waypoints.append(el[ind])

        # Sort the waypoints
        full_path = []
        order_idxs = []
        for el in path:
            for e in el:
                full_path.append([e[0], e[1], e[2]])
        for el in dubins_waypoints:
            order_idxs.append(full_path.index([el[0], el[1], el[2]])) # Get the dubins wp index in the full path
        order_idxs = np.sort(order_idxs)
        dubins_waypoints = [full_path[i] for i in order_idxs]

        dubins_mission = MissionControl()
        dubins_mission = mission

        # utm_to_latlon service called here to avoid calling it in the loop
        # utm_to_ll_serv = self._get_utm_to_latlon_service()

        del dubins_mission.waypoints[:]
        dwp = GotoWaypoint()
        for wp in dubins_waypoints:
            dwp.pose.pose.position.x = wp[0]
            dwp.pose.pose.position.y = wp[1]
            dwp.pose.pose.position.z = travel_altitude
            # dwp.lat, dwp.lon = self.utm_to_latlon(wp[0], wp[1], utm_to_ll_serv)

            quaternion = quaternion_from_euler(0.0, 0.0, wp[2])
            dwp.pose.pose.orientation.x = quaternion[0]
            dwp.pose.pose.orientation.y = quaternion[1]
            dwp.pose.pose.orientation.z = quaternion[2]
            dwp.pose.pose.orientation.w = quaternion[3]

            dwp.goal_tolerance = goal_tolerance
            dwp.z_control_mode = z_control_mode
            dwp.travel_altitude = travel_altitude 
            dwp.travel_depth = travel_depth
            dwp.speed_control_mode = speed_control_mode
            dwp.travel_rpm = travel_rpm
            dwp.travel_speed = travel_speed

            dubins_mission.waypoints.append(dwp)

        rospy.loginfo("Dubins mission ready")
        return dubins_mission




    def __str__(self):
        s = self.plan_id+':\n'
        for wp in self.waypoints:
            s += '\t'+str(wp)+'\n'
        return s



    def is_complete(self):
        # check if we are 'done'
        if self.current_wp_index >= len(self.waypoints):
            # we went tru all wps, we're done
            return True

        return False

    def is_in_progress(self):
        if self.current_wp_index < len(self.waypoints) and\
           self.current_wp_index >= 0:
            return True

        return False


    def visit_wp(self):
        """ call this when you finish going to the wp"""
        if self.is_complete():
            return

        self.current_wp_index += 1


    def get_current_wp(self):
        """
        pop a wp from the remaining wps and return it
        """
        if self.is_complete():
            return None

        # we havent started yet, this is the first time ever
        # someone wanted a wp, meaning we _start now_
        if self.current_wp_index == -1:
            self.current_wp_index = 0

        wp = self.waypoints[self.current_wp_index]
        return wp



