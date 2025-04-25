from setuptools import find_packages, setup
from glob import glob
import os
package_name = 'action_client_python'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='greenquest',
    maintainer_email='sabbi.chakri@gmail.com',
    description='TODO: Package description',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'fibonacci_action_client = action_client_python.action_client:main',
            'fibonacci_action_client_socket = action_client_python.action_client_websocket:main',
            'action_client_waypoint = action_client_python.action_waypoint:main',
        ],
    },
)
