#
#
# def run(config, onehour=False, running_mode=None):
#     slack_token = encrypt_decrypt(config.private['trading_system_token'], 'decrypt')
#     channel_id = encrypt_decrypt(config.private['jwj_system_health_webhook'], 'decrypt')
#     chat_app = ChatApp(channel_id=channel_id,
#                        token=slack_token,
#                        logger=logging)
#     disk_infos, disk_alerts = get_disk_usage()
#     cpu_percent, cpu_threshold, cpu_alert = get_total_cpu_usage()
#     memory_percent, memory_threshold, memory_alert = get_memory_usage()
#
#     alerts = disk_alerts
#     if cpu_alert:
#         alerts.append(f"CPU total utilization rate is too high：current {cpu_percent}%，threshold {cpu_threshold}%")
#     if memory_alert:
#         alerts.append(f"Excessive memory usage：current {memory_percent}%，threshold {memory_threshold}%")
#
#     # 拼接 markdown 磁盘表格
#     disk_df = pd.DataFrame(disk_infos)
#     disk_markdown = disk_df.to_markdown(index=False)
#
#     description = []
#     if disk_alerts:
#         alert_text = "\n".join(
#             [f"{a}\n" for a in
#              alerts])
#         description.append(f"```\n-----disk info-----:\n{disk_markdown}\n```\n\n{alert_text}")
#
#         send_ad(chat_app, df=disk_df, description=description, running_mode=running_mode)
#     if onehour:
#         description = [f"CPU Total utilization rate：current {cpu_percent}%，threshold {cpu_threshold}%\n"
#                        f"Memory usage rate：current {memory_percent}%，threshold {memory_threshold}%\n"]
#         send_ad(chat_app, df=disk_df, description=description, running_mode=running_mode)

import logging
import time
import psutil
import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from pytradekit.utils.config_agent import ConfigAgent
from pytradekit.utils.tools import find_project_root, encrypt_decrypt
from pytradekit.utils.dynamic_types import RunningMode
from pytradekit.notifiers.slack_app.chat_app import ChatApp


class SystemMonitor:
    def __init__(self, config, running_mode):
        self.config = config
        self.running_mode = running_mode
        self.chat_app = ChatApp(
            channel_id=encrypt_decrypt(config.private['jwj_system_health_webhook'], 'decrypt'),
            token=encrypt_decrypt(config.private['trading_system_token'], 'decrypt'),
            logger=logging
        )
        self.max_cpu = 0
        self.max_memory = 0
        self.used = 0
        self.percent = 0
        self.disk_infos = []
        self.used_hbdata = 0
        self.percent_hbdata = 0
        self.disk_infos_hbdata = []

    def get_memory_usage(self, threshold=80):
        mem = psutil.virtual_memory()
        percent = round(mem.percent, 2)
        alert = percent > threshold
        self.max_memory = max(self.max_memory, percent)
        return percent, threshold, alert

    def get_disk_usage(self, threshold=80, paths=None):
        if paths is None:
            paths = ['/']
        disk_infos = []
        alerts = []
        for path in paths:
            usage = psutil.disk_usage(path)
            percent = round(usage.percent, 2)
            total = round(usage.total / (1024 ** 3), 1)
            used = round(usage.used / (1024 ** 3), 1)
            free = round(usage.free / (1024 ** 3), 1)
            disk_infos.append({
                'path': path,
                'total': f"{total}G",
                'used': f"{used}G",
                'free': f"{free}G",
                'disk_percent': percent
            })
            if percent > threshold:
                alerts.append(f"{path} The disk usage is too high：current {percent}%，threshold {threshold}%")
            self.used =  max(self.used, used)
            self.percent = max(self.percent, percent)
            self.disk_infos=[{
                'path': path,
                'total': f"{total}G",
                'used': f"{self.used}G",
                'free': f"{round(total-self.used,1)}G",
                'disk_percent': self.percent
            }]
        return disk_infos, alerts

    def get_disk_usage_hbdata(self, threshold=80,):
        paths = ['/hbdata2']
        disk_infos = []
        alerts = []
        for path in paths:
            usage = psutil.disk_usage(path)
            percent = round(usage.percent, 2)
            total = round(usage.total / (1024 ** 3), 1)
            used = round(usage.used / (1024 ** 3), 1)
            free = round(usage.free / (1024 ** 3), 1)
            disk_infos.append({
                'path': path,
                'total': f"{total}G",
                'used': f"{used}G",
                'free': f"{free}G",
                'disk_percent': percent
            })
            if percent > threshold:
                alerts.append(f"{path} The disk usage is too high：current {percent}%，threshold {threshold}%")
            self.used_hbdata = max(self.used_hbdata, used)
            self.percent_hbdata = max(self.percent_hbdata, percent)
            self.disk_infos_hbdata=[{
                'path': path,
                'total': f"{total}G",
                'used': f"{self.used}G",
                'free': f"{round(total - self.used, 1)}G",
                'disk_percent': self.percent_hbdata
            }]
        return disk_infos, alerts

    def get_total_cpu_usage(self, threshold=90):
        percent = round(psutil.cpu_percent(interval=1), 2)
        self.max_cpu = max(self.max_cpu, percent)
        alert = percent > threshold
        return percent, threshold, alert

    def send_ad(self, df, description):
        title = f'monitor server processes ({self.running_mode})'
        if df.empty:
            blocks = []
            blocks.append(self.chat_app.get_title_block(title=title))
            blocks.append(self.chat_app.get_description_block(description))
            self.chat_app.send_message(blocks)
        else:
            report_block = self.chat_app.get_fully_df_report(df=df, description=description, title=title)
            self.chat_app.send_message(report_block)


    def run_every_minute(self,  onehour=False):
        disk_infos, disk_alerts = self.get_disk_usage()
        disk_infos_hbdata, disk_alerts_hbdata = self.get_disk_usage_hbdata()
        cpu_percent, cpu_threshold, cpu_alert = self.get_total_cpu_usage()
        memory_percent, memory_threshold, memory_alert = self.get_memory_usage()

        alerts = disk_alerts
        alerts_hbdata = disk_alerts_hbdata
        if cpu_alert:
            alerts.append(f"CPU total utilization is too high：{cpu_percent}%，threshold {cpu_threshold}%")
        if memory_alert:
            alerts.append(f"Memory usage too high：{memory_percent}%，threshold {memory_threshold}%")

        disk_df = pd.DataFrame(disk_infos)
        disk_df_hbdata = pd.DataFrame(disk_alerts_hbdata)

        if alerts:
            alerts.append(
                f"\n")
            self.send_ad(df=disk_df, description=alerts)
        if alerts_hbdata:
            alerts.append(
                f"\n")
            self.send_ad(df=disk_df_hbdata, description=alerts_hbdata)
        if onehour:
            description = [f"CPU Total utilization rate：current {cpu_percent}%，threshold {cpu_threshold}%\n"
                           f"Memory usage rate：current {memory_percent}%，threshold {memory_threshold}%\n"]
            self.send_ad(df=disk_df, description=description)

    def run_every_day(self):
        description = [f"One-day max CPU usage：{self.max_cpu}%\n"
                       f"One-day max memory usage：{self.max_memory}%\n"
                       f"One-day max disk_use: {self.used}G, max disk_percent: {self.percent}%\n"
                       f"One-day max hbdata_disk_use: {self.used_hbdata}G, max hbdata_disk_percent: {self.percent_hbdata}%\n"]
        df = pd.DataFrame()
        self.send_ad(df=df, description=description)

        # reset
        self.max_cpu = 0
        self.max_memory = 0
        self.used = 0
        self.percent = 0
        self.disk_infos=[]
        self.used_hbdata = 0
        self.percent_hbdata = 0
        self.disk_infos_hbdata = []


if __name__ == '__main__':
    config0 = ConfigAgent(find_project_root(__file__))
    running_mode = RunningMode.production_flag.name
    monitor = SystemMonitor(config=config0, running_mode=running_mode)

    if running_mode != RunningMode.development_flag.name:
        sched = BlockingScheduler()
        sched.add_job(monitor.run_every_minute, 'cron', minute='*')
        sched.add_job(monitor.run_every_day, 'cron', huor=0, minute=10)
        sched.start()
    else:
        # 手动运行测试
        monitor.run_every_minute(onehour=True)
        monitor.run_every_day()

