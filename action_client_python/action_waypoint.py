import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from nav2_msgs.action import FollowWaypoints
from geometry_msgs.msg import PoseStamped
from tf_transformations import quaternion_from_euler
import yaml
import os
import websocket
import json
import threading
import time
import logging
from datetime import datetime
from ament_index_python.packages import get_package_share_directory

# Configure logging to output to stdout with proper formatting
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    filename='/home/sabbi/ros_link/rlog/action_client.log',  # <-- Set your desired file path here
    filemode='a'  # 'a' for append, 'w' for overwrite
    
)
logger = logging.getLogger(__name__)

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

config_path = os.path.join(
    get_package_share_directory('action_client_python'),
    'config',
    'waypoints.yaml'
)

class FollowWaypointsClient(Node):

    def __init__(self):
        super().__init__('follow_waypoints_client')
        self._action_client = ActionClient(self, FollowWaypoints, '/follow_waypoints')
        self.waypoints_data = self.load_waypoints()
        self.ws = None
        self.ws_thread = None
        self.ws_url = "ws://localhost:48236"  # Change to your WebSocket server address
        self.current_order_id = None
        self.should_reconnect = True
        self.connected = False
        self.connect_to_websocket()

    def connect_to_websocket(self):
        """Establish WebSocket connection with retry until connected"""
        logger.info(f"{Colors.BLUE}Attempting to connect to WebSocket at {self.ws_url}{Colors.END}")

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if 'tray' in data:
                    logger.info(f"{Colors.GREEN}Received valid table order: {data['tray']}{Colors.END}")
                    self.current_order_id = data['order']
                    data['tray'].append("T0")
                    self.send_goal(data['tray'])
                    logger.info(f"{Colors.CYAN}Waypoints to follow: {data['tray']}{Colors.END}")
                else:
                    logger.warning(f"{Colors.YELLOW}Received data missing 'order' field{Colors.END}")
            except json.JSONDecodeError:
                logger.error(f"{Colors.RED}Failed to parse WebSocket message as JSON{Colors.END}")
            except Exception as e:
                logger.error(f"{Colors.RED}Error processing message: {e}{Colors.END}")
            
        def on_error(ws, error):
            logger.error(f"{Colors.RED}WebSocket error: {error}{Colors.END}")
            
        def on_close(ws, close_status_code, close_msg):
            logger.error(f"{Colors.RED}WebSocket connection closed{Colors.END}")
            self.connected = False
            if self.should_reconnect:
                logger.warning(f"{Colors.YELLOW}Attempting to reconnect...{Colors.END}")
                time.sleep(5)  # Wait before reconnecting
                self.connect_to_websocket()
            
        def on_open(ws):
            logger.info(f"{Colors.GREEN}WebSocket connection established{Colors.END}")
            self.connected = True

        def run_ws():
            while not self.connected and self.should_reconnect:
                logger.info(f"{Colors.BLUE}Trying to connect...{Colors.END}")
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
                    logger.error(f"{Colors.RED}WebSocket run_forever exception: {e}{Colors.END}")
                    time.sleep(5)  # Wait before retrying
                    continue

        # Start WebSocket thread if not already running
        if self.ws_thread is None or not self.ws_thread.is_alive():
            self.ws_thread = threading.Thread(target=run_ws)
            self.ws_thread.daemon = True
            self.ws_thread.start()
    
    def load_waypoints(self):
        try:
            with open(config_path, 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            logger.error("waypoints.yaml file not found!")
            return None
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML file: {e}")
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
                logger.error(f"Waypoint {name} not found!")

        if not waypoints:
            logger.error("No valid waypoints to follow!")
            return
            
        goal_msg = FollowWaypoints.Goal()
        goal_msg.poses = waypoints
        
        logger.info(f'waypoint names: {waypoints}')
        logger.info(f'waiting for server...')

        self._action_client.wait_for_server()

        logger.info(f'Connected to action server')
        
        self._send_goal_future = self._action_client.send_goal_async(goal_msg,
                feedback_callback=self.feedback_callback)
        
        self._send_goal_future.add_done_callback(self.goal_response_callback)

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        logger.info(f'Currently at waypoint index: {feedback.current_waypoint}')

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            logger.info('Goal rejected')
            return

        logger.info('Goal accepted')
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        logger.info(f'Result: {result.missed_waypoints}')
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
            logger.error(f'Failed to send result over WebSocket: {e}')
            
    def destroy(self):
        self.should_reconnect = False
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
            logger.info(f"{Colors.YELLOW}WebSocket thread joined the main: {Colors.END}")

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
        # Keep the node running to process WebSocket messages and ROS callbacks
        rclpy.spin(client)
    except KeyboardInterrupt:
        client.destroy()
        client.destroy_node()
        logger.info("\nShutting down gracefully...")
    finally:
        client.destroy()
        client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()
