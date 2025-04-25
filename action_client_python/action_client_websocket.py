#!/usr/bin/env python3
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from custom_my_interfaces.action import Fibonacci
import websocket
import json
import threading
import time
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
class FibonacciActionClient(Node):
    def __init__(self):
        super().__init__('fibonacci_action_client')
        self._action_client = ActionClient(self, Fibonacci, 'fibonacci')
        
        # WebSocket configuration
        self.ws = None
        self.ws_thread = None
        self.connect_to_websocket("ws://localhost:48236")  # Change to your WebSocket server address
        
    def connect_to_websocket(self, ws_url):
        """Establish WebSocket connection with retry until connected"""
        self.get_logger().info(f'Attempting to connect to WebSocket at {ws_url}')

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if 'order' in data:
                    self.send_goal(int(data['order']))
                    print(f"{Colors.GREEN}Received valid order: {data['order']}{Colors.END}")
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
    
    def send_goal(self, order):
        self.get_logger().info('Waiting for action server...')
        self._action_client.wait_for_server()
        
        goal_msg = Fibonacci.Goal()
        goal_msg.order = order
        
        self.get_logger().info(f'Sending goal: Fibonacci sequence of order {order}')
        self._send_goal_future = self._action_client.send_goal_async(
            goal_msg,
            feedback_callback=self.feedback_callback
        )
        self._send_goal_future.add_done_callback(self.goal_response_callback)
        
    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('Goal rejected :(')
            return
            
        self.get_logger().info('Goal accepted :)')
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)
        
    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.get_logger().info(f'Received feedback: {feedback.partial_sequence}')
        
    def get_result_callback(self, future):
        result = future.result().result
        self.get_logger().info(f'Final result: {result.sequence}')
        try:
            result_message = json.dumps({
                'type': 'fibonacci_result',
                'sequence': result.sequence.tolist() 
            })
            if self.ws:
                self.ws.send(result_message)
                self.get_logger().info('Sent result back to WebSocket server')
            else:
                self.get_logger().warn('WebSocket not available to send result')
        except Exception as e:
            self.get_logger().error(f'Failed to send result over WebSocket: {e}')
            
    def destroy(self):
        """Clean shutdown of the node and WebSocket connection"""
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
        # self.get_logger().info("WebSocket closed and thread joined.")

        

def main(args=None):
    rclpy.init(args=args)
    
    # Create the action client with WebSocket listener
    action_client = FibonacciActionClient()
    
    try:
        # Keep the node running to process WebSocket messages and ROS callbacks
        rclpy.spin(action_client)
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    finally:
        action_client.destroy()
        action_client.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()