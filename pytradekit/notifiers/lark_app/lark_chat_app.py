import requests
import json
from pandas import DataFrame


class LarkChatApp:
    """
    Lark Webhook Sender (飞书机器人推送模块)
    """

    def __init__(self, webhook_url, logger=None):
        self.webhook_url = webhook_url
        self.logger = logger

    # ======== Block 构建工具 ========= #

    @staticmethod
    def get_title_block(title):
        return {
            "tag": "markdown",
            "content": f"**{title}**"
        }

    @staticmethod
    def get_description_block(description: list):
        desc = "\n".join(description)
        return {
            "tag": "markdown",
            "content": desc
        }

    @staticmethod
    def df_to_markdown(df: DataFrame):
        """将 df 转 Markdown 格式"""
        return df.to_markdown(index=False)

    def get_df_block(self, df: DataFrame):
        md = self.df_to_markdown(df)
        return {
            "tag": "markdown",
            "content": f"```\n{md}\n```"
        }

    def get_fully_df_report(self, df: DataFrame, description: list = None, title: str = None):
        """
        返回飞书卡片用的 card.elements
        """
        elements = []

        if title:
            elements.append(self.get_title_block(title))

        elements.append(self.get_df_block(df))

        if description:
            elements.append(self.get_description_block(description))

        return elements

    # ======== 发送消息 =========== #

    def send_message(self, elements: list):
        """
        发送 Lark card 消息
        """
        data = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "elements": elements
            }
        }

        try:
            res = requests.post(
                self.webhook_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(data)
            ).json()

            if res.get("code", 0) != 0:
                raise Exception(f"Lark API Error: {res}")

            return res
        except Exception as e:
            if self.logger:
                self.logger.exception(e)
            print("Lark send_message error:", e)
            raise e

    # ======== 图片发送（Webhook 不支持） ======== #
    def send_report_image(self, image_path):
        """
        Webhook 无法上传文件，这里直接报错。
        如需上传，请改用 Lark OpenAPI。
        """
        raise NotImplementedError("Lark Webhook 不支持图片上传，请使用 Lark OpenAPI token 版本。")
