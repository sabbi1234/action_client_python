import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from std_msgs.msg import Empty
from nav2_msgs.action import FollowWaypoints, NavigateToPose
from geometry_msgs.msg import PoseStamped
from tf_transformations import quaternion_from_euler
from std_msgs.msg import Int32
import yaml
import os
import websocket
import json
import threading
import time
import logging
import sys
from datetime import datetime
import queue
from ament_index_python.packages import get_package_share_directory
import math
class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[96m',  # Light cyan
        'INFO': '\033[92m',   # Light green
        'WARNING': '\033[93m',# Yellow
        'ERROR': '\033[91m',  # Red
        'CRITICAL': '\033[91m',
    }
    RESET = '\033[0m'

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
        self.data_subscriber = self.create_subscription(Int32,'waypoint_reached',self.waypoint_reached_callback,10)
        self.ws_message_queue = queue.Queue()
        self.waypoints_data = self.load_waypoints()
        self.ws = None
        self.ws_thread = None
        self.ws_url = "ws://localhost:48236"
        self.current_order_id = None
        self.should_reconnect = True
        self.connected = False
        self.current_goal_handle = None
        
        # Sequential navigation state
        self.pending_waypoint_sequence = None
        self.sequential_waypoints = ["I2", "I1"]  # Define the waypoints to visit first
        self.current_sequential_index = 0
        self.is_sequential_navigation = False
        
        # Post-completion navigation state
        self.post_completion_waypoints = ["Ic1", "Ic2", "T0"]  # Define waypoints after main sequence
        self.current_post_completion_index = 0
        self.is_post_completion_navigation = False
        
        # Start the WebSocket connection in a single thread
        self.start_websocket_thread()

    def start_websocket_thread(self):
        """Creates and starts a single WebSocket thread"""
        if self.ws_thread is None or not self.ws_thread.is_alive():
            self.ws_thread = threading.Thread(target=self.websocket_worker)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
    def websocket_worker(self):
        """Main WebSocket worker that handles connections and reconnections"""
        while self.should_reconnect:
            try:
                self.attempt_websocket_connection()
                
                # If we get here, the WebSocket connection has closed
                # Wait before attempting to reconnect
                if self.should_reconnect:
                    logger.warning("WebSocket closed. Waiting 5 seconds before reconnecting...")
                    time.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket worker error: {e}")
                if self.should_reconnect:
                    logger.warning("Waiting 5 seconds before reconnecting...")
                    time.sleep(5)
                    
        logger.info("WebSocket worker thread exiting")

    def attempt_websocket_connection(self):
        """Attempts to establish and maintain a single WebSocket connection"""
        logger.info(f"Attempting to connect to WebSocket at {self.ws_url}")
        
        # Define callbacks
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get('type') == "waypoint_order":
                    logger.info(f"Received valid table order: {data['order']}")
                    if 'tray' in data:
                        logger.info(f"Received valid table order: {data['tray']}")
                        self.current_order_id = data['order']
                        
                        # Store the main waypoint sequence for later execution (without T0)
                        main_sequence = data['tray'].copy()
                        # Don't append T0 here as it will be handled in post-completion
                        self.pending_waypoint_sequence = main_sequence
                        
                        # Start sequential navigation to I1 and I2 first
                        self.current_sequential_index = 0
                        self.is_sequential_navigation = True
                        self.navigate_to_next_sequential_waypoint()
                        
                        logger.info(f"Starting sequential navigation to {self.sequential_waypoints}, then will follow: {main_sequence}, then post-completion: {self.post_completion_waypoints}")
                    else:
                        logger.warning("Received data missing 'tray' field")
                elif data.get('type') == "waypoint_next":
                    if data['publish']:
                        self.publish_next_waypoint_signal()
                elif data.get('type') == "waypoint_cancel":
                    logger.info("Cancelling all waypoints...")
                    self.reset_sequential_navigation()
                    self.reset_post_completion_navigation()
                    self._cancel_timer = self.create_timer(0.1, self._cancel_timer_callback)
            except json.JSONDecodeError:
                logger.error("Failed to parse WebSocket message as JSON")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

        def on_error(ws, error):
            logger.error(f"WebSocket error: {error}")
            self.connected = False

        def on_close(ws, close_status_code, close_msg):
            logger.error(f"WebSocket connection closed (code: {close_status_code}, message: {close_msg})")
            self.connected = False
            # The reconnection is handled by the main websocket_worker loop

        def on_open(ws):
            logger.info("WebSocket connection established")
            self.connected = True
            # Process any messages that were queued while disconnected
            self.process_message_queue()

        # Create and run a new WebSocket connection
        try:
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            # This blocks until the connection is closed
            self.ws.run_forever()
        finally:
            self.connected = False

    def reset_sequential_navigation(self):
        """Reset sequential navigation state"""
        self.pending_waypoint_sequence = None
        self.current_sequential_index = 0
        self.is_sequential_navigation = False

    def reset_post_completion_navigation(self):
        """Reset post-completion navigation state"""
        self.current_post_completion_index = 0
        self.is_post_completion_navigation = False

    def navigate_to_next_sequential_waypoint(self):
        """Navigate to the next waypoint in the sequential list"""
        if self.current_sequential_index >= len(self.sequential_waypoints):
            # Finished with sequential waypoints, now execute the main sequence
            logger.info("Completed sequential navigation. Starting main waypoint sequence.")
            self.is_sequential_navigation = False
            if self.pending_waypoint_sequence:
                time.sleep(2)
                self.send_goal(self.pending_waypoint_sequence)
                self.pending_waypoint_sequence = None
            return

        waypoint_name = self.sequential_waypoints[self.current_sequential_index]
        pose = self.get_waypoint_by_name(waypoint_name)
        
        if pose is None:
            logger.error(f"Sequential waypoint {waypoint_name} not found in waypoints file.")
            # Skip to next waypoint
            self.current_sequential_index += 1
            self.navigate_to_next_sequential_waypoint()
            return

        goal = NavigateToPose.Goal()
        goal.pose = pose

        self._pose_action_client.wait_for_server()
        logger.info(f"Navigating to sequential waypoint: {waypoint_name}")
        time.sleep(2)
        nav_future = self._pose_action_client.send_goal_async(goal)
        nav_future.add_done_callback(self.sequential_goal_response_callback)

    def sequential_goal_response_callback(self, future):
        """Handle response for sequential navigation goals"""
        goal_handle = future.result()
        if not goal_handle.accepted:
            logger.warning(f"Sequential navigation goal rejected for waypoint {self.sequential_waypoints[self.current_sequential_index]}")
            # Move to next waypoint even if this one was rejected
            self.current_sequential_index += 1
            self.navigate_to_next_sequential_waypoint()
            return

        logger.info(f"Sequential navigation goal accepted for waypoint {self.sequential_waypoints[self.current_sequential_index]}")
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self.sequential_navigation_result_callback)

    def sequential_navigation_result_callback(self, future):
        """Handle result for sequential navigation"""
        try:
            result = future.result()
            status = result.status
            waypoint_name = self.sequential_waypoints[self.current_sequential_index]
            
            if status == 4:  # SUCCEEDED
                logger.info(f"Successfully reached sequential waypoint: {waypoint_name}")
            elif status == 5:  # CANCELED
                logger.warning(f"Sequential navigation to {waypoint_name} was canceled.")
            elif status == 6:  # ABORTED
                logger.error(f"Sequential navigation to {waypoint_name} was aborted.")
            else:
                logger.warning(f"Sequential navigation to {waypoint_name} ended with status: {status}")

            # Move to next sequential waypoint
            self.current_sequential_index += 1
            self.navigate_to_next_sequential_waypoint()
            
        except Exception as e:
            logger.error(f"Error processing sequential navigation result: {e}")
            # Continue to next waypoint despite error
            self.current_sequential_index += 1
            self.navigate_to_next_sequential_waypoint()

    def navigate_to_next_post_completion_waypoint(self):
        """Navigate to the next waypoint in the post-completion list"""
        if self.current_post_completion_index >= len(self.post_completion_waypoints):
            # Finished with all post-completion waypoints
            logger.info("Completed all post-completion navigation.")
            self.is_post_completion_navigation = False
            
            # Send final completion message
            self.send_final_completion_message()
            return

        waypoint_name = self.post_completion_waypoints[self.current_post_completion_index]
        pose = self.get_waypoint_by_name(waypoint_name)
        
        if pose is None:
            logger.error(f"Post-completion waypoint {waypoint_name} not found in waypoints file.")
            # Skip to next waypoint
            self.current_post_completion_index += 1
            self.navigate_to_next_post_completion_waypoint()
            return

        goal = NavigateToPose.Goal()
        goal.pose = pose

        self._pose_action_client.wait_for_server()
        logger.info(f"Navigating to post-completion waypoint: {waypoint_name}")

        nav_future = self._pose_action_client.send_goal_async(goal)
        nav_future.add_done_callback(self.post_completion_goal_response_callback)

    def post_completion_goal_response_callback(self, future):
        """Handle response for post-completion navigation goals"""
        goal_handle = future.result()
        if not goal_handle.accepted:
            logger.warning(f"Post-completion navigation goal rejected for waypoint {self.post_completion_waypoints[self.current_post_completion_index]}")
            # Move to next waypoint even if this one was rejected
            self.current_post_completion_index += 1
            self.navigate_to_next_post_completion_waypoint()
            return

        logger.info(f"Post-completion navigation goal accepted for waypoint {self.post_completion_waypoints[self.current_post_completion_index]}")
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self.post_completion_navigation_result_callback)

    def post_completion_navigation_result_callback(self, future):
        """Handle result for post-completion navigation"""
        try:
            result = future.result()
            status = result.status
            waypoint_name = self.post_completion_waypoints[self.current_post_completion_index]
            
            if status == 4:  # SUCCEEDED
                logger.info(f"Successfully reached post-completion waypoint: {waypoint_name}")
                
                # If this is the final T0 waypoint, send completion data to server
                if waypoint_name == "T0":
                    self.send_t0_completion_message(status)
                    
            elif status == 5:  # CANCELED
                logger.warning(f"Post-completion navigation to {waypoint_name} was canceled.")
                if waypoint_name == "T0":
                    self.send_t0_completion_message(status)
            elif status == 6:  # ABORTED
                logger.error(f"Post-completion navigation to {waypoint_name} was aborted.")
                if waypoint_name == "T0":
                    self.send_t0_completion_message(status)
            else:
                logger.warning(f"Post-completion navigation to {waypoint_name} ended with status: {status}")
                if waypoint_name == "T0":
                    self.send_t0_completion_message(status)

            # Move to next post-completion waypoint
            self.current_post_completion_index += 1
            self.navigate_to_next_post_completion_waypoint()
            
        except Exception as e:
            logger.error(f"Error processing post-completion navigation result: {e}")
            # Continue to next waypoint despite error
            self.current_post_completion_index += 1
            self.navigate_to_next_post_completion_waypoint()

    def send_t0_completion_message(self, status):
        """Send completion message when T0 is reached"""
        # Convert status code to readable string
        status_sent = 'UNKNOWN'
        if status == 4:  # SUCCEEDED
            status_sent = 'SUCCEEDED'
        elif status == 5:  # CANCELED
            status_sent = 'CANCELED'
        elif status == 6:  # ABORTED
            status_sent = 'ABORTED'
        else:
            status_sent = f'STATUS_{status}'

        t0_completion_msg = {
            'type': 'return_to_chef',
            'order': self.current_order_id,
            'status': status_sent,
            'message': 'Completed full navigation sequence and returned to T0'
        }
        
        if self.connected and self.ws:
            self.ws.send(json.dumps(t0_completion_msg))
            logger.info(f"Sent T0 completion message to WebSocket server with status: {status_sent}")
        else:
            # Queue the message if WebSocket isn't available
            self.ws_message_queue.put(t0_completion_msg)
            logger.info(f"Queued T0 completion message for later sending with status: {status_sent}")

    def send_final_completion_message(self):
        """Send final completion message after all navigation is done"""
        completion_msg = {
            'type': 'final_completion',
            'order': self.current_order_id,
            'status': 'COMPLETED',
            'message': 'All waypoints completed successfully'
        }
        
        if self.connected and self.ws:
            self.ws.send(json.dumps(completion_msg))
            logger.info("Sent final completion message to WebSocket server")
        else:
            # Queue the message if WebSocket isn't available
            self.ws_message_queue.put(completion_msg)
            logger.info("Queued final completion message for later sending")
            
    def waypoint_reached_callback(self, msg):
        try:
            ws_message = {
                'type': 'current_waypoint',
                'order': self.current_order_id,
                'content': msg.data 
            }
            self.ws_message_queue.put(ws_message)  # Thread-safe operation
            
            # Try to send the message right away if connected
            self.process_message_queue()
        except Exception as e:
            logger.error(f"Failed to queue WS message: {e}")
    
    def process_message_queue(self):
        """Process and send any messages in the queue if connected"""
        if self.connected and self.ws:
            try:
                while not self.ws_message_queue.empty():
                    message = self.ws_message_queue.get_nowait()
                    self.ws.send(json.dumps(message))
                    logger.info(f"Sent message from queue: {message['type']}")
            except Exception as e:
                logger.error(f"Error sending queued message: {e}")

    def publish_next_waypoint_signal(self):
        msg = Empty()
        self.next_waypoint_publisher.publish(msg)
        logger.info("Published Empty to input_at_waypoint/input topic")

    def cancel_waypoints(self):
        try:
            # Reset sequential navigation state
            self.reset_sequential_navigation()
            self.reset_post_completion_navigation()
            
            if self.current_goal_handle is not None:
                logger.info("Cancelling current waypoint navigation...")
                self.publish_next_waypoint_signal()
                time.sleep(0.1)
                cancel_future = self.current_goal_handle.cancel_goal_async()
                cancel_future.add_done_callback(self._cancel_waypoints_complete)
            else:
                logger.warning("No active goal to cancel. Proceeding to cancel pose.")
                # self.go_to_cancel_pose() 
        except Exception as e:
            logger.error(f"Error during cancellation: {e}")
            # self.go_to_cancel_pose() 

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
        # self.go_to_cancel_pose() 

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
                result_get_result_future = result.get_result_async()
                
                def on_nav_result(future):
                    try:
                        nav_result = future.result().result
                        status = future.result().status  # goal status (int enum)
                        status_sent = 'UNKNOWN'
                        if status == 4:  # SUCCEEDED
                            logger.info("Reached cancel pose.")
                            status_sent = 'SUCCEEDED'
                        elif status == 5:  # CANCELED
                            logger.warning("Navigation goal was canceled.")
                            status_sent = 'CANCELED'
                        elif status == 6:  # ABORTED
                            logger.error("Navigation goal was aborted.")
                            status_sent = 'ABORTED'
                        else:
                            logger.warning(f"Navigation goal ended with unknown status: {status}")

                        completion_msg = {
                            'type': 'return_to_chef',
                            'order': self.current_order_id,
                            'status': status_sent
                        }
                        
                        if self.connected and self.ws:
                            self.ws.send(json.dumps(completion_msg))
                            logger.info("Sent cancel completion message to WebSocket server")
                        else:
                            # Queue the message if WebSocket isn't available
                            self.ws_message_queue.put(completion_msg)
                            logger.info("Queued cancel completion message for later sending")
                            
                    except Exception as e:
                        logger.error(f"Error processing navigation result: {e}")
                            
                result_get_result_future.add_done_callback(on_nav_result)
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
            'waypoints_office.yaml'
        )
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
        logger.info('Waiting for the action server....')
        self._waypoint_action_client.wait_for_server()
        logger.info('Connected to action server')

        self._send_goal_future = self._waypoint_action_client.send_goal_async(
            goal_msg,
            feedback_callback=self.feedback_callback
        )
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
        self.current_goal_handle = goal_handle
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        logger.info(f'Main waypoint sequence result: {result.missed_waypoints}')
        
        try:
            # Send the main sequence result to WebSocket
            if self.connected and self.ws:
                result_message = json.dumps({
                    'type': 'waypoint_result',
                    'order': self.current_order_id,
                    'sequence': result.missed_waypoints.tolist()
                })
                self.ws.send(result_message)
                logger.info('Sent main sequence result back to WebSocket server')
            else:
                logger.warning('WebSocket not available to send result')
                ws_message = {
                    'type': 'waypoint_result',
                    'order': self.current_order_id,
                    'sequence': result.missed_waypoints.tolist()
                }
                self.ws_message_queue.put(ws_message)
                logger.info('Queued result message for later sending')
                
            # Start post-completion navigation
            logger.info("Main waypoint sequence completed. Starting post-completion navigation.")
            self.current_post_completion_index = 0
            self.is_post_completion_navigation = True
            self.navigate_to_next_post_completion_waypoint()
            
        except Exception as e:
            logger.error(f'Failed to send result over WebSocket: {e}')
            # Still proceed with post-completion navigation even if WebSocket fails
            logger.info("Starting post-completion navigation despite WebSocket error.")
            self.current_post_completion_index = 0
            self.is_post_completion_navigation = True
            self.navigate_to_next_post_completion_waypoint()

    def destroy(self):
        logger.info("Shutting down WebSocket connection...")
        self.should_reconnect = False
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            logger.info("Waiting for WebSocket thread to terminate...")
            self.ws_thread.join(timeout=2.0)
            logger.info("WebSocket thread terminated")

def create_pose(x, y, theta):
    pose = PoseStamped()
    pose.header.frame_id = 'map'
    pose.pose.position.x = x
    pose.pose.position.y = y
    pose.pose.position.z = 0.0
    theta_radians= math.radians(theta)
    q = quaternion_from_euler(0, 0, theta_radians)
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
    except Exception as e:
        logger.info("External shutdown detected. Cleaning up...")
    except KeyboardInterrupt:
        logger.info("\nShutting down gracefully...")
    finally:
        client.destroy()
        client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()
