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


    def connect_to_websocket(self, ws_url):
        """Establish WebSocket connection with retry until connected"""
        self.get_logger().info(f'Attempting to connect to WebSocket at {ws_url}')

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if 'tray' in data:
                    print(f"{Colors.GREEN}Received valid table order: {data['tray']}{Colors.END}")
                    self.send_goal(data['tray'])
                    print(data['tray'])
                else:
                    print(f"{Colors.YELLOW}Received data missing 'order' field{Colors.END}")
            except json.JSONDecodeError:
                print(f"{Colors.RED}Failed to parse WebSocket message as JSON{Colors.END}")
            except Exception as e:
                print(f"{Colors.RED}Error processing message: {e}{Colors.END}")
            
        def on_error(ws, error):
            print(f"{Colors.RED}WebSocket error: {error}{Colors.END}")
            # self.get_logger().error(f"WebSocket error: {error}")
        def on_close(ws, close_status_code, close_msg):
            # self.get_logger().info("WebSocket connection closed")
            print(f"{Colors.RED}WebSocket connection closed{Colors.END}")
        def on_open(ws):
            # self.get_logger().info("WebSocket connection established")
            print(f"{Colors.GREEN}WebSocket connection established{Colors.END}")
            self.connected = True

        # Flag to track connection status
        self.connected = False

        def run_ws():
            while not self.connected:
                # self.get_logger().info("Trying to connect...")
                print(f"{Colors.BLUE}Trying to connect...{Colors.END}")

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
                    # self.get_logger().error(f"WebSocket run_forever exception: {e}")
                    print(f"{Colors.RED}WebSocket run_forever exception: {e}{Colors.END}")
                    pass
                if not self.connected:
                    # self.get_logger().info("Retrying in 5 seconds...")
                    print(f"{Colors.YELLOW}Retrying in 5 seconds...{Colors.END}")
                    time.sleep(5)

        self.ws_thread = threading.Thread(target=run_ws)
        self.ws_thread.daemon = True
        self.ws_thread.start()
    
    def load_waypoints(self):
        try:
            with open(config_path, 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            self.get_logger().error("waypoints.yaml file not found!")
            return None
        except yaml.YAMLError as e:
            self.get_logger().error(f"Error parsing YAML file: {e}")
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
            self.get_logger().error("No waypoints data available!")
            return

        waypoints = []
        for name in waypoint_names:
            pose = self.get_waypoint_by_name(name)
            if pose:
                waypoints.append(pose)
            else:
                self.get_logger().error(f"Waypoint {name} not found!")

        if not waypoints:
            self.get_logger().error("No valid waypoints to follow!")
            return
        goal_msg = FollowWaypoints.Goal()
        goal_msg.poses = waypoints
        self.get_logger().info(f'waypoint names: {waypoints}')
        self.get_logger().info(f'waing for server ....')

        self._action_client.wait_for_server()

        self.get_logger().info(f'Connected to action server')
        

        self._send_goal_future = self._action_client.send_goal_async(goal_msg,
                feedback_callback=self.feedback_callback)
        
        self._send_goal_future.add_done_callback(self.goal_response_callback)

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.get_logger().info(f'Currently at waypoint index: {feedback.current_waypoint}')

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('Goal rejected')
            return

        self.get_logger().info('Goal accepted')
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        self.get_logger().info(f'Result: {result}')
        rclpy.shutdown()
    def destroy(self):
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
            print(f"{Colors.YELLOW}WebSocket thread join the main: {Colors.END}")

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

    # waypoint_names = ['T1', 'T2', 'T3']
    # client.send_goal(waypoint_names)
    try:
        # Keep the node running to process WebSocket messages and ROS callbacks
        rclpy.spin(client)
    except KeyboardInterrupt:
        client.destroy()
        client.destroy_node()
        print("\nShutting down gracefully...")
    finally:
        client.destroy()
        client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()
