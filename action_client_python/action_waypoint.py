import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from nav2_msgs.action import FollowWaypoints
from geometry_msgs.msg import PoseStamped
from tf_transformations import quaternion_from_euler
import yaml
import os
import websocketimport rclpy
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
from datetime import datetime
from ament_index_python.packages import get_package_share_directory

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

def get_current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_info(node, message):
    node.get_logger().info(message)
    print(f"[{get_current_time()}] INFO: {message}")

def log_error(node, message):
    node.get_logger().error(message)
    print(f"[{get_current_time()}] ERROR: {message}")

def log_warn(node, message):
    node.get_logger().warn(message)
    print(f"[{get_current_time()}] WARN: {message}")

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
        print(f"[{get_current_time()}] {Colors.BLUE}Attempting to connect to WebSocket at {self.ws_url}{Colors.END}")

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if 'tray' in data:
                    print(f"[{get_current_time()}] {Colors.GREEN}Received valid table order: {data['tray']}{Colors.END}")
                    self.current_order_id = data['order']
                    data['tray'].append("T0")
                    self.send_goal(data['tray'])
                    print(f"[{get_current_time()}] {Colors.CYAN}Waypoints to follow: {data['tray']}{Colors.END}")
                else:
                    print(f"[{get_current_time()}] {Colors.YELLOW}Received data missing 'order' field{Colors.END}")
            except json.JSONDecodeError:
                print(f"[{get_current_time()}] {Colors.RED}Failed to parse WebSocket message as JSON{Colors.END}")
            except Exception as e:
                print(f"[{get_current_time()}] {Colors.RED}Error processing message: {e}{Colors.END}")
            
        def on_error(ws, error):
            print(f"[{get_current_time()}] {Colors.RED}WebSocket error: {error}{Colors.END}")
            
        def on_close(ws, close_status_code, close_msg):
            print(f"[{get_current_time()}] {Colors.RED}WebSocket connection closed{Colors.END}")
            self.connected = False
            if self.should_reconnect:
                print(f"[{get_current_time()}] {Colors.YELLOW}Attempting to reconnect...{Colors.END}")
                time.sleep(5)  # Wait before reconnecting
                self.connect_to_websocket()
            
        def on_open(ws):
            print(f"[{get_current_time()}] {Colors.GREEN}WebSocket connection established{Colors.END}")
            self.connected = True

        def run_ws():
            while not self.connected and self.should_reconnect:
                print(f"[{get_current_time()}] {Colors.BLUE}Trying to connect...{Colors.END}")
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
                    print(f"[{get_current_time()}] {Colors.RED}WebSocket run_forever exception: {e}{Colors.END}")
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
            log_error(self, "waypoints.yaml file not found!")
            return None
        except yaml.YAMLError as e:
            log_error(self, f"Error parsing YAML file: {e}")
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
            log_error(self, "No waypoints data available!")
            return

        waypoints = []
        for name in waypoint_names:
            pose = self.get_waypoint_by_name(name)
            if pose:
                waypoints.append(pose)
            else:
                log_error(self, f"Waypoint {name} not found!")

        if not waypoints:
            log_error(self, "No valid waypoints to follow!")
            return
            
        goal_msg = FollowWaypoints.Goal()
        goal_msg.poses = waypoints
        
        log_info(self, f'waypoint names: {waypoints}')
        log_info(self, f'waiting for server...')

        self._action_client.wait_for_server()

        log_info(self, f'Connected to action server')
        
        self._send_goal_future = self._action_client.send_goal_async(goal_msg,
                feedback_callback=self.feedback_callback)
        
        self._send_goal_future.add_done_callback(self.goal_response_callback)

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        log_info(self, f'Currently at waypoint index: {feedback.current_waypoint}')

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            log_info(self, 'Goal rejected')
            return

        log_info(self, 'Goal accepted')
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        log_info(self, f'Result: {result.missed_waypoints}')
        try:
            if self.connected and self.ws:
                result_message = json.dumps({
                    'type': 'waypoint_result',
                    'order': self.current_order_id, 
                    'sequence': result.missed_waypoints.tolist() 
                })
                self.ws.send(result_message)
                log_info(self, 'Sent result back to WebSocket server')
            else:
                log_warn(self, 'WebSocket not available to send result')
        except Exception as e:
            log_error(self, f'Failed to send result over WebSocket: {e}')
            
    def destroy(self):
        self.should_reconnect = False
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
            print(f"[{get_current_time()}] {Colors.YELLOW}WebSocket thread joined the main: {Colors.END}")

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
        print(f"\n[{get_current_time()}] Shutting down gracefully...")
    finally:
        client.destroy()
        client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()
import json
import threading
import time
from datetime import datetime
from ament_index_python.packages import get_package_share_directory

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

def get_current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_info(node, message):
    node.get_logger().info(message)
    print(f"[{get_current_time()}] INFO: {message}")

def log_error(node, message):
    node.get_logger().error(message)
    print(f"[{get_current_time()}] ERROR: {message}")

def log_warn(node, message):
    node.get_logger().warn(message)
    print(f"[{get_current_time()}] WARN: {message}")

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
        self.connect_to_websocket("ws://localhost:48236")  # Change to your WebSocket server address
        self.current_order_id = None


    def connect_to_websocket(self, ws_url):
        """Establish WebSocket connection with retry until connected"""
        print(f"[{get_current_time()}] {Colors.BLUE}Attempting to connect to WebSocket at {ws_url}{Colors.END}")

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if 'tray' in data:
                    print(f"[{get_current_time()}] {Colors.GREEN}Received valid table order: {data['tray']}{Colors.END}")
                    self.current_order_id = data['order']
                    data['tray'].append("T0")
                    self.send_goal(data['tray'])
                    print(f"[{get_current_time()}] {Colors.CYAN}Waypoints to follow: {data['tray']}{Colors.END}")
                else:
                    print(f"[{get_current_time()}] {Colors.YELLOW}Received data missing 'order' field{Colors.END}")
            except json.JSONDecodeError:
                print(f"[{get_current_time()}] {Colors.RED}Failed to parse WebSocket message as JSON{Colors.END}")
            except Exception as e:
                print(f"[{get_current_time()}] {Colors.RED}Error processing message: {e}{Colors.END}")
            
        def on_error(ws, error):
            print(f"[{get_current_time()}] {Colors.RED}WebSocket error: {error}{Colors.END}")
            
        def on_close(ws, close_status_code, close_msg):
            print(f"[{get_current_time()}] {Colors.RED}WebSocket connection closed{Colors.END}")
            
        def on_open(ws):
            print(f"[{get_current_time()}] {Colors.GREEN}WebSocket connection established{Colors.END}")
            self.connected = True

        # Flag to track connection status
        self.connected = False

        def run_ws():
            while not self.connected:
                print(f"[{get_current_time()}] {Colors.BLUE}Trying to connect...{Colors.END}")

                self.ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                try:
                    self.ws.run_forever()
                except Exception as e:
                    print(f"[{get_current_time()}] {Colors.RED}WebSocket run_forever exception: {e}{Colors.END}")
                    pass
                if not self.connected:
                    print(f"[{get_current_time()}] {Colors.YELLOW}Retrying in 5 seconds...{Colors.END}")
                    time.sleep(5)

        self.ws_thread = threading.Thread(target=run_ws)
        self.ws_thread.daemon = True
        self.ws_thread.start()
    
    def load_waypoints(self):
        try:
            with open(config_path, 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            log_error(self, "waypoints.yaml file not found!")
            return None
        except yaml.YAMLError as e:
            log_error(self, f"Error parsing YAML file: {e}")
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
            log_error(self, "No waypoints data available!")
            return

        waypoints = []
        for name in waypoint_names:
            pose = self.get_waypoint_by_name(name)
            if pose:
                waypoints.append(pose)
            else:
                log_error(self, f"Waypoint {name} not found!")

        if not waypoints:
            log_error(self, "No valid waypoints to follow!")
            return
            
        goal_msg = FollowWaypoints.Goal()
        goal_msg.poses = waypoints
        
        log_info(self, f'waypoint names: {waypoints}')
        log_info(self, f'waiting for server...')

        self._action_client.wait_for_server()

        log_info(self, f'Connected to action server')
        
        self._send_goal_future = self._action_client.send_goal_async(goal_msg,
                feedback_callback=self.feedback_callback)
        
        self._send_goal_future.add_done_callback(self.goal_response_callback)

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        log_info(self, f'Currently at waypoint index: {feedback.current_waypoint}')

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            log_info(self, 'Goal rejected')
            return

        log_info(self, 'Goal accepted')
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        log_info(self, f'Result: {result.missed_waypoints}')
        try:
            result_message = json.dumps({
                'type': 'waypoint_result',
                'order': self.current_order_id, 
                'sequence': result.missed_waypoints.tolist() 
            })
            if self.ws:
                self.ws.send(result_message)
                log_info(self, 'Sent result back to WebSocket server')
            else:
                log_warn(self, 'WebSocket not available to send result')
        except Exception as e:
            log_error(self, f'Failed to send result over WebSocket: {e}')
            
    def destroy(self):
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
            print(f"[{get_current_time()}] {Colors.YELLOW}WebSocket thread join the main: {Colors.END}")

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
        print(f"\n[{get_current_time()}] Shutting down gracefully...")
    finally:
        client.destroy()
        client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()
