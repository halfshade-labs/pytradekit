from pytradekit.utils.mongodb_operations import MongodbOperations


# 测试MongoDB客户端的创建
def test_create_client(mocker):
    mongodb_url = "mongodb://username:password@localhost:27017"
    spy = mocker.spy(MongodbOperations, '_create_client')
    # 初始化MongodbOperations实例以触发_create_client调用
    MongodbOperations(mongodb_url)
    # 现在使用spy对象来断言_create_client是否被正确调用
    spy.assert_called_once_with(mongodb_url)


# 测试关闭MongoDB连接
def test_close():
    mongodb_url = "mongodb://username:password@localhost:27017"
    mongodb_operations = MongodbOperations(mongodb_url)
    mongodb_operations.close()
    assert MongodbOperations._client is None


def test_check_connection(mocker):
    mongodb_url = "mongodb://username:password@localhost:27017"
    # 创建MongoClient的模拟实例
    mocked_mongo_client = mocker.Mock()
    # 创建admin属性的模拟对象
    mocked_admin = mocker.Mock()
    # 将mocked_admin设置为mocked_mongo_client的admin属性的返回值
    mocked_mongo_client.admin = mocked_admin
    # 设置admin.command方法的返回值为True
    mocked_admin.command.return_value = True

    # 使用patch替换MongoClient，使其返回模拟的MongoClient实例
    mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_mongo_client)

    mongodb_operations_instance = MongodbOperations(mongodb_url)
    # 调用待测试的方法
    connection_status = mongodb_operations_instance._check_connection()

    # 验证admin.command方法是否被正确调用
    mocked_admin.command.assert_called_once_with('ping')
    # 验证返回值是否为True，即连接正常
    assert connection_status is True
