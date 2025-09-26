import socket
import struct
import subprocess
import threading
import time
import random
import os
import logging
from enum import Enum
from typing import Optional, List, Tuple

import av

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TouchAction(Enum):
    """触摸动作枚举"""
    DOWN = 0
    UP = 1
    MOVE = 2


class DeviceController:
    """设备控制器类，用于通过scrcpy控制Android设备"""

    def __init__(self,
                 serial: Optional[str] = None,
                 port: int = 27188,
                 push_server: bool = True,
                 server_dir: str = '.',
                 max_size: int = 800,
                 video_bit_rate: int = 2000000,
                 max_fps: int = 15) -> None:
        """
        初始化设备控制器

        Args:
            serial: 设备序列号，None表示使用默认设备
            port: 监听端口
            push_server: 是否推送scrcpy服务器到设备
            server_dir: scrcpy服务器文件目录
            max_size: 最大分辨率尺寸
            video_bit_rate: 视频比特率
            max_fps: 最大帧率
        """
        self.serial = serial
        self.port = port
        self.session_id = format(random.randint(0, 0x7FFFFFFF), '08x')
        self.device_width = 0
        self.device_height = 0
        self.collector_running = False

        # ADB命令前缀
        self.adb_cmd = ['adb']
        if serial:
            self.adb_cmd.extend(['-s', serial])

        # 初始化连接
        self._setup_scrcpy_server(push_server, server_dir, max_size, video_bit_rate, max_fps)
        self._setup_sockets()
        self._get_device_info()
        self._start_collectors()

        logger.info(f"Device controller initialized for device: {serial or 'default'}")
        logger.info(f"Device size: {self.device_width}x{self.device_height}")

    def _setup_scrcpy_server(self, push_server: bool, server_dir: str,
                             max_size: int, video_bit_rate: int, max_fps: int) -> None:
        """设置scrcpy服务器"""
        # 查找服务器文件
        server_files = [f for f in os.listdir(server_dir) if f.startswith('scrcpy-server-v')]
        if not server_files:
            raise FileNotFoundError(f"No scrcpy server found in {server_dir}")

        server_file = os.path.join(server_dir, server_files[0])
        server_version = server_file.split('v')[-1].replace('.jar', '')

        # 推送服务器到设备
        if push_server:
            logger.info("Pushing scrcpy server to device...")
            result = subprocess.run(
                [*self.adb_cmd, 'push', server_file, '/data/local/tmp/scrcpy-server.jar'],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to push scrcpy server: {result.stderr}")

        # 设置端口转发
        logger.info("Setting up port forwarding...")
        result = subprocess.run(
            [*self.adb_cmd, 'reverse', f'localabstract:scrcpy_{self.session_id}', f'tcp:{self.port}'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to set up port forwarding: {result.stderr}")

        # 启动服务器进程
        logger.info("Starting scrcpy server...")
        self.server_process = subprocess.Popen([
            *self.adb_cmd,
            'shell',
            'CLASSPATH=/data/local/tmp/scrcpy-server.jar',
            'app_process',
            '/',
            'com.genymobile.scrcpy.Server',
            server_version,
            f'scid={self.session_id}',
            'log_level=info',
            'audio=false',
            'clipboard_autosync=false',
            'video_codec=h264',
            'video_encoder=OMX.google.h264.encoder',
            f'max_size={max_size}',
            f'video_bit_rate={video_bit_rate}',
            f'max_fps={max_fps}',
            'display_id=0',
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _setup_sockets(self) -> None:
        """设置socket连接"""
        # 创建监听socket
        self.listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener_socket.bind(('localhost', self.port))
        self.listener_socket.listen(2)  # 需要接受两个连接

        # 接受视频和控制socket连接
        logger.info("Waiting for video socket connection...")
        self.video_socket, _ = self.listener_socket.accept()
        logger.info("Waiting for control socket connection...")
        self.control_socket, _ = self.listener_socket.accept()

        # 清理端口转发
        subprocess.run(
            [*self.adb_cmd, 'reverse', '--remove', f'localabstract:scrcpy_{self.session_id}'],
            capture_output=True
        )
        self.listener_socket.close()

    def _get_device_info(self) -> None:
        """获取设备信息"""
        # 读取设备名称
        device_name = self.video_socket.recv(64)
        logger.debug(f"Device name: {device_name.decode(errors='ignore')}")

        # 读取编解码器信息
        codec_id = self.video_socket.recv(4).decode()
        logger.debug(f"Codec ID: {codec_id}")

        # 读取设备尺寸
        self.device_width = int.from_bytes(self.video_socket.recv(4), 'big')
        self.device_height = int.from_bytes(self.video_socket.recv(4), 'big')

        # 如果无法获取尺寸，使用备用方法
        if self.device_width == 0 or self.device_height == 0:
            logger.warning("Failed to get device size from socket, using ADB fallback")
            self._get_device_size_via_adb()

    def _get_device_size_via_adb(self) -> None:
        """通过ADB获取设备尺寸"""
        try:
            result = subprocess.run(
                [*self.adb_cmd, 'shell', 'wm', 'size'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                if 'Physical size:' in output:
                    size_str = output.split('Physical size: ')[1]
                    width, height = map(int, size_str.split('x'))
                    self.device_width = width
                    self.device_height = height
                    logger.info(f"Fallback device size: {self.device_width}x{self.device_height}")
                else:
                    raise RuntimeError("Unexpected output from 'wm size' command")
            else:
                raise RuntimeError(f"ADB command failed: {result.stderr}")
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.error(f"Failed to get device size via ADB: {e}")
            # 设置默认尺寸
            self.device_width = 1080
            self.device_height = 1920
            logger.info(f"Using default device size: {self.device_width}x{self.device_height}")

    def _start_collectors(self) -> None:
        """启动数据收集线程"""
        self.collector_running = True

        # 视频流解码器线程
        self.streaming_collector = threading.Thread(
            target=self._streaming_decoder,
            daemon=True
        )
        self.streaming_collector.start()

        # 控制消息接收器线程
        self.control_collector = threading.Thread(
            target=self._ctrlmsg_receiver,
            daemon=True
        )
        self.control_collector.start()

    def _streaming_decoder(self) -> None:
        """解码视频流数据"""
        codec = av.CodecContext.create('h264', 'r')
        try:
            while self.collector_running:
                # 读取时间戳（未使用）
                _pts = self.video_socket.recv(8)

                # 读取数据大小
                size_data = self.video_socket.recv(4)
                if not size_data:
                    break

                size = int.from_bytes(size_data, 'big')

                # 读取视频数据
                video_data = self.video_socket.recv(size)
                if not video_data:
                    break

                # 解码视频帧
                packets = codec.parse(video_data)
                for packet in packets:
                    frames = codec.decode(packet)
                    for frame in frames:
                        # 更新设备尺寸信息
                        if (self.device_width != frame.width or
                                self.device_height != frame.height):
                            logger.info(
                                f"Device size updated: {self.device_width}x{self.device_height} "
                                f"-> {frame.width}x{frame.height}"
                            )
                            self.device_width = frame.width
                            self.device_height = frame.height
                        break
        except Exception as e:
            logger.error(f"Video streaming decoder error: {e}")
            self.collector_running = False

    def _ctrlmsg_receiver(self) -> None:
        """接收控制消息"""
        try:
            while self.collector_running:
                # 读取消息类型
                msg_type = self.control_socket.recv(1)
                if not msg_type:
                    break

                # 读取消息大小
                size_data = self.control_socket.recv(4)
                if not size_data:
                    break

                size = int.from_bytes(size_data, 'big')

                # 读取消息内容
                if size > 0:
                    self.control_socket.recv(size)
        except Exception as e:
            logger.error(f"Control message receiver error: {e}")
            self.collector_running = False

    def touch(self, x: int, y: int, action: TouchAction, pointer_id: int = 1000) -> None:
        """
        发送触摸事件

        Args:
            x: 触摸点的X坐标
            y: 触摸点的Y坐标
            action: 触摸动作
            pointer_id: 指针ID，默认为1000
        """
        # 添加小延迟防止ADB过载
        time.sleep(0.001)

        # 确保坐标在设备范围内
        x = max(0, min(x, self.device_width - 1))
        y = max(0, min(y, self.device_height - 1))

        try:
            # 构建触摸事件数据包
            data = struct.pack(
                '!bbQiiHHHII',
                2,  # SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT
                action.value,
                pointer_id,
                x,
                y,
                self.device_width,
                self.device_height,
                0xFFFF,  # pressure
                1,  # action_button: AMOTION_EVENT_BUTTON_PRIMARY
                1,  # buttons: AMOTION_EVENT_BUTTON_PRIMARY
            )
            self.control_socket.send(data)

        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error(f"Socket connection lost: {e}")
            self.collector_running = False
        except Exception as e:
            logger.error(f"Failed to send touch event: {e}")

    def tap(self, x: int, y: int, pointer_id: int = 1000, delay: float = 0.1) -> None:
        """
        执行点击操作

        Args:
            x: 点击点的X坐标
            y: 点击点的Y坐标
            pointer_id: 指针ID，默认为1000
            delay: 按下和抬起之间的延迟，默认为0.1秒
        """
        self.touch(x, y, TouchAction.DOWN, pointer_id)
        time.sleep(delay)
        self.touch(x, y, TouchAction.UP, pointer_id)

    def swipe(self, start_x: int, start_y: int, end_x: int, end_y: int,
              duration: float = 0.5, steps: int = 10) -> None:
        """
        执行滑动操作

        Args:
            start_x: 起始点X坐标
            start_y: 起始点Y坐标
            end_x: 结束点X坐标
            end_y: 结束点Y坐标
            duration: 滑动持续时间，默认为0.5秒
            steps: 滑动步数，默认为10
        """
        # 按下起始点
        self.touch(start_x, start_y, TouchAction.DOWN)

        # 计算中间点
        step_delay = duration / steps
        for i in range(1, steps):
            ratio = i / steps
            x = int(start_x + (end_x - start_x) * ratio)
            y = int(start_y + (end_y - start_y) * ratio)
            self.touch(x, y, TouchAction.MOVE)
            time.sleep(step_delay)

        # 抬起结束点
        self.touch(end_x, end_y, TouchAction.UP)

    def close(self) -> None:
        """关闭连接并清理资源"""
        self.collector_running = False

        # 关闭socket连接
        for sock in [self.video_socket, self.control_socket]:
            try:
                sock.close()
            except:
                pass

        # 终止服务器进程
        try:
            self.server_process.terminate()
            self.server_process.wait(timeout=5)
        except:
            try:
                self.server_process.kill()
            except:
                pass

        logger.info("Device controller closed")

    def __del__(self) -> None:
        """析构函数，确保资源被清理"""
        self.close()

    @staticmethod
    def get_devices() -> List[str]:
        """
        获取已连接的设备列表

        Returns:
            设备序列号列表
        """
        try:
            result = subprocess.run(
                ['adb', 'devices'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return []

            devices = []
            for line in result.stdout.splitlines():
                if line.strip() and not line.startswith('List of devices attached'):
                    parts = line.split('\t')
                    if len(parts) >= 2 and parts[1] == 'device':
                        devices.append(parts[0])
            return devices
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.error(f"Failed to get devices: {e}")
            return []


if __name__ == '__main__':
    # 示例用法
    devices = DeviceController.get_devices()
    print(f"Connected devices: {devices}")

    if devices:
        # 使用第一个设备
        controller = DeviceController(serial=devices[0])

        try:
            # 点击屏幕中心
            center_x = controller.device_width // 2
            center_y = controller.device_height // 2
            controller.tap(center_x, center_y)

            # 等待一段时间
            time.sleep(2)

        finally:
            controller.close()
    else:
        print("No devices found")