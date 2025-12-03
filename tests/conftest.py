import os
import sys
import pytest
import logging
from io import StringIO

# 添加项目根目录到 Python 路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

@pytest.fixture
def logger():
    """创建一个用于测试的logger fixture"""
    logger = logging.getLogger('test_logger')
    logger.setLevel(logging.INFO)
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    logger.addHandler(handler)
    return logger, log_stream 