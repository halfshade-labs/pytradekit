import json
from typing import Any, Dict

import requests

from pytradekit.utils.tools import encrypt_decrypt


def send_lark_card(webhook_url: str, card: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    """
    Send a Lark interactive card to the given webhook.

    Args:
        webhook_url: Lark bot webhook URL
        card: Interactive card payload (dict)
        timeout: HTTP timeout in seconds

    Returns:
        Parsed JSON response from Lark server
    """
    payload = {
        "msg_type": "interactive",
        "card": card,
    }

    response = requests.post(
        webhook_url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def send_lark_card_with_config(
    config: Any,
    logger: Any,
    card: Dict[str, Any],
    private_key: str,
) -> None:
    """
    Send a Lark interactive card using webhook stored in private config (.env).

    This helper is generic: the caller provides the private key name
    used in ConfigAgent.private (e.g. 'DEVELOPMENT_OUTPUT_LARK').

    Args:
        config: ConfigAgent instance (or compatible object with `private` dict)
        logger: Logger instance
        card: Lark interactive card payload
        private_key: Key in `config.private` that stores encrypted webhook
    """
    try:
        private_cfg = getattr(config, "private", None)
        webhook_cipher = private_cfg.get(private_key) if private_cfg else None
        if not webhook_cipher:
            logger.debug(f"{private_key} not found in private config, skip sending Lark card")
            return

        webhook_url = encrypt_decrypt(webhook_cipher, "decrypt")
        response = send_lark_card(webhook_url, card)
        logger.debug(f"Sent Lark card, response: {response}")
    except Exception as exc:
        logger.warning(f"Failed to send Lark card: {exc}")


def send_lark_markdown_report(
    config: Any,
    logger: Any,
    title: str,
    markdown: str,
    private_key: str,
) -> None:
    """
    Build and send a simple markdown report as Lark interactive card.

    Args:
        config: ConfigAgent instance
        logger: Logger instance
        title: Card header title
        markdown: Markdown body content
        private_key: Key in `config.private` that stores encrypted webhook
    """
    card: Dict[str, Any] = {
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "template": "turquoise",
            "title": {
                "tag": "plain_text",
                "content": title,
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": markdown,
            },
        ],
    }
    send_lark_card_with_config(config, logger, card, private_key=private_key)
