import os

from pandas import DataFrame

from slack_sdk import WebClient

from pytradekit.utils.exceptions import DependencyException
from pytradekit.utils.tools import save_report_df_csv
from pytradekit.utils.dynamic_types import SlackUser


class AtUser:
    debug = f''


class ChatApp:

    def __init__(self, channel_id, token=None, logger=None):
        self.channel_id = channel_id
        self.client = WebClient(token=token)
        self.logger = logger

    @staticmethod
    def get_title_block(title):
        block = {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": title,
                "emoji": True
            }
        }
        return block

    @staticmethod
    def get_context_block(elements):
        block = {
            "type": "context",
            "elements": elements
        }
        return block

    @staticmethod
    def get_plain_text_block(text):
        block = {
            "type": "plain_text",
            "text": text
        }
        return block

    @staticmethod
    def get_mardown_block(text, emoji=False):
        block = {
            "type": "mrkdwn",
            "text": text,
        }
        if emoji:
            block.update({"emoji": True})
        return block

    @staticmethod
    def get_description_block(description: list):
        description = "\n".join(description)
        block = {"type": "section",
                 "text": {"type": "mrkdwn", "text": f"{description}"}}
        return block

    def get_df_block(self, df: DataFrame):
        """
        df 没有index，如需保留index信息，reset index作为普通的列
        """
        assert df.shape[1] <= 10
        data_elements = []
        for col in df.columns:
            text = f"*{col}*\n" + "\n".join(df[col].astype(str))
            data_elements.append(self.get_mardown_block(text))
        block = self.get_context_block(data_elements)
        return block

    def get_fully_df_report(self, df: DataFrame, description: list = None, title: str = None, is_save_data_flag=True):
        blocks = []
        if title:
            blocks.append(self.get_title_block(title))
        blocks.append(self.get_df_block(df))
        if description:
            blocks.append(self.get_description_block(description))
        if is_save_data_flag:
            try:
                save_report_df_csv(title, df)
            except Exception as e:
                self.logger.info(f"save report df error: {e} {AtUser.debug}")
        return blocks

    def send_message(self, blocks: list, main_thread_ts=None):
        try:
            if main_thread_ts:
                result = self.client.chat_postMessage(
                    text="report bot",
                    channel=self.channel_id,
                    blocks=blocks,
                    thread_ts=main_thread_ts)
            else:
                result = self.client.chat_postMessage(
                    text="report bot",
                    channel=self.channel_id,
                    blocks=blocks)
            return result['ts']
        except Exception as e:
            if self.logger:
                self.logger.exception(e)
            else:
                print(e)
            raise DependencyException(f"failed to send message to channel: {self.channel_id}") from e

    def send_report_image(self, image_path, main_thread_ts=None):
        try:
            if main_thread_ts:
                result = self.client.files_upload(
                    channels=self.channel_id,
                    thread_ts=main_thread_ts,
                    file=image_path
                )
            else:
                result = self.client.files_upload(
                    text="report bot",
                    channels=self.channel_id,
                    file=image_path
                )
            return result['file']['shares']['private'][self.channel_id][0]['ts']
        except Exception as e:
            if self.logger:
                self.logger.exception(e)
            else:
                print(e)
            raise DependencyException(f"failed to send image to channel: {self.channel_id}") from e

    def get_chat_history(self, limit=5):
        try:
            result = self.client.conversations_history(
                channel=self.channel_id,
                inclusive=True,
                limit=limit
            )
            return result.data['messages']
        except Exception as e:
            if self.logger:
                self.logger.exception(e)
            else:
                print(e)
            raise DependencyException(f"failed to get chat history from channel: {self.channel_id}") from e

    def delete_chat_history(self, ts):
        try:
            result = self.client.chat_delete(
                channel=self.channel_id,
                ts=ts
            )
            return result
        except Exception as e:
            if self.logger:
                self.logger.exception(e)
            else:
                print(e)
            raise DependencyException(f"failed to delete chat history from channel: {self.channel_id}") from e

    def delete_multiple_msg_in_last_thread(self, limit=2):
        try:
            for i in range(limit):
                print(i)
                res = self.get_chat_history(1)
                ts = res[0]['latest_reply']
                self.delete_chat_history(ts)
            res = self.get_chat_history(1)
            ts = res[0]['ts']
            self.delete_chat_history(ts)
        except Exception as e:
            if self.logger:
                self.logger.exception(e)
            else:
                print(e)
            # return DependencyException(f"Failed to delete messages in last thread  in channel: {self.channel_id}, {e}")


if __name__ == "__main__":
    token = ''
    channel_id = ''
    chat_app = ChatApp(channel_id, token)
    print(chat_app.get_chat_history())
    # print(chat_app.delete_chat_history('ts'))
