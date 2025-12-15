import logging
import time
import psutil
import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from pytradekit.utils.config_agent import ConfigAgent
from pytradekit.utils.tools import find_project_root
from pytradekit.utils.dynamic_types import RunningMode
from pytradekit.lark_app.lark_chat_app import LarkChatApp
from pytradekit.utils.tools import encrypt_decrypt

CPUMAX = 400
MEMORYMAX = 30
ALLCPUMAX = 100
ALLCPUMAXQUANTITY = 10
ALLMEMORYMAX = 50


def send_ad(chat_app, df, description, running_mode):
    title = f'monitor server processes ({running_mode})'
    report_block = chat_app.get_fully_df_report(df=df, description=description, title=title, is_save_data_flag=False)
    chat_app.send_message(report_block)


def extract_python_file(cmdline):
    parts = cmdline.split()
    if "-m" in parts:
        module_path = parts[parts.index("-m") + 1]
        file_name = module_path.split(".")[-1]
        return file_name
    return None


def get_process_info(chat_app, running_mode):
    process_infos = []
    all_memory = 0
    cpu_exc = 0
    for proc in psutil.process_iter(['pid', 'cpu_percent', 'memory_percent']):
        try:
            cmdline = ' '.join(proc.cmdline())
            if 'python' in cmdline or 'mongo' in cmdline or 'redis' in cmdline:
                proc.cpu_percent(interval=None)
                time.sleep(0.3)

                pid = proc.info['pid']
                cpu = proc.cpu_percent(interval=None)
                memory = proc.info['memory_percent']
                memory_formatted = round(memory, 2)
                process_info = {
                    'CPU (%)': cpu,
                    'Memory (%)': memory_formatted,
                    'Command': cmdline,
                    'PID': pid
                }
                if memory >= MEMORYMAX:
                    df_info = pd.DataFrame([process_info])
                    send_ad(chat_app, df_info, description=[
                        f'memory oversize\n'],
                            running_mode=running_mode)
                all_memory += memory
                if cpu > ALLCPUMAX:
                    cpu_exc += 1
                if cmdline.startswith('python') and cmdline != 'python3':
                    python_file = extract_python_file(cmdline)
                    process_info1 = {
                        'CPU(%)': cpu,
                        'Memory (%)': memory_formatted,
                        'Command': python_file,
                        'PID': pid
                    }
                    process_infos.append(process_info1)
                if (
                        cmdline.startswith('mongo -u mvid_trader') or
                        cmdline.startswith('redis-server *:6379') or
                        cmdline.startswith('mongod --config /etc/mongod.conf')
                ):
                    process_info1 = {
                        'CPU(%)': cpu,
                        'Memory (%)': memory_formatted,
                        'Command': cmdline,
                        'PID': pid
                    }
                    process_infos.append(process_info1)

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    process_infos.sort(key=lambda x: x['Memory (%)'], reverse=True)
    df = pd.DataFrame(process_infos)
    return all_memory, cpu_exc, df


def run(config, onehour=False, running_mode=None):
    chat_app = LarkChatApp(webhook_url='https://open.larksuite.com/open-apis/bot/v2/hook/d38a337c-11c2-4910-8b8b-9aa3518caf25')
    all_memory, cpu_exc, df = get_process_info(chat_app, running_mode)
    if all_memory > ALLMEMORYMAX:
        send_ad(chat_app, df, description=[
            f'all_memory exceeds 50, all_memory: {all_memory}\n'],
                running_mode=running_mode)
    if cpu_exc > ALLCPUMAXQUANTITY:
        send_ad(chat_app, df, description=[
            f'cpu > 100%  exceeds 10\n'],
                running_mode=running_mode)  # TODO at user
    if onehour:
        send_ad(chat_app, df, description=[], running_mode=running_mode)


if __name__ == '__main__':
    config0 = ConfigAgent(find_project_root(__file__))
    running_mode = RunningMode.production_flag.name
    if running_mode != RunningMode.development_flag.name:
        sched = BlockingScheduler()
        #sched.add_job(run, 'cron', minute='*', second='0', args=(config0, False,running_mode))
        sched.add_job(run, 'cron', minute='0', hour='0,12', args=(config0, True, running_mode))
        sched.start()
    else:
        run(config0, True, running_mode)
