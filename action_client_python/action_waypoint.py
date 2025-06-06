import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from std_msgs.msg import Empty
from nav2_msgs.action import FollowWaypoints, NavigateToPose
from geometry_msgs.msg import PoseStamped
from tf_transformations import quaternion_from_euler
import yaml
import os
import websocket
import json
import threading
import time
import logging
import sys
from datetime import datetime
from ament_index_python.packages import get_package_share_directory

class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '[96m',
        'INFO': '[92m',
        'WARNING': '[93m',
        'ERROR': '[91m',
        'CRITICAL': '[91m',
    }
    RESET = '[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        message = super().format(record)
        if color:
            message = f"{color}{message}{self.RESET}"
        return message

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

file_formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_formatter = ColorFormatter(
    '[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

file_handler = logging.FileHandler(
    filename='/home/sabbi/ros_link/rlog/action_client.log',
    mode='a'
)
file_handler.setFormatter(file_formatter)
console_handler = logging.StreamHandler(stream=sys.stdout)
console_handler.setFormatter(console_formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

class FollowWaypointsClient(Node):
    def __init__(self):
        super().__init__('follow_waypoints_client')
        self._waypoint_action_client = ActionClient(self, FollowWaypoints, '/follow_waypoints')
        self._pose_action_client = ActionClient(self, NavigateToPose, '/navigate_to_pose')
        self.next_waypoint_publisher = self.create_publisher(Empty,'input_at_waypoint/input',10)
        self.waypoints_data = self.load_waypoints()
        self.ws = None
        self.ws_thread = None
        self.ws_url = "ws://localhost:48236"
        self.current_order_id = None
        self.should_reconnect = True
        self.connected = False
        self.current_goal_handle = None
        self.connect_to_websocket()

    def connect_to_websocket(self):
        logger.info("Attempting to connect to WebSocket at %s", self.ws_url)

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get('type') == "waypoint_order":
                    logger.info(f"Received valid table order: {data['order']}")
                    if 'tray' in data:
                        logger.info("Received valid table order: %s", data['tray'])
                        self.current_order_id = data['order']
                        data['tray'].append("T0")
                        self.send_goal(data['tray'])
                        logger.info("Waypoints to follow: %s", data['tray'])
                    else:
                        logger.warning("Received data missing 'order' field")
                elif data.get('type') == "waypoint_next":
                    if data['publish']:
                        self.publish_next_waypoint_signal()
                elif data.get('type') == "waypoint_cancel":
                    logger.info(f"Cancelling all waypoints...")
                    self._cancel_timer = self.create_timer(0.1, self._cancel_timer_callback)
            except json.JSONDecodeError:
                logger.error("Failed to parse WebSocket message as JSON")
            except Exception as e:
                logger.error("Error processing message: %s", e)

        def on_error(ws, error):
            logger.error("WebSocket error: %s", error)

        def on_close(ws, close_status_code, close_msg):
            logger.error("WebSocket connection closed")
            self.connected = False
            if self.should_reconnect:
                logger.warning("Attempting to reconnect...")
                time.sleep(5)
                self.connect_to_websocket()

        def on_open(ws):
            logger.info("WebSocket connection established")
            self.connected = True

        def run_ws():
            while not self.connected and self.should_reconnect:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.ws_url,
                        on_open=on_open,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close
                    )
                    self.ws.run_forever()
                except Exception as e:
                    logger.error("WebSocket run_forever exception: %s", e)
                    time.sleep(5)

        if self.ws_thread is None or not self.ws_thread.is_alive():
            self.ws_thread = threading.Thread(target=run_ws)
            self.ws_thread.daemon = True
            self.ws_thread.start()

    def publish_next_waypoint_signal(self):
        msg = Empty()
        self.next_waypoint_publisher.publish(msg)
        logger.info("Published Empty to input_at_waypoint/input topic")

    def cancel_waypoints(self):
        try:
            if self.current_goal_handle is not None:
                logger.info("Cancelling current waypoint navigation...")
                self.publish_next_waypoint_signal()
                time.sleep(0.1)
                cancel_future = self.current_goal_handle.cancel_goal_async()
                cancel_future.add_done_callback(self._cancel_waypoints_complete)
            else:
                logger.warning("No active goal to cancel. Proceeding to cancel pose.")
                self.go_to_cancel_pose()
        except Exception as e:
            logger.error(f"Error during cancellation: {e}")
            self.go_to_cancel_pose()

    def _cancel_waypoints_complete(self, future):
        try:
            cancel_response = future.result()
            if cancel_response.goals_canceling:
                logger.info("Successfully canceled waypoint navigation!")
            else:
                logger.warning("Failed to cancel goal or was already completed.")
        except Exception as e:
            logger.error(f"Error in cancellation callback: {e}")
        time.sleep(2)
        self.go_to_cancel_pose()

    def go_to_cancel_pose(self):
        pose = self.get_waypoint_by_name("T0")
        if pose is None:
            logger.error("T0 pose not found in waypoints file.")
            return

        goal = NavigateToPose.Goal()
        goal.pose = pose

        self._pose_action_client.wait_for_server()
        logger.info("Sending navigation goal to cancel pose (T0)...")

        nav_future = self._pose_action_client.send_goal_async(goal)

        def on_result(future):
            result = future.result()
            if result.accepted:
                logger.info("Navigation goal accepted. Waiting for result...")
                result.get_result_async().add_done_callback(lambda r: logger.info("Reached cancel pose."))
            else:
                logger.warning("Navigation goal to cancel pose was rejected.")

        nav_future.add_done_callback(on_result)

    def _cancel_timer_callback(self):
        self.destroy_timer(self._cancel_timer)
        self.cancel_waypoints()

    def load_waypoints(self):
        config_path = os.path.join(
            get_package_share_directory('action_client_python'),
            'config',
            'waypoints.yaml'
        )
        try:
            with open(config_path, 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            logger.error("waypoints.yaml file not found!")
            return None
        except yaml.YAMLError as e:
            logger.error("Error parsing YAML file: %s", e)
            return None

    def get_waypoint_by_name(self, name):
        if not self.waypoints_data:
            return None
        wp = self.waypoints_data['waypoints'].get(name)
        if wp:
            return create_pose(wp['x'], wp['y'], wp['theta'])
        return None

    def send_goal(self, waypoint_names):
        if not self.waypoints_data:
            logger.error("No waypoints data available!")
            return

        waypoints = []
        for name in waypoint_names:
            pose = self.get_waypoint_by_name(name)
            if pose:
                waypoints.append(pose)
            else:
                logger.error("Waypoint %s not found!", name)

        if not waypoints:
            logger.error("No valid waypoints to follow!")
            return

        goal_msg = FollowWaypoints.Goal()
        goal_msg.poses = waypoints

        self._waypoint_action_client.wait_for_server()
        logger.info('Connected to action server')

        self._send_goal_future = self._waypoint_action_client.send_goal_async(
            goal_msg,
            feedback_callback=self.feedback_callback
        )
        self._send_goal_future.add_done_callback(self.goal_response_callback)

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        logger.info('Currently at waypoint index: %s', feedback.current_waypoint)

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            logger.info('Goal rejected')
            return

        logger.info('Goal accepted')
        self.current_goal_handle = goal_handle
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        logger.info('Result: %s', result.missed_waypoints)
        try:
            if self.connected and self.ws:
                result_message = json.dumps({
                    'type': 'waypoint_result',
                    'order': self.current_order_id,
                    'sequence': result.missed_waypoints.tolist()
                })
                self.ws.send(result_message)
                logger.info('Sent result back to WebSocket server')
            else:
                logger.warning('WebSocket not available to send result')
        except Exception as e:
            logger.error('Failed to send result over WebSocket: %s', e)

    def destroy(self):
        self.should_reconnect = False
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
            logger.info("WebSocket thread joined the main")

def create_pose(x, y, theta):
    pose = PoseStamped()
    pose.header.frame_id = 'map'
    pose.pose.position.x = x
    pose.pose.position.y = y
    pose.pose.position.z = 0.0
    q = quaternion_from_euler(0, 0, theta)
    pose.pose.orientation.x = q[0]
    pose.pose.orientation.y = q[1]
    pose.pose.orientation.z = q[2]
    pose.pose.orientation.w = q[3]
    return pose

def main(args=None):
    rclpy.init(args=args)

    client = FollowWaypointsClient()

    try:
        rclpy.spin(client)
    except KeyboardInterrupt:
        logger.info("\nShutting down gracefully...")
    finally:
        client.destroy()
        client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()
