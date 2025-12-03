import socket
import ssl
import base64
import random
import string
import time
import threading
from datetime import datetime, timezone
from Crypto.Signature import eddsa
from Crypto.PublicKey import ECC
from pytradekit.utils.dynamic_types import BinanceWebSocket
from pytradekit.utils.time_handler import get_millisecond_str, get_datetime


class FixClient:
    def __init__(self, account_id, api_key, order_queue, strategy_id, portfolio_id):
        with open('/home/new_monitor/private_key.pem', "rb") as f:
            self.private_key = ECC.import_key(f.read())
        self._account_id = account_id
        self._strategy_id = strategy_id
        self._portfolio_id = portfolio_id
        self.api_key = api_key
        self.fix_host = 'fix-oe.binance.com'
        self.fix_port = 9000
        self.sock = None
        self.ssl_sock = None
        self.msg_seq_num = 1
        self.sender_comp_id = ''.join(random.sample(string.ascii_letters + string.digits, 8))
        self.target_comp_id = 'SPOT'
        self.buffer = ''
        self.heartbeat_interval = 30
        self.last_received_time = time.time()
        self.order_queue = order_queue

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        context = ssl.create_default_context()
        self.ssl_sock = context.wrap_socket(self.sock, server_hostname=self.fix_host)
        self.ssl_sock.connect((self.fix_host, self.fix_port))
        self.login()
        threading.Thread(target=self.receive_orders, daemon=True).start()
        threading.Thread(target=self.heartbeat_monitor, daemon=True).start()

    def login(self):
        """登录到 FIX 服务器"""
        utc_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S.%f')[:-3]
        payload = chr(1).join(['A', self.sender_comp_id, self.target_comp_id, str(self.msg_seq_num), utc_timestamp])
        signature = self.sign(payload)

        fix_message = (
            f"35=A|49={self.sender_comp_id}|56={self.target_comp_id}|34={self.msg_seq_num}|"
            f"52={utc_timestamp}|95={len(signature)}|96={signature}|98=0|108=10|141=Y|553={self.api_key}|25035=2|"
        )
        self.send_message(fix_message)

    def receive_orders(self):
        """持续接收订单消息"""
        while True:
            try:
                response = self.ssl_sock.recv(4096)
                if response:
                    self.last_received_time = time.time()
                    self.buffer += response.decode('ASCII')
                    self.process_buffer()
            except (ssl.SSLError, socket.error):
                self.reconnect()

    def process_buffer(self):
        while True:
            start_idx = self.buffer.find('8=FIX')
            if start_idx == -1:
                self.buffer = ''
                return

            end_idx = self.buffer.find('\x0110=', start_idx)
            if end_idx == -1:
                return

            end_idx += len('\x0110=') + 4
            if end_idx > len(self.buffer):
                return

            full_message = self.buffer[start_idx:end_idx]
            self.buffer = self.buffer[end_idx:]

            self.handle_message(full_message)

    def handle_message(self, message):
        data_list = message.split('\x01')
        if '35=8' in data_list:
            msg = {'order_trade': data_list, BinanceWebSocket.run_time_ms.value: get_millisecond_str(get_datetime())}
            msg[BinanceWebSocket.portfolio_id.value] = self._portfolio_id
            msg[BinanceWebSocket.strategy_id.value] = self._strategy_id
            msg[BinanceWebSocket.account_id.value] = self._account_id
            self.order_queue.put_nowait(msg)
        elif '35=1' in data_list:
            test_req_id = data_list[-3]
            if test_req_id:
                self.send_heartbeat_response(test_req_id)

    def heartbeat_monitor(self):
        """发送心跳并监控断线"""
        while True:
            if time.time() - self.last_received_time > self.heartbeat_interval:
                self.send_heartbeat()
            time.sleep(self.heartbeat_interval)

    def send_heartbeat(self):
        """发送心跳消息"""
        utc_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S.%f')[:-3]
        heartbeat_message = f"35=0|49={self.sender_comp_id}|56={self.target_comp_id}|34={self.msg_seq_num}|52={utc_timestamp}|"
        self.send_message(heartbeat_message)

    def send_heartbeat_response(self, test_req_id):
        """响应心跳请求"""
        utc_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S.%f')[:-3]
        response_message = (
            f"35=0|49={self.sender_comp_id}|56={self.target_comp_id}|34={self.msg_seq_num}|"
            f"52={utc_timestamp}|"f"{test_req_id}|"
        )
        self.send_message(response_message)

    def reconnect(self):
        """重新连接并重新登录"""
        try:
            self.ssl_sock.close()
            self.connect()
        except:
            time.sleep(5)  # 等待后重试

    def send_message(self, message):
        """发送消息并计算校验和"""
        fix_message = f"8=FIX.4.4|9={len(message)}|" + message
        checksum = sum(ord(char) for char in fix_message.replace('|', '\x01')) % 256
        full_message = fix_message + f"10={checksum:03}|"
        self.ssl_sock.sendall(full_message.replace('|', '\x01').encode('ASCII'))
        self.msg_seq_num += 1

    def sign(self, payload):
        """签名生成"""
        sign = eddsa.new(self.private_key, 'rfc8032')
        sign_data = sign.sign(payload.encode("ASCII"))
        return base64.b64encode(sign_data).decode('ASCII')

    def create_new_order(self, cl_ord_id, symbol, side, order_qty, ord_type, price=None, time_in_force=None):
        """构建并发送新订单消息"""
        utc_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S.%f')[:-3]
        payload = chr(1).join(
            [str(self.msg_seq_num), self.sender_comp_id, self.target_comp_id, str(self.msg_seq_num), utc_timestamp])
        signature = self.sign(payload)

        fix_message = (
            f"35=D|49={self.sender_comp_id}|56={self.target_comp_id}|34={self.msg_seq_num}|52={utc_timestamp}|"
            f"11={cl_ord_id}|38={order_qty}|40={ord_type}|54={side}|55={symbol}|")

        if price:
            fix_message += f"44={price}|"

        if time_in_force:
            fix_message += f"59={time_in_force}|"

        fix_message += f"95={len(signature)}|96={signature}|98=0|108=10|141=Y|553={self.api_key}|25035=2|"

        self.send_message(fix_message)
